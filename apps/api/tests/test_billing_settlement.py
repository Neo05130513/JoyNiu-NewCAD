"""Explicit, opt-in insufficient balance policies and atomic recharge recovery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from app.platform import ConflictError
from .test_billing import account, pay


def settle(billing, user, mode, *, key="attempt-1", units=10):
    return billing.settle_usage(owner_id=user.id, job_id="job-1", credit_units=units,
                                idempotency_key=key, policy_version="test-policy-version", reason="test actual usage",
                                insufficient_balance_policy=mode)


def fund(billing, admin, user, units=3, key="seed"):
    return billing.adjust(admin.id, owner_id=user.id, credit_units=units, reason="test funded balance", idempotency_key=key)


def buy(billing, admin, user, units, key):
    package = billing.save_package(admin.id, {"name": "TEST ONLY", "amountFen": 1, "creditUnits": units, "active": True})
    order = billing.create_order(user.id, package_id=package["id"], idempotency_key=key)
    _, event = pay(billing, order, event_id=key, transaction=key)
    return event


def test_deferred_due_is_separate_from_nonnegative_wallet_and_verified_recharge_pays_fifo(account):
    billing, _, admin, user, _, _ = account
    fund(billing, admin, user)
    result = settle(billing, user, "deferred_due")
    assert result["chargedUnits"] == 3 and result["deferredUnits"] == 7 and result["platformAbsorbedUnits"] == 0
    assert billing.wallet(user.id)["creditUnits"] == 0 and billing.wallet(user.id)["dueUnits"] == 7
    assert billing.can_start_charged_work(user.id) is False
    first = buy(billing, admin, user, 5, "recharge-1")
    assert billing.wallet(user.id)["dueUnits"] == 2 and billing.wallet(user.id)["creditUnits"] == 0
    billing.accept_payment(first)
    billing.accept_payment(replace(first, event_id="same-payment-new-event"))
    assert billing.wallet(user.id)["dueUnits"] == 2
    second = buy(billing, admin, user, 10, "recharge-2")
    assert billing.wallet(user.id)["dueUnits"] == 0 and billing.wallet(user.id)["creditUnits"] == 8
    assert billing.wallet(user.id)["canStartChargedWork"] is True
    assert settle(billing, user, "deferred_due") == result  # Initial receipt immutable after repayment.
    billing.accept_payment(second)
    assert billing.wallet(user.id)["creditUnits"] == 8
    ledger = billing.list_records("ledger", owner_id=user.id)["items"]
    assert sum(row["deltaUnits"] for row in ledger) == 8
    assert sorted(-row["deltaUnits"] for row in ledger if row["kind"] == "due_payment") == [2, 5]


def test_cap_at_balance_has_audited_platform_absorption_and_no_later_debt(account):
    billing, _, admin, user, _, _ = account
    fund(billing, admin, user)
    result = settle(billing, user, "cap_at_balance")
    assert result["chargedUnits"] == 3 and result["platformAbsorbedUnits"] == 7 and result["deferredUnits"] == 0
    assert billing.wallet(user.id)["creditUnits"] == billing.wallet(user.id)["dueUnits"] == 0
    buy(billing, admin, user, 5, "recharge")
    assert billing.wallet(user.id)["creditUnits"] == 5
    audits = billing.list_records("audit", actor_id=admin.id)["items"]
    assert any(row["action"] == "usage.settled" and row["details"]["platformAbsorbedUnits"] == 7 for row in audits)
    with pytest.raises(ConflictError):
        settle(billing, user, "deferred_due")


def test_concurrent_duplicate_and_distinct_attempts_never_overdraw_or_duplicate_due(account):
    billing, _, admin, user, _, _ = account
    fund(billing, admin, user, 15)
    with ThreadPoolExecutor(6) as pool:
        rows = list(pool.map(lambda _: settle(billing, user, "deferred_due"), range(12)))
    assert all(row == rows[0] for row in rows)
    assert billing.wallet(user.id)["creditUnits"] == 5
    with ThreadPoolExecutor(2) as pool:
        other = list(pool.map(lambda key: settle(billing, user, "deferred_due", key=key), ["attempt-2", "attempt-3"]))
    assert sum(row["chargedUnits"] for row in other) == 5 and sum(row["deferredUnits"] for row in other) == 15
    assert billing.wallet(user.id)["dueUnits"] == 15 and billing.wallet(user.id)["creditUnits"] == 0
    fund(billing, admin, user, 16, "support-compensation")
    assert billing.wallet(user.id)["creditUnits"] == 1 and billing.wallet(user.id)["dueUnits"] == 0
    for table in ("billing_usage_settlements", "billing_dues", "billing_due_payments"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with billing._transaction() as db:
                db.execute(f"DELETE FROM {table}")


def test_missing_policy_never_silently_caps_or_creates_due_and_toggle_replay_safe(account):
    billing, _, admin, user, _, _ = account
    fund(billing, admin, user)
    assert settle(billing, user, None)["status"] == "needs_review"
    assert billing.wallet(user.id)["creditUnits"] == 3 and billing.wallet(user.id)["dueUnits"] == 0
    billing.enabled = False
    assert settle(billing, user, "deferred_due")["status"] == "disabled"
    billing.enabled = True
    result = settle(billing, user, "deferred_due")
    billing.enabled = False
    assert settle(billing, user, "deferred_due") == result
