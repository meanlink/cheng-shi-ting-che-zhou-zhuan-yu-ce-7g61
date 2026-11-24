from datetime import datetime, time, timedelta, timezone
import json

from .analytics import seasonal_moving_average


def _upsert_lot(conn, code, name, district, capacity, zones, occupancy):
    row = conn.execute('''INSERT INTO parking_lot(code,name,district,capacity,zones,current_occupancy)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,district=EXCLUDED.district,
        capacity=EXCLUDED.capacity,zones=EXCLUDED.zones,current_occupancy=EXCLUDED.current_occupancy
        RETURNING id''', (code, name, district, capacity, zones, occupancy)).fetchone()
    return (row or conn.execute('SELECT id FROM parking_lot WHERE code=%s', (code,)).fetchone())['id']


def _observations(conn, lot_id):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc).date()
    for i in range(22):
        day = start + timedelta(days=i)
        morning = 64 + (i % 7) * 7 + (16 if day.weekday() >= 5 else 0)
        evening = 76 + ((i + 2) % 7) * 6 + (20 if day.weekday() >= 5 else 0)
        observed_at = datetime.combine(day, time(22, 0), tzinfo=timezone.utc)
        conn.execute('''INSERT INTO interval_observation
            (lot_id,observed_at,entry_count,exit_count,occupancy,source_file,source_row,entered_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(lot_id,observed_at,source_file,source_row) DO NOTHING''',
            (lot_id, observed_at, morning, evening, 90 + (i * 4) % 80, '停车闸机_2026-11.csv',
             f'PARK-19-{day.isoformat()}', 'analyst@metro.demo'))


def seed_handover(conn, lot_ids):
    day = datetime.now(timezone.utc).date() + timedelta(days=1)
    source_id = _upsert_lot(conn, 'PARK-19', '北辰立交备用停车场', '北城区', 300,
                            ['地面层', '夹层'], 88)
    lot_ids['PARK-19'] = source_id
    target_id = lot_ids['PARK-12']

    _observations(conn, source_id)
    forecast_code = f'PF-RELAY-{day:%Y%m%d}'
    rows = conn.execute('''SELECT observed_at,entry_count,exit_count FROM interval_observation
        WHERE lot_id=%s ORDER BY observed_at''', (source_id,)).fetchall()
    predictions = seasonal_moving_average(rows, day, 3)
    conn.execute('''INSERT INTO forecast_plan (code,lot_id,horizon_start,horizon_days,capacity_snapshot,
        status,calendar_complete,capacity_change_confirmed,predictions,model_version,created_by,reviewed_by,review_note)
        SELECT %s,l.id,%s,3,l.capacity,'published',true,true,%s,'seasonal-moving-average-v1',
        'analyst@metro.demo','supervisor@metro.demo','分流复盘演示用已发布预测' FROM parking_lot l WHERE l.id=%s
        ON CONFLICT(code) DO NOTHING''', (forecast_code, day, json.dumps(predictions), source_id))
    turnover = next(point['turnover'] for point in predictions if point['date'] == day.isoformat())

    for suffix, blocked, spaces in (('A', 30, 14), ('B', 20, 10)):
        closure_ref = f'WL-RELAY-{suffix}-19'
        conn.execute('''INSERT INTO capacity_closure
            (reference,lot_id,starts_on,ends_on,blocked_spaces,reason,status,created_by,approved_by)
            VALUES (%s,%s,%s,%s,%s,%s,'approved','operator@metro.demo','supervisor@metro.demo')
            ON CONFLICT (reference) DO NOTHING''',
            (closure_ref, source_id, day, day, blocked, f'{suffix} 区匝道维护，分流车辆由接收场清点'))
        closure = conn.execute('SELECT id FROM capacity_closure WHERE reference=%s', (closure_ref,)).fetchone()
        forecast = conn.execute('SELECT id FROM forecast_plan WHERE code=%s', (forecast_code,)).fetchone()
        conn.execute('''INSERT INTO parking_diversion
            (closure_id,forecast_id,target_lot_id,service_day,reserved_spaces,forecast_turnover,
             target_headroom_at_activation,dispatch_note,status,created_by,activated_by)
            SELECT %s,%s,%s,%s,%s,%s,110,%s,'active','operator@metro.demo','operator@metro.demo'
            WHERE NOT EXISTS (SELECT 1 FROM parking_diversion WHERE closure_id=%s)''',
            (closure['id'], forecast['id'], target_id, day, spaces, turnover,
             f'{suffix} 线分流：门岗按交班回执向江畔文体中心移交车辆', closure['id']))

    target = conn.execute('SELECT id FROM parking_lot WHERE id=%s', (target_id,)).fetchone()
    receipt_ref = f'HD-PARK12-{day:%Y%m%d}'
    receipt = conn.execute('''INSERT INTO handover_receipt (lot_id,service_day,reference,total_vehicles,reason,created_by)
        VALUES (%s,%s,%s,36,'接收场当天交班清点，两张分流单共用','analyst@metro.demo')
        ON CONFLICT (lot_id,service_day) DO NOTHING RETURNING id''',
        (target['id'], day, receipt_ref)).fetchone()
    if receipt is None:
        receipt = conn.execute('SELECT id FROM handover_receipt WHERE lot_id=%s AND service_day=%s',
                               (target['id'], day)).fetchone()
    diversions = conn.execute('''SELECT d.id, d.reserved_spaces FROM parking_diversion d
        JOIN capacity_closure c ON c.id=d.closure_id
        WHERE c.reference IN ('WL-RELAY-A-19','WL-RELAY-B-19') AND d.service_day=%s
        ORDER BY c.reference''', (day,)).fetchall()
    for index, diversion in enumerate(diversions, start=1):
        conn.execute('''INSERT INTO diversion_claim (receipt_id,diversion_id,receipt_no,claimed_vehicles,created_by)
            SELECT %s,%s,%s,%s,'analyst@metro.demo'
            WHERE NOT EXISTS (SELECT 1 FROM diversion_claim WHERE receipt_id=%s AND receipt_no=%s)''',
            (receipt['id'], diversion['id'], f'RC-{day:%Y%m%d}-{index:02d}',
             diversion['reserved_spaces'], receipt['id'], f'RC-{day:%Y%m%d}-{index:02d}'))
    return receipt['id']
