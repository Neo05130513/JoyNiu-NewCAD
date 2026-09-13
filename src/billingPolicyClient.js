import { API_BASE } from './api.js'

export const billingPolicyStatuses = {
  ready: '建模完成', review_required: '需要复核', needs_input: '需要补充信息',
  failed: '失败', cancelled: '已取消', interrupted: '已中断',
}
export const insufficientBalancePolicies = { deferred_due: '记录待补缴积分', cap_at_balance: '最多扣除现有余额' }
export const policyRateFields = {
  inputUnitsPerMillion: '普通输入', cachedInputUnitsPerMillion: '缓存输入', outputUnitsPerMillion: '输出（含推理）',
}

export const emptyBillingPolicyDraft = () => ({ rates: [], insufficientBalancePolicy: '', billableStatuses: [], reason: '' })
export const emptyBillingRate = () => ({ provider: '', model: '', inputUnitsPerMillion: '', cachedInputUnitsPerMillion: '', outputUnitsPerMillion: '' })

export function parsePricingDecimal(value, label = '计费数值') {
  const text = String(value ?? '').trim()
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(text) || Number(text) <= 0 || Number(text) > 100) throw new Error(`${label}须大于 0、不超过 100，最多 6 位小数。`)
  return text
}

export function officialBillingPolicyDraft(catalog, version) {
  const source = version?.apiPricing
  return {
    apiPricing: { catalogVersion: catalog.version, usdCnyRate: source?.usdCnyRate || '7', multiplier: source?.multiplier || '1', pointsPerCny: 100 },
    rates: version?.apiPricing ? version.rates.map(({ provider, model }) => ({ provider, model })) : catalog.models.map(({ model }) => ({ provider: 'codex', model })),
    insufficientBalancePolicy: 'deferred_due', billableStatuses: ['ready', 'review_required'], reason: '',
  }
}

export const formatPolicyRate = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 8 }) : '—'

export function parseBillingRate(value) {
  const text = String(value ?? '').trim()
  if (!/^(?:0|[1-9]\d*)$/.test(text)) throw new Error('每百万 token 的积分费率须明确填写非负整数；免费项请填写 0。')
  const result = Number(text)
  if (!Number.isSafeInteger(result) || result > 1_000_000_000_000) throw new Error('积分费率超出允许范围。')
  return result
}

function reasonText(value) {
  const reason = String(value ?? '').trim()
  if (!reason || reason.length > 500) throw new Error('请填写变更原因，最多 500 字。')
  return reason
}

export function billingPolicyDraftPayload(draft) {
  if (!Array.isArray(draft?.rates) || draft.rates.length > 100) throw new Error('最多配置 100 项模型费率。')
  const seen = new Set()
  const rates = draft.rates.map(row => {
    const provider = String(row.provider ?? '').trim()
    const model = String(row.model ?? '').trim()
    if (!provider || !model || provider.length > 100 || model.length > 128 || provider.includes('*') || model.includes('*')) throw new Error('请填写准确的供应商和模型名称，不能使用通配符。')
    const key = JSON.stringify([provider, model])
    if (seen.has(key)) throw new Error('同一供应商和模型只能配置一项费率。')
    seen.add(key)
    return { provider, model, ...(draft.apiPricing ? {} : Object.fromEntries(Object.keys(policyRateFields).map(field => [field, parseBillingRate(row[field])]))) }
  })
  const insufficientBalancePolicy = draft.insufficientBalancePolicy || null
  if (insufficientBalancePolicy !== null && !Object.hasOwn(insufficientBalancePolicies, insufficientBalancePolicy)) throw new Error('余额不足策略无效。')
  if (!Array.isArray(draft.billableStatuses) || draft.billableStatuses.some(value => !Object.hasOwn(billingPolicyStatuses, value)) || new Set(draft.billableStatuses).size !== draft.billableStatuses.length) throw new Error('请选择有效且不重复的收费终态。')
  const result = { rates, insufficientBalancePolicy, billableStatuses: [...draft.billableStatuses], reason: reasonText(draft.reason) }
  if (draft.apiPricing) {
    if (draft.apiPricing.pointsPerCny !== 100 || typeof draft.apiPricing.catalogVersion !== 'string' || !draft.apiPricing.catalogVersion) throw new Error('官方价格目录或充值换算无效，请刷新。')
    result.apiPricing = { catalogVersion: draft.apiPricing.catalogVersion, pointsPerCny: 100,
      usdCnyRate: parsePricingDecimal(draft.apiPricing.usdCnyRate, '结算汇率'), multiplier: parsePricingDecimal(draft.apiPricing.multiplier, '收费倍率') }
  }
  return result
}

