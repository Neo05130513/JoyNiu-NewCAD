import test from 'node:test'
import assert from 'node:assert/strict'
import { createSessionOperationCoordinator, createWorkspaceSynchronizer, legacyImportEligibility, selectWorkspaceCache, workspaceLoadDecision } from './workspaceSync.js'
import { resolveAccountWorkspace } from './accountWorkspace.js'
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b}); return {promise,resolve,reject} }
test('a late cloud save preserves newer edits and advances CAS revision', async () => {
  const first=deferred(), calls=[], cached=[]
  const sync=createWorkspaceSynchronizer({revision:3,delay:999999,cache:x=>cached.push(x),save:x=>{calls.push(x); return calls.length===1?first.promise:Promise.resolve({revision:5})}})
  sync.offer({text:'first'}); const running=sync.flush(); sync.offer({text:'second'}); first.resolve({revision:4}); await running
  assert.equal(cached.at(-1).snapshot.text,'second'); assert.equal(cached.at(-1).dirty,true); assert.equal(cached.at(-1).revision,4)
  await sync.flush(); assert.equal(calls[1].expectedRevision,4); assert.equal(cached.at(-1).dirty,false); sync.close()
})
test('a conflict blocks automatic writes without replacing local data', async () => {
  let calls=0; const states=[],cached=[]
  const sync=createWorkspaceSynchronizer({delay:999999,cache:x=>cached.push(x),onState:x=>states.push(x),save:async()=>{calls++; throw Object.assign(new Error('conflict'),{status:409})}})
  sync.offer({text:'mine'}); await sync.flush(); sync.offer({text:'newer'}); await sync.flush()
  assert.equal(calls,1); assert.equal(states.at(-1).status,'conflict'); assert.equal(cached.at(-1).snapshot.text,'newer'); sync.close()
})
test('closed account ignores late save response', async () => {
  const first=deferred(),states=[]; const sync=createWorkspaceSynchronizer({delay:999999,cache:()=>{},onState:x=>states.push(x),save:()=>first.promise})
  sync.offer({a:1}); const running=sync.flush(); sync.close(); first.resolve({revision:1}); await running
  assert.equal(states.length,2); assert.ok(states.every(state => state.status === 'saving'))
})

test('opening the same saved cloud snapshot, including StrictMode remount, does not create revisions', async () => {
  const snapshot = { schemaVersion: 1, projects: [], activeProjectId: null }
  let writes = 0
  for (let mount = 0; mount < 2; mount++) {
    const sync = createWorkspaceSynchronizer({ revision: 5, initialSnapshot: snapshot, cache: () => {}, save: async () => { writes++; return { revision: 6 } } })
    sync.offer(snapshot)
    await sync.flush()
    sync.close()
  }
  assert.equal(writes, 0)
})

test('reverting to the baseline while another snapshot is in flight stays dirty on reload', async () => {
  const pending = deferred(), cached = []
  const original = { text: 'original' }
  const sync = createWorkspaceSynchronizer({ revision: 1, initialSnapshot: original, delay: 999999, cache: value => cached.push(value), save: () => pending.promise })
  sync.offer({ text: 'temporary edit' })
  const saving = sync.flush()
  sync.offer(original)
  assert.deepEqual(cached.at(-1).snapshot, original)
  assert.equal(cached.at(-1).dirty, true)
  sync.close()
  pending.resolve({ revision: 2 }); await saving
  assert.equal(cached.at(-1).revision, 1)
  assert.equal(cached.at(-1).dirty, true)
})

