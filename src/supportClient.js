import { API_BASE } from './api.js'
import { createClientId } from './clientId.js'
import { dataRequestPayload, emptyDataRequest } from './supportDataRequest.js'

export const supportCategories = { modeling: '建模与图纸', billing: '积分与订单', account: '账号与云保存', data_deletion: '数据删除申请', other: '其他问题' }
export const supportStatuses = { open: '待处理', in_progress: '处理中', waiting_customer: '等待客户补充', resolved: '已提供处理结论', closed: '已关闭' }
export const supportPriorities = { low: '低', normal: '普通', high: '高', urgent: '紧急' }
export const supportViews = { pending: '全部待处理', mine: '分配给我', unassigned: '未分配' }
export function supportQueryState(params = {}) {
  const status = typeof params.status === 'string' && (params.status === 'pending' || Object.hasOwn(supportStatuses, params.status)) ? params.status : ''
  const assignedTo = typeof params.assignedTo === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(params.assignedTo) ? params.assignedTo : ''
  const priority = typeof params.priority === 'string' && Object.hasOwn(supportPriorities, params.priority) ? params.priority : ''
  const offset = /^(0|[1-9]\d*)$/.test(String(params.offset ?? 0)) ? Number(params.offset ?? 0) : 0
  return { q: typeof params.q === 'string' ? params.q.trim().slice(0, 128) : '', status, assignedTo, priority, offset: Number.isSafeInteger(offset) && offset <= 1_000_000 ? Math.floor(offset / 20) * 20 : 0 }
}
export const supportReplyTemplates = [
  { id: 'reproduce', label: '补充操作步骤', body: '请补充出现问题时的操作步骤，以及预期结果与实际结果的差异，方便我们核对。' },
  { id: 'dimension', label: '核对尺寸依据', body: '请指出需要核对的参数名称、图纸视图和对应尺寸标注；如有多种解释，请说明你希望采用的依据。' },
  { id: 'billing', label: '查询积分记录', body: '请提供相关订单编号或任务编号，以及你认为有差异的积分记录。请勿发送登录密码或支付凭据。' },
]
export function supportViewFilters(view) {
  if (!Object.hasOwn(supportViews, view)) return { status: '', assignedTo: '' }
  return { status: 'pending', assignedTo: view === 'mine' ? 'me' : view === 'unassigned' ? 'unassigned' : '' }
}
export function supportReplyDraft(current, templateId) {
  const template = supportReplyTemplates.find(item => item.id === templateId)
  if (!template) return current
  const draft = current.trim() ? `${current}\n\n${template.body}` : template.body
  if (draft.length > 5000) throw new Error('追加常用回复后超过 5000 字，请先精简草稿。')
  return draft
}
const transitions = { open: ['in_progress', 'waiting_customer', 'resolved', 'closed'], in_progress: ['waiting_customer', 'resolved', 'closed'], waiting_customer: ['in_progress', 'resolved', 'closed'], resolved: ['in_progress', 'closed'], closed: ['in_progress'] }
export const supportTransitions = status => transitions[status] || []
export const supportSessionKey = (session, admin = false) => `${session?.user?.id || 'guest'}:${admin}`
export const supportPageOffset = (total, offset, limit = 20) => Math.min(Math.max(0, offset), Math.max(0, Math.ceil(total / limit) - 1) * limit)
export const emptySupportDraft = () => ({ subject: '', category: 'other', body: '', runId: '', drawingConsent: false, dataRequest: emptyDataRequest() })

function text(value, label, maximum) {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > maximum) throw new Error(`${label}须填写 1 至 ${maximum} 个字符。`)
  return value.trim()
}

export function supportCreatePayload(value) {
  if (!Object.hasOwn(supportCategories, value?.category)) throw new Error('请选择有效的问题分类。')
  if (typeof value.drawingConsent !== 'boolean') throw new Error('附图授权必须由你明确选择。')
  const runId = typeof value.runId === 'string' && value.runId.trim() ? text(value.runId, '任务编号', 128) : null
  if (value.drawingConsent && !runId) throw new Error('请先关联自己的建模任务，再选择附图授权。')
  return { subject: text(value.subject, '问题标题', 120), category: value.category, body: text(value.body, '问题说明', 5000), runId, drawingConsent: value.drawingConsent, ...(value.category === 'data_deletion' ? { dataRequest: dataRequestPayload(value.dataRequest) } : {}) }
}

