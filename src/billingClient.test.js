import test from 'node:test'
import assert from 'node:assert/strict'
import { billingSessionKey, billingStatusLabel, canReviewBillingRequest, createBillingActionGate, createBillingClient, formatCreditUnits, formatFen, parseCreditUnits, parseMoneyFen, paymentCheckout, refundResultNotice, shouldPollOrder } from './billingClient.js'
import { billingPublicPricing, formatBillingRate, billingPricingNote, offlineRechargeUnits } from './billingClient.js'

test('offline amount maps exactly to points and independent payment switch suppresses a stale checkout', () => {
  assert.equal(offlineRechargeUnits('10.00'), 1000)
  assert.equal(offlineRechargeUnits('12.34'), 1234)
  const pending = { status: 'pending_payment', checkout: { codeUrl: 'https://pay.example.test/order' } }
  assert.equal(paymentCheckout(pending, Date.now(), false).canPay, false)
  assert.equal(paymentCheckout(pending, Date.now(), false).url, '')
  assert.equal(paymentCheckout({ ...pending, provider: 'offline' }).canPay, false)
})

test('offline writes carry actual receipt confirmation and reuse the caller idempotency key', async () => {
  const calls = []
  const api = createBillingClient({ fetchImpl: async (url, init) => { calls.push({ url, ...init }); return { ok: true, json: async () => ({ id: 'actual-order' }) } } })
  const body = { ownerId: 'customer', amountFen: 1000, externalReference: 'BANK-001', reason: '核对到账', receiptConfirmed: true, idempotencyKey: 'same-key' }
  await api.offlineRecharge('token', body)
  await api.offlineRecharge('token', body)
  assert.deepEqual(JSON.parse(calls[0].body), body)
  assert.equal(calls[0].body, calls[1].body)
  assert.match(calls[0].url, /\/billing\/admin\/offline-recharges$/)
  await api.offlineRefund('token', 'ord/specific', { externalReference: 'REFUND-001', reason: '完成', refundConfirmed: true, idempotencyKey: 'r1' })
  assert.match(calls[2].url, /ord%2Fspecific\/refund$/)
  assert.equal(calls[2].headers.Authorization, 'Bearer token')
})

test('public official rates preserve exact decimal values and reject malformed rates', () => {
  const pricing = { enabled: true, versionId: 'official', tokenUnit: 1_000_000, billableStatuses: ['ready', 'review_required'], insufficientBalancePolicy: 'deferred_due',
    apiPricing: { usdCnyRate: '7.234567', multiplier: '1.5', pointsPerCny: 100, serviceTier: 'standard' },
    rates: [{ provider: 'test', model: 'test', inputUnitsPerMillion: '7234.567', cachedInputUnitsPerMillion: '723.4567', cacheWriteUnitsPerMillion: '9043.208750000001', outputUnitsPerMillion: '36172.835' }] }
  assert.equal(billingPublicPricing(pricing).rates[0].cacheWriteUnitsPerMillion, '9043.208750000001')
  assert.equal(formatBillingRate('7234.567'), '7,234.567')
  for (const invalid of ['1e3', '-1', 'Infinity', true, null, '1.1234567890123456789']) {
    assert.throws(() => billingPublicPricing({ ...pricing, rates: [{ ...pricing.rates[0], inputUnitsPerMillion: invalid }] }))
  }
  assert.match(billingPricingNote('cache_write_premium_waived'), /未加收写入差价/)
})

test('currency parsing stores exact integer fen and rejects rounding and exponent input', () => {
  assert.equal(parseMoneyFen('0.01'), 1)
  assert.equal(parseMoneyFen('10.10'), 1010)
  assert.equal(parseMoneyFen(' 19.9 '), 1990)
  assert.equal(parseMoneyFen('1000000'), 100_000_000)
  for (const value of ['', '0', '-1', '1.001', '1e2', 'Infinity', 'NaN', '01', '1,000', '1000000.01']) assert.throws(() => parseMoneyFen(value), undefined, value)
  assert.throws(() => parseMoneyFen('10.01', { maximum: 1000 }))
  assert.equal(formatFen(12345), '¥123.45')
  assert.equal(formatFen(null), '—')
  assert.equal(formatCreditUnits(null), '—')
  assert.equal(formatCreditUnits(0), '0')
})

