from fastapi import HTTPException

from .db import connect
from .operations import fetch
from .workflow import audit

RECEIPT_SELECT = '''SELECT r.*, l.code AS lot_code, l.name AS lot_name,
    coalesce(t.claimed_total, 0) AS claimed_total,
    coalesce(t.held_total, 0) AS held_total,
    coalesce(t.occupied_total, 0) AS occupied_total,
    r.total_vehicles - coalesce(t.occupied_total, 0) AS remaining_vehicles
    FROM handover_receipt r JOIN parking_lot l ON l.id=r.lot_id
    LEFT JOIN LATERAL (
        SELECT sum(claimed_vehicles) AS claimed_total,
               sum(claimed_vehicles) FILTER (WHERE status='held') AS held_total,
               sum(claimed_vehicles) FILTER (WHERE status IN ('held','settled')) AS occupied_total
        FROM diversion_claim WHERE receipt_id=r.id
    ) t ON TRUE'''

CLAIM_SELECT = '''SELECT k.*, d.service_day, d.reserved_spaces, d.status AS diversion_status,
    source.name AS source_name, target.name AS target_name, l.code AS lot_code, l.name AS lot_name,
    r.reference AS receipt_reference
    FROM diversion_claim k
    JOIN handover_receipt r ON r.id=k.receipt_id
    JOIN parking_diversion d ON d.id=k.diversion_id
    JOIN capacity_closure c ON c.id=d.closure_id
    JOIN parking_lot source ON source.id=c.lot_id
    JOIN parking_lot target ON target.id=d.target_lot_id
    JOIN parking_lot l ON l.id=r.lot_id'''


def list_receipts():
    with connect() as conn:
        return conn.execute(RECEIPT_SELECT + ' ORDER BY r.service_day DESC, r.id DESC').fetchall()


def list_claims():
    with connect() as conn:
        return conn.execute(CLAIM_SELECT + ' ORDER BY k.id DESC').fetchall()


def register_receipt(item, actor):
    with connect() as conn:
        lot = fetch(conn, 'SELECT * FROM parking_lot WHERE code=%s', (item.lot_code,), '接收停车场不存在')
        if lot['status'] != 'active':
            raise HTTPException(422, '交班单仅可登记在运营中的接收场')
        created = conn.execute('''INSERT INTO handover_receipt
            (lot_id,service_day,reference,total_vehicles,reason,created_by)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (lot_id,service_day) DO NOTHING RETURNING id''',
            (lot['id'], item.service_day, item.reference, item.total_vehicles, item.reason, actor)).fetchone()
        if created is None:
            raise HTTPException(409, '该接收场当天已登记交班单，请在原单上补正总数')
        audit(conn, actor, 'handover_receipt', created['id'], 'registered',
              f'{item.lot_code} · {item.service_day} · {item.reference} · 实收 {item.total_vehicles} 辆')
        return conn.execute(RECEIPT_SELECT + ' WHERE r.id=%s', (created['id'],)).fetchone()


def correct_total(receipt_id, item, actor):
    with connect() as conn:
        # 与认领同一把行锁串行化，避免补正与认领同时发生导致超领。
        row = fetch(conn, 'SELECT * FROM handover_receipt WHERE id=%s FOR UPDATE',
                    (receipt_id,), '交班单不存在')
        # 占用中与已结案的份额都继续占用交班总数，只有已释放的份额回到可领池。
        occupied = conn.execute('''SELECT coalesce(sum(claimed_vehicles),0) AS occupied
            FROM diversion_claim WHERE receipt_id=%s AND status IN ('held','settled') ''',
            (receipt_id,)).fetchone()['occupied']
        if item.total_vehicles < occupied:
            raise HTTPException(422, f'新总数 {item.total_vehicles} 辆低于当前已认领占用 {occupied} 辆，请先退回相关复盘释放份额')
        conn.execute('''UPDATE handover_receipt SET total_vehicles=%s,updated_by=%s,updated_at=now()
            WHERE id=%s''', (item.total_vehicles, actor, receipt_id))
        audit(conn, actor, 'handover_receipt', receipt_id, 'corrected',
              f'{row["reference"]} · 总数 {row["total_vehicles"]} → {item.total_vehicles} 辆 · 已认领占用 {occupied} 辆')
        return conn.execute(RECEIPT_SELECT + ' WHERE r.id=%s', (receipt_id,)).fetchone()


