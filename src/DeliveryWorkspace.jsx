import React, { useCallback, useEffect, useRef, useState } from 'react'
import { downloadBlob } from './workspaceFeedback.js'
import { createDeliveryIntentTracker, deliveryAccountScope, deliveryCreatePayload, deliveryErrorMessage, deliveryFileSize, deliveryFileSourceIndex, deliveryFileSourceLabel, deliveryFileDownloadName, deliverySeedSelection, deliverySelectionState, deliverySourceKey, deliverySourceKinds, deliverySourceRef, deliveryVerificationLabel, deliveryVersionLabel, deliveryWorkspaceClient, readDeliveryDraft, writeDeliveryDraft } from './deliveryWorkspaceClient.js'
import './delivery-workspace.css'

function draftStorage() { try { return globalThis.sessionStorage } catch { return undefined } }
function dateLabel(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间待核对' : date.toLocaleString('zh-CN', { hour12: false })
}
const provenanceLabels = { kind: '来源类型', sourceKind: '来源类型', id: '来源编号', runId: 'AI 任务编号', sourceRunId: '原始 AI 任务', designId: '设计编号', fileId: '文件编号', sourceId: '来源编号', revision: '来源版本', sourceRevision: '来源版本', createdAt: '创建时间', title: '名称', name: '名称', status: '来源状态', sha256: '文件校验值', filename: '文件名', fileName: '文件名', artifact: '成果文件', artifacts: '成果文件', message: '说明', verificationStatus: '核验状态', ownerId: '所属账号', gate: '交付核验', source: '原始来源', files: '文件记录', bytes: '文件大小', mimeType: '文件类型', selected: '本次手动选择', manufacturingReleased: '已制造放行', drawingAgreement: '图纸一致性', sourceFeature: '关联特征设计', instances: '装配实例', position: '位置', rotation: '旋转角度', fixed: '固定实例', assemblyPlan: '装配方案', jobId: '装配任务编号', partId: '零件编号', changeNote: '修改说明', sourceRun: '原始 AI 任务', statusAtFork: '派生时状态', planSha256: '方案校验值', artifactsIncluded: '已包含原始成果', sharedDesignId: '关联工程设计', sourceFile: '原始图纸', sourceFiles: '原始文件', confirmedAt: '来源确认时间', confirmationStatus: '来源确认状态' }
function provenanceText(value, depth = 0) {
  if (value == null) return '未提供'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value !== 'object') return String(value)
  if (depth > 4) return '更多信息已保存在交付清单中'
  if (Array.isArray(value)) return value.map(item => provenanceText(item, depth + 1)).join('\n')
  return Object.entries(value).map(([key, item]) => `${provenanceLabels[key] || key}：${provenanceText(item, depth + 1)}`).join('\n')
}

export function DeliverySourceList({ sources = [], selected = [], loading = false, error = '', disabled = false, query = '', kind = '', onSelect = () => {}, onRetry = () => {} }) {
  const selectedKeys = new Set(selected.map(deliverySourceKey))
  const filtered = sources.filter(source => (!kind || source.kind === kind) && (!query.trim() || `${source.name || ''} ${deliverySourceKinds[source.kind] || ''}`.toLowerCase().includes(query.trim().toLowerCase())))
  if (loading && !sources.length) return <p className="delivery-empty" role="status">正在读取当前账号的可交付来源…</p>
  if (error) return <div className="delivery-error" role="alert"><p>{error}</p><button type="button" disabled={loading || disabled} onClick={onRetry}>重新读取来源</button></div>
  if (!sources.length) return <div className="delivery-empty"><strong>还没有可选来源</strong><p>先在工程设计、特征设计或二维图纸中保存成果，或完成 AI 建模结果的交付确认，再回到这里刷新。</p></div>
  if (!filtered.length) return <p className="delivery-empty">没有符合当前筛选条件的来源。可以清除搜索或切换来源类型。</p>
  return <div className="delivery-source-list" aria-busy={loading}>{filtered.map(source => <label className={`delivery-source${selectedKeys.has(deliverySourceKey(source)) ? ' selected' : ''}${source.eligible !== true ? ' unavailable' : ''}`} key={deliverySourceKey(source)}>
    <input type="checkbox" checked={selectedKeys.has(deliverySourceKey(source))} disabled={disabled || loading || source.eligible !== true} onChange={() => onSelect(source)} aria-label={`选择 ${source.name || source.id}，${deliveryVersionLabel(source)}`} />
    <span className="delivery-source-content"><strong>{source.name || '未命名来源'}</strong><span className="delivery-meta">{deliverySourceKinds[source.kind] || '其他来源'} · {deliveryVersionLabel(source)}</span>{source.eligible !== true && <span className="delivery-unavailable-reason">{source.reason || '当前来源尚未满足打包条件。'}</span>}</span>
  </label>)}</div>
}

