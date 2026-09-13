"""API-equivalent customer billing with exact math; no real AI/customer charge."""
from fractions import Fraction
import pytest
from app.official_api_pricing import CATALOG_VERSION, catalog, price_call, normalize_api_pricing
from app.billing_policy import BillingPolicyService
from app.platform import ValidationError
from .test_billing import account


def document(multiplier='1', fx='7'):
    return {'apiPricing': {'catalogVersion': CATALOG_VERSION, 'usdCnyRate': fx, 'multiplier': multiplier, 'pointsPerCny': 100},
            'rates': [{'provider':'codex','model':'gpt-6-astra'}],
            'billableStatuses':['ready','review_required'], 'insufficientBalancePolicy':'deferred_due', 'reason':'TEST fixture'}


def call(**kwargs):
    return {'callId':'one','provider':'codex','model':'gpt-6-astra','inputTokens':100000,'cachedInputTokens':20000,
            'cacheWriteTokens':30000,'outputTokens':10000,'usageScope':'request', **kwargs}


def test_exact_standard_cache_write_and_reasoning_not_double_counted():
    config, rates = normalize_api_pricing(document()['apiPricing'], document()['rates'])
    points, detail = price_call(call(reasoningOutputTokens=9000), rates[0], config)
    # (50k * 10 + 20k * 1 + 30k * 12.5 + 10k * 50) / 1M = $1.395
    assert points == Fraction('976.5')
    assert detail['apiEquivalentUsd'] == '1.395'
    assert detail['notes'] == []
    assert rates[0]['cacheWriteUnitsPerMillion'] == '8750'


def test_long_context_is_per_request_not_accumulated_turn():
    config, rates = normalize_api_pricing(document()['apiPricing'], document()['rates'])
    _, short = price_call(call(inputTokens=272000), rates[0], config)
    _, long = price_call(call(inputTokens=272001), rates[0], config)
    assert short['contextBand'] == 'standard' and long['contextBand'] == 'long'
    _, total = price_call(call(inputTokens=900000, usageScope='aggregate', cacheWriteTokens=None), rates[0], config)
    assert total['contextBand'] == 'standard'
    assert set(total['notes']) == {'cache_write_premium_waived', 'unconfirmed_long_context_premium_waived'}
    assert price_call(call(cacheWriteTokens=90000), rates[0], config) is None


@pytest.mark.parametrize('field,value',[('multiplier',True),('multiplier','NaN'),('multiplier','0'),('multiplier','-1'),('multiplier','1e2'),('multiplier','101'),('usdCnyRate',''),('pointsPerCny',1000),('catalogVersion','invented')])
def test_pricing_validates_decimal_bounds_and_server_owned_baseline(field,value):
    payload=document(); payload['apiPricing'][field]=value
    with pytest.raises(ValidationError): normalize_api_pricing(payload['apiPricing'],payload['rates'])


def test_price_catalog_immutable_copy_and_no_client_price_override():
    first=catalog();first['models'][0]['inputUsdPerMillion']='0'
    assert catalog()['models'][0]['inputUsdPerMillion']=='10'
    bad=document()['rates'];bad[0]['inputUsdPerMillion']='0'
    with pytest.raises(ValidationError): normalize_api_pricing(document()['apiPricing'],bad)


def setup(account,multiplier='1'):
    billing,_,admin,user,other,_=account
    policy=BillingPolicyService(billing);billing.charging_status_provider=policy.status
    v=policy.save_version(admin.id,document(multiplier))
    policy.set_enabled(admin.id,revision=0,version_id=v['id'],enabled=True,reason='TEST')
    billing.adjust(admin.id,owner_id=user.id,credit_units=10000,reason='TEST',idempotency_key='fund')
    return billing,policy,admin,user,v


def record(billing,user,attempt='a',cid='c',**extra):
    billing.record_usage(owner_id=user.id,job_id='job',attempt_id=attempt,call_id=cid,stage='planner',provider='codex',model='gpt-6-astra',
                         input_tokens=100000,cached_input_tokens=20000,cache_write_tokens=30000,output_tokens=10000,
                         reasoning_output_tokens=9000,usage_scope='request',**extra)


def finish(policy,user,attempt='a',calls=None,status='review_required'):
    return policy.settle_attempt(owner_id=user.id,job_id='job',attempt_id=attempt,terminal_status=status,call_ids=calls or ['c'])


def test_multiplier_is_snapshotted_and_receipt_replay_cannot_redebit(account):
    billing,policy,admin,user,old=setup(account)
    policy.register_attempt(owner_id=user.id,job_id='job',attempt_id='a')
    new=policy.save_version(admin.id,document('2'))
    policy.set_enabled(admin.id,revision=1,version_id=new['id'],enabled=True,reason='TEST next')
    record(billing,user)
    a=finish(policy,user)
    assert a['chargedUnits']==977 and a['creditUnitsExact']=='976.5'
    assert a['apiPricing']['multiplier']=='1' and a['versionId']==old['id']
    assert finish(policy,user)==a
    policy.register_attempt(owner_id=user.id,job_id='job',attempt_id='b')
    record(billing,user,attempt='b',cid='d')
    b=finish(policy,user,attempt='b',calls=['d'])
    assert b['chargedUnits']==1953
    assert b['apiPricing']['multiplier']=='2'
    assert policy.pricing()['apiPricing']['multiplier']=='2'
    assert billing.list_records('usage',actor_id=admin.id)['items'][0]['costMicroUsd'] is None


@pytest.mark.parametrize('status',['failed','needs_input','cancelled','interrupted'])
def test_unfinished_rounds_excluded_even_with_known_token_usage(account,status):
    billing,policy,_,user,_=setup(account)
    policy.register_attempt(owner_id=user.id,job_id='job',attempt_id='a');record(billing,user)
    receipt=finish(policy,user,status=status)
    assert receipt['status']=='not_charged' and receipt['expectedCreditUnits']==0
    assert billing.wallet(user.id)['creditUnits']==10000


def test_all_calls_sum_before_rounding_and_insufficient_becomes_due(account):
    billing,policy,admin,user,_=setup(account)
    billing.adjust(admin.id,owner_id=user.id,credit_units=-9000,reason='TEST lower',idempotency_key='lower')
    policy.register_attempt(owner_id=user.id,job_id='job',attempt_id='a')
    record(billing,user,cid='c');record(billing,user,cid='d')
    receipt=finish(policy,user,calls=['c','d'])
    assert receipt['expectedCreditUnits']==1953
    assert receipt['chargedUnits']==1000 and receipt['deferredUnits']==953
    assert policy.check_start(user.id)['allowed'] is False
    billing.adjust(admin.id,owner_id=user.id,credit_units=1000,reason='TEST repayment',idempotency_key='repay')
    assert policy.check_start(user.id)['allowed'] is True
    assert billing.wallet(user.id)['creditUnits']==47
