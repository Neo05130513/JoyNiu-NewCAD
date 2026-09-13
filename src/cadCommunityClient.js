import { API_BASE } from './api.js'

export const cadCommunityBase = `${API_BASE.replace(/\/api\/v1\/?$/, '').replace(/\/$/, '')}/api/cad/community`
export function createCadCommunityClient({ base = cadCommunityBase, fetchImpl = globalThis.fetch } = {}) {
  async function request(path, token, { body, signal, blob = false } = {}) {
    const bearer = typeof token === 'function' ? token() : token
    const controller = new AbortController(), abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, body ? 180000 : 30000)
    try {
      const response = await fetchImpl(base + path, { method: body ? 'POST' : 'GET', signal: controller.signal,
        headers: { ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) })
      if (blob && response.ok) return response.blob()
      const value = await response.json().catch(() => ({}))
      if (!response.ok) throw Object.assign(new Error(typeof value.detail === 'string' ? value.detail : value.detail?.message || `社区请求失败（${response.status}）`), { status: response.status })
      return value
    } catch (error) { if (timedOut) throw new Error('社区响应超时，可使用相同请求重试。'); throw error }
    finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }
  return {
    list: (token, { query = '', mine = false } = {}, signal) => request(`?q=${encodeURIComponent(query)}&mine=${mine ? 'true' : 'false'}`, token, { signal }),
    publish: (token, body, signal) => request('', token, { body, signal }),
    get: (token, resourceId, signal) => request(`/${encodeURIComponent(resourceId)}`, token, { signal }),
    withdraw: (token, resourceId, signal) => request(`/${encodeURIComponent(resourceId)}/withdraw`, token, { body: {}, signal }),
    open: (token, resourceId, body, signal) => request(`/${encodeURIComponent(resourceId)}/open`, token, { body, signal }),
    artifact: (token, resourceId, kind, signal) => request(`/${encodeURIComponent(resourceId)}/artifacts/${encodeURIComponent(kind)}`, token, { signal, blob: true }),
  }
}
export const cadCommunityClient = createCadCommunityClient()
