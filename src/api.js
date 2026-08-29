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
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...fetchOptions,
      signal: fetchOptions.signal || controller.signal,
      headers: { Accept: 'application/json', ...(fetchOptions.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...(fetchOptions.headers || {}) },
    })
  } finally {
    window.clearTimeout(timeout)
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

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function absoluteUrl(path) {
  try { return new URL(path, `${API_BASE}/`).toString() } catch { return path }
}

export const api = {
  health: () => request('/health'),
  recognizeDrawing: (file) => { const form = new FormData(); form.append('file', file); return request('/drawings/recognize', { method: 'POST', body: form }) },
  confirmDrawing: (drawingId, payload = {}, token) => request(`/drawings/${encodeURIComponent(drawingId)}/confirm`, { method: 'POST', body: JSON.stringify(payload), headers: authHeaders(token) }),
  validateBracket: (parameters) => request('/brackets/validate', { method: 'POST', body: JSON.stringify(parameters) }),
  generateBracket: (parameters) => request('/brackets/generate', { method: 'POST', body: JSON.stringify(parameters) }),
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