test('reload retains a dirty stale local draft and blocks writes until the user chooses cloud', async () => {
  const draft = { schemaVersion: 1, projects: [], note: 'my unsynced edit' }
  const cloud = { revision: 5, snapshot: { schemaVersion: 1, projects: [], note: 'saved on another device' } }
  const cached = { scopeKey: 'account-a', revision: 3, snapshot: draft, dirty: true, legacyImportState: 'pending' }
  const resolved = resolveAccountWorkspace({ cloud, local: cached, scopeKey: 'account-a' })
  const decision = workspaceLoadDecision({ cloud, cached, resolved })
  assert.deepEqual(decision.snapshot, draft)
  assert.equal(decision.blocked, true)
  assert.equal(decision.revision, 3)
  assert.equal(decision.legacyImportState, 'pending')
  let writes = 0
  const sync = createWorkspaceSynchronizer({ revision: decision.revision, initiallyBlocked: decision.blocked, cache: () => {}, save: async () => { writes++ } })
  sync.offer(draft); await sync.flush(); sync.close()
  assert.equal(writes, 0)
  const chosenCloud = workspaceLoadDecision({ cloud, cached, resolved, preferCloud: true })
  assert.deepEqual(chosenCloud.snapshot, cloud.snapshot)
  assert.equal(chosenCloud.revision, 5)
  assert.equal(chosenCloud.blocked, false)
  assert.equal(chosenCloud.legacyImportState, null)
  assert.equal(cached.snapshot.note, 'my unsynced edit')
})

test('an unfinished legacy import remains marked across reload and keeps its pending data', () => {
  const cloud = { revision: 0, snapshot: null }
  const cached = { scopeKey: 'account-a', revision: 0, dirty: true, legacyImportState: 'pending', snapshot: { schemaVersion: 1, projects: [], imported: true } }
  const resolved = resolveAccountWorkspace({ cloud, local: cached, scopeKey: 'account-a' })
  const decision = workspaceLoadDecision({ cloud, cached, resolved })
  assert.equal(decision.dirty, true)
  assert.equal(decision.snapshot.imported, true)
  assert.equal(decision.legacyImportState, 'pending')
})

test('unclaimed legacy eligibility survives the first automatically saved blank cloud project', () => {
  const blank = { schemaVersion: 1, projects: [{ id: 'new-project', files: [] }] }
  const cloud = { revision: 1, snapshot: blank }
  const cached = { scopeKey: 'account-a', revision: 1, dirty: false, legacyImportState: 'eligible', snapshot: blank }
  const resolved = resolveAccountWorkspace({ cloud, local: cached, scopeKey: 'account-a' })
  const decision = workspaceLoadDecision({ cloud, cached, resolved })
  assert.equal(decision.legacyImportState, 'eligible')
  assert.equal(decision.snapshot.projects[0].id, 'new-project')
  assert.equal(decision.dirty, false)
})

test('a stale eligible tab cannot reopen a claimed import and cloud completion follows the account', () => {
  const cloudSnapshot = { schemaVersion: 1, projects: [] }
  assert.equal(legacyImportEligibility({ marker: 'pending', cachedState: 'eligible', cloudSnapshot, hasLegacy: true }), 'pending')
  assert.equal(legacyImportEligibility({ marker: 'completed', cachedState: 'eligible', cloudSnapshot, hasLegacy: true }), 'completed')
  assert.equal(legacyImportEligibility({ cloudSnapshot: { ...cloudSnapshot, legacyImportState: 'completed' }, hasLegacy: true }), 'completed')
  assert.equal(legacyImportEligibility({ marker: 'eligible', cloudSnapshot, hasLegacy: true }), 'eligible')
  assert.equal(legacyImportEligibility({ cloudSnapshot, hasLegacy: true }), 'eligible')
})

test('a cache from a different account cannot activate conflict recovery or supply projects', () => {
  const cloud = { revision: 0, snapshot: null }
  const cached = { scopeKey: 'account-b', revision: 8, dirty: true, snapshot: { schemaVersion: 1, projects: [], private: 'B' } }
  const resolved = resolveAccountWorkspace({ cloud, local: cached, scopeKey: 'account-a' })
  const decision = workspaceLoadDecision({ cloud, cached, resolved })
  assert.equal(decision.blocked, false)
  assert.equal(decision.snapshot.private, undefined)
})

