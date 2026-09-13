"""Money invariants use a clearly test-only gateway; no external payments."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.billing import BillingService, BillingUnavailable, VerifiedPaymentEvent
from app.platform import AuthService, AuthorizationError, ConflictError, NotFoundError, ValidationError


class TestGateway:
    __test__ = False
    name = "test-only-gateway"
    configured = True

    def __init__(self):
        self.calls = []

    def create_order(self, order):
        self.calls.append(order["id"])
        return {"codeUrl": "https://payments.example.test/" + order["id"], "expiresAt": "2030-01-01T00:00:00Z", "secret": "must-not-be-published"}


@pytest.fixture
def account(tmp_path):
    database = tmp_path / "billing.sqlite3"
    auth = AuthService(database, token_secret="test-billing-32-byte-auth-secret-long")
    admin = auth.create_user("admin@example.test", "test-password", "Admin", roles=["admin"])
    first = auth.create_user("first@example.test", "test-password", "First", roles=["designer"])
    second = auth.create_user("second@example.test", "test-password", "Second", roles=["designer"])
    gateway = TestGateway()
    service = BillingService(database, auth=auth, payment_adapter=gateway, enabled=True, charging_configured=True, online_payments_enabled=True)
    yield service, auth, admin, first, second, gateway
    service.close()
    auth.close()


def package(service, admin):
    return service.save_package(admin.id, {"name": "测试套餐（不用于生产）", "amountFen": 1200, "creditUnits": 100, "active": True})


def pay(service, order, *, event_id="event-1", transaction="txn-1"):
    event = VerifiedPaymentEvent(service.payment_adapter.name, event_id, order["id"], transaction, order["amountFen"])
    return service.accept_payment(event), event


def test_unconfigured_never_creates_order_or_charges(account):
    service, auth, admin, user, _, _ = account
    disabled = BillingService(":memory:", auth=auth)
    try:
        plan = package(disabled, admin)
        assert disabled.wallet(user.id)["creditUnits"] == 0
        assert disabled.status()["enabled"] is False
        assert disabled.status()["purchaseEnabled"] is False
        with pytest.raises(BillingUnavailable):
            disabled.create_order(user.id, package_id=plan["id"], idempotency_key="a")
        assert disabled.list_records("orders", owner_id=user.id)["total"] == 0
        assert disabled.settle_usage(owner_id=user.id, job_id="x", credit_units=10, idempotency_key="s", policy_version="undecided", reason="test")["status"] == "disabled"
        assert disabled.list_records("ledger", owner_id=user.id)["total"] == 0
    finally:
        disabled.close()


@pytest.mark.parametrize("field,value", [("amountFen", 1.5), ("amountFen", True), ("creditUnits", 0), ("creditUnits", "10"), ("active", 1)])
def test_package_validates_fixed_point_integers(account, field, value):
    service, _, admin, _, _, _ = account
    payload = {"name": "Test", "amountFen": 1, "creditUnits": 1, "active": False, field: value}
    with pytest.raises(ValidationError):
        service.save_package(admin.id, payload)


def test_snapshot_survives_package_price_change_and_duplicate_create(account):
    service, _, admin, user, _, gateway = account
    plan = package(service, admin)
    order = service.create_order(user.id, package_id=plan["id"], idempotency_key="create-a")
    assert "secret" not in order["checkout"]
    changed = service.save_package(admin.id, {"version": 1, "amountFen": 9000, "creditUnits": 700, "active": False}, package_id=plan["id"])
    assert changed["version"] == 2
    repeated = service.create_order(user.id, package_id=plan["id"], idempotency_key="create-a")
    assert repeated["id"] == order["id"] and repeated["amountFen"] == 1200 and repeated["creditUnits"] == 100
    assert len(gateway.calls) == 1
    with pytest.raises(NotFoundError):
        service.create_order(user.id, package_id=plan["id"], idempotency_key="create-b")
    with pytest.raises(ConflictError):
        service.create_order(user.id, package_id="different", idempotency_key="create-a")
    with pytest.raises(ConflictError):
        service.save_package(admin.id, {"version": True, "active": True}, package_id=plan["id"])


def test_verified_payment_is_atomic_idempotent_and_bound_to_amount(account):
    service, _, admin, user, other, _ = account
    plan = package(service, admin)
    order = service.create_order(user.id, package_id=plan["id"], idempotency_key="a")
    event = VerifiedPaymentEvent(service.payment_adapter.name, "e1", order["id"], "t1", 1199)
    with pytest.raises(ConflictError):
        service.accept_payment(event)
    assert service.wallet(user.id)["creditUnits"] == 0
    accepted = replace(event, amount_fen=1200)
    assert service.accept_payment(accepted)["duplicate"] is False
    assert service.accept_payment(accepted)["duplicate"] is True
    assert service.accept_payment(replace(accepted, event_id="different-event-same-transaction"))["duplicate"] is True
    assert service.wallet(user.id)["creditUnits"] == 100
    assert service.list_records("ledger", owner_id=user.id)["total"] == 1
    with pytest.raises(ConflictError):
        service.accept_payment(replace(accepted, amount_fen=1300))
    other_order = service.create_order(other.id, package_id=plan["id"], idempotency_key="a")
    with pytest.raises(ConflictError):
        service.accept_payment(replace(accepted, event_id="e2", order_id=other_order["id"]))
    assert service.wallet(other.id)["creditUnits"] == 0


def test_orders_and_requests_are_scoped_to_owner(account):
    service, _, admin, user, other, _ = account
    plan = package(service, admin)
    order = service.create_order(user.id, package_id=plan["id"], idempotency_key="a")
    pay(service, order)
    with pytest.raises(NotFoundError):
        service.order(other.id, order["id"])
    with pytest.raises(NotFoundError):
        service.request_service(other.id, order["id"], kind="refund", values={"reason": "x", "amountFen": 100}, idempotency_key="req")
    assert service.list_records("orders", owner_id=other.id)["items"] == []
    with pytest.raises(AuthorizationError):
        service.dashboard(other.id)
    with pytest.raises(AuthorizationError):
        service.adjust(other.id, owner_id=user.id, credit_units=500, reason="x", idempotency_key="admin-spoof")


def test_adjustment_idempotent_audited_not_supplier_cost(account):
    service, _, admin, user, _, _ = account
    args = {"owner_id": user.id, "credit_units": 50, "reason": "客户支持补偿", "idempotency_key": "one"}
    first = service.adjust(admin.id, **args)
    assert service.adjust(admin.id, **args) == first
    assert service.wallet(user.id)["creditUnits"] == 50
    assert service.list_records("audit", actor_id=admin.id)["total"] == 1
    assert service.dashboard(admin.id)["knownCostMicroUsd"] == 0
    with pytest.raises(ConflictError):
        service.adjust(admin.id, **{**args, "credit_units": 60})
    with pytest.raises(ConflictError):
        service.adjust(admin.id, **{**args, "credit_units": -60, "idempotency_key": "two"})
    assert service.wallet(user.id)["creditUnits"] == 50


def test_sql_financial_history_cannot_be_rewritten(account):
    service, _, admin, user, _, _ = account
    plan = package(service, admin)
    order = service.create_order(user.id, package_id=plan["id"], idempotency_key="a")
    pay(service, order)
    with sqlite3.connect(service.database) as db:
        for sql in ("UPDATE billing_ledger SET delta_units=999", "DELETE FROM billing_ledger", "DELETE FROM billing_payment_events", "DELETE FROM billing_audit", "UPDATE billing_orders SET amount_fen=1", "DELETE FROM billing_orders"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


def test_usage_immutable_unknown_is_not_zero_and_not_double_counted(account):
    service, _, admin, user, _, _ = account
    args = dict(owner_id=user.id, job_id="job-1", call_id="call-1", stage="planner", provider="codex", model="test-model",
                input_tokens=1000, output_tokens=200, cached_input_tokens=800, reasoning_output_tokens=100,
                cost_micro_usd=500, cost_source="rate_card_calculated", rate_card_version="test-v1")
    service.record_usage(**args)
    service.record_usage(**args)
    service.record_usage(owner_id=user.id, job_id="job-1", call_id="call-2", stage="reader", provider="codex", model="test-model", outcome="timeout")
    dashboard = service.dashboard(admin.id)
    assert dashboard["usageCalls"] == 2 and dashboard["knownCostMicroUsd"] == 500
    assert dashboard["unknownCostCalls"] == 1 and dashboard["unknownUsageCalls"] == 1
    with pytest.raises(ConflictError):
        service.record_usage(**{**args, "cost_micro_usd": 600})
    with pytest.raises(ValidationError):
        service.record_usage(**{**args, "call_id": "bad", "cached_input_tokens": 1001})
    assert service.wallet(user.id)["creditUnits"] == 0
    with sqlite3.connect(service.database) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE billing_usage SET cost_micro_usd=0")


def test_inflight_usage_remains_recordable_after_account_is_disabled(account):
    service, auth, admin, user, _, _ = account
    auth.set_active(user.id, False, actor_id=admin.id)
    record = service.record_usage(owner_id=user.id, job_id="started-before-disable", call_id="call-final", stage="planner", provider="codex", model="test-model", input_tokens=100, output_tokens=20)
    assert record["usageKnown"] is True
    assert service.dashboard(admin.id)["usageCalls"] == 1
    with pytest.raises(AuthorizationError):
        service.wallet(user.id)


def test_settlement_replay_and_concurrency_cannot_overspend(account):
    service, auth, admin, user, _, _ = account
    service.adjust(admin.id, owner_id=user.id, credit_units=100, reason="test", idempotency_key="seed")
    second = BillingService(service.database, auth=auth, enabled=True, charging_configured=True, online_payments_enabled=True)
    args = dict(owner_id=user.id, job_id="job", credit_units=70, policy_version="test-only", reason="test")
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(engine.settle_usage, **args, idempotency_key=f"different-{i}") for i, engine in enumerate((service, second))]
            results = [future.result() for future in futures]
        assert sorted(result["status"] for result in results) == ["needs_review", "settled"]
        winner = next(i for i, result in enumerate(results) if result["status"] == "settled")
        assert service.settle_usage(**args, idempotency_key=f"different-{winner}")["status"] == "settled"
        assert service.wallet(user.id)["creditUnits"] == 30
        assert service.list_records("ledger", owner_id=user.id)["total"] == 2
    finally:
        second.close()


def test_refund_approval_is_request_only_not_cash_or_credit_return(account):
    service, _, admin, user, _, _ = account
    order = service.create_order(user.id, package_id=package(service, admin)["id"], idempotency_key="a")
    pay(service, order)
    args = dict(kind="refund", values={"reason": "不再使用", "amountFen": 1200}, idempotency_key="r1")
    request = service.request_service(user.id, order["id"], **args)
    assert service.request_service(user.id, order["id"], **args)["id"] == request["id"]
    approved = service.review_request(admin.id, request["id"], status="approved", note="待支付渠道实际退款")
    assert approved["status"] == "approved"
    assert service.order(user.id, order["id"])["status"] == "paid"
    assert service.wallet(user.id)["creditUnits"] == 100
    with pytest.raises(ValidationError):
        service.review_request(admin.id, request["id"], status="refunded", note="fake")


def test_invoice_requires_actual_external_reference_and_survives_reopen(account):
    service, auth, admin, user, _, _ = account
    order = service.create_order(user.id, package_id=package(service, admin)["id"], idempotency_key="a")
    pay(service, order)
    request = service.request_service(user.id, order["id"], kind="invoice", values={"title": "测试公司", "email": "accounts@example.test", "taxId": "TEST123"}, idempotency_key="i1")
    with pytest.raises(ValidationError):
        service.review_request(admin.id, request["id"], status="issued", note="test")
    service.review_request(admin.id, request["id"], status="issued", note="外部开票系统已开具", external_reference="TEST-INVOICE-1")
    reopened = BillingService(service.database, auth=auth)
    try:
        assert reopened.wallet(user.id)["creditUnits"] == 100
        assert reopened.list_records("invoice", owner_id=user.id)["items"][0]["externalReference"] == "TEST-INVOICE-1"
    finally:
        reopened.close()
