"""Only synthetic test text; no real legal documents, payment or provider calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.commercial_terms import CommercialTermsService, CommercialTermsUnavailable, KINDS
from app.commercial_terms_api import create_commercial_terms_router
from app.platform import AuthService, AuthorizationError, ConflictError, ValidationError
from app.platform_api import build_platform_services
from tests.test_billing import account, package
from tests.test_billing_policy import document as pricing_document


PASSWORD = "terms-fixture-password"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(AuthService, "_PBKDF2_ITERATIONS", 1000)
    services = build_platform_services(":memory:", auth_secret="terms-regression-secret-at-least-32-bytes")
    users = [services.auth.create_user(f"{name}@example.test", PASSWORD, name, roles=(role,))
             for name, role in [("admin", "admin"), ("owner", "designer"), ("other", "designer")]]
    headers = [{"Authorization": "Bearer " + services.auth.issue_token(user).token} for user in users]
    app = FastAPI()
    router = create_commercial_terms_router(services)
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        yield services, router.terms_service, client, users, headers
    services.close()


def body(kind, revision, version="test-v1"):
    return {"kind": kind, "title": f"{kind} fixture title", "version": version,
            "body": f"Synthetic {kind} fixture only. This is not a legal document.", "expectedRevision": revision}


def publish_all(service, admin_id):
    for kind in KINDS:
        snapshot = service.publish(admin_id, body(kind, service.terms()["revision"]))
    return snapshot


def ids(snapshot):
    return [document["id"] for document in snapshot["documents"]]


def test_empty_database_has_no_fabricated_documents_or_consent(api):
    _, service, client, users, headers = api
    response = client.get("/api/v1/commercial/terms")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"ready": False, "revision": 0, "documents": []}
    assert service.ready() is False
    assert client.get("/api/v1/commercial/acceptance").status_code == 401
    assert client.get("/api/v1/commercial/acceptance", headers=headers[1]).json() == {
        "ready": False, "revision": 0, "accepted": False, "receipt": None}
    with pytest.raises(CommercialTermsUnavailable):
        service.require_acceptance(users[1].id)
    response = client.post("/api/v1/commercial/acceptance", headers=headers[1], json={
        "documentIds": ["terms_" + digit * 32 for digit in "abc"], "idempotencyKey": "missing-terms-123"})
    assert response.status_code == 503


def test_only_current_administrator_can_publish_and_all_three_kinds_are_needed(api):
    _, service, client, users, headers = api
    assert client.post("/api/v1/commercial/admin/terms", json=body("service", 0)).status_code == 401
    assert client.post("/api/v1/commercial/admin/terms", headers=headers[1], json=body("service", 0)).status_code == 403
    for index, kind in enumerate(KINDS):
        response = client.post("/api/v1/commercial/admin/terms", headers=headers[0], json=body(kind, index))
        assert response.status_code == 201, response.text
        snapshot = response.json()
        assert snapshot["revision"] == index + 1
        assert snapshot["ready"] is (index == 2)
        document = next(row for row in snapshot["documents"] if row["kind"] == kind)
        assert set(document) == {"id", "kind", "title", "version", "body", "publishedAt"}
    assert service.ready() is True
    assert service.acceptance(users[1].id)["accepted"] is False
    with pytest.raises(ConflictError):
        service.require_acceptance(users[1].id)


def test_explicit_acceptance_is_bound_to_current_documents_and_customer(api):
    _, service, client, users, headers = api
    current = publish_all(service, users[0].id)
    payload = {"documentIds": ids(current), "idempotencyKey": "accept-current-123"}
    response = client.post("/api/v1/commercial/acceptance", headers=headers[1], json=payload)
    assert response.status_code == 200 and response.json()["accepted"] is True
    receipt = response.json()["receipt"]
    assert receipt["ownerId"] == users[1].id and receipt["revision"] == 3
    assert receipt["documentIds"] == sorted(ids(current))
    assert receipt["versions"] == {document["kind"]: document["id"] for document in current["documents"]}
    assert receipt["acceptanceId"].startswith("accept_") and receipt["acceptedAt"]
    assert service.require_acceptance(users[1].id) == receipt
    assert client.get("/api/v1/commercial/acceptance", headers=headers[2]).json()["accepted"] is False
    # The same request key is scoped to the authenticated account, not a
    # shared consent token that could copy another customer's acceptance.
    other = client.post("/api/v1/commercial/acceptance", headers=headers[2], json=payload).json()["receipt"]
    assert other["ownerId"] == users[2].id and other["acceptanceId"] != receipt["acceptanceId"]
    assert client.post("/api/v1/commercial/acceptance", headers=headers[2], json={**payload, "ownerId": users[1].id}).status_code == 422


def test_replays_and_double_clicks_do_not_duplicate_acceptance_or_audit(api):
    services, service, client, users, headers = api
    current = publish_all(service, users[0].id)
    payload = {"documentIds": ids(current), "idempotencyKey": "accept-current-123"}
    first = client.post("/api/v1/commercial/acceptance", headers=headers[1], json=payload).json()
    for request in [payload, {**payload, "documentIds": list(reversed(payload["documentIds"]))},
                    {**payload, "idempotencyKey": "a-second-click-123"}]:
        replay = client.post("/api/v1/commercial/acceptance", headers=headers[1], json=request)
        assert replay.status_code == 200 and replay.json() == first
    db = services.auth._connection
    assert db.execute("SELECT COUNT(*) FROM commercial_terms_acceptances").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM commercial_terms_audit WHERE action='terms.accepted'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM commercial_terms_acceptance_keys").fetchone()[0] == 2


def test_publishing_new_version_invalidates_current_consent_without_erasing_history(api):
    services, service, client, users, headers = api
    original = publish_all(service, users[0].id)
    payload = {"documentIds": ids(original), "idempotencyKey": "accept-current-123"}
    first = client.post("/api/v1/commercial/acceptance", headers=headers[1], json=payload).json()["receipt"]
    updated = service.publish(users[0].id, body("privacy", 3, "test-v2"))
    assert service.acceptance(users[1].id) == {"ready": True, "revision": 4, "accepted": False, "receipt": None}
    with pytest.raises(ConflictError):
        service.require_acceptance(users[1].id)
    assert client.post("/api/v1/commercial/acceptance", headers=headers[1], json=payload).status_code == 409
    assert client.post("/api/v1/commercial/acceptance", headers=headers[1], json={**payload, "documentIds": ids(updated)}).status_code == 409
    new = client.post("/api/v1/commercial/acceptance", headers=headers[1], json={
        "documentIds": ids(updated), "idempotencyKey": "explicit-new-consent-123"})
    assert new.status_code == 200 and new.json()["receipt"]["acceptanceId"] != first["acceptanceId"]
    assert services.auth._connection.execute("SELECT COUNT(*) FROM commercial_terms_acceptances").fetchone()[0] == 2
    assert services.auth._connection.execute("SELECT COUNT(*) FROM commercial_terms_documents").fetchone()[0] == 4
    assert client.get("/api/v1/commercial/admin/terms/history", headers=headers[1]).status_code == 403
    history = client.get("/api/v1/commercial/admin/terms/history", headers=headers[0]).json()
    assert history["total"] == 4 and sum(document["active"] for document in history["documents"]) == 3


@pytest.mark.parametrize("change", [{"kind": "other"}, {"kind": []}, {"body": "   "}, {"body": "x" * 100001},
    {"body": 42}, {"title": ""}, {"version": ""}, {"expectedRevision": True}, {"expectedRevision": -1},
    {"publishedBy": "forged"}, {"accepted": True}])
def test_invalid_publish_fields_never_create_partial_version(api, change):
    _, service, client, _, headers = api
    assert client.post("/api/v1/commercial/admin/terms", headers=headers[0], json={**body("service", 0), **change}).status_code == 422
    assert service.terms() == {"ready": False, "revision": 0, "documents": []}


def test_publish_cas_and_immutable_version_label_prevent_overwriting_reviewed_text(api):
    services, service, client, users, headers = api
    service.publish(users[0].id, body("service", 0))
    assert client.post("/api/v1/commercial/admin/terms", headers=headers[0], json=body("privacy", 0)).status_code == 409
    assert client.post("/api/v1/commercial/admin/terms", headers=headers[0], json={**body("service", 1), "body": "Altered fixture"}).status_code == 409
    assert service.terms()["revision"] == 1
    assert services.auth._connection.execute("SELECT COUNT(*) FROM commercial_terms_documents").fetchone()[0] == 1


@pytest.mark.parametrize("case", ["missing", "duplicate", "forged", "bad_key", "extra"])
def test_forged_or_incomplete_acceptance_does_not_grant_consent(api, case):
    _, service, client, users, headers = api
    current = publish_all(service, users[0].id)
    payload = {"documentIds": ids(current), "idempotencyKey": "accept-current-123"}
    expected = 422
    if case == "missing":
        payload["documentIds"] = payload["documentIds"][:2]
    elif case == "duplicate":
        payload["documentIds"][1] = payload["documentIds"][0]
    elif case == "forged":
        payload["documentIds"][1] = "terms_" + "a" * 32
        expected = 409
    elif case == "bad_key":
        payload["idempotencyKey"] = "short"
    else:
        payload["acceptedAt"] = "forged-time"
    assert client.post("/api/v1/commercial/acceptance", headers=headers[1], json=payload).status_code == expected
    assert service.acceptance(users[1].id)["accepted"] is False


def test_receipts_documents_and_audit_cannot_be_rewritten_or_deleted(api):
    services, service, _, users, _ = api
    current = publish_all(service, users[0].id)
    service.accept(users[1].id, document_ids=ids(current), idempotency_key="fixture-accept-123")
    for table, column in [("commercial_terms_documents", "body"), ("commercial_terms_acceptances", "accepted_at"),
                          ("commercial_terms_acceptance_keys", "request_hash"), ("commercial_terms_audit", "at")]:
        for statement in [f"DELETE FROM {table}", f"UPDATE {table} SET {column}='tampered'"]:
            with pytest.raises(sqlite3.IntegrityError):
                services.auth._connection.execute(statement)
            services.auth._connection.rollback()


def test_disabled_or_removed_user_cannot_get_or_grant_consent(api):
    services, service, client, users, headers = api
    current = publish_all(service, users[0].id)
    service.accept(users[1].id, document_ids=ids(current), idempotency_key="fixture-accept-123")
    services.auth.set_active(users[1].id, False, actor_id=users[0].id)
    assert client.get("/api/v1/commercial/acceptance", headers=headers[1]).status_code == 401
    with pytest.raises(AuthorizationError):
        service.require_acceptance(users[1].id)
    with pytest.raises(AuthorizationError):
        service.accept("missing-user", document_ids=ids(current), idempotency_key="fixture-forgery-123")


def test_multiple_workers_publish_with_cas_and_accept_once_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthService, "_PBKDF2_ITERATIONS", 1000)
    path = tmp_path / "auth.sqlite3"
    auths = [AuthService(path, token_secret="multi-worker-terms-fixture-secret-123") for _ in range(4)]
    admin = auths[0].create_user("admin@example.test", PASSWORD, "Admin", roles=("admin",))
    owner = auths[0].create_user("owner@example.test", PASSWORD, "Owner", roles=("designer",))
    workers = [CommercialTermsService(auth) for auth in auths]
    barrier = threading.Barrier(4)

    def publish(worker):
        barrier.wait()
        try:
            return worker.publish(admin.id, body("service", 0))
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        published = list(pool.map(publish, workers))
    assert sum(item == "conflict" for item in published) == 3
    workers[0].publish(admin.id, body("privacy", 1))
    snapshot = workers[0].publish(admin.id, body("credits", 2))
    barrier = threading.Barrier(4)

    def accept(worker):
        barrier.wait()
        return worker.accept(owner.id, document_ids=ids(snapshot), idempotency_key="same-concurrent-accept")

    with ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(accept, workers))
    assert all(result == accepted[0] for result in accepted)
    for auth in auths:
        auth.close()
    restarted = AuthService(path, token_secret="multi-worker-terms-fixture-secret-123")
    try:
        service = CommercialTermsService(restarted)
        assert service.terms() == snapshot
        assert service.require_acceptance(owner.id) == accepted[0]["receipt"]
        assert restarted._connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restarted._connection.execute("SELECT COUNT(*) FROM commercial_terms_acceptances").fetchone()[0] == 1
    finally:
        restarted.close()


def test_actual_billing_and_task_hooks_keep_receipt_and_block_new_unaccepted_versions(account):
    from app.billing import BillingUnavailable
    from app.billing_policy import BillingPolicyService
    billing, auth, admin, user, _, gateway = account
    terms = billing.terms_provider = CommercialTermsService(auth)
    plan = package(billing, admin)
    with pytest.raises(BillingUnavailable):
        billing.create_order(user.id, package_id=plan["id"], idempotency_key="fixture-order")
    snapshot = publish_all(terms, admin.id)
    with pytest.raises(ConflictError):
        billing.create_order(user.id, package_id=plan["id"], idempotency_key="fixture-order")
    assert gateway.calls == []
    accepted = terms.accept(user.id, document_ids=ids(snapshot), idempotency_key="fixture-acceptance")
    order = billing.create_order(user.id, package_id=plan["id"], idempotency_key="fixture-order")
    assert order["termsAcceptance"]["acceptanceId"] == accepted["receipt"]["acceptanceId"]
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    pricing = policy.save_version(admin.id, pricing_document())
    policy.set_enabled(admin.id, revision=0, version_id=pricing["id"], enabled=True, reason="Fixture only")
    billing.adjust(admin.id, owner_id=user.id, credit_units=20, reason="TEST ONLY", idempotency_key="fixture-balance")
    policy.register_attempt(owner_id=user.id, job_id="fixture-job", attempt_id="fixture-attempt")
    saved = json.loads(billing._db.execute("SELECT terms_json FROM billing_policy_attempts WHERE attempt_id='fixture-attempt'").fetchone()[0])
    assert saved == order["termsAcceptance"]
    terms.publish(admin.id, body("credits", 3, "fixture-new-version"))
    with pytest.raises(ConflictError):
        billing.create_order(user.id, package_id=plan["id"], idempotency_key="new-fixture-order")
    with pytest.raises(ConflictError):
        policy.register_attempt(owner_id=user.id, job_id="fixture-job", attempt_id="new-fixture-attempt")
    assert len(gateway.calls) == 1
    assert billing.create_order(user.id, package_id=plan["id"], idempotency_key="fixture-order")["termsAcceptance"] == saved
    assert policy.register_attempt(owner_id=user.id, job_id="fixture-job", attempt_id="fixture-attempt")["chargeEnabled"] is True


def test_waiting_publisher_does_not_deadlock_billing_current_terms_read(account, monkeypatch):
    billing, auth, admin, user, _, _ = account
    terms = billing.terms_provider = CommercialTermsService(auth)
    snapshot = publish_all(terms, admin.id)
    terms.accept(user.id, document_ids=ids(snapshot), idempotency_key="fixture-acceptance")
    waiting = threading.Event()
    connect = terms._connect

    def traced_connection():
        db = connect()
        db.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
        return db

    monkeypatch.setattr(terms, "_connect", traced_connection)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with billing._transaction():
            publication = pool.submit(terms.publish, admin.id, body("privacy", 3, "fixture-new-version"))
            assert waiting.wait(2), "publisher should contend for the same SQLite writer"
            assert not publication.done()
            # The writer owns a transaction before reading the receipt. A
            # terms reader must not wait behind the competing publisher lock.
            assert billing.require_terms(user.id)["versions"] == {row["kind"]: row["id"] for row in snapshot["documents"]}
        assert publication.result(timeout=3)["revision"] == 4
    with pytest.raises(ConflictError):
        billing.require_terms(user.id)
