import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let server, Content
before(async () => {
  server = await createServer({ configFile: false, server: { middlewareMode: true, hmr: false, watch: null }, appType: 'custom' })
  Content = (await server.ssrLoadModule('/src/AdminOperationsWorkspace.jsx')).AdminOperationsContent
})
after(async () => { await server?.close() })
const admin = { id: 'admin-qa', active: true, roles: ['admin'], permissions: ['*'] }
const user = role => ({ id: `${role}-qa`, active: true, roles: [role] })
const customer = { id: 'customer-qa', displayName: '合同测试客户', email: 'contract@example.test', active: true, roles: ['designer'], taskCount: 2, openTicketCount: 0, createdAt: '2026-09-10T01:00:00Z' }
const task = { runId: 'cad-contract', jobId: 'job-contract', ownerId: customer.id, customer, status: 'queued', revision: 1, canCancel: true, stage: 'queued', elapsedSeconds: null, createdAt: '2026-09-10T01:00:00Z' }
const unknownUsage = { calls: null, inputTokens: null, cachedInputTokens: null, outputTokens: null, reasoningOutputTokens: null, costMicroUsd: null, unknownUsageCalls: null, unknownCostCalls: null }
const render = (section, data, params = {}, actor = admin) => renderToStaticMarkup(React.createElement(Content, { section, data, params, user: actor }))

test('the same task projection distinguishes missing historical usage from known zero calls', () => {
  const detail = { task, attempts: [task], usage: unknownUsage, diagnostics: { phase: 'source_reading', traceEvents: null }, financialVisible: true, settlements: [] }
  const historical = render('tasks', detail, { runId: task.runId })
  assert.match(historical, /调用次数<\/dt><dd>—<\/dd>/)
  assert.match(historical, /输入 Token<\/dt><dd>—<\/dd>/)
  assert.match(historical, /未取得完整成本/)
  assert.match(historical, /读取图纸/)
  assert.doesNotMatch(historical, /Invalid Date|NaN/)
  const known = render('tasks', { ...detail, usage: { ...unknownUsage, calls: 0, inputTokens: 0, outputTokens: 0 } }, { runId: task.runId })
  assert.match(known, /调用次数<\/dt><dd>0<\/dd>/)
  assert.match(known, /输入 Token<\/dt><dd>0<\/dd>/)
})

test('legacy provider attempt counts never become measured calls or inferred retries', () => {
  const data = { task, attempts: [task], usage: unknownUsage, diagnostics: { provider: 'codex', providerAttempts: 13, retryCount: null, traceEvents: 5 }, financialVisible: false }
  const html = render('tasks', data, { runId: task.runId })
  assert.match(html, /供应商请求次数<\/dt><dd>13<\/dd>/)
  assert.match(html, /自动重试次数<\/dt><dd>—<\/dd>/)
  assert.match(html, /调用次数<\/dt><dd>—<\/dd>/)
  assert.doesNotMatch(html, /自动重试次数<\/dt><dd>12/)
  const explicit = render('tasks', { ...data, diagnostics: { ...data.diagnostics, retryCount: 3 } }, { runId: task.runId })
  assert.match(explicit, /自动重试次数<\/dt><dd>3<\/dd>/)
})

test('safe customer response rendering hides financial records without billing permission', () => {
  const data = { customer, tasks: [task], tickets: [{ id: 'support-1', status: 'waiting_customer' }], financialVisible: true,
    financial: { wallet: { balanceUnits: 54321, dueUnits: 123 }, orders: [{ id: 'private-order-marker', amountFen: 12345, status: 'refund_abnormal' }], ledger: [{ id: 'ledger-1', kind: 'purchase', deltaUnits: 54321 }], usage: { ...unknownUsage, scope: 'recorded_calls', historicalTasksWithoutJournal: 2 } },
    sourceFiles: [{ url: '/private/customer-original.dwg?access=private-capability-marker' }], rawError: 'secret-source-code-marker' }
  const finance = render('customers', data, { customerId: customer.id }, user('finance'))
  assert.match(finance, /54,321/); assert.match(finance, /退款异常，待核查/)
  assert.match(finance, /旧任务的用量未完整记录/); assert.match(finance, /当前汇总仅包含已有调用记录/)
  const support = render('customers', data, { customerId: customer.id }, user('support'))
  assert.match(support, /等待客户补充/)
  assert.doesNotMatch(support, /private-order-marker|54,321|积分余额|已知成本/)
  for (const html of [finance, support]) assert.doesNotMatch(html, /private-capability-marker|secret-source-code-marker|customer-original\.dwg/)
})

