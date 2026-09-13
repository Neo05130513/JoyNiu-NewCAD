import { API_BASE } from './api.js'
import { cadAgent, cadArtifactUrl } from './cadAgentClient.js'

/** Renew file capabilities without replacing edited dimensions or locating dimensions again. */
export function latestSourceFileRun(run, ...candidates) {
  const matching = [run, ...candidates].filter(item => item && item.runId === run?.runId && item.revision === run?.revision)
  const rank = item => [Number(item.fileLinksEpoch) || 0, (typeof item.fileLinksExpiresAt === 'number'
    ? item.fileLinksExpiresAt * (item.fileLinksExpiresAt < 1e12 ? 1000 : 1) : Date.parse(item.fileLinksExpiresAt)) || 0]
  return matching.reduce((latest, item) => {
    const a = rank(latest), b = rank(item)
    return b[0] > a[0] || b[0] === a[0] && b[1] >= a[1] ? item : latest
  }, run)
}

export async function refreshSourceFileRun({ runId, revision, token, signal, getRun = cadAgent.getRun }) {
  signal?.throwIfAborted()
  const result = await getRun({ runId, revision, token, signal })
  signal?.throwIfAborted()
  if (result?.runId !== runId || result.revision !== revision) throw new Error('返回的文件不属于当前模型版本，请重新打开当前文件。')
  if (result.fileLinksAvailable === false) throw new Error('当前文件访问权限不可用，请重新登录或联系管理员。')
  return result
}

export async function refreshProjectionFile({ artifactId, ...options }) {
  const result = await refreshSourceFileRun(options)
  const artifact = result.artifacts?.find(item => item.id === artifactId && ['png', 'svg'].includes(item.format))
  if (!artifact) throw new Error('当前版本没有这张投影图，请重新打开当前模型。')
  const base = new URL(API_BASE)
  let url
  try { url = new URL(cadArtifactUrl(artifact)) }
  catch { throw new Error('投影图地址无效，请重新打开当前模型。') }
  if (url.origin !== base.origin || url.pathname !== `${base.pathname}/cad-agent/runs/${encodeURIComponent(options.runId)}/${options.revision}/artifacts/${encodeURIComponent(artifactId)}`) {
    throw new Error('投影图地址与当前模型版本不一致，加载已停止。')
  }
  return artifact
}

export async function downloadSourceFile({ documentId, runId, revision, token, signal, getRun, fetcher = fetch }) {
  const result = await refreshSourceFileRun({ runId, revision, token, signal, getRun })
  const document = result.sourceDocuments?.find(item => item.id === documentId)
  const match = /^source-(0|[1-9][0-9]*)$/.exec(documentId || '')
  if (!document || !match || !document.downloadUrl) throw new Error('当前版本没有这份原图，请重新加载原图列表。')
  const base = new URL(API_BASE)
  let url
  try { url = new URL(cadArtifactUrl({ url: document.downloadUrl })) }
  catch { throw new Error('原图地址无效，请重新加载当前文件。') }
  const expected = `${base.pathname}/cad-agent/runs/${encodeURIComponent(runId)}/${revision}/sources/${match[1]}/download`
  if (url.origin !== base.origin || url.pathname !== expected) throw new Error('原图地址与当前服务或文件不一致，下载已停止。')
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true })
  let timedOut = false
  const timer = setTimeout(() => { timedOut = true; controller.abort() }, 120000)
  const credential = typeof token === 'function' ? token() : token
  try {
    const response = await fetcher(url.href, { signal: controller.signal, cache: 'no-store', redirect: 'error',
      headers: credential ? { Authorization: `Bearer ${credential}` } : {} })
    if (!response.ok) throw new Error(response.status === 401 || response.status === 403
      ? '原图访问权限已失效，请重新登录后重试。' : `原图下载失败（${response.status}），请稍后重试。`)
    const blob = await response.blob()
    controller.signal.throwIfAborted()
    return { run: result, blob, filename: document.filename || '原始图纸' }
  } catch (error) {
    if (timedOut && !signal?.aborted) throw new Error('原图下载超时，请稍后重试。')
    throw error
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
}
