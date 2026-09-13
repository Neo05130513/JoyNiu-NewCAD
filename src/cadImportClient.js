import { API_BASE } from './api.js'
export const cadImportBase = `${API_BASE.replace(/\/api\/v1\/?$/, '').replace(/\/$/, '')}/api/cad/designs/imports`
export function createCadImportClient({ fetchImpl = globalThis.fetch, base = cadImportBase } = {}) {
  return { async upload(token, file, { name, units = 'mm', requestId }, signal) {
    const bearer = typeof token === 'function' ? token() : token
    if (!bearer) throw new Error('请登录后导入实体。')
    const controller = new AbortController(), abort = () => controller.abort(signal?.reason)
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, 125000)
    try {
      const body = new FormData(); body.append('file', file); body.append('name', name); body.append('units', units); body.append('requestId', requestId)
      const response = await fetchImpl(base, { method: 'POST', headers: { Authorization: `Bearer ${bearer}` }, body, signal: controller.signal })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) throw Object.assign(new Error(typeof result.detail === 'string' ? result.detail : result.detail?.message || `导入失败（${response.status}）`), { status: response.status })
      return result
    } catch (error) { if (timedOut) throw new Error('实体转换响应超时，可使用相同文件重试。'); throw error }
    finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  } }
}
export const cadImportClient = createCadImportClient()