test('stop action is available only to operators with task-management permission and an active cancellable task', () => {
  const detail = { task, attempts: [], usage: unknownUsage, diagnostics: {}, financialVisible: false, settlements: null }
  assert.match(render('tasks', detail, { runId: task.runId }, user('ops')), /停止任务<\/button>/)
  assert.doesNotMatch(render('tasks', detail, { runId: task.runId }, user('support')), /停止任务<\/button>/)
  assert.doesNotMatch(render('tasks', detail, { runId: task.runId }, user('auditor')), /停止任务<\/button>/)
  const waiting = render('tasks', { ...detail, task: { ...task, status: 'cancel_requested' } }, { runId: task.runId }, user('ops'))
  assert.match(waiting, /任务尚未确认结束/)
  assert.doesNotMatch(waiting, /停止任务<\/button>/)
  assert.match(render('tasks', detail, {}, user('finance')), /没有查看此页面的权限/)
})

test('system displays actual runtime metadata and marks unconfigured probes honestly', () => {
  const data = { queue: { available: true, queued: 2, running: 1, cancelRequested: 0, runningOperations: 3, capacity: { maxConcurrent: 4, maxQueued: 20, maxPerOwner: 1 } },
    configuration: { provider: 'codex', model: 'gpt-6-astra', reasoningEffort: 'high', externalProbe: false }, cadKernel: { available: true, engine: 'cadquery-occt', version: 'fixture-version' },
    databases: { platformReadable: true, cadReadable: true }, disk: { available: true, freeBytes: 5 * 1024 ** 3, totalBytes: 20 * 1024 ** 3, message: '只表示工作区文件系统。' },
    billing: { available: true, chargingEnabled: null }, support: { available: true, open: 0 }, alerts: [],
    monitoring: { available: false, message: '尚未配置外部持续监控。' }, externalNotifications: { available: false, message: '尚未配置外部告警通知。' }, backup: { available: false, message: '未配置自动异地备份监控。' } }
  const html = render('system', data)
  for (const text of ['gpt-6-astra', 'cadquery-occt', 'fixture-version', '5.00 GB', '20.00 GB', '只表示工作区文件系统', '并行辅助操作', '未取得启用状态', '未配置自动异地备份监控']) assert.ok(html.includes(text), text)
  assert.match(html, /推理强度<\/dt><dd>高<\/dd>/)
  assert.doesNotMatch(html, /计费关闭|监控正常|备份成功/)
})

test('all empty list contracts render a real empty state with correct pagination', () => {
  const page = { items: [], total: 0, limit: 20, offset: 0, financialVisible: false }
  for (const section of ['customers', 'tasks', 'audit']) {
    const html = render(section, page)
    assert.match(html, /没有符合条件/)
    assert.match(html, /共 0 条/)
    assert.match(html, /第 1 \/ 1 页/)
  }
})

