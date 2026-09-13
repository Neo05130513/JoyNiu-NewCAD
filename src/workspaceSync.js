/** Serialize cloud writes; a changed snapshot is never cleared by an older save. */
export function createWorkspaceSynchronizer({ revision = 0, initialSnapshot = null, initiallyBlocked = false, save, cache, onState = () => {}, delay = 800 }) {
  let currentRevision = revision
  let pending = null
  let lastSaved = initialSnapshot === null ? null : JSON.stringify(initialSnapshot)
  let inFlight = false
  let blocked = initiallyBlocked
  let closed = false
  let timer = null
  const persist = (dirty) => pending && cache({ revision: currentRevision, snapshot: pending.snapshot, dirty })
  async function flush() {
    clearTimeout(timer)
    if (closed || blocked || inFlight || !pending) return
    if (pending.json === lastSaved) {
      try { persist(false); onState({ status: 'saved', error: '', revision: currentRevision }) }
      catch (error) { onState({ status: 'error', error: `云端已保存，本地缓存未能更新：${error.message}`, revision: currentRevision }) }
      return
    }
    inFlight = true
    const sending = pending
    onState({ status: 'saving', error: '' })
    try {
      const result = await save({ expectedRevision: currentRevision, snapshot: sending.snapshot })
      if (closed) return
      if (!Number.isSafeInteger(result?.revision) || result.revision <= currentRevision) throw new Error('云端保存返回了无效版本，请重新连接。')
      currentRevision = result.revision
      lastSaved = sending.json
      try {
        persist(pending.json !== lastSaved)
        onState({ status: pending.json === lastSaved ? 'saved' : 'saving', error: '', revision: currentRevision })
      } catch (error) {
        onState({ status: 'error', error: `云端已保存，本地缓存未能更新：${error.message}`, revision: currentRevision })
      }
    } catch (error) {
      if (closed) return
      blocked = true
      onState({ status: error.status === 409 ? 'conflict' : 'error', error: error.message, revision: currentRevision })
    } finally {
      inFlight = false
      clearTimeout(timer)
      if (!closed && !blocked && pending?.json !== lastSaved) timer = setTimeout(flush, delay)
    }
  }
  return {
    offer(snapshot) {
      if (closed) return
      const json = JSON.stringify(snapshot)
      if (json === pending?.json) return
      pending = { json, snapshot: JSON.parse(json) }
      clearTimeout(timer)
      // A full browser cache must not prevent an authorized cloud save.
      try {
        // Reverting to the old baseline is still a dirty edit while a different
        // snapshot may already be committing on the server.
        persist(inFlight || json !== lastSaved)
        if (!blocked && (inFlight || json !== lastSaved)) onState({ status: 'saving', error: '', revision: currentRevision })
      }
      finally { if (!blocked && json !== lastSaved) timer = setTimeout(flush, delay) }
    },
    flush,
    retry() { blocked = false; return flush() },
    close() { closed = true; clearTimeout(timer) },
  }
}

/** Resolve a known dirty cache conflict without ever discarding the local draft. */
export function workspaceLoadDecision({ cloud, resolved, cached, preferCloud = false }) {
  if (!preferCloud && resolved.localConflict) return {
    snapshot: cached.snapshot, revision: cached.revision, blocked: true, dirty: true,
    source: 'local-conflict', legacyImportState: cached.legacyImportState || null,
  }
  const useCloud = preferCloud || resolved.source === 'cloud' || resolved.source === 'empty'
  return {
    snapshot: preferCloud ? cloud.snapshot : resolved.snapshot,
    revision: cloud.revision, blocked: false, dirty: preferCloud ? false : resolved.needsSave,
    source: useCloud ? 'cloud' : resolved.source,
    legacyImportState: preferCloud && cached?.legacyImportState === 'pending' ? null : cached?.legacyImportState || null,
  }
}

/** Session storage is private to a tab, so another tab cannot replace its draft. */
export function selectWorkspaceCache(scopeKey, tabCache, sharedCache) {
  if (tabCache?.scopeKey === scopeKey) return tabCache
  return sharedCache?.scopeKey === scopeKey ? sharedCache : null
}

export function legacyImportEligibility({ marker, cachedState, cloudSnapshot, hasLegacy }) {
  if (cloudSnapshot?.legacyImportState === 'completed' || marker === 'completed' || cachedState === 'completed') return 'completed'
  if (marker === 'pending' || cachedState === 'pending') return 'pending'
  if (marker === 'eligible' || cachedState === 'eligible' || hasLegacy) return 'eligible'
  return null
}

/** Serialize rotating-cookie operations across tabs, with an in-page fallback. */
export function createSessionOperationCoordinator({ locks, name }) {
  let tail = Promise.resolve()
  return {
    run(operation) {
      const result = tail.catch(() => {}).then(() => locks?.request ? locks.request(name, operation) : operation())
      tail = result
      return result
    },
  }
}

/** Nonpersistent sessions expire locally; a later account session is untouched. */
export function createSessionExpiryTimer({ expiresAt, onExpire, now = Date.now, schedule = setTimeout, unschedule = clearTimeout }) {
  if (typeof expiresAt !== 'number' || !Number.isFinite(expiresAt) || expiresAt <= 0) return () => {}
  let timer = null, closed = false
  const check = () => {
    if (closed) return
    const remaining = expiresAt * 1000 - now()
    if (remaining <= 0) { closed = true; onExpire(); return }
    timer = schedule(check, Math.min(remaining, 2_147_483_647))
  }
  timer = schedule(check, 0)
  return () => { closed = true; unschedule(timer) }
}
