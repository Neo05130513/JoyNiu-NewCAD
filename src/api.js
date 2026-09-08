/**
 * JoyNiu NewCAD API client.
 *
 * The browser can run in offline demo mode, but every production workflow
 * has a typed HTTP boundary so the same UI can use the FastAPI/CadQuery
 * service when it is available.
 */

import { readableError } from './workspaceFeedback.js'

export const API_BASE = (import.meta.env?.VITE_API_BASE || 'http://localhost:8010/api/v1').replace(/\/$/, '')

const fieldLabels = { email: '邮箱', password: '密码', displayName: '显示名称', display_name: '显示名称', roles: '角色', file: '文件', files: '文件', message: '消息' }
function validationMessage(value) {
  return String(value || '')
    .replace(/^Field required$/i, '此项必填')
    .replace(/^String should have at least (\d+) characters$/i, '至少需要 $1 个字符')
    .replace(/^String should have at most (\d+) characters$/i, '最多允许 $1 个字符')
    .replace(/^Input should be greater than (.+)$/i, '输入值必须大于 $1')
    .replace(/^Input should be less than (.+)$/i, '输入值必须小于 $1')
    .replace(/^Input should be a valid number.*$/i, '请输入有效数字')
    .replace(/^Input should be a valid integer.*$/i, '请输入整数')
    .replace(/^value is not a valid email address.*$/i, '请输入有效邮箱地址')
}

function responseMessage(payload) {
  if (typeof payload === 'string') return payload
  if (Array.isArray(payload)) return payload.map((item) => {
    const location = Array.isArray(item?.loc) ? item.loc.filter((part) => !['body', 'query', 'path'].includes(part)).map((part) => fieldLabels[part] || part).join('.') : ''
    const detail = validationMessage(item?.msg || responseMessage(item))
    return location ? `${location}：${detail}` : detail
  }).filter(Boolean).join('；')
  if (!payload || typeof payload !== 'object') return ''
  return responseMessage(payload.detail ?? payload.message ?? payload.msg ?? payload.error ?? '')
}

function responseError(payload, status) {
  const fallback = { 400: '请求参数不正确，请检查后重试。', 404: '请求的文件或记录不存在，请刷新后重试。', 409: '记录已发生变更或已存在，请刷新后重试。', 413: '文件超过服务允许的大小，请选择较小的文件。', 422: '输入内容未通过校验，请检查后重试。', 429: '请求过于频繁，请稍后重试。', 500: '服务处理失败，请稍后重试。', 502: '上游服务暂时不可用，请稍后重试。', 503: '服务暂时不可用，请稍后重试。', 504: '服务响应超时，请重试。' }
  const error = new Error(responseMessage(payload) || fallback[status] || '请求未完成，请重试。')
  error.status = status
  error.payload = payload
  error.message = readableError(error)
  return error
}

function transportError(error) {
  // Callers distinguish cancellation from failure by name and identity.
  if (error?.name === 'AbortError') return error
  const message = readableError(error)
  if (error instanceof Error && message === error.message) return error
  const localized = new Error(message, { cause: error })
  if (error?.name) localized.name = error.name
  if (error?.status !== undefined) localized.status = error.status
  if (error?.payload !== undefined) localized.payload = error.payload
  return localized
}

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
  } catch (error) {
    throw transportError(error)
  } finally {
    window.clearTimeout(timeout)
    externalSignal?.removeEventListener('abort', forwardAbort)
  }
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) {
    throw responseError(payload, response.status)
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
  }).catch((error) => { throw transportError(error) })
  if (!response.ok) {
    const contentType = response.headers.get('content-type') || ''
    const payload = contentType.includes('application/json') ? await response.json() : await response.text()
    throw responseError(payload, response.status)
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
    if (eventName === 'turn.error') {
      const error = responseError(payload, payload?.status)
      onEvent(eventName, { ...payload, message: error.message })
      throw error
    }
    onEvent(eventName, payload)
    if (eventName === 'turn.result') finalResult = payload
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
  } catch (error) {
    throw transportError(error)
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
  validateModel: (payload) => request('/models/validate', { method: 'POST', body: JSON.stringify(payload) }),
  generateModel: (payload, signal) => request('/models/generate', { method: 'POST', body: JSON.stringify(payload), signal }),
  artifactUrl: (artifactId, format = 'step') => absoluteUrl(`/api/artifacts/${encodeURIComponent(artifactId)}.${format}`),
  login: (email, password) => request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  createUser: (payload, token) => request('/auth/users', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  users: (token, includeInactive = false) => request(`/auth/users?include_inactive=${includeInactive ? 'true' : 'false'}`, { headers: authHeaders(token) }),
  me: (token) => request('/auth/me', { headers: authHeaders(token) }),
  projects: (token, includeArchived = false) => request(`/pdm/projects?include_archived=${includeArchived ? 'true' : 'false'}`, { headers: authHeaders(token) }),
  createProject: (payload, token) => request('/pdm/projects', { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  renameProject: (projectId, name, token) => request(`/pdm/projects/${encodeURIComponent(projectId)}`, { method: 'PATCH', body: JSON.stringify({ name }), headers: authHeaders(token) }),
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