test('a full local cache still permits cloud saving, then retries only the cache update', async () => {
  let cacheFails = true, calls = 0
  const states = []
  const sync = createWorkspaceSynchronizer({ delay: 999999, cache: () => { if (cacheFails) throw new Error('QuotaExceeded') }, onState: state => states.push(state), save: async () => { calls++; return { revision: 1 } } })
  assert.throws(() => sync.offer({ note: 'persist me in the cloud' }), /QuotaExceeded/)
  await sync.flush()
  assert.equal(calls, 1)
  assert.match(states.at(-1).error, /云端已保存/)
  cacheFails = false
  await sync.retry()
  assert.equal(calls, 1)
  assert.equal(states.at(-1).status, 'saved')
  sync.close()
})

test('cookie-changing operations are serial even when refresh and login overlap', async () => {
  const waiting = deferred(), events = []
  const coordinator = createSessionOperationCoordinator({ name: 'deployment-a' })
  const refresh = coordinator.run(async () => { events.push('refresh-start'); await waiting.promise; events.push('refresh-cookie') })
  const login = coordinator.run(async () => { events.push('login-cookie') })
  await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(events, ['refresh-start'])
  waiting.resolve()
  await Promise.all([refresh, login])
  assert.deepEqual(events, ['refresh-start', 'refresh-cookie', 'login-cookie'])
  await assert.rejects(coordinator.run(async () => { throw new Error('bad login') }))
  assert.equal(await coordinator.run(async () => 'next login'), 'next login')
})

test('coordinators in separate tabs request the same named browser lock', async () => {
  const names = [], events = [], waiting = deferred()
  let browserQueue = Promise.resolve()
  const locks = { request(name, callback) { names.push(name); const result = browserQueue.catch(() => {}).then(callback); browserQueue = result; return result } }
  const tabA = createSessionOperationCoordinator({ locks, name: 'deployment-a' })
  const tabB = createSessionOperationCoordinator({ locks, name: 'deployment-a' })
  const a = tabA.run(async () => { events.push('A-start'); await waiting.promise; events.push('A-end') })
  const b = tabB.run(async () => { events.push('B') })
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(events, ['A-start'])
  waiting.resolve(); await Promise.all([a, b])
  assert.deepEqual(names, ['deployment-a', 'deployment-a'])
  assert.deepEqual(events, ['A-start', 'A-end', 'B'])
})

test('another tab saving the shared account cache cannot erase this tabs unsynced draft', () => {
  const tabDraft = { scopeKey: 'account-a', revision: 2, dirty: true, snapshot: { text: 'tab A draft' } }
  const otherTab = { scopeKey: 'account-a', revision: 4, dirty: false, snapshot: { text: 'tab B saved' } }
  assert.equal(selectWorkspaceCache('account-a', tabDraft, otherTab), tabDraft)
  assert.equal(selectWorkspaceCache('account-a', null, otherTab), otherTab)
  assert.equal(selectWorkspaceCache('account-b', tabDraft, otherTab), null)
})

test('a nonpersistent session expires at its timestamp and cleanup cancels stale expiry', async () => {
  const { createSessionExpiryTimer } = await import('./workspaceSync.js')
  let now = 1000, expired = 0, nextId = 0
  const callbacks = new Map()
  const schedule = (callback, delay) => { callbacks.set(++nextId, { callback, delay }); return nextId }
  const unschedule = id => callbacks.delete(id)
  const stop = createSessionExpiryTimer({ expiresAt: 3, now: () => now, schedule, unschedule, onExpire: () => expired++ })
  const first = callbacks.get(1); callbacks.delete(1); first.callback()
  assert.equal(callbacks.get(2).delay, 2000)
  now = 3000; callbacks.get(2).callback(); assert.equal(expired, 1)
  callbacks.get(2).callback(); assert.equal(expired, 1)
  stop()
  const cleanup = createSessionExpiryTimer({ expiresAt: 4, now: () => now, schedule, unschedule, onExpire: () => expired++ })
  const pending = [...callbacks.values()].at(-1).callback
  cleanup(); now = 5000; pending(); assert.equal(expired, 1)
})
