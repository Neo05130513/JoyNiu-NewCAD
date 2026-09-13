import { API_BASE } from './api.js'

async function request(path, { token, body, signal, method = 'GET' } = {}) {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
  let expired = false
  const timer = setTimeout(() => { expired = true; controller.abort() }, 30000)
  try {
    const response = await fetch(`${API_BASE}/commercial${path}`, { method, signal: controller.signal, credentials: 'include',
      headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}) })
    const value = await response.json().catch(() => null)
    if (!response.ok) throw Object.assign(new Error(typeof value?.detail === 'string' ? value.detail : value?.detail?.message || '条款操作未完成，请刷新后重试。'), { status: response.status })
    return value
  } catch (error) {
    if (expired && !signal?.aborted) throw new Error('条款服务响应超时，请重试；未确认操作不会在页面显示为成功。')
    throw error
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
}

export const commercialTerms = {
  read: signal => request('/terms', { signal }),
  acceptance: (token, signal) => request('/acceptance', { token, signal }),
  accept: (token, documentIds, idempotencyKey, signal) => request('/acceptance', { token, signal, method: 'POST', body: { documentIds, idempotencyKey } }),
  publish: (token, body, signal) => request('/admin/terms', { token, body, signal, method: 'POST' }),
}
