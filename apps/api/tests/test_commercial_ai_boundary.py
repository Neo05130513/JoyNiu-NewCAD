"""Commercial mode cannot access the legacy, non-metered AI routes."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai_proxy import AIConversationResult
from app.billing import BillingService, BillingUnavailable
from app.commercial_ai_boundary import legacy_ai_available
from app.platform_api import build_platform_services, create_platform_router


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("JOYNIU_BILLING_ENABLED", raising=False)
    services = build_platform_services(tmp_path / "accounts.sqlite3", auth_secret="legacy-commercial-boundary-32-bytes")
    user = services.auth.create_user("designer@example.test", "test-password", "Designer", roles=["designer"])
    admin = services.auth.create_user("admin@example.test", "test-password", "Admin", roles=["admin"])
    class LegacyAI:
        allow_anonymous = False
        calls = 0
        def status(self): return {"configured": True, "mode": "fixture"}
        def converse(self, *args, **kwargs):
            self.calls += 1
            return AIConversationResult("fixture-response", "旧版本地兼容对话", {}, False, ())
    services.ai = LegacyAI()
    billing = BillingService(services.pdm.database, auth=services.auth)
    app = FastAPI()
    # Both aliases in main must be guarded, with late resolution so main can
    # create billing after platform without capturing a permanent None.
    app.state.billing = billing
    router = create_platform_router(services, billing_provider=lambda: app.state.billing)
    app.include_router(router, prefix="/api/v1")
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + services.auth.issue_token(user).token}
        yield services, billing, client, headers, admin
    billing.close()
    services.close()


@pytest.mark.parametrize("prefix", ["/api/v1", "/api"])
@pytest.mark.parametrize("path", ["/ai/conversation", "/ai/conversation/stream", "/ai/chat"])
def test_commercial_gate_rejects_before_preprocessing_and_provider(api, prefix, path):
    services, billing, client, headers, _ = api
    billing.enabled = True
    # Invalid DWG would otherwise reach preprocessing; commercial rejection
    # takes precedence and never starts a supplier request or stream worker.
    response = client.post(prefix + path, data={"message": "test", "modelState": "invalid JSON"},
                           files={"file": ("drawing.dwg", b"fixture invalid DWG", "application/octet-stream")}, headers=headers)
    assert response.status_code == 409 and "任务中心" in response.text
    assert "text/event-stream" not in response.headers["content-type"]
    assert services.ai.calls == 0
    assert billing.list_records("usage", actor_id=api[4].id)["items"] == []


def test_capabilities_follow_runtime_policy_and_fail_closed(api):
    _, billing, client, _, _ = api
    assert client.get("/api/v1/ai/status").json()["legacyConversationAvailable"] is True
    live = {"enabled": False, "configured": False}
    billing.charging_status_provider = lambda: live.copy()
    live.update(enabled=True, configured=True)
    assert billing.status()["enabled"] is True
    assert client.get("/api/v1/ai/status").json()["legacyConversationAvailable"] is False
    live.update(enabled=False, configured=True)
    assert client.get("/api/v1/ai/status").json()["legacyConversationAvailable"] is True
    def unavailable(): raise RuntimeError("sensitive config path")
    billing.charging_status_provider = unavailable
    state = client.get("/api/v1/ai/status")
    assert state.json()["legacyConversationAvailable"] is False and "sensitive" not in state.text
    assert billing.status()["enabled"] is False and billing.status()["policyAvailable"] is False


@pytest.mark.parametrize("path", ["/ai/conversation", "/ai/conversation/stream", "/ai/chat"])
def test_noncommercial_legacy_behavior_is_preserved(api, path):
    services, _, client, headers, _ = api
    response = client.post("/api/v1" + path, data={"message": "hello"}, headers=headers)
    assert response.status_code == 200
    if path == "/ai/conversation/stream":
        assert "event: turn.result" in response.text and "event: turn.done" in response.text
    else:
        assert response.json()["message"] == "旧版本地兼容对话"
    assert services.ai.calls == 1


def test_authentication_still_required_and_local_anonymous_cannot_bypass_commercial_mode(api, monkeypatch):
    services, billing, client, _, _ = api
    billing.enabled = True
    assert client.post("/api/v1/ai/chat", data={"message": "hello"}).status_code == 401
    services.ai.allow_anonymous = True
    monkeypatch.setattr("app.platform_api._anonymous_ai_request_allowed", lambda _: True)
    response = client.post("/api/v1/ai/chat", data={"message": "hello"})
    assert response.status_code == 409 and services.ai.calls == 0


def test_env_closes_legacy_even_if_composition_omits_billing(monkeypatch):
    monkeypatch.setenv("JOYNIU_BILLING_ENABLED", "true")
    assert legacy_ai_available() is False
    assert legacy_ai_available(lambda: None) is False
    assert legacy_ai_available(lambda: SimpleNamespace(enabled=False, status=lambda: {"enabled": False})) is False


def test_persisted_policy_status_disables_old_worker_purchase_and_settlement(api):
    services, billing, _, _, admin = api
    user = services.auth.create_user("purchaser@example.test", "test-password", "Purchaser", roles=["designer"])
    class Gateway:
        configured = True
        name = "fixture"
        def create_order(self, order): return {"codeUrl": "https://payments.example.test/" + order["id"]}
    billing.payment_adapter = Gateway()
    billing.online_payments_enabled = True  # Fixture explicitly enables its test gateway.
    policy = {"enabled": True, "configured": True}
    billing.charging_status_provider = lambda: policy.copy()
    plan = billing.save_package(admin.id, {"name": "FIXTURE", "amountFen": 10, "creditUnits": 5, "active": True})
    assert billing.create_order(user.id, package_id=plan["id"], idempotency_key="one")["status"] == "pending_payment"
    policy.update(enabled=False)
    with pytest.raises(BillingUnavailable):
        billing.create_order(user.id, package_id=plan["id"], idempotency_key="two")
    assert billing.settle_usage(owner_id=user.id, job_id="x", credit_units=1, idempotency_key="s", policy_version="v1", reason="fixture")["status"] == "disabled"
