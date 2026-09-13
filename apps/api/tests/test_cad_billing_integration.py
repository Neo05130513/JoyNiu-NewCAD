"""Real API/SQLite billing hooks with deterministic local provider and fake CAD."""
import json
import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import BillingService
from app.billing_policy import BillingPolicyService
from app.cad_agent_api import create_cad_agent_router
from app.cad_agent_store import CadRunStore
from app.platform_api import build_platform_services
from .test_billing_policy import document
from .test_cad_agent_api import FakeAgent, fake_execute, run_result


class LocalProvider:
    provider_info = {"mode": "test-provider", "model": "test-model"}

    def __init__(self):
        self.calls = []

    def __call__(self, body, timeout):
        from .test_cad_source_locations import output, located
        self.calls.append(body)
        return {**output(located()), "usage": {"input_tokens": 100000, "cached_input_tokens": 0,
                                               "output_tokens": 300000, "reasoning_output_tokens": 100000}}


class LocalAgent(FakeAgent):
    def __init__(self, provider):
        super().__init__()
        self.provider_call = provider

    def run(self, **kwargs):
        self.provider_call({"model": "test-model"}, 1)
        result = super().run(**kwargs)
        if kwargs.get("files"):
            from .test_cad_source_locations import annotation
            digest = hashlib.sha256(kwargs["files"][0].data).hexdigest()
            result["sourceTranscription"] = {"annotations": [annotation()], "sourceFiles": [{"fileIndex": 0, "sha256": digest}],
                                               "preparedFiles": [{"fileIndex": 0, "sha256": digest}]}
        return result


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "0")
    services = build_platform_services(tmp_path / "platform.sqlite3", auth_secret="cad-billing-integration-test-secret32")
    admin = services.auth.create_user("admin@example.test", "test-password", "Admin", roles=["admin"])
    user = services.auth.create_user("user@example.test", "test-password", "Model owner", roles=["designer"])
    other = services.auth.create_user("other@example.test", "test-password", "Other", roles=["designer"])
    billing = BillingService(services.pdm.database, auth=services.auth)
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    provider = LocalProvider()
    agent = LocalAgent(provider)
    store = CadRunStore(tmp_path / "cad")
    router = create_cad_agent_router(services, store, agent=agent, executor=fake_execute, billing=billing, billing_policy=policy)
    app = FastAPI()
    app.include_router(router)
    headers = lambda actor: {"Authorization": "Bearer " + services.auth.issue_token(actor).token}
    with TestClient(app) as client:
        yield client, billing, policy, admin, user, other, provider, store, agent, services, headers
    billing.close()
    services.close()


def activate(billing, policy, admin, user, balance=10):
    if balance:
        billing.adjust(admin.id, owner_id=user.id, credit_units=balance, reason="TEST funding", idempotency_key="fund")
    version = policy.save_version(admin.id, document(insufficientBalancePolicy="deferred_due", billableStatuses=["review_required", "ready", "failed", "needs_input", "cancelled", "interrupted"]))
    policy.set_enabled(admin.id, revision=0, version_id=version["id"], enabled=True, reason="TEST enable")


def test_configured_real_run_charges_actual_attempt_only_and_due_stops_new_work(api):
    client, billing, policy, admin, user, other, provider, _, _, _, headers = api
    activate(billing, policy, admin, user)
    first = run_result(client.post("/api/v1/cad-agent/run", data={"message": "Build plate", "requestId": "one"}, headers=headers(user)))
    assert billing.wallet(user.id)["creditUnits"] == 3
    duplicate = client.post("/api/v1/cad-agent/run", data={"message": "Build plate", "requestId": "one"}, headers=headers(user))
    assert duplicate.json()["runId"] == first["runId"] and len(provider.calls) == 1
    resumed = run_result(client.post("/api/v1/cad-agent/run", data={"message": "continue", "requestId": "two",
                         "modelState": json.dumps({"agentRun": {"runId": first["runId"], "revision": 1}})}, headers=headers(user)))
    assert resumed["jobId"] == first["jobId"] and len(provider.calls) == 2
    assert billing.wallet(user.id)["creditUnits"] == 0 and billing.wallet(user.id)["dueUnits"] == 4
    denied = client.post("/api/v1/cad-agent/run", data={"message": "another plate", "requestId": "three"}, headers=headers(user))
    assert denied.status_code == 402 and len(provider.calls) == 2
    assert policy.settlements(owner_id=user.id)["total"] == 2
    assert policy.settlements(owner_id=other.id)["total"] == 0


