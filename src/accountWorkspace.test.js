import test from 'node:test'
import assert from 'node:assert/strict'
import {
  accountWorkspaceStorageKey, accountWorkspaceTransition, emptyAccountWorkspace,
  resolveAccountWorkspace, sanitizeAccountWorkspace, createAccountWorkspaceClient,
} from './accountWorkspace.js'

const apiBase = 'https://cad.example.com/api/v1'
const accountA = { apiBase, userId: 'account-a' }
const accountB = { apiBase, userId: 'account-b' }
const store = (name = '加工轴') => ({
  schemaVersion: 1, activeProjectId: 'p1', activeFileId: 'f1',
  projects: [{ id: 'p1', name, files: [{ id: 'f1', snapshot: {
    model: { kind: 'shaft', length: 70, token: 'legitimate model parameter' },
    messages: [{ role: 'ai', text: '孔径是 10 mm' }],
    generation: { artifacts: [{ url: '/cad/source/drawing', token: 'scoped-download-token' }] },
  }, versions: [{ id: 'v1', snapshot: { model: { kind: 'shaft', length: 50 }, messages: [{ text: '历史对话' }] } }] }] }],
  trash: [{ id: 'trash1', file: { snapshot: { documentText: '可恢复的说明书' } } }],
  futureMetadata: { precision: 0.001 },
})

test('storage is isolated by account and API deployment, including guest identity', () => {
  const a = accountWorkspaceStorageKey(accountA)
  assert.notEqual(a, accountWorkspaceStorageKey(accountB))
  assert.notEqual(a, accountWorkspaceStorageKey({ ...accountA, apiBase: 'https://other.example.com/api/v1' }))
  assert.notEqual(a, accountWorkspaceStorageKey({ ...accountA, apiBase: 'https://cad.example.com/other-api' }))
  assert.equal(a, accountWorkspaceStorageKey({ ...accountA, apiBase: `${apiBase}/` }))
  assert.notEqual(accountWorkspaceStorageKey({ apiBase, userId: '' }), accountWorkspaceStorageKey({ apiBase, userId: 'guest' }))
  assert.notEqual(a, 'joyniu-workspace-v1')
  assert.throws(() => accountWorkspaceStorageKey({ ...accountA, apiBase: 'https://name:password@example.com/api' }))
})

test('login, account switch, API switch and logout clear visible workspace before loading', () => {
  const initial = accountWorkspaceTransition(null, accountA)
  assert.equal(initial.clearWorkspace, true)
  assert.equal(initial.loadCloud, true)
  assert.equal(initial.allowSave, true)
  assert.equal(accountWorkspaceTransition(accountA, accountB).clearWorkspace, true)
  assert.equal(accountWorkspaceTransition(accountA, { ...accountA, apiBase: 'https://other.example.com/api' }).loadCloud, true)
  const logout = accountWorkspaceTransition(accountA, { apiBase, userId: '' })
  assert.equal(logout.clearWorkspace, true)
  assert.equal(logout.loadCloud, false)
  assert.equal(logout.allowSave, false)
  const expired = accountWorkspaceTransition(accountA, { ...accountA, authenticated: false })
  assert.equal(expired.clearWorkspace, true)
  assert.equal(expired.allowSave, false)
})

test('refreshing an access token for the same verified account does not reset workspace', () => {
  const transition = accountWorkspaceTransition({ ...accountA, token: 'old' }, { ...accountA, token: 'new' })
  assert.equal(transition.changed, false)
  assert.equal(transition.clearWorkspace, false)
  assert.equal(transition.loadCloud, false)
})

test('pending cloud reads cannot be bypassed by local or legacy data', () => {
  const scopeKey = accountWorkspaceStorageKey(accountA)
  const resolved = resolveAccountWorkspace({ local: { scopeKey, revision: 0, snapshot: store(), dirty: true }, legacy: store(), scopeKey })
  assert.equal(resolved.source, 'pending')
  assert.equal(resolved.snapshot, null)
  assert.equal(resolved.needsSave, false)
})

test('empty cloud for a new account never silently imports public browser history', () => {
  const legacy = store('属于之前使用者的项目')
  const result = resolveAccountWorkspace({ cloud: { revision: 0, snapshot: null }, legacy })
  assert.deepEqual(result.snapshot, emptyAccountWorkspace())
  assert.equal(result.source, 'empty')
  assert.equal(result.canImportLegacy, true)
  assert.equal(result.needsSave, false)
  const imported = resolveAccountWorkspace({ cloud: { revision: 0, snapshot: null }, legacy, explicitLegacyImport: true })
  assert.deepEqual(imported.snapshot, legacy)
  assert.equal(imported.source, 'legacy-import')
  assert.equal(imported.needsSave, true)
})

test('existing cloud always wins over legacy even when import is requested', () => {
  const existing = store('云端账号工作区')
  const result = resolveAccountWorkspace({ cloud: { revision: 9, snapshot: existing }, legacy: store('旧本地数据'), explicitLegacyImport: true })
  assert.deepEqual(result.snapshot, existing)
  assert.equal(result.source, 'cloud')
  assert.equal(result.canImportLegacy, false)
})

