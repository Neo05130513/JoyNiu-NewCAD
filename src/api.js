/**
 * JoyNiu NewCAD API client.
 *
 * The browser can run in offline demo mode, but every production workflow
 * has a typed HTTP boundary so the same UI can use the FastAPI/CadQuery
 * service when it is available.
 */

export const API_BASE = (import.meta.env.VITE_API_BASE || 'http://localhost:8010/api/v1').replace(/\/$/, '')

async function request(path, options = {}) {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), options.timeoutMs || 45_000)
  const { timeoutMs: _timeoutMs, ...fetchOptions } = options
  const externalSignal = fetchOptions.signal
  const forwardAbort = () => controller.abort(externalSignal?.reason)
  if (externalSignal?.aborted) forwardAbort()
  else externalSignal?.addEventListener('abort', forwardAbort, { once: true })
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...fetchOptions,
      signal: controller.signal,
      headers: { Accept: 'application/json', ...(fetchOptions.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...(fetchOptions.headers || {}) },
    })
  } finally {
    window.clearTimeout(timeout)
    externalSignal?.removeEventListener('abort', forwardAbort)
  }
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = typeof payload === 'string' ? payload : payload?.detail || payload?.message
    const error = new Error(detail || `API ${response.status}`)
    error.status = response.status
    error.payload = payload
    throw error
  }
  return payload
}

async function streamRequest(path, options = {}, onEvent = () => {}) {
  const { timeoutMs: _timeoutMs, ...fetchOptions } = options
  const response = await fetch(`${API_BASE}${path}`, {
    ...fetchOptions,
    headers: {
      Accept: 'text/event-stream',
      ...(fetchOptions.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(fetchOptions.headers || {}),
    },
  })
  if (!response.ok) {
    const contentType = response.headers.get('content-type') || ''
    const payload = contentType.includes('application/json') ? await response.json() : await response.text()
    const detail = typeof payload === 'string' ? payload : payload?.detail || payload?.message
    const error = new Error(detail || `API ${response.status}`)
    error.status = response.status
    error.payload = payload
    throw error
  }
  if (!response.body) throw new Error('浏览器未提供流式响应体')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResult = null

  const consumeBlock = (block) => {
    let eventName = 'message'
    const dataLines = []
    block.split(/\r?\n/).forEach((line) => {
      if (line.startsWith('event:')) eventName = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    })
    if (!dataLines.length) return
    let payload
    try { payload = JSON.parse(dataLines.join('\n')) } catch { throw new Error('AI 流返回了无效事件') }
    onEvent(eventName, payload)
    if (eventName === 'turn.result') finalResult = payload
    if (eventName === 'turn.error') {
      const error = new Error(payload?.message || 'AI 对话处理失败')
      error.status = payload?.status
      throw error
    }
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() || ''
      blocks.forEach(consumeBlock)
      if (done) break
    }
    if (buffer.trim()) consumeBlock(buffer)
  } finally {
    reader.releaseLock()
  }
  if (!finalResult) throw new Error('AI 流在返回最终结果前结束')
  return finalResult
}

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function absoluteUrl(path) {
  try { return new URL(path, `${API_BASE}/`).toString() } catch { return path }
}

// FileList/DataTransfer.files are array-like but not Array instances. Keep
// this boundary tolerant so callers can pass a picker result, an Array, or a
// single File without accidentally serializing the collection object itself.
function normalizeFilesInput(input) {
  if (!input || typeof input === 'string') return []
  if (Array.isArray(input)) return input.filter(Boolean)
  if (typeof input === 'object') {
    if (input.files && input.files !== input) return normalizeFilesInput(input.files)
    const fileLike = typeof input.name === 'string' || typeof input.arrayBuffer === 'function'
    const length = Number(input.length)
    if (!fileLike && Number.isFinite(length) && length >= 0) {
      try { return Array.from(input).filter(Boolean) } catch { /* fall through to iterable/single-item handling */ }
    }
    if (!fileLike && typeof Symbol !== 'undefined') {
      try {
        if (typeof input[Symbol.iterator] === 'function') return Array.from(input).filter(Boolean)
      } catch { /* malformed iterables should not break request construction */ }
    }
  }
  return [input].filter(Boolean)
}

