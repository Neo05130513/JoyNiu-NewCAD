import { useEffect, useRef, useState } from 'react'
import { hasAdminPermission } from './adminPermissions.js'
import { createClientId } from './clientId.js'
import { adminOperations, adminReadPath, adminListParams, adminSectionNames, adminTaskStatusNames, adminRoleNames, adminTicketStatusNames, adminOrderStatusNames,
  adminDate, adminNumber, adminMoney, adminElapsed, adminPageOffset, adminStopPayload, adminStopOutcome, createAdminRequestGate } from './adminOperationsClient.js'
import { DetailTabs, OperationsTrend, CustomerCrm, TaskHandling, OperationsNotes, FailureAdvice } from './AdminOperationsEnhancements.jsx'
import { adminHandlingNames, adminPriorityNames, adminPageCsv, adminDiagnosticExport, downloadAdminText } from './adminOperationsClient.js'
import './admin-operations.css'

const sectionPermission = { overview: 'admin:overview', customers: 'admin:customers', tasks: 'admin:tasks', system: 'admin:system', audit: 'admin:audit' }
const nameOf = customer => customer?.displayName || customer?.email || '未命名账号'
const list = value => Array.isArray(value) ? value : []
const stages = { agent_working: '模型分析中', plan_saved: '草稿已保存', provider_error: '模型请求异常', read_source: '读取原图', interpret_source_spatial: '解释图纸空间关系', observe_source: '核对原图依据', geometry_repair: '修正几何', inspect_source: '核对原图局部', record_observations: '记录尺寸依据', observations_recorded: '尺寸依据已记录', observations_rejected: '尺寸依据需修正', edit_plan: '编辑模型计划', inspect_draft: '检查草稿', projection_compare: '比对实体投影', independent_drawing_review: '独立图纸复核', ask_user: '等待客户补充', finish: '整理本轮结果', source_transcription_reused: '复用原图转录', source_spatial_interpretation: '核对空间关系', source_spatial_reused: '复用空间关系', source_spatial_complete: '空间关系核对完成', source_spatial_unavailable: '空间关系待核对', queued: '等待调度', started: '开始处理', prepare: '准备任务', preparing: '准备任务', preprocessing: '图纸预处理', source_preprocessing: '图纸预处理', read: '读取图纸', reading: '读取图纸', source_reader: '读取图纸', source_reading: '读取图纸', source_transcription: '转录图纸标注', source_spatial: '分析空间关系', source_question_review: '复读原图', source_question_resolved: '核对补充信息', source_locations: '定位尺寸', analysis: '图纸分析', modeling: '实体建模', build: '构建实体', building: '构建实体', execute: '执行建模', execute_plan: '执行建模', inspect: '检查实体', inspect_geometry: '检查几何', checking: '检查实体', review: '图纸复核', drawing_review: '图纸复核', human_confirmation: '等待人工确认', provider_retry: '重试模型调用', retry: '重试处理', queued_for_resource: '等待资源', thinking: '分析建模方案', exporting: '准备交付', finalize: '整理结果', completed: '处理结束', failed: '任务失败', planning: '制定建模计划', running: '处理中', ready: '处理完成', cancel_requested: '等待停止', cancelled: '已取消', interrupted: '处理已中断' }
const errorCategories = { timeout: '响应超时', cancelled: '用户或管理员取消', access: '认证或权限问题', provider: '模型服务问题', geometry: '几何或建模检查问题', unknown: '未分类错误' }
const ledgerKinds = { recharge: '充值到账', purchase: '充值到账', payment: '充值到账', consumption: '建模扣费', usage: '建模扣费', settlement: '任务结算', adjustment: '人工调整', admin_adjustment: '人工调整', refund: '退款回收', refund_recovery: '退款回收', refund_return: '退款关闭，归还积分', due_payment: '补缴已完成任务', debt_payment: '欠缴抵扣' }
const auditSources = { operations: '运营操作', accounts: '账号安全', billing: '账单与积分', support: '客户服务' }
const auditActions = { 'customer.crm.updated': '更新客户档案', 'customer.note.added': '追加客户跟进', 'task.handling.updated': '调整人工处理安排', 'task.note.added': '追加任务备注', 'customer.detail.read': '查看客户详情', 'task.detail.read': '查看任务诊断', task_cancel_requested: '请求停止任务', 'task.cancel': '停止任务', 'task.cancel_requested': '请求停止任务', user_created: '创建账号', user_disabled: '停用账号', user_enabled: '启用账号', roles_changed: '变更角色', password_reset: '重置密码', login: '登录', logout: '退出登录', login_failed: '登录失败' }
function Badge({ value, labels = adminTaskStatusNames }) { return <span className={`ao-badge ao-${['failed', 'interrupted', 'critical', 'cancelled'].includes(value) ? 'danger' : ['ready', 'paid', 'active', 'resolved', 'completed'].includes(value) ? 'success' : 'neutral'}`}>{Object.hasOwn(labels, value) ? labels[value] : '未分类'}</span> }
function Empty({ children = '暂无记录' }) { return <p className="ao-empty">{children}</p> }
function Info({ rows }) { return <dl className="ao-info">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? '—'}</dd></div>)}</dl> }
function Panel({ title, note, children, action }) { return <section className="ao-panel"><header><div><h2>{title}</h2>{note && <p>{note}</p>}</div>{action}</header>{children}</section> }
function Table({ columns, items, empty = '暂无记录', rowKey = item => item.id || item.runId || item.attemptId }) {
  if (!items.length) return <Empty>{empty}</Empty>
  return <div className="ao-table-scroll" tabIndex={0} aria-label="数据表格，可横向滚动"><table><thead><tr>{columns.map(column => <th key={column.title} scope="col">{column.title}</th>)}</tr></thead><tbody>{items.map((item, index) => <tr key={rowKey(item) || index}>{columns.map(column => <td key={column.title}>{column.render(item)}</td>)}</tr>)}</tbody></table></div>
}
function CustomerIdentity({ customer, onNavigate, canOpen }) {
  return <div className="ao-identity">{canOpen && customer?.id ? <button className="ao-link" onClick={() => onNavigate('customers', { customerId: customer.id })}>{nameOf(customer)}</button> : <strong>{nameOf(customer)}</strong>}<span>{customer?.email || '未记录邮箱'}</span></div>
}
function Metrics({ items }) { return <div className="ao-metrics">{items.map(([label, value, hint]) => <article key={label}><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</article>)}</div> }
function Alerts({ alerts }) { return list(alerts).length ? <ul className="ao-alerts">{alerts.map((alert, index) => <li key={alert.code || index} className={`ao-alert-${alert.severity}`}><span className="ao-alert-dot" /><div><strong>{alert.message || '存在待关注事项'}</strong>{typeof alert.count === 'number' && <span>{adminNumber(alert.count)} 项</span>}</div></li>)}</ul> : <Empty>当前接口未报告告警。</Empty> }
function Pager({ data, loading, params, onChange }) {
  return <nav className="ao-pager" aria-label="数据分页"><span>共 {adminNumber(data.total)} 条 · 第 {Math.floor(data.offset / data.limit) + 1} / {Math.max(1, Math.ceil(data.total / data.limit))} 页</span><div><button disabled={loading || data.offset === 0} onClick={() => onChange({ ...params, offset: Math.max(0, data.offset - data.limit) })}>上一页</button><button disabled={loading || data.offset + data.limit >= data.total} onClick={() => onChange({ ...params, offset: data.offset + data.limit })}>下一页</button></div></nav>
}

