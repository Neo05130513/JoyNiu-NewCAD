import { API_BASE } from './api.js'

export const adminSectionNames = { overview: '运营概览', customers: '客户管理', tasks: '任务管理', system: '系统状态', audit: '操作审计' }
export const adminTaskStatusNames = { queued: '排队中', running: '处理中', cancel_requested: '等待停止', needs_input: '等待客户补充', review_required: '等待确认', ready: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消' }
export const adminRoleNames = { admin: '管理员', ops: '运营', finance: '财务', support: '客服', auditor: '审计员', designer: '客户', viewer: '只读成员' }
export const adminTicketStatusNames = { open: '待处理', pending: '待处理', in_progress: '处理中', waiting_customer: '等待客户补充', awaiting_customer: '等待客户', resolved: '已提供处理结论', closed: '已关闭', approved: '已审核', rejected: '已拒绝', submitted: '已提交', completed: '已完成' }
export const adminOrderStatusNames = { creating: '正在创建支付', pending_payment: '待支付', payment_unavailable: '支付渠道暂不可用', cancelled: '已取消', requested: '已提交', reviewing: '审核中', rejected: '已驳回', approved: '退款审核通过，待渠道处理', processing: '开票处理中', issued: '已开票', refund_processing: '退款处理中', refund_closed: '退款已关闭', refund_abnormal: '退款异常，待核查', pending: '待支付', created: '已创建', paid: '已支付', closed: '已关闭', expired: '已过期', refunded: '已退款', refund_pending: '退款处理中', failed: '未完成' }
export const adminDate = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
export const adminNumber = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'
export const adminMoney = value => typeof value === 'number' && Number.isFinite(value) ? `¥${(value / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'
export function adminElapsed(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  const seconds = Math.floor(value)
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分` : seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`
}

export function adminListParams(section, params = {}) {
  const values = { limit: 20, offset: Math.min(1_000_000, Math.max(0, Math.floor(Number(params.offset) || 0))) }
  const q = typeof params.q === 'string' ? params.q.trim().slice(0, 128) : ''
  if (q && ['customers', 'tasks'].includes(section)) values.q = q
  if (section === 'customers' && ['true', 'false'].includes(String(params.active))) values.active = String(params.active)
  if (section === 'tasks') {
    if (Object.hasOwn(adminTaskStatusNames, params.status)) values.status = params.status
    if (Object.hasOwn(adminHandlingNames, params.handlingStatus)) values.handlingStatus = params.handlingStatus
    if (Object.hasOwn(adminPriorityNames, params.priority)) values.priority = params.priority
    if (typeof params.ownerId === 'string' && params.ownerId.trim()) values.ownerId = params.ownerId.trim().slice(0, 160)
  }
  if (section === 'audit') {
    if (['operations', 'accounts', 'billing', 'support'].includes(params.source)) values.source = params.source
    if (typeof params.actorId === 'string' && params.actorId.trim()) values.actorId = params.actorId.trim().slice(0, 128)
  }
  return values
}

export function adminReadPath(section, params = {}) {
  if (!Object.hasOwn(adminSectionNames, section)) throw new Error('此运营页面不存在。')
  const id = section === 'customers' ? params.customerId : section === 'tasks' ? params.runId : null
  if (id) return `/${section}/${encodeURIComponent(id)}`
  if (['customers', 'tasks', 'audit'].includes(section)) return `/${section}?${new URLSearchParams(adminListParams(section, params))}`
  if (section === 'overview' && params.days !== undefined) return `/overview?days=${adminTrendDays(params.days)}`
  return `/${section}`
}

export function validateAdminPage(value) {
  if (!Array.isArray(value?.items) || !Number.isSafeInteger(value.total) || value.total < 0
    || !Number.isSafeInteger(value.limit) || value.limit < 1 || value.limit > 100 || !Number.isSafeInteger(value.offset) || value.offset < 0) {
    throw new Error('列表数据格式不完整，请重新读取。')
  }
  return value
}

export const adminPageOffset = (total, offset, limit = 20) => Math.min(Math.max(0, offset), Math.max(0, Math.ceil(total / limit) - 1) * limit)

// Invalidated requests cannot write into another account, filter, or detail view.
export function createAdminRequestGate() {
  let current = null
  return {
    begin(key) {
      current?.controller.abort()
      const entry = { key, controller: new AbortController() }; current = entry
      return { signal: entry.controller.signal, isCurrent: () => current === entry && !entry.controller.signal.aborted,
        abort: () => { entry.controller.abort(); if (current === entry) current = null } }
    },
    abort() { current?.controller.abort(); current = null },
  }
}

export function adminStopPayload(reason, idempotencyKey) {
  const text = String(reason || '').trim()
  if (text.length < 5 || text.length > 500) throw new Error('请填写 5 至 500 字的停止原因。')
  if (typeof idempotencyKey !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(idempotencyKey)) throw new Error('缺少有效操作编号，请重新打开停止窗口。')
  return { reason: text, idempotencyKey }
}

export function adminStopOutcome(runId, value) {
  if (value?.task?.runId !== runId || typeof value.accepted !== 'boolean' || !Object.hasOwn(adminTaskStatusNames, value.task.status)) {
    throw new Error('尚未取得当前任务的停止结果，请刷新任务状态；再次提交会复用本次操作编号。')
  }
  if (value.task.status === 'cancelled') return '任务已取消，停止记录已保存。'
  if (value.task.status === 'cancel_requested') return '停止请求已提交，正在等待任务结束。请刷新状态确认。'
  if (value.task.status === 'queued') return value.accepted ? '停止请求已记录，任务仍在队列中等待确认，请刷新状态。' : '当前任务仍在排队，请刷新详情核对停止状态。'
  if (!['queued', 'running'].includes(value.task.status)) return `任务当前为“${adminTaskStatusNames[value.task.status]}”，请以详情中的最新状态为准。`
  return '服务已响应，任务仍在运行，请刷新状态确认是否已停止。'
}

export async function requestAdminOperations(path, { token, signal, body, method = 'GET', fetcher = fetch } = {}) {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, 30_000)
  try {
    controller.signal.throwIfAborted()
    const response = await fetcher(`${API_BASE}/admin${path}`, { method, signal: controller.signal, credentials: 'include', cache: 'no-store', redirect: 'error',
      headers: { Accept: 'application/json', Authorization: `Bearer ${typeof token === 'function' ? token() : token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}) })
    const value = await response.json().catch(() => null)
    controller.signal.throwIfAborted()
    if (!response.ok) {
      const message = ({ 401: '登录已失效，请重新登录运营账号。', 403: '当前账号没有查看或操作此项的权限。', 404: '此记录不存在或已不可访问。', 409: '记录状态已变化，请刷新详情后重试。', 422: '查询条件或操作内容有误，请检查后重试。', 429: '操作过于频繁，请稍后重试。' })[response.status]
      throw Object.assign(new Error(message || '运营服务暂时不可用，请稍后重试。'), { status: response.status })
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('运营服务返回的数据不完整，请重试。')
    return value
  } catch (error) {
    if (timedOut && !signal?.aborted) throw new Error('运营服务响应超时，请刷新状态。操作是否完成以服务端记录为准。')
    if (error instanceof TypeError) throw new Error('无法连接运营服务，请检查网络后重试。')
    throw error
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
}

