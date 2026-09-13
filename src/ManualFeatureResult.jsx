import { useEffect, useRef, useState } from 'react'
import { EngineeringModelCanvas } from './EngineeringWorkspace.jsx'
import CadModelViewport from './CadModelViewport.jsx'
import { directFeatureClient } from './directFeatureClient.js'
import { manualFeatureRecordMatchesReference, normalizeManualFeatureReference } from './manualFeatureReference.js'
import { downloadBlob, readableError } from './workspaceFeedback.js'
import './manual-feature-result.css'

export default function ManualFeatureResult({ reference, token, accountKey, active = true, onEdit, onShowOriginal, showToast }) {
  const pointer = normalizeManualFeatureReference(reference)
  const referenceKey = JSON.stringify(pointer)
  const allowed = Boolean(pointer && token && (!accountKey || accountKey === pointer.accountKey))
  const [loaded, setLoaded] = useState(null), [error, setError] = useState(''), [retry, setRetry] = useState(0), [downloading, setDownloading] = useState('')
  const scope = useRef(null), credentials = useRef(token), downloadRequest = useRef(null)
  credentials.current = token
  // Invalidate visible records immediately during render, before effect cleanup.
  scope.current = { referenceKey, token, active, allowed }
  const current = stamp => stamp.referenceKey === scope.current.referenceKey && stamp.token === scope.current.token && scope.current.active && scope.current.allowed
  const record = allowed && loaded?.referenceKey === referenceKey && loaded.token === token ? loaded.record : null
  useEffect(() => {
    const controller = new AbortController(), stamp = { referenceKey, token }
    downloadRequest.current?.abort(); downloadRequest.current = null
    setDownloading(''); setError(''); setLoaded(null)
    if (allowed && active) directFeatureClient.get(() => credentials.current, pointer.featureId, pointer.revision, controller.signal)
      .then(value => {
        if (controller.signal.aborted || !current(stamp)) return
        if (!manualFeatureRecordMatchesReference(value, pointer)) throw new Error('返回的手工版本与来源记录不一致，已停止预览和下载。')
        setLoaded({ ...stamp, record: value })
      }).catch(reason => { if (!controller.signal.aborted && current(stamp)) setError(readableError(reason)) })
    return () => { controller.abort(); downloadRequest.current?.abort(); downloadRequest.current = null }
  }, [referenceKey, token, accountKey, active, retry])
  const download = async format => {
    if (!record || !active || downloadRequest.current || !record.artifacts?.[format]) return
    const controller = new AbortController(), stamp = { referenceKey, token }
    downloadRequest.current = controller; setDownloading(format); setError('')
    try {
      const blob = await directFeatureClient.download(() => credentials.current, record, format, controller.signal)
      if (controller.signal.aborted || !current(stamp)) return
      if (!(blob instanceof Blob) || !blob.size) throw new Error('手工文件为空，请重新读取该版本。')
      const name = String(record.name || pointer.name).replace(/[\\/\u0000-\u001f]/g, '_')
      downloadBlob(blob, `${name}-手工-r${record.revision}.${format}`, blob.type || 'application/octet-stream')
      showToast?.(`手工版本 r${record.revision} · ${format.toUpperCase()} 已准备下载`)
    } catch (reason) {
      if (!controller.signal.aborted && current(stamp)) { const message = readableError(reason); setError(message); showToast?.(message, 'error') }
    } finally {
      if (downloadRequest.current === controller) { downloadRequest.current = null; if (current(stamp)) setDownloading('') }
    }
  }
  if (!pointer) return <section className="manual-feature-result"><p role="alert">手工版本引用无效，请从特征编辑中重新打开。</p>{onShowOriginal && <button onClick={onShowOriginal}>查看原 AI 版本</button>}</section>
  const shared = record?.sharedDesign
  const preview = shared?.sourceFeature?.id === pointer.featureId && shared.sourceFeature?.revision === pointer.revision ? shared : null
  return <section className="manual-feature-result" aria-label="手工修改结果">
    <header><div><h2>{pointer.name}</h2><p>{pointer.status === 'draft' ? '手工草稿' : '手工修改'} · r{pointer.revision} · 原图一致性未核验</p></div><div className="manual-feature-actions">
      <button className="primary-button" disabled={!allowed || !active || Boolean(downloading)} onClick={() => onEdit?.(pointer)}>继续编辑</button>
      {onShowOriginal && <button className="secondary-button" onClick={onShowOriginal}>查看原 AI 版本</button>}
    </div></header>
    {!allowed ? <p role="status">请使用保存此版本的账号登录。</p> : error ? <div className="manual-feature-error" role="alert"><span>{error}</span><button disabled={!active || Boolean(downloading)} onClick={() => setRetry(value => value + 1)}>重新读取版本</button></div> : !record && <p role="status">{active ? '正在读取已保存的手工版本…' : '返回页面后读取手工版本。'}</p>}
    {record?.status === 'draft' && <p role="status">草稿已保存，继续编辑并重建后可预览和下载实体。</p>}
    {record?.status === 'built' && <>
      {record.artifacts?.glb?.url ? <div style={{height:'min(68vh,720px)',minHeight:360}}><CadModelViewport key={`${record.id}:${record.revision}`} active={active} token={token} fallback={{glbUrl:record.artifacts.glb.url}} pickKind="none" /></div> : preview ? <EngineeringModelCanvas key={`${record.id}:${record.revision}`} active={active} token={token} design={preview} /> : <p>该版本没有可用的三维预览，可下载实体文件或继续编辑。</p>}
      <div className="manual-feature-downloads">{['step', 'glb'].map(format => <button key={format} disabled={!active || Boolean(downloading) || !record.artifacts?.[format]} onClick={() => download(format)}>{downloading === format ? '正在准备下载…' : `下载手工 ${format.toUpperCase()}`}</button>)}<span>保存版本 r{record.revision}</span></div>
      <details><summary>修改来源与实体检查</summary><p>手工输出与原 AI 模型分别保存；此版本不继承原图核验或 AI 确认结果。</p>
        {record.sourceRun && <p>来源 AI 修订 r{record.sourceRun.revision}</p>}
        {record.changeNote && <p>{record.changeNote}</p>}
        {record.inspection && <p>{Number.isInteger(record.inspection.solidCount) ? `${record.inspection.solidCount} 个实体` : '已完成实体重建'}{Number.isFinite(record.inspection.volumeMm3) ? ` · 体积 ${record.inspection.volumeMm3.toFixed(2)} mm³` : ''}{record.inspection.stepReadback?.valid === true ? ' · STEP 回读通过' : ''}</p>}
      </details>
    </>}
  </section>
}
