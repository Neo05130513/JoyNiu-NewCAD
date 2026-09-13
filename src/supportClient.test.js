import test from 'node:test'
import assert from 'node:assert/strict'
import { createSupportClient, createSupportIntentKeys, emptySupportDraft, supportCreatePayload, supportMessagePayload, supportPageOffset, supportSessionKey, supportTransitions, supportUpdatePayload } from './supportClient.js'
import { createBillingActionGate } from './billingClient.js'

test('drawing consent is never defaulted on and a linked task must be explicit', () => {
  assert.equal(emptySupportDraft().drawingConsent, false)
  const input = { ...emptySupportDraft(), subject: '真实问题', body: '请求核对' }
  assert.equal(supportCreatePayload(input).runId, null)
  assert.equal(supportCreatePayload(input).drawingConsent, false)
  assert.throws(() => supportCreatePayload({ ...input, drawingConsent: true }), /关联/)
  assert.throws(() => supportCreatePayload({ ...input, drawingConsent: 'true' }))
  assert.throws(() => supportCreatePayload({ ...input, body: 'x'.repeat(5001) }))
  assert.throws(() => supportMessagePayload('  '))
  assert.equal(supportCreatePayload({ ...input, runId: 'cad_saved', drawingConsent: true }).drawingConsent, true)
})

test('closed tickets can revoke consent but customers cannot perform administrator transitions', () => {
  const ticket = { revision: 4, status: 'closed', runId: 'cad-own' }
  assert.deepEqual(supportUpdatePayload(ticket, { drawingConsent: false }), { revision: 4, drawingConsent: false })
  assert.throws(() => supportUpdatePayload(ticket, { status: 'in_progress' }))
  assert.deepEqual(supportTransitions('closed'), ['in_progress'])
  assert.deepEqual(supportUpdatePayload(ticket, { status: 'in_progress', reason: '重新核查' }, true), { revision: 4, status: 'in_progress', reason: '重新核查' })
  assert.throws(() => supportUpdatePayload(ticket, { status: 'resolved', reason: '不能直接跳过' }, true))
})

test('token refresh keeps message intent while switching accounts remounts state', () => {
  assert.equal(supportSessionKey({ user: { id: 'A' }, access_token: 'old' }), supportSessionKey({ user: { id: 'A' }, access_token: 'new' }))
  assert.notEqual(supportSessionKey({ user: { id: 'A' } }), supportSessionKey({ user: { id: 'B' } }))
  let count = 0; const keys = createSupportIntentKeys(() => `key-${++count}`); const payload = { body: '同一说明' }
  const key = keys.get('reply:ticket', payload)
  assert.equal(keys.get('reply:ticket', { ...payload }), key)
  assert.notEqual(keys.get('reply:other', payload), key)
  assert.notEqual(keys.get('reply:ticket', { body: '另一条说明' }), key)
  keys.complete('reply:ticket', payload)
  assert.notEqual(keys.get('reply:ticket', payload), key)
})

test('double click shares one mutation and failed submission retains its retry key', async () => {
  const gate = createBillingActionGate(), keys = createSupportIntentKeys(() => 'stable-request'); let count = 0
  const body = { body: '补充说明' }, key = keys.get('reply', body)
  const first = gate.run('support-write', async () => { count++; throw new Error('network failure') })
  const second = gate.run('support-write', async () => { count++ })
  assert.equal(first, second); await assert.rejects(first)
  assert.equal(count, 1); assert.equal(keys.get('reply', body), key)
})

test('ticket and message pages clamp correctly after server totals change', () => {
  assert.equal(supportPageOffset(21, 40, 20), 20)
  assert.equal(supportPageOffset(0, 20, 20), 0)
  assert.equal(supportPageOffset(101, 150, 50), 100)
})

test('client uses own/admin paths, bearer header and exact server-owned IDs without download access', async () => {
  const calls = []
  const client = createSupportClient({ apiBase: 'https://cad.example/api/v1', fetchImpl: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({}) } } })
  await client.list('session', { status: 'open', offset: 20 })
  await client.create('session', { ...emptySupportDraft(), subject: '问题', body: '说明' }, 'create')
  await client.append('rotated', 'SUP/test', { body: '回复' }, 'message', { admin: true })
  await client.update('session', 'SUP/test', { revision: 1, drawingConsent: false }, 'revoke')
  await client.audit('admin', 'SUP/test', { offset: 20 })
  assert.match(calls[0].url, /support\/tickets\?limit=20&offset=20&status=open$/)
  assert.equal(calls[0].options.credentials, 'include')
  assert.equal(calls[2].options.headers.Authorization, 'Bearer rotated')
  assert.match(calls[2].url, /support\/admin\/tickets\/SUP%2Ftest\/messages$/)
  assert.deepEqual(JSON.parse(calls[3].options.body), { revision: 1, drawingConsent: false, idempotencyKey: 'revoke' })
  assert.doesNotMatch(calls[1].options.body, /session|ownerId/)
  assert.match(calls[4].url, /audit\?limit=20&offset=20$/)
  await assert.rejects(client.list(''), error => error.status === 401)
})

