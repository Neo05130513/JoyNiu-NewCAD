"""Real HMAC/SQLite tests; no network, CAD kernel or model provider calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from app.cad_agent_store import CadRunStore
from app.cad_file_access import CadFileAccess, CadFileAccessDenied


class Accounts:
    active = True

    def get_user(self, owner):
        if owner != "usr_owner":
            raise KeyError(owner)
        return SimpleNamespace(active=self.active)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("JOYNIU_BILLING_ENABLED", raising=False)
    monkeypatch.delenv("JOYNIU_CAD_ALLOW_LEGACY_FILE_TOKENS", raising=False)
    monkeypatch.delenv("JOYNIU_CAD_FILE_LINK_TTL_SECONDS", raising=False)
    store = CadRunStore(tmp_path / "cad")
    record = {"runId": "cad_" + "a" * 32, "revision": 1, "owner": "usr_owner", "status": "failed",
              "downloadToken": "legacy-token-that-was-public", "files": [{"filename": "原图.png"}]}
    store.save(record)
    accounts = Accounts()
    clock = [1900000000]
    service = CadFileAccess(store, accounts, clock=lambda: clock[0])
    return store, record, accounts, clock, service


def token(url):
    return parse_qs(urlparse(url).query)["access"][0]


def test_one_hour_link_is_scoped_to_resource_revision_run_and_owner(fixture):
    store, record, accounts, clock, service = fixture
    resource = "artifacts/glb"
    access = service.issue(record, resource)
    assert access.startswith(f"v1.{clock[0] + 3600}.0.")
    assert service.allows(record, resource, access)
    for different in ["artifacts/step", "artifacts/view-front", "sources/0/download", "sources/0/pages/1"]:
        assert not service.allows(record, different, access)
    other_revision = {**record, "revision": 2}
    store.save(other_revision, previous_revision=1)
    assert not service.allows(other_revision, resource, access)
    other = {**record, "runId": "cad_" + "b" * 32}
    store.save(other)
    assert not service.allows(other, resource, access)
    assert not service.allows({**record, "owner": "usr_other"}, resource, access)
    clock[0] += 3599
    assert service.allows(record, resource, access)
    clock[0] += 1
    assert not service.allows(record, resource, access)
    assert service.allows(record, resource, service.issue(record, resource))


@pytest.mark.parametrize("resource", ["sources/0/download", "sources/0/pages/1", "sources/1/pages/2", "artifacts/view-front"])
def test_drawing_files_and_page_links_are_individually_bound(fixture, resource):
    _, record, _, _, service = fixture
    access = service.issue(record, resource)
    assert service.allows(record, resource, access)
    assert not service.allows(record, resource + "0", access)


@pytest.mark.parametrize("resource", ["../../etc/passwd", "artifacts/../step", "sources/-1/download", "sources/0/pages/0",
    "sources/00/download", "artifacts/glb?other=1", "artifacts/glb\n", "sources/0/pages/1/", "artifacts/%2Fstep"])
def test_unsafe_resource_never_becomes_a_capability(fixture, resource):
    _, record, _, _, service = fixture
    with pytest.raises(CadFileAccessDenied):
        service.issue(record, resource)
    assert not service.allows(record, resource, "legacy-token-that-was-public")


def test_modified_token_cannot_extend_expiry_change_epoch_or_signature(fixture):
    _, record, _, clock, service = fixture
    access = service.issue(record, "artifacts/glb")
    variants = [access.replace(str(clock[0] + 3600), str(clock[0] + 7200)), access.replace(".0.", ".1."),
                access[:-1] + ("A" if access[-1] != "A" else "B"), "v2" + access[2:], access + "=", "x" * 10000,
                access.replace("v1.", "v1.0"), "", None]
    for bad in variants:
        assert not service.allows(record, "artifacts/glb", bad)


def test_unknown_or_disabled_owner_is_denied_without_signing(fixture):
    _, record, accounts, _, service = fixture
    access = service.issue(record, "artifacts/glb")
    accounts.active = False
    assert not service.allows(record, "artifacts/glb", access)
    with pytest.raises(CadFileAccessDenied):
        service.issue(record, "artifacts/glb")
    assert not service.owner_active({**record, "owner": "usr_removed"})
    public = {"artifacts": [{"id": "glb", "url": "old", "downloadUrl": "old"}],
              "sourceDocuments": [{"id": "source-0", "downloadUrl": "old", "pages": [{"page": 1, "url": "old"}]}]}
    safe = service.protect_result(record, public, "/api/v1")
    assert safe["fileLinksAvailable"] is False and safe["fileLinksExpiresAt"] is None
    assert "old" not in json.dumps(safe)
    assert public["artifacts"][0]["url"] == "old"


def test_revocation_is_atomic_persistent_audited_and_preserves_every_geometry_revision(fixture):
    store, record, accounts, clock, service = fixture
    second = {**record, "revision": 2, "downloadToken": "second-permanent-token"}
    store.save(second, previous_revision=1)
    before = [store.load(record["runId"], revision) for revision in [1, 2]]
    first_token, second_token = service.issue(record, "artifacts/glb"), service.issue(second, "sources/0/download")
    stale_worker = CadFileAccess(store, accounts, clock=lambda: clock[0])
    result = service.revoke(second, record["owner"])
    assert result["fileLinksEpoch"] == 1
    assert not stale_worker.allows(record, "artifacts/glb", first_token)
    assert not stale_worker.allows(second, "sources/0/download", second_token)
    new = stale_worker.issue(record, "artifacts/glb")
    assert new != first_token and service.allows(record, "artifacts/glb", new)
    assert [store.load(record["runId"], revision) for revision in [1, 2]] == before
    with sqlite3.connect(store.database) as db:
        audit = db.execute("SELECT run_id,actor_id,action,epoch FROM cad_file_access_audit").fetchall()
        assert audit == [(record["runId"], record["owner"], "file_links.revoked", 1)]
        serialized = str(audit)
        assert first_token not in serialized and record["downloadToken"] not in serialized
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM cad_file_access_audit")
    with pytest.raises(CadFileAccessDenied):
        service.revoke(record, "usr_other")


def test_concurrent_initial_issuance_and_revocations_share_durable_keys(fixture):
    store, record, accounts, clock, service = fixture
    workers = [CadFileAccess(store, accounts, clock=lambda: clock[0]) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        issued = list(pool.map(lambda worker: worker.issue(record, "artifacts/glb"), workers))
    assert len(set(issued)) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        revoked = list(pool.map(lambda worker: worker.revoke(record, record["owner"]), workers))
    assert sorted(item["fileLinksEpoch"] for item in revoked) == list(range(1, 9))
    assert all(not worker.allows(record, "artifacts/glb", issued[0]) for worker in workers)
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT COUNT(*) FROM cad_file_access_audit").fetchone()[0] == 8


def test_permanent_tokens_are_opt_in_noncommercial_and_stay_dead_after_revoke(fixture, monkeypatch):
    store, record, accounts, clock, service = fixture
    old = record["downloadToken"]
    assert not service.allows(record, "artifacts/glb", old)
    monkeypatch.setenv("JOYNIU_CAD_ALLOW_LEGACY_FILE_TOKENS", "true")
    assert service.allows(record, "artifacts/glb", old)
    explicit_off = CadFileAccess(store, accounts, allow_legacy=False, clock=lambda: clock[0])
    assert not explicit_off.allows(record, "artifacts/glb", old)
    monkeypatch.setenv("JOYNIU_BILLING_ENABLED", "true")
    assert not service.allows(record, "artifacts/glb", old)
    monkeypatch.delenv("JOYNIU_BILLING_ENABLED")
    service.revoke(record, record["owner"])
    assert not service.allows(record, "artifacts/glb", old)


@pytest.mark.parametrize("status", [{"enabled": True}, {"commercialMode": True}, {"policyAvailable": False}, None])
def test_live_commercial_policy_or_unavailable_policy_disables_legacy(fixture, status):
    store, record, accounts, _, _ = fixture
    billing = SimpleNamespace(enabled=False, status=lambda: status)
    service = CadFileAccess(store, accounts, billing=billing, allow_legacy=True)
    assert not service.allows(record, "artifacts/glb", record["downloadToken"])
    # Paid mode still permits scoped expiring links for an active real user.
    assert service.allows(record, "artifacts/glb", service.issue(record, "artifacts/glb"))


def test_anonymous_links_require_explicit_local_mode_and_never_commercial(fixture, monkeypatch):
    store, record, _, _, service = fixture
    anonymous = {**record, "runId": "cad_" + "c" * 32, "owner": "local-anonymous"}
    store.save(anonymous)
    with pytest.raises(CadFileAccessDenied):
        service.issue(anonymous, "artifacts/glb")
    local = CadFileAccess(store, allow_anonymous=True)
    access = local.issue(anonymous, "artifacts/glb")
    assert local.allows(anonymous, "artifacts/glb", access)
    monkeypatch.setenv("JOYNIU_BILLING_ENABLED", "1")
    assert not local.allows(anonymous, "artifacts/glb", access)


def test_public_result_replaces_all_urls_without_mutating_original_or_leaking_key(fixture):
    store, record, _, _, service = fixture
    public = {"artifacts": [{"id": "glb", "url": "https://untrusted.test/?secret=old", "downloadUrl": "old"},
                            {"id": "view-front", "url": "old"}],
              "sourceDocuments": [{"id": "source-0", "downloadUrl": "old", "pages": [{"page": 1, "url": "old"}, {"page": 2, "url": "old"}]}]}
    before = deepcopy(public)
    safe = service.protect_result(record, public, "/api/v1")
    assert public == before
    pairs = [(safe["artifacts"][0]["url"], "artifacts/glb"), (safe["artifacts"][1]["url"], "artifacts/view-front"),
             (safe["sourceDocuments"][0]["downloadUrl"], "sources/0/download"),
             (safe["sourceDocuments"][0]["pages"][0]["url"], "sources/0/pages/1"),
             (safe["sourceDocuments"][0]["pages"][1]["url"], "sources/0/pages/2")]
    assert safe["fileLinksAvailable"] is True and safe["fileLinksEpoch"] == 0
    for link, resource in pairs:
        assert link.startswith("/api/v1/") and service.allows(record, resource, token(link))
    assert len({token(link) for link, _ in pairs}) == 5
    with sqlite3.connect(store.database) as db:
        secret = db.execute("SELECT signing_key FROM cad_file_access").fetchone()[0]
    assert secret not in json.dumps(safe) and record["downloadToken"] not in json.dumps(safe)


@pytest.mark.parametrize("value", ["bad", "1.5", 0, -1, 86401, True, float("inf"), float("nan")])
def test_invalid_lifetime_is_a_configuration_error(fixture, value):
    store, _, _, _, _ = fixture
    with pytest.raises(ValueError):
        CadFileAccess(store, ttl_seconds=value)