export const supportMessagePayload = body => ({ body: text(body, '说明内容', 5000) })
export function supportAssignmentPayload(ticket, values) {
  if (!Number.isSafeInteger(ticket?.revision) || ticket.revision < 1) throw new Error('工单版本尚未加载，请刷新重试。')
  if (!Object.hasOwn(supportPriorities, values.priority)) throw new Error('请选择有效的优先级。')
  return { revision: ticket.revision, assignedTo: values.assignedTo ? text(values.assignedTo, '负责人', 128) : null, priority: values.priority, reason: text(values.reason, '分派说明', 500) }
}
export function supportNotePayload(ticket, body) {
  if (!Number.isSafeInteger(ticket?.revision) || ticket.revision < 1) throw new Error('工单版本尚未加载，请刷新重试。')
  return { ...supportMessagePayload(body), revision: ticket.revision }
}
export function supportUpdatePayload(ticket, changes, admin = false) {
  if (!Number.isSafeInteger(ticket?.revision) || ticket.revision < 1) throw new Error('工单版本尚未加载，请刷新重试。')
  if (admin) {
    if (!supportTransitions(ticket.status).includes(changes.status)) throw new Error('当前工单不支持此状态流转。')
    return { revision: ticket.revision, status: changes.status, reason: text(changes.reason, '状态变更说明', 500) }
  }
  if (Object.hasOwn(changes, 'drawingConsent')) {
    if (typeof changes.drawingConsent !== 'boolean' || (changes.drawingConsent && !ticket.runId)) throw new Error('授权须关联自己的建模任务，并明确选择。')
    return { revision: ticket.revision, drawingConsent: changes.drawingConsent }
  }
  if (changes.status !== 'closed') throw new Error('客户仅可关闭工单。')
  return { revision: ticket.revision, status: 'closed' }
}

/** Retry the same logical payload with its original key, including token renewal. */
export function createSupportIntentKeys(makeId = createClientId) {
  const keys = new Map()
  const fingerprint = (action, payload) => JSON.stringify([action, payload])
  return {
    get(action, payload) { const key = fingerprint(action, payload); if (!keys.has(key)) keys.set(key, makeId()); return keys.get(key) },
    complete(action, payload) { keys.delete(fingerprint(action, payload)) },
  }
}

export function createSupportClient({ apiBase = API_BASE, fetchImpl = globalThis.fetch, timeoutMs = 30_000 } = {}) {
  const base = `${apiBase.replace(/\/+$/, '')}/support`
  async function request(path, token, { method = 'GET', body, signal } = {}) {
    if (!token) throw Object.assign(new Error('请登录后查看自己的支持工单。'), { status: 401 })
    const controller = new AbortController()
    const abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort()
    else signal?.addEventListener('abort', abort, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
      const response = await fetchImpl(`${base}${path}`, { method, credentials: 'include', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
      let payload
      try { payload = await response.json() } catch { throw new Error('支持服务返回了无效响应，请刷新确认提交结果。') }
      if (!response.ok) { const detail = payload?.detail; throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message || '工单操作未完成，请重试。'), { status: response.status, payload }) }
      return payload
    } catch (error) {
      if (timedOut && !signal?.aborted) throw new Error('工单请求超时。说明与请求编号已保留，请刷新确认或重试同一操作。')
      throw error
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }
  const path = (id, admin) => `${admin ? '/admin' : ''}/tickets${id ? `/${encodeURIComponent(id)}` : ''}`
  return {
    list: (token, { admin = false, status = '', q = '', assignedTo = '', priority = '', limit = 20, offset = 0, signal } = {}) => {
      const query = new URLSearchParams({ limit, offset })
      if (status) query.set('status', status)
      if (admin) for (const [key, value] of Object.entries({ q, assignedTo, priority })) if (value) query.set(key, value)
      return request(`${path('', admin)}?${query}`, token, { signal })
    },
    create: (token, values, idempotencyKey, signal) => request('/tickets', token, { method: 'POST', body: { ...supportCreatePayload(values), idempotencyKey }, signal }),
    ticket: (token, id, { admin = false, signal } = {}) => request(path(id, admin), token, { signal }),
    messages: (token, id, { admin = false, limit = 50, offset = 0, signal } = {}) => request(`${path(id, admin)}/messages?limit=${limit}&offset=${offset}`, token, { signal }),
    append: (token, id, values, idempotencyKey, { admin = false, signal } = {}) => request(`${path(id, admin)}/messages`, token, { method: 'POST', body: { ...supportMessagePayload(values.body), idempotencyKey }, signal }),
    update: (token, id, values, idempotencyKey, { admin = false, signal } = {}) => request(path(id, admin), token, { method: 'PATCH', body: { ...values, idempotencyKey }, signal }),
    audit: (token, id, { limit = 20, offset = 0, signal } = {}) => request(`${path(id, true)}/audit?limit=${limit}&offset=${offset}`, token, { signal }),
    assignees: (token, signal) => request('/admin/assignees', token, { signal }),
    notes: (token, id, { limit = 20, offset = 0, signal } = {}) => request(`${path(id, true)}/notes?limit=${limit}&offset=${offset}`, token, { signal }),
    appendNote: (token, id, values, idempotencyKey, signal) => request(`${path(id, true)}/notes`, token, { method: 'POST', body: { ...values, idempotencyKey }, signal }),
    recordDataReceipt: (token, id, values, idempotencyKey, signal) => request(`${path(id, true)}/data-receipts`, token, { method: 'POST', body: { ...values, idempotencyKey }, signal }),
  }
}

export const supportClient = createSupportClient()
