import test from 'node:test'
import assert from 'node:assert/strict'
import { API_BASE } from './api.js'
import { adminReadPath, adminListParams, adminNumber, adminMoney, adminDate, adminElapsed, adminPageOffset, adminOrderStatusNames, adminTicketStatusNames, validateAdminPage, createAdminRequestGate,
  adminStopPayload, adminStopOutcome, requestAdminOperations, adminOperations } from './adminOperationsClient.js'

test('unknown financial and usage values stay unknown, while real zero remains visible', () => {
  for (const value of [null, undefined, NaN, Infinity, '', '0']) { assert.equal(adminNumber(value), '—'); assert.equal(adminMoney(value), '—') }
  assert.equal(adminNumber(0), '0'); assert.equal(adminMoney(0), '¥0.00'); assert.equal(adminMoney(1234), '¥12.34')
  assert.equal(adminDate('not a date'), '—'); assert.equal(adminDate(null), '—')
  assert.equal(adminElapsed(null), '—'); assert.equal(adminElapsed(-1), '—'); assert.equal(adminElapsed(0), '0 秒')
  assert.equal(adminElapsed(61), '1 分 1 秒'); assert.equal(adminElapsed(3660), '1 小时 1 分')
})

test('list filters are whitelisted and server-side pagination is bounded', () => {
  assert.deepEqual(adminListParams('tasks', { q: ' 客户甲 ', status: 'failed', ownerId: 'user-1', offset: 40, secret: 'must-not-send' }), { limit: 20, offset: 40, q: '客户甲', status: 'failed', ownerId: 'user-1' })
  assert.deepEqual(adminListParams('customers', { active: false, offset: -20, status: 'failed' }), { limit: 20, offset: 0, active: 'false' })
  assert.equal(adminListParams('tasks', { offset: Infinity }).offset, 1000000)
  assert.equal(adminListParams('tasks', { offset: 'n/a', status: '__proto__' }).offset, 0)
  assert.equal(adminListParams('tasks', { q: 'a'.repeat(400) }).q.length, 128)
  assert.deepEqual(adminListParams('audit', { source: 'accounts', actorId: 'admin-2', q: 'ignored' }), { limit: 20, offset: 0, source: 'accounts', actorId: 'admin-2' })
})

test('detail paths encode identifiers and never accept a remote path or wrong section', () => {
  assert.equal(adminReadPath('customers', { customerId: 'u/one?secret#fragment' }), '/customers/u%2Fone%3Fsecret%23fragment')
  assert.equal(adminReadPath('tasks', { runId: 'cad-1', q: 'ignored' }), '/tasks/cad-1')
  assert.match(adminReadPath('tasks', { customerId: 'not-task-id', status: 'failed' }), /^\/tasks\?limit=20&offset=0&status=failed$/)
  assert.equal(adminReadPath('overview', { ownerId: 'ignored' }), '/overview')
  assert.throws(() => adminReadPath('https://untrusted.example'))
})

test('bad pages cannot masquerade as an empty successful query', () => {
  for (const value of [null, {}, { items: [], total: -1, limit: 20, offset: 0 }, { items: [], total: 1.5, limit: 20, offset: 0 }, { items: [], total: 0, limit: 0, offset: 0 }, { items: {}, total: 0, limit: 20, offset: 0 }]) assert.throws(() => validateAdminPage(value))
  const empty = { items: [], total: 0, limit: 20, offset: 0 }
  assert.equal(validateAdminPage(empty), empty)
})

test('empty or shortened lists move an out-of-range URL to the last existing page', () => {
  assert.equal(adminPageOffset(0, 40), 0)
  assert.equal(adminPageOffset(20, 20), 0)
  assert.equal(adminPageOffset(21, 40), 20)
  assert.equal(adminPageOffset(100, 1000000), 80)
  assert.equal(adminPageOffset(100, 40), 40)
})

test('actual billing and support states retain their operational meaning', () => {
  assert.equal(adminTicketStatusNames.waiting_customer, '等待客户补充')
  assert.equal(adminTicketStatusNames.resolved, '已提供处理结论')
  for (const state of ['creating', 'pending_payment', 'payment_unavailable', 'paid', 'cancelled', 'expired', 'refunded', 'refund_processing', 'refund_closed', 'refund_abnormal']) {
    assert.equal(typeof adminOrderStatusNames[state], 'string')
  }
  assert.equal(adminOrderStatusNames.pending_payment, '待支付')
  assert.match(adminOrderStatusNames.refund_abnormal, /待核查/)
  assert.doesNotMatch(adminOrderStatusNames.refund_processing, /已退款/)
})