export function billingPolicyEnableBlock(version, overview) {
  if (!version?.id) return '请先选择一个已保存的规则版本。'
  try { billingPolicyDraftPayload(version) } catch (error) { return error.message }
  if (!version.rates.length) return '尚未配置模型费率。'
  if (!version.billableStatuses.length) return '尚未选择收费终态。'
  if (!version.insufficientBalancePolicy) return '尚未选择余额不足策略。'
  if (!overview?.supportedInsufficientBalancePolicies?.includes(version.insufficientBalancePolicy)) return '服务端尚未支持所选余额不足策略，当前不能启用计费。'
  return ''
}

export function billingPolicyStatePayload({ overview, version, enabled, reason, confirmed = false }) {
  if (!Number.isSafeInteger(overview?.revision) || overview.revision < 0) throw new Error('规则状态尚未加载，请刷新后重试。')
  if (typeof enabled !== 'boolean') throw new Error('计费启停状态无效。')
  if (enabled) {
    const blocked = billingPolicyEnableBlock(version, overview)
    if (blocked) throw new Error(blocked)
    if (!confirmed) throw new Error('请确认启用所选规则的实际积分结算。')
  }
  return { revision: overview.revision, versionId: version?.id || null, enabled, reason: reasonText(reason) }
}

export function validateBillingPolicyOverview(value) {
  if (!value || !Number.isSafeInteger(value.revision) || value.revision < 0 || typeof value.enabled !== 'boolean' || typeof value.configured !== 'boolean' || !Array.isArray(value.versions) || !Array.isArray(value.supportedInsufficientBalancePolicies)) throw new Error('计费规则响应无效，请刷新重试。')
  return value
}

const settlementStatuses = { settled: '已结算', not_charged: '未收费', pending_settlement: '待结算', needs_review: '待核查', disabled: '未启用' }
export const settlementStatusLabel = value => settlementStatuses[value] || value || '未知状态'
const settlementReasons = {
  disabled_at_start: '本次操作未启用计费', outcome_excluded_by_policy: '该终态不在收费范围',
  call_roster_mismatch: '调用清单待核对', model_rate_missing: '模型费率缺失', supplier_usage_unknown: '供应商用量待确认',
  charging_disabled: '计费已关闭', insufficient_balance_policy_unavailable: '余额不足策略尚不可用',
  insufficient_balance_policy_unresolved: '余额不足处理待确认',
}
export const settlementReasonLabel = value => settlementReasons[value] || value || '—'

export function createBillingPolicyClient({ apiBase = API_BASE, fetchImpl = globalThis.fetch, timeoutMs = 30_000 } = {}) {
  const base = `${apiBase.replace(/\/+$/, '')}/billing`
  async function request(path, token, { method = 'GET', body, signal } = {}) {
    if (!token) throw Object.assign(new Error('请登录管理员账号后继续。'), { status: 401 })
    const controller = new AbortController()
    const cancel = () => controller.abort(signal?.reason)
    if (signal?.aborted) cancel()
    else signal?.addEventListener('abort', cancel, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
      const response = await fetchImpl(`${base}${path}`, {
        method, credentials: 'include', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      const payload = await response.json()
      if (!response.ok) {
        const detail = payload?.detail ?? payload
        throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message || '计费规则请求失败，请重试。'), { status: response.status, payload })
      }
      return payload
    } catch (error) {
      if (timedOut && !signal?.aborted) throw new Error('计费规则请求超时，请刷新确认服务端状态后重试。')
      throw error
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', cancel) }
  }
  return {
    overview: async (token, signal) => validateBillingPolicyOverview(await request('/admin/policy', token, { signal })),
    saveVersion: (token, values, signal) => request('/admin/policy/versions', token, { method: 'POST', body: billingPolicyDraftPayload(values), signal }),
    setState: (token, values, signal) => request('/admin/policy', token, { method: 'PATCH', body: values, signal }),
    settlements: (token, { admin = true, offset = 0, limit = 20, signal } = {}) => request(`${admin ? '/admin' : ''}/settlements?limit=${limit}&offset=${offset}`, token, { signal }),
  }
}

export const billingPolicyClient = createBillingPolicyClient()
