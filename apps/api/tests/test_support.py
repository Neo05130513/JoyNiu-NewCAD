from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace
import sqlite3

import pytest

from app.platform import AuthService, AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.support import SupportService


@pytest.fixture
def support(tmp_path):
    auth = AuthService(tmp_path / "support.sqlite3", token_secret="support-test-secret-" * 3)
    users = [auth.create_user(f"{name}@example.test", "a-long-test-password", name, roles=["admin" if name == "admin" else "viewer"]) for name in ("first", "other", "admin")]
    records = {"run-first": {"owner": users[0].id, "files": [{"token": "private-original"}]}, "run-other": {"owner": users[1].id}}
    def load(run_id):
        return deepcopy(records[run_id])
    store = SimpleNamespace(load=load)
    service = SupportService(auth, cad_store=store)
    yield service, auth, users, store
    service.close(); auth.close()


def create_values(**changes):
    return {"subject": "原图尺寸核对问题", "category": "modeling", "body": "孔径对应位置需要说明", "runId": None, "drawingConsent": False, "idempotencyKey": "create-one", **changes}


def test_ticket_and_messages_are_owned_and_foreign_run_ids_do_not_leak(support):
    service, _, (first, other, admin), _ = support
    ticket = service.create(first.id, create_values(runId="run-first"))
    assert ticket["number"].startswith("SUP-") and ticket["drawingConsent"] is False
    assert service.list_tickets(other.id)["total"] == 0
    for read in (service.ticket, service.messages):
        with pytest.raises(NotFoundError): read(other.id, ticket["id"])
    with pytest.raises(NotFoundError): service.create(first.id, create_values(runId="run-other", idempotencyKey="other-run"))
    with pytest.raises(NotFoundError): service.create(first.id, create_values(runId="missing", idempotencyKey="missing"))
    with pytest.raises(NotFoundError): service.create(admin.id, create_values(runId="run-first", idempotencyKey="admin-foreign"))
    assert service.ticket(admin.id, ticket["id"], admin=True)["ownerId"] == first.id
    assert "private-original" not in str(service.ticket(admin.id, ticket["id"], admin=True))


def test_create_and_reply_are_idempotent_without_duplicate_messages_or_audit(support):
    service, _, (first, _, admin), _ = support
    ticket = service.create(first.id, create_values())
    assert service.create(first.id, create_values())["id"] == ticket["id"]
    values = {"body": "补充一个尺寸依据", "idempotencyKey": "reply-one"}
    reply = service.append(first.id, ticket["id"], values)
    assert service.append(first.id, ticket["id"], values) == reply
    assert service.messages(first.id, ticket["id"])["total"] == 2
    assert service.audit(admin.id, ticket["id"])["total"] == 3
    with pytest.raises(ConflictError): service.append(first.id, ticket["id"], {**values, "body": "不同内容"})
    with pytest.raises(ConflictError): service.create(first.id, create_values(body="不同内容"))


def test_concurrent_connections_share_one_reply_idempotency_key(support):
    service, auth, (first, _, _), store = support
    second = SupportService(auth, cad_store=store)
    ticket = service.create(first.id, create_values())
    barrier = Barrier(2)
    def write(current):
        barrier.wait()
        return current.append(first.id, ticket["id"], {"body": "网络重试的同一说明", "idempotencyKey": "network-retry"})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(write, [service, second]))
        assert values[0]["message"]["id"] == values[1]["message"]["id"]
        assert service.messages(first.id, ticket["id"])["total"] == 2
    finally:
        second.close()


def test_consent_is_explicit_revocable_after_close_and_cannot_be_changed_by_admin(support):
    service, _, (first, _, admin), _ = support
    ticket = service.create(first.id, create_values(runId="run-first", drawingConsent=True))
    closed = service.update(first.id, ticket["id"], {"revision": 1, "status": "closed", "idempotencyKey": "close"})
    revoked = service.update(first.id, ticket["id"], {"revision": closed["revision"], "drawingConsent": False, "idempotencyKey": "revoke"})
    assert revoked["status"] == "closed" and revoked["drawingConsent"] is False
    assert revoked["closedAt"] == closed["closedAt"]
    assert service.create(first.id, create_values(runId="run-first", drawingConsent=True))["drawingConsent"] is False
    with pytest.raises(ValidationError): service.update(admin.id, ticket["id"], {"revision": revoked["revision"], "drawingConsent": True, "idempotencyKey": "admin-grant", "reason": "不能代为授权"}, admin=True)
    events = service.audit(admin.id, ticket["id"])["items"]
    assert events[0]["action"] == "drawing_consent.changed" and events[0]["details"]["after"] is False