test('credit input is integral, bounded, nonzero, and negative only for an adjustment', () => {
  assert.equal(parseCreditUnits('100'), 100)
  assert.equal(parseCreditUnits('-100', { signed: true }), -100)
  for (const value of ['0', '-1', '1.1', '1e3', '1000000000001', 'NaN', '+1']) assert.throws(() => parseCreditUnits(value), undefined, value)
  assert.throws(() => parseCreditUnits('0', { signed: true }))
  assert.throws(() => parseCreditUnits('-1000000000001', { signed: true }))
})

test('session refresh preserves purchase intent identity while account switching clears it', () => {
  const a = { user: { id: 'account-a' }, access_token: 'old-access' }
  assert.equal(billingSessionKey(a), billingSessionKey({ ...a, access_token: 'rotated-access' }))
  assert.notEqual(billingSessionKey(a), billingSessionKey({ user: { id: 'account-b' }, access_token: 'new-account' }))
  assert.notEqual(billingSessionKey(a), billingSessionKey(a, true))
})

test('only actual pending checkouts show payment links and expired links stop being payable', () => {
  const order = { status: 'pending_payment', checkout: { codeUrl: 'weixin://wxpay/bizpayurl?pr=encoded', expiresAt: '2026-09-10T12:00:00Z' } }
  assert.equal(paymentCheckout(order, Date.parse('2026-09-10T11:59:59Z')).canPay, true)
  assert.equal(paymentCheckout(order, Date.parse('2026-09-10T12:00:00Z')).canPay, false)
  assert.equal(paymentCheckout({ ...order, status: 'paid' }, 0).canPay, false)
  assert.equal(paymentCheckout({ status: 'pending_payment', checkout: { codeUrl: 'javascript:alert(1)' } }).url, '')
  assert.equal(paymentCheckout({ status: 'pending_payment', checkout: { codeUrl: 'https://user:password@pay.example.com' } }).url, '')
  assert.equal(paymentCheckout({ status: 'pending_payment', checkout: {} }).canPay, false)
  assert.equal(shouldPollOrder(order), true)
  assert.equal(shouldPollOrder({ status: 'paid' }), false)
  assert.equal(shouldPollOrder({ status: 'payment_unavailable' }), false)
  assert.match(billingStatusLabel('approved'), /待渠道处理/)
  assert.notEqual(billingStatusLabel('approved'), '已退款')
})

test('synchronous duplicate clicks share exactly one pending mutation and release after completion', async () => {
  const gate = createBillingActionGate()
  let finish, count = 0
  const operation = () => { count += 1; return new Promise((resolve) => { finish = resolve }) }
  const first = gate.run('purchase:one', operation)
  const duplicate = gate.run('purchase:one', operation)
  assert.equal(first, duplicate)
  assert.equal(gate.has('purchase:one'), true)
  await Promise.resolve()
  assert.equal(count, 1)
  finish({ orderId: 'real-response' })
  assert.deepEqual(await first, { orderId: 'real-response' })
  assert.equal(gate.has('purchase:one'), false)
  await gate.run('purchase:one', async () => { count += 1 })
  assert.equal(count, 2)
})

test('failed mutations release the click gate so the same idempotency key can retry', async () => {
  const gate = createBillingActionGate()
  await assert.rejects(gate.run('request', () => { throw new Error('network interrupted') }), /interrupted/)
  assert.equal(gate.has('request'), false)
  assert.equal(await gate.run('request', async () => 'retried'), 'retried')
})

test('billing writes carry real identifiers and session authorization only in headers', async () => {
  const calls = []
  const client = createBillingClient({ apiBase: 'https://cad.example.com/api/v1/', fetchImpl: async (...args) => {
    calls.push(args)
    return { ok: true, status: 201, json: async () => ({ id: 'server-record' }) }
  } })
  await client.createOrder('session-only', 'pkg-real', 'logical-purchase-1')
  await client.requestService('session-only', 'refund', 'order-id', { reason: '退款', amountFen: 100, idempotencyKey: 'refund-1' })
  await client.savePackage('session-only', { version: 2, active: false }, 'pkg-real')
  assert.equal(calls[0][0], 'https://cad.example.com/api/v1/billing/orders')
  assert.equal(calls[0][1].credentials, 'include')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer session-only')
  assert.deepEqual(JSON.parse(calls[0][1].body), { packageId: 'pkg-real', idempotencyKey: 'logical-purchase-1' })
  assert.doesNotMatch(calls[0][1].body, /session-only|paid|creditUnits|ownerId/)
  assert.equal(calls[1][0], 'https://cad.example.com/api/v1/billing/orders/order-id/refund-requests')
  assert.equal(calls[2][1].method, 'PATCH')
})