def test_unpriced_configured_model_is_rejected_before_any_supplier_call(api):
    client, billing, policy, admin, user, _, provider, _, _, _, headers = api
    activate(billing, policy, admin, user)
    provider.provider_info = {"mode": "test-provider", "model": "unpriced-model"}
    response = client.post("/api/v1/cad-agent/run", data={"message": "Build plate", "requestId": "unpriced"}, headers=headers(user))
    assert response.status_code == 409 and "费率尚未配置" in response.text
    assert provider.calls == [] and billing.wallet(user.id)["creditUnits"] == 10


def test_disabled_default_preserves_free_tasks_while_recording_real_usage_and_confirmation_actor(api):
    client, billing, policy, _, user, _, provider, store, _, _, headers = api
    result = run_result(client.post("/api/v1/cad-agent/run", data={"message": "Build plate", "requestId": "free"}, headers=headers(user)))
    assert len(provider.calls) == 1 and billing.wallet(user.id)["creditUnits"] == 0
    assert policy.settlements(owner_id=user.id)["items"][0]["reason"] == "disabled_at_start"
    assert result["confirmedBy"] is None
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": result["runId"], "revision": 1,
                            "confirmedBy": {"id": "forged", "displayName": "forged"}}, headers=headers(user))
    assert confirmed.status_code == 200
    expected = {"id": user.id, "displayName": "Model owner"}
    assert confirmed.json()["confirmedBy"] == expected
    assert store.load(result["runId"])["confirmedBy"] == expected


def test_source_localization_and_forced_retry_keep_usage_but_never_charge(api):
    from .test_cad_source_locations import picture
    client, billing, policy, admin, user, _, provider, store, agent, services, headers = api
    activate(billing, policy, admin, user, 100)
    result = run_result(client.post("/api/v1/cad-agent/run", data={"message": "build from drawing"},
                                   files={"files": ("drawing.png", picture(), "image/png")}, headers=headers(user)))
    assert billing.wallet(user.id)["creditUnits"] == 93
    path = f"/api/v1/cad-agent/runs/{result['runId']}/source-locations"
    response = client.post(path, json={}, headers=headers(user))
    assert response.json()["status"] == "succeeded"
    for _ in range(100):
        if policy.settlements(owner_id=user.id)["total"] == 2:
            break
        time.sleep(.01)
    assert billing.wallet(user.id)["creditUnits"] == 93 and len(provider.calls) == 2
    assert client.post(path, json={}, headers=headers(user)).json()["status"] == "succeeded"
    assert billing.wallet(user.id)["creditUnits"] == 93 and len(provider.calls) == 2
    rows = policy.settlements(owner_id=user.id)["items"]
    assert len(rows) == 2 and len({row["attemptId"] for row in rows}) == 2
    assert {row["jobId"] for row in rows} == {result["jobId"]}
    location = next(row for row in rows if row['attemptId'].startswith('location_'))
    assert location['status'] == 'not_charged' and location['expectedCreditUnits'] == 0
    assert client.post(path, json={'force': True}, headers=headers(user)).json()['status'] == 'succeeded'
    for _ in range(100):
        if policy.settlements(owner_id=user.id)['total'] == 3:
            break
        time.sleep(.01)
    assert len(provider.calls) == 3 and billing.wallet(user.id)['creditUnits'] == 93
    assert len(billing.list_records('usage', actor_id=admin.id)['items']) == 3
    assert all(row['chargedUnits'] == 0 for row in policy.settlements(owner_id=user.id)['items'] if row['attemptId'].startswith('location_'))


