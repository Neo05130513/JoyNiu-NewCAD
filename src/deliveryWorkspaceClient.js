import { API_BASE } from './api.js'
import { createClientId } from './clientId.js'

export const deliverySourceKinds = { engineering: '工程设计', feature: '特征设计', native: '二维图纸', cad_run: 'AI 建模结果' }
export const deliveryAccountScope = (accountKey, token) => accountKey || token || 'guest'
export const deliverySourceKey = source => JSON.stringify([source.kind, source.id, source.revision ?? null])
export const deliverySourceRef = source => ({ kind: source.kind, id: source.id, ...(source.revision == null ? {} : { revision: source.revision }) })
export const deliveryVersionLabel = source => source.revision == null ? '封存时锁定文件版本' : `版本 ${source.revision}`

export function deliverySelectionState(selected, sources) {
  const inventory = new Map(sources.map(source => [deliverySourceKey(source), source]))
  return selected.map(ref => {
    const current = inventory.get(deliverySourceKey(ref))
    return current || { ...ref, name: ref.name || ref.id, eligible: false, reason: '此版本已不在可选来源中，请刷新并重新选择。' }
  })
}

export function deliveryCreatePayload({ title, notes = '', selected }, sources) {
  if (typeof title !== 'string' || !title.trim()) throw new Error('请填写交付包名称。')
  if (title.trim().length > 180) throw new Error('交付包名称最多 180 个字符。')
  if (typeof notes !== 'string' || notes.trim().length > 4000) throw new Error('备注最多 4000 个字符。')
  if (!Array.isArray(selected) || !selected.length) throw new Error('请至少选择一个可交付来源。')
  if (selected.length > 20) throw new Error('一个交付包最多选择 20 个来源。')
  const resolved = deliverySelectionState(selected, sources)
  const unavailable = resolved.find(source => source.eligible !== true)
  if (unavailable) throw new Error(`${unavailable.name}：${unavailable.reason || '当前不可打包，请重新选择。'}`)
  const refs = new Map(resolved.map(source => [deliverySourceKey(source), deliverySourceRef(source)]))
  return { title: title.trim(), notes: notes.trim(), sourceRefs: [...refs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, ref]) => ref) }
}

export function deliveryIntentFingerprint(payload) {
  return JSON.stringify({ title: payload.title, notes: payload.notes || '', sourceRefs: [...payload.sourceRefs].map(deliverySourceRef).sort((a, b) => deliverySourceKey(a).localeCompare(deliverySourceKey(b))) })
}

/** A failed or uncertain request keeps its ID through retries and credential renewal. */
export function createDeliveryIntentTracker(saved = null, makeId = createClientId) {
  let intent = saved && typeof saved.requestId === 'string' && /^[A-Za-z0-9_-]{8,128}$/.test(saved.requestId) && typeof saved.fingerprint === 'string' ? saved : null
  return {
    get(payload) {
      const fingerprint = deliveryIntentFingerprint(payload)
      if (intent?.fingerprint !== fingerprint) intent = { fingerprint, requestId: makeId() }
      return intent.requestId
    },
    snapshot: () => intent,
    clear() { intent = null },
  }
}

const draftKey = accountKey => accountKey ? `joyniu-delivery-draft-v1:${encodeURIComponent(accountKey)}` : null
export const emptyDeliveryDraft = () => ({ title: '', notes: '', selected: [], intent: null })
export function readDeliveryDraft(storage, accountKey) {
  const key = draftKey(accountKey)
  if (!key) return emptyDeliveryDraft()
  try {
    const value = JSON.parse(storage?.getItem(key) || 'null')
    if (!value || value.version !== 1 || typeof value.title !== 'string' || typeof value.notes !== 'string' || !Array.isArray(value.selected)) return emptyDeliveryDraft()
    const selected = value.selected.filter(ref => ref && Object.hasOwn(deliverySourceKinds, ref.kind) && typeof ref.id === 'string' && (ref.revision == null || typeof ref.revision === 'string' || Number.isSafeInteger(ref.revision))).map(deliverySourceRef)
    return { title: value.title, notes: value.notes, selected, intent: value.intent || null }
  } catch { return emptyDeliveryDraft() }
}
export function writeDeliveryDraft(storage, accountKey, draft) {
  const key = draftKey(accountKey)
  if (!key) return false
  try {
    storage?.setItem(key, JSON.stringify({ version: 1, title: draft.title, notes: draft.notes, selected: draft.selected.map(deliverySourceRef), intent: draft.intent || null }))
    return Boolean(storage)
  } catch { return false }
}

