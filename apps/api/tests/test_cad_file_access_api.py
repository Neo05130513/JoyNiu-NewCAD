"""Exercise protected CAD HTTP routes using saved local fixtures, never AI."""
from copy import deepcopy
import hashlib
import io
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.cad_agent_api import create_cad_agent_router
from app.cad_agent_store import CadRunStore
from app.cad_file_access import CadFileAccess
from app.platform import AuthService
from app.platform_api import build_platform_services
from tests.test_cad_agent_api import PLAN, fake_execute, independent_review


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JOYNIU_ENV", "production")
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "0")
    monkeypatch.delenv("JOYNIU_BILLING_ENABLED", raising=False)
    monkeypatch.delenv("JOYNIU_CAD_ALLOW_LEGACY_FILE_TOKENS", raising=False)
    monkeypatch.setattr(AuthService, "_PBKDF2_ITERATIONS", 1000)
    services = build_platform_services(":memory:", auth_secret="cad-file-access-test-secret-long-enough")
    users = [services.auth.create_user(f"{name}@example.test", "fixture-password-123", name, roles=("designer",))
             for name in ["owner", "other"]]
    headers = [{"Authorization": "Bearer " + services.auth.issue_token(user).token} for user in users]
    store = CadRunStore(tmp_path / "cad")
    run_id = "cad_" + "d" * 32
    directory = store.directory(run_id)
    directory.mkdir()
    image = io.BytesIO()
    Image.new("RGB", (12, 12), "white").save(image, "PNG")
    data = image.getvalue()
    original = directory / "source.png"
    original.write_bytes(data)
    result = fake_execute(deepcopy(PLAN), directory / "built")
    record = {**result, "runId": run_id, "revision": 1, "owner": users[0].id,
              "status": "review_required", "downloadToken": "old-permanent-url-secret",
              "files": [{"path": str(original), "filename": "图纸.png", "contentType": "image/png", "sha256": hashlib.sha256(data).hexdigest()}],
              "drawingReview": {"status": "consistent", "independentReview": independent_review(PLAN)},
              "observations": [{"id": "one", "kind": "solid_count", "expected": 1, "source": {"type": "user", "text": "one plate"}}]}
    store.save(record)
    # Explicitly fail any accidental model call in the download-only suite.
    class NoProvider:
        def run(self, **kwargs):
            raise AssertionError("File HTTP tests must not invoke an AI provider")
    router = create_cad_agent_router(services, store, agent=NoProvider(), executor=fake_execute)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="https://cad.example.test") as client:
        yield client, services, store, record, users, headers, data
    services.close()