def test_revision_conflicts_cannot_overwrite_new_customer_consent(support):
    service, _, (first, _, admin), _ = support
    ticket = service.create(first.id, create_values(runId="run-first", drawingConsent=True))
    service.update(first.id, ticket["id"], {"revision": 1, "drawingConsent": False, "idempotencyKey": "revoke"})
    with pytest.raises(ConflictError): service.update(admin.id, ticket["id"], {"revision": 1, "status": "in_progress", "reason": "开始处理", "idempotencyKey": "stale"}, admin=True)
    assert service.ticket(first.id, ticket["id"])["drawingConsent"] is False


def test_admin_workflow_and_customer_replies_preserve_real_authors(support):
    service, _, (first, other, admin), _ = support
    ticket = service.create(first.id, create_values())
    with pytest.raises(AuthorizationError): service.list_tickets(first.id, admin=True)
    with pytest.raises(NotFoundError): service.append(other.id, ticket["id"], {"body": "越权回复", "idempotencyKey": "foreign"})
    reply = service.append(admin.id, ticket["id"], {"body": "请补充孔距依据", "idempotencyKey": "admin-reply"}, admin=True)
    assert reply["ticket"]["status"] == "waiting_customer"
    assert reply["message"]["authorRole"] == "admin" and reply["message"]["actorId"] == admin.id
    response = service.append(first.id, ticket["id"], {"body": "已补充", "idempotencyKey": "customer-reply"})
    assert response["ticket"]["status"] == "open"
    resolved = service.update(admin.id, ticket["id"], {"revision": response["ticket"]["revision"], "status": "resolved", "reason": "依据已核对", "idempotencyKey": "resolve"}, admin=True)
    closed = service.update(first.id, ticket["id"], {"revision": resolved["revision"], "status": "closed", "idempotencyKey": "close"})
    with pytest.raises(ConflictError): service.append(first.id, ticket["id"], {"body": "关闭后不应发送", "idempotencyKey": "closed"})
    reopened = service.update(admin.id, ticket["id"], {"revision": closed["revision"], "status": "in_progress", "reason": "继续核查", "idempotencyKey": "reopen"}, admin=True)
    assert reopened["closedAt"] is None


def test_pagination_restart_and_immutable_audit(support):
    service, auth, (first, _, admin), store = support
    first_ticket = service.create(first.id, create_values())
    service.create(first.id, create_values(idempotencyKey="two"))
    assert service.list_tickets(first.id, limit=1, offset=1)["total"] == 2
    assert len(service.list_tickets(first.id, limit=1, offset=1)["items"]) == 1
    for i in range(3): service.append(first.id, first_ticket["id"], {"body": f"说明 {i}", "idempotencyKey": f"message-{i}"})
    page = service.messages(first.id, first_ticket["id"], offset=2, limit=1)
    assert page["total"] == 4 and page["items"][0]["body"] == "说明 1"
    second = SupportService(auth, cad_store=store)
    try: assert second.ticket(first.id, first_ticket["id"])["messageCount"] == 4
    finally: second.close()
    with pytest.raises(sqlite3.IntegrityError): service._db.execute("DELETE FROM support_audit")
    assert service.audit(admin.id, first_ticket["id"], limit=1)["total"] == 5


@pytest.mark.parametrize("patch", [{"ownerId": "forged"}, {"drawingConsent": "true"}, {"subject": ""}, {"body": "x" * 5001}, {"idempotencyKey": "../bad"}, {"category": "unknown"}, {"category": []}, {"drawingConsent": True}])
def test_invalid_creation_does_not_write(patch, support):
    service, _, (first, _, _), _ = support
    with pytest.raises(ValidationError): service.create(first.id, create_values(**patch))
    assert service.list_tickets(first.id)["total"] == 0


def test_role_removal_blocks_former_support_admin_immediately(support):
    service, auth, (first, _, admin), _ = support
    ticket = service.create(first.id, create_values())
    other_admin = auth.create_user("another-admin@example.test", "a-long-test-password", "Another admin", roles=["admin"])
    auth.assign_roles(admin.id, ["viewer"], actor_id=other_admin.id)
    with pytest.raises(AuthorizationError): service.ticket(admin.id, ticket["id"], admin=True)
    with pytest.raises(AuthorizationError): service.append(admin.id, ticket["id"], {"body": "已无权限", "idempotencyKey": "revoked"}, admin=True)
    assert service.messages(first.id, ticket["id"])["total"] == 1


