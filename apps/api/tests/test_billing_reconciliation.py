"""Durable payment/refund accounting under callbacks, retries and restarts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import BillingService, BillingUnavailable, VerifiedPaymentEvent, VerifiedRefundEvent
from app.billing_api import create_billing_router
from app.platform import AuthService, AuthorizationError, ConflictError, NotFoundError, RateLimitError
from app.platform_api import build_platform_services


class FixtureGateway:
    name = "fixture-payments-only"
    configured = refunds_configured = True

    def __init__(self):
        self.payment_queries = 0
        self.refund_submissions = []
        self.state = "paid"
        self.refund_state = "processing"
        self.fail_submission = False
        self.not_found = False

    def create_order(self, order):
        return {"codeUrl": "https://payments.example.test/" + order["id"]}

    def query_order(self, order):
        self.payment_queries += 1
        result = {"status": self.state}
        if self.state == "paid":
            result["event"] = VerifiedPaymentEvent(self.name, "query-" + order["id"], order["id"], "txn-" + order["id"], order["amountFen"])
        return result

    def refund_event(self, order, request, *, event_id=None, state=None):
        status = state or self.refund_state
        return VerifiedRefundEvent(self.name, event_id or "refund-" + request["id"] + "-" + status,
            order["id"], request["id"], "wx-refund-" + request["id"], order["transactionId"],
            request["data"]["amountFen"], order["amountFen"], status=status)

    def create_refund(self, order, request):
        self.refund_submissions.append((order, request))
        if self.fail_submission:
            raise TimeoutError("private merchant failure")
        return self.refund_event(order, request)

    def query_refund(self, order, request):
        if self.not_found:
            error = BillingUnavailable("signed provider not found")
            error.code = "payment_resource_not_exists"
            raise error
        return self.refund_event(order, request)

    def verify_refund_event(self, body, headers):
        if headers.get("x-fixture-verified") != "yes":
            raise ValueError("never expose private verifier information")
        return self.refund_event(*self.refund_submissions[0], state="succeeded")


@pytest.fixture
def fixture(tmp_path):
    database = tmp_path / "account.sqlite3"
    auth = AuthService(database, token_secret="test-billing-reconciliation-secret-32-bytes")
    admin = auth.create_user("admin@example.test", "test-password", "Admin", roles=["admin"])
    user = auth.create_user("user@example.test", "test-password", "Customer", roles=["designer"])
    other = auth.create_user("other@example.test", "test-password", "Other", roles=["designer"])
    gateway = FixtureGateway()
    service = BillingService(database, auth=auth, payment_adapter=gateway, enabled=True, charging_configured=True, online_payments_enabled=True, refunds_enabled=True)
    plan = service.save_package(admin.id, {"name": "TEST ONLY", "amountFen": 1200, "creditUnits": 100, "active": True})
    order = service.create_order(user.id, package_id=plan["id"], idempotency_key="one")
    yield service, auth, admin, user, other, gateway, order
    service.close()
    auth.close()


def approve(fixture, *, amount=1200):
    service, _, admin, user, _, _, order = fixture
    paid = service.reconcile_order(user.id, order["id"])["order"]
    request = service.request_service(user.id, order["id"], kind="refund", values={"amountFen": amount, "reason": "退款测试"}, idempotency_key="refund-one")
    request = service.review_request(admin.id, request["id"], status="approved", note="运营审核通过")
    return paid, request


def test_query_reconciles_missing_callback_once_and_is_owner_scoped(fixture):
    service, _, admin, user, other, gateway, order = fixture
    with pytest.raises(NotFoundError):
        service.reconcile_order(other.id, order["id"])
    assert gateway.payment_queries == 0
    result = service.reconcile_order(user.id, order["id"])
    assert result["gatewayStatus"] == "paid" and result["order"]["status"] == "paid"
    assert service.wallet(user.id)["creditUnits"] == 100
    with pytest.raises(RateLimitError):
        service.reconcile_order(admin.id, order["id"])
    service.accept_payment(replace(gateway.query_order(order)["event"], event_id="late-callback"))
    assert service.wallet(user.id)["creditUnits"] == 100
    assert service.list_records("ledger", owner_id=user.id)["total"] == 1


def test_query_rate_limit_survives_service_restart(fixture):
    service, auth, _, user, _, gateway, order = fixture
    service.reconcile_order(user.id, order["id"])
    reopened = BillingService(service.database, auth=auth, payment_adapter=gateway)
    try:
        with pytest.raises(RateLimitError):
            reopened.reconcile_order(user.id, order["id"])
    finally:
        reopened.close()


def test_pending_closed_queries_do_not_credit_or_erase_paid_callback(fixture):
    service, _, _, user, _, gateway, order = fixture
    gateway.state = "closed"
    service.accept_payment(VerifiedPaymentEvent(gateway.name, "callback", order["id"], "txn-" + order["id"], 1200))
    result = service.reconcile_order(user.id, order["id"])
    assert result["gatewayStatus"] == "closed" and result["order"]["status"] == "paid"
    assert service.wallet(user.id)["creditUnits"] == 100


def test_refund_disabled_and_partial_approval_never_send_or_debit(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    _, request = approve(fixture, amount=600)
    service.refunds_enabled = False
    with pytest.raises(BillingUnavailable):
        service.execute_refund(admin.id, request["id"])
    service.refunds_enabled = True
    with pytest.raises(ConflictError, match="仅支持整单"):
        service.execute_refund(admin.id, request["id"])
    assert service.wallet(user.id)["creditUnits"] == 100 and gateway.refund_submissions == []
    assert service.list_records("refund", owner_id=user.id)["items"][0]["status"] == "approved"


def test_insufficient_credits_blocks_before_gateway_mutation(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    _, request = approve(fixture)
    service.adjust(admin.id, owner_id=user.id, credit_units=-1, reason="已使用1积分", idempotency_key="used")
    with pytest.raises(ConflictError, match="积分不足"):
        service.execute_refund(admin.id, request["id"])
    assert service.wallet(user.id)["creditUnits"] == 99 and gateway.refund_submissions == []
    assert service._refund_result(request["id"])["refund"] is None


def test_refund_collects_exact_credits_before_network_and_only_verified_success_completes(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    order, request = approve(fixture)
    original = gateway.create_refund
    def inspect(*args):
        assert service.wallet(user.id)["creditUnits"] == 0
        assert service._refund_result(request["id"])["refund"]["status"] == "submitting"
        return original(*args)
    gateway.create_refund = inspect
    result = service.execute_refund(admin.id, request["id"])
    assert result["request"]["status"] == "refund_processing" and result["order"]["status"] == "paid"
    assert result["refund"]["status"] == "processing"
    event = gateway.refund_event(order, request, state="succeeded")
    service.accept_refund(event)
    assert service.accept_refund(event)["duplicate"] is True
    assert service.execute_refund(admin.id, request["id"])["refund"]["status"] == "succeeded"
    assert service._refund_result(request["id"])["order"]["status"] == "refunded"
    assert service.wallet(user.id)["creditUnits"] == 0 and len(gateway.refund_submissions) == 1
    # A delayed payment notification cannot credit a refunded order again.
    service.accept_payment(VerifiedPaymentEvent(gateway.name, "late-payment", order["id"], order["transactionId"], 1200))
    assert service.wallet(user.id)["creditUnits"] == 0
    assert service.order(user.id, order["id"])["status"] == "refunded"


def test_refund_submission_retry_concurrent_and_after_restart_never_redebits(fixture):
    service, auth, admin, user, _, gateway, _ = fixture
    _, request = approve(fixture)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.execute_refund(admin.id, request["id"]), range(4)))
    assert len(gateway.refund_submissions) == 1
    assert all(result["refund"] for result in results)
    reopened = BillingService(service.database, auth=auth, payment_adapter=gateway, refunds_enabled=True)
    try:
        reopened.execute_refund(admin.id, request["id"])
    finally:
        reopened.close()
    assert len(gateway.refund_submissions) == 1 and service.wallet(user.id)["creditUnits"] == 0
    entries = service.list_records("ledger", owner_id=user.id)["items"]
    assert len([entry for entry in entries if entry["kind"] == "refund_recovery"]) == 1


def test_timeout_retains_refund_then_query_completes_without_new_submission(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    _, request = approve(fixture)
    gateway.fail_submission = True
    result = service.execute_refund(admin.id, request["id"])
    assert result["refund"]["status"] == "unknown" and "private" not in result["message"]
    assert service.wallet(user.id)["creditUnits"] == 0
    gateway.refund_state = "succeeded"
    result = service.reconcile_refund(admin.id, request["id"])
    assert result["refund"]["status"] == "succeeded" and len(gateway.refund_submissions) == 1


def test_wrong_submission_event_cannot_complete_another_request(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    order, request = approve(fixture)
    gateway.create_refund = lambda *_: replace(gateway.refund_event(order, request, state="succeeded"), refund_request_id="another-request")
    result = service.execute_refund(admin.id, request["id"])
    assert result["refund"]["status"] == "unknown"
    assert result["order"]["status"] == "paid" and service.wallet(user.id)["creditUnits"] == 0


def test_duplicate_refund_event_rejects_changed_payload_and_callbacks_continue_when_disabled(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    order, request = approve(fixture)
    service.execute_refund(admin.id, request["id"])
    service.refunds_enabled = False
    event = gateway.refund_event(order, request, state="succeeded")
    service.accept_refund(event)
    with pytest.raises(ConflictError, match="重复退款事件"):
        service.accept_refund(replace(event, status="closed"))
    assert service.wallet(user.id)["creditUnits"] == 0
    assert service.dashboard(admin.id)["grossRevenueFen"] == 1200
    assert service.dashboard(admin.id)["revenueFen"] == 0
    assert service.dashboard(admin.id)["refundedFen"] == 1200


def test_verified_not_found_recovers_crash_with_same_refund_id_no_new_debit(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    _, request = approve(fixture)
    gateway.fail_submission = True
    service.execute_refund(admin.id, request["id"])
    gateway.fail_submission = False
    gateway.not_found = True
    with service._transaction() as db:
        db.execute("UPDATE billing_refunds SET updated_at=? WHERE request_id=?", ((datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(), request["id"]))
    result = service.reconcile_refund(admin.id, request["id"])
    assert result["refund"]["status"] == "processing"
    assert [item[1]["id"] for item in gateway.refund_submissions] == [request["id"], request["id"]]
    assert service.wallet(user.id)["creditUnits"] == 0
    assert len(service.list_records("ledger", owner_id=user.id)["items"]) == 2


def test_abnormal_never_restores_credits_closed_restores_once(fixture):
    service, _, admin, user, _, gateway, _ = fixture
    order, request = approve(fixture)
    service.execute_refund(admin.id, request["id"])
    service.accept_refund(gateway.refund_event(order, request, state="abnormal"))
    assert service.wallet(user.id)["creditUnits"] == 0
    closed = gateway.refund_event(order, request, state="closed")
    service.accept_refund(closed)
    service.accept_refund(replace(closed, event_id="different-closed"))
    assert service.wallet(user.id)["creditUnits"] == 100
    service.accept_refund(gateway.refund_event(order, request, event_id="delayed-processing", state="processing"))
    assert service.wallet(user.id)["creditUnits"] == 100
    assert service._refund_result(request["id"])["refund"]["status"] == "closed"
    with pytest.raises(ConflictError, match="终态"):
        service.accept_refund(gateway.refund_event(order, request, state="succeeded"))


@pytest.mark.parametrize("field,value", [("amount_fen", 1199), ("total_fen", 1201), ("transaction_id", "different"),
    ("order_id", "unrelated"), ("refund_request_id", "unrelated"), ("refund_id", "different")])
def test_refund_event_matches_persisted_payment_and_approved_request(fixture, field, value):
    service, _, admin, user, _, gateway, _ = fixture
    order, request = approve(fixture)
    service.execute_refund(admin.id, request["id"])
    event = gateway.refund_event(order, request, state="succeeded")
    with pytest.raises((ConflictError, NotFoundError)):
        service.accept_refund(replace(event, **{field: value}))
    assert service.wallet(user.id)["creditUnits"] == 0
    assert service._refund_result(request["id"])["refund"]["status"] == "processing"


def test_refund_approval_cannot_be_forged_by_callback_or_overwritten_after_submission(fixture):
    service, _, admin, _, other, gateway, _ = fixture
    order, request = approve(fixture)
    with pytest.raises(NotFoundError):
        service.accept_refund(gateway.refund_event(order, request, state="succeeded"))
    with pytest.raises(AuthorizationError):
        service.execute_refund(other.id, request["id"])
    service.execute_refund(admin.id, request["id"])
    with pytest.raises(ConflictError):
        service.review_request(admin.id, request["id"], status="rejected", note="覆盖")
    with sqlite3.connect(service.database) as db:
        for sql in ("UPDATE billing_refunds SET amount_fen=1", "DELETE FROM billing_refunds", "DELETE FROM billing_refund_events"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


def test_api_query_and_refund_routes_guard_identity_fields_and_truthful_status(tmp_path):
    services = build_platform_services(tmp_path / "api.sqlite3", auth_secret="test-refund-api-secret-longer-than32")
    admin = services.auth.create_user("admin@example.test", "password-123", "Admin", roles=["admin"])
    user = services.auth.create_user("user@example.test", "password-123", "User", roles=["designer"])
    other = services.auth.create_user("other@example.test", "password-123", "Other", roles=["designer"])
    gateway = FixtureGateway()
    service = BillingService(services.pdm.database, auth=services.auth, payment_adapter=gateway, enabled=True, charging_configured=True, online_payments_enabled=True, refunds_enabled=True)
    app = FastAPI()
    app.include_router(create_billing_router(services, billing=service), prefix="/api/v1")
    def headers(actor): return {"Authorization": "Bearer " + services.auth.issue_token(actor).token}
    plan = service.save_package(admin.id, {"name": "FIXTURE", "amountFen": 1200, "creditUnits": 100, "active": True})
    order = service.create_order(user.id, package_id=plan["id"], idempotency_key="first")
    try:
        with TestClient(app) as client:
            query_url = "/api/v1/billing/orders/" + order["id"] + "/reconcile"
            assert client.post(query_url, json={}).status_code == 401
            assert client.post(query_url, json={}, headers=headers(other)).status_code == 404
            assert client.post(query_url, json={"amountFen": 1}, headers=headers(user)).status_code == 422
            assert client.post(query_url, json={}, headers=headers(user)).json()["order"]["status"] == "paid"
            limited = client.post(query_url, json={}, headers=headers(user))
            assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
            request = service.request_service(user.id, order["id"], kind="refund", values={"amountFen": 1200, "reason": "退"}, idempotency_key="refund")
            service.review_request(admin.id, request["id"], status="approved", note="审核")
            execute_url = "/api/v1/billing/admin/requests/" + request["id"] + "/refund"
            assert client.post(execute_url, json={}, headers=headers(user)).status_code == 403
            assert client.post(execute_url, json={}, headers=headers(admin)).json()["refund"]["status"] == "processing"
            notification_url = "/api/v1/billing/refund-notifications/" + gateway.name
            assert client.post(notification_url, content=b"unverified").status_code == 400
            assert service._refund_result(request["id"])["order"]["status"] == "paid"
            assert client.post(notification_url, content=b"fixture", headers={"x-fixture-verified": "yes"}).status_code == 204
            assert client.post(notification_url, content=b"fixture", headers={"x-fixture-verified": "yes"}).status_code == 204
            assert service.order(user.id, order["id"])["status"] == "refunded"
    finally:
        service.close()
        services.close()