def load(client, record, headers):
    response = client.get(f"/api/v1/cad-agent/runs/{record['runId']}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def file_urls(public):
    return [public["artifacts"][0]["url"], public["sourceDocuments"][0]["downloadUrl"], public["sourceDocuments"][0]["pages"][0]["url"]]


def test_current_owner_receives_temporary_urls_for_all_three_file_kinds(api):
    client, _, _, record, _, headers, data = api
    public = load(client, record, headers[0])
    assert public["fileLinksAvailable"] is True and public["fileLinksExpiresAt"]
    assert record["downloadToken"] not in json.dumps(public)
    urls = file_urls(public)
    expected = [b"test-glb", data, None]
    for url, content in zip(urls, expected):
        assert parse_qs(urlparse(url).query)["access"][0].startswith("v1.")
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        if content is not None:
            assert response.content == content
    assert client.get(urls[2]).headers["content-type"] == "image/png"


def test_signature_cannot_be_reused_on_another_resource_and_bearer_cannot_cross_owner(api):
    client, _, _, record, _, headers, _ = api
    public = load(client, record, headers[0])
    glb, original, page = file_urls(public)
    access = parse_qs(urlparse(glb).query)["access"][0]
    for target in [original, page]:
        assert client.get(urlparse(target).path + "?access=" + access).status_code == 403
    assert client.get(glb, headers=headers[1]).status_code == 403
    assert client.get(urlparse(glb).path, headers=headers[1]).status_code == 403
    assert client.get(urlparse(glb).path, headers=headers[0]).status_code == 200
    assert client.get(urlparse(glb).path).status_code == 403


def test_expired_url_is_rejected_but_owner_can_refresh_and_direct_download(api):
    client, services, store, record, _, headers, _ = api
    old_service = CadFileAccess(store, services.auth, clock=lambda: 1_600_000_000)
    expired = old_service.issue(record, "artifacts/glb")
    path = f"/api/v1/cad-agent/runs/{record['runId']}/1/artifacts/glb"
    assert client.get(path + "?access=" + expired).status_code == 403
    assert client.get(path + "?access=" + expired, headers=headers[0]).status_code == 200
    fresh = load(client, record, headers[0])
    assert client.get(fresh["artifacts"][0]["url"]).status_code == 200


def test_owner_revokes_all_old_links_without_changing_model_and_new_links_work(api):
    client, _, store, record, users, headers, _ = api
    public = load(client, record, headers[0])
    old_urls = file_urls(public)
    path = f"/api/v1/cad-agent/runs/{record['runId']}/revoke-file-links"
    before = store.load(record["runId"])
    assert client.post(path, headers=headers[1], json={}).status_code == 403
    assert client.post(path, json={}).status_code in {401, 403}
    revoked = client.post(path, headers=headers[0], json={})
    assert revoked.status_code == 200, revoked.text
    new = revoked.json()
    assert new["revision"] == 1 and new["fileLinksEpoch"] == 1 and new["revokedAt"]
    assert store.load(record["runId"]) == before
    for old in old_urls:
        assert client.get(old).status_code == 403
    for fresh in file_urls(new):
        assert client.get(fresh).status_code == 200
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT actor_id,action FROM cad_file_access_audit").fetchall() == [(users[0].id, "file_links.revoked")]


def test_disabled_account_loses_existing_signed_links_and_bearer_access(api):
    client, services, _, record, users, headers, _ = api
    public = load(client, record, headers[0])
    services.auth.set_active(users[0].id, False, actor_id="system")
    for url in file_urls(public):
        assert client.get(url).status_code == 403
        assert client.get(url, headers=headers[0]).status_code in {401, 403}


def test_legacy_compatibility_is_explicit_noncommercial_and_revoke_is_final(api, monkeypatch):
    client, _, _, record, _, headers, _ = api
    path = f"/api/v1/cad-agent/runs/{record['runId']}/1/artifacts/glb?access={record['downloadToken']}"
    assert client.get(path).status_code == 403
    monkeypatch.setenv("JOYNIU_CAD_ALLOW_LEGACY_FILE_TOKENS", "true")
    assert client.get(path).status_code == 200
    monkeypatch.setenv("JOYNIU_BILLING_ENABLED", "true")
    assert client.get(path).status_code == 403
    # Owner authentication remains sufficient during migration and paid mode.
    assert client.get(path, headers=headers[0]).status_code == 200
    monkeypatch.delenv("JOYNIU_BILLING_ENABLED")
    revoked = client.post(f"/api/v1/cad-agent/runs/{record['runId']}/revoke-file-links", headers=headers[0], json={})
    assert revoked.status_code == 200
    assert client.get(path).status_code == 403


def test_signatures_do_not_bypass_step_confirmation_or_filesystem_boundaries(api):
    client, services, store, record, _, headers, _ = api
    service = CadFileAccess(store, services.auth)
    step_path = f"/api/v1/cad-agent/runs/{record['runId']}/1/artifacts/step"
    step_access = service.issue(record, "artifacts/step")
    assert client.get(step_path + "?access=" + step_access).status_code == 409
    assert client.get(step_path, headers=headers[0]).status_code == 409
    # A valid signature still cannot cause reading a path outside the CAD store.
    outside = store.root.parent / "private-file.glb"
    outside.write_bytes(b"should-not-leak")
    changed = deepcopy(record)
    changed["revision"] = 2
    changed["artifacts"]["glb"]["path"] = str(outside)
    store.save(changed, previous_revision=1)
    access = service.issue(changed, "artifacts/glb")
    denied = client.get(f"/api/v1/cad-agent/runs/{record['runId']}/2/artifacts/glb?access={access}")
    assert denied.status_code == 404 and b"should-not-leak" not in denied.content
