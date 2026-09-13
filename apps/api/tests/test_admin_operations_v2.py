"""Persistent CRM, human task triage and date-accurate read-only analytics."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import sqlite3
import pytest
from app.admin_operations import AdminOperationsService
from app.platform import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .test_admin_operations import setup, client, headers, run


def crm(**changes):
    return {'company': '测试精密机械有限公司', 'contactName': '陈工', 'phone': '13800000000',
            'tags': ['试用客户', '钣金'], 'internalNote': '只接收可编辑实体交付',
            'expectedVersion': 0, 'idempotencyKey': 'crm-save', 'reason': '根据客户提供的信息补充资料', **changes}


def handling(**changes):
    return {'status': 'pending', 'priority': 'high', 'expectedVersion': 0,
            'idempotencyKey': 'triage-save', 'reason': '已收到客户反馈需要人工跟进', **changes}


def test_crm_persists_and_search_matches_company_contacts_phone_and_tags(setup):
    e = setup
    owner = e.users['viewer'].id
    assert e.ops.customer(e.users['support'].id, owner)['crm']['version'] == 0
    first = e.ops.update_crm(e.users['ops'].id, owner, crm())
    assert first['crm']['version'] == 1 and not first['replayed']
    assert first['crm']['updatedBy'] == e.users['ops'].id
    for q in ['精密机械', '陈工', '13800000000', '试用客户']:
        result = e.ops.customers(e.users['support'].id, q=q)
        assert result['total'] == 1 and result['items'][0]['id'] == owner
    assert e.ops.customers(e.users['ops'].id, q='%')['total'] == 0
    second = AdminOperationsService(e.services, cad_store=e.store, billing=e.billing, billing_policy=e.policy)
    try:
        assert second.customer(e.users['finance'].id, owner)['crm'] == first['crm']
        assert second.customer(e.users['auditor'].id, owner)['crm']['internalNote'] == crm()['internalNote']
    finally:
        second.close()


@pytest.mark.parametrize('role', ['finance', 'auditor', 'viewer'])
def test_read_only_roles_cannot_write_crm_or_task_followup(setup, role):
    e = setup
    rid, _ = run(e)
    user, owner = e.users[role].id, e.users['viewer'].id
    for method, args in [(e.ops.update_crm, (owner, crm())), (e.ops.update_handling, (rid, handling())),
                         (e.ops.add_note, ('customer', owner, {'content': '资料跟进', 'idempotencyKey': 'follow'})),
                         (e.ops.add_note, ('task', rid, {'content': '任务跟进', 'idempotencyKey': 'note'}))]:
        with pytest.raises(AuthorizationError):
            method(user, *args)
    assert e.ops._cad.execute('SELECT count(*) FROM admin_operations_mutations').fetchone()[0] == 0


def test_idempotency_replays_original_response_and_version_conflicts_prevent_overwrite(setup):
    e = setup
    owner = e.users['viewer'].id
    first = e.ops.update_crm(e.users['support'].id, owner, crm())
    second = e.ops.update_crm(e.users['ops'].id, owner, crm(company='已确认的新公司', expectedVersion=1, idempotencyKey='next'))
    replay = e.ops.update_crm(e.users['support'].id, owner, crm())
    assert replay['replayed'] and replay['auditId'] == first['auditId']
    assert replay['crm']['version'] == 1 and second['crm']['version'] == 2
    with pytest.raises(ConflictError):
        e.ops.update_crm(e.users['support'].id, owner, crm(company='篡改旧请求'))
    with pytest.raises(ConflictError):
        e.ops.update_crm(e.users['support'].id, owner, crm(idempotencyKey='stale-version'))
    assert e.ops._crm(owner)['company'] == '已确认的新公司'
    assert e.ops._cad.execute("SELECT count(*) FROM admin_operations_audit WHERE action='customer.crm.updated'").fetchone()[0] == 2


def test_concurrent_profiles_only_one_version_wins_and_duplicate_only_one_note(setup):
    e = setup
    owner = e.users['viewer'].id
    second = AdminOperationsService(e.services, cad_store=e.store, billing=e.billing)
    barrier = Barrier(2)
    def edit(pair):
        service, key = pair
        barrier.wait()
        try:
            return service.update_crm(e.users['ops'].id, owner, crm(idempotencyKey=key))
        except ConflictError:
            return 'conflict'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, [(e.ops, 'race-a'), (second, 'race-b')]))
        assert results.count('conflict') == 1 and e.ops._crm(owner)['version'] == 1
        barrier = Barrier(2)
        def note(service):
            barrier.wait()
            return service.add_note(e.users['support'].id, 'customer', owner, {'content': '已电话了解需求', 'idempotencyKey': 'concurrent-note'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(note, [e.ops, second]))
        assert len({item['entry']['id'] for item in results}) == 1
        assert sum(item['replayed'] for item in results) == 1
    finally:
        second.close()


def test_human_task_triage_does_not_change_cad_status_or_trigger_calls(setup):
    e = setup
    rid, _ = run(e, registered=True, status='failed')
    before, registry_before = e.store.load(rid), e.ops.registry.get(rid)
    assert e.ops.task(e.users['ops'].id, rid)['handling']['status'] == 'unassigned'
    first = e.ops.update_handling(e.users['support'].id, rid, handling())
    assert first['handling']['status'] == 'pending'
    assert e.ops.tasks(e.users['ops'].id, handling_status='pending', priority='high')['total'] == 1
    assert e.ops.tasks(e.users['ops'].id, handling_status='unassigned')['total'] == 0
    assert e.ops.tasks(e.users['ops'].id, priority='normal')['total'] == 0
    final = e.ops.update_handling(e.users['ops'].id, rid, handling(status='resolved', expectedVersion=1, idempotencyKey='resolved'))
    assert final['handling']['version'] == 2
    assert e.store.load(rid) == before and e.ops.registry.get(rid) == registry_before
    assert e.ops._cad.execute('SELECT count(*) FROM cad_provider_calls').fetchone()[0] == 0
    with pytest.raises(ConflictError):
        e.ops.update_handling(e.users['ops'].id, rid, handling(idempotencyKey='stale'))
    with pytest.raises(ValidationError):
        e.ops.update_handling(e.users['ops'].id, rid, handling(status='ready'))


def test_notes_are_scoped_paginated_append_only_and_audited_without_content(setup):
    e = setup
    rid, _ = run(e)
    owner, other = e.users['viewer'].id, e.users['other'].id
    for index in range(3):
        e.ops.add_note(e.users['support'].id, 'customer', owner, {'content': f'客户跟进 {index}', 'idempotencyKey': f'customer-note-{index}'})
    e.ops.add_note(e.users['ops'].id, 'task', rid, {'content': '排查记录 INTERNAL_NOTE_ONLY', 'idempotencyKey': 'task-note'})
    assert e.ops.notes(e.users['finance'].id, 'customer', other)['total'] == 0
    notes = e.ops.notes(e.users['auditor'].id, 'customer', owner, limit=1, offset=1)
    assert notes['total'] == 3 and len(notes['items']) == 1
    assert e.ops.customer(e.users['support'].id, owner)['followups']['total'] == 3
    assert e.ops.task(e.users['support'].id, rid)['notes']['total'] == 1
    assert 'INTERNAL_NOTE_ONLY' not in json.dumps(e.ops.audit(e.users['auditor'].id, source='operations'))
    for table in ['admin_operations_notes', 'admin_operations_mutations', 'admin_operations_audit']:
        with pytest.raises(sqlite3.IntegrityError):
            e.ops._cad.execute('DELETE FROM ' + table)
        e.ops._cad.rollback()


def test_audit_failure_rolls_back_crm_and_receipt(setup):
    e = setup
    e.ops._cad.execute("CREATE TRIGGER deny_ops_audit BEFORE INSERT ON admin_operations_audit BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
    e.ops._cad.commit()
    with pytest.raises(sqlite3.IntegrityError):
        e.ops.update_crm(e.users['ops'].id, e.users['viewer'].id, crm())
    assert e.ops._crm(e.users['viewer'].id)['version'] == 0
    assert e.ops._cad.execute('SELECT count(*) FROM admin_operations_mutations').fetchone()[0] == 0


@pytest.mark.parametrize('change', [{'company': 'x'*121}, {'tags': ['a']*13}, {'tags': ['']}, {'phone': 1}, {'reason': '短'}, {'expectedVersion': True}, {'internalNote': 'x\x00y'}, {'idempotencyKey': 'spaces not allowed'}, {'ownerId': 'spoof'}])
def test_crm_validation_rejects_bad_or_extra_fields_without_writes(setup, change):
    with pytest.raises(ValidationError):
        setup.ops.update_crm(setup.users['ops'].id, setup.users['viewer'].id, crm(**change))
    assert setup.ops._cad.execute('SELECT count(*) FROM admin_customer_crm').fetchone()[0] == 0


def test_missing_target_blank_note_and_revocation_cannot_write(setup):
    e = setup
    with pytest.raises(NotFoundError):
        e.ops.update_crm(e.users['ops'].id, 'usr_missing', crm())
    with pytest.raises(NotFoundError):
        e.ops.update_handling(e.users['ops'].id, 'cad_missing', handling())
    with pytest.raises(ValidationError):
        e.ops.add_note(e.users['support'].id, 'customer', e.users['viewer'].id, {'content': '  ', 'idempotencyKey': 'blank'})
    e.auth.set_active(e.users['support'].id, False, actor_id=e.users['admin'].id)
    with pytest.raises(AuthorizationError):
        e.ops.add_note(e.users['support'].id, 'customer', e.users['viewer'].id, {'content': '失效账号', 'idempotencyKey': 'revoked'})


def test_trend_uses_shanghai_dates_latest_task_status_and_explicit_unknown_usage(setup, monkeypatch):
    e = setup
    monkeypatch.setattr('app.admin_operations.now', lambda: '2026-09-10T16:30:00Z')
    # In Shanghai this is September 11. The task's later revision appears once.
    rid, _ = run(e, status='failed')
    record = e.store.load(rid)
    record.update(revision=2, status='review_required', createdAt='2026-09-10T16:01:00Z')
    e.store.save(record, previous_revision=1)
    run(e, status='ready')
    run(e, status='needs_input')
    trend = e.ops.overview(e.users['ops'].id, days=7)['trend']
    assert len(trend['points']) == 7 and trend['points'][-1]['date'] == '2026-09-11'
    assert trend['timeZone'] == 'Asia/Shanghai' and trend['successStatuses'] == ['ready']
    assert trend['totals']['tasks'] == 3 and trend['totals']['success'] == 1
    assert trend['totals']['reviewRequired'] == 1 and trend['totals']['needsInput'] == 1 and trend['totals']['failed'] == 0
    latest = trend['points'][-1]
    assert latest['tasks'] == 1 and latest['usage']['calls'] == 0 and latest['usage']['inputTokens'] is None
    assert 'costMicroUsd' not in latest['usage']
    assert latest['historicalTasksWithoutJournal'] == 1
    assert len(e.ops.overview(e.users['auditor'].id, days=90)['trend']['points']) == 90
    for days in [0, 8, 365, True, '7']:
        with pytest.raises(ValidationError):
            e.ops.overview(e.users['ops'].id, days=days)


def test_trend_deduplicates_receipts_uses_call_start_and_preserves_partial_unknowns(setup, monkeypatch):
    e = setup
    monkeypatch.setattr('app.admin_operations.now', lambda: '2026-09-11T02:00:00Z')
    monkeypatch.setattr('app.billing._now', lambda: '2026-09-11T01:00:00Z')
    rid, identity = run(e, registered=True)
    for call, complete in [('known', True), ('unknown', False)]:
        e.ops.registry.begin_call(call_id=call, owner=e.users['viewer'].id, job_id=identity['jobId'], attempt_id=rid, stage='planner', identity={})
        if complete:
            e.ops.registry.finish_call(call, {'input_tokens': 100, 'cached_input_tokens': 25, 'output_tokens': 50, 'reasoning_output_tokens': 5, 'cost_micro_usd': 500})
        e.ops._cad.execute('UPDATE cad_provider_calls SET started_at=? WHERE call_id=?', ('2026-09-10T01:00:00Z', call))
        e.ops._cad.commit()
    e.billing.record_usage(owner_id=e.users['viewer'].id, job_id=identity['jobId'], attempt_id=rid, call_id='known', stage='planner', provider='codex-cli', model='gpt-6-astra', input_tokens=100, cached_input_tokens=25, output_tokens=50, reasoning_output_tokens=5, cost_micro_usd=500, cost_source='provider_reported')
    trend = e.ops.overview(e.users['auditor'].id, days=7)['trend']
    by_date = {row['date']: row for row in trend['points']}
    usage = by_date['2026-09-10']['usage']
    assert usage['calls'] == 2 and usage['unknownUsageCalls'] == 1 and usage['inputTokens'] is None
    assert usage['costMicroUsd'] is None and usage['unknownCostCalls'] == 1
    assert by_date['2026-09-11']['usage']['calls'] == 0
    # A delayed receipt for a call before the range is not moved into today.
    e.ops._cad.execute("UPDATE cad_provider_calls SET started_at='2026-08-01T00:00:00Z' WHERE call_id='known'")
    e.ops._cad.commit()
    assert sum(row['usage']['calls'] for row in e.ops.overview(e.users['ops'].id, days=7)['trend']['points']) == 1


def test_overview_distinguishes_staff_customers_and_resolved_support(setup):
    e = setup
    owner = e.users['viewer'].id
    for status in ['open', 'in_progress', 'waiting_customer', 'resolved', 'closed']:
        ticket = e.support.create(owner, {'subject': '测试工单', 'body': '测试内容', 'category': 'other', 'runId': None, 'drawingConsent': False, 'idempotencyKey': status})
        with e.auth._lock, e.auth._connection:
            e.auth._connection.execute('UPDATE support_tickets SET status=? WHERE id=?', (status, ticket['id']))
    overview = e.ops.overview(e.users['ops'].id)
    assert overview['customers']['businessCustomers'] == 2 and overview['customers']['staffAccounts'] == 5
    assert overview['support'] == {'available': True, 'total': 5, 'open': 2, 'waitingCustomer': 1, 'resolved': 1, 'closed': 1}
    assert e.ops.customer(e.users['support'].id, owner)['customer']['openTicketCount'] == 2


def test_new_api_routes_auth_permissions_conflicts_and_bounded_json(client, setup):
    e = setup
    owner = e.users['viewer'].id
    rid, _ = run(e)
    crm_path = '/api/v1/admin/customers/' + owner + '/crm'
    routes = [(crm_path, 'put', crm()), ('/api/v1/admin/customers/' + owner + '/followups', 'post', {'content': '电话回访', 'idempotencyKey': 'call'}),
              ('/api/v1/admin/tasks/' + rid + '/handling', 'put', handling()), ('/api/v1/admin/tasks/' + rid + '/notes', 'post', {'content': '已确认失败原因', 'idempotencyKey': 'note'})]
    for path, verb, data in routes:
        request = getattr(client, verb)
        assert request(path, json=data).status_code == 401
        assert request(path, json=data, headers=headers(e, 'finance')).status_code == 403
        response = request(path, json=data, headers=headers(e, 'support'))
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
    assert client.put(crm_path, json=crm(idempotencyKey='stale'), headers=headers(e, 'ops')).status_code == 409
    assert client.put(crm_path, content='{}', headers=headers(e, 'ops')).status_code == 415
    json_headers = {**headers(e, 'ops'), 'Content-Type': 'application/json'}
    assert client.put(crm_path, content='x'*32769, headers=json_headers).status_code == 413
    for invalid in ['{"a":1,"a":2}', '{"value":NaN}', '[]']:
        assert client.put(crm_path, content=invalid, headers=json_headers).status_code == 422
    assert client.get('/api/v1/admin/tasks', params={'handlingStatus': 'pending', 'priority': 'high'}, headers=headers(e, 'ops')).json()['total'] == 1
    assert client.get('/api/v1/admin/overview?days=8', headers=headers(e, 'ops')).status_code == 422
    assert client.get('/api/v1/admin/overview?days=7', headers=headers(e, 'ops')).json()['trend']['days'] == 7
    for suffix in ['/customers/' + owner + '/followups', '/tasks/' + rid + '/notes']:
        assert client.get('/api/v1/admin' + suffix, headers=headers(e, 'viewer')).status_code == 403
        assert client.get('/api/v1/admin' + suffix + '?limit=101', headers=headers(e, 'support')).status_code == 422
        assert client.get('/api/v1/admin' + suffix, headers=headers(e, 'support')).json()['total'] == 1