export function deliverySeedSelection(seed, sources) {
  const requested = Array.isArray(seed) ? seed : seed ? [seed] : []
  const selected = [], unavailable = []
  for (const ref of requested) {
    const found = sources.find(source => source.kind === ref.kind && source.id === ref.id && (ref.revision == null || source.revision === ref.revision))
    if (found?.eligible === true) selected.push(deliverySourceRef(found))
    else unavailable.push(found?.reason || '指定来源或版本暂不可打包，请在来源列表核对。')
  }
  return { selected, unavailable }
}

export function deliveryFileSourceIndex(sources = []) {
  const index = new Map()
  for (const source of sources) {
    for (const fileId of Array.isArray(source.fileIds) ? source.fileIds : []) {
      const owners = index.get(fileId) || []
      if (!owners.some(item => deliverySourceKey(item) === deliverySourceKey(source))) owners.push(source)
      index.set(fileId, owners)
    }
  }
  return index
}
export const deliverySealedVersionLabel = (source, packageId) => source?.revision == null ? `封存版 ${String(packageId || '').slice(-8) || '已锁定'}` : `版本 ${source.revision}`
export const deliveryFileSourceLabel = (source, packageId) => `${source.name || '未命名来源'} · ${deliverySourceKinds[source.kind] || '其他来源'} · ${deliverySealedVersionLabel(source, packageId)}`