export const adminOperations = {
  async read(token, section, params, signal) {
    const result = await requestAdminOperations(adminReadPath(section, params), { token, signal })
    const detail = section === 'customers' && params?.customerId || section === 'tasks' && params?.runId
    return ['customers', 'tasks', 'audit'].includes(section) && !detail ? validateAdminPage(result) : result
  },
  cancel: (token, runId, payload, signal) => requestAdminOperations(`/tasks/${encodeURIComponent(runId)}/cancel`, { token, signal, method: 'POST', body: adminStopPayload(payload.reason, payload.idempotencyKey) }),
}

export const adminHandlingNames = { unassigned: '未分派', pending: '待跟进', in_progress: '跟进中', resolved: '已处理' }
export const adminPriorityNames = { normal: '普通', high: '高优先级', urgent: '紧急' }
export const adminTrendDays = value => [7, 30, 90].includes(Number(value)) ? Number(value) : 30
const safeCount = value => Number.isSafeInteger(value) && value >= 0 ? value : null
export function adminTrendPoints(trend) {
  if (!Array.isArray(trend?.points)) return []
  const seen = new Set()
  return trend.points.filter(point => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(point?.date || '') || !Number.isFinite(Date.parse(`${point.date}T00:00:00Z`)) || new Date(`${point.date}T00:00:00Z`).toISOString().slice(0, 10) !== point.date || seen.has(point.date)) return false
    seen.add(point.date); return true
  }).map(point => ({ date: point.date, tasks: safeCount(point.tasks), success: safeCount(point.success), failed: safeCount(point.failed), reviewRequired: safeCount(point.reviewRequired), needsInput: safeCount(point.needsInput), calls: safeCount(point.usage?.calls), unknownUsageCalls: safeCount(point.usage?.unknownUsageCalls), historicalTasksWithoutJournal: safeCount(point.historicalTasksWithoutJournal) })).sort((a, b) => a.date.localeCompare(b.date)).slice(-90)
}
export function adminTrendGeometry(points, field, width = 840, height = 200) {
  const values = points.map(point => safeCount(point[field])), max = Math.max(1, ...values.filter(value => value !== null)), segments = []; let segment = []
  const firstDate = Date.parse(points[0]?.date || ''), lastDate = Date.parse(points.at(-1)?.date || '')
  const coordinates = values.map((value, index) => value === null ? null : { x: 32 + (Date.parse(points[index].date) - firstDate) / Math.max(86400000, lastDate - firstDate) * (width - 48), y: height - 28 - value / max * (height - 48), value, date: points[index].date })
  for (const point of coordinates) {
    if (point && segment.length && Date.parse(point.date) - Date.parse(segment.at(-1).date) > 86400000) { segments.push(segment); segment = [] }
    if (point) segment.push(point); else if (segment.length) { segments.push(segment); segment = [] }
  }
  if (segment.length) segments.push(segment)
  return { max, coordinates, paths: segments.map(values => values.map((point, i) => `${i ? 'L' : 'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ')) }
}
function requiredText(value, max, label, min = 0) {
  if (typeof value !== 'string') throw new Error(`${label}格式不正确。`)
  const text = value.trim()
  if (text.length < min || text.length > max) throw new Error(`${label}须为 ${min} 至 ${max} 字。`)
  return text
}
function mutationBase(values) {
  const { reason, idempotencyKey } = adminStopPayload(values.reason, values.idempotencyKey)
  if (!Number.isSafeInteger(values.expectedVersion) || values.expectedVersion < 0) throw new Error('缺少有效记录版本，请刷新详情。')
  return { expectedVersion: values.expectedVersion, idempotencyKey, reason }
}
export function adminCrmPayload(values) {
  const tags = Array.isArray(values.tags) ? values.tags : String(values.tags || '').split(/[,，\n]/)
  const cleaned = [...new Set(tags.map(tag => requiredText(tag, 24, '标签')).filter(Boolean))]
  if (cleaned.length > 12) throw new Error('客户标签最多 12 个，每个最多 24 字。')
  return { company: requiredText(values.company, 120, '公司名称'), contactName: requiredText(values.contactName, 80, '联系人'), phone: requiredText(values.phone, 40, '联系电话'), tags: cleaned, internalNote: requiredText(values.internalNote, 2000, '内部备注'), ...mutationBase(values) }
}
export function adminHandlingPayload(values) {
  if (!Object.hasOwn(adminHandlingNames, values.status) || !Object.hasOwn(adminPriorityNames, values.priority)) throw new Error('请选择有效的人工处理状态与优先级。')
  return { status: values.status, priority: values.priority, ...mutationBase(values) }
}
export function adminEntryPayload(content, idempotencyKey) {
  adminStopPayload('验证操作编号', idempotencyKey)
  return { content: requiredText(content, 2000, '跟进内容', 1), idempotencyKey }
}
// One logical payload gets one key, including uncertain failures and token refresh.
export function createAdminMutationIdentity(createKey) {
  let previous = null
  return { forPayload(value) { const fingerprint = JSON.stringify(value); if (previous?.fingerprint !== fingerprint) previous = { fingerprint, key: createKey() }; return previous.key }, reset() { previous = null } }
}
export function adminFailureAdvice(code, status) {
  if (status === 'cancel_requested') return { title: '等待执行进程确认停止', steps: ['刷新任务状态确认是否结束。', '远程请求可能仍在返回，不要把停止请求当作已取消。'] }
  if (['timeout', 'time_limit', 'http_408', 'http_504', 'worker_timeout'].includes(code)) return { title: '核对服务响应与已保存进度', steps: ['查看发生时间、当前阶段和供应商请求次数。', '先核对已有任务与客户反馈，再由客户决定是否重新提交，避免重复工作。'] }
  if (['authentication', 'permission_denied', 'http_401', 'http_403', 'not_configured'].includes(code)) return { title: '核对服务配置或账号权限', steps: ['由有权限的管理员检查当前服务配置及账号状态。', '不要在跟进记录中粘贴密钥、登录令牌或原图链接。'] }
  if (['rate_limit', 'http_429', 'quota_exceeded'].includes(code)) return { title: '核对供应商限制与当前队列', steps: ['查看系统状态中的真实排队与并发数据。', '保留任务编号和发生时间，确认限制解除后再安排后续处理。'] }
  if (['invalid_plan', 'invalid_geometry', 'geometry_failed', 'execution_failed', 'inspection_failed'].includes(code)) return { title: '核对几何问题与待确认信息', steps: ['查看客户工作台中的核对结果和未决问题。', '记录客户补充的依据；人工处理状态不代表模型已通过制造验证。'] }
  if (status === 'needs_input') return { title: '等待客户补充明确依据', steps: ['查看关联工单或客户跟进记录。', '记录已说明的问题，避免重复追问；此处不会发送外部通知。'] }
  return { title: ['failed', 'interrupted'].includes(status) ? '保留诊断后安排人工核查' : '按当前任务状态继续跟进', steps: ['核对任务编号、发生时间和已记录的错误类型。', '需要排障时导出本条脱敏诊断；此处不会自动重试建模。'] }
}
export function adminCsvCell(value) {
  let text = value == null ? '' : String(value).replace(/\u0000/g, '')
  if (/^[\s\u0000-\u001f\u007f]*[=+\-@]/u.test(text) || /^[\t\r\n]/u.test(text)) text = `'${text}`
  return `"${text.replaceAll('"', '""')}"`
}
export function adminPageCsv(section, data, { canFinance = false, filter = '', readAt = '' } = {}) {
  validateAdminPage(data)
  if (data.items.length > data.limit || (data.items.length && data.offset + data.items.length > data.total)) throw new Error('当前页范围不一致，请重新读取后导出。')
  const financial = canFinance && data.financialVisible === true
  let columns
  if (section === 'customers') columns = [['客户ID', v => v.id], ['名称', v => v.displayName], ['邮箱', v => v.email], ['账号状态', v => v.active === true ? '已启用' : v.active === false ? '已停用' : '未记录'], ['公司', v => v.crm?.company], ['标签', v => v.crm?.tags?.join('、')], ['任务数', v => v.taskCount], ...(financial ? [['积分余额', v => v.financial?.balanceUnits], ['待补缴', v => v.financial?.dueUnits]] : [])]
  else if (section === 'tasks') columns = [['任务编号', v => v.runId], ['客户ID', v => v.ownerId], ['任务状态', v => adminTaskStatusNames[v.status] || '未知'], ['人工状态', v => adminHandlingNames[v.handling?.status] || '未记录'], ['优先级', v => adminPriorityNames[v.handling?.priority] || '未记录'], ['错误码', v => v.errorCode], ['耗时秒', v => v.elapsedSeconds], ['创建时间', v => v.createdAt]]
  else if (section === 'audit') columns = [['审计编号', v => v.id], ['来源', v => v.source], ['操作人', v => v.actorId], ['操作', v => v.action], ['对象', v => v.targetId], ['发生时间', v => v.createdAt]]
  else throw new Error('此页面不支持列表导出。')
  const rows = [['导出范围', `仅当前已读取页面：第 ${data.offset + (data.items.length ? 1 : 0)} 至 ${data.offset + data.items.length} 条，共 ${data.total} 条`], ['读取时间', readAt], ['当前筛选', filter], columns.map(([name]) => name), ...data.items.map(item => columns.map(([, read]) => read(item)))]
  return '\uFEFF' + rows.map(row => row.map(adminCsvCell).join(',')).join('\r\n')
}
export function adminDiagnosticExport(data, { canFinance = false, readAt = '' } = {}) {
  if (!data?.task?.runId) throw new Error('缺少当前任务诊断。')
  const pick = (source, keys) => Object.fromEntries(keys.filter(key => source && Object.hasOwn(source, key)).map(key => {
    const value = source[key]
    return [key, value === null || (typeof value === 'string' && /^[A-Za-z0-9_.:+-]{1,160}$/.test(value)) || (typeof value === 'number' && Number.isFinite(value)) ? value : null]
  }))
  return JSON.stringify({ report: 'JOYNIU CAD 脱敏任务诊断', scope: '仅当前任务；不含原图、对话、模型代码、CRM备注或下载链接', readAt,
    task: pick(data.task, ['runId', 'jobId', 'attemptId', 'status', 'stage', 'errorCode', 'revision', 'createdAt', 'updatedAt', 'completedAt', 'elapsedSeconds']),
    diagnostics: pick(data.diagnostics, ['phase', 'errorCode', 'errorCategory', 'provider', 'model', 'retryCount', 'providerAttempts', 'traceEvents']),
    usage: pick(data.usage, ['scope', 'calls', 'inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'unknownUsageCalls', ...(canFinance && data.financialVisible ? ['costMicroUsd', 'unknownCostCalls'] : [])]),
    handling: pick(data.handling || data.task.handling, ['status', 'priority', 'version']) }, null, 2)
}
export function downloadAdminText(text, filename, type = 'text/plain;charset=utf-8') {
  const url = URL.createObjectURL(new Blob([text], { type })), link = document.createElement('a')
  link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
adminOperations.crm = (token, id, values, signal) => requestAdminOperations(`/customers/${encodeURIComponent(id)}/crm`, { token, signal, method: 'PUT', body: adminCrmPayload(values) })
adminOperations.handling = (token, id, values, signal) => requestAdminOperations(`/tasks/${encodeURIComponent(id)}/handling`, { token, signal, method: 'PUT', body: adminHandlingPayload(values) })
adminOperations.entries = async (token, kind, id, offset = 0, signal) => {
  if (!['customers', 'tasks'].includes(kind)) throw new Error('记录类型不正确。')
  return validateAdminPage(await requestAdminOperations(`/${kind}/${encodeURIComponent(id)}/${kind === 'customers' ? 'followups' : 'notes'}?limit=10&offset=${Math.min(1_000_000, Math.max(0, Math.floor(Number(offset) || 0)))}`, { token, signal }))
}
adminOperations.append = (token, kind, id, values, signal) => {
  if (!['customers', 'tasks'].includes(kind)) throw new Error('记录类型不正确。')
  return requestAdminOperations(`/${kind}/${encodeURIComponent(id)}/${kind === 'customers' ? 'followups' : 'notes'}`, { token, signal, method: 'POST', body: adminEntryPayload(values.content, values.idempotencyKey) })
}

export function createAdminActionGate() {
  let pending = null
  return {
    begin(scope) {
      if (pending?.scope === scope) return null
      pending?.controller.abort()
      const entry = { scope, controller: new AbortController() }; pending = entry
      return { signal: entry.controller.signal,
        isCurrent: currentScope => pending === entry && !entry.controller.signal.aborted && currentScope === scope,
        finish: () => { if (pending === entry) pending = null },
      }
    },
    abort() { pending?.controller.abort(); pending = null },
  }
}
