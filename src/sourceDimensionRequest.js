const TERMINAL = new Set(['succeeded', 'partial', 'unavailable'])
const abortError = () => new DOMException('已停止等待尺寸定位', 'AbortError')

/** A long POST may lose its response while its durable server work completes. */
export function awaitSourceDimensionLocations({ start, read, runId, revision, operationId, force = false,
  signal, onProgress = () => {}, timeoutMs = 225000, pollIntervalMs = 3000, readTimeoutMs = 8000, tickMs = 1000 }) {
  return new Promise((resolve, reject) => {
    const controller = new AbortController()
    const startedAt = Date.now()
    let settled = false, nextPoll, activeRead, postError = null, failedReads = 0
    let latest = { status: 'running', stage: 'preparing', elapsedSeconds: 0 }
    const matches = (value) => value?.version === 'cad-source-locations-v1' && value.runId === runId && value.revision === revision
    const finish = (value, error) => {
      if (settled) return
      settled = true
      clearTimeout(deadline); clearTimeout(nextPoll); clearInterval(ticker)
      signal?.removeEventListener('abort', cancel)
      controller.abort(); activeRead?.abort()
      if (error) reject(error)
      else resolve(value)
    }
    const emit = () => {
      if (!settled) onProgress({ ...latest, elapsedSeconds: Math.max(latest.elapsedSeconds || 0, Math.floor((Date.now() - startedAt) / 1000)) })
    }
    const accept = (value, direct = false) => {
      if (settled || !matches(value)) return false
      if (!direct && force && value.operationId !== operationId) return false
      if (TERMINAL.has(value.status)) { finish(value); return true }
      if (value.status === 'running') { latest = value; emit() }
      return false
    }
    const cancel = () => finish(null, abortError())
    const deadline = setTimeout(() => finish(null, Object.assign(new Error('尺寸定位等待超时，请点击“重新定位尺寸”重试。已识别的位置仍可核对。'), { code: 'source_location_timeout' })), timeoutMs)
    const ticker = setInterval(emit, tickMs)
    if (signal?.aborted) { cancel(); return }
    signal?.addEventListener('abort', cancel, { once: true })
    emit()

    const poll = async () => {
      if (settled) return
      const reader = new AbortController()
      activeRead = reader
      let timer, stop
      try {
        const timeout = new Promise((_, rejectRead) => {
          stop = () => rejectRead(abortError())
          reader.signal.addEventListener('abort', stop, { once: true })
          timer = setTimeout(() => reader.abort(), readTimeoutMs)
        })
        const run = await Promise.race([Promise.resolve().then(() => read({ signal: reader.signal })), timeout])
        if (settled) return
        failedReads = 0
        const progress = run?.sourceDimensionLocationProgress
        // During a force retry, only this operation can supersede the old cache.
        if (accept(progress)) return
        const ownProgress = matches(progress) && (!force || progress.operationId === operationId)
        if (!ownProgress || progress.status !== 'running') accept(run?.sourceDimensionLocations)
      } catch (error) {
        if (!settled && postError && ++failedReads >= 3) finish(null, new Error('无法连接尺寸定位服务，请检查服务后重试。'))
      } finally {
        clearTimeout(timer)
        reader.signal.removeEventListener('abort', stop)
        if (activeRead === reader) activeRead = null
        if (!settled) nextPoll = setTimeout(poll, pollIntervalMs)
      }
    }
    nextPoll = setTimeout(poll, pollIntervalMs)
    Promise.resolve().then(() => start({ signal: controller.signal, operationId })).then((result) => {
      if (!accept(result, true) && !settled && result?.status !== 'running') finish(null, new Error('尺寸定位返回了不匹配的模型版本，请重新加载原图。'))
    }).catch((error) => {
      if (settled) return
      if (error.status >= 400 && error.status < 500) { finish(null, error); return }
      postError = error
      latest = { ...latest, stage: 'recovering' }
      emit()
    })
  })
}