def test_queued_task_rechecks_after_prior_attempt_settles_before_first_supplier_call(api, monkeypatch):
    client, billing, policy, admin, user, _, provider, _, _, _, headers = api
    activate(billing, policy, admin, user, 7)
    entered, release = threading.Event(), threading.Event()
    original = LocalProvider.__call__

    def blocking(instance, body, timeout):
        entered.set()
        assert release.wait(5)
        return original(instance, body, timeout)

    monkeypatch.setattr(LocalProvider, "__call__", blocking)
    def submit(request_id):
        return run_result(client.post("/api/v1/cad-agent/run", data={"message": "Build plate", "requestId": request_id}, headers=headers(user)))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(submit, "first")
        assert entered.wait(5)
        second = pool.submit(submit, "queued")
        try:
            for _ in range(200):
                queued = client.get("/api/v1/cad-agent/jobs/by-request/queued", headers=headers(user))
                if queued.status_code == 200 and queued.json().get("status") == "queued":
                    break
                time.sleep(.01)
            else:
                pytest.fail("second attempt never entered queue")
        finally:
            release.set()
        assert first.result(timeout=10)["status"] == "review_required"
        denied = second.result(timeout=10)
    assert denied["status"] == "failed" and "尚未调用 AI" in denied["message"]
    assert "积分" in denied["message"] and len(provider.calls) == 1
    assert billing.wallet(user.id)["creditUnits"] == 0 and billing.wallet(user.id)["dueUnits"] == 0


def test_source_preparation_does_not_block_viewing_after_balance_becomes_zero(api, monkeypatch):
    from app import cad_source_locations
    from .test_cad_source_locations import picture
    client, billing, policy, admin, user, _, provider, _, _, _, headers = api
    activate(billing, policy, admin, user, 14)
    result = run_result(client.post("/api/v1/cad-agent/run", data={"message": "build drawing"},
                        files={"files": ("drawing.png", picture(), "image/png")}, headers=headers(user)))
    entered, release = threading.Event(), threading.Event()
    original = cad_source_locations._prepared_before_deadline
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(cad_source_locations, "_prepared_before_deadline", blocked)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.post, f"/api/v1/cad-agent/runs/{result['runId']}/source-locations", json={}, headers=headers(user))
        try:
            assert entered.wait(5)
            billing.adjust(admin.id, owner_id=user.id, credit_units=-7, reason="TEST balance changed during preparation", idempotency_key="consume")
        finally:
            release.set()
        response = future.result(timeout=10)
    assert response.status_code == 200 and response.json()['status'] == 'succeeded'
    assert len(provider.calls) == 2 and billing.wallet(user.id)["creditUnits"] == 0
    assert billing.wallet(user.id)['dueUnits'] == 0


def test_source_viewing_needs_ownership_but_not_paid_terms_or_balance(api, monkeypatch):
    from .test_cad_source_locations import picture
    from app.platform import ValidationError
    client, billing, policy, admin, user, other, provider, _, _, services, headers = api
    activate(billing, policy, admin, user, 7)
    result = run_result(client.post('/api/v1/cad-agent/run', data={'message': 'build drawing', 'chargeable': 'false'},
                        files={'files': ('drawing.png', picture(), 'image/png')}, headers=headers(user)))
    # A client field cannot opt an actual model attempt out of charging.
    assert billing.wallet(user.id)['creditUnits'] == 0 and len(provider.calls) == 1
    def unpaid_terms(_owner):
        raise ValidationError('Paid terms not accepted')
    monkeypatch.setattr(billing, 'require_terms', unpaid_terms)
    path = f"/api/v1/cad-agent/runs/{result['runId']}/source-locations"
    assert client.post(path, json={}, headers=headers(other)).status_code in {403, 404}
    assert client.post(path, json={}, headers=headers(user)).json()['status'] == 'succeeded'
    assert len(provider.calls) == 2
    assert billing.wallet(user.id)['creditUnits'] == 0 and billing.wallet(user.id)['dueUnits'] == 0
    user_headers = headers(user)
    services.auth.set_active(user.id, False, actor_id=admin.id)
    assert client.post(path, json={'force': True}, headers=user_headers).status_code in {401, 403}
    assert len(provider.calls) == 2
