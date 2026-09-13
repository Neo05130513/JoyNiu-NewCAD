import test from 'node:test'
import assert from 'node:assert/strict'
import { billingPolicyDraftPayload, billingPolicyEnableBlock, billingPolicyStatePayload, createBillingPolicyClient, emptyBillingPolicyDraft, emptyBillingRate, parseBillingRate, validateBillingPolicyOverview } from './billingPolicyClient.js'
import { createBillingActionGate } from './billingClient.js'

const complete = () => ({ id: 'version-a', rates: [{ provider: 'exact-provider', model: 'exact-model', inputUnitsPerMillion: '0', cachedInputUnitsPerMillion: '1', outputUnitsPerMillion: '2' }], insufficientBalancePolicy: 'deferred_due', billableStatuses: ['ready'], reason: '管理员明确输入的配置' })
const overview = () => ({ revision: 4, enabled: false, configured: false, versions: [], supportedInsufficientBalancePolicies: ['deferred_due'] })

test('new policy contains no prices, insufficient-balance policy, or billable outcome defaults', () => {
  assert.deepEqual(emptyBillingPolicyDraft(), { rates: [], insufficientBalancePolicy: '', billableStatuses: [], reason: '' })
  assert.ok(Object.values(emptyBillingRate()).every(value => value === ''))
  assert.deepEqual(billingPolicyDraftPayload({ ...emptyBillingPolicyDraft(), reason: '保存待定版本' }), { rates: [], insufficientBalancePolicy: null, billableStatuses: [], reason: '保存待定版本' })
})

test('rate parsing accepts explicit zero but never infers it from blank or rounds a fraction', () => {
  for (const value of ['', null, undefined, '0.5', '-1', '1e3', '01', Infinity, '1000000000001']) assert.throws(() => parseBillingRate(value))
  assert.equal(parseBillingRate('0'), 0)
  assert.equal(parseBillingRate('1000000000000'), 1_000_000_000_000)
})

test('draft validates exact unique models and complete rate rows without defaulting omissions', () => {
  assert.equal(billingPolicyDraftPayload(complete()).rates[0].inputUnitsPerMillion, 0)
  const duplicate = complete(); duplicate.rates.push({ ...duplicate.rates[0] }); assert.throws(() => billingPolicyDraftPayload(duplicate), /只能配置/)
  const wildcard = complete(); wildcard.rates[0].model = '*'; assert.throws(() => billingPolicyDraftPayload(wildcard), /通配符/)
  const incomplete = complete(); delete incomplete.rates[0].cachedInputUnitsPerMillion; assert.throws(() => billingPolicyDraftPayload(incomplete))
  assert.throws(() => billingPolicyDraftPayload({ ...complete(), billableStatuses: ['ready', 'ready'] }))
  assert.throws(() => billingPolicyDraftPayload({ ...complete(), billableStatuses: ['running'] }))
  assert.throws(() => billingPolicyDraftPayload({ ...complete(), reason: '' }))
})

test('enabling requires explicit complete saved policy, supported server capability and confirmation', () => {
  assert.match(billingPolicyEnableBlock(null, overview()), /选择/)
  assert.match(billingPolicyEnableBlock({ ...complete(), rates: [] }, overview()), /费率/)
  assert.match(billingPolicyEnableBlock({ ...complete(), billableStatuses: [] }, overview()), /终态/)
  assert.match(billingPolicyEnableBlock({ ...complete(), insufficientBalancePolicy: null }, overview()), /选择/)
  assert.match(billingPolicyEnableBlock(complete(), { ...overview(), supportedInsufficientBalancePolicies: [] }), /尚未支持/)
  assert.throws(() => billingPolicyStatePayload({ overview: overview(), version: complete(), enabled: true, reason: '开启规则' }), /确认/)
  assert.deepEqual(billingPolicyStatePayload({ overview: overview(), version: complete(), enabled: true, confirmed: true, reason: '开启规则' }), { revision: 4, versionId: 'version-a', enabled: true, reason: '开启规则' })
  assert.deepEqual(billingPolicyStatePayload({ overview: overview(), version: null, enabled: false, reason: '关闭计费' }), { revision: 4, versionId: null, enabled: false, reason: '关闭计费' })
})

test('unknown server status fails closed', () => {
  assert.throws(() => validateBillingPolicyOverview({ ...overview(), enabled: 'false' }))
  assert.throws(() => validateBillingPolicyOverview({ ...overview(), supportedInsufficientBalancePolicies: undefined }))
})

test('policy client matches version/CAS contracts and isolates bearer credentials from payload', async () => {
  const calls = []
  const client = createBillingPolicyClient({ apiBase: 'https://cad.example/api/v1/', fetchImpl: async (url, options) => {
    calls.push({ url, options }); return { ok: true, json: async () => overview() }
  } })
  await client.overview('access-token')
  await client.saveVersion('access-token', complete())
  await client.setState('new-access-token', { revision: 4, versionId: 'version-a', enabled: false, reason: '关闭计费' })
  await client.settlements('access-token', { admin: false, offset: 20 })
  assert.equal(calls[0].url, 'https://cad.example/api/v1/billing/admin/policy')
  assert.equal(calls[0].options.credentials, 'include')
  assert.equal(calls[1].options.method, 'POST')
  assert.match(calls[1].url, /policy\/versions$/)
  assert.doesNotMatch(calls[1].options.body, /access-token/)
  assert.equal(calls[2].options.headers.Authorization, 'Bearer new-access-token')
  assert.deepEqual(JSON.parse(calls[2].options.body), { revision: 4, versionId: 'version-a', enabled: false, reason: '关闭计费' })
  assert.match(calls[3].url, /billing\/settlements\?limit=20&offset=20$/)
  await assert.rejects(client.overview(''), error => error.status === 401)
  assert.equal(calls.length, 4)
})

test('revision conflict is reported and timeout remains a visible error', async () => {
  const conflict = createBillingPolicyClient({ fetchImpl: async () => ({ ok: false, status: 409, json: async () => ({ detail: '规则已更新' }) }) })
  await assert.rejects(conflict.setState('token', {}), error => error.status === 409 && /已更新/.test(error.message))
  const timeout = createBillingPolicyClient({ timeoutMs: 1, fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))) })
  await assert.rejects(timeout.overview('token'), /超时/)
})

test('a repeated rule submit shares one in-flight operation and permits retry after failure', async () => {
  const gate = createBillingActionGate(); let count = 0; let resolve
  const operation = () => { count++; return new Promise(done => { resolve = done }) }
  const first = gate.run('policy-mutation', operation)
  assert.equal(first, gate.run('policy-mutation', operation))
  await Promise.resolve(); resolve('saved'); assert.equal(await first, 'saved'); assert.equal(count, 1)
  await assert.rejects(gate.run('policy-mutation', () => { throw new Error('failed') }))
  assert.equal(await gate.run('policy-mutation', async () => 'retried'), 'retried')
})
