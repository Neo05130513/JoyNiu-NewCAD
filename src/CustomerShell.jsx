import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { api, API_BASE } from './api.js'
import * as ProjectStore from './projectStore.js'
import { accountWorkspaceStorageKey, createAccountWorkspaceClient, resolveAccountWorkspace } from './accountWorkspace.js'
import { createSessionExpiryTimer, createSessionOperationCoordinator, createWorkspaceSynchronizer, legacyImportEligibility, selectWorkspaceCache, workspaceLoadDecision } from './workspaceSync.js'
import { downloadBlob } from './workspaceFeedback.js'
import { readAppRoute } from './adminNavigation.js'

const AdminShell = lazy(() => import('./AdminShell.jsx'))

const cloudClient = createAccountWorkspaceClient({ apiBase: API_BASE })
const logoutKey = `joyniu-session-logout:${API_BASE}`
const sessionOperations = createSessionOperationCoordinator({ locks: globalThis.navigator?.locks, name: `joyniu-session-cookie:${API_BASE}` })
let refreshing = null
function refreshSession() {
  if (!refreshing) refreshing = sessionOperations.run(() => api.refreshSession()).finally(() => { refreshing = null })
  return refreshing
}
function read(key) { try { return JSON.parse(localStorage.getItem(key)) } catch { return null } }
function readWorkspaceCache(scope) {
  let tabCache = null
  try { tabCache = JSON.parse(sessionStorage.getItem(scope)) } catch { /* A shared cache remains available when tab storage is disabled. */ }
  return selectWorkspaceCache(scope, tabCache, read(scope))
}
function writeWorkspaceCache(scope, value) {
  const encoded = JSON.stringify(value)
  let failure = null
  try { sessionStorage.setItem(scope, encoded) } catch (error) { failure = new Error(`当前标签页的草稿缓存未保存：${error.message}`) }
  try { localStorage.setItem(scope, encoded) } catch (error) { failure ||= error }
  if (failure) throw failure
}
function locallyLoggedOut() {
  if (read(logoutKey)) return true
  try { return Boolean(sessionStorage.getItem(logoutKey)) } catch { return false }
}
function normalizeStore(value) {
  return ProjectStore.loadProjectStore({ getItem: key => key === ProjectStore.PROJECT_STORE_KEY && value ? JSON.stringify(value) : null })
}
function saveableStore(value) {
  return ProjectStore.persistProjectStore(value, { setItem: () => {} })
}
export default function CustomerShell({ Workbench }) {
  const [route, setRoute] = useState(() => readAppRoute())
  const visitedWorkspace = useRef(null)
  useEffect(() => {
    const changed = () => setRoute(readAppRoute())
    window.addEventListener('popstate', changed)
    window.addEventListener('joyniu:navigate', changed)
    return () => { window.removeEventListener('popstate', changed); window.removeEventListener('joyniu:navigate', changed) }
  }, [])
  const [session, setSession] = useState(null)
  const [checking, setChecking] = useState(true)
  const [sessionNotice, setSessionNotice] = useState('')
  const currentSession = useRef(session); currentSession.current = session
  const authEpoch = useRef(0)
  useEffect(() => {
    let active = true
    const epoch = authEpoch.current
    if (locallyLoggedOut()) { setChecking(false); return }
    refreshSession().then(value => { if (active && epoch === authEpoch.current) setSession(value) }).catch(() => {})
      .finally(() => { if (active) setChecking(false) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    if (!session?.expires_at) return
    if (!session.persistent_session) return createSessionExpiryTimer({ expiresAt: session.expires_at, onExpire: () => {
      if (currentSession.current?.access_token !== session.access_token) return
      setSessionNotice('本次登录已到期，请重新登录。该账号的项目与本地草稿仍保留在原账号工作区。')
      setSession(null)
    } })
    const epoch = authEpoch.current
    let active = true
    let timer
    const renew = () => {
      refreshSession().then(value => { if (active && epoch === authEpoch.current) setSession(value) }).catch(error => {
        if (!active || epoch !== authEpoch.current) return
        if (error.status === 401) setSession(null)
        else timer = setTimeout(renew, 30_000)
      })
    }
    timer = setTimeout(renew, Math.max(5000, session.expires_at * 1000 - Date.now() - 120000))
    return () => { active = false; clearTimeout(timer) }
  }, [session])
  useEffect(() => {
    const changed = event => { if (event.key === logoutKey && event.newValue) { authEpoch.current++; setSession(null) } }
    window.addEventListener('storage', changed)
    return () => window.removeEventListener('storage', changed)
  }, [])
  const accept = (value, epoch) => {
    if (epoch !== authEpoch.current) throw new DOMException('此登录操作已被更新的账号操作替代。', 'AbortError')
    try { localStorage.removeItem(logoutKey) } catch { /* In-memory login remains available without browser storage. */ }
    try { sessionStorage.removeItem(logoutKey) } catch { /* Session storage can be disabled by browser policy. */ }
    setSessionNotice(''); setSession(value); return value
  }
  const account = {
    session, notice: sessionNotice,
    login: async ({ email, password, bootstrap, displayName, roles }) => {
      const epoch = ++authEpoch.current
      return accept(await sessionOperations.run(async () => {
        if (bootstrap) await api.createUser({ email, password, displayName, roles })
        return api.login(email, password)
      }), epoch)
    },
    register: async payload => { const epoch = ++authEpoch.current; return accept(await sessionOperations.run(() => api.register(payload)), epoch) },
    changePassword: async payload => { const epoch = ++authEpoch.current; return accept(await sessionOperations.run(() => api.changePassword(payload, session?.access_token)), epoch) },
    logout: async () => {
      const token = currentSession.current?.access_token
      authEpoch.current++; setSessionNotice(''); setSession(null)
      const marker = JSON.stringify(Date.now())
      try { localStorage.setItem(logoutKey, marker) } catch { /* A full workspace cache must not block logout. */ }
      try { sessionStorage.setItem(logoutKey, marker) } catch { /* The server logout is still attempted below. */ }
      try { await sessionOperations.run(() => api.logout(token)) } catch { /* Local logout remains effective even while offline. */ }
    },
  }
  if (checking) return <div className="customer-loading" role="status">正在恢复登录与工作区…</div>
  const scope = accountWorkspaceStorageKey({ apiBase: API_BASE, userId: session?.user?.id || '' })
  if (!route.isAdmin) visitedWorkspace.current = scope
  return <>
    {visitedWorkspace.current === scope && <div hidden={route.isAdmin}><SessionWorkspace key={scope} {...{ scope, account, Workbench }} suspended={route.isAdmin} /></div>}
    {route.isAdmin && <Suspense fallback={<div className="customer-loading" role="status">正在打开管理后台…</div>}><AdminShell key={scope} account={account} route={route} /></Suspense>}
  </>
}

function SessionWorkspace({ scope, account, Workbench, suspended = false }) {
  const [initial, setInitial] = useState(null)
  const [saveState, setSaveState] = useState({ status: 'loading', error: '' })
  const [reload, setReload] = useState(0)
  const [canImportLegacy, setCanImportLegacy] = useState(false)
  const latest = useRef(null)
  const sync = useRef(null)
  const token = useRef(account.session?.access_token)
  token.current = account.session?.access_token
  const signedIn = Boolean(account.session?.user?.id)
  const legacyImportState = useRef(null)
  const importingLegacy = useRef(false)
  const preferCloud = useRef(false)
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setInitial(null); setSaveState({ status: 'loading', error: '' })
    async function load() {
      try {
        if (!signedIn) {
          const cached = readWorkspaceCache(scope)
          const next = normalizeStore(cached?.snapshot)
          latest.current = next; setInitial(next); setSaveState({ status: 'local', error: '' }); return
        }
        const cloud = await cloudClient.load({ token: token.current, signal: controller.signal })
        if (!active) return
        const legacy = read(ProjectStore.PROJECT_STORE_KEY)
        const cached = readWorkspaceCache(scope)
        const resolved = resolveAccountWorkspace({ cloud, local: cached, legacy, scopeKey: scope })
        const decision = workspaceLoadDecision({ cloud, resolved, cached, preferCloud: preferCloud.current })
        const next = normalizeStore(decision.snapshot)
        preferCloud.current = false
        legacyImportState.current = legacyImportEligibility({ marker: read(`${scope}:legacy-import`)?.state, cachedState: decision.legacyImportState, cloudSnapshot: cloud.snapshot, hasLegacy: Boolean(legacy?.projects?.length) })
        latest.current = next; setCanImportLegacy(legacyImportState.current === 'eligible' && Boolean(legacy?.projects?.length))
        setInitial(next); setSaveState(decision.blocked
          ? { status: 'conflict', error: '云端已有新版本，本地未同步草稿已保留。', revision: decision.revision }
          : { status: 'saved', error: '', revision: decision.revision })
        sync.current = createWorkspaceSynchronizer({
          revision: decision.revision,
          initialSnapshot: cloud.snapshot ? saveableStore(normalizeStore(cloud.snapshot)) : null,
          initiallyBlocked: decision.blocked,
          save: payload => cloudClient.save({ ...payload, token: token.current, signal: controller.signal }),
          cache: payload => {
            if (legacyImportState.current === 'pending' && !payload.dirty && payload.snapshot.legacyImportState === 'completed') {
              legacyImportState.current = 'completed'
              localStorage.setItem(`${scope}:legacy-import`, JSON.stringify({ state: 'completed' }))
            }
            writeWorkspaceCache(scope, { ...payload, scopeKey: scope, legacyImportState: legacyImportState.current })
          },
          onState: value => { if (active) setSaveState(value) },
        })
        // Persist eligibility before the automatically created blank project is
        // uploaded; a later cloud snapshot must not remove an unclaimed import.
        if (legacyImportState.current === 'eligible') {
          try {
            if (!read(`${scope}:legacy-import`)?.state) localStorage.setItem(`${scope}:legacy-import`, JSON.stringify({ state: 'eligible' }))
            writeWorkspaceCache(scope, { scopeKey: scope, revision: decision.revision, snapshot: saveableStore(next), dirty: decision.dirty || !cloud.snapshot, legacyImportState: 'eligible' })
          }
          catch (error) { setSaveState(current => ({ ...current, status: 'error', error: error.message })) }
        }
      } catch (error) {
        if (active) setSaveState({ status: 'load-error', error: error.message })
      }
    }
    void load()
    return () => { active = false; controller.abort(); sync.current?.close(); sync.current = null }
  }, [scope, signedIn, reload])
  const persist = value => {
    const snapshot = saveableStore(value)
    latest.current = snapshot
    if (signedIn) sync.current?.offer(snapshot)
    else writeWorkspaceCache(scope, { scopeKey: scope, snapshot })
  }
  const exportLocal = () => latest.current && downloadBlob(JSON.stringify(ProjectStore.exportProjectBackup(latest.current), null, 2), 'JoyNiu-未同步项目备份.json')
  const reloadCloud = () => {
    try {
      exportLocal()
      if (latest.current) localStorage.setItem(`${scope}:recovery`, JSON.stringify({ scopeKey: scope, snapshot: latest.current, savedAt: new Date().toISOString() }))
      preferCloud.current = true
      sync.current?.close()
      setReload(value => value + 1)
    } catch (error) { setSaveState(current => ({ ...current, status: 'error', error: `备份未完成，已保留当前草稿：${error.message}` })) }
  }
  const importLegacy = async () => {
    if (!signedIn || legacyImportState.current !== 'eligible' || importingLegacy.current) return
    importingLegacy.current = true
    try {
      const importOnce = () => {
        if (!sync.current) return
        const prior = read(`${scope}:legacy-import`)?.state || read(scope)?.legacyImportState
        if (['pending', 'completed'].includes(prior)) { legacyImportState.current = prior; setCanImportLegacy(false); return }
        const raw = read(ProjectStore.PROJECT_STORE_KEY)
        if (!raw?.projects?.length || !latest.current) return
        const imported = ProjectStore.importProjectBackup(latest.current, { format: ProjectStore.PROJECT_BACKUP_FORMAT, schemaVersion: 1, ...raw })
        const next = { ...imported, legacyImportState: 'completed' }
        localStorage.setItem(`${scope}:legacy-import`, JSON.stringify({ state: 'pending' }))
        legacyImportState.current = 'pending'
        setCanImportLegacy(false); setInitial(next); setImportVersion(value => value + 1)
        persist(next)
      }
      if (globalThis.navigator?.locks?.request) await navigator.locks.request(`joyniu-legacy-import:${scope}`, importOnce)
      else importOnce()
    } catch (error) { setSaveState(current => ({ ...current, status: 'error', error: error.message })) }
    finally { importingLegacy.current = false }
  }
  const [importVersion, setImportVersion] = useState(0)
  useEffect(() => {
    const saveNow = () => { if (document.visibilityState === 'hidden') void sync.current?.flush() }
    document.addEventListener('visibilitychange', saveNow)
    return () => document.removeEventListener('visibilitychange', saveNow)
  }, [])
  const workspace = { saveState, persist, canImportLegacy, importLegacy, retry: () => sync.current?.retry(), reloadCloud, exportLocal }
  if (!initial) return <div className="customer-loading" role="status">{saveState.status === 'load-error' ? <><p>云端项目读取失败，暂未打开工作区。</p><p>{saveState.error}</p><button onClick={() => setReload(value => value + 1)}>重新连接</button><button onClick={account.logout}>退出登录</button></> : '正在读取你的项目…'}</div>
  return <Workbench key={`${scope}:${reload}:${importVersion}`} initialStore={initial} account={account} workspace={workspace} suspended={suspended} />
}
