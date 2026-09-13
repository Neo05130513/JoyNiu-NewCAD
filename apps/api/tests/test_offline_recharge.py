"""Offline receipts are local test records; no gateway or supplier is called."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.billing import BillingService, BillingUnavailable
from app.platform import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .test_billing import account, package
from .test_billing_api import api
from .test_billing_terms_hooks import TermsFixture


def recharge(billing, actor, customer, **overrides):
    values = dict(owner_id=customer.id, amount_fen=1000, external_reference="BANK-RECEIPT-001",
                  reason="已核对客户线下到账", receipt_confirmed=True, idempotency_key="offline-first")
    return billing.offline_recharge(actor.id, **{**values, **overrides})


def refund(billing, actor, order, **overrides):
    return billing.offline_refund(actor.id, order["id"], **{
        "external_reference": "BANK-REFUND-001", "reason": "已核实线下整单退款完成",
        "refund_confirmed": True, "idempotency_key": "offline-refund-first", **overrides})


def test_fixed_rate_atomic_order_ledger_receipt_and_real_revenue(account):
    billing, _, admin, user, other, gateway = account
    billing.online_payments_enabled = False
    order = recharge(billing, admin, user, amount_fen=1234)
    assert order["amountFen"] == order["creditUnits"] == 1234
    assert order["provider"] == "offline" and order["status"] == "paid" and order["checkout"] == {}
    assert order["offlineReceipt"]["pointsPerCny"] == 100
    assert order["offlineReceipt"]["confirmedBy"] == admin.id
    ledger = billing.list_records("ledger", owner_id=user.id)["items"]
    assert len(ledger) == 1 and ledger[0]["kind"] == "offline_recharge"
    assert ledger[0]["referenceId"] == order["id"] and ledger[0]["deltaUnits"] == 1234
    assert billing.wallet(user.id)["creditUnits"] == 1234
    assert billing.list_records("orders", owner_id=other.id)["items"] == []
    with pytest.raises(NotFoundError):
        billing.order(other.id, order["id"])
    for category in ("gift", "compensation", "adjustment"):
        billing.adjust(admin.id, owner_id=user.id, credit_units=3, reason=category, idempotency_key=category, category=category)
    report = billing.dashboard(admin.id)
    assert report["grossRevenueFen"] == report["offlineGrossRevenueFen"] == 1234
    assert report["nonRevenueCreditUnits"] == {"gift": 3, "compensation": 3, "adjustment": 3}
    assert not gateway.calls
    with billing._lock:
        row = billing._db.execute("SELECT * FROM billing_offline_records").fetchone()
        assert row["ledger_id"] == ledger[0]["id"] and row["order_id"] == order["id"]
    for sql in ("UPDATE billing_offline_records SET amount_fen=1", "DELETE FROM billing_offline_records",
                "UPDATE billing_orders SET offline_json='{}'", "UPDATE billing_orders SET amount_fen=1",
                "UPDATE billing_orders SET transaction_id='changed'"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with billing._transaction() as db:
                db.execute(sql)


def test_independent_online_gate_stays_closed_even_with_configured_charging_and_gateway(account):
    billing, auth, admin, user, _, gateway = account
    closed = BillingService(billing.database, auth=auth, payment_adapter=gateway, enabled=True, charging_configured=True)
    try:
        plan = package(closed, admin)
        assert closed.status()["enabled"] is True and closed.status()["paymentConfigured"] is True
        assert closed.status()["onlinePaymentEnabled"] is False and closed.status()["purchaseEnabled"] is False
        with pytest.raises(BillingUnavailable):
            closed.create_order(user.id, package_id=plan["id"], idempotency_key="blocked")
        order = recharge(closed, admin, user)
        assert order["creditUnits"] == 1000 and not gateway.calls
    finally:
        closed.close()


@pytest.mark.parametrize("overrides", [{"amount_fen": True}, {"amount_fen": 1.5}, {"amount_fen": "1000"},
    {"amount_fen": 0}, {"amount_fen": -1}, {"amount_fen": 100_000_001}, {"external_reference": " "},
    {"reason": ""}, {"receipt_confirmed": False}, {"receipt_confirmed": 1}])
def test_invalid_receipt_has_no_partial_financial_writes(account, overrides):
    billing, _, admin, user, *_ = account
    with pytest.raises(ValidationError):
        recharge(billing, admin, user, **overrides)
    assert billing.wallet(user.id)["creditUnits"] == 0
    assert billing.list_records("orders", owner_id=user.id)["total"] == 0
    assert billing.list_records("ledger", owner_id=user.id)["total"] == 0


def test_replays_global_keys_and_receipt_uniqueness_across_operators_and_connections(account):
    billing, auth, admin, user, *_ = account
    finance = auth.create_user("finance-offline@example.test", "test-password", "Finance", roles=["finance"])
    second = BillingService(billing.database, auth=auth)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda actor: recharge(billing if actor == admin else second, actor, user), [admin, finance]))
        assert results[0]["id"] == results[1]["id"]
        assert billing.wallet(user.id)["creditUnits"] == 1000
        with pytest.raises(ConflictError, match="幂等"):
            recharge(second, finance, user, amount_fen=1001)
        for reference in ("BANK-RECEIPT-001", "bank-receipt-001", "ＢＡＮＫ-ＲＥＣＥＩＰＴ-００１"):
            with pytest.raises(ConflictError, match="凭证"):
                recharge(second, finance, user, idempotency_key="another-operator", external_reference=reference)
        assert billing.list_records("orders", actor_id=admin.id, q="BANK-RECEIPT-001")["total"] == 1
        assert billing.list_records("orders", actor_id=admin.id, filter_owner_id=user.id, status="paid")["total"] == 1
    finally:
        second.close()


def test_new_customer_can_receive_offline_funds_without_operator_accepting_terms(account):
    from app.billing_policy import BillingPolicyService
    from .test_billing_policy import document
    billing, _, admin, user, *_ = account
    terms = billing.terms_provider = TermsFixture()
    order = recharge(billing, admin, user)
    assert order["termsAcceptance"] == {} and terms.checked == []
    assert billing.status()["offlineRechargeEnabled"] is True
    assert recharge(billing, admin, user)["id"] == order["id"]
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    version = policy.save_version(admin.id, document())
    policy.set_enabled(admin.id, revision=0, enabled=True, version_id=version["id"], reason="Test paid flow")
    with pytest.raises(BillingUnavailable):
        policy.check_start(user.id)
    terms.published = True
    with pytest.raises(ConflictError, match="接受"):
        policy.check_start(user.id)
    terms.accepted = True
    assert policy.check_start(user.id)["allowed"] is True
    assert billing.order(user.id, order["id"])["termsAcceptance"] == {}


def test_offline_full_refund_reverses_once_and_preserves_original_linkage(account):
    billing, _, admin, user, *_ = account
    order = recharge(billing, admin, user)
    result = refund(billing, admin, order)
    assert result["status"] == "refunded" and billing.wallet(user.id)["creditUnits"] == 0
    assert refund(billing, admin, order)["id"] == result["id"]
    with pytest.raises(ConflictError):
        refund(billing, admin, order, idempotency_key="second-refund", external_reference="new-refund-ref")
    report = billing.dashboard(admin.id)
    assert report["revenueFen"] == 0 and report["refundedFen"] == 1000
    records = billing._db.execute("SELECT * FROM billing_offline_records ORDER BY rowid").fetchall()
    assert records[1]["original_record_id"] == records[0]["id"]
    assert billing.list_records("refund", owner_id=user.id)["items"][0]["status"] == "refunded"
    with pytest.raises(ConflictError):
        recharge(billing, admin, user, idempotency_key="refund-ref-reuse", external_reference="BANK-REFUND-001")


def test_insufficient_balance_refund_rolls_back_and_recharge_pays_due_first(account):
    billing, _, admin, user, *_ = account
    billing.adjust(admin.id, owner_id=user.id, credit_units=1, reason="fixture", idempotency_key="fixture")
    billing.settle_usage(owner_id=user.id, job_id="test-job", credit_units=51, idempotency_key="test-settle", policy_version="test", reason="test due", insufficient_balance_policy="deferred_due")
    order = recharge(billing, admin, user)
    assert billing.wallet(user.id)["creditUnits"] == 950 and billing.wallet(user.id)["dueUnits"] == 0
    with pytest.raises(ConflictError, match="余额不足"):
        refund(billing, admin, order)
    assert billing.order(user.id, order["id"])["status"] == "paid"
    assert billing._db.execute("SELECT count(*) FROM billing_offline_records").fetchone()[0] == 1
    assert billing.list_records("refund", owner_id=user.id)["total"] == 0
    assert billing.dashboard(admin.id)["refundedFen"] == 0


def test_offline_customer_refund_and_invoice_use_original_order(account):
    billing, _, admin, user, *_ = account
    order = recharge(billing, admin, user)
    invoice = billing.request_service(user.id, order["id"], kind="invoice", values={"title": "Test Customer", "email": "invoice@example.test"}, idempotency_key="invoice")
    assert invoice["data"]["provider"] == "offline"
    with pytest.raises(ValidationError, match="整单"):
        billing.request_service(user.id, order["id"], kind="refund", values={"reason": "partial", "amountFen": 50}, idempotency_key="partial")
    request = billing.request_service(user.id, order["id"], kind="refund", values={"reason": "whole", "amountFen": 1000}, idempotency_key="whole")
    with pytest.raises(ConflictError):
        refund(billing, admin, order)
    with pytest.raises(ConflictError):
        refund(billing, admin, order, refund_request_id=request["id"])
    billing.review_request(admin.id, request["id"], status="approved", note="已审核")
    assert refund(billing, admin, order, refund_request_id=request["id"])["status"] == "refunded"
    assert billing.list_records("refund", owner_id=user.id)["total"] == 1


def test_offline_http_permission_identity_amount_and_unknown_fields(api):
    services, billing, client, admin, customer, other, headers = api
    body = {"ownerId": customer.id, "amountFen": 1000, "externalReference": "HTTP-RECEIPT", "reason": "Test only",
            "receiptConfirmed": True, "idempotencyKey": "http-once"}
    url = "/api/v1/billing/admin/offline-recharges"
    assert client.post(url, json=body).status_code == 401
    for role in ("auditor", "support", "ops", "designer"):
        user = services.auth.create_user(f"offline-{role}@example.test", "test-password", role, roles=[role])
        assert client.post(url, json=body, headers=headers(user)).status_code == 403
        assert client.post(url + "/missing/refund", json={}, headers=headers(user)).status_code == 403
    assert client.post(url, json={**body, "creditUnits": 999999}, headers=headers(admin)).status_code == 422
    response = client.post(url, json=body, headers=headers(admin))
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    order = response.json()
    assert client.post(url, json=body, headers=headers(admin)).json()["id"] == order["id"]
    assert client.get("/api/v1/billing/orders", headers=headers(customer)).json()["total"] == 1
    assert client.get("/api/v1/billing/orders", headers=headers(other)).json()["total"] == 0
    assert client.get("/api/v1/billing/orders/" + order["id"], headers=headers(other)).status_code == 404
    assert client.get("/api/v1/billing/admin/orders?q=HTTP-RECEIPT", headers=headers(admin)).json()["total"] == 1
    refund_body = {"externalReference": "HTTP-REFUND", "reason": "Refund completed test", "refundConfirmed": True, "idempotencyKey": "http-refund"}
    assert client.post(url + f"/{order['id']}/refund", json=refund_body, headers=headers(customer)).status_code == 403
    assert client.post(url + f"/{order['id']}/refund", json=refund_body, headers=headers(admin)).json()["status"] == "refunded"
    assert billing.wallet(customer.id)["creditUnits"] == 0


def test_http_first_login_receipt_then_personal_acceptance_before_paid_work(api):
    from app.billing_policy_api import create_billing_policy_router
    from app.commercial_terms_api import create_commercial_terms_router
    from app.official_api_pricing import CATALOG_VERSION
    services, billing, client, admin, customer, other, headers = api
    policy_router = create_billing_policy_router(services, billing=billing)
    terms_router = create_commercial_terms_router(services)
    client.app.include_router(policy_router, prefix="/api/v1")
    client.app.include_router(terms_router, prefix="/api/v1")
    billing.charging_status_provider = policy_router.policy_service.status
    billing.terms_provider = terms_router.terms_service
    # No documents or customer acceptance exists at receipt entry time.
    created = client.post("/api/v1/billing/admin/offline-recharges", headers=headers(admin), json={
        "ownerId": customer.id, "amountFen": 1000, "externalReference": "FIRST-LOGIN-RECEIPT",
        "reason": "TEST ONLY: received before first login", "receiptConfirmed": True, "idempotencyKey": "first-login"})
    assert created.status_code == 201 and created.json()["termsAcceptance"] == {}
    assert client.get("/api/v1/commercial/acceptance", headers=headers(customer)).json()["accepted"] is False
    policy_doc = {"rates": [{"provider": "test-provider", "model": "gpt-6-astra"}],
        "apiPricing": {"catalogVersion": CATALOG_VERSION, "usdCnyRate": "7", "multiplier": "1.5", "pointsPerCny": 100},
        "insufficientBalancePolicy": "deferred_due", "billableStatuses": ["ready", "review_required"], "reason": "TEST ONLY pricing"}
    version = client.post("/api/v1/billing/admin/policy/versions", headers=headers(admin), json=policy_doc)
    assert version.status_code == 201, version.text
    enabled = client.patch("/api/v1/billing/admin/policy", headers=headers(admin), json={
        "revision": 0, "versionId": version.json()["id"], "enabled": True, "reason": "TEST ONLY enable"})
    assert enabled.status_code == 200
    pricing = client.get("/api/v1/billing/pricing").json()
    assert pricing["rates"][0]["inputUnitsPerMillion"] == "10500"
    assert pricing["apiPricing"]["multiplier"] == "1.5"
    assert client.get("/api/v1/billing/status").json()["purchaseEnabled"] is False
    for index, kind in enumerate(("service", "privacy", "credits")):
        published = client.post("/api/v1/commercial/admin/terms", headers=headers(admin), json={
            "kind": kind, "title": f"Test {kind}", "body": "Synthetic fixture only. Not a real legal agreement.",
            "version": "test-first-login-v1", "expectedRevision": index})
        assert published.status_code == 201, published.text
    with pytest.raises(ConflictError):
        policy_router.policy_service.check_start(customer.id)
    ids = [item["id"] for item in published.json()["documents"]]
    # The operator cannot submit ownerId to impersonate customer acceptance.
    assert client.post("/api/v1/commercial/acceptance", headers=headers(admin), json={
        "ownerId": customer.id, "documentIds": ids, "idempotencyKey": "operator-proxy"}).status_code == 422
    accepted = client.post("/api/v1/commercial/acceptance", headers=headers(customer), json={
        "documentIds": ids, "idempotencyKey": "customer-personal-acceptance"})
    assert accepted.status_code == 200 and accepted.json()["accepted"] is True
    assert policy_router.policy_service.check_start(customer.id)["allowed"] is True
    assert client.get("/api/v1/commercial/acceptance", headers=headers(other)).json()["accepted"] is False
    assert billing.order(customer.id, created.json()["id"])["termsAcceptance"] == {}