export function DeliveryPackageDetail({ detail, loading = false, error = '', downloadKey = '', downloadError = '', onDownload = () => {}, onRetry = () => {}, onNew = () => {} }) {
  if (loading) return <section className="delivery-card delivery-empty" role="status">正在读取交付包及文件清单…</section>
  if (error) return <section className="delivery-card delivery-error" role="alert"><p>{error}</p><button type="button" onClick={onRetry}>重试打开</button></section>
  if (!detail) return <section className="delivery-card delivery-empty"><strong>选择一个交付包查看文件</strong><p>已封存的包可随时重开和下载；修改来源后，请另建一个交付包保存新版本。</p></section>
  const sources = Array.isArray(detail.sources) ? detail.sources : []
  const files = Array.isArray(detail.files) ? detail.files : []
  const sourceIndex = deliveryFileSourceIndex(sources)
  return <section className="delivery-card delivery-detail" aria-label="交付包详情">
    <div className="delivery-section-title"><div><span className="delivery-eyebrow">已封存的交付包</span><h2>{detail.title}</h2><p className="delivery-meta">{dateLabel(detail.createdAt)} · {sources.length} 个来源 · {files.length} 个文件</p></div><button type="button" onClick={onNew}>开始新的交付包</button></div>
    {detail.notes && <p className="delivery-notes">{detail.notes}</p>}
    <div className="delivery-review" role="note"><strong>{deliveryVerificationLabel(detail.verification?.status)}</strong><p>{detail.verification?.message || '请按交付用途核对尺寸、材料、装配关系及图纸内容。'}</p><p>创建交付包只封存文件与来源记录，不会自动完成人工验收，也不代表制造放行。</p></div>
    <div className="delivery-section-title"><h3>整套文件</h3><button type="button" className="delivery-primary" disabled={Boolean(downloadKey) || !files.length} onClick={() => onDownload(null)}>{downloadKey === 'archive' ? '正在准备整包…' : '下载整包 ZIP'}</button></div>
    {downloadError && <p className="delivery-error" role="alert">{downloadError}</p>}
    {!files.length ? <p className="delivery-error" role="alert">此交付包没有可下载文件，请联系管理员核对记录。</p> : <ul className="delivery-files">{files.map(file => <li key={file.id}>
      <div className="delivery-file-info"><strong>{file.name}</strong><div className="delivery-file-sources">{(sourceIndex.get(file.id) || []).length ? sourceIndex.get(file.id).map(source => <span key={deliverySourceKey(source)}>{deliveryFileSourceLabel(source, detail.id)}</span>) : <span>来源未标记 · 封存版 {String(detail.id).slice(-8)}</span>}</div><span className="delivery-meta">{deliveryFileSize(file.bytes)}</span><details><summary>查看文件校验值</summary><p className="delivery-meta">SHA-256，用于核对下载文件是否与封存版本一致。</p><code>{file.sha256 || '未提供校验值'}</code></details></div>
      <button type="button" disabled={Boolean(downloadKey)} onClick={() => onDownload(file)} aria-label={`下载 ${(sourceIndex.get(file.id) || []).map(source => deliveryFileSourceLabel(source, detail.id)).join('；') || '来源未标记'} · ${file.name}`}>{downloadKey === file.id ? '下载中…' : '下载'}</button>
    </li>)}</ul>}
    <details className="delivery-provenance"><summary>来源与版本记录（{sources.length}）</summary><ul>{sources.map((source, index) => <li key={`${deliverySourceKey(source)}:${index}`}><strong>{source.name || source.id}</strong><p className="delivery-meta">{deliverySourceKinds[source.kind] || '其他来源'} · {source.revision == null ? '文件快照已锁定' : deliveryVersionLabel(source)}{source.provenance?.selected === false ? ' · 自动关联' : ''}</p>{source.provenance && <pre>{provenanceText(source.provenance)}</pre>}</li>)}</ul></details>
    {(detail.manifest || detail.archive?.sha256) && <details className="delivery-provenance"><summary>整包与交付清单校验值</summary>{detail.manifest && <><p className="delivery-meta">{detail.manifest.name || '交付清单'} · {deliveryFileSize(detail.manifest.bytes)}</p><code>{detail.manifest.sha256}</code></>}{detail.archive?.sha256 && <><p className="delivery-meta">整包 ZIP · {deliveryFileSize(detail.archive.bytes)}</p><code>{detail.archive.sha256}</code></>}</details>}
    <p className="delivery-meta delivery-package-id">交付包编号：{detail.id}</p>
  </section>
}

