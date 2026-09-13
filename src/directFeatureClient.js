import { API_BASE } from './api.js'

export const featureWorkspaceBase = `${API_BASE.replace(/\/api\/v1\/?$/, '').replace(/\/$/, '')}/api/cad/features`
export function createDirectFeatureClient({ base = featureWorkspaceBase, fetchImpl = globalThis.fetch, pollIntervalMs = 1500, requestTimeoutMs = 30000 } = {}) {
  async function request(path, token, { method = 'GET', body, signal, blob = false, timeoutMs = requestTimeoutMs } = {}) {
    const bearer = typeof token === 'function' ? token() : token
    if (!bearer) throw new Error('请登录后保存和重建自己的特征设计。')
    const controller = new AbortController(), abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
    const response = await fetchImpl(`${base}${path}`, { method, signal: controller.signal, headers: { Authorization: `Bearer ${bearer}`, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) })
    if (!response.ok) {
      const value = await response.json().catch(() => ({}))
      throw Object.assign(new Error(typeof value.detail === 'string' ? value.detail : value.detail?.message || `特征服务请求失败（${response.status}）`), { status: response.status, featureId: value.detail?.featureId })
    }
    return await (blob ? response.blob() : response.json())
    } catch (error) {
      if (timedOut) throw new Error('读取特征服务超时；已保存的设计仍可重新打开。')
      throw error
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }
  async function build(token, record, signal) {
    const controller = new AbortController(), abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
    let timer, settled = false, lastError
    // One mutation only. A disconnected POST must be recovered by reading
    // its immutable next revision, never by repeating expensive construction.
    return new Promise((resolve, reject) => {
      const finish = (error, value) => {
        if (settled) return
        settled = true; clearTimeout(timer); clearTimeout(deadline)
        signal?.removeEventListener('abort', externalAbort)
        signal?.removeEventListener('abort', abort); controller.abort()
        error ? reject(error) : resolve(value)
      }
      const externalAbort = () => finish(signal?.reason || new DOMException('已停止等待', 'AbortError'))
      const deadline = setTimeout(() => finish(lastError || new Error('建模响应尚未恢复，请重新打开已保存设计查看结果。')), 240000)
      signal?.addEventListener('abort', externalAbort, { once: true })
      if (signal?.aborted) { externalAbort(); return }
      request(`/${encodeURIComponent(record.id)}/build`, token, { method: 'POST', body: { expectedRevision: record.revision }, signal: controller.signal, timeoutMs: 240000 })
        .then(value => finish(null, value)).catch(error => {
          if (settled) return
          if (error.status && error.status !== 409 && error.status < 500) finish(error)
          else lastError = error
        })
      const poll = async () => {
        if (settled) return
        try {
          const current = await request(`/${encodeURIComponent(record.id)}`, token, { signal: controller.signal, timeoutMs: 8000 })
          if (current.id !== record.id) return finish(new Error('设计编号不一致，已停止恢复。'))
          if (current.revision === record.revision + 1 && current.status === 'built') return finish(null, current)
          if (current.revision > record.revision) return finish(new Error('设计已有其他更新，请重新打开最新版本。'))
        } catch (error) { if (error.status === 401 || error.status === 403 || error.status === 404) return finish(error); lastError = error }
        if (!settled) timer = setTimeout(poll, pollIntervalMs)
      }
      timer = setTimeout(poll, pollIntervalMs)
    })
  }
  return {
    commit: (token, record, payload, signal) => request('/commit', token, { method: 'POST', body: { ...payload, ...(record ? { designId: record.id, expectedRevision: record.revision } : {}) }, signal, timeoutMs: 240000 }),
    preview: (token, payload, signal) => request('/preview', token, { method: 'POST', body: payload, signal, timeoutMs: 120000 }),
    list: (token, signal) => request('', token, { signal }),
    get: (token, id, revision, signal) => request(`/${encodeURIComponent(id)}${revision ? `?revision=${revision}` : ''}`, token, { signal }),
    versions: (token, id, signal) => request(`/${encodeURIComponent(id)}/versions`, token, { signal }),
    save: (token, record, payload, signal) => request(record ? `/${encodeURIComponent(record.id)}` : '', token, { method: record ? 'PUT' : 'POST', body: payload, signal }),
    build,
    download: (token, record, format, signal) => request(`/${encodeURIComponent(record.id)}/versions/${record.revision}/artifacts/${format}`, token, { signal, blob: true }),
  }
}
export const directFeatureClient = createDirectFeatureClient()