test('late responses from another account, query or detail lose their write permission', async () => {
  const gate = createAdminRequestGate(), applied = []
  let release
  const old = gate.begin('account-a:customer-1')
  const oldResponse = new Promise(resolve => { release = resolve }).then(value => { if (old.isCurrent()) applied.push(value) })
  const current = gate.begin('account-b:task-2')
  assert.equal(old.signal.aborted, true); assert.equal(current.isCurrent(), true)
  release('stale secret account data'); await oldResponse
  assert.deepEqual(applied, [])
  old.abort(); assert.equal(current.isCurrent(), true, 'old effect cleanup does not cancel the newer read')
  gate.abort(); assert.equal(current.isCurrent(), false); assert.equal(current.signal.aborted, true)
})

test('stop requests require an explicit reason and a stable valid operation identifier', () => {
  const payload = adminStopPayload('  客户反馈本任务重复  ', 'stop_123')
  assert.deepEqual(payload, { reason: '客户反馈本任务重复', idempotencyKey: 'stop_123' })
  assert.deepEqual(adminStopPayload(payload.reason, payload.idempotencyKey), payload)
  for (const reason of ['', '太短', 'x'.repeat(501)]) assert.throws(() => adminStopPayload(reason, 'stop-1'))
  for (const key of ['', 'bad key', 'a'.repeat(129), null]) assert.throws(() => adminStopPayload('客户反馈本任务重复', key))
})

test('a queued stop is never announced as completed, and foreign results are rejected', () => {
  assert.match(adminStopOutcome('cad-1', { task: { runId: 'cad-1', status: 'cancel_requested' }, accepted: true }), /正在等待任务结束/)
  assert.doesNotMatch(adminStopOutcome('cad-1', { task: { runId: 'cad-1', status: 'queued' }, accepted: true }), /已取消/)
  assert.match(adminStopOutcome('cad-1', { task: { runId: 'cad-1', status: 'ready' }, accepted: false }), /已完成/)
  assert.match(adminStopOutcome('cad-1', { task: { runId: 'cad-1', status: 'cancelled' }, accepted: true }), /任务已取消/)
  assert.throws(() => adminStopOutcome('cad-1', { task: { runId: 'other', status: 'cancelled' }, accepted: true }))
  assert.throws(() => adminStopOutcome('cad-1', { task: { runId: 'cad-1', status: 'cancelled' } }))
})

test('admin requests authenticate, bypass caches and cannot follow a redirected response', async () => {
  const result = await requestAdminOperations('/tasks/cad-1/cancel', { token: 'fixture-token', method: 'POST', body: { reason: '客户请求暂停处理', idempotencyKey: 'same-key' },
    fetcher: async (url, options) => {
      assert.equal(url, `${API_BASE}/admin/tasks/cad-1/cancel`)
      assert.equal(options.headers.Authorization, 'Bearer fixture-token'); assert.equal(options.credentials, 'include')
      assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store'); assert.equal(options.method, 'POST')
      assert.equal(JSON.parse(options.body).idempotencyKey, 'same-key')
      return Response.json({ accepted: true })
    } })
  assert.equal(result.accepted, true)
})

test('server errors do not expose raw paths, tracebacks, tokens or customer documents', async () => {
  for (const status of [401, 403, 404, 409, 422, 429, 500]) {
    await assert.rejects(requestAdminOperations('/system', { token: 'fixture', fetcher: async () => Response.json({ detail: '/private/secret key=sk-sensitive source drawing content' }, { status }) }), error => {
      assert.equal(error.status, status); assert.doesNotMatch(error.message, /private|sensitive|drawing content/); return true
    })
  }
  await assert.rejects(requestAdminOperations('/system', { fetcher: async () => new Response('not json') }), /数据不完整/)
})

test('aborted admin requests never start network work and discard late JSON', async () => {
  const before = new AbortController(); before.abort()
  await assert.rejects(requestAdminOperations('/overview', { signal: before.signal, fetcher: async () => assert.fail('must not fetch') }), { name: 'AbortError' })
  const during = new AbortController()
  await assert.rejects(requestAdminOperations('/overview', { signal: during.signal, fetcher: async () => { during.abort(); return Response.json({ customers: { total: 99 } }) } }), { name: 'AbortError' })
})

test('irrelevant detail parameters cannot skip list response validation', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => Response.json({ items: null })
  try { await assert.rejects(adminOperations.read('fixture', 'tasks', { customerId: 'irrelevant' }), /列表数据/) }
  finally { globalThis.fetch = original }
})
