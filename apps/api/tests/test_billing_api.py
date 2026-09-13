"""HTTP identity/role gates and verified payment notification boundaries."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import VerifiedPaymentEvent
from app.billing_api import create_billing_router
from app.platform_api import build_platform_services


@pytest.fixture
def api(tmp_path):
    services = build_platform_services(tmp_path / "platform.sqlite3", auth_secret="billing-api-test-secret-with-32-bytes")
    admin = services.auth.create_user("admin@example.test", "test-password", "Admin", roles=["admin"])
    first = services.auth.create_user("first@example.test", "test-password", "First", roles=["designer"])
    other = services.auth.create_user("other@example.test", "test-password", "Other", roles=["designer"])
    router = create_billing_router(services)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    headers = lambda user: {"Authorization": "Bearer " + services.auth.issue_token(user).token}
    with TestClient(app) as client:
        yield services, router.billing_service, client, admin, first, other, headers
    router.billing_service.close()
    services.close()


def test_anonymous_can_only_read_public_catalog_and_status(api):
    _, service, client, _, _, _, _ = api
    status = client.get("/api/v1/billing/status")
    assert status.status_code == 200 and status.json()["purchaseEnabled"] is False
    assert status.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/billing/packages").json() == {"items": []}
    for path in ("wallet", "ledger", "orders", "refund-requests", "invoice-requests", "admin/dashboard", "admin/packages", "admin/orders", "admin/usage", "admin/audit"):
        assert client.get("/api/v1/billing/" + path).status_code == 401
    assert service.database.endswith("platform.sqlite3")


def test_admin_mutations_require_role_and_unknown_fields_rejected(api):
    _, _, client, admin, first, _, headers = api
    data = {"name": "测试套餐", "amountFen": 1000, "creditUnits": 50, "active": True}
    assert client.post("/api/v1/billing/admin/packages", json=data, headers=headers(first)).status_code == 403
    created = client.post("/api/v1/billing/admin/packages", json=data, headers=headers(admin))
    assert created.status_code == 201
    package_id = created.json()["id"]
    update = client.patch("/api/v1/billing/admin/packages/" + package_id, json={"version": 1, "active": False}, headers=headers(admin))
    assert update.status_code == 200 and update.json()["active"] is False
    assert client.get("/api/v1/billing/packages").json()["items"] == []
    assert len(client.get("/api/v1/billing/admin/packages", headers=headers(admin)).json()["items"]) == 1
    assert client.post("/api/v1/billing/orders", json={"packageId": package_id, "idempotencyKey": "x", "ownerId": admin.id}, headers=headers(first)).status_code == 422
    assert client.post("/api/v1/billing/orders", json={"packageId": package_id, "idempotencyKey": "x"}, headers=headers(first)).status_code == 503


def test_adjustment_is_admin_only_scoped_and_idempotent(api):
    _, _, client, admin, first, other, headers = api
    data = {"ownerId": first.id, "creditUnits": 99, "reason": "客服补偿", "idempotencyKey": "same-key"}
    assert client.post("/api/v1/billing/admin/adjustments", json=data, headers=headers(first)).status_code == 403
    assert client.post("/api/v1/billing/admin/adjustments", json=data, headers=headers(admin)).status_code == 201
    assert client.post("/api/v1/billing/admin/adjustments", json=data, headers=headers(admin)).status_code == 201
    assert client.get("/api/v1/billing/wallet", headers=headers(first)).json()["creditUnits"] == 99
    assert client.get("/api/v1/billing/wallet", headers=headers(other)).json()["creditUnits"] == 0
    assert len(client.get("/api/v1/billing/ledger", headers=headers(first)).json()["items"]) == 1
    assert client.get("/api/v1/billing/ledger", headers=headers(other)).json()["items"] == []
    assert client.get("/api/v1/billing/admin/dashboard", headers=headers(first)).status_code == 403
    assert client.get("/api/v1/billing/admin/usage", headers=headers(first)).status_code == 403
    assert client.get("/api/v1/billing/admin/audit", headers=headers(admin)).json()["total"] == 1


def test_missing_payment_credentials_never_accepts_forged_callback(api):
    _, _, client, _, _, _, _ = api
    response = client.post("/api/v1/billing/payment-notifications/wechat_native", json={"status": "SUCCESS", "amount": 1})
    assert response.status_code == 503
    assert "支付渠道尚未配置" in response.text


def test_gateway_failure_hides_secrets_and_keeps_one_order(api):
    _, service, client, admin, first, _, headers = api

    class FailureGateway:
        configured = True
        name = "test-only"
        def create_order(self, order):
            raise RuntimeError("merchant-private-key-never-show")

    service.payment_adapter = FailureGateway()
    service.enabled = service.charging_configured = service.online_payments_enabled = True
    package = service.save_package(admin.id, {"name": "Test", "amountFen": 1, "creditUnits": 1, "active": True})
    payload = {"packageId": package["id"], "idempotencyKey": "same"}
    response = client.post("/api/v1/billing/orders", json=payload, headers=headers(first))
    assert response.status_code == 503 and "merchant-private" not in response.text
    repeated = client.post("/api/v1/billing/orders", json=payload, headers=headers(first))
    assert repeated.status_code == 201 and repeated.json()["status"] == "payment_unavailable"
    assert service.list_records("orders", owner_id=first.id)["total"] == 1
    assert service.wallet(first.id)["creditUnits"] == 0


def test_only_verified_matching_gateway_event_credits_the_order(api):
    _, service, client, admin, first, other, headers = api

    class VerifiedTestGateway:
        configured = True
        name = "test-only"
        event = None
        def create_order(self, order):
            self.event = VerifiedPaymentEvent(self.name, "e1", order["id"], "t1", order["amountFen"])
            return {"codeUrl": "https://payments.example.test/" + order["id"]}
        def verify_event(self, body, request_headers):
            # Test stub stands in for a gateway SDK verifier, never production.
            if request_headers.get("x-test-verified") != "yes":
                raise ValueError("private verifier diagnostic")
            return self.event

    gateway = VerifiedTestGateway()
    service.payment_adapter = gateway
    service.enabled = service.charging_configured = service.online_payments_enabled = True
    package = service.save_package(admin.id, {"name": "Test", "amountFen": 10, "creditUnits": 5, "active": True})
    order = client.post("/api/v1/billing/orders", json={"packageId": package["id"], "idempotencyKey": "create"}, headers=headers(first)).json()
    url = "/api/v1/billing/payment-notifications/test-only"
    assert client.post(url, content=b"unverified").status_code == 400
    assert service.wallet(first.id)["creditUnits"] == 0
    assert client.post(url, content=b"verified", headers={"x-test-verified": "yes"}).status_code == 200
    assert client.post(url, content=b"verified", headers={"x-test-verified": "yes"}).status_code == 200
    assert service.wallet(first.id)["creditUnits"] == 5
    assert client.get("/api/v1/billing/orders/" + order["id"], headers=headers(other)).status_code == 404
    body = {"amountFen": 10, "reason": "退款申请", "idempotencyKey": "refund"}
    assert client.post("/api/v1/billing/orders/" + order["id"] + "/refund-requests", json=body, headers=headers(other)).status_code == 404
    request = client.post("/api/v1/billing/orders/" + order["id"] + "/refund-requests", json=body, headers=headers(first))
    assert request.status_code == 201
    assert client.get("/api/v1/billing/refund-requests", headers=headers(other)).json()["items"] == []
    review_url = "/api/v1/billing/admin/requests/" + request.json()["id"]
    assert client.patch(review_url, json={"status": "approved", "note": "待渠道退款"}, headers=headers(first)).status_code == 403
    assert client.patch(review_url, json={"status": "approved", "note": "待渠道退款"}, headers=headers(admin)).status_code == 200
    assert service.order(first.id, order["id"])["status"] == "paid"