def test_assignment_is_persistent_versioned_and_only_targets_active_support_staff(support):
    service, auth, (first, other, admin), store = support
    staff = auth.create_user('support-staff@example.test', 'a-long-test-password', '客服甲', roles=['support'])
    finance = auth.create_user('finance-staff@example.test', 'a-long-test-password', '财务', roles=['finance'])
    candidates = service.assignees(admin.id)['items']
    assert {item['id'] for item in candidates} == {admin.id, staff.id}
    ticket = service.create(first.id, create_values())
    managed = service.ticket(staff.id, ticket['id'], admin=True)
    assert managed['assignedTo'] is None and managed['priority'] == 'normal'
    payload = {'revision': 1, 'assignedTo': staff.id, 'priority': 'urgent', 'reason': '需优先检查几何结果', 'idempotencyKey': 'assign'}
    updated = service.update(admin.id, ticket['id'], payload, admin=True)
    assert updated['assignee']['displayName'] == '客服甲' and updated['priority'] == 'urgent'
    assert service.update(admin.id, ticket['id'], payload, admin=True)['revision'] == 2
    with pytest.raises(ConflictError): service.update(admin.id, ticket['id'], {**payload, 'idempotencyKey': 'stale'}, admin=True)
    for candidate in (first.id, finance.id, 'missing'):
        with pytest.raises(ValidationError): service.update(admin.id, ticket['id'], {**payload, 'revision': 2, 'assignedTo': candidate, 'idempotencyKey': candidate}, admin=True)
    auth.set_active(staff.id, False, actor_id=admin.id)
    assert staff.id not in {item['id'] for item in service.assignees(admin.id)['items']}
    assert service.ticket(admin.id, ticket['id'], admin=True)['assignee']['available'] is False
    with pytest.raises(ValidationError): service.update(admin.id, ticket['id'], {**payload, 'revision': 2, 'idempotencyKey': 'disabled'}, admin=True)
    second = SupportService(auth, cad_store=store)
    try:
        assert second.ticket(admin.id, ticket['id'], admin=True)['priority'] == 'urgent'
        unassigned = second.update(admin.id, ticket['id'], {'revision': 2, 'assignedTo': None, 'reason': '停用账号后重新分派', 'idempotencyKey': 'clear'}, admin=True)
        assert unassigned['assignedTo'] is None
    finally: second.close()
    assert service.audit(admin.id, ticket['id'])['items'][0]['action'] == 'assignment.changed'
    public = service.ticket(first.id, ticket['id'])
    assert public['statusNote'] == ''
    assert not {'assignedTo','assignee','priority','internalNoteCount'}.intersection(public)


def test_queue_views_search_and_filters_count_only_pending_work(support):
    service, _, (first, _, admin), _ = support
    one = service.create(first.id, create_values(subject='孔径 A_1%', idempotencyKey='queue-one'))
    two = service.create(first.id, create_values(subject='检查螺纹', idempotencyKey='queue-two'))
    three = service.create(first.id, create_values(subject='已解决的问题', idempotencyKey='queue-three'))
    service.update(admin.id, one['id'], {'revision': 1, 'assignedTo': admin.id, 'priority': 'high', 'reason': '本人核对', 'idempotencyKey': 'assign'}, admin=True)
    service.update(admin.id, three['id'], {'revision': 1, 'status': 'resolved', 'reason': '已解决', 'idempotencyKey': 'resolve'}, admin=True)
    queue = service.list_tickets(admin.id, admin=True, status='pending')
    assert queue['total'] == 2 and queue['views'] == {'pending': 2, 'mine': 1, 'unassigned': 1}
    assert service.list_tickets(admin.id, admin=True, assigned_to='me')['items'][0]['id'] == one['id']
    assert service.list_tickets(admin.id, admin=True, assigned_to=admin.id, priority='high')['total'] == 1
    assert service.list_tickets(admin.id, admin=True, status='pending', assigned_to='unassigned')['items'][0]['id'] == two['id']
    assert service.list_tickets(admin.id, admin=True, q='A_1%')['total'] == 1
    assert service.list_tickets(admin.id, admin=True, q='%')['total'] == 1
    assert service.list_tickets(admin.id, admin=True, q='检查', priority='normal')['total'] == 1
    assert service.list_tickets(admin.id, admin=True, q=one['id'])['total'] == 1
    for patch in ({'priority':'wrong'}, {'assigned_to':'bad / id'}, {'q':'x'*129}):
        with pytest.raises(ValidationError): service.list_tickets(admin.id, admin=True, **patch)
    with pytest.raises(AuthorizationError): service.list_tickets(first.id, assigned_to='me')


