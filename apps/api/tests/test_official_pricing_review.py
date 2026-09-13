"""Independent regression review: local providers/SQLite only, no model calls."""
import pytest

from app.cad_agent_store import CadRunStore
from app.cad_job_registry import CadJobRegistry
from app.metered_cad_provider import MeteredCadProvider, RunCallContext
from app.billing_policy import BillingPolicyService
from app.official_api_pricing import CATALOG
from app.platform import ValidationError
from .test_billing import account
from .test_official_api_pricing import setup, finish, record, document


def test_retried_codex_call_counts_each_real_call_once_without_readding_cached_or_reasoning(account, tmp_path):
    billing, policy, admin, user, _ = setup(account)
    policy.register_attempt(owner_id=user.id, job_id='job', attempt_id='a')
    registry = CadJobRegistry(CadRunStore(tmp_path / 'cad'))
    context = RunCallContext(registry=registry, billing=billing, owner=user.id, job_id='job', attempt_id='a')

    class LocalRetryProvider:
        supports_diagnostics = True
        provider_info = {'mode': 'codex', 'model': 'gpt-6-astra'}
        calls = 0

        def __call__(self, body, timeout, on_diagnostics):
            self.calls += 1
            usage = {'input_tokens': 1000, 'cached_tokens': 800, 'output_tokens': 100, 'reasoning_tokens': 60}
            on_diagnostics({'usage': {**usage, 'output_tokens': 50, 'reasoning_tokens': 30}})
            on_diagnostics({'usage': usage})
            if self.calls == 1:
                error = RuntimeError('deterministic failed response with known usage')
                error.diagnostics = {'usage': usage}
                raise error
            return {'usage': usage}

    provider = LocalRetryProvider()
    wrapped = MeteredCadProvider(provider, context)
    with pytest.raises(RuntimeError):
        wrapped({'model': 'gpt-6-astra'}, 1)
    wrapped({'model': 'gpt-6-astra'}, 1)
    registry.deliver_usage(billing)
    registry.deliver_usage(billing)
    rows = billing.list_records('usage', actor_id=admin.id)['items']
    assert len(rows) == 2 and {row['outcome'] for row in rows} == {'failed', 'completed'}
    assert all(row['usageScope'] == 'aggregate' and row['cachedInputTokens'] == 800 for row in rows)
    result = finish(policy, user, calls=[row['callId'] for row in rows])
    # 2 * ((200 * $10 + 800 * $1 + 100 * $50) / 1M) * 7 * 100 = 10.92.
    assert result['creditUnitsExact'] == '10.92' and result['chargedUnits'] == 11
    assert result['callCount'] == 2
    assert finish(policy, user, calls=[row['callId'] for row in rows]) == result
    assert billing.wallet(user.id)['creditUnits'] == 9989


def test_saved_official_attempt_survives_changes_to_live_catalog(account, monkeypatch):
    billing, policy, _, user, _ = setup(account)
    policy.register_attempt(owner_id=user.id, job_id='job', attempt_id='a')
    record(billing, user)
    monkeypatch.setitem(CATALOG['models'][0], 'inputUsdPerMillion', '999')
    monkeypatch.setitem(CATALOG['models'][0], 'longContextInputMultiplier', '99')
    result = finish(policy, user)
    assert result['creditUnitsExact'] == '976.5'
    assert result['chargedUnits'] == 977


def test_legacy_attempt_keeps_legacy_formula_after_official_rule_activation(account):
    billing, _, admin, user, _, _ = account
    policy = BillingPolicyService(billing)
    billing.charging_status_provider = policy.status
    old = policy.save_version(admin.id, {
        'rates': [{'provider': 'codex', 'model': 'gpt-6-astra', 'inputUnitsPerMillion': 10, 'cachedInputUnitsPerMillion': 1, 'outputUnitsPerMillion': 50}],
        'billableStatuses': ['ready', 'review_required'], 'insufficientBalancePolicy': 'deferred_due', 'reason': 'Legacy exact rule',
    })
    policy.set_enabled(admin.id, revision=0, version_id=old['id'], enabled=True, reason='TEST')
    billing.adjust(admin.id, owner_id=user.id, credit_units=10000, reason='TEST funding', idempotency_key='legacy-fund')
    policy.register_attempt(owner_id=user.id, job_id='job', attempt_id='a')
    new = policy.save_version(admin.id, document())
    policy.set_enabled(admin.id, revision=1, version_id=new['id'], enabled=True, reason='TEST migrate')
    record(billing, user)
    result = finish(policy, user)
    assert result['versionId'] == old['id']
    assert result['expectedCreditUnits'] == 2
    assert 'apiPricing' not in result and 'callBreakdown' not in result


@pytest.mark.parametrize('updates', [
    {'billableStatuses': ['ready']}, {'billableStatuses': ['review_required']},
    {'billableStatuses': ['ready', 'review_required', 'failed']}, {'billableStatuses': []},
    {'insufficientBalancePolicy': 'cap_at_balance'}, {'insufficientBalancePolicy': None},
])
def test_new_official_rule_enforces_confirmed_customer_billing_scope(account, updates):
    billing, _, admin, _, _, _ = account
    policy = BillingPolicyService(billing)
    with pytest.raises(ValidationError, match='固定仅对成功候选'):
        policy.save_version(admin.id, {**document(), **updates})
    assert policy.overview(admin.id)['versions'] == []


def test_nonchargeable_attempt_skips_paid_admission_but_keeps_immutable_usage(account, monkeypatch):
    billing, policy, _, user, _ = setup(account)
    def blocked(*_args, **_kwargs):
        raise AssertionError('Nonchargeable operation must not request paid admission')
    monkeypatch.setattr(policy, 'check_start', blocked)
    registered = policy.register_attempt(owner_id=user.id, job_id='job', attempt_id='a', chargeable=False)
    assert registered['chargeEnabled'] is False and registered['allowed'] is True
    # Replaying this registration can never turn the existing operation into a charge.
    assert policy.register_attempt(owner_id=user.id, job_id='job', attempt_id='a')['chargeEnabled'] is False
    record(billing, user)
    result = finish(policy, user)
    assert result['status'] == 'not_charged' and result['chargedUnits'] == 0
    assert billing.wallet(user.id)['creditUnits'] == 10000
    assert finish(policy, user) == result
    with pytest.raises(ValidationError, match='布尔'):
        policy.register_attempt(owner_id=user.id, job_id='job', attempt_id='bad', chargeable='false')