export const api = {
  health: () => request('/health'),
  recognizeDrawing: (file, signal) => { const form = new FormData(); form.append('file', file); return request('/drawings/recognize', { method: 'POST', body: form, signal }) },
  aiStatus: () => request('/ai/status'),
  aiConversation: (message, fileOrFiles, modelState = {}, previousResponseId = '', token) => {
    const form = new FormData()
    if (message) form.append('message', message)
    form.append('model_state_json', JSON.stringify(modelState || {}))
    if (previousResponseId) form.append('previous_response_id', previousResponseId)
    const files = normalizeFilesInput(fileOrFiles)
    files.forEach((file, index) => form.append(index === 0 ? 'file' : 'files', file))
    return request('/ai/conversation', { method: 'POST', body: form, headers: authHeaders(token), timeoutMs: 120_000 })
  },
  aiConversationStream: (message, fileOrFiles, modelState = {}, history = [], previousResponseId = '', token, onEvent, signal) => {
    const form = new FormData()
    if (message) form.append('message', message)
    form.append('model_state_json', JSON.stringify(modelState || {}))
    form.append('history_json', JSON.stringify(history || []))
    if (previousResponseId) form.append('previous_response_id', previousResponseId)
    const files = normalizeFilesInput(fileOrFiles)
    files.forEach((file, index) => form.append(index === 0 ? 'file' : 'files', file))
    return streamRequest('/ai/conversation/stream', {
      method: 'POST',
      body: form,
      headers: authHeaders(token),
      signal,
    }, onEvent)
  },
  confirmDrawing: (drawingId, payload = {}, token) => request(`/drawings/${encodeURIComponent(drawingId)}/confirm`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  // Customer-facing acknowledgement of OCR candidates.  Unlike the legacy
  // reviewer-only confirm endpoint this endpoint accepts the edited model
  // parameters as overrides and can be called by any authenticated user.
  acceptDrawing: (drawingId, payload = {}, token) => request(`/drawings/${encodeURIComponent(drawingId)}/accept`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  validateBracket: (parameters) => request('/brackets/validate', { method: 'POST', body: JSON.stringify(parameters) }),
  generateBracket: (parameters, signal) => request('/brackets/generate', { method: 'POST', body: JSON.stringify(parameters), signal }),
  artifactUrl: (artifactId, format = 'step') => absoluteUrl(`/api/artifacts/${encodeURIComponent(artifactId)}.${format}`),
  login: (email, password) => request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  createUser: (payload, token) => request('/auth/users', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  users: (token, includeInactive = false) => request(`/auth/users?include_inactive=${includeInactive ? 'true' : 'false'}`, { headers: authHeaders(token) }),
  me: (token) => request('/auth/me', { headers: authHeaders(token) }),
  projects: (token, includeArchived = false) => request(`/pdm/projects?include_archived=${includeArchived ? 'true' : 'false'}`, { headers: authHeaders(token) }),
  createProject: (payload, token) => request('/pdm/projects', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  projectManifest: (projectId, token) => request(`/pdm/projects/${encodeURIComponent(projectId)}/manifest`, { headers: authHeaders(token) }),
  createDocument: (projectId, payload, token) => request(`/pdm/projects/${encodeURIComponent(projectId)}/documents`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  createVersion: (documentId, payload, token) => request(`/pdm/documents/${encodeURIComponent(documentId)}/versions`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  ocrAnalyze: (payload, token) => request('/ocr/analyze', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  ocrConfirm: (recognitionId, payload = {}, token) => request(`/ocr/${encodeURIComponent(recognitionId)}/confirm`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  drawingToModel: (payload, token) => request('/workflows/drawing-to-model', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token), timeoutMs: 120_000 }),
  camPlan: (payload, token) => request('/cam/plans', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  camPlans: (token, projectId = '') => request(`/cam/plans${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`, { headers: authHeaders(token) }),
  camPlanById: (planId, token) => request(`/cam/plans/${encodeURIComponent(planId)}`, { headers: authHeaders(token) }),
  camOperation: (planId, payload, token) => request(`/cam/plans/${encodeURIComponent(planId)}/operations`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  camSimulate: (planId, payload = {}, token) => request(`/cam/plans/${encodeURIComponent(planId)}/simulate`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  camSimulation: (simulationId, token) => request(`/cam/simulations/${encodeURIComponent(simulationId)}`, { headers: authHeaders(token) }),
  camGate: (planId, token) => request(`/cam/plans/${encodeURIComponent(planId)}/gate`, { headers: authHeaders(token) }),
  camApprove: (planId, payload = {}, token) => request(`/cam/plans/${encodeURIComponent(planId)}/approve`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  camRelease: (planId, payload = {}, token) => request(`/cam/plans/${encodeURIComponent(planId)}/release`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  camNcInfo: (programId, token) => request(`/cam/nc/${encodeURIComponent(programId)}/info`, { headers: authHeaders(token) }),
  camNcText: (programId, token) => request(`/cam/nc/${encodeURIComponent(programId)}`, { headers: authHeaders(token) }),
  camNcDownloadUrl: (programId) => absoluteUrl(`/api/v1/cam/nc/${encodeURIComponent(programId)}`),
  projectMembers: (projectId, token) => request(`/pdm/projects/${encodeURIComponent(projectId)}/members`, { headers: authHeaders(token) }),
  updateProjectMembers: (projectId, members, token) => request(`/pdm/projects/${encodeURIComponent(projectId)}/members`, { method: 'PUT', body: JSON.stringify({ members }), headers: authHeaders(token) }),
  audit: (token, resourceId = '') => request(`/audit${resourceId ? `?resource_id=${encodeURIComponent(resourceId)}` : ''}`, { headers: authHeaders(token) }),
}
