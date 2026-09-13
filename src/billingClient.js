import { API_BASE } from './api.js'

export function parseMoneyFen(value, { maximum = 100_000_000 } = {}) {
  const text = String(value ?? '').trim()
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(text)) throw new Error('金额请填写最多两位小数的人民币数值。')
  const [yuan, cents = ''] = text.split('.')
  const result = Number(yuan) * 100 + Number(cents.padEnd(2, '0'))
  if (!Number.isSafeInteger(result) || result < 1 || result > maximum) throw new Error('金额超出允许范围。')
  return result
}

export function parseCreditUnits(value, { signed = false } = {}) {
  const text = String(value ?? '').trim()
  if (!(signed ? /^-?[1-9]\d*$/ : /^[1-9]\d*$/).test(text)) throw new Error(signed ? '调整积分请填写非零整数，扣减使用负数。' : '积分请填写正整数。')
  const result = Number(text)
  if (!Number.isSafeInteger(result) || Math.abs(result) > 1_000_000_000_000) throw new Error('积分数值超出允许范围。')
  return result
}

export const formatFen = (value) => Number.isSafeInteger(value) ? `¥${(value / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'
export const formatCreditUnits = (value) => Number.isSafeInteger(value) ? value.toLocaleString('zh-CN') : '—'
export const formatBillingDate = (value) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'

const statuses = {
  creating: '正在创建支付', pending_payment: '待支付', paid: '已支付', payment_unavailable: '支付渠道暂不可用',
  cancelled: '已取消', expired: '已过期', refunded: '已退款', requested: '已提交', reviewing: '审核中',
  rejected: '已驳回', approved: '退款审核通过，待渠道处理', processing: '开票处理中', issued: '已开票',
  refund_processing: '退款处理中', refund_closed: '退款已关闭', refund_abnormal: '退款异常，待核查',
}
export const billingStatusLabel = (status) => statuses[status] || status || '未知状态'
export const shouldPollOrder = (order) => ['creating', 'pending_payment'].includes(order?.status)
export const billingSessionKey = (session, admin = false) => `${session?.user?.id || session?.access_token || 'guest'}:${admin}`
export function walletDueNotice(wallet) {
  return Number.isSafeInteger(wallet?.dueUnits) && wallet.dueUnits > 0
    ? `当前有 ${formatCreditUnits(wallet.dueUnits)} 积分待补缴。充值会优先补缴已完成任务；补缴完成后才能发起新的收费任务。`
    : ''
}
export const billingLedgerKindLabel = kind => ({ payment: '在线充值', offline_recharge: '线下实收充值', offline_refund: '线下退款冲销', adjustment: '人工调账', gift: '赠送积分', compensation: '故障补偿', usage: '模型调用', due_payment: '补缴已完成任务' }[kind] || kind || '—')
export const billingLedgerLabel = item => item?.reason || billingLedgerKindLabel(item?.kind)
export const offlineRechargeUnits = amount => parseMoneyFen(amount)
const validRate = value => (Number.isSafeInteger(value) && value >= 0) || (typeof value === 'string' && /^(?:0|[1-9]\d{0,11})(?:\.\d{1,18})?$/.test(value) && Number(value) <= 1_000_000_000_000)
export const formatBillingRate = value => validRate(value) ? Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 6 }) : '—'
export const billingPricingNote = value => ({ cache_write_premium_waived: '缺少缓存写入明细，未加收写入差价', unconfirmed_long_context_premium_waived: '缺少单次请求上下文明细，未加收长上下文差价' }[value] || '用量详情待核对')
export function billingPublicPricing(value) {
  if (!value || typeof value.enabled !== 'boolean') throw new Error('公开费率响应无效，请刷新确认。')
  if (!value.enabled) return { ...value, versionId: null, rates: [], billableStatuses: [], insufficientBalancePolicy: null }
  const validRates = Array.isArray(value.rates) && value.rates.length > 0 && value.rates.every(row => typeof row?.provider === 'string' && row.provider.trim() && typeof row.model === 'string' && row.model.trim() && ['inputUnitsPerMillion', 'cachedInputUnitsPerMillion', 'outputUnitsPerMillion'].every(key => validRate(row[key])) && (!value.apiPricing || validRate(row.cacheWriteUnitsPerMillion)))
  if (!validRates || value.tokenUnit !== 1_000_000 || !value.versionId || !Array.isArray(value.billableStatuses) || !value.billableStatuses.length || !['deferred_due', 'cap_at_balance'].includes(value.insufficientBalancePolicy)) throw new Error('公开费率信息不完整，请刷新后核对。')
  if (value.apiPricing && (!validRate(value.apiPricing.usdCnyRate) || Number(value.apiPricing.usdCnyRate) <= 0 || !validRate(value.apiPricing.multiplier) || Number(value.apiPricing.multiplier) <= 0 || value.apiPricing.pointsPerCny !== 100 || value.apiPricing.serviceTier !== 'standard')) throw new Error('API 价格折算信息不完整，请刷新后核对。')
  return value
}
export const canReviewBillingRequest = request => ['requested', 'reviewing', 'processing'].includes(request?.status)
export function refundResultNotice(result) {
  return result?.request?.status === 'refunded' && result?.refund?.status === 'succeeded'
    ? '支付渠道已确认退款成功，订单积分已回收。'
    : result?.message || `退款当前状态：${billingStatusLabel(result?.request?.status)}。到账结果以支付渠道确认为准。`
}

export function paymentCheckout(order, now = Date.now(), onlinePaymentEnabled = true) {
  let url = ''
  try {
    const parsed = new URL(order?.checkout?.codeUrl)
    if (['https:', 'weixin:'].includes(parsed.protocol) && !parsed.username && !parsed.password) url = parsed.href
  } catch { /* An absent or invalid checkout must never become a payment link. */ }
  const expiry = Date.parse(order?.checkout?.expiresAt)
  const expired = Number.isFinite(expiry) && now >= expiry
  return { url: onlinePaymentEnabled ? url : '', expired, canPay: onlinePaymentEnabled === true && Boolean(url) && order?.provider !== 'offline' && order?.status === 'pending_payment' && !expired }
}

/** Share one pending operation for a logical action, including synchronous double clicks. */
export function createBillingActionGate() {
  const pending = new Map()
  return {
    has: (key) => pending.has(key),
    run(key, operation) {
      if (pending.has(key)) return pending.get(key)
      const result = Promise.resolve().then(operation).finally(() => { if (pending.get(key) === result) pending.delete(key) })
      pending.set(key, result)
      return result
    },
  }
}

function errorMessage(payload, fallback) {
  const detail = payload?.detail ?? payload
  if (typeof detail === 'string') return detail
  if (typeof detail?.message === 'string') return detail.message
  if (Array.isArray(detail)) return detail.map((item) => item?.msg).filter(Boolean).join('；') || fallback
  return fallback
}

export function createBillingClient({ apiBase = API_BASE, fetchImpl = globalThis.fetch, timeoutMs = 30_000 } = {}) {
  const base = `${apiBase.replace(/\/+$/, '')}/billing`
  async function request(path, { token, method = 'GET', body, signal, publicRequest = false } = {}) {
    if (!publicRequest && !token) throw Object.assign(new Error('请登录后查看积分与订单。'), { status: 401 })
    const controller = new AbortController()
    const cancel = () => controller.abort(signal?.reason)
    if (signal?.aborted) cancel()
    else signal?.addEventListener('abort', cancel, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
      const response = await fetchImpl(`${base}${path}`, {
        method, credentials: 'include', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      let payload
      try { payload = await response.json() } catch { throw Object.assign(new Error('积分服务返回了无效响应，请重试。'), { status: response.status }) }
      if (!response.ok) {
        const retryAfter = Number(response.headers?.get?.('retry-after')) || undefined
        const message = errorMessage(payload, '积分服务请求失败，请重试。')
        throw Object.assign(new Error(response.status === 429 && retryAfter ? `${message} 请在 ${retryAfter} 秒后重试。` : message), { status: response.status, payload, retryAfter })
      }
      return payload
    } catch (error) {
      if (timedOut && !signal?.aborted) throw new Error('积分服务请求超时，请刷新确认订单或账单状态后重试。')
      throw error
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', cancel)
    }
  }
  return {
    status: (signal) => request('/status', { publicRequest: true, signal }),
    pricing: async signal => billingPublicPricing(await request('/pricing', { publicRequest: true, signal })),
    packages: (token, { admin = false, signal } = {}) => request(admin ? '/admin/packages' : '/packages', { token, publicRequest: !admin, signal }),
    wallet: (token, signal) => request('/wallet', { token, signal }),
    dashboard: (token, signal) => request('/admin/dashboard', { token, signal }),
    list: (token, kind, { admin = false, offset = 0, limit = 20, signal, q, status, ownerId, dateFrom, dateTo, source } = {}) => {
      if (!['orders', 'ledger', 'settlements', 'refund-requests', 'invoice-requests', 'usage', 'audit'].includes(kind)) throw new Error('账单列表类型无效。')
      const query = new URLSearchParams({limit, offset}); if (admin) for (const [key, value] of Object.entries({q,status,ownerId,dateFrom,dateTo,source})) if (typeof value === 'string' && value.trim()) query.set(key,value.trim())
      return request(`${admin ? '/admin' : ''}/${kind}?${query}`, { token, signal })
    },
    createOrder: (token, packageId, idempotencyKey, signal) => request('/orders', { token, method: 'POST', body: { packageId, idempotencyKey }, signal }),
    order: (token, orderId, signal) => request(`/orders/${encodeURIComponent(orderId)}`, { token, signal }),
    reconcileOrder: (token, orderId, signal) => request(`/orders/${encodeURIComponent(orderId)}/reconcile`, { token, method: 'POST', body: {}, signal }),
    requestService: (token, kind, orderId, values, signal) => {
      if (!['refund', 'invoice'].includes(kind)) throw new Error('申请类型无效。')
      return request(`/orders/${encodeURIComponent(orderId)}/${kind}-requests`, { token, method: 'POST', body: values, signal })
    },
    savePackage: (token, values, packageId, signal) => request(`/admin/packages${packageId ? `/${encodeURIComponent(packageId)}` : ''}`, { token, method: packageId ? 'PATCH' : 'POST', body: values, signal }),
    adjust: (token, values, signal) => request('/admin/adjustments', { token, method: 'POST', body: values, signal }),
    offlineRecharge: (token, values, signal) => request('/admin/offline-recharges', { token, method: 'POST', body: values, signal }),
    offlineRefund: (token, orderId, values, signal) => request(`/admin/offline-recharges/${encodeURIComponent(orderId)}/refund`, { token, method: 'POST', body: values, signal }),
    reviewRequest: (token, requestId, values, signal) => request(`/admin/requests/${encodeURIComponent(requestId)}`, { token, method: 'PATCH', body: values, signal }),
    executeRefund: (token, requestId, signal) => request(`/admin/requests/${encodeURIComponent(requestId)}/refund`, { token, method: 'POST', body: {}, signal }),
    reconcileRefund: (token, requestId, signal) => request(`/admin/requests/${encodeURIComponent(requestId)}/refund/reconcile`, { token, method: 'POST', body: {}, signal }),
  }
}

export const billingClient = createBillingClient()
