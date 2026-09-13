import { API_BASE } from './api.js'

export function engineeringApiBase(base = API_BASE) {
  return new URL('/api/cad/designs', base).href.replace(/\/$/, '')
}

export function createEngineeringClient({ base = engineeringApiBase(), fetchImpl = globalThis.fetch } = {}) {
  async function request(path, { token, signal, method = 'GET', body, binary = false, timeoutMs = 120000 } = {}) {
    const controller = new AbortController()
    let timedOut = false
    const forwardAbort = () => controller.abort(signal?.reason)
    if (signal?.aborted) forwardAbort()
    else signal?.addEventListener('abort', forwardAbort, { once: true })
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
    const multipart = typeof FormData !== 'undefined' && body instanceof FormData
    const response = await fetchImpl(`${base}${path}`, { method, signal: controller.signal,
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body && !multipart ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: multipart ? body : JSON.stringify(body) } : {}) })
    if (!response.ok) {
      const error = await response.json().catch(() => ({}))
      const detail = error.detail
      throw Object.assign(new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => x.msg).join('；') : `工程服务请求失败（${response.status}）`), { status: response.status })
    }
    return await (binary ? response.blob() : response.json())
    } catch (error) {
      if (timedOut && !signal?.aborted) throw Object.assign(new Error('工程服务响应超时，请刷新设计列表确认保存结果后再重试。'), { name: 'TimeoutError' })
      if (error?.name === 'TypeError') throw new Error('无法连接工程服务，请检查网络后重试。')
      throw error
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', forwardAbort)
    }
  }
  const id = encodeURIComponent
  return {
    list: options => request('', options),
    get: (designId, options) => request(`/${id(designId)}`, options),
    importStep(file, fileId, options = {}) { validateEngineeringStep(file); const body = new FormData(); body.append('file', file); body.append('fileId', fileId); return request('/step', { ...options, method: 'POST', body }) },
    assemble: (body, options) => request('/assemblies', { ...options, method: 'POST', body }),
    assemblyJobs: (fileId, options) => request(`/assembly-jobs${fileId ? `?fileId=${id(fileId)}` : ''}`, options),
    assemblyJob: (jobId, options) => request(`/assembly-jobs/${id(jobId)}`, options),
    startAiAssembly: (body, options) => request('/ai-assemblies', { ...options, method: 'POST', body }),
    compileAssembly: (body, options) => request('/assembly-plans', { ...options, method: 'POST', body }),
    measure: (designId, body, options) => request(`/${id(designId)}/measure`, { ...options, method: 'POST', body }),
    drawing: (designId, body, options) => request(`/${id(designId)}/drawing`, { ...options, method: 'POST', body }),
    artifact: (designId, key, options) => request(`/${id(designId)}/artifacts/${id(key)}`, { ...options, binary: true }),
  }
}

export const engineeringClient = createEngineeringClient()
export const topologyKey = item => `${item.kind}:${item.index}`
export function topologySelector(value) {
  const [kind, index, extra] = String(value).split(':')
  if (extra !== undefined || !['vertex', 'edge', 'face'].includes(kind) || !/^\d+$/.test(index || '') || !Number.isSafeInteger(Number(index))) return null
  return { kind, index: Number(index) }
}

export function validateEngineeringStep(file) {
  if (!/\.(step|stp)$/i.test(file?.name || '')) throw new Error('请选择 STEP 或 STP 文件。')
  if (!file.size) throw new Error('文件为空，请选择包含实体的 STEP 文件。')
  if (file.size > 20 * 1024 * 1024) throw new Error('STEP 文件不能超过 20 MB。')
}

export function engineeringNumber(value, label, { min = -1000000, max = 1000000 } = {}) {
  if (value === null || value === undefined || typeof value === 'boolean' || String(value).trim() === '' || !Number.isFinite(Number(value)) || Number(value) < min || Number(value) > max) throw new Error(`${label}请输入 ${min} 至 ${max} 范围内的有效数字。`)
  return Number(value)
}

export function engineeringDirection(value, label = '法向') {
  if (!Array.isArray(value) || value.length !== 3) throw new Error(`${label}需要 X、Y、Z 三个数字。`)
  const result = value.map((v, i) => engineeringNumber(v, `${label} ${'XYZ'[i]}`))
  if (Math.hypot(...result) < 1e-8) throw new Error(`${label}不能全部为零。`)
  return result
}

export function engineeringDrawingSettings({ page, hidden, views, dimensions }) {
  return { page, hidden, views: views.map(item => Object.fromEntries(Object.entries(item).filter(([, value]) => value !== '').map(([key, value]) => [key, ['x', 'y', 'scale', 'position', 'z'].includes(key) ? engineeringNumber(value, `视图 ${key}`, key === 'scale' ? { min: Number.MIN_VALUE, max: 100 } : {}) : key === 'normal' ? engineeringDirection(value) : value]))), dimensions }
}

export function engineeringDraftEquals(a, b) {
  const encode = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item)
  return encode(a) === encode(b)
}

export function engineeringCanvasVisible({ active, hidden, width, height }) {
  return Boolean(active && !hidden && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0)
}

// Removing a view only removes its own dimensions; later view references shift.
export function removeEngineeringView({ views, dimensions, dimension }, index) {
  if (views.length <= 1 || index < 0 || index >= views.length) return { views, dimensions, dimension, removedDimensions: 0 }
  const remaining = dimensions.filter(item => item.viewIndex !== index)
  return {
    views: views.filter((_, i) => i !== index),
    dimensions: remaining.map(item => ({ ...item, viewIndex: item.viewIndex > index ? item.viewIndex - 1 : item.viewIndex })),
    dimension: { ...dimension, viewIndex: Math.max(0, Math.min(views.length - 2, dimension.viewIndex - Number(dimension.viewIndex > index))) },
    removedDimensions: dimensions.length - remaining.length,
  }
}
