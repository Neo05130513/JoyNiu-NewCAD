import { API_BASE } from './api.js'
import { awaitSourceDimensionLocations } from './sourceDimensionRequest.js'
import { createClientId } from './clientId.js'

function errorMessage(payload, fallback) {
  const detail = payload?.detail || payload?.message || payload?.error
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg || item.message || String(item)).join('；')
  if (detail && typeof detail === 'object') return [detail.message, ...(detail.errors || []).map((item) => typeof item === 'string' ? item : item.message)].filter(Boolean).join('；') || fallback
  return fallback
}

async function requestCadJson(url, options, timeoutMs = 30000) {
  const controller = new AbortController()
  const abort = () => controller.abort(options.signal?.reason)
  if (options.signal?.aborted) abort()
  else options.signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
  try {
    return await readCadAgentEvents(await fetch(url, { ...options, signal: controller.signal }))
  } catch (error) {
    if (timedOut && !options.signal?.aborted) throw new Error('读取任务服务超时，后台任务状态未改变，请稍后刷新。')
    throw error
  } finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort) }
}

export function cadArtifactUrl(artifact) {
  const path = artifact?.url || artifact?.downloadUrl || artifact?.download_url
  if (!path) return ''
  try {
    const url = new URL(path, `${API_BASE}/`)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : ''
  } catch { return '' }
}

export async function readCadAgentEvents(response, onEvent = () => {}) {
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw Object.assign(new Error(errorMessage(payload, `CAD 服务请求失败（${response.status}）`)), { status: response.status })
  }
  if (!response.headers.get('content-type')?.includes('text/event-stream')) return response.json()
  if (!response.body) throw new Error('CAD 服务未提供流式响应')
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = '', result = null
  const consume = (block) => {
    const lines = block.split(/\r?\n/)
    const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message'
    const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n')
    if (!data) return
    let payload
    try { payload = JSON.parse(data) } catch { throw new Error('CAD 服务返回了不完整的事件数据') }
    onEvent(event, payload)
    if (event === 'error' || event.endsWith('.error')) throw new Error(errorMessage(payload, 'CAD 建模未完成'))
    if (event === 'result' || event.endsWith('.result')) result = payload
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        consume(block)
        // The complete result is already durable on the server. A later
        // socket reset must not turn that completed operation into a failure.
        if (result) { await reader.cancel().catch(() => {}); return result }
      }
      if (done) break
    }
    if (buffer.trim()) consume(buffer)
  } catch (error) {
    await reader.cancel().catch(() => {})
    throw error
  } finally { reader.releaseLock() }
  if (!result) throw new Error('CAD 建模连接已结束，但没有收到结果。请重试本轮。')
  return result
}