def claim_vehicles(item, actor):
    with connect() as conn:
        # 先锁交班单：同一接收场当天的全部认领在此排队，并发提交也不会超领。
        receipt = fetch(conn, 'SELECT * FROM handover_receipt WHERE id=%s FOR UPDATE',
                        (item.receipt_id,), '交班单不存在')
        # 回执幂等：丢包重试时同一回执号直接返回原认领，绝不二次扣减。
        previous = conn.execute('SELECT * FROM diversion_claim WHERE receipt_id=%s AND receipt_no=%s',
                                (item.receipt_id, item.receipt_no)).fetchone()
        if previous:
            return conn.execute(CLAIM_SELECT + ' WHERE k.id=%s', (previous['id'],)).fetchone()

        diversion = fetch(conn, '''SELECT d.*, c.lot_id AS source_id FROM parking_diversion d
            JOIN capacity_closure c ON c.id=d.closure_id WHERE d.id=%s FOR UPDATE OF d''',
                          (item.diversion_id,), '分流单不存在')
        if diversion['target_lot_id'] != receipt['lot_id'] or diversion['service_day'] != receipt['service_day']:
            raise HTTPException(422, '分流单的接收场或运营日与交班单不一致，不能认领')
        if diversion['status'] != 'active':
            raise HTTPException(409, '仅已启用、尚未结案的分流单可认领车数')

        occupied = conn.execute('''SELECT coalesce(sum(claimed_vehicles),0) AS occupied
            FROM diversion_claim WHERE receipt_id=%s AND status IN ('held','settled') ''',
            (item.receipt_id,)).fetchone()['occupied']
        if occupied + item.claimed_vehicles > receipt['total_vehicles']:
            remaining = receipt['total_vehicles'] - occupied
            raise HTTPException(409, f'交班单剩余可领 {remaining} 辆，本次申请 {item.claimed_vehicles} 辆，不能超领')

        row = conn.execute('''INSERT INTO diversion_claim
            (receipt_id,diversion_id,receipt_no,claimed_vehicles,created_by)
            VALUES (%s,%s,%s,%s,%s) RETURNING *''',
            (item.receipt_id, item.diversion_id, item.receipt_no, item.claimed_vehicles, actor)).fetchone()
        audit(conn, actor, 'diversion_claim', row['id'], 'claimed',
              f'{item.receipt_no} · 分流 #{item.diversion_id} · 认领 {item.claimed_vehicles} 辆 · {receipt["reference"]}')
        return conn.execute(CLAIM_SELECT + ' WHERE k.id=%s', (row['id'],)).fetchone()


def settle_claims(conn, diversion_id, accepted, actor, note):
    """复盘审核联动：通过则份额结案继续占用；退回则释放认领，总数重新可领。"""
    if accepted:
        settled = conn.execute('''UPDATE diversion_claim SET status='settled'
            WHERE diversion_id=%s AND status='held' RETURNING id,claimed_vehicles''', (diversion_id,)).fetchall()
        for row in settled:
            audit(conn, actor, 'diversion_claim', row['id'], 'settled',
                  f'复盘通过 · 结案占用 {row["claimed_vehicles"]} 辆 · {note}')
        return len(settled)
    released = conn.execute('''UPDATE diversion_claim SET status='released', released_at=now(), release_note=%s
        WHERE diversion_id=%s AND status='held' RETURNING id,claimed_vehicles''', (note, diversion_id)).fetchall()
    for row in released:
        audit(conn, actor, 'diversion_claim', row['id'], 'released',
              f'复盘退回 · 释放 {row["claimed_vehicles"]} 辆 · {note}')
    return len(released)