function safeDownloadName(value) {
  return String(value || '').replace(/[\\/:*?"<>|\x00-\x1f\x7f]/g, '-').replace(/^[. ]+|[. ]+$/g, '') || '交付文件'
}
function boundedFilename(value, maxBytes) {
  const name = safeDownloadName(value)
  const suffix = name.match(/\.[a-zA-Z0-9]{1,12}$/)?.[0] || ''
  const stem = suffix ? name.slice(0, -suffix.length) : name
  const encoder = new TextEncoder()
  let result = '', bytes = encoder.encode(suffix).length
  for (const char of stem) {
    const length = encoder.encode(char).length
    if (bytes + length > maxBytes) break
    result += char; bytes += length
  }
  return (result || '文件') + suffix
}
/** Local download names clarify provenance; the sealed artifact name and hash stay unchanged. */
export function deliveryFileDownloadName(detail, file) {
  const index = deliveryFileSourceIndex(detail.sources)
  const baseName = item => {
    const owners = index.get(item.id) || []
    const source = owners[0]
    const version = source?.revision == null ? `封存版${String(detail.id || '').slice(-8)}` : `v${source.revision}`
    const name = source ? `${boundedFilename(source.name || '未命名来源', 80)}-${deliverySourceKinds[source.kind] || '其他来源'}-${version}${owners.length > 1 ? `-等${owners.length}个来源` : ''}` : `来源未标记-${version}`
    return boundedFilename(`${name}_${boundedFilename(item.name, 90)}`, 210)
  }
  const proposed = baseName(file)
  if ((detail.files || []).filter(item => baseName(item).normalize('NFC').toLowerCase() === proposed.normalize('NFC').toLowerCase()).length < 2) return proposed
  // Equal names and versions (or names shortened for filesystem limits) must not overwrite each other.
  const suffix = proposed.match(/\.[a-zA-Z0-9]{1,12}$/)?.[0] || ''
  return `${suffix ? proposed.slice(0, -suffix.length) : proposed}-${safeDownloadName(file.id).replace(/^file_/, '')}${suffix}`
}

export function deliveryVerificationLabel(status) {
  return status === 'confirmed_source' ? '来源已确认 · 包待验收' : '待人工验收'
}
export function deliveryFileSize(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '大小待核对'
  const size = Number(bytes)
  return size < 1024 ? `${size} B` : size < 1024 ** 2 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 ** 2).toFixed(1)} MB`
}

export function deliveryErrorMessage(error) {
  if (error?.status === 401) return '登录已失效，请重新登录后重试。当前账号的交付草稿已保留。'
  if (error?.status === 403) return '当前账号没有执行此操作的权限。请联系管理员核对权限。'
  if (/failed to fetch|networkerror|network request failed/i.test(error?.message || '')) return '暂时无法连接服务。若刚才在创建交付包，请先刷新已保存清单确认结果，再重试同一操作。'
  return error?.message || '操作未完成，请重试。'
}

export function createDeliveryWorkspaceClient({ apiBase = API_BASE, fetchImpl = globalThis.fetch, timeoutMs = 120_000 } = {}) {
  const base = new URL('/api/cad/deliveries', apiBase).href
  async function request(path, token, { method = 'GET', body, signal, binary = false } = {}) {
    const credential = typeof token === 'function' ? token() : token
    if (!credential) throw Object.assign(new Error('请先登录后使用交付中心。'), { status: 401 })
    const controller = new AbortController()
    const forwardAbort = () => controller.abort(signal?.reason)
    if (signal?.aborted) forwardAbort()
    else signal?.addEventListener('abort', forwardAbort, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
      const response = await fetchImpl(`${base}${path}`, { method, cache: 'no-store', credentials: 'include', signal: controller.signal,
        headers: { Accept: binary ? 'application/octet-stream' : 'application/json', Authorization: `Bearer ${credential}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
      if (!response.ok) {
        let payload
        try { payload = await response.json() } catch { /* Some gateways return a plain error page. */ }
        const detail = payload?.detail
        throw Object.assign(new Error(typeof detail === 'string' ? detail : detail?.message || `交付服务请求失败（${response.status}），请重试。`), { status: response.status, code: detail?.code, payload })
      }
      if (binary) return await response.blob()
      try { return await response.json() } catch (error) {
        if (controller.signal.aborted) throw error
        throw new Error('交付服务返回了无法读取的结果。请刷新已保存清单确认创建结果，再重试。')
      }
    } catch (error) {
      if (timedOut && !signal?.aborted) throw new Error('交付请求超时。草稿与本次请求编号已保留；请先刷新已保存清单确认结果，再重试。')
      throw error
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', forwardAbort) }
  }
  function packageResult(value) {
    if (!value || typeof value.id !== 'string' || !value.id || typeof value.title !== 'string' || !Array.isArray(value.sources) || !Array.isArray(value.files) || !value.verification || typeof value.verification.status !== 'string') throw new Error('交付清单内容不完整。请刷新已保存清单核对结果后重试。')
    return value
  }
  const part = value => encodeURIComponent(value)
  return {
    sources: (token, signal) => request('/sources', token, { signal }),
    list: (token, signal) => request('', token, { signal }),
    create: (token, payload, requestId, signal) => request('', token, { method: 'POST', body: { ...payload, requestId }, signal }).then(packageResult),
    detail: (token, id, signal) => request(`/${part(id)}`, token, { signal }).then(packageResult),
    archive: (token, id, signal) => request(`/${part(id)}/archive`, token, { signal, binary: true }),
    file: (token, id, fileId, signal) => request(`/${part(id)}/files/${part(fileId)}`, token, { signal, binary: true }),
  }
}
export const deliveryWorkspaceClient = createDeliveryWorkspaceClient()
