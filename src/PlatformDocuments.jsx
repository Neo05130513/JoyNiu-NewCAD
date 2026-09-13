import { useEffect, useRef, useState } from 'react'
import { API_BASE } from './api.js'
import { downloadBlob, readableError } from './workspaceFeedback.js'
import { pdmDocumentName, pdmDocumentPage } from './platformWorkspaceState.js'
import './platform-documents.css'

export default function PlatformDocuments({ documents, token, projectId, canRead = true }) {
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const requestRef = useRef(null)
  const contextRef = useRef({ token, projectId })
  contextRef.current = { token, projectId }
  useEffect(() => {
    setQuery(''); setPage(0); setBusy(''); setError('')
    return () => { requestRef.current?.abort(); requestRef.current = null }
  }, [token, projectId])
  const result = pdmDocumentPage(documents, query, page)
  const download = async (version) => {
    if (requestRef.current || !canRead) return
    const controller = new AbortController()
    requestRef.current = controller
    const scope = { token, projectId }
    const current = () => requestRef.current === controller && contextRef.current.token === scope.token && contextRef.current.projectId === scope.projectId
    const timeout = setTimeout(() => controller.abort(), 45_000)
    setBusy(version.id); setError('')
    try {
      const response = await fetch(`${API_BASE}/pdm/versions/${encodeURIComponent(version.id)}/content`, {
        headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        const failure = new Error(typeof payload.detail === 'string' ? payload.detail : '文档下载失败，请重试。')
        failure.status = response.status
        throw failure
      }
      const blob = await response.blob()
      if (current()) downloadBlob(blob, version.file_name || version.fileName || `${version.label || '文档版本'}.bin`)
    } catch (failure) {
      if (current()) setError(failure.name === 'AbortError' ? '文档下载超时，请重试。' : readableError(failure))
    } finally {
      clearTimeout(timeout)
      if (current()) { requestRef.current = null; setBusy('') }
    }
  }
  return <div className="pdm-browser">
    <label className="pdm-search">查找文档<input type="search" value={query} onChange={(event) => { setQuery(event.target.value); setPage(0) }} placeholder="搜索名称或文件名" /></label>
    <p className="pdm-browser-count">{result.total} 个文档 · 展开查看历史版本与下载</p>
    {error && <p className="parameter-error" role="alert">{error}</p>}
    {!documents.length ? <div className="platform-empty">此项目还没有文档，点击“同步当前文件”保存第一份快照。</div>
      : !result.total ? <div className="platform-empty">没有匹配的文档。<button className="text-button" onClick={() => setQuery('')}>清空搜索</button></div>
        : result.items.map((entry) => <details className="pdm-document" key={entry.document?.id || entry.id}>
          <summary><span><b>{pdmDocumentName(entry)}</b><small>{entry.document?.kind === 'drawing' ? '图纸' : '模型与资料'} · {entry.versions?.length || 0} 个版本</small></span><span aria-hidden="true">⌄</span></summary>
          <div className="pdm-version-list">{(entry.versions || []).map((version) => <div className="pdm-version" key={version.id}>
            <div><b>{version.label || `版本 ${version.revision}`}</b><small>{version.file_name || version.fileName}</small><small>{new Date(version.created_at || version.createdAt).toLocaleString('zh-CN')} · {((version.size_bytes || version.sizeBytes || 0) / 1024).toFixed(1)} KB</small>{version.note && <small>{version.note}</small>}</div>
            <button type="button" className="secondary-button" disabled={!canRead || Boolean(busy)} title={canRead ? '下载此历史版本的文件' : '当前账号没有读取文档的权限'} onClick={() => download(version)}>{busy === version.id ? '下载中…' : '下载'}</button>
          </div>)}{!entry.versions?.length && <p className="platform-help">此文档尚未保存版本。</p>}</div>
        </details>)}
    {result.pageCount > 1 && <div className="pdm-pagination"><button className="secondary-button" disabled={result.page === 0} onClick={() => setPage(result.page - 1)}>上一页</button><span>{result.page + 1} / {result.pageCount}</span><button className="secondary-button" disabled={result.page + 1 === result.pageCount} onClick={() => setPage(result.page + 1)}>下一页</button></div>}
  </div>
}
