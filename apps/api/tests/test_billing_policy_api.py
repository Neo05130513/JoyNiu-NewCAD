"""HTTP role gates and honest disabled policy configuration."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import BillingService
from app.billing_policy_api import create_billing_policy_router
from app.platform_api import build_platform_services
from .test_billing_policy import document


@pytest.fixture
def api(tmp_path):
    services = build_platform_services(tmp_path / "policy.sqlite3", auth_secret="policy-api-secret-with-at-least32bytes")
    admin = services.auth.create_user("admin@example.test", "test-password", "Admin", roles=["admin"])
    user = services.auth.create_user("user@example.test", "test-password", "User", roles=["designer"])
    billing = BillingService(services.pdm.database, auth=services.auth)
    router = create_billing_policy_router(services, billing=billing)
    billing.charging_status_provider = router.policy_service.status
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    headers = lambda who: {"Authorization": "Bearer " + services.auth.issue_token(who).token}
    with TestClient(app) as client:
        yield client, billing, router.policy_service, admin, user, headers
    billing.close()
    services.close()


def test_policy_configuration_requires_active_administrator(api):
    client, _, _, admin, user, headers = api
    path = "/api/v1/billing/admin/policy"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers(user)).status_code == 403
    response = client.get(path, headers=headers(admin))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["enabled"] is False and response.json()["versions"] == []
    assert client.post(path + "/versions", json=document(), headers=headers(user)).status_code == 403
    assert client.get("/api/v1/billing/admin/settlements", headers=headers(user)).status_code == 403
    assert client.get("/api/v1/billing/settlements").status_code == 401
    assert client.get("/api/v1/billing/settlements", headers=headers(user)).json()["items"] == []
    pricing = client.get("/api/v1/billing/pricing").json()
    assert pricing["enabled"] is False and pricing["rates"] == []
    assert "versions" not in pricing and "createdBy" not in pricing


def test_missing_policy_or_unimplemented_balance_rule_cannot_enable(api):
    client, billing, _, admin, _, headers = api
    path, auth = "/api/v1/billing/admin/policy", headers(admin)
    created = client.post(path + "/versions", json=document(insufficientBalancePolicy=None), headers=auth)
    assert created.status_code == 201
    version = created.json()
    enable = {"revision": 0, "versionId": version["id"], "enabled": True, "reason": "test enable"}
    assert client.patch(path, json=enable, headers=auth).status_code == 422
    assert billing.status()["enabled"] is False
    disable = {**enable, "enabled": False}
    response = client.patch(path, json=disable, headers=auth)
    assert response.status_code == 200 and response.json()["revision"] == 1
    assert client.patch(path, json=disable, headers=auth).status_code == 409
    assert client.patch(path, json={**disable, "creditUnits": 100}, headers=auth).status_code == 422
    assert client.post("/api/v1/billing/settlements", json={"chargedUnits": 0}, headers=auth).status_code == 405


def test_no_policy_configuration_can_accept_customer_usage_or_fake_paid_fields(api):
    client, _, _, admin, _, headers = api
    path = "/api/v1/billing/admin/policy/versions"
    for field in ("inputTokens", "balance", "maxCost", "freezeUnits", "paid"):
        response = client.post(path, json={**document(), field: 10}, headers=headers(admin))
        assert response.status_code == 422
