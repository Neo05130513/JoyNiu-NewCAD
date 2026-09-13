"""Local policy arithmetic and attempt isolation; no commercial rates are seeded."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.billing_policy import BillingPolicyService
from app.platform import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .test_billing import account


@pytest.fixture
def configured(account):
    billing, auth, admin, user, other, gateway = account
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    billing.adjust(admin.id, owner_id=user.id, credit_units=1000, reason="test funding", idempotency_key="fund")
    version = policy.save_version(admin.id, document())
    policy.set_enabled(admin.id, revision=0, version_id=version["id"], enabled=True, reason="test enable")
    return billing, policy, admin, user, other


def document(**overrides):
    return {"rates": [{"provider": "test-provider", "model": "test-model", "inputUnitsPerMillion": 10,
                       "cachedInputUnitsPerMillion": 2, "outputUnitsPerMillion": 20}],
            "insufficientBalancePolicy": "cap_at_balance",
            "billableStatuses": ["review_required", "needs_input", "failed", "cancelled"],
            "reason": "TEST ONLY rates", **overrides}


def register(policy, user, attempt="attempt-1", job="job-1"):
    return policy.register_attempt(owner_id=user.id, job_id=job, attempt_id=attempt)


def usage(billing, user, call="call-1", attempt="attempt-1", job="job-1", **overrides):
    return billing.record_usage(**{"owner_id": user.id, "job_id": job, "attempt_id": attempt,
                                   "call_id": call, "stage": "planner", "provider": "test-provider", "model": "test-model",
                                   "input_tokens": 500000, "cached_input_tokens": 100000,
                                   "output_tokens": 200000, "reasoning_output_tokens": 100000, **overrides})


def settle(policy, user, calls=None, attempt="attempt-1", job="job-1", status="review_required"):
    return policy.settle_attempt(owner_id=user.id, job_id=job, attempt_id=attempt,
                                 terminal_status=status, call_ids=["call-1"] if calls is None else calls)


def test_unconfigured_has_no_prices_or_charging_and_disabled_attempt_not_retroactively_billed(account):
    billing, _, admin, user, _, _ = account
    policy = BillingPolicyService(billing)
    assert policy.overview(admin.id)["versions"] == []
    assert policy.status()["enabled"] is False
    assert register(policy, user)["chargeEnabled"] is False
    usage(billing, user)
    assert settle(policy, user)["reason"] == "disabled_at_start"
    assert billing.list_records("ledger", owner_id=user.id)["total"] == 0
    draft = policy.save_version(admin.id, document(insufficientBalancePolicy=None))
    with pytest.raises(ValidationError):
        policy.set_enabled(admin.id, revision=0, version_id=draft["id"], enabled=True, reason="test")
    draft = policy.save_version(admin.id, document())
    billing.supported_insufficient_balance_policies = set()  # Unsupported future deployment fails closed.
    with pytest.raises(ValidationError):
        policy.set_enabled(admin.id, revision=0, version_id=draft["id"], enabled=True, reason="wallet branch unavailable")
    assert policy.status()["enabled"] is False


@pytest.mark.parametrize("rates", [
    [{"provider": "p", "model": "m", "inputUnitsPerMillion": True, "cachedInputUnitsPerMillion": 1, "outputUnitsPerMillion": 1}],
    [{"provider": "p", "model": "m", "inputUnitsPerMillion": 1.5, "cachedInputUnitsPerMillion": 1, "outputUnitsPerMillion": 1}],
    [{"provider": "p", "model": "*", "inputUnitsPerMillion": 1, "cachedInputUnitsPerMillion": 1, "outputUnitsPerMillion": 1}],
    document()["rates"] * 2,
])
def test_rates_require_fixed_integers_and_exact_unique_provider_model(account, rates):
    billing, _, admin, _, _, _ = account
    with pytest.raises(ValidationError):
        BillingPolicyService(billing).save_version(admin.id, document(rates=rates))


def test_rules_and_start_snapshot_immutable_and_changes_use_optimistic_revision(configured):
    billing, policy, admin, user, _ = configured
    old = register(policy, user)
    revised = document()
    revised["rates"][0]["outputUnitsPerMillion"] = 1000
    new = policy.save_version(admin.id, revised)
    policy.set_enabled(admin.id, revision=1, version_id=new["id"], enabled=True, reason="test new version")
    with pytest.raises(ConflictError):
        policy.set_enabled(admin.id, revision=1, version_id=new["id"], enabled=False, reason="stale")
    assert register(policy, user)["versionId"] == old["versionId"]
    usage(billing, user)
    bill = settle(policy, user)
    assert bill["versionId"] == old["versionId"] and bill["chargedUnits"] == 9
    for table in ("billing_policy_versions", "billing_policy_attempts", "billing_policy_seals", "billing_policy_receipts"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with billing._transaction() as db:
                db.execute(f"DELETE FROM {table}")
    assert billing.list_records("audit", actor_id=admin.id)["total"] >= 5


def test_integer_split_cache_reasoning_and_round_once_per_attempt(configured):
    billing, policy, admin, user, _ = configured
    rates = document()["rates"]
    rates[0].update(inputUnitsPerMillion=1, cachedInputUnitsPerMillion=0, outputUnitsPerMillion=1)
    version = policy.save_version(admin.id, document(rates=rates))
    policy.set_enabled(admin.id, revision=1, version_id=version["id"], enabled=True, reason="test rounding")
    register(policy, user)
    usage(billing, user, call="one", input_tokens=1, cached_input_tokens=0, output_tokens=1, reasoning_output_tokens=1)
    usage(billing, user, call="two", input_tokens=1, cached_input_tokens=0, output_tokens=1, reasoning_output_tokens=1)
    result = settle(policy, user, ["one", "two"])
    assert result["rateNumerator"] == "4" and result["expectedCreditUnits"] == 1
    assert result["chargedUnits"] == 1
    records = billing.list_records("usage", actor_id=admin.id)["items"]
    assert all(row["costMicroUsd"] is None for row in records)  # Credits never fabricate supplier dollars.


def test_unique_call_roster_prevents_old_attempt_and_other_owner_usage_and_replays_once(configured):
    billing, policy, _, user, other = configured
    register(policy, user)
    usage(billing, user)
    usage(billing, user, "historical", attempt="prior-attempt")
    usage(billing, other, "other-owner")
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: settle(policy, user), range(8)))
    assert all(row == results[0] for row in results)
    assert results[0]["chargedUnits"] == 9 and billing.wallet(user.id)["creditUnits"] == 991
    assert len([row for row in billing.list_records("ledger", owner_id=user.id)["items"] if row["kind"] == "usage"]) == 1
    with pytest.raises(ConflictError):
        settle(policy, user, ["call-1", "historical"])
    with pytest.raises(NotFoundError):
        settle(policy, other)
    with pytest.raises(ConflictError):
        register(policy, other)


@pytest.mark.parametrize("missing", ["input_tokens", "cached_input_tokens", "output_tokens"])
def test_unknown_actual_usage_stays_pending_not_free_or_zero(configured, missing):
    billing, policy, _, user, _ = configured
    register(policy, user)
    other_fields = {missing: None}
    if missing == "input_tokens":
        other_fields["cached_input_tokens"] = None
    if missing == "output_tokens":
        other_fields["reasoning_output_tokens"] = None
    usage(billing, user, **other_fields)
    result = settle(policy, user)
    assert result["status"] == "needs_review" and result["expectedCreditUnits"] is None
    assert result["reason"] == "supplier_usage_unknown"
    assert billing.wallet(user.id)["creditUnits"] == 1000


def test_delayed_usage_is_pending_and_retry_can_finish_same_sealed_batch(configured):
    billing, policy, _, user, _ = configured
    register(policy, user)
    result = settle(policy, user)
    assert result["reason"] == "call_roster_mismatch" and result["expectedCreditUnits"] is None
    usage(billing, user)
    assert settle(policy, user)["chargedUnits"] == 9
    register(policy, user, "attempt-2")
    usage(billing, user, "second", "attempt-2")
    assert settle(policy, user, ["second"], "attempt-2")["chargedUnits"] == 9


def test_new_task_gate_does_not_freeze_and_stopped_service_defers_settlement(configured):
    billing, policy, admin, user, other = configured
    assert policy.check_start(other.id)["allowed"] is False
    assert register(policy, other)["allowed"] is False
    register(policy, user)
    assert billing.wallet(user.id)["creditUnits"] == 1000
    usage(billing, user)
    active = policy.status()
    policy.set_enabled(admin.id, revision=active["revision"], version_id=active["versionId"], enabled=False, reason="test stop")
    result = settle(policy, user)
    assert result["status"] == "pending_settlement" and result["chargedUnits"] == 0
    assert billing.wallet(user.id)["creditUnits"] == 1000
    assert policy.check_start(other.id)["chargeEnabled"] is False


def test_zero_calls_and_explicit_nonbillable_outcomes_not_invented_failure_refunds(configured):
    billing, policy, _, user, _ = configured
    register(policy, user)
    assert settle(policy, user, [], status="cancelled")["reason"] == "zero_actual_charge"
    register(policy, user, "second")
    usage(billing, user, "second-call", "second")
    assert settle(policy, user, ["second-call"], "second", status="failed")["chargedUnits"] == 9
    register(policy, user, "third")
    usage(billing, user, "third-call", "third")
    assert settle(policy, user, ["third-call"], "third", status="interrupted")["reason"] == "outcome_excluded_by_policy"


def test_settlement_read_scope_and_admin_changes(configured):
    billing, policy, admin, user, other = configured
    register(policy, user)
    usage(billing, user)
    settle(policy, user)
    assert policy.settlements(owner_id=user.id)["total"] == 1
    assert policy.settlements(owner_id=other.id)["total"] == 0
    assert policy.settlements(actor_id=admin.id)["total"] == 1
    with pytest.raises(AuthorizationError):
        policy.overview(user.id)
    with pytest.raises(AuthorizationError):
        policy.save_version(user.id, document())
    with pytest.raises(ValidationError):
        settle(policy, user, status="running")