export const cadAgent = {
  async jobs({ token, signal, limit = 20, offset = 0 }) {
    return requestCadJson(`${API_BASE}/cad-agent/jobs?limit=${limit}&offset=${offset}`, { signal, headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
  },
  async byRequest({ token, signal, requestId }) {
    return requestCadJson(`${API_BASE}/cad-agent/jobs/by-request/${encodeURIComponent(requestId)}`, { signal, headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
  },
  async cancel({ token, signal, runId }) {
    return requestCadJson(`${API_BASE}/cad-agent/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', signal, headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
  },
  async cancelRequest({ token, signal, requestId }) {
    return requestCadJson(`${API_BASE}/cad-agent/jobs/by-request/${encodeURIComponent(requestId)}/cancel`, { method: 'POST', signal, headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
  },
  async revokeFileLinks({ token, signal, runId }) {
    return requestCadJson(`${API_BASE}/cad-agent/runs/${encodeURIComponent(runId)}/revoke-file-links`, { method: 'POST', signal, headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: '{}' })
  },
  async downloadArtifact({ token, signal, runId, revision, format, artifactId }) {
    const result = await cadAgent.getRun({ token, signal, runId, revision })
    if (result.runId !== runId || result.revision !== revision) throw new Error('服务返回了其他模型版本，下载已停止。')
    const artifact = result.artifacts?.find(item => artifactId ? item.id === artifactId : item.format === format)
    if (!artifact) throw new Error('此版本没有可下载的交付文件，请重新核对模型状态。')
    const url = new URL(cadArtifactUrl(artifact))
    const base = new URL(API_BASE)
    if (url.origin !== base.origin || !url.pathname.startsWith(`${base.pathname}/cad-agent/runs/${encodeURIComponent(runId)}/${revision}/artifacts/`)) throw new Error('交付文件地址与当前服务不一致，下载已停止。')
    const credential = typeof token === 'function' ? token() : token
    const controller = new AbortController()
    const abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
    const timer = setTimeout(() => controller.abort(), 120000)
    try {
      const response = await fetch(url.href, { signal: controller.signal, headers: { ...(credential ? { Authorization: `Bearer ${credential}` } : {}) } })
      if (!response.ok) throw new Error(`交付文件读取失败（${response.status}），请重新登录或刷新任务后重试。`)
      return { blob: await response.blob(), mimeType: response.headers.get('content-type'), artifact }
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  },
  async waitForSourceDimensions({ runId, revision, force = false, signal, token, onProgress, resume }) {
    const active = !force && resume?.status === 'running' && resume.runId === runId && resume.revision === revision && resume.operationId ? resume : null
    const operationId = active?.operationId || createClientId()
    return awaitSourceDimensionLocations({ runId, revision, force: force || Boolean(active), operationId, signal, onProgress,
      start: ({ signal, operationId }) => active || cadAgent.locateSourceDimensions({ runId, revision, force, signal, token, operationId }),
      read: ({ signal }) => cadAgent.getRun({ runId, revision, signal, token }),
    })
  },
  async locateSourceDimensions({ runId, revision, force = false, signal, token, operationId }) {
    const response = await fetch(`${API_BASE}/cad-agent/runs/${encodeURIComponent(runId)}/source-locations`, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ ...(Number.isInteger(revision) ? { revision } : {}), force, ...(operationId ? { operationId } : {}) }),
    })
    return readCadAgentEvents(response)
  },
  async getRun({ runId, revision, signal, token }) {
    token = typeof token === 'function' ? token() : token
    const query = Number.isInteger(revision) ? `?revision=${revision}` : ''
    return requestCadJson(`${API_BASE}/cad-agent/runs/${encodeURIComponent(runId)}${query}`, { signal, headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
  },
  async waitForRun({ runId, signal, token, onProgress = () => {}, intervalMs = 2000 }) {
    while (true) {
      const result = await cadAgent.getRun({ runId, signal, token })
      if (!['queued', 'running', 'cancel_requested'].includes(result.status)) return result
      onProgress({ ...result.progress, runId: result.runId, jobId: result.jobId, attemptId: result.attemptId, revision: result.revision, status: result.status, ...(result.provider ? { provider: result.provider } : {}) })
      await new Promise((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(new DOMException('已停止等待', 'AbortError')) }
        const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, intervalMs)
        if (signal?.aborted) abort()
        else signal?.addEventListener('abort', abort, { once: true })
      })
    }
  },
  async run({ message, modelState = {}, history = [], files = [], signal, onEvent, token, requestId = createClientId() }) {
    const form = new FormData()
    form.append('message', message)
    form.append('modelState', JSON.stringify(modelState))
    form.append('history', JSON.stringify(history))
    form.append('requestId', requestId)
    files.forEach((file) => form.append('files', file))
    const response = await fetch(`${API_BASE}/cad-agent/run`, { method: 'POST', body: form, signal, headers: { Accept: 'text/event-stream', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
    return readCadAgentEvents(response, onEvent)
  },
  async confirm({ runId, revision, parameters, signal, token }) {
    const response = await fetch(`${API_BASE}/cad-agent/confirm`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ runId, revision, ...(parameters ? { parameters } : {}) }),
    })
    return readCadAgentEvents(response)
  },
}
