"""Terms are supplied by a separate service; no production terms are fabricated."""
import json
import sqlite3

import pytest

from app.billing import BillingUnavailable
from app.billing_policy import BillingPolicyService
from app.platform import ConflictError
from .test_billing import account, package
from .test_billing_policy import document


class TermsFixture:
    def __init__(self):
        self.published = False
        self.accepted = False
        self.checked = []
        self.version = "test-version-1"

    def ready(self):
        return self.published

    def require_acceptance(self, owner):
        self.checked.append(owner)
        if not self.accepted:
            raise ConflictError("请先阅读并接受当前条款")
        return {"acceptanceId": "test-acceptance", "versions": {key: self.version for key in ("service", "privacy", "credits")},
                "acceptedAt": "2030-01-01T00:00:00Z"}


def test_order_requires_current_terms_before_gateway_and_keeps_original_acceptance_on_replay(account):
    billing, _, admin, user, _, gateway = account
    terms = billing.terms_provider = TermsFixture()
    plan = package(billing, admin)
    assert billing.status()["purchaseEnabled"] is False
    with pytest.raises(BillingUnavailable):
        billing.create_order(user.id, package_id=plan["id"], idempotency_key="order")
    terms.published = True
    with pytest.raises(ConflictError):
        billing.create_order(user.id, package_id=plan["id"], idempotency_key="order")
    assert not gateway.calls
    terms.accepted = True
    order = billing.create_order(user.id, package_id=plan["id"], idempotency_key="order")
    assert order["termsAcceptance"]["versions"]["credits"] == "test-version-1"
    terms.version, terms.accepted = "new-version", False
    assert billing.create_order(user.id, package_id=plan["id"], idempotency_key="order")["termsAcceptance"] == order["termsAcceptance"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with billing._transaction() as db:
            db.execute("UPDATE billing_orders SET terms_json='{}'")


def test_only_new_charged_attempts_require_terms_and_snapshot_is_preserved(account):
    billing, _, admin, user, _, _ = account
    terms = billing.terms_provider = TermsFixture()
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    assert policy.register_attempt(owner_id=user.id, job_id="job", attempt_id="free")["chargeEnabled"] is False
    assert terms.checked == []
    version = policy.save_version(admin.id, document())
    policy.set_enabled(admin.id, revision=0, version_id=version["id"], enabled=True, reason="test enable")
    billing.adjust(admin.id, owner_id=user.id, credit_units=20, reason="TEST balance", idempotency_key="fund")
    with pytest.raises(BillingUnavailable):
        policy.check_start(user.id)
    terms.published = True
    with pytest.raises(ConflictError):
        policy.check_start(user.id)
    terms.accepted = True
    policy.register_attempt(owner_id=user.id, job_id="job", attempt_id="charged")
    with billing._lock:
        saved = json.loads(billing._db.execute("SELECT terms_json FROM billing_policy_attempts WHERE attempt_id='charged'").fetchone()[0])
    assert saved["acceptanceId"] == "test-acceptance"
    terms.accepted = False
    assert policy.register_attempt(owner_id=user.id, job_id="job", attempt_id="charged")["chargeEnabled"] is True


def test_first_call_recheck_requires_current_terms_and_account_without_rewriting_snapshot(account):
    from app.platform import AuthorizationError
    billing, auth, admin, user, _, _ = account
    terms = billing.terms_provider = TermsFixture()
    terms.published = terms.accepted = True
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    version = policy.save_version(admin.id, document())
    policy.set_enabled(admin.id, revision=0, version_id=version["id"], enabled=True, reason="test enable")
    billing.adjust(admin.id, owner_id=user.id, credit_units=20, reason="TEST balance", idempotency_key="fund")
    identity = dict(owner_id=user.id, job_id="job", attempt_id="queued")
    policy.register_attempt(**identity)
    original = dict(policy._read_attempt(user.id, "job", "queued"))
    terms.version, terms.accepted = "new-version", False
    with pytest.raises(ConflictError):
        policy.recheck_attempt_start(**identity)
    terms.accepted = True
    newer = policy.save_version(admin.id, document(reason="test new rates"))
    policy.set_enabled(admin.id, revision=1, version_id=newer["id"], enabled=True, reason="test enable new")
    assert policy.recheck_attempt_start(**identity) == {"allowed": True, "versionId": version["id"]}
    assert dict(policy._read_attempt(user.id, "job", "queued")) == original
    auth.set_active(user.id, False, actor_id=admin.id)
    with pytest.raises(AuthorizationError):
        policy.recheck_attempt_start(**identity)
