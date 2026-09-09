import { API_BASE } from './api.js'

function errorMessage(payload, fallback) {
  const detail = payload?.detail || payload?.message || payload?.error
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg || item.message || String(item)).join('；')
  if (detail && typeof detail === 'object') return [detail.message, ...(detail.errors || []).map((item) => typeof item === 'string' ? item : item.message)].filter(Boolean).join('；') || fallback
  return fallback
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
  async getRun({ runId, signal, token }) {
    const response = await fetch(`${API_BASE}/cad-agent/runs/${encodeURIComponent(runId)}`, { signal, headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
    return readCadAgentEvents(response)
  },
  async waitForRun({ runId, signal, token, onProgress = () => {}, intervalMs = 2000 }) {
    while (true) {
      const result = await cadAgent.getRun({ runId, signal, token })
      if (result.status !== 'running') return result
      onProgress({ ...result.progress, runId: result.runId, revision: result.revision, status: 'running', ...(result.provider ? { provider: result.provider } : {}) })
      await new Promise((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(new DOMException('已停止等待', 'AbortError')) }
        const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, intervalMs)
        if (signal?.aborted) abort()
        else signal?.addEventListener('abort', abort, { once: true })
      })
    }
  },
  async run({ message, modelState = {}, history = [], files = [], signal, onEvent, token }) {
    const form = new FormData()
    form.append('message', message)
    form.append('modelState', JSON.stringify(modelState))
    form.append('history', JSON.stringify(history))
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
