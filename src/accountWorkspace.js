/** Account isolation and cloud revision policy. No storage or global session reads. */

const STORAGE_PREFIX = 'joyniu-workspace-account-v1'
const credentialKey = /^(?:access_?token|refresh_?token|session_?token|login_?token|auth_?token|authorization|password|api_?key)$/i
const sessionContainers = new Set(['platform', 'session', 'auth'])
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const validRevision = (value) => Number.isSafeInteger(value) && value >= 0

export function emptyAccountWorkspace() {
  return { schemaVersion: 1, projects: [], activeProjectId: null, activeFileId: null }
}

function isWorkspace(value) {
  return isObject(value) && value.schemaVersion === 1 && Array.isArray(value.projects)
    && value.projects.every((project) => isObject(project) && typeof project.id === 'string' && project.id && Array.isArray(project.files))
}

/** Strip login credentials while keeping scoped artifact tokens and all CAD data. */
export function sanitizeAccountWorkspace(snapshot) {
  if (!isWorkspace(snapshot)) throw new TypeError('工作区必须是有效的 ProjectStore。')
  const seen = new WeakSet()
  function copy(value, path = [], depth = 0) {
    if (depth > 128) throw new TypeError('工作区 JSON 嵌套过深。')
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) throw new TypeError('工作区不能包含非有限数字。')
      return value
    }
    if (typeof value !== 'object') return undefined
    if (value instanceof Date) return value.toISOString()
    if (typeof Blob !== 'undefined' && value instanceof Blob) return undefined
    if (seen.has(value)) throw new TypeError('工作区不能包含循环引用。')
    seen.add(value)
    const result = Array.isArray(value) ? value.map((child) => copy(child, path, depth + 1) ?? null)
      : Object.fromEntries(Object.entries(value).flatMap(([key, child]) => {
        if (credentialKey.test(key) || (key.toLowerCase() === 'token' && (!path.length || sessionContainers.has(path.at(-1).toLowerCase())))) return []
        const clean = copy(child, [...path, key], depth + 1)
        return clean === undefined ? [] : [[key, clean]]
      }))
    seen.delete(value)
    return result
  }
  return copy(snapshot)
}

function normalizedApiBase(apiBase) {
  if (typeof apiBase !== 'string' || !apiBase.trim()) throw new TypeError('工作区存储需要 API 地址。')
  const url = new URL(apiBase)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new TypeError('API 地址必须是无凭据的 HTTP(S) 地址。')
  return `${url.origin}${url.pathname.replace(/\/+$/, '')}`
}

/** Origin and API path are included so separate deployments cannot share a cache. */
export function accountWorkspaceStorageKey({ apiBase, userId = '', authenticated = true }) {
  const identity = authenticated !== false && typeof userId === 'string' && userId.trim() ? `user:${encodeURIComponent(userId)}` : 'guest'
  return `${STORAGE_PREFIX}:${encodeURIComponent(normalizedApiBase(apiBase))}:${identity}`
}

export function accountWorkspaceTransition(previous, next) {
  const scopeKey = accountWorkspaceStorageKey(next)
  const previousKey = previous ? accountWorkspaceStorageKey(previous) : null
  const changed = scopeKey !== previousKey
  const authenticated = next.authenticated !== false && typeof next.userId === 'string' && Boolean(next.userId.trim())
  return { scopeKey, changed, clearWorkspace: changed, loadCloud: changed && authenticated, allowSave: authenticated }
}

/**
 * A cloud read must finish before choosing data. `local` is an explicitly scoped
 * {scopeKey, revision, snapshot, dirty} cache. Public legacy data needs a user
 * migration action and an empty cloud workspace; it never becomes a fallback.
 */
export function resolveAccountWorkspace({ cloud, local = null, legacy = null, explicitLegacyImport = false, scopeKey } = {}) {
  if (cloud == null) return { snapshot: null, revision: null, source: 'pending', needsSave: false, canImportLegacy: false }
  if (!isObject(cloud) || !validRevision(cloud.revision) || !(cloud.snapshot === null || isWorkspace(cloud.snapshot))) {
    throw new TypeError('云端工作区响应无效，请重新加载。')
  }
  const canImportLegacy = cloud.snapshot === null && isWorkspace(legacy)
  const matchedLocal = Boolean(scopeKey && local?.scopeKey === scopeKey && isWorkspace(local?.snapshot) && validRevision(local?.revision))
  if (explicitLegacyImport && canImportLegacy) {
    return { snapshot: sanitizeAccountWorkspace(legacy), revision: cloud.revision, source: 'legacy-import', needsSave: true, canImportLegacy: false }
  }
  if (matchedLocal && local.dirty === true && local.revision === cloud.revision) {
    return { snapshot: sanitizeAccountWorkspace(local.snapshot), revision: cloud.revision, source: 'local-pending', needsSave: true, canImportLegacy }
  }
  const result = {
    snapshot: cloud.snapshot === null ? emptyAccountWorkspace() : sanitizeAccountWorkspace(cloud.snapshot),
    revision: cloud.revision,
    source: cloud.snapshot === null ? 'empty' : 'cloud',
    needsSave: false,
    canImportLegacy,
  }
  if (matchedLocal && local.dirty === true && local.revision !== cloud.revision) result.localConflict = true
  return result
}

export class AccountWorkspaceError extends Error {
  constructor(message, { status, revision, payload } = {}) {
    super(message)
    this.name = 'AccountWorkspaceError'
    this.status = status
    this.revision = revision
    this.payload = payload
  }
}

export function createAccountWorkspaceClient({ apiBase, fetchImpl = globalThis.fetch }) {
  const endpoint = `${normalizedApiBase(apiBase)}/account/workspace`
  if (typeof fetchImpl !== 'function') throw new TypeError('工作区客户端需要 fetch。')
  async function request({ token, signal, method = 'GET', body }) {
    if (typeof token !== 'string' || !token.trim()) throw new AccountWorkspaceError('请登录后加载或保存工作区。', { status: 401 })
    const response = await fetchImpl(endpoint, {
      method, credentials: 'include', cache: 'no-store', signal,
      headers: { Accept: 'application/json', Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    let payload
    try { payload = await response.json() } catch {
      throw new AccountWorkspaceError('工作区服务返回了无效响应。', { status: response.status })
    }
    if (!response.ok) {
      const detail = payload?.detail ?? payload
      const fallback = response.status === 409 ? '工作区已在其他窗口更新，请重新加载后再保存。' : '工作区请求失败，请重试。'
      throw new AccountWorkspaceError(typeof detail === 'string' ? detail : detail?.message || fallback,
        { status: response.status, revision: validRevision(detail?.revision) ? detail.revision : undefined, payload })
    }
    if (!isObject(payload) || !validRevision(payload.revision) || !(payload.snapshot === null || isWorkspace(payload.snapshot))) {
      throw new AccountWorkspaceError('云端工作区响应无效，请重新加载。', { status: response.status, payload })
    }
    return payload
  }
  return {
    load: ({ token, signal } = {}) => request({ token, signal }),
    save: ({ token, expectedRevision, snapshot, signal } = {}) => {
      if (!validRevision(expectedRevision) || expectedRevision === Number.MAX_SAFE_INTEGER) throw new TypeError('expectedRevision 必须是非负整数。')
      return request({ token, signal, method: 'PUT', body: { expectedRevision, snapshot: sanitizeAccountWorkspace(snapshot) } })
    },
  }
}
