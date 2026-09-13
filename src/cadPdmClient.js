import { API_BASE } from './api.js'

export const cadPdmBase = `${API_BASE.replace(/\/api\/v1\/?$/, '').replace(/\/$/, '')}/api/cad/designs/pdm`
export function createCadPdmClient({ base = cadPdmBase, fetchImpl = globalThis.fetch } = {}) {
  async function request(path, token, { body, signal } = {}) {
    const bearer = typeof token === 'function' ? token() : token
    if (!bearer) throw new Error('请登录后使用 PDM 工程库。')
    const controller = new AbortController(), abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, body ? 150000 : 20000)
    try {
    const response = await fetchImpl(base + path, { method: body ? 'POST' : 'GET', signal: controller.signal, headers: { Authorization: `Bearer ${bearer}`, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) })
    const value = await response.json().catch(() => ({}))
    if (!response.ok) throw Object.assign(new Error(typeof value.detail === 'string' ? value.detail : value.detail?.message || `PDM 请求失败（${response.status}）`), { status: response.status })
    return value
    } catch (error) { if (timedOut) throw new Error('PDM 响应超时，请刷新或重试；相同版本不会重复保存。'); throw error }
    finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }
  return {
    projects: (token, signal) => request('/projects', token, { signal }),
    createProject: (token, name, signal) => request('/projects', token, { body: { name }, signal }),
    documents: (token, projectId, signal) => request(`/projects/${encodeURIComponent(projectId)}/documents`, token, { signal }),
    save: (token, body, signal) => request('/versions', token, { body, signal }),
    get: (token, versionId, signal) => request(`/versions/${encodeURIComponent(versionId)}`, token, { signal }),
    open: (token, versionId, body, signal) => request(`/versions/${encodeURIComponent(versionId)}/open`, token, { body, signal }),
  }
}
export const cadPdmClient = createCadPdmClient()
