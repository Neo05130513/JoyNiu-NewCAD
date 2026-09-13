from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.account_workspace_api import create_account_workspace_router
from app.platform import AuthService


@pytest.fixture
def account_api(tmp_path):
    auth = AuthService(tmp_path / "accounts.sqlite3", token_secret="workspace-test-" * 4)
    users = [auth.create_user(f"{name}@example.com", "a-long-test-password", name, roles=["viewer"]) for name in ["A", "B"]]
    app = FastAPI()
    app.include_router(create_account_workspace_router(SimpleNamespace(auth=auth), max_body_bytes=4096), prefix="/api/v1")
    headers = [{"Authorization": f"Bearer {auth.issue_token(user).token}"} for user in users]
    try:
        with TestClient(app) as client:
            yield client, headers, auth, users
    finally:
        auth.close()


def empty_store(**extra):
    return {"schemaVersion": 1, "projects": [], "activeProjectId": None, "activeFileId": None, **extra}


def test_current_account_only_even_when_owner_is_forged(account_api):
    client, headers, _auth, users = account_api
    path = "/api/v1/account/workspace"
    assert client.get(path).status_code == 401
    assert client.put(path, json={}).status_code == 401
    result = client.put(path, headers=headers[0], json={
        "expectedRevision": 0, "ownerId": users[1].id, "snapshot": empty_store(privateNote="A secret workspace"),
    })
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert client.get(f"{path}?ownerId={users[1].id}", headers=headers[0]).json() == result.json()
    other = client.get(f"{path}?ownerId={users[0].id}", headers=headers[1])
    assert other.json() == {"revision": 0, "snapshot": None, "updatedAt": None}
    assert "A secret workspace" not in other.text


def test_conflict_only_reports_current_accounts_revision(account_api):
    client, headers, _auth, _users = account_api
    path = "/api/v1/account/workspace"
    assert client.put(path, headers=headers[0], json={"expectedRevision": 0, "snapshot": empty_store(note="private A")}).status_code == 200
    conflict = client.put(path, headers=headers[0], json={"expectedRevision": 0, "snapshot": empty_store()})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "workspace_revision_conflict", "message": "工作区已在其他窗口更新，请重新加载后再保存。", "revision": 1,
    }
    assert "private A" not in conflict.text and "snapshot" not in conflict.text
    other = client.put(path, headers=headers[1], json={"expectedRevision": 1, "snapshot": empty_store()})
    assert other.status_code == 409
    assert other.json()["detail"]["revision"] == 0


def test_inactive_accounts_cannot_read_or_overwrite(account_api):
    client, headers, auth, users = account_api
    auth.set_active(users[0].id, False, actor_id="test-admin")
    assert client.get("/api/v1/account/workspace", headers=headers[0]).status_code == 401
    assert client.put("/api/v1/account/workspace", headers=headers[0], json={"expectedRevision": 0, "snapshot": empty_store()}).status_code == 401


def test_body_size_and_json_validation_precede_persistence(account_api):
    client, headers, _auth, _users = account_api
    path = "/api/v1/account/workspace"
    assert client.put(path, headers=headers[0], json={"expectedRevision": 0, "snapshot": empty_store(note="x" * 5000)}).status_code == 413
    # A caller cannot evade the streamed count by sending a smaller Content-Length.
    response = client.put(path, headers={**headers[0], "Content-Type": "application/json", "Content-Length": "1"}, content='{"note":"' + "x" * 5000 + '"}')
    assert response.status_code == 413
    assert client.put(path, headers={**headers[0], "Content-Type": "application/json"}, content="{").status_code == 422
    assert client.put(path, headers={**headers[0], "Content-Type": "text/plain"}, content="{}").status_code == 415
    assert client.put(path, headers=headers[0], json={"snapshot": empty_store()}).status_code == 422
    assert client.put(path, headers=headers[0], json={"expectedRevision": 0, "snapshot": empty_store(sessionToken="credentials")}).status_code == 422
    assert client.get(path, headers=headers[0]).json()["revision"] == 0


def test_openapi_resolves_streaming_request_annotation(account_api):
    client, *_ = account_api
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]["/api/v1/account/workspace"]) == {"get", "put"}
