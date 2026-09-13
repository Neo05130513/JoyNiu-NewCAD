import test from 'node:test'
import assert from 'node:assert/strict'
import { adminReadPath, adminListParams, adminTrendDays, adminTrendPoints, adminTrendGeometry, adminCrmPayload, adminHandlingPayload, adminEntryPayload, createAdminMutationIdentity, adminFailureAdvice, adminCsvCell, adminPageCsv, adminDiagnosticExport, adminOperations } from './adminOperationsClient.js'

const base = { expectedVersion: 0, idempotencyKey: 'fixture-key', reason: '已经核对客户资料' }
const crm = { company: ' A公司 ', contactName: '王工', phone: '+86 123', tags: '长期客户，待确认,长期客户', internalNote: '内部核对信息', ...base }
test('trend ranges and manual filters are explicit server-side queries', () => {
  for (const days of [7, 30, 90]) assert.equal(adminReadPath('overview', { days }), `/overview?days=${days}`)
  assert.equal(adminTrendDays(365), 30)
  assert.equal(adminReadPath('overview'), '/overview')
  assert.deepEqual(adminListParams('tasks', { priority: 'urgent', handlingStatus: 'in_progress', offset: 20, status: 'failed', ownerId: 'u-1' }), { limit: 20, offset: 20, status: 'failed', handlingStatus: 'in_progress', priority: 'urgent', ownerId: 'u-1' })
  assert.equal(adminListParams('tasks', { priority: 'arbitrary', handlingStatus: '__proto__' }).priority, undefined)
})
test('trend charts preserve real zero, missing values and missing dates without inventing points', () => {
  const points = adminTrendPoints({ points: [{ date: '2026-09-01', tasks: 0, success: 0, reviewRequired: 2, needsInput: 1, failed: null, usage: { calls: null } }, { date: '2026-09-02', tasks: null }, { date: '2026-09-04', tasks: 4 }, { date: '2026-09-05', tasks: 2 }, { date: '2026-02-31', tasks: 4 }, { date: 'invalid', tasks: 5 }, { date: '2026-09-04', tasks: 99 }] })
  assert.equal(points.length, 4); assert.equal(points[0].tasks, 0); assert.equal(points[0].calls, null)
  assert.equal(points[0].reviewRequired, 2); assert.equal(points[0].success, 0)
  const chart = adminTrendGeometry(points, 'tasks')
  assert.equal(chart.max, 4); assert.equal(chart.coordinates[1], null); assert.equal(chart.paths.length, 2)
  assert.ok(chart.coordinates[2].x > 600, 'x position uses elapsed calendar days')
  assert.equal(adminTrendGeometry([points[0], points[2]], 'tasks').paths.length, 2, 'a missing date breaks the line')
  assert.ok(chart.paths.every(path => !/NaN|Infinity/.test(path)))
})
test('CRM and handling mutations match versioned contracts and never submit task execution state', () => {
  assert.deepEqual(adminCrmPayload(crm), { company: 'A公司', contactName: '王工', phone: '+86 123', tags: ['长期客户', '待确认'], internalNote: '内部核对信息', ...base })
  assert.deepEqual(adminHandlingPayload({ status: 'resolved', priority: 'urgent', ...base, ownerId: 'forged', aiStatus: 'ready' }), { status: 'resolved', priority: 'urgent', ...base })
  assert.throws(() => adminHandlingPayload({ status: 'ready', priority: 'urgent', ...base }))
  for (const expectedVersion of [null, undefined, -1, '0', 0.2]) assert.throws(() => adminCrmPayload({ ...crm, expectedVersion }))
  assert.throws(() => adminCrmPayload({ ...crm, tags: Array.from({ length: 13 }, (_, i) => `标签${i}`) }))
  assert.throws(() => adminCrmPayload({ ...crm, tags: ['x'.repeat(25)] }))
  assert.throws(() => adminCrmPayload({ ...crm, company: 'x'.repeat(121) }))
  assert.throws(() => adminCrmPayload({ ...crm, reason: '短' }))
  assert.deepEqual(adminEntryPayload('  已核对客户描述  ', 'entry-1'), { content: '已核对客户描述', idempotencyKey: 'entry-1' })
  for (const text of ['', 'x'.repeat(2001)]) assert.throws(() => adminEntryPayload(text, 'entry-1'))
})
test('an uncertain mutation retry preserves its key independently of token refresh', () => {
  let count = 0; const identity = createAdminMutationIdentity(() => `id-${++count}`)
  const body = { status: 'pending', reason: '同一条操作说明', expectedVersion: 3 }
  assert.equal(identity.forPayload(body), 'id-1')
  assert.equal(identity.forPayload({ ...body }), 'id-1')
  const newAccessToken = 'renewed-session'; assert.ok(newAccessToken)
  assert.equal(identity.forPayload(body), 'id-1', 'session token is never part of operation identity')
  assert.equal(identity.forPayload({ ...body, reason: '修改后的另一说明' }), 'id-2')
  identity.reset(); assert.equal(identity.forPayload(body), 'id-3')
})
test('CSV protects formulas and exports only the current whitelisted visible page', () => {
  for (const value of ['=CMD()', '+cmd', '-1+2', '@SUM(A1)', '  =1+2', '\t=1', '\r+CMD', '\n@SUM(1)', '\ufeff=1']) assert.ok(adminCsvCell(value).startsWith('"\''), value)
  assert.equal(adminCsvCell('a,"b"\nc'), '"a,""b""\nc"')
  const page = { items: [{ id: 'u-1', email: '=HYPERLINK("evil")', active: true, displayName: '客户', crm: { company: '公司', tags: ['待跟进'], internalNote: 'DO_NOT_EXPORT_NOTE' }, taskCount: 1, financial: { balanceUnits: 98765, dueUnits: 0 }, password: 'DO_NOT_EXPORT_PASSWORD' }], total: 70, limit: 20, offset: 20, financialVisible: true }
  const result = adminPageCsv('customers', page, { canFinance: false, filter: 'q=客户', readAt: '2026-09-10T01:00:00Z' })
  assert.match(result, /第 21 至 21 条，共 70 条/); assert.match(result, /当前筛选/); assert.match(result, /'=/)
  assert.doesNotMatch(result, /DO_NOT_EXPORT|98765|积分余额/)
  assert.match(adminPageCsv('customers', page, { canFinance: true }), /98765/)
  assert.doesNotMatch(adminPageCsv('customers', { ...page, financialVisible: false }, { canFinance: true }), /98765/)
  assert.throws(() => adminPageCsv('unknown', page))
})
test('diagnostic JSON excludes originals, credentials, notes and unauthorized financial fields', () => {
  const data = { task: { runId: 'cad-1', status: 'failed', sourceFiles: [{ url: 'SECRET_LINK' }], access_token: 'SECRET_TOKEN' }, diagnostics: { phase: 'planning', errorCode: 'timeout', model: 'gpt-6-astra', requestBody: 'SECRET_PROMPT' }, usage: { calls: null, inputTokens: null, costMicroUsd: 123, unknownCostCalls: 1 }, handling: { status: 'pending', priority: 'high', version: 1, reason: 'SECRET_REASON' }, financialVisible: true, notes: ['SECRET_NOTE'] }
  const json = adminDiagnosticExport(data)
  assert.doesNotMatch(json, /SECRET_|costMicroUsd|unknownCostCalls/)
  assert.equal(JSON.parse(json).usage.calls, null)
  assert.equal(JSON.parse(adminDiagnosticExport(data, { canFinance: true })).usage.costMicroUsd, 123)
})
test('failure advice is Chinese, constrained and never calls a service', () => {
  assert.match(adminFailureAdvice('http_429', 'failed').title, /限制/)
  assert.match(adminFailureAdvice('timeout', 'failed').steps.join(''), /重复/)
  assert.match(adminFailureAdvice(null, 'cancel_requested').steps.join(''), /不要.*已取消/)
  assert.doesNotMatch(JSON.stringify(adminFailureAdvice('PRIVATE_RAW_ERROR', 'failed')), /PRIVATE_RAW_ERROR/)
})
test('CRM and note helpers use actual endpoints and the fresh token getter', async () => {
  const original = globalThis.fetch, calls = []; let token = 'initial'
  globalThis.fetch = async (url, options) => { calls.push({ url, options }); return Response.json({ crm: { version: 1 }, entry: { id: 'note-1' }, items: [], total: 0, limit: 10, offset: 0 }) }
  try {
    await adminOperations.crm(() => token, 'u/1', crm)
    token = 'renewed'
    await adminOperations.append(() => token, 'tasks', 'cad-1', { content: '真实人工说明', idempotencyKey: 'note-1' })
    await adminOperations.entries(() => token, 'customers', 'u-1', 10)
    assert.match(calls[0].url, /\/customers\/u%2F1\/crm$/); assert.equal(calls[0].options.method, 'PUT')
    assert.equal(calls[1].options.headers.Authorization, 'Bearer renewed'); assert.match(calls[1].url, /\/tasks\/cad-1\/notes$/)
    assert.match(calls[2].url, /followups\?limit=10&offset=10$/)
    assert.deepEqual(JSON.parse(calls[1].options.body), { content: '真实人工说明', idempotencyKey: 'note-1' })
    await assert.rejects(adminOperations.entries(() => token, 'billing', 'x'), /记录类型/)
  } finally { globalThis.fetch = original }
})

test('synchronous repeated saves are blocked and late actions cannot overwrite another record', async () => {
  const { createAdminActionGate } = await import('./adminOperationsClient.js')
  const gate = createAdminActionGate(), first = gate.begin('account-a:customer-1')
  assert.equal(gate.begin('account-a:customer-1'), null, 'double click and token renewal do not reserve a second action')
  const next = gate.begin('account-b:task-2')
  assert.equal(first.signal.aborted, true); assert.equal(first.isCurrent('account-b:task-2'), false)
  first.finish(); assert.equal(next.isCurrent('account-b:task-2'), true)
  assert.equal(gate.begin('account-b:task-2'), null)
  next.finish(); assert.ok(gate.begin('account-b:task-2'))
  gate.abort()
})

test('diagnostic exports cannot recursively include credentials through malformed known fields', () => {
  const result = adminDiagnosticExport({ task: { runId: 'cad-1', status: { access_token: 'SECRET_TOKEN' }, stage: 'http://private/link?access=SECRET' }, diagnostics: { model: { request: 'SECRET_PROMPT' }, errorCode: 'contains raw private message' }, usage: { calls: null } })
  assert.doesNotMatch(result, /SECRET|private message|http:\/\//)
  assert.equal(JSON.parse(result).task.status, null)
})