test('revision errors and network timeouts remain visible rather than successful replies', async () => {
  const conflict = createSupportClient({ fetchImpl: async () => ({ ok: false, status: 409, json: async () => ({ detail: '工单已有新内容' }) }) })
  await assert.rejects(conflict.update('token', 'id', {}, 'key'), error => error.status === 409)
  const timed = createSupportClient({ timeoutMs: 1, fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))) })
  await assert.rejects(timed.ticket('token', 'id'), /请求超时/)
})

 test('workdesk queue filters, assignments and notes carry explicit versions', async () => {
  const { supportViewFilters, supportAssignmentPayload, supportNotePayload } = await import('./supportClient.js')
  assert.deepEqual(supportViewFilters('pending'), { status: 'pending', assignedTo: '' })
  assert.deepEqual(supportViewFilters('mine'), { status: 'pending', assignedTo: 'me' })
  assert.deepEqual(supportViewFilters('unassigned'), { status: 'pending', assignedTo: 'unassigned' })
  const ticket = { revision: 7 }
  assert.deepEqual(supportAssignmentPayload(ticket, { assignedTo: '', priority: 'high', reason: '  需要重新分配  ' }), { revision: 7, assignedTo: null, priority: 'high', reason: '需要重新分配' })
  assert.throws(() => supportAssignmentPayload(ticket, { priority: 'invalid', reason: '说明' }))
  assert.throws(() => supportAssignmentPayload(ticket, { priority: 'normal', reason: '' }))
  assert.throws(() => supportNotePayload({}, '备注'), /版本/)
  assert.deepEqual(supportNotePayload(ticket, '  内部记录  '), { body: '内部记录', revision: 7 })
})

test('support client uses private endpoints for notes, and never sends admin filters to customer routes', async () => {
  const calls = []
  const client = createSupportClient({ apiBase: 'https://cad.example/api/v1', fetchImpl: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({}) } } })
  await client.list('admin', { admin: true, status: 'pending', assignedTo: 'me', priority: 'urgent', q: '孔 A_1%' })
  await client.list('customer', { assignedTo: 'me', priority: 'urgent', q: 'internal' })
  await client.assignees('admin')
  await client.notes('admin', 'SUP/test', { offset: 20 })
  await client.appendNote('admin', 'SUP/test', { revision: 3, body: '仅内部' }, 'stable-key')
  const url = new URL(calls[0].url)
  assert.equal(url.searchParams.get('q'), '孔 A_1%')
  assert.equal(url.searchParams.get('assignedTo'), 'me')
  assert.equal(url.searchParams.get('priority'), 'urgent')
  assert.doesNotMatch(calls[1].url, /assignedTo|priority|internal/)
  assert.match(calls[2].url, /support\/admin\/assignees$/)
  assert.match(calls[3].url, /admin\/tickets\/SUP%2Ftest\/notes\?limit=20&offset=20$/)
  assert.equal(calls[4].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[4].options.body), { revision: 3, body: '仅内部', idempotencyKey: 'stable-key' })
})

test('reply templates append to a draft without posting or silently discarding existing input', async () => {
  const { supportReplyDraft } = await import('./supportClient.js')
  const original = '已检查部分参数。'
  assert.match(supportReplyDraft(original, 'dimension'), /^已检查部分参数。\n\n请指出/)
  assert.equal(supportReplyDraft(original, 'missing-template'), original)
  assert.throws(() => supportReplyDraft('字'.repeat(4999), 'billing'), /超过/)
})

test('support deep links normalize all admin filters deterministically', async () => {
  const { supportQueryState } = await import('./supportClient.js')
  const filters = supportQueryState({ q: ' 尺寸核对 ', status: 'resolved', assignedTo: 'me', priority: 'urgent', offset: '40' })
  assert.deepEqual(filters, { q: '尺寸核对', status: 'resolved', assignedTo: 'me', priority: 'urgent', offset: 40 })
  assert.equal(filters.status, 'resolved')
  assert.equal(supportQueryState({ status: 'pending' }).status, 'pending')
  assert.deepEqual(supportQueryState({ q: [], status: 'invalid', assignedTo: '../../x', priority: 'invalid', offset: 'Infinity' }), { q: '', status: '', assignedTo: '', priority: '', offset: 0 })
  assert.equal(supportQueryState({ offset: '21' }).offset, 20)
  assert.equal(supportQueryState({ offset: '1000001' }).offset, 0)
  assert.equal(JSON.stringify(supportQueryState({ q: 'x', status: 'open' })), JSON.stringify(supportQueryState({ status: 'open', q: 'x' })))
})