test('overview exposes actual trend semantics and never promotes review-required to success', () => {
  const data = { customers: { total: 8, active: 8, inactive: 0, businessCustomers: 5, staffAccounts: 3 }, tasks: { total: 3, running: 0, queued: 0, byStatus: {} }, support: { available: true, open: 1, waitingCustomer: 2, resolved: 4 }, alerts: [],
    trend: { days: 7, points: [{ date: '2026-09-09', tasks: 2, success: 0, reviewRequired: 2, failed: 0, usage: { calls: null } }, { date: '2026-09-10', tasks: 1, success: 1, reviewRequired: 0, failed: 0, usage: { calls: 1 } }], undatedTasks: 2 } }
  const html = render('overview', data, { days: 7 })
  for (const value of ['全部账号','业务客户','后台成员','任务与调用趋势','近 7 日','近 30 日','近 90 日','仅 ready 状态','待确认、待补充均不计为完成','2 条历史任务缺少日期','svg']) assert.ok(html.includes(value), value)
  assert.doesNotMatch(html, /NaN|Infinity|制造成功/)
  assert.match(html, /已记录调用<\/span><strong>—<\/strong>/)
})

test('detail tabs expose real CRM and followup records while hiding write forms without capability', () => {
  const data = { customer: { ...customer, crm: { company: '实际客户公司', contactName: '张工', phone: '123', tags: ['待跟进'], internalNote: '内部档案说明', version: 0 } }, tasks: [], tickets: [], financialVisible: false,
    followups: { items: [{ id: 'followup-real', content: '真实人工跟进记录 <script>不会执行</script>', createdBy: 'ops-real', createdAt: '2026-09-10T01:00:00Z' }], total: 1, limit: 20, offset: 0 } }
  const html = render('customers', data, { customerId: customer.id }, user('auditor'))
  for (const value of ['role="tablist"', 'role="tabpanel"', '客户档案', '跟进记录', '实际客户公司', 'followup-real', 'ops-real']) assert.ok(html.includes(value), value)
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>|保存客户档案|保存记录<\/button>/)
})

test('manual task state stays separate from failed execution state and warnings remain visible', () => {
  const data = { task: { ...task, status: 'failed', canCancel: false }, diagnostics: { errorCode: 'timeout', errorCategory: 'timeout' }, usage: unknownUsage, financialVisible: false,
    handling: { status: 'resolved', priority: 'urgent', version: 2 }, notes: { items: [{ id: 'note-real', content: '客户已知悉本次失败', createdBy: 'ops-real' }], total: 1, limit: 20, offset: 0 } }
  const html = render('tasks', data, { runId: task.runId }, user('auditor'))
  for (const value of ['人工处理安排','处理备注','失败','已处理','紧急','不改变 AI 队列顺序','不会把失败模型改为完成','客户已知悉本次失败','人工排查建议']) assert.ok(html.includes(value), value)
  assert.doesNotMatch(html, /保存处理安排|停止任务<\/button>/)
})

test('write controls require the new explicit capabilities and known versions', () => {
  const actor = { ...user('support'), permissions: ['admin:customers','admin:tasks','admin:customer-manage','admin:task-followup'] }
  const context = { scope: 'support-qa:customer-qa', token: () => 'fixture-only', onSaved() {} }
  const html = renderToStaticMarkup(React.createElement(Content, { section: 'customers', params: { customerId: customer.id }, data: { customer, tasks: [], tickets: [], crm: { version: 0, company: '', contactName: '', phone: '', tags: [], internalNote: '' }, followups: { items: [], total: 0, limit: 20, offset: 0 } }, user: actor, context }))
  assert.match(html, /保存客户档案/); assert.match(html, /保存记录/); assert.doesNotMatch(html, /expectedVersion/)
})

test('customer shortcuts link to account and owner-filtered financial work only when authorized', () => {
  const data = { customer, tasks: [], tickets: [], financialVisible: true, financial: { wallet: {}, orders: [], ledger: [], usage: unknownUsage } }
  const adminHtml = render('customers', data, { customerId: customer.id })
  for (const label of ['管理此账号','此客户全部订单','此客户全部流水']) assert.ok(adminHtml.includes(label), label)
  const financeHtml = render('customers', data, { customerId: customer.id }, user('finance'))
  assert.match(financeHtml, /此客户全部订单/); assert.doesNotMatch(financeHtml, /管理此账号/)
  const supportHtml = render('customers', data, { customerId: customer.id }, user('support'))
  assert.doesNotMatch(supportHtml, /管理此账号|此客户全部订单|此客户全部流水/)
})
