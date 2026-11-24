"""分流复盘 · 交班认领端到端测试（需要本地 PG16 + Redis，环境变量由 CI 注入）。"""
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('PGHOST', '/tmp')
os.environ.setdefault('PGPORT', '15432')
os.environ.setdefault('PGDATABASE', 'parking')
os.environ.setdefault('PGUSER', 'parking')
os.environ.setdefault('PGPASSWORD', 'unused')
os.environ.setdefault('REDIS_URL', 'redis://localhost:16379/0')
os.environ['SEED_DEMO'] = 'false'

from src.app import app  # noqa: E402
from src.db import connect, redis_client  # noqa: E402
from src.handover_input import DiversionClaimIn  # noqa: E402
from src import handover  # noqa: E402

PASSWORD = 'ParkingDemo26!'


@pytest.fixture(scope='session')
def client():
    from src.db import migrate
    migrate()
    # 清掉上轮测试数据，保证套件可在同一数据库上重复运行。
    with connect() as conn:
        conn.execute('''DELETE FROM diversion_claim; DELETE FROM handover_receipt;
            DELETE FROM diversion_settlement; DELETE FROM parking_diversion;
            DELETE FROM capacity_closure WHERE reference LIKE 'WL-T-%';
            DELETE FROM forecast_plan WHERE code LIKE 'PF-T-%';
            DELETE FROM interval_observation WHERE source_file='test-handover.csv';''')
        conn.commit()
    from scripts.seed_demo import seed
    seed()
    redis_client().flushdb()
    return TestClient(app)


