from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform import AuthService
from app.support_api import create_support_router


@pytest.fixture
def api(tmp_path):
    auth = AuthService(tmp_path / "auth.sqlite3", token_secret="support-api-secret-" * 3)
    first, other, admin = [auth.create_user(f"{name}@example.test", "a-long-test-password", name, roles=["admin" if name == "admin" else "viewer"]) for name in ("first", "other", "admin")]
    router = create_support_router(SimpleNamespace(auth=auth), cad_store=SimpleNamespace(load=lambda run_id: {"owner": first.id}), max_body_bytes=8192)
    app = FastAPI(); app.include_router(router, prefix="/api/v1")
    headers = lambda user: {"Authorization": "Bearer " + auth.issue_token(user).token}
    with TestClient(app) as client: yield client, auth, first, other, admin, headers
    auth.close()


def values(**updates):
    return {"subject": "实际支持问题", "category": "other", "body": "请说明操作步骤", "runId": None, "drawingConsent": False, "idempotencyKey": "request-one", **updates}


def test_support_routes_authenticate_and_hide_other_accounts(api):
    client, _, first, other, admin, headers = api
    base = "/api/v1/support"
    assert client.get(base + "/tickets").status_code == 401
    assert client.post(base + "/tickets", json=values()).status_code == 401
    created = client.post(base + "/tickets", json=values(), headers=headers(first))
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    ticket = created.json()
    for suffix in ("", "/messages"):
        assert client.get(base + "/tickets/" + ticket["id"] + suffix, headers=headers(other)).status_code == 404
    assert client.get(base + "/tickets", headers=headers(other)).json()["items"] == []
    assert client.get(base + "/admin/tickets", headers=headers(first)).status_code == 403
    assert client.get(base + "/admin/tickets", headers=headers(admin)).json()["total"] == 1
    assert client.get(base + "/admin/tickets/" + ticket["id"] + "/audit", headers=headers(first)).status_code == 403
    assert client.get(base + "/admin/tickets/" + ticket["id"] + "/audit", headers=headers(admin)).json()["total"] == 2


def test_owner_forgery_unowned_run_and_malformed_bodies_fail_without_writes(api):
    client, _, first, other, _, headers = api
    path = "/api/v1/support/tickets"
    assert client.post(path, json=values(ownerId=other.id), headers=headers(first)).status_code == 422
    assert client.post(path, json=values(runId="owned-by-first"), headers=headers(other)).status_code == 404
    assert client.post(path, content="x" * 9000, headers={**headers(first), "Content-Type": "application/json", "Content-Length": "1"}).status_code == 413
    assert client.post(path, content="not-json", headers={**headers(first), "Content-Type": "application/json"}).status_code == 422
    assert client.post(path, content="{}", headers={**headers(first), "Content-Type": "text/plain"}).status_code == 415
    assert client.get(path, headers=headers(first)).json()["total"] == 0


def test_idempotent_reply_and_consent_revoke_use_real_ticket_and_cas(api):
    client, _, first, _, admin, headers = api
    path = "/api/v1/support/tickets"
    ticket = client.post(path, json=values(runId="run-first", drawingConsent=True), headers=headers(first)).json()
    detail = path + "/" + ticket["id"]
    reply = {"body": "追加说明", "idempotencyKey": "repeat"}
    one = client.post(detail + "/messages", json=reply, headers=headers(first))
    two = client.post(detail + "/messages", json=reply, headers=headers(first))
    assert one.status_code == two.status_code == 201 and one.json()["message"]["id"] == two.json()["message"]["id"]
    assert client.patch(detail, json={"revision": 1, "drawingConsent": False, "idempotencyKey": "stale"}, headers=headers(first)).status_code == 409
    revoked = client.patch(detail, json={"revision": 2, "drawingConsent": False, "idempotencyKey": "revoke"}, headers=headers(first))
    assert revoked.json()["drawingConsent"] is False
    assert client.get(detail + "/messages?limit=1&offset=1", headers=headers(first)).json()["total"] == 2
    assert client.get(detail + "/messages?limit=101", headers=headers(first)).status_code == 422
    admin_path = detail.replace("/support/tickets", "/support/admin/tickets")
    assert client.patch(admin_path, json={"revision": 3, "status": "resolved", "reason": "已记录处理结论", "idempotencyKey": "resolve"}, headers=headers(admin)).json()["status"] == "resolved"
    schema = client.get("/openapi.json").json()
    assert not any("source" in name or "artifact" in name or "download" in name for name in schema["paths"])