def test_private_notes_never_enter_customer_messages_and_survive_restart(support):
    service, auth, (first, other, admin), store = support
    ticket = service.create(first.id, create_values())
    payload = {'body': '内部排查：供应商线路待核对', 'revision': 1, 'idempotencyKey': 'private-note'}
    result = service.append_note(admin.id, ticket['id'], payload)
    assert service.append_note(admin.id, ticket['id'], payload)['noteId'] == result['noteId']
    assert result['ticket']['revision'] == 2 and result['ticket']['status'] == 'open'
    assert result['ticket']['updatedAt'] == ticket['updatedAt'] and result['ticket']['messageCount'] == 1
    assert result['ticket']['internalNoteCount'] == 1
    assert service.notes(admin.id, ticket['id'])['items'][0]['body'] == payload['body']
    for public in (service.ticket(first.id, ticket['id']), service.list_tickets(first.id), service.messages(first.id, ticket['id']), service.create(first.id, create_values())):
        assert '供应商线路待核对' not in str(public) and 'internalNoteCount' not in str(public)
    assert service.audit(admin.id, ticket['id'])['items'][0]['details'] == {'noteId': result['noteId']}
    with pytest.raises(ConflictError): service.append_note(admin.id, ticket['id'], {**payload, 'body': '不允许替换'})
    with pytest.raises(ConflictError): service.append_note(admin.id, ticket['id'], {**payload, 'idempotencyKey': 'old-version'})
    for actor in (first, other):
        with pytest.raises(AuthorizationError): service.notes(actor.id, ticket['id'])
        with pytest.raises(AuthorizationError): service.append_note(actor.id, ticket['id'], payload)
    for table in ('support_internal_notes', 'support_audit'):
        with pytest.raises(sqlite3.IntegrityError): service._db.execute(f'DELETE FROM {table}')
    second = SupportService(auth, cad_store=store)
    try: assert second.notes(admin.id, ticket['id'])['total'] == 1
    finally: second.close()


def test_internal_note_permissions_checked_again_after_role_removal(support):
    service, auth, (first, _, admin), _ = support
    staff = auth.create_user('temporary-support@example.test', 'a-long-test-password', '临时客服', roles=['support'])
    ticket = service.create(first.id, create_values())
    service.append_note(staff.id, ticket['id'], {'body': '交接记录', 'revision': 1, 'idempotencyKey': 'before'})
    auth.assign_roles(staff.id, ['viewer'], actor_id=admin.id)
    with pytest.raises(AuthorizationError): service.notes(staff.id, ticket['id'])
    with pytest.raises(AuthorizationError): service.assignees(staff.id)
    with pytest.raises(AuthorizationError): service.append_note(staff.id, ticket['id'], {'body': '越权写入', 'revision': 2, 'idempotencyKey': 'after'})
    assert service.notes(admin.id, ticket['id'])['total'] == 1


def test_notes_pagination_and_closed_ticket_keep_public_state(support):
    service, _, (first, _, admin), _ = support
    ticket = service.create(first.id, create_values())
    closed = service.update(first.id, ticket['id'], {'revision': 1, 'status': 'closed', 'idempotencyKey': 'closed'})
    for i in range(3):
        service.append_note(admin.id, ticket['id'], {'body': f'内部复盘{i}', 'revision': i+2, 'idempotencyKey': f'note-{i}'})
    page = service.notes(admin.id, ticket['id'], limit=1, offset=1)
    assert page['total'] == 3 and page['items'][0]['body'] == '内部复盘1'
    public = service.ticket(first.id, ticket['id'])
    assert public['status'] == 'closed' and public['closedAt'] == closed['closedAt'] and public['messageCount'] == 1
    with pytest.raises(ValidationError): service.append_note(admin.id, ticket['id'], {'body': 'secret', 'revision': 5, 'visibility': 'public', 'idempotencyKey': 'invalid'})


def test_concurrent_private_note_retry_writes_only_one_note(support):
    service, auth, (first, _, admin), store = support
    second = SupportService(auth, cad_store=store)
    ticket = service.create(first.id, create_values())
    barrier = Barrier(2)
    def write(current):
        barrier.wait()
        return current.append_note(admin.id, ticket['id'], {'body': '并发重试的内部记录', 'revision': 1, 'idempotencyKey': 'same-note'})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [service, second]))
        assert results[0]['noteId'] == results[1]['noteId']
        assert service.notes(admin.id, ticket['id'])['total'] == 1
        assert service.ticket(first.id, ticket['id'])['messageCount'] == 1
        assert service.ticket(first.id, ticket['id'])['revision'] == 2
    finally: second.close()