function Filters({ section, params, onChange, canFinance }) {
  const [draft, setDraft] = useState(() => ({ ...adminListParams(section, params) }))
  const set = field => event => setDraft(current => ({ ...current, [field]: event.target.value }))
  return <form className="ao-filters" onSubmit={event => { event.preventDefault(); onChange({ ...draft, offset: 0 }) }}>
    {section !== 'audit' && <label className="ao-search">{section === 'customers' ? '查找客户' : '查找任务'}<input type="search" maxLength={128} placeholder={section === 'customers' ? '名称、邮箱、公司、联系人或标签' : '任务编号、客户名称或邮箱'} value={draft.q || ''} onChange={set('q')} /></label>}
    {section === 'customers' && <label>账号状态<select value={draft.active || ''} onChange={set('active')}><option value="">全部状态</option><option value="true">已启用</option><option value="false">已停用</option></select></label>}
    {section === 'tasks' && <><label>任务状态<select value={draft.status || ''} onChange={set('status')}><option value="">全部状态</option>{Object.entries(adminTaskStatusNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>人工处理<select value={draft.handlingStatus || ''} onChange={set('handlingStatus')}><option value="">全部人工状态</option>{Object.entries(adminHandlingNames).map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>运营优先级<select value={draft.priority || ''} onChange={set('priority')}><option value="">全部优先级</option>{Object.entries(adminPriorityNames).map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select></label><label>所属客户 ID<input maxLength={160} placeholder="全部客户" value={draft.ownerId || ''} onChange={set('ownerId')} /></label></>}
    {section === 'audit' && <><label>审计来源<select value={draft.source || ''} onChange={set('source')}><option value="">全部有权访问的来源</option>{Object.entries(auditSources).filter(([id]) => id !== 'billing' || canFinance).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><label className="ao-search">操作人 ID<input maxLength={128} placeholder="输入操作人账号 ID" value={draft.actorId || ''} onChange={set('actorId')} /></label></>}
    <div className="ao-filter-actions"><button className="ao-primary" type="submit">查询</button><button type="button" onClick={() => onChange({})}>重置</button></div>
  </form>
}

function Overview({ data, user, onNavigate, params, loading }) {
  const tasks = data.tasks || {}, customers = data.customers || {}, financial = data.financialVisible && hasAdminPermission(user, 'billing:read') ? data.financial : null
  const todos = [
    { title: '等待客户补充', count: tasks.byStatus?.needs_input, section: 'tasks', params: { status: 'needs_input' }, permission: 'admin:tasks' },
    { title: '等待客户确认', count: tasks.byStatus?.review_required, section: 'tasks', params: { status: 'review_required' }, permission: 'admin:tasks' },
    { title: '失败任务待排查', count: tasks.byStatus?.failed, section: 'tasks', params: { status: 'failed' }, permission: 'admin:tasks' },
    { title: '已提交停止请求', count: tasks.cancelRequested, section: 'tasks', params: { status: 'cancel_requested' }, permission: 'admin:tasks' },
  ].filter(item => hasAdminPermission(user, item.permission))
  return <><Metrics items={[["全部账号", adminNumber(customers.total), `已启用 ${adminNumber(customers.active)} · 已停用 ${adminNumber(customers.inactive)}`], ['任务总数', adminNumber(tasks.total), '每个任务仅计最新版本，含历史任务'], ['正在处理', adminNumber(tasks.running), `排队 ${adminNumber(tasks.queued)}`], ['待处理工单', data.support?.available ? adminNumber(data.support.open) : '未接入', '仅待处理和处理中，已回复另列']]} />
    {financial && <Metrics items={[["充值净到账金额", adminMoney(financial.revenueFen), `已扣除退款 · 已支付订单 ${adminNumber(financial.paidOrders)} 笔`], ['账户积分余额合计', adminNumber(financial.balanceUnits), '积分'], ['待补缴合计', adminNumber(financial.dueUnits), '积分'], ['待核账任务', adminNumber(financial.pendingSettlements), '人工核对后处理']]} />}
    <div className="ao-operations-strip"><span>业务客户 <b>{adminNumber(customers.businessCustomers)}</b></span><span>后台成员 <b>{adminNumber(customers.staffAccounts)}</b></span><span>工单待客户回复 <b>{adminNumber(data.support?.waitingCustomer)}</b></span><span>已提供处理结论 <b>{adminNumber(data.support?.resolved)}</b></span></div>
    <div className="ao-dashboard-grid"><OperationsTrend trend={data.trend} params={params} loading={loading} onNavigate={onNavigate} /><div><Panel title="待办事项" note="按当前任务状态汇总，不代表今日新增。"><div className="ao-todos">{todos.map(item => <button key={item.title} onClick={() => onNavigate(item.section, item.params)}><span>{item.title}</span><b>{adminNumber(item.count)}</b><span aria-hidden="true">→</span></button>)}{!todos.length && <Empty>当前角色没有任务处理入口。</Empty>}</div></Panel><Panel title="运行提醒"><Alerts alerts={data.alerts} /></Panel></div></div>
    {data.monitoring && <p className="ao-note">{data.monitoring.message || '这里只显示服务当前返回的状态，未配置自动外部监控。'}</p>}
  </>
}

function CustomerList({ data, user, onNavigate }) {
  const financial = data.financialVisible && hasAdminPermission(user, 'billing:read')
  const columns = [{ title: '客户', render: customer => <CustomerIdentity customer={customer} onNavigate={onNavigate} canOpen /> },
    { title: '公司 / 标签', render: customer => <><span>{customer.crm?.company || '未填写公司'}</span><div className="ao-tags">{list(customer.crm?.tags).map(tag => <span key={tag}>{tag}</span>)}</div></> },
    { title: '状态 / 角色', render: customer => <><Badge value={customer.active ? 'active' : 'inactive'} labels={{ active: '已启用', inactive: '已停用' }} /><span className="ao-subtext">{list(customer.roles).map(role => adminRoleNames[role] || '其他角色').join('、') || '未配置'}</span></> },
    { title: '任务 / 待处理工单', render: customer => <>{adminNumber(customer.taskCount)} / {adminNumber(customer.openTicketCount)}</> },
    ...(financial ? [{ title: '积分余额 / 待补缴', render: customer => <>{adminNumber(customer.financial?.balanceUnits)} / {adminNumber(customer.financial?.dueUnits)}</> }] : []),
    { title: '创建时间', render: customer => adminDate(customer.createdAt) },
    { title: '操作', render: customer => <button onClick={() => onNavigate('customers', { customerId: customer.id })}>查看详情</button> }]
  return <Table items={data.items} columns={columns} empty="没有符合条件的客户，请调整查询条件。" />
}

function TaskTable({ tasks, user, onNavigate, empty }) {
  const canCustomers = hasAdminPermission(user, 'admin:customers'), canTasks = hasAdminPermission(user, 'admin:tasks')
  return <Table items={tasks} empty={empty || '暂无关联任务。'} columns={[
    { title: '任务编号', render: task => <div className="ao-identity">{canTasks ? <button className="ao-link ao-id" onClick={() => onNavigate('tasks', { runId: task.runId })}>{task.runId}</button> : <code>{task.runId}</code>}<span>第 {task.revision ?? '—'} 版</span></div> },
    { title: '所属客户', render: task => task.customer ? <CustomerIdentity customer={task.customer} canOpen={canCustomers} onNavigate={onNavigate} /> : <span className="ao-id">{task.ownerId || '未记录'}</span> },
    { title: '状态 / 阶段', render: task => <><Badge value={task.status} /><span className="ao-subtext">{stages[task.stage] || (task.stage ? '处理中间步骤' : '—')}</span></> },
    { title: '人工处理', render: task => <><Badge value={task.handling?.status} labels={adminHandlingNames} /><span className={`ao-subtext ${task.handling?.priority === 'urgent' ? 'ao-priority-urgent' : ''}`}>{adminPriorityNames[task.handling?.priority] || '未记录优先级'}</span></> },
    { title: '耗时', render: task => adminElapsed(task.elapsedSeconds) }, { title: '更新时间', render: task => adminDate(task.updatedAt || task.createdAt) },
  ]} />
}

function Usage({ data, financial = false }) {
  if (!data) return <Empty>暂无调用统计。</Empty>
  return <>{data.historicalTasksWithoutJournal > 0 && <p className="ao-note">{adminNumber(data.historicalTasksWithoutJournal)} 条旧任务的用量未完整记录；当前汇总仅包含已有调用记录。</p>}<Info rows={[["调用次数", adminNumber(data.calls)], ['输入 Token', adminNumber(data.inputTokens)], ['缓存输入 Token', adminNumber(data.cachedInputTokens)], ['输出 Token', adminNumber(data.outputTokens)], ['推理输出 Token', adminNumber(data.reasoningOutputTokens)],
    ...(data.unknownUsageCalls !== undefined ? [['用量未完整记录的调用', adminNumber(data.unknownUsageCalls)]] : []),
    ...(financial ? [['已知调用成本（美元）', typeof data.costMicroUsd === 'number' ? `$${(data.costMicroUsd / 1e6).toFixed(6)}` : '未取得完整成本'], ['成本未知的调用', adminNumber(data.unknownCostCalls)]] : []),
  ]} /></>
}

function CustomerRecordSummary({ data, user, onNavigate }) {
  const customer = data.customer, financial = data.financialVisible && hasAdminPermission(user, 'billing:read') ? data.financial : null
  return <><Panel title={nameOf(customer)} action={<div className="ao-inline-actions"><Badge value={customer.active ? 'active' : 'inactive'} labels={{ active: '已启用', inactive: '已停用' }} />{hasAdminPermission(user, 'user:manage') && <button onClick={() => onNavigate('users', { q: customer.id })}>管理此账号 →</button>}</div>}><Info rows={[["客户 ID", <code>{customer.id}</code>], ['邮箱', customer.email], ['角色', list(customer.roles).map(role => adminRoleNames[role] || '其他角色').join('、')], ['创建时间', adminDate(customer.createdAt)], ['最近更新', adminDate(customer.updatedAt)], ['任务总数', adminNumber(customer.taskCount)], ['待处理工单', adminNumber(customer.openTicketCount)]]} /></Panel>
    {financial && <Metrics items={[["积分余额", adminNumber(financial.wallet?.balanceUnits), '以服务端账本为准'], ['待补缴积分', adminNumber(financial.wallet?.dueUnits), '以服务端账本为准']]} />}
    <Panel title="最近建模任务" note="最多展示最近 10 条记录。" action={hasAdminPermission(user, 'admin:tasks') && <button onClick={() => onNavigate('tasks', { ownerId: customer.id })}>查看全部任务 →</button>}><TaskTable tasks={list(data.tasks)} user={user} onNavigate={onNavigate} /></Panel>
    <Panel title="最近服务工单" note="仅展示工单状态与关联编号。"><Table items={list(data.tickets)} columns={[
      { title: '工单编号', render: item => hasAdminPermission(user, 'support:manage') ? <button className="ao-link" onClick={() => onNavigate('support', { ticketId: item.id })}>{item.number || item.id}</button> : <code>{item.number || item.id}</code> },
      { title: '状态', render: item => <Badge value={item.status} labels={adminTicketStatusNames} /> }, { title: '关联任务', render: item => item.runId && hasAdminPermission(user, 'admin:tasks') ? <button className="ao-link ao-id" onClick={() => onNavigate('tasks', { runId: item.runId })}>{item.runId}</button> : item.runId || '—' },
      { title: '更新时间', render: item => adminDate(item.updatedAt || item.createdAt) },
    ]} /></Panel>
    {financial && <><Panel title="最近充值订单" note="订单金额以服务端订单为准。" action={<button onClick={() => onNavigate('orders', { ownerId: customer.id })}>此客户全部订单 →</button>}><Table items={list(financial.orders)} columns={[
      { title: '订单编号', render: item => <code>{item.id}</code> }, { title: '金额', render: item => adminMoney(item.amountFen) }, { title: '充值积分', render: item => adminNumber(item.creditUnits) }, { title: '状态', render: item => <Badge value={item.status} labels={adminOrderStatusNames} /> }, { title: '更新时间', render: item => adminDate(item.updatedAt || item.createdAt) },
    ]} /></Panel><Panel title="最近积分流水" action={<button onClick={() => onNavigate('credits', { ownerId: customer.id })}>此客户全部流水 →</button>}><Table items={list(financial.ledger)} columns={[
      { title: '记录时间', render: item => adminDate(item.createdAt) }, { title: '类型', render: item => ledgerKinds[item.kind] || '其他账本记录' }, { title: '积分变动', render: item => adminNumber(item.deltaUnits) }, { title: '变更后余额', render: item => adminNumber(item.balanceAfter) }, { title: '关联编号', render: item => <code>{item.referenceId || item.id}</code> },
    ]} /></Panel><Panel title="调用用量与已知成本"><Usage data={financial.usage} financial /></Panel></>}
  </>
}

function TaskDiagnosticsDetails({ data, user, onNavigate, onStop }) {
  const task = data.task, financial = data.financialVisible && hasAdminPermission(user, 'billing:read'), diagnostics = data.diagnostics || {}
  return <><Panel title="任务详情" action={<div className="ao-inline-actions"><Badge value={task.status} />{hasAdminPermission(user, 'admin:task-manage') && task.canCancel && ['queued', 'running'].includes(task.status) && <button className="ao-danger-button" onClick={() => onStop(task)}>停止任务</button>}</div>}><Info rows={[
    ['本次任务编号', <code>{task.runId}</code>], ['客户', task.customer ? <CustomerIdentity customer={task.customer} canOpen={hasAdminPermission(user, 'admin:customers')} onNavigate={onNavigate} /> : task.ownerId], ['任务组编号', <code>{task.jobId || '—'}</code>], ['版本', task.revision], ['当前阶段', stages[task.stage] || '未分类阶段'], ['耗时', adminElapsed(task.elapsedSeconds)], ['创建时间', adminDate(task.createdAt)], ['更新时间', adminDate(task.updatedAt)], ['完成时间', adminDate(task.completedAt)],
  ]} />{task.status === 'cancel_requested' && <p className="ao-note" role="status">停止请求已提交，任务尚未确认结束。请刷新状态后查看结果。</p>}</Panel>
    <FailureAdvice code={diagnostics.errorCode || task.errorCode} status={task.status} />
    <div className="ao-two-columns"><Panel title="任务诊断" note="仅包含运行状态，不显示客户图纸、对话和代码。"><Info rows={[["诊断阶段", stages[diagnostics.phase] || diagnostics.phase || '未记录'], ['错误类型', errorCategories[diagnostics.errorCategory] || '未记录'], ['错误编号', diagnostics.errorCode || task.errorCode || '未记录'], ['调用服务', diagnostics.provider || '未记录'], ['模型', diagnostics.model || '未记录'], ['供应商请求次数', adminNumber(diagnostics.providerAttempts)], ['自动重试次数', adminNumber(diagnostics.retryCount)], ['过程事件数', adminNumber(diagnostics.traceEvents)]]} /></Panel><Panel title="实际调用用量" note="未知用量保持未记录，不按零计算。"><Usage data={data.usage} financial={financial} /></Panel></div>
    {financial && <Panel title="同组任务的结算记录" note="包含本任务组各次处理，最多展示最近 20 条。"><Table items={list(data.settlements)} columns={[
      { title: '处理记录', render: item => <code>{item.attemptId}</code> }, { title: '结算状态', render: item => ({ charged: '已扣费', not_charged: '未扣费', settled: '已结算', needs_review: '待核账', pending_settlement: '待结算', deferred_due: '余额不足，差额待补缴', cap_at_balance: '按余额扣费，超额平台承担', pending: '待结算', deferred: '待补缴', disabled: '计费未启用', uncharged: '未扣费' })[item.status] || '待核实' },
      { title: '应计积分', render: item => adminNumber(item.expectedCreditUnits) }, { title: '已扣积分', render: item => adminNumber(item.chargedUnits) }, { title: '待补缴', render: item => adminNumber(item.deferredUnits) }, { title: '平台承担', render: item => adminNumber(item.platformAbsorbedUnits) },
    ]} /></Panel>}
    <Panel title="同一任务的处理记录" note="最多展示最近 20 条记录。"><TaskTable tasks={list(data.attempts)} user={user} onNavigate={onNavigate} /></Panel>
  </>
}

function CustomerDetail({ data, user, onNavigate, context }) {
  return <DetailTabs tabs={[
    { id: 'overview', label: '账户与业务', content: <CustomerRecordSummary data={data} user={user} onNavigate={onNavigate} /> },
    { id: 'crm', label: '客户档案', content: <CustomerCrm crm={data.crm || data.customer.crm} customerId={data.customer.id} user={user} context={context} /> },
    { id: 'followups', label: '跟进记录', content: <OperationsNotes kind="customers" recordId={data.customer.id} initial={data.followups} user={user} context={context} /> },
  ]} />
}
function TaskDetail({ data, user, onNavigate, onStop, context }) {
  return <DetailTabs tabs={[
    { id: 'diagnostics', label: '运行诊断', content: <TaskDiagnosticsDetails data={data} user={user} onNavigate={onNavigate} onStop={onStop} /> },
    { id: 'handling', label: '人工处理安排', content: <TaskHandling handling={data.handling || data.task.handling} runId={data.task.runId} user={user} context={context} /> },
    { id: 'notes', label: '处理备注', content: <OperationsNotes kind="tasks" recordId={data.task.runId} initial={data.notes} user={user} context={context} /> },
  ]} />
}

function SystemStatus({ data, user }) {
  const queue = data.queue || {}, config = data.configuration || {}, disk = data.disk || {}
  const host = data.hostOperations || {}, monitoredDisk = host.dataDisk || {}, backupDisk = host.backupDisk || {}, backup = data.backup || {}
  const bytes = value => typeof value === 'number' && value >= 0 ? `${(value / 1024 ** 3).toFixed(2)} GB` : '—'
  return <><Metrics items={[["正在处理", queue.available ? adminNumber(queue.running) : '不可读取', `并发上限 ${adminNumber(queue.capacity?.maxConcurrent)}`], ['排队任务', queue.available ? adminNumber(queue.queued) : '不可读取', `队列上限 ${adminNumber(queue.capacity?.maxQueued)}`], ['等待停止', queue.available ? adminNumber(queue.cancelRequested) : '不可读取', `单客户并发上限 ${adminNumber(queue.capacity?.maxPerOwner)}`], ['任务存储可用空间', disk.available ? bytes(disk.freeBytes) : '未取得', `总空间 ${bytes(disk.totalBytes)}`]]} />
    <div className="ao-two-columns"><Panel title="服务配置" note="读取当前运行配置，不主动调用外部模型。"><Info rows={[["AI 服务", config.provider || '未配置'], ['模型', config.model || '未配置'], ['推理强度', ({ low: '低', medium: '中', high: '高', xhigh: '更高', max: '最高', ultra: '最高' })[config.reasoningEffort] || config.reasoningEffort || '未配置'], ['CAD 内核', data.cadKernel?.available ? [data.cadKernel.engine, data.cadKernel.version].filter(Boolean).join(' · ') || '可用' : '不可用'], ['平台数据库', data.databases?.platformReadable === true ? '可读取' : data.databases?.platformReadable === false ? '不可读取' : '未检查'], ['任务数据库', data.databases?.cadReadable === true ? '可读取' : data.databases?.cadReadable === false ? '不可读取' : '未检查'], ['调用测试', '本页不执行 AI 或建模测试']]} /></Panel><Panel title="系统告警"><Alerts alerts={data.alerts} /></Panel></div>
    {disk.message && <p className="ao-note">{disk.message}</p>}<div className="ao-two-columns"><Panel title="业务服务"><Info rows={[["计费服务", data.billing?.available ? data.billing.chargingEnabled === true ? '已启用计费' : data.billing.chargingEnabled === false ? '计费关闭' : '未取得启用状态' : '不可读取'], ['并行辅助操作', adminNumber(queue.runningOperations)], ['客服服务', data.support?.available ? '可读取' : '不可读取'], ['待处理工单', data.support?.available ? adminNumber(data.support.open) : '—'], ...(hasAdminPermission(user, 'billing:read') ? [['待核账记录', adminNumber(data.billing?.pendingSettlements)], ['用量未知的调用', adminNumber(data.billing?.unknownUsageCalls)]] : [])]} /></Panel><Panel title="监控、告警与备份"><div className="ao-availability">{[['宿主持续监控', data.monitoring], ['外部告警通知', data.externalNotifications], ['本机备份校验', data.backup], ['异地备份', data.offsiteBackup]].map(([label, value]) => <article key={label}><div><strong>{label}</strong><Badge value={value?.stale ? 'stale' : value?.available ? 'active' : 'inactive'} labels={{ active: '已接入', inactive: '未配置', stale: '报告过期' }} /></div><p>{value?.message || '尚未取得配置状态。'}</p></article>)}</div></Panel></div>
    {data.monitoring?.configured && <Panel title="宿主最近检查" note={data.monitoring.stale ? '报告已过期，以下数值不能作为实时状态。' : '宿主每五分钟检查服务、磁盘、任务与备份；后台读取最近一次报告。'}><Info rows={[
      ['检查时间', adminDate(data.monitoring.generatedAt)], ['检查结果', ({ ok: '未触发阈值', warning: '有预警', critical: '有严重告警', stale: '报告过期' })[data.monitoring.status] || '未取得'],
      ['宿主数据盘可用', bytes(monitoredDisk.freeBytes)], ['宿主数据盘使用率', typeof monitoredDisk.usedPercent === 'number' ? `${monitoredDisk.usedPercent}%` : '—'],
      ['备份盘可用', bytes(backupDisk.freeBytes)], ['已识别自动备份集', adminNumber(backup.managedSets)],
      ['自动备份安排', backup.automaticEnabled === true ? '已启用，空闲后执行' : backup.automaticEnabled === false ? '尚未启用' : '未取得'],
      ['最近备份时间', adminDate(backup.lastBackupAt)], ['最近完整校验', adminDate(backup.lastVerifiedAt)],
      ['上游近一小时失败', adminNumber(host.jobs?.providerFailuresLastHour)],
    ]} /></Panel>}
  </>
}

function AuditList({ data }) { return <Table items={data.items} empty="没有符合条件的审计记录。" rowKey={item => `${item.source}:${item.id}`} columns={[
  { title: '时间', render: item => adminDate(item.createdAt) }, { title: '来源', render: item => auditSources[item.source] || '其他来源' }, { title: '操作', render: item => auditActions[item.action] || item.action || '未记录' }, { title: '操作人', render: item => <code>{item.actorId || '系统'}</code> }, { title: '关联对象', render: item => <code>{item.targetId || '—'}</code> }, { title: '原因记录', render: item => item.reasonRecorded === true ? '已记录' : '无原因记录' },
]} /> }

function StopDialog({ task, busy, error, onClose, onSubmit }) {
  const dialog = useRef(null), idempotency = useRef(createClientId())
  const [reason, setReason] = useState(''), [confirmed, setConfirmed] = useState(false), [validation, setValidation] = useState(''), [submitted, setSubmitted] = useState(false)
  useEffect(() => { const focused = document.activeElement; dialog.current?.showModal(); return () => focused?.focus?.() }, [])
  const submit = event => {
    event.preventDefault(); setValidation('')
    try { const payload = adminStopPayload(reason, idempotency.current); if (!confirmed) throw new Error('请确认任务编号和停止操作。'); setSubmitted(true); onSubmit(payload) }
    catch (failure) { setValidation(failure.message) }
  }
  return <dialog className="ao-stop-dialog" ref={dialog} aria-label="确认停止任务" onCancel={event => { event.preventDefault(); if (!busy) onClose() }}><header><h2>停止当前任务</h2><button type="button" aria-label="关闭停止窗口" disabled={busy} onClick={onClose}>×</button></header><form onSubmit={submit}><p>停止后将保留已有任务记录。已发生的用量不会因停止请求而自动消失。</p><Info rows={[["任务编号", <code>{task.runId}</code>], ['当前状态', <Badge value={task.status} />], ['客户', nameOf(task.customer)]]} /><label>停止原因<textarea autoFocus required minLength={5} maxLength={500} disabled={busy || submitted} value={reason} onChange={event => setReason(event.target.value)} placeholder="说明为什么需要停止，至少 5 个字" /></label><label className="ao-checkbox"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />已核对任务编号，确认提交停止请求并保存原因。</label>{submitted && <p className="ao-note">再次提交将复用本次操作编号；处理是否完成以服务端最新状态为准。</p>}{(validation || error) && <p className="ao-error" role="alert">{validation || error}</p>}<footer><button type="button" disabled={busy} onClick={onClose}>返回详情</button><button type="submit" className="ao-danger-button" disabled={busy || !confirmed}>{busy ? '正在提交…' : '确认停止任务'}</button></footer></form></dialog>
}

/** Pure response projection, shared by the live page and contract-render checks. */
export function AdminOperationsContent({ section, params = {}, data, user, loading = false, onNavigate = () => {}, onStop = () => {}, context }) {
  if (!hasAdminPermission(user, sectionPermission[section])) return <p className="ao-error" role="alert">当前账号没有查看此页面的权限。</p>
  const detail = Boolean(section === 'customers' && params.customerId || section === 'tasks' && params.runId)
  const navigate = next => onNavigate(section, next)
  if (!data) return <Empty>尚未取得页面数据。</Empty>
  return <div aria-busy={loading} className={loading ? 'ao-refreshing' : ''}>
    {section === 'overview' && <Overview data={data} user={user} params={params} loading={loading} onNavigate={onNavigate} />}
    {section === 'customers' && (detail ? <CustomerDetail key={data.customer.id} data={data} user={user} onNavigate={onNavigate} context={context} /> : <Panel title="客户账号"><CustomerList data={data} user={user} onNavigate={onNavigate} /><Pager data={data} loading={loading} params={adminListParams(section, params)} onChange={navigate} /></Panel>)}
    {section === 'tasks' && (detail ? <TaskDetail key={data.task.runId} data={data} user={user} onNavigate={onNavigate} onStop={onStop} context={context} /> : <Panel title="建模任务" note="搜索与状态筛选由服务端执行，覆盖全部记录。"><TaskTable tasks={data.items} user={user} onNavigate={onNavigate} empty="没有符合条件的任务，请调整查询条件。" /><Pager data={data} loading={loading} params={adminListParams(section, params)} onChange={navigate} /></Panel>)}
    {section === 'system' && <SystemStatus data={data} user={user} />}
    {section === 'audit' && <Panel title="审计记录" note="仅展示有权访问的记录摘要，原因正文及敏感内容不在此展开。"><AuditList data={data} /><Pager data={data} loading={loading} params={adminListParams(section, params)} onChange={navigate} /></Panel>}
  </div>
}

function OperationsSession({ session, section, params, onNavigate }) {
  const token = session.access_token, user = session.user, path = adminReadPath(section, params), viewKey = JSON.stringify([user.id, path]), readKey = JSON.stringify([token, path])
  const tokenRef = useRef(token); tokenRef.current = token
  const requestGate = useRef(createAdminRequestGate()), actionRef = useRef(null), liveKey = useRef(readKey)
  liveKey.current = readKey
  const [state, setState] = useState(null), [attempt, setAttempt] = useState(0), [stopTask, setStopTask] = useState(null), [busy, setBusy] = useState(false), [actionError, setActionError] = useState(''), [notice, setNotice] = useState('')
  useEffect(() => {
    const request = requestGate.current.begin(readKey)
    setState(previous => ({ key: viewKey, data: previous?.key === viewKey ? previous.data : null, loading: true, error: '' }))
    adminOperations.read(token, section, params, request.signal).then(data => {
      if (!request.isCurrent() || liveKey.current !== readKey) return
      if (section === 'customers' && params.customerId && data.customer?.id !== params.customerId || section === 'tasks' && params.runId && data.task?.runId !== params.runId) throw new Error('返回记录与当前页面不一致，请重新打开详情。')
      if (Array.isArray(data.items) && adminPageOffset(data.total, data.offset, data.limit) !== data.offset) {
        onNavigate(section, { ...adminListParams(section, params), offset: adminPageOffset(data.total, data.offset, data.limit) }); return
      }
      setState({ key: viewKey, data, loading: false, error: '', readAt: new Date().toISOString() })
    }).catch(error => { if (request.isCurrent() && liveKey.current === readKey && error.name !== 'AbortError') setState(previous => ({ key: viewKey, data: previous?.key === viewKey ? previous.data : null, loading: false, error: error.message })) })
    return request.abort
  }, [readKey, attempt])
  useEffect(() => { setStopTask(null); setActionError(''); setNotice('') }, [path])
  useEffect(() => { setBusy(false); return () => { actionRef.current?.abort(); actionRef.current = null } }, [readKey])
  const current = state?.key === viewKey ? state : null, data = current?.data, loading = current?.loading ?? true
  const refresh = () => setAttempt(value => value + 1)
  const navigate = next => onNavigate(section, next)
  const openRecord = (target, next) => onNavigate(target, target === section && (next?.customerId || next?.runId)
    ? { ...adminListParams(section, params), ...next } : next)
  const submitStop = async payload => {
    if (actionRef.current) return
    const controller = new AbortController(); actionRef.current = controller
    setBusy(true); setActionError('')
    try {
      const result = await adminOperations.cancel(token, stopTask.runId, payload, controller.signal)
      if (controller.signal.aborted || liveKey.current !== readKey) return
      setNotice(adminStopOutcome(stopTask.runId, result)); setStopTask(null); refresh()
    } catch (error) { if (!controller.signal.aborted && liveKey.current === readKey) setActionError(error.message) }
    finally { if (actionRef.current === controller) { actionRef.current = null; setBusy(false) } }
  }
  const detail = Boolean(section === 'customers' && params.customerId || section === 'tasks' && params.runId)
  const context = { scope: `${user.id}:${path}`, token: () => tokenRef.current, onSaved: refresh }
  const exportCurrent = () => { try {
    const options = { canFinance: hasAdminPermission(user, 'billing:read'), readAt: current?.readAt, filter: new URLSearchParams(adminListParams(section, params)).toString() }
    if (section === 'tasks' && detail) downloadAdminText(adminDiagnosticExport(data, options), `joyniu-task-diagnostic-${data.task.runId.replace(/[^a-zA-Z0-9_-]/g, '_')}.json`, 'application/json;charset=utf-8')
    else downloadAdminText(adminPageCsv(section, data, options), `joyniu-${section}-page-${Math.floor(data.offset/data.limit)+1}.csv`, 'text/csv;charset=utf-8')
    setNotice(detail ? '已导出当前任务的脱敏诊断。' : '已导出当前已读取页面；筛选范围与读取时间写在 CSV 中。')
  } catch (failure) { setNotice(failure.message) } }
  return <section className="admin-operations" aria-label={adminSectionNames[section]}><header className="ao-page-header"><div><span className="ao-eyebrow">JOYNIU CAD · 运营后台</span><h1>{adminSectionNames[section]}{detail ? ' / 详情' : ''}</h1><p>{current?.readAt ? `最近读取：${adminDate(data?.generatedAt || current.readAt)}` : '读取当前服务中的真实记录'}</p></div><div className="ao-inline-actions">{detail && <button onClick={() => onNavigate(section, adminListParams(section, params))}>← 返回列表</button>}{data && ((detail && section === 'tasks') || (!detail && ['customers','tasks','audit'].includes(section))) && <button disabled={loading || busy} onClick={exportCurrent}>{detail ? '导出脱敏诊断' : `导出本页 ${data.items?.length || 0} 条`}</button>}<button disabled={loading || busy} onClick={refresh}>{loading ? '正在读取…' : '刷新数据'}</button></div></header>
    {!detail && section === 'tasks' && <div className="ao-quick-filters"><span>快捷工作视图</span>{[['未分派',{ handlingStatus: 'unassigned' }],['紧急跟进',{ priority: 'urgent' }],['失败排查',{ status: 'failed' }],['等待确认',{ status: 'review_required' }],['等待停止',{ status: 'cancel_requested' }]].map(([label,filter]) => <button key={label} aria-pressed={Object.entries(filter).every(([key,value]) => params[key] === value)} onClick={() => navigate({ ...adminListParams(section,params), status: '', handlingStatus: '', priority: '', ...filter, offset: 0 })}>{label}</button>)}</div>}
    {!detail && ['customers', 'tasks', 'audit'].includes(section) && <Filters key={path} section={section} params={params} onChange={navigate} canFinance={hasAdminPermission(user, 'billing:read')} />}
    {current?.error && <div className="ao-error" role="alert"><span>{current.error}{data ? ' 下方保留上次读取的数据。' : ''}</span><button disabled={loading} onClick={refresh}>重新读取</button></div>}{notice && <p className="ao-notice" role="status">{notice}</p>}
    {loading && !data && <div className="ao-loading" role="status"><span className="ao-spinner" />正在加载{adminSectionNames[section]}…</div>}
    {data && <AdminOperationsContent section={section} params={params} data={data} user={user} loading={loading} context={context} onNavigate={openRecord} onStop={task => { setActionError(''); setStopTask(task) }} />}
    {stopTask && <StopDialog key={stopTask.runId} task={stopTask} busy={busy} error={actionError} onClose={() => { if (!busy) setStopTask(null) }} onSubmit={submitStop} />}
  </section>
}

export default function AdminOperationsWorkspace({ account, section = 'overview', params = {}, onNavigate = () => {} }) {
  const session = account?.session
  if (!session?.access_token || !session.user) return <section className="admin-operations"><h1>运营后台</h1><Empty>请登录有运营权限的账号后继续。</Empty></section>
  if (!sectionPermission[section] || !hasAdminPermission(session.user, sectionPermission[section])) return <section className="admin-operations"><h1>{adminSectionNames[section] || '运营后台'}</h1><p className="ao-error" role="alert">当前账号没有查看此页面的权限。</p></section>
  return <OperationsSession key={`${session.user.id}:${JSON.stringify([session.user.roles, session.user.permissions])}`} session={session} section={section} params={params} onNavigate={onNavigate} />
}