def test_disabled_account_and_revoked_admin_role_cannot_read_or_reply(api):
    client, auth, first, _, admin, headers = api
    first_headers, admin_headers = headers(first), headers(admin)
    ticket = client.post("/api/v1/support/tickets", json=values(), headers=first_headers).json()
    auth.set_active(first.id, False, actor_id=admin.id)
    assert client.get("/api/v1/support/tickets", headers=first_headers).status_code == 401
    assert client.post(f"/api/v1/support/tickets/{ticket['id']}/messages", headers=first_headers, json={"body": "停止的账号", "idempotencyKey": "inactive"}).status_code == 401


def test_workdesk_assignment_private_notes_and_filtering_are_real_admin_routes(api):
    client, auth, first, other, admin, headers = api
    base = '/api/v1/support'
    staff = auth.create_user('desk@example.test', 'a-long-test-password', '客服人员', roles=['support'])
    ticket = client.post(base + '/tickets', json=values(), headers=headers(first)).json()
    path = base + '/admin/tickets/' + ticket['id']
    candidates = client.get(base + '/admin/assignees', headers=headers(admin))
    assert candidates.status_code == 200 and {item['id'] for item in candidates.json()['items']} == {admin.id, staff.id}
    change = {'assignedTo': staff.id, 'priority': 'urgent', 'revision': 1, 'reason': '交接给客服', 'idempotencyKey': 'assign'}
    assigned = client.patch(path, json=change, headers=headers(admin))
    assert assigned.status_code == 200 and assigned.json()['assignee']['displayName'] == '客服人员'
    assert client.patch(base + '/tickets/' + ticket['id'], json=change, headers=headers(first)).status_code == 422
    filtered = client.get(base + '/admin/tickets?status=pending&assignedTo=me&priority=urgent&q=实际', headers=headers(staff))
    assert filtered.status_code == 200 and filtered.json()['total'] == 1
    assert filtered.json()['views']['mine'] == 1
    body = {'body': '仅内部使用的分析说明', 'revision': 2, 'idempotencyKey': 'note'}
    noted = client.post(path + '/notes', json=body, headers=headers(staff))
    assert noted.status_code == 201 and noted.headers['cache-control'] == 'no-store'
    assert client.post(path + '/notes', json=body, headers=headers(staff)).json()['noteId'] == noted.json()['noteId']
    assert client.get(path + '/notes', headers=headers(admin)).json()['total'] == 1
    for who in (first, other):
        for route in (path + '/notes', base + '/admin/assignees'):
            assert client.get(route, headers=headers(who)).status_code == 403
        assert client.post(path + '/notes', json=body, headers=headers(who)).status_code == 403
    assert client.get(base + '/tickets/' + ticket['id'] + '/notes', headers=headers(first)).status_code == 404
    for suffix in ('', '/messages'):
        public = client.get(base + '/tickets/' + ticket['id'] + suffix, headers=headers(first)).text
        assert '仅内部使用' not in public and 'internalNoteCount' not in public and 'assignedTo' not in public
    assert client.get(path + '/notes?offset=100&limit=101', headers=headers(admin)).status_code == 422
    for query in ('priority=secret', 'assignedTo=../admin', 'q=' + 'x'*129):
        assert client.get(base + '/admin/tickets?' + query, headers=headers(admin)).status_code == 422
    auth.assign_roles(staff.id, ['viewer'], actor_id=admin.id)
    assert client.get(path + '/notes', headers=headers(staff)).status_code in (401, 403)
