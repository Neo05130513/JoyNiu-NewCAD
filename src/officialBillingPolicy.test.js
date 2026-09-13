import test from 'node:test'
import assert from 'node:assert/strict'
import { officialBillingPolicyDraft, billingPolicyDraftPayload, parsePricingDecimal, billingPolicyEnableBlock, formatPolicyRate } from './billingPolicyClient.js'
const catalog = { version: 'verified-v1', models: [{ model: 'gpt-6-astra' }] }
test('official mode defaults to requested successful candidates and deferred dues, with 1x adjustable multiplier', () => {
  const draft = officialBillingPolicyDraft(catalog)
  assert.deepEqual(draft.billableStatuses, ['ready', 'review_required'])
  assert.equal(draft.insufficientBalancePolicy, 'deferred_due')
  assert.equal(draft.apiPricing.multiplier, '1')
  assert.equal(draft.apiPricing.pointsPerCny, 100)
  const payload = billingPolicyDraftPayload({ ...draft, reason: '调整倍率', apiPricing: { ...draft.apiPricing, multiplier: '1.25', usdCnyRate: '6.8888' } })
  assert.deepEqual(payload.rates, [{ provider: 'codex', model: 'gpt-6-astra' }])
  assert.equal(payload.apiPricing.multiplier, '1.25')
  assert.equal(payload.apiPricing.usdCnyRate, '6.8888')
})
test('pricing parser rejects empty, zero, negative, unsafe exponent or excessive precision', () => {
  for (const v of ['', '0', 'NaN', Infinity, true, '-1', '100.1', '1e1', '1.0000001']) assert.throws(() => parsePricingDecimal(v))
  assert.equal(parsePricingDecimal('0.000001'), '0.000001')
  assert.equal(formatPolicyRate('700.1234'), '700.1234')
})
test('saved official rules are editable by cloning settings and preserve old decimal prices', () => {
  const version = { ...officialBillingPolicyDraft(catalog), id: 'version', reason: '保存规则' }
  version.apiPricing.multiplier = '2.25'
  version.rates[0].inputUnitsPerMillion = '15750'
  version.apiPricing.sourceUrl = 'https://developers.openai.com/api/docs/pricing'
  assert.equal(billingPolicyEnableBlock(version, { supportedInsufficientBalancePolicies: ['deferred_due'] }), '')
  const draft = officialBillingPolicyDraft(catalog, version)
  draft.apiPricing.multiplier = '3'
  assert.equal(version.apiPricing.multiplier, '2.25')
  assert.equal(billingPolicyDraftPayload({ ...draft, reason: '新的规则' }).apiPricing.multiplier, '3')
})