def login(client, email):
    response = client.post('/auth/login', json={'email': email, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()['access_token']
    return {'Authorization': f'Bearer {token}'}


@pytest.fixture(scope='session')
def analyst(client):
    return login(client, 'analyst@metro.demo')


@pytest.fixture(scope='session')
def supervisor(client):
    return login(client, 'supervisor@metro.demo')


def sql(query, args=()):
    with connect() as conn:
        return conn.execute(query, args).fetchall()


def unique(prefix):
    return f'{prefix}-{datetime.now(timezone.utc):%H%M%S%f}'


_day_counter = {'n': 0}


def make_receipt(client, headers, lot_code='PARK-12', total=36, service_day=None):
    _day_counter['n'] += 1
    day = service_day or (datetime.now(timezone.utc).date() - timedelta(days=2 + _day_counter['n']))
    payload = {'lot_code': lot_code, 'service_day': day.isoformat(), 'reference': unique('HD-T'),
               'total_vehicles': total, 'reason': '测试交班清点，覆盖同场同日多单认领'}
    response = client.post('/handover/receipts', json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json(), day


def make_active_diversion(day, target_code='PARK-12', spaces=10):
    """直接构造一张当日已启用分流单（仅测试夹具用）。"""
    with connect() as conn:
        target = conn.execute('SELECT id FROM parking_lot WHERE code=%s', (target_code,)).fetchone()
        source = conn.execute("SELECT id FROM parking_lot WHERE code='PARK-19'").fetchone()
        forecast = conn.execute('SELECT id FROM forecast_plan WHERE lot_id=%s LIMIT 1',
                                (source['id'],)).fetchone()
        closure = conn.execute('''INSERT INTO capacity_closure
            (reference,lot_id,starts_on,ends_on,blocked_spaces,reason,status,created_by,approved_by)
            VALUES (%s,%s,%s,%s,12,'测试封控窗口','approved','operator@metro.demo','supervisor@metro.demo')
            RETURNING id''', (unique('WL-T'), source['id'], day, day)).fetchone()
        row = conn.execute('''INSERT INTO parking_diversion
            (closure_id,forecast_id,target_lot_id,service_day,reserved_spaces,forecast_turnover,
             target_headroom_at_activation,dispatch_note,status,created_by,activated_by)
            VALUES (%s,%s,%s,%s,%s,100,200,'测试分流单','active','operator@metro.demo','operator@metro.demo')
            RETURNING id''', (closure['id'], forecast['id'], target['id'], day, spaces)).fetchone()
        conn.commit()
        return row['id']


# ---------------------------------------------------------------- 登记与唯一性

def test_receipt_register_is_unique_per_lot_day(client, analyst):
    first, day = make_receipt(client, analyst, total=18)
    assert first['claimed_total'] == 0 and first['remaining_vehicles'] == 18
    dup = client.post('/handover/receipts', json={
        'lot_code': 'PARK-12', 'service_day': day.isoformat(), 'reference': unique('HD-DUP'),
        'total_vehicles': 30, 'reason': '重复登记应当被拒绝'}, headers=analyst)
    assert dup.status_code == 409
    assert '已登记交班单' in dup.json()['detail']


def test_operator_cannot_register_receipt(client):
    headers = login(client, 'operator@metro.demo')
    day = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    response = client.post('/handover/receipts', json={
        'lot_code': 'PARK-12', 'service_day': day, 'reference': unique('HD-OP'),
        'total_vehicles': 5, 'reason': '现场岗位无权登记交班单'}, headers=headers)
    assert response.status_code == 403


# ---------------------------------------------------------------- 认领幂等与超领

def test_claim_idempotent_receipt_retry_does_not_double_deduct(client, analyst):
    receipt, day = make_receipt(client, analyst, total=20)
    diversion = make_active_diversion(day)
    payload = {'receipt_id': receipt['id'], 'diversion_id': diversion,
               'receipt_no': unique('RC'), 'claimed_vehicles': 8}
    first = client.post('/handover/claims', json=payload, headers=analyst)
    assert first.status_code == 201, first.text
    # 回执丢失后重试：换数量也必须返回原记录，不能二次扣减。
    retry = client.post('/handover/claims', json={**payload, 'claimed_vehicles': 15}, headers=analyst)
    assert retry.status_code == 201
    assert retry.json()['id'] == first.json()['id']
    assert retry.json()['claimed_vehicles'] == 8
    refreshed = next(row for row in client.get('/handover/receipts', headers=analyst).json()
                     if row['id'] == receipt['id'])
    assert refreshed['held_total'] == 8 and refreshed['remaining_vehicles'] == 12
    receipt_claims = [c for c in client.get('/handover/claims', headers=analyst).json()
                      if c['receipt_id'] == receipt['id']]
    assert len(receipt_claims) == 1


def test_claim_cannot_exceed_remaining(client, analyst):
    receipt, day = make_receipt(client, analyst, total=10)
    diversion = make_active_diversion(day, spaces=99)
    response = client.post('/handover/claims', json={
        'receipt_id': receipt['id'], 'diversion_id': diversion,
        'receipt_no': unique('RC-BIG'), 'claimed_vehicles': 11}, headers=analyst)
    assert response.status_code == 409
    assert '不能超领' in response.json()['detail']


def test_concurrent_claims_never_over_commit(client, analyst):
    receipt, day = make_receipt(client, analyst, total=30)
    diversions = [make_active_diversion(day) for _ in range(2)]
    errors, results = [], []

    def worker(diversion_id, amount, index):
        try:
            item = DiversionClaimIn(receipt_id=receipt['id'], diversion_id=diversion_id,
                                    receipt_no=f'CC-{day}-{index}-{unique("")}', claimed_vehicles=amount)
            results.append(handover.claim_vehicles(item, 'analyst@metro.demo'))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(diversions[0], 20, 1)),
        threading.Thread(target=worker, args=(diversions[1], 20, 2)),
        threading.Thread(target=worker, args=(diversions[0], 10, 3)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    refreshed = next(row for row in client.get('/handover/receipts', headers=analyst).json()
                     if row['id'] == receipt['id'])
    assert refreshed['held_total'] == 30 and refreshed['remaining_vehicles'] == 0
    assert len(results) == 2 and len(errors) == 1
    assert '不能超领' in str(errors[0])


def test_claim_requires_matching_lot_and_day(client, analyst):
    receipt, day = make_receipt(client, analyst, total=30)
    other_day = day - timedelta(days=3)
    diversion = make_active_diversion(other_day)
    response = client.post('/handover/claims', json={
        'receipt_id': receipt['id'], 'diversion_id': diversion,
        'receipt_no': unique('RC-MISMATCH'), 'claimed_vehicles': 4}, headers=analyst)
    assert response.status_code == 422
    assert '不一致' in response.json()['detail']


def test_closed_diversion_cannot_claim(client, analyst):
    receipt, day = make_receipt(client, analyst, total=30)
    diversion = make_active_diversion(day)
    with connect() as conn:
        conn.execute("UPDATE parking_diversion SET status='closed' WHERE id=%s", (diversion,))
        conn.commit()
    response = client.post('/handover/claims', json={
        'receipt_id': receipt['id'], 'diversion_id': diversion,
        'receipt_no': unique('RC-CLOSED'), 'claimed_vehicles': 4}, headers=analyst)
    assert response.status_code == 409


# ---------------------------------------------------------------- 补正与复盘联动

def test_correction_below_occupied_is_rejected(client, analyst):
    receipt, day = make_receipt(client, analyst, total=25)
    diversion = make_active_diversion(day)
    ok = client.post('/handover/claims', json={
        'receipt_id': receipt['id'], 'diversion_id': diversion,
        'receipt_no': unique('RC-CORR'), 'claimed_vehicles': 12}, headers=analyst)
    assert ok.status_code == 201
    rejected = client.post(f'/handover/receipts/{receipt["id"]}/correct',
                           json={'total_vehicles': 11}, headers=analyst)
    assert rejected.status_code == 422
    assert '低于当前已认领占用' in rejected.json()['detail']
    accepted = client.post(f'/handover/receipts/{receipt["id"]}/correct',
                           json={'total_vehicles': 40}, headers=analyst)
    assert accepted.status_code == 200
    assert accepted.json()['total_vehicles'] == 40
    assert accepted.json()['remaining_vehicles'] == 28


def test_settlement_return_releases_and_accept_keeps_occupation(client, analyst, supervisor):
    receipt, day = make_receipt(client, analyst, total=40)
    diversion_a = make_active_diversion(day, spaces=10)
    diversion_b = make_active_diversion(day, spaces=8)
    for diversion, amount, tag in ((diversion_a, 14, 'A'), (diversion_b, 10, 'B')):
        response = client.post('/handover/claims', json={
            'receipt_id': receipt['id'], 'diversion_id': diversion,
            'receipt_no': f'RC-LINK-{tag}-{unique("")}', 'claimed_vehicles': amount}, headers=analyst)
        assert response.status_code == 201, response.text

    # 构造覆盖今日的已发布预测与来源场观测，使复盘流程可以真正提交。
    today = datetime.now(timezone.utc).date()
    with connect() as conn:
        source = conn.execute("SELECT id FROM parking_lot WHERE code='PARK-19'").fetchone()
        plan_code = unique('PF-T')
        conn.execute('''INSERT INTO forecast_plan(code,lot_id,horizon_start,horizon_days,capacity_snapshot,
            status,calendar_complete,capacity_change_confirmed,predictions,model_version,created_by)
            VALUES(%s,%s,%s,3,300,'published',true,true,%s,'test-v1','analyst@metro.demo')''',
            (plan_code, source['id'], today, json.dumps([
                {'date': (today + timedelta(days=i)).isoformat(), 'turnover': 100} for i in range(3)])))
        plan = conn.execute('SELECT id FROM forecast_plan WHERE code=%s', (plan_code,)).fetchone()
        conn.execute('''UPDATE parking_diversion SET forecast_id=%s, service_day=%s WHERE id IN (%s,%s)''',
                     (plan['id'], today, diversion_a, diversion_b))
        conn.execute('''INSERT INTO interval_observation
            (lot_id,observed_at,entry_count,exit_count,occupancy,source_file,source_row,entered_by)
            VALUES(%s,now(),40,30,120,'test-handover.csv',%s,'analyst@metro.demo')
            ON CONFLICT DO NOTHING''', (source['id'], unique('ROW')))
        receipt_row = conn.execute('SELECT id FROM handover_receipt WHERE lot_id=%s AND service_day=%s',
                                   (conn.execute('SELECT target_lot_id FROM parking_diversion WHERE id=%s',
                                                 (diversion_a,)).fetchone()['target_lot_id'], day)).fetchone()
        conn.execute('UPDATE handover_receipt SET service_day=%s WHERE id=%s', (today, receipt_row['id']))
        conn.commit()

    def submit_settlement(diversion_id):
        response = client.post('/operations/settlements', json={
            'diversion_id': diversion_id, 'ledger_reference': unique('LED-T'),
            'actual_diverted': 12, 'analysis_note': '交班认领联动测试：提交复盘实绩'}, headers=analyst)
        assert response.status_code == 200, response.text
        return response.json()['id']

    settlement_a = submit_settlement(diversion_a)
    returned = client.post(f'/operations/settlements/{settlement_a}/return',
                           json={'note': '退回补正：核对交班回执后重提'}, headers=supervisor)
    assert returned.status_code == 200
    refreshed = next(row for row in client.get('/handover/receipts', headers=analyst).json()
                     if row['id'] == receipt['id'])
    assert refreshed['held_total'] == 10  # B 单仍占用，A 单 14 辆已释放
    assert refreshed['claimed_total'] == 24 and refreshed['remaining_vehicles'] == 30
    released_claim = next(c for c in client.get('/handover/claims', headers=analyst).json()
                          if c['diversion_id'] == diversion_a and c['status'] == 'released')
    assert released_claim['release_note'].startswith('退回补正')
    assert released_claim['release_note'].startswith('退回补正')

    # 释放后同一分流单可按新回执重新认领。
    reclaim = client.post('/handover/claims', json={
        'receipt_id': receipt['id'], 'diversion_id': diversion_a,
        'receipt_no': unique('RC-RECLAIM'), 'claimed_vehicles': 6}, headers=analyst)
    assert reclaim.status_code == 201

    settlement_b = submit_settlement(diversion_b)
    accepted = client.post(f'/operations/settlements/{settlement_b}/accept',
                           json={'note': '通过结案：份额继续占用交班总数'}, headers=supervisor)
    assert accepted.status_code == 200
    final = next(row for row in client.get('/handover/receipts', headers=analyst).json()
                 if row['id'] == receipt['id'])
    assert final['held_total'] == 6          # A 单重新认领的份额
    assert final['occupied_total'] == 16     # 6 占用中 + 10 已结案
    assert final['remaining_vehicles'] == 24
    settled_claim = next(c for c in client.get('/handover/claims', headers=analyst).json()
                         if c['diversion_id'] == diversion_b and c['status'] == 'settled')
    assert settled_claim['claimed_vehicles'] == 10
    # 结案后补正仍受已认领占用约束。
    blocked = client.post(f'/handover/receipts/{receipt["id"]}/correct',
                          json={'total_vehicles': 15}, headers=analyst)
    assert blocked.status_code == 422