test('wallet requires a session and disabled payment errors remain visible', async () => {
  let count = 0
  const client = createBillingClient({ apiBase: 'https://cad.example.com/api/v1', fetchImpl: async () => {
    count += 1
    return { ok: false, status: 503, json: async () => ({ detail: '支付渠道尚未开通' }) }
  } })
  await assert.rejects(client.wallet(''), (error) => error.status === 401)
  assert.equal(count, 0)
  await assert.rejects(client.createOrder('session', 'pkg', 'idem'), (error) => error.status === 503 && error.message === '支付渠道尚未开通')
  assert.equal(count, 1)
})

test('refund approval never appears as successful execution before channel confirmation', () => {
  assert.equal(canReviewBillingRequest({ status: 'approved' }), false)
  assert.equal(canReviewBillingRequest({ status: 'refund_processing' }), false)
  assert.equal(canReviewBillingRequest({ status: 'reviewing' }), true)
  assert.doesNotMatch(refundResultNotice({ request: { status: 'approved' }, refund: { status: 'unknown' } }), /退款成功/)
  assert.doesNotMatch(refundResultNotice({ request: { status: 'refund_processing' }, refund: { status: 'processing' } }), /退款成功/)
  assert.match(refundResultNotice({ request: { status: 'refunded' }, refund: { status: 'succeeded' } }), /已确认退款成功/)
})

test('real gateway reconciliation and refund execution send only the server-owned record id', async () => {
  const calls = []
  const client = createBillingClient({ apiBase: 'https://cad.example.com/api/v1', fetchImpl: async (...args) => { calls.push(args); return { ok: true, status: 200, json: async () => ({}) } } })
  await client.reconcileOrder('session', 'order/1')
  await client.executeRefund('session', 'request-1')
  await client.reconcileRefund('session', 'request-1')
  assert.match(calls[0][0], /orders\/order%2F1\/reconcile$/)
  assert.match(calls[1][0], /admin\/requests\/request-1\/refund$/)
  assert.match(calls[2][0], /admin\/requests\/request-1\/refund\/reconcile$/)
  for (const [, options] of calls) { assert.equal(options.method, 'POST'); assert.equal(options.body, '{}') }
})


test('wallet explains actual outstanding dues without treating missing amounts as debt', async () => {
  const { walletDueNotice, billingLedgerLabel } = await import('./billingClient.js')
  assert.match(walletDueNotice({ dueUnits: 50 }), /50 积分待补缴/)
  assert.match(walletDueNotice({ dueUnits: 50 }), /优先补缴/)
  for (const wallet of [null, {}, { dueUnits: 0 }, { dueUnits: -1 }, { dueUnits: '50' }]) assert.equal(walletDueNotice(wallet), '')
  assert.equal(billingLedgerLabel({ kind: 'due_payment' }), '补缴已完成任务')
})

test('own settlement history uses the user route and request timeout is visible', async () => {
  const { createBillingClient } = await import('./billingClient.js')
  let route
  const client = createBillingClient({ fetchImpl: async url => { route = url; return { ok: true, json: async () => ({ items: [], total: 0 }) } } })
  await client.list('token', 'settlements', { offset: 20 })
  assert.match(route, /billing\/settlements\?limit=20&offset=20$/)
  const timed = createBillingClient({ timeoutMs: 1, fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))) })
  await assert.rejects(timed.wallet('token'), /请求超时/)
})

test('public pricing displays only explicitly enabled complete real rates', async () => {
  const { billingPublicPricing } = await import('./billingClient.js')
  assert.deepEqual(billingPublicPricing({ enabled: false, rates: [{ secretDraft: true }], billableStatuses: ['failed'], insufficientBalancePolicy: 'deferred_due' }).rates, [])
  assert.throws(() => billingPublicPricing({ enabled: 'true' }))
  assert.throws(() => billingPublicPricing({ enabled: true, rates: [] }))
  const price = { enabled: true, versionId: 'rate-v1', tokenUnit: 1000000, billableStatuses: ['ready'], insufficientBalancePolicy: 'cap_at_balance', rates: [{ provider: 'provider', model: 'model', inputUnitsPerMillion: 0, cachedInputUnitsPerMillion: 1, outputUnitsPerMillion: 2 }] }
  assert.equal(billingPublicPricing(price), price)
  let authorization
  const client = createBillingClient({ fetchImpl: async (url, options) => { assert.match(url, /billing\/pricing$/); authorization = options.headers.Authorization; return { ok: true, json: async () => price } } })
  assert.equal((await client.pricing()).rates[0].inputUnitsPerMillion, 0)
  assert.equal(authorization, undefined)
})