test('only dirty local data scoped to the same account and cloud revision can recover', () => {
  const scopeKey = accountWorkspaceStorageKey(accountA)
  const cloud = { revision: 3, snapshot: store('已同步') }
  const local = { scopeKey, revision: 3, snapshot: store('未同步修改'), dirty: true }
  const recovered = resolveAccountWorkspace({ cloud, local, scopeKey })
  assert.equal(recovered.source, 'local-pending')
  assert.equal(recovered.snapshot.projects[0].name, '未同步修改')
  assert.equal(recovered.needsSave, true)
  assert.equal(resolveAccountWorkspace({ cloud, local, scopeKey: accountWorkspaceStorageKey(accountB) }).source, 'cloud')
  assert.equal(resolveAccountWorkspace({ cloud, local }).source, 'cloud')
  const conflict = resolveAccountWorkspace({ cloud: { ...cloud, revision: 4 }, local, scopeKey })
  assert.equal(conflict.source, 'cloud')
  assert.equal(conflict.localConflict, true)
  assert.equal(conflict.needsSave, false)
  assert.equal(local.snapshot.projects[0].name, '未同步修改')
})

test('cloud deserialization keeps model, message, version and trash data independent', () => {
  const source = store()
  const resolved = resolveAccountWorkspace({ cloud: { revision: 1, snapshot: source } })
  assert.deepEqual(resolved.snapshot, source)
  resolved.snapshot.projects[0].files[0].snapshot.model.length = 999
  assert.equal(source.projects[0].files[0].snapshot.model.length, 70)
  for (const cloud of [{}, { revision: true, snapshot: null }, { revision: 1, snapshot: { projects: [] } }]) {
    assert.throws(() => resolveAccountWorkspace({ cloud }))
  }
})

test('sanitizer removes login credentials without deleting scoped source download tokens', () => {
  const source = store()
  source.sessionToken = 'session-secret'
  source.access_token = 'access-secret'
  source.token = 'top-level-login-secret'
  source.platform = { token: 'platform-login-secret', user: { id: 'account-a' } }
  source.projects[0].files[0].versions[0].snapshot.refreshToken = 'refresh-secret'
  const sanitized = sanitizeAccountWorkspace(source)
  const serialized = JSON.stringify(sanitized)
  assert.doesNotMatch(serialized, /(?:session|access|top-level-login|platform-login|refresh)-secret/)
  assert.equal(sanitized.projects[0].files[0].snapshot.generation.artifacts[0].token, 'scoped-download-token')
  assert.equal(sanitized.projects[0].files[0].snapshot.model.token, 'legitimate model parameter')
  assert.equal(sanitized.projects[0].files[0].versions[0].snapshot.messages[0].text, '历史对话')
  assert.equal(source.access_token, 'access-secret')
})

test('API client sends authentication and credentials separately from the snapshot', async () => {
  const calls = []
  const client = createAccountWorkspaceClient({ apiBase, fetchImpl: async (...args) => {
    calls.push(args)
    return { ok: true, status: 200, json: async () => ({ revision: calls.length, snapshot: store(), updatedAt: '2026-09-10T00:00:00Z' }) }
  } })
  const signal = new AbortController().signal
  await client.load({ token: 'access-header-only', signal })
  await client.save({ token: 'access-header-only', expectedRevision: 1, snapshot: { ...store(), accessToken: 'must-not-store' }, signal })
  assert.equal(calls[0][0], `${apiBase}/account/workspace`)
  assert.equal(calls[0][1].method, 'GET')
  assert.equal(calls[0][1].credentials, 'include')
  assert.equal(calls[1][1].method, 'PUT')
  assert.equal(calls[1][1].headers.Authorization, 'Bearer access-header-only')
  assert.equal(calls[1][1].signal, signal)
  const body = JSON.parse(calls[1][1].body)
  assert.equal(body.expectedRevision, 1)
  assert.deepEqual(body.snapshot, store())
  assert.doesNotMatch(calls[1][1].body, /access-header-only|must-not-store/)
})

test('client rejects unauthenticated work and exposes safe revision on 409', async () => {
  let count = 0
  const client = createAccountWorkspaceClient({ apiBase, fetchImpl: async () => {
    count += 1
    return { ok: false, status: 409, json: async () => ({ detail: { code: 'workspace_revision_conflict', message: '请重新加载', revision: 8 } }) }
  } })
  await assert.rejects(client.load(), (error) => error.status === 401)
  assert.equal(count, 0)
  await assert.rejects(client.save({ token: 'signed-in', expectedRevision: 1, snapshot: store() }), (error) => error.status === 409 && error.revision === 8)
  assert.equal(count, 1)
})

test('client preserves AbortError and rejects malformed successful responses', async () => {
  const abort = new DOMException('cancelled', 'AbortError')
  const cancelled = createAccountWorkspaceClient({ apiBase, fetchImpl: async () => { throw abort } })
  await assert.rejects(cancelled.load({ token: 'signed-in' }), (error) => error === abort)
  const malformed = createAccountWorkspaceClient({ apiBase, fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ snapshot: null }) }) })
  await assert.rejects(malformed.load({ token: 'signed-in' }), /响应无效/)
})
