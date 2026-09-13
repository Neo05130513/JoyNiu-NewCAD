"""Finance read filters against isolated SQLite history; no gateway or AI calls."""
from types import SimpleNamespace
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import BillingService, admin_record_filters
from app.billing_api import create_billing_router
from app.billing_policy import BillingPolicyService
from app.billing_policy_api import create_billing_policy_router
from app.platform import AuthService, AuthorizationError, ValidationError


@pytest.fixture
def finance(tmp_path, monkeypatch):
    auth = AuthService(tmp_path/'finance.sqlite3', token_secret='isolated-finance-filter-test-secret-'*2)
    users = {role: auth.create_user(role+'@example.test', 'test-password', role, roles=[role]) for role in ['admin','finance','auditor','ops','support','viewer']}
    users['other'] = auth.create_user('other@example.test', 'test-password', 'other', roles=['viewer'])
    services = SimpleNamespace(auth=auth, pdm=SimpleNamespace(database=auth.database))
    billing = BillingService(auth.database, auth=auth)
    policy = BillingPolicyService(billing)
    app = FastAPI()
    app.include_router(create_billing_router(services, billing=billing), prefix='/api/v1')
    app.include_router(create_billing_policy_router(services, billing=billing, policy=policy), prefix='/api/v1')
    timestamps = ['2026-09-09T15:59:59Z', '2026-09-09T16:00:00+00:00', '2026-09-10T14:00:00Z', '2026-09-10T23:59:59+08:00', '2026-09-10T16:00:00Z']
    search_ids = ['edge%needle', 'edge_needle', 'edgeXneedle', 'edge\\needle', 'outside']
    rows = []
    for index, stamp in enumerate(timestamps):
        monkeypatch.setattr('app.billing._now', lambda stamp=stamp: stamp)
        monkeypatch.setattr('app.billing_policy._now', lambda stamp=stamp: stamp)
        owner = users['viewer'] if index in [0, 1, 3] else users['other']
        order_id, attempt_id, call_id = f'order_{index}', 'attempt_' + search_ids[index], 'call_' + search_ids[index]
        state = 'paid' if index in [0,1,3] else 'pending_payment' if index == 2 else 'expired'
        # These historical orders/requests are explicit local read fixtures.
        # No checkout, payment callback or refund execution is invoked.
        with billing._transaction() as db:
            db.execute('INSERT INTO billing_orders (id,owner_id,idempotency_key,request_hash,package_json,amount_fen,credit_units,currency,status,provider,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (order_id, owner.id, 'order-key-'+str(index), 'fixture-hash', '{"name":"测试历史套餐"}', 1000, 100, 'CNY', state, 'offline-test-fixture', stamp, stamp))
            for kind in ['refund', 'invoice']:
                request_status = 'requested' if index in [0,1,4] else ('approved' if kind == 'refund' else 'processing') if index == 2 else ('refunded' if kind == 'refund' else 'issued')
                db.execute('INSERT INTO billing_requests (id,owner_id,order_id,kind,idempotency_key,request_hash,data_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (kind+'_'+str(index), owner.id, order_id, kind, kind+'-key-'+str(index), 'fixture-hash', '{"amountFen":100,"title":"本地测试"}', request_status, stamp, stamp))
        entry = billing.adjust(users['admin'].id, owner_id=owner.id, credit_units=index+1, reason='本地筛选测试历史记录', idempotency_key='ledger-'+str(index))
        billing.record_usage(owner_id=owner.id, job_id='job_'+search_ids[index], attempt_id=attempt_id, call_id=call_id,
                             stage='planner', provider='test-fixture', model='test-model',
                             input_tokens=None if index == 2 else 120, cached_input_tokens=None if index == 2 else 20,
                             output_tokens=None if index == 2 else 50, reasoning_output_tokens=None if index == 2 else 10,
                             cost_micro_usd=None if index == 2 else 75, cost_source=None if index == 2 else 'provider_reported')
        policy.register_attempt(owner_id=owner.id, job_id='job_'+search_ids[index], attempt_id=attempt_id)
        if index == 2:
            # A worker can stop after writing the seal and before its receipt.
            with billing._transaction() as db:
                db.execute('INSERT INTO billing_policy_seals VALUES (?,?,?,?,?)', (attempt_id, 'failed', json.dumps([call_id]), 'sealed-test-roster', stamp))
        else:
            policy.settle_attempt(owner_id=owner.id, job_id='job_'+search_ids[index], attempt_id=attempt_id, terminal_status='review_required', call_ids=[call_id])
        rows.append({'ownerId': owner.id, 'orderId': order_id, 'attemptId': attempt_id, 'callId': call_id, 'ledgerId': entry['id'], 'stamp': stamp})
    header = lambda role: {'Authorization': 'Bearer ' + auth.issue_token(users[role]).token}
    with TestClient(app) as client:
        yield SimpleNamespace(auth=auth, users=users, billing=billing, policy=policy, rows=rows, client=client, headers=header)
    billing.close()
    auth.close()


READ_PATHS = ['ledger', 'orders', 'refund-requests', 'invoice-requests', 'usage', 'audit', 'settlements']


@pytest.mark.parametrize('kind', READ_PATHS)
def test_every_admin_finance_list_requires_auth_and_financial_permission(finance, kind):
    e = finance
    path = '/api/v1/billing/admin/' + kind
    assert e.client.get(path).status_code == 401
    for role in ['viewer', 'ops', 'support']:
        assert e.client.get(path, params={'ownerId': e.users['other'].id}, headers=e.headers(role)).status_code == 403
    for role in ['admin', 'finance', 'auditor']:
        response = e.client.get(path, headers=e.headers(role))
        assert response.status_code == 200 and response.json()['total'] == 5
        assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('kind', ['ledger', 'orders', 'refund-requests', 'invoice-requests', 'settlements'])
def test_customer_lists_ignore_forged_admin_filters_and_remain_owner_scoped(finance, kind):
    e = finance
    response = e.client.get('/api/v1/billing/' + kind, headers=e.headers('viewer'),
                            params={'ownerId': e.users['other'].id, 'actorId': e.users['admin'].id, 'q': e.users['other'].id, 'status': 'paid', 'dateFrom': '2026-09-11'})
    assert response.status_code == 200
    result = response.json()
    assert result['total'] == 3
    assert {row['ownerId'] for row in result['items']} == {e.users['viewer'].id}


def test_direct_service_filters_cannot_elevate_customer_scope(finance):
    e = finance
    with pytest.raises(AuthorizationError):
        e.billing.list_records('orders', owner_id=e.users['viewer'].id, actor_id=e.users['viewer'].id, filter_owner_id=e.users['other'].id)
    with pytest.raises(AuthorizationError):
        e.policy.settlements(owner_id=e.users['viewer'].id, actor_id=e.users['viewer'].id, filter_owner_id=e.users['other'].id)


def test_disabled_finance_token_cannot_keep_reading_any_new_admin_list(finance):
    e = finance
    old_headers = e.headers('finance')
    e.auth.set_active(e.users['finance'].id, False, actor_id=e.users['admin'].id)
    for kind in READ_PATHS:
        response = e.client.get('/api/v1/billing/admin/' + kind, headers=old_headers)
        assert response.status_code in {401, 403}
    with pytest.raises(AuthorizationError):
        e.billing.list_records('ledger', actor_id=e.users['finance'].id)


@pytest.mark.parametrize('kind', READ_PATHS)
def test_finance_can_read_inactive_customer_history_without_reactivating_it(finance, kind):
    e = finance
    e.auth.set_active(e.users['other'].id, False, actor_id=e.users['admin'].id)
    owner = e.users['admin'].id if kind == 'audit' else e.users['other'].id
    response = e.client.get('/api/v1/billing/admin/' + kind, headers=e.headers('finance'), params={'ownerId': owner})
    assert response.status_code == 200
    assert response.json()['total'] == (5 if kind == 'audit' else 2)
    assert e.auth.get_user(e.users['other'].id).active is False


@pytest.mark.parametrize('kind,key', [('orders','id'), ('ledger','id'), ('refund-requests','id'), ('invoice-requests','id'), ('usage','callId'), ('settlements','attemptId'), ('audit','id')])
def test_shanghai_calendar_range_is_inclusive_start_exclusive_next_day_with_pagination(finance, kind, key):
    e = finance
    path = '/api/v1/billing/admin/' + kind
    args = {'dateFrom': '2026-09-10', 'dateTo': '2026-09-10'}
    result = e.client.get(path, headers=e.headers('finance'), params=args).json()
    assert result['total'] == 3
    for offset in [0, 1, 2, 3]:
        page = e.client.get(path, headers=e.headers('finance'), params={**args, 'limit': 1, 'offset': offset}).json()
        assert page['total'] == 3 and page['offset'] == offset and page['limit'] == 1
        assert [row[key] for row in page['items']] == [row[key] for row in result['items'][offset:offset+1]]
    after = e.client.get(path, headers=e.headers('auditor'), params={'dateFrom': '2026-09-11'}).json()
    before = e.client.get(path, headers=e.headers('auditor'), params={'dateTo': '2026-09-09'}).json()
    assert after['total'] == before['total'] == 1


def test_mixed_offset_timestamps_sort_by_actual_instant_for_stable_pages(finance):
    e = finance
    orders = e.client.get('/api/v1/billing/admin/orders', headers=e.headers('finance')).json()['items']
    assert [row['id'] for row in orders] == ['order_4', 'order_3', 'order_2', 'order_1', 'order_0']
    settlements = e.client.get('/api/v1/billing/admin/settlements', headers=e.headers('finance')).json()['items']
    assert [row['attemptId'] for row in settlements] == [row['attemptId'] for row in reversed(e.rows)]


@pytest.mark.parametrize('query,index', [('edge%needle', 0), ('edge_needle', 1), ('edge\\needle', 3)])
def test_search_escapes_literal_percent_underscore_and_backslash(finance, query, index):
    e = finance
    usage = e.client.get('/api/v1/billing/admin/usage', params={'q': query}, headers=e.headers('finance')).json()
    settlements = e.client.get('/api/v1/billing/admin/settlements', params={'q': query}, headers=e.headers('finance')).json()
    assert usage['total'] == settlements['total'] == 1
    assert usage['items'][0]['callId'] == e.rows[index]['callId']
    assert settlements['items'][0]['attemptId'] == e.rows[index]['attemptId']


@pytest.mark.parametrize('kind', READ_PATHS)
def test_query_and_owner_injection_remain_parameter_values(finance, kind):
    e = finance
    path = '/api/v1/billing/admin/' + kind
    for param in [{'q': "' OR 1=1 --"}, {'ownerId': "usr_fake' OR 1=1 --"}]:
        response = e.client.get(path, params=param, headers=e.headers('finance'))
        assert response.status_code == 200 and response.json()['total'] == 0
    assert e.billing._db.execute('SELECT count(*) FROM billing_orders').fetchone()[0] == 5


@pytest.mark.parametrize('kind', ['orders', 'ledger', 'refund-requests', 'invoice-requests', 'settlements'])
def test_status_injection_cannot_broaden_results(finance, kind):
    response = finance.client.get('/api/v1/billing/admin/' + kind, params={'status': "paid' OR 1=1 --"}, headers=finance.headers('finance'))
    assert response.status_code == 200 and response.json()['total'] == 0


@pytest.mark.parametrize('filters', [
    {'dateFrom': '2026-02-30'}, {'dateTo': 'not-a-date'}, {'dateTo': '2026-9-10'},
    {'dateFrom': '2026-09-11', 'dateTo': '2026-09-10'}, {'dateTo': '9999-12-31'},
    {'dateFrom': '0000-01-01'}, {'dateFrom': '2026-09-10T00:00:00Z'},
])
def test_invalid_or_out_of_range_dates_return_422_never_server_error(finance, filters):
    e = finance
    for kind in ['orders', 'settlements']:
        response = e.client.get('/api/v1/billing/admin/' + kind, params=filters, headers=e.headers('finance'))
        assert response.status_code == 422


@pytest.mark.parametrize('filters', [{'limit': 0}, {'limit': 101}, {'offset': -1}, {'offset': 1_000_001}, {'q': 'x'*129}, {'ownerId': 'x'*161}, {'status': 'x'*65}])
def test_limits_and_filter_lengths_are_bounded(finance, filters):
    for kind in ['orders', 'settlements']:
        response = finance.client.get('/api/v1/billing/admin/' + kind, params=filters, headers=finance.headers('finance'))
        assert response.status_code == 422


def test_filter_intersection_admin_ledger_fields_and_known_unknown_usage(finance):
    e = finance
    owner = e.users['viewer'].id
    filters = {'ownerId': owner, 'dateFrom': '2026-09-10', 'dateTo': '2026-09-10'}
    orders = e.client.get('/api/v1/billing/admin/orders', params={**filters, 'status': 'paid'}, headers=e.headers('finance')).json()
    assert orders['total'] == 2 and {row['id'] for row in orders['items']} == {'order_1', 'order_3'}
    ledger = e.client.get('/api/v1/billing/admin/ledger', params={**filters, 'status': 'adjustment'}, headers=e.headers('auditor')).json()
    assert ledger['total'] == 2 and {row['ownerId'] for row in ledger['items']} == {owner}
    for row in ledger['items']:
        assert {'id','ownerId','deltaUnits','balanceAfter','kind','referenceId','reason','createdAt'} <= row.keys()
        assert not {'event_key','request_hash','password','accessToken'} & row.keys()
    known = e.client.get('/api/v1/billing/admin/usage', params={'q': e.rows[1]['callId']}, headers=e.headers('finance')).json()['items'][0]
    assert {key: known[key] for key in ['inputTokens','cachedInputTokens','outputTokens','reasoningOutputTokens','costMicroUsd']} == {'inputTokens':120,'cachedInputTokens':20,'outputTokens':50,'reasoningOutputTokens':10,'costMicroUsd':75}
    assert known['ownerId'] == owner and known['attemptId'] == e.rows[1]['attemptId'] and known['createdAt'] == e.rows[1]['stamp']
    unknown = e.client.get('/api/v1/billing/admin/usage', params={'q': e.rows[2]['callId']}, headers=e.headers('finance')).json()['items'][0]
    assert unknown['inputTokens'] is None and unknown['costMicroUsd'] is None and unknown['usageKnown'] is False
    assert e.billing._db.execute('SELECT count(*) FROM billing_payment_events').fetchone()[0] == 0
    assert e.billing._db.execute('SELECT count(*) FROM billing_refunds').fetchone()[0] == 0
    assert e.policy.status()['enabled'] is False


def test_refund_invoice_status_and_settlement_owner_status_filters(finance):
    e = finance
    for kind in ['refund', 'invoice']:
        response = e.client.get('/api/v1/billing/admin/' + kind + '-requests', params={'status': 'approved' if kind == 'refund' else 'processing', 'ownerId': e.users['other'].id}, headers=e.headers('finance')).json()
        assert response['total'] == 1 and response['items'][0]['kind'] == kind
    pending = e.client.get('/api/v1/billing/admin/settlements', params={'status': 'pending_settlement', 'ownerId': e.users['other'].id}, headers=e.headers('finance')).json()
    assert pending['total'] == 1 and pending['items'][0]['attemptId'] == e.rows[2]['attemptId']
    assert pending['items'][0]['expectedCreditUnits'] is None and pending['items'][0]['terminalStatus'] == 'failed'
    done = e.client.get('/api/v1/billing/admin/settlements', params={'status': 'not_charged', 'ownerId': e.users['viewer'].id, 'dateFrom':'2026-09-10', 'dateTo':'2026-09-10', 'limit':1, 'offset':1}, headers=e.headers('auditor')).json()
    assert done['total'] == 2 and len(done['items']) == 1 and done['items'][0]['ownerId'] == e.users['viewer'].id


def test_service_filter_validation_rejects_nonstring_and_unavailable_status_column():
    for filters in [{'q': []}, {'status': 1}, {'filter_owner_id': {}}, {'date_from': 5}, {'date_to': None}, {'status': 'paid'}]:
        with pytest.raises(ValidationError):
            admin_record_filters(**filters, columns=('id',))