function DeliveryWorkspaceSession({ token, accountKey, active = true, showToast, seed, onLogin, onNavigate, client = deliveryWorkspaceClient }) {
  const initialDraftRef = useRef(null)
  if (!initialDraftRef.current) initialDraftRef.current = readDeliveryDraft(draftStorage(), accountKey)
  const [title, setTitle] = useState(initialDraftRef.current.title)
  const [notes, setNotes] = useState(initialDraftRef.current.notes)
  const [selected, setSelected] = useState(initialDraftRef.current.selected)
  const [sources, setSources] = useState([])
  const [packages, setPackages] = useState([])
  const [sourcesLoading, setSourcesLoading] = useState(Boolean(token))
  const [packagesLoading, setPackagesLoading] = useState(Boolean(token))
  const [sourcesError, setSourcesError] = useState('')
  const [packagesError, setPackagesError] = useState('')
  const [tab, setTab] = useState('create')
  const [kind, setKind] = useState('')
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [notice, setNotice] = useState('')
  const [draftStorageUnavailable, setDraftStorageUnavailable] = useState(false)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [downloadKey, setDownloadKey] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const tokenRef = useRef(token); tokenRef.current = token
  const activeRef = useRef(active); activeRef.current = active
  const toastRef = useRef(showToast); toastRef.current = showToast
  const alive = useRef(true)
  const controllers = useRef(new Set())
  const refreshController = useRef(null)
  const detailController = useRef(null)
  const openingId = useRef(null)
  const createLock = useRef(false)
  const downloadLock = useRef(false)
  const seedApplied = useRef('')
  const intents = useRef(null)
  if (!intents.current) intents.current = createDeliveryIntentTracker(initialDraftRef.current.intent)
  const credential = useCallback(() => tokenRef.current, [])
  const persist = useCallback(draft => writeDeliveryDraft(draftStorage(), accountKey, draft), [accountKey])

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; for (const controller of controllers.current) controller.abort(); controllers.current.clear() }
  }, [])
  useEffect(() => { const stored = persist({ title, notes, selected, intent: intents.current.snapshot() }); setDraftStorageUnavailable(Boolean(accountKey) && !stored) }, [title, notes, selected, persist, accountKey])

  const refresh = useCallback(async () => {
    if (!tokenRef.current || createLock.current) return
    refreshController.current?.abort()
    const controller = new AbortController(); refreshController.current = controller; controllers.current.add(controller)
    setSourcesLoading(true); setPackagesLoading(true)
    const results = await Promise.allSettled([client.sources(credential, controller.signal), client.list(credential, controller.signal)])
    controllers.current.delete(controller)
    if (!alive.current || controller.signal.aborted) return
    const [sourceResult, packageResult] = results
    if (sourceResult.status === 'fulfilled' && Array.isArray(sourceResult.value?.items)) { setSources(sourceResult.value.items); setSourcesError('') }
    else setSourcesError(deliveryErrorMessage(sourceResult.reason || new Error('来源列表返回异常，请重新读取。')))
    if (packageResult.status === 'fulfilled' && Array.isArray(packageResult.value?.items)) { setPackages(packageResult.value.items); setPackagesError('') }
    else setPackagesError(deliveryErrorMessage(packageResult.reason || new Error('交付包列表返回异常，请重新读取。')))
    setSourcesLoading(false); setPackagesLoading(false)
  }, [client, credential])

  useEffect(() => { if (active && token) void refresh() }, [token, active, refresh])
  useEffect(() => {
    if (!active || !seed || sourcesLoading || sourcesError) return
    const signature = JSON.stringify(seed)
    if (seedApplied.current === signature) return
    seedApplied.current = signature
    const result = deliverySeedSelection(seed, sources)
    setSelected(current => [...new Map([...current, ...result.selected].map(ref => [deliverySourceKey(ref), ref])).values()])
    const messages = []
    if (result.selected.length) { setTab('create'); messages.push('已选中指定来源，请填写交付包名称并核对版本。') }
    messages.push(...result.unavailable)
    setNotice(messages.join(' '))
  }, [seed, sources, sourcesError, sourcesLoading, active])

  async function openPackage(id) {
    if (createLock.current || (openingId.current === id && detailController.current)) return
    detailController.current?.abort()
    const controller = new AbortController(); detailController.current = controller; controllers.current.add(controller); openingId.current = id
    setTab('saved'); setDetail(null); setDetailLoading(true); setDetailError(''); setDownloadError('')
    try {
      const result = await client.detail(credential, id, controller.signal)
      if (alive.current && !controller.signal.aborted) setDetail(result)
    } catch (error) { if (alive.current && !controller.signal.aborted) setDetailError(deliveryErrorMessage(error)) }
    finally {
      controllers.current.delete(controller)
      if (detailController.current === controller) { detailController.current = null; if (alive.current) setDetailLoading(false) }
    }
  }

  async function createPackage(event) {
    event.preventDefault()
    if (createLock.current || sourcesLoading || sourcesError) return
    let payload
    try { payload = deliveryCreatePayload({ title, notes, selected }, sources) }
    catch (error) { setCreateError(deliveryErrorMessage(error)); return }
    createLock.current = true; setCreating(true); setCreateError(''); setNotice('')
    const requestId = intents.current.get(payload)
    persist({ title, notes, selected, intent: intents.current.snapshot() })
    const controller = new AbortController(); controllers.current.add(controller)
    try {
      const result = await client.create(credential, payload, requestId, controller.signal)
      if (!alive.current || controller.signal.aborted) return
      detailController.current?.abort(); openingId.current = result.id
      setDetail(result); setDetailError(''); setDetailLoading(false); setDownloadError(''); setTab('saved')
      setPackages(current => [{ id: result.id, title: result.title, createdAt: result.createdAt, sourceCount: result.sources?.length || 0, fileCount: result.files?.length || 0, verificationStatus: result.verification?.status }, ...current.filter(item => item.id !== result.id)])
      setPackagesError(''); setNotice('交付包已封存。请下载并进行人工验收。')
      if (activeRef.current) toastRef.current?.('交付包已封存，请继续人工验收。')
    } catch (error) { if (alive.current && !controller.signal.aborted) setCreateError(deliveryErrorMessage(error)) }
    finally { controllers.current.delete(controller); createLock.current = false; if (alive.current) setCreating(false) }
  }

  async function download(file) {
    if (downloadLock.current || !detail) return
    downloadLock.current = true; setDownloadKey(file?.id || 'archive'); setDownloadError('')
    const controller = new AbortController(); controllers.current.add(controller)
    try {
      const blob = file ? await client.file(credential, detail.id, file.id, controller.signal) : await client.archive(credential, detail.id, controller.signal)
      if (alive.current && !controller.signal.aborted) downloadBlob(blob, file ? deliveryFileDownloadName(detail, file) : `${detail.title || '交付包'}.zip`)
    } catch (error) { if (alive.current && !controller.signal.aborted) setDownloadError(`下载${file?.name || '整包 ZIP'}未完成：${deliveryErrorMessage(error)}`) }
    finally { controllers.current.delete(controller); downloadLock.current = false; if (alive.current) setDownloadKey('') }
  }

  function newPackage() {
    if (createLock.current) return
    setTitle(''); setNotes(''); setSelected([]); intents.current.clear(); setCreateError(''); setNotice(''); setTab('create')
  }
  function toggleSource(source) {
    if (createLock.current || source.eligible !== true) return
    if (selected.length >= 20 && !selected.some(ref => deliverySourceKey(ref) === deliverySourceKey(source))) { setCreateError('一个交付包最多选择 20 个来源。'); return }
    const key = deliverySourceKey(source)
    setSelected(current => current.some(ref => deliverySourceKey(ref) === key) ? current.filter(ref => deliverySourceKey(ref) !== key) : [...current, deliverySourceRef(source)])
    setCreateError(''); setNotice('')
  }
  if (!token) return <div className="delivery-workspace"><div className="delivery-card delivery-empty"><h1>交付中心</h1><p>请先登录，查看和封存自己账号的设计成果。</p>{onLogin&&<button className="delivery-primary" onClick={onLogin}>登录并继续交付</button>}</div></div>
  const selection = deliverySelectionState(selected, sources)
  const unavailable = !sourcesLoading && !sourcesError && selection.some(source => source.eligible !== true)
  const hasSelection = selection.length > 0
  return <div className="delivery-workspace" aria-label="交付中心">
    <header className="delivery-heading"><div><span className="delivery-eyebrow">设计成果 · 版本留档</span><h1>交付中心</h1><p>选好来源，封存整套文件，交付时有据可查。</p></div><button type="button" disabled={sourcesLoading || packagesLoading || creating} onClick={refresh}>{sourcesLoading || packagesLoading ? '刷新中…' : '刷新来源与清单'}</button></header>
    <nav className="delivery-tabs" aria-label="交付中心视图"><button type="button" disabled={creating} aria-pressed={tab === 'create'} onClick={() => setTab('create')}>新建交付包</button><button type="button" disabled={creating} aria-pressed={tab === 'saved'} onClick={() => setTab('saved')}>已保存交付包{packages.length ? `（${packages.length}）` : ''}</button></nav>
    {notice && <p className="delivery-notice" role="status">{notice}</p>}
    {draftStorageUnavailable && <p className="delivery-error" role="status">当前浏览器无法留存交付草稿。离开或刷新前，请复制名称和备注；已封存的交付包不受影响。</p>}
    <form className="delivery-create-layout" hidden={tab !== 'create'} onSubmit={createPackage}>
      <section className="delivery-card"><div className="delivery-section-title"><h2>1. 选择来源</h2><span className="delivery-meta">已选 {selection.length} 个</span></div><p className="delivery-meta">仅显示当前账号的成果，每包最多选 20 个来源。尚未通过原有交付检查的结果不能打包。</p>
        <div className="delivery-filters"><label>来源类型<select value={kind} onChange={event => setKind(event.target.value)}><option value="">全部来源</option>{Object.entries(deliverySourceKinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>搜索名称<input value={query} onChange={event => setQuery(event.target.value)} type="search" placeholder="输入设计或图纸名称" /></label></div>
        {!sourcesLoading&&!sourcesError&&!sources.length&&onNavigate&&<div className="delivery-empty-actions"><button type="button" onClick={()=>onNavigate('原生二维')}>创建二维图纸</button><button type="button" onClick={()=>onNavigate('特征编辑')}>创建三维模型</button></div>}
        <DeliverySourceList sources={sources} selected={selected} loading={sourcesLoading} error={sourcesError} disabled={creating} query={query} kind={kind} onSelect={toggleSource} onRetry={refresh} />
      </section>
      <section className="delivery-card delivery-compose"><h2>2. 命名并封存</h2><fieldset disabled={creating}><label>交付包名称<input value={title} maxLength={180} onChange={event => { setTitle(event.target.value); setCreateError('') }} placeholder="例如：减速器样机 · 首轮交付" required /></label><label>交付备注<span className="delivery-meta">可填写用途、客户要求或需要重点核对的内容。</span><textarea value={notes} maxLength={4000} onChange={event => setNotes(event.target.value)} rows={4} placeholder="选填" /></label>
        <div className="delivery-selected"><h3>本次来源</h3>{!hasSelection ? <p className="delivery-meta">从左侧选择成果；手机上请先在上方选择。</p> : <ul>{selection.map(source => <li key={deliverySourceKey(source)}><span><strong>{source.name || source.id}</strong><span className="delivery-meta">{deliverySourceKinds[source.kind]} · {deliveryVersionLabel(source)}</span>{!sourcesLoading && !sourcesError && source.eligible !== true && <span className="delivery-unavailable-reason">{source.reason || '此来源已不可打包，请移除后重新选择。'}</span>}</span><button type="button" onClick={() => { setSelected(current => current.filter(ref => deliverySourceKey(ref) !== deliverySourceKey(source))); setCreateError('') }} aria-label={`移除 ${source.name || source.id}`}>移除</button></li>)}</ul>}</div>
        <p className="delivery-review-note">封存后，文件与来源版本不再随设计修改而改变。创建交付包不会自动确认模型或完成人工验收。</p>
        {createError && <p className="delivery-error" role="alert">{createError}</p>}
        <button className="delivery-primary delivery-create-button" type="submit" disabled={creating || !title.trim() || !hasSelection || sourcesLoading || Boolean(sourcesError) || unavailable}>{creating ? '正在封存文件…' : '创建交付包'}</button>
        {creating && <p className="delivery-meta" role="status">正在固定版本并核对文件，完成前请保留此页面。</p>}
      </fieldset></section>
    </form>
    <div className="delivery-saved-layout" hidden={tab !== 'saved'}>
      <aside className="delivery-card delivery-history" aria-label="已保存交付包"><h2>已保存交付包</h2>{packagesLoading && <p role="status" className="delivery-meta">正在更新交付清单…</p>}{packagesError && <div className="delivery-error" role="alert"><p>{packagesError}</p><button type="button" disabled={packagesLoading || creating} onClick={refresh}>重新读取清单</button></div>}{!packagesLoading && !packagesError && !packages.length && <div className="delivery-empty"><strong>还没有交付包</strong><p>选择设计成果后创建第一份交付包，文件会保存在这里。</p><button type="button" onClick={() => setTab('create')}>去选择来源</button></div>}{packages.map(item => <button type="button" className={`delivery-history-item${detail?.id === item.id ? ' selected' : ''}`} key={item.id} disabled={creating || (detailLoading && openingId.current === item.id)} onClick={() => openPackage(item.id)} aria-pressed={detail?.id === item.id}><strong>{item.title}</strong><span className="delivery-meta">{item.sourceCount} 个来源 · {item.fileCount} 个文件</span><span className="delivery-meta">{dateLabel(item.createdAt)}</span><span className="delivery-status">{deliveryVerificationLabel(item.verificationStatus)}</span></button>)}</aside>
      <DeliveryPackageDetail detail={detail} loading={detailLoading} error={detailError} downloadKey={downloadKey} downloadError={downloadError} onDownload={download} onRetry={() => openPackage(openingId.current)} onNew={newPackage} />
    </div>
  </div>
}

export default function DeliveryWorkspace(props) {
  return <DeliveryWorkspaceSession key={deliveryAccountScope(props.accountKey, props.token)} {...props} />
}
