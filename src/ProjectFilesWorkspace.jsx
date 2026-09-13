import { useEffect, useMemo, useRef, useState } from 'react'
import { FILE_TYPES, getActiveFile, getActiveProject, importProjectBackupFile } from './projectStore.js'
import { cadParameterRows } from './cadAgentState.js'
import { cadParameterLabel } from './cadParameterLabels.js'
import './project-files.css'

function FileIcon({ type = 'folder' }) {
  const paths = {
    folder: <path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" />,
    all: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
    零件: <><path d="m12 2 9 5v10l-9 5-9-5V7Z M3 7l9 5 9-5 M12 12v10 M7.5 4.5l9 5v5" /></>,
    工程图: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M7 7h7v7H7Z M17 7v10H7 M14 17h3" /></>,
    装配体: <><path d="m8 3 5 3v6l-5 3-5-3V6Z M3 6l5 3 5-3 M8 9v6 M13 10l3-2 5 3v6l-5 3-5-3v-3 M16 14v6 M13 12l3 2 5-3" /></>,
    文档: <><path d="M14 3H5v18h14V8Z M14 3v5h5 M8 12h8 M8 16h6" /></>,
  }
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[type] || paths.folder}</svg>
}
const dateLabel = (value) => {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
const sizeLabel = (snapshot) => {
  const bytes = new TextEncoder().encode(JSON.stringify(snapshot || {})).length
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`
}

export function BackupImportButton({ onImport, className = 'pf-button', children = '导入备份' }) {
  const inputRef = useRef(null)
  const importRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => () => { importRef.current?.abort(); importRef.current = null }, [])
  const importFile = async (event) => {
    const file = event.target.files?.[0]
    if (!file || importRef.current) return
    const controller = new AbortController()
    importRef.current = controller
    setMessage(''); setFailed(false); setBusy(true)
    try {
      await importProjectBackupFile(file, onImport, { signal: controller.signal })
      if (controller.signal.aborted) return
      setMessage(`已导入 ${file.name}（${(file.size / 1024).toFixed(1)} KB）`)
    } catch (error) {
      if (!controller.signal.aborted) { setFailed(true); setMessage(error.message || '导入失败，请重试。') }
    } finally {
      if (importRef.current === controller) { importRef.current = null; setBusy(false); if (inputRef.current) inputRef.current.value = '' }
    }
  }
  return <span className="pf-backup-import"><input ref={inputRef} type="file" accept=".json,application/json" hidden onChange={importFile} aria-label="选择项目备份文件" />
    <button type="button" className={className} disabled={busy || !onImport} onClick={() => inputRef.current?.click()}>{busy ? '正在导入…' : children}</button>
    {message && <span className={`pf-import-message${failed ? ' is-error' : ''}`} role={failed ? 'alert' : 'status'}>{message}</span>}
  </span>
}

function SnapshotDetails({ snapshot, title }) {
  const model = snapshot?.model
  const rows = model?.cadPlan ? cadParameterRows(model.cadPlan).map((row) => ({ ...row,
    value: row.expression ? (!model.agentRun?.dirty ? model.agentRun?.resolvedParameters?.[row.key] : null) : row.value }))
    : Object.entries(model || {}).filter(([key, value]) => !['name', 'kind', 'type', 'recipeId', 'updatedAt', 'material', 'units'].includes(key) && typeof value === 'number')
      .map(([key, value], index) => ({ key, label: cadParameterLabel(key, {}, index), value }))
  return <div className="pf-snapshot-details">
    <h4>{title}</h4>
    {typeof snapshot?.documentText === 'string' ? <pre className="pf-document-preview">{snapshot.documentText || '此版本尚无正文。'}</pre> : <>
      <p>{model?.name || '空白文件 · 尚无模型'}{model?.material ? ` · ${model.material}` : ''}</p>
      <dl>{rows.map((row) => <div key={row.key}><dt>{row.label}</dt><dd>{row.value == null ? '待确定' : String(row.value)}{row.unit ? ` ${row.unit}` : ''}</dd></div>)}</dl>
      <p className="pf-muted">图纸：{snapshot?.drawingJob?.fileMeta?.name || '未附加'} · 装配实例：{snapshot?.assemblyItems?.length || 0} · 对话：{snapshot?.messages?.length || 0} 条</p>
    </>}
  </div>
}

/** Every file action receives its actual target; no action assumes the first row is the current file. */
export default function ProjectFilesWorkspace({
  store, onSelectProject, onCreateProject, onRenameProject, onCreateFile, onRenameFile,
  onOpenFile, onDownloadFile, onSaveVersion, onRestoreVersion, onUpdateDocument,
  onDuplicateFile, onDeleteFile, onDeleteProject, onRestoreTrash, onExportBackup, onImportBackup,
  storageError = '',
}) {
  const project = getActiveProject(store)
  const activeFile = getActiveFile(store)
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState('all')
  const [fileType, setFileType] = useState('全部')
  const [menuFileId, setMenuFileId] = useState(null)
  const [sort, setSort] = useState('updated')
  const [versionFileId, setVersionFileId] = useState(null)
  const [previewVersionId, setPreviewVersionId] = useState(null)
  const [documentFileId, setDocumentFileId] = useState(null)
  const [dialog, setDialog] = useState(null)
  const [name, setName] = useState('')
  const [type, setType] = useState('零件')
  const [source, setSource] = useState('blank')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [showTrash, setShowTrash] = useState(false)
  const dialogRef = useRef(null)
  const previousProjectRef = useRef(project?.id)
  const browsingProjectRef = useRef(false)
  const detailsRef = useRef(null)
  const allFiles = useMemo(() => store.projects.flatMap((item) => item.files.map((file) => ({ ...file, projectId: item.id, projectName: item.name }))), [store.projects])
  const searching = Boolean(query.trim())
  const scopedFiles = useMemo(() => allFiles.filter((file) => (searching || scope === 'all' || file.projectId === project?.id)
    && `${file.name} ${file.type} ${file.projectName}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [allFiles, scope, project?.id, query, searching])
  const files = useMemo(() => scopedFiles.filter((file) => fileType === '全部' || file.type === fileType).slice().sort((a, b) => sort === 'name'
    ? a.name.localeCompare(b.name, 'zh-CN')
    : sort === 'type' ? a.type.localeCompare(b.type, 'zh-CN') || a.name.localeCompare(b.name, 'zh-CN')
      : String(sort === 'opened' ? b.lastOpenedAt || b.updatedAt : b.updatedAt).localeCompare(String(sort === 'opened' ? a.lastOpenedAt || a.updatedAt : a.updatedAt))), [scopedFiles, fileType, sort])
  const versionFile = allFiles.find((file) => file.id === versionFileId)
  const previewVersion = versionFile?.versions.find((version) => version.id === previewVersionId)
  const documentFile = allFiles.find((file) => file.id === documentFileId && file.type === '文档')

  useEffect(() => {
    setVersionFileId(null); setPreviewVersionId(null); setDocumentFileId(null); setQuery(''); setError(''); setDialog(null)
    setMenuFileId(null); setFileType('全部'); setShowTrash(false)
    if (previousProjectRef.current !== project?.id) setScope('project')
    previousProjectRef.current = project?.id
  }, [project?.id])
  useEffect(() => {
    if (!browsingProjectRef.current && activeFile?.type === '文档' && !activeFile.contentUnavailable) { setDocumentFileId(activeFile.id); setVersionFileId(null); setShowTrash(false) }
    browsingProjectRef.current = false
  }, [activeFile?.id, activeFile?.type])
  useEffect(() => {
    if (dialog && dialogRef.current && !dialogRef.current.open) {
      dialogRef.current.showModal()
      dialogRef.current.querySelector('input:not([type="hidden"]), textarea, select')?.focus()
    }
  }, [dialog])
  useEffect(() => {
    if (versionFileId || documentFileId || showTrash) detailsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [versionFileId, documentFileId, showTrash])
  useEffect(() => {
    if (!menuFileId) return
    const outside = (event) => { if (!event.target.closest('.pf-actions-menu')) setMenuFileId(null) }
    const escape = (event) => { if (event.key === 'Escape') { document.getElementById(`pf-menu-${menuFileId}`)?.focus(); setMenuFileId(null) } }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [menuFileId])

  const openDialog = (mode, file = null) => {
    setMenuFileId(null)
    setDialog({ mode, file }); setError(''); setNote('')
    setName(mode === 'renameProject' ? project?.name || '' : mode === 'renameFile' ? file?.name || '' : mode === 'duplicateFile' ? `${file?.name || '文件'}（副本）` : '')
    setType('零件'); setSource('blank')
  }
  const closeDialog = () => { dialogRef.current?.close(); setDialog(null); setError('') }
  const submit = async (event) => {
    event.preventDefault()
    if (submitting) return
    if (!['saveVersion', 'deleteFile', 'deleteProject'].includes(dialog.mode) && !name.trim()) { setError('名称不能为空。'); return }
    setSubmitting(true)
    try {
      let result
      if (dialog.mode === 'createProject') result = await onCreateProject?.(name.trim(), '项目管理')
      if (dialog.mode === 'renameProject') result = await onRenameProject?.(project.id, name.trim())
      if (dialog.mode === 'createFile') result = await onCreateFile?.({ name: name.trim(), type, source, projectId: dialog.file?.projectId || project?.id })
      if (dialog.mode === 'renameFile') result = await onRenameFile?.(dialog.file.id, name.trim(), dialog.file.projectId)
      if (dialog.mode === 'saveVersion') result = await onSaveVersion?.(dialog.file.id, note.trim(), dialog.file.projectId)
      if (dialog.mode === 'duplicateFile') result = await onDuplicateFile?.(dialog.file.id, name.trim(), dialog.file.projectId)
      if (dialog.mode === 'deleteFile') result = await onDeleteFile?.(dialog.file.id, dialog.file.projectId)
      if (dialog.mode === 'deleteProject') result = await onDeleteProject?.(project.id)
      if (result === false) { setError('操作尚未完成，请确认当前任务已结束后重试。'); return }
      if (['renameFile', 'duplicateFile'].includes(dialog.mode) && searching) setQuery(name.trim())
      closeDialog()
    } catch (failure) { setError(failure?.message || '操作失败，请重试。') }
    finally { setSubmitting(false) }
  }
  const runAction = async (action) => {
    setError('')
    try { if (await action?.() === false) setError('操作尚未完成，请确认当前任务已结束后重试。') }
    catch (failure) { setError(failure?.message || '操作失败，请重试。') }
  }
  const openFile = (file) => {
    browsingProjectRef.current = false
    if (onOpenFile?.(file) === false) return
    if (file.type === '文档') { setDocumentFileId(file.id); setVersionFileId(null); setShowTrash(false) }
  }
  const selectProject = (projectId) => {
    browsingProjectRef.current = projectId !== project?.id
    if (onSelectProject?.(projectId) === false) { browsingProjectRef.current = false; return }
    setScope('project'); setQuery(''); setFileType('全部'); setShowTrash(false)
    setMenuFileId(null); setVersionFileId(null); setDocumentFileId(null)
  }
  const showVersions = (file) => { setVersionFileId(file.id); setPreviewVersionId(null); setDocumentFileId(null); setShowTrash(false); setMenuFileId(null) }
  const dialogTitles = { createProject: '新建项目', renameProject: '重命名项目', createFile: '新建文件', renameFile: '重命名文件', saveVersion: '保存设计版本', duplicateFile: '复制文件', deleteFile: '删除文件', deleteProject: '删除项目' }

  return <div className="pf-workspace">
    <header className="pf-heading">
      <div><h1>我的项目 <span>{store.projects.length}</span></h1><p>所有设计集中在这里，点击文件即可继续工作。</p></div>
      <button type="button" className="pf-button pf-primary" onClick={() => openDialog('createProject')}>＋ 新建项目</button>
    </header>
    {storageError && <p className="pf-error" role="alert">{storageError}</p>}
    {error && !dialog && <p className="pf-error" role="alert">{error}</p>}
    <div className="pf-explorer">
      <aside className="pf-project-rail" aria-label="项目导航">
        <button type="button" className={`pf-project-item pf-all-files${scope === 'all' && !searching ? ' is-selected' : ''}`} aria-pressed={scope === 'all' && !searching} onClick={() => { setScope('all'); setQuery(''); setFileType('全部'); setShowTrash(false); setDocumentFileId(null); setVersionFileId(null) }}><FileIcon type="all" /><span>全部文件</span><small>{allFiles.length}</small></button>
        <div className="pf-rail-label">项目列表 <span>{store.projects.length}</span></div>
        <ul className="pf-project-list">{store.projects.map((item) => <li key={item.id}><button type="button" title={item.name} className={`pf-project-item${scope === 'project' && !searching && item.id === project?.id ? ' is-selected' : ''}`} aria-pressed={scope === 'project' && !searching && item.id === project?.id} onClick={() => selectProject(item.id)}><FileIcon /><span>{item.name}</span><small>{item.files.length}</small></button></li>)}</ul>
        <div className="pf-rail-footer"><span className={`pf-save-indicator${storageError ? ' is-error' : ''}`} />{storageError ? '工作区保存异常' : '保存进度请查看顶部状态'}</div>
      </aside>
      <div className="pf-explorer-content">
        <label className="pf-global-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></svg><input type="search" aria-label="搜索全部项目中的文件" value={query} onChange={(event) => { setQuery(event.target.value); setFileType('全部'); setMenuFileId(null) }} placeholder="搜索文件名称、类型或所属项目…" />{query && <button type="button" aria-label="清空文件搜索" onClick={() => setQuery('')}>×</button>}</label>
        <section className="pf-files-card" aria-label="项目文件列表">
          <div className="pf-collection-heading"><div><h2 title={scope === 'project' && !searching ? project?.name : undefined}>{searching ? '搜索结果' : scope === 'all' ? '全部文件' : project?.name || '项目文件'}</h2><p>{searching ? `在全部项目中找到 ${scopedFiles.length} 个文件` : scope === 'all' ? `${store.projects.length} 个项目 · ${allFiles.length} 个文件，跨项目直接打开` : `${project?.files.length || 0} 个文件 · 修改后自动保存`}</p></div>
            {scope === 'project' && !searching && project && <div className="pf-heading-actions"><details className="pf-project-settings" key={project.id} onKeyDown={(event) => { if (event.key === 'Escape') { event.currentTarget.open = false; event.currentTarget.querySelector('summary')?.focus() } }}><summary className="pf-button">项目设置</summary><div><button type="button" className="pf-button" onClick={(event) => { event.currentTarget.closest('details').open = false; openDialog('renameProject') }}>重命名项目</button><button type="button" className="pf-button pf-danger" disabled={!onDeleteProject} onClick={(event) => { event.currentTarget.closest('details').open = false; openDialog('deleteProject') }}>删除项目</button></div></details><button type="button" className="pf-button pf-primary" onClick={() => openDialog('createFile')}>＋ 新建文件</button></div>}
          </div>
          <div className="pf-toolbar"><div className="pf-type-filters" role="group" aria-label="筛选文件类型">{['全部', ...FILE_TYPES].map((item) => <button key={item} type="button" aria-pressed={fileType === item} onClick={() => { setFileType(item); setMenuFileId(null) }}>{item}<span>{item === '全部' ? scopedFiles.length : scopedFiles.filter((file) => file.type === item).length}</span></button>)}</div><label className="pf-sort"><span className="pf-sr-only">文件排序</span><select value={sort} onChange={(event) => setSort(event.target.value)}><option value="updated">最近修改</option><option value="opened">最近打开</option><option value="name">文件名称</option><option value="type">文件类型</option></select></label></div>
          {files.length ? <ul className="pf-file-list">{files.map((file) => <li key={file.id} className={`pf-file-card${file.id === activeFile?.id && file.projectId === project?.id ? ' pf-active-file' : ''}${menuFileId === file.id ? ' has-menu' : ''}`}>
            <button type="button" className="pf-file-open" disabled={file.contentUnavailable} onClick={() => openFile(file)} aria-label={`${file.type === '文档' ? '编辑文档' : '打开'}：${file.name}`}>
              <span className="pf-file-top"><span className={`pf-file-icon pf-type-${FILE_TYPES.indexOf(file.type)}`}><FileIcon type={file.type} /></span><span className="pf-file-kind">{file.type}</span>{file.id === activeFile?.id && file.projectId === project?.id && <span className="pf-current-badge">当前打开</span>}</span>
              <strong title={file.name}>{file.name}</strong><span className="pf-file-subtitle">{file.contentUnavailable ? '仅保留旧版文件名，内容不可用' : file.type === '文档' ? '设计说明与交付文档' : file.snapshot?.model?.kind ? `${file.snapshot.model.material || '设计草稿'} · 可继续编辑` : '空白草稿 · 等待开始设计'}</span>
            </button>
            <div className="pf-card-location"><button type="button" title={`查看项目：${file.projectName}`} onClick={() => selectProject(file.projectId)}><FileIcon /><span>{file.projectName}</span></button><span title={`修改于 ${dateLabel(file.updatedAt)} · ${sizeLabel(file.snapshot)} 快照`}>{dateLabel(sort === 'opened' ? file.lastOpenedAt || file.updatedAt : file.updatedAt)}</span></div>
            <div className="pf-card-footer"><button type="button" className="pf-version-link" aria-label={`${file.name}的版本记录（${file.versions.length}）`} onClick={() => showVersions(file)}>{file.versions.length ? `${file.versions.at(-1).label} · ${file.versions.length} 个版本` : '工作草稿'}</button>
              <div className="pf-card-actions" role="group" aria-label={`${file.name}的操作`}><button type="button" className="pf-open-link" disabled={file.contentUnavailable} onClick={() => openFile(file)}>{file.type === '文档' ? '编辑文档' : '打开'} <span aria-hidden="true">↗</span></button><div className="pf-actions-menu"><button type="button" id={`pf-menu-${file.id}`} className="pf-more-button" aria-label={`更多操作：${file.name}`} aria-expanded={menuFileId === file.id} aria-controls={menuFileId === file.id ? `pf-actions-${file.id}` : undefined} onClick={() => setMenuFileId((current) => current === file.id ? null : file.id)}>···</button>
                {menuFileId === file.id && <div id={`pf-actions-${file.id}`} className="pf-file-menu" role="group" aria-label={`${file.name}的更多操作`}>{file.contentUnavailable && <button type="button" onClick={() => { openDialog('createFile', file); setName(`${file.name} · 新建`); setType(file.type) }}>新建此类文件</button>}<button type="button" disabled={file.contentUnavailable} onClick={() => { setMenuFileId(null); runAction(() => onDownloadFile?.(file)) }}>下载文件</button><button type="button" onClick={() => openDialog('renameFile', file)}>重命名</button><button type="button" disabled={file.contentUnavailable || !onDuplicateFile} onClick={() => openDialog('duplicateFile', file)}>复制文件</button><button type="button" disabled={file.contentUnavailable} onClick={() => openDialog('saveVersion', file)}>保存版本</button><button type="button" onClick={() => showVersions(file)}>版本记录（{file.versions.length}）</button><button type="button" className="pf-menu-danger" disabled={!onDeleteFile} onClick={() => openDialog('deleteFile', file)}>移入回收站</button></div>}
              </div></div>
            </div>
          </li>)}</ul> : <div className="pf-empty"><span className="pf-empty-icon"><FileIcon type={fileType === '全部' ? 'folder' : fileType} /></span><b>{searching || fileType !== '全部' ? '没有找到匹配的文件' : '这里还没有文件'}</b><p>{searching ? '试试其他关键词，或清空搜索查看全部文件。' : fileType !== '全部' ? '此分类暂无文件，可以切换到全部类型。' : scope === 'project' ? '新建零件、工程图、装配或文档，开始这个项目。' : '从左侧选择一个项目新建文件，或创建新项目。'}</p>{(searching || fileType !== '全部') ? <button type="button" className="pf-button" onClick={() => { setQuery(''); setFileType('全部') }}>清除筛选</button> : <button type="button" className="pf-button pf-primary" onClick={() => openDialog(scope === 'project' ? 'createFile' : 'createProject')}>{scope === 'project' ? '＋ 新建文件' : '＋ 新建项目'}</button>}</div>}
          <footer className="pf-list-footer">显示 {files.length} 个文件{fileType !== '全部' ? ` · ${fileType}` : ''}<span>点击文件名称打开 · 更多操作见 ···</span></footer>
        </section>
      </div>
    </div>
    <div className="pf-backup-bar"><div className="pf-heading-actions"><button type="button" className="pf-button" disabled={!onExportBackup} onClick={() => runAction(onExportBackup)}>备份全部项目</button><BackupImportButton onImport={onImportBackup} /><button type="button" className="pf-button" aria-expanded={showTrash} onClick={() => { setShowTrash((value) => !value); setVersionFileId(null); setDocumentFileId(null) }}>回收站（{store.trash?.length || 0}）</button></div><p>定期备份，换浏览器也能恢复设计与版本。原图和实体文件仍从原 CAD 服务读取。</p></div>
    <div ref={detailsRef} className="pf-detail-anchor" />
    {showTrash && <section className="pf-versions-card" aria-label="回收站"><div className="pf-section-heading"><div><h2>回收站</h2><p>删除的项目和文件随当前工作区保存，可在这里恢复；云端同步进度请查看顶部状态。</p></div><button type="button" className="pf-button" onClick={() => setShowTrash(false)}>收起回收站</button></div>
      {store.trash?.length ? <ul className="pf-version-list">{[...store.trash].reverse().map((record) => <li key={record.id}><div><b>{record.item.name}</b><span>{record.kind === 'project' ? `项目 · ${record.item.files.length} 个文件` : `${record.item.type} · ${record.projectName}`}</span><p>删除于 {dateLabel(record.deletedAt)}</p></div><button type="button" className="pf-button" disabled={!onRestoreTrash} onClick={() => runAction(() => onRestoreTrash(record.id))}>恢复</button></li>)}</ul> : <div className="pf-empty"><p>回收站为空。</p></div>}
    </section>}

    {documentFile && <section className="pf-document-card" aria-label="文档编辑器"><div className="pf-section-heading"><div><h2>{documentFile.name}</h2><p>正文随输入保存到此文件的工作草稿；保存版本后可回看。</p></div><button type="button" className="pf-button" onClick={() => setDocumentFileId(null)}>收起编辑器</button></div><label className="pf-document-label">文档正文<textarea aria-label="文档正文" value={documentFile.snapshot.documentText || ''} onChange={(event) => onUpdateDocument?.(documentFile.id, event.target.value, documentFile.projectId)} placeholder="记录设计要求、尺寸说明或交付备注…" /></label><div className="pf-heading-actions"><button type="button" className="pf-button" onClick={() => onDownloadFile?.(documentFile)}>下载文档</button><button type="button" className="pf-button pf-primary" onClick={() => openDialog('saveVersion', documentFile)}>保存文档版本</button></div></section>}

    {versionFile && <section className="pf-versions-card" aria-label={`${versionFile.name}的版本记录`}><div className="pf-section-heading"><div><h2>{versionFile.name} · 版本记录</h2><p>每个版本保存独立快照；恢复只更新当前工作草稿，历史版本保持不变。</p></div><button type="button" className="pf-button" onClick={() => { setVersionFileId(null); setPreviewVersionId(null) }}>关闭记录</button></div>
      {versionFile.versions.length ? <ol className="pf-version-list">{[...versionFile.versions].reverse().map((version) => <li key={version.id}><div><b>{version.label}</b><span>{dateLabel(version.createdAt)}</span><p>{version.note || '未填写版本说明'}</p></div><div className="pf-file-actions"><button type="button" className="pf-button" onClick={() => setPreviewVersionId((current) => current === version.id ? null : version.id)}>查看快照</button><button type="button" className="pf-button" onClick={() => runAction(() => onDownloadFile?.({ ...versionFile, name: `${versionFile.name}-${version.label}`, snapshot: version.snapshot }))}>下载此版本</button><button type="button" className="pf-button" onClick={() => runAction(() => onRestoreVersion?.(versionFile.id, version.id, versionFile.projectId))}>恢复到工作草稿</button></div></li>)}</ol> : <div className="pf-empty"><p>尚未保存任何版本。当前草稿会继续独立保存。</p><button type="button" className="pf-button pf-primary" disabled={versionFile.contentUnavailable} onClick={() => openDialog('saveVersion', versionFile)}>保存第一个版本</button></div>}
      {previewVersion && <SnapshotDetails snapshot={previewVersion.snapshot} title={`${previewVersion.label} · 只读快照`} />}
    </section>}

    {dialog && <dialog ref={dialogRef} className="pf-dialog" onCancel={closeDialog} aria-labelledby="pf-dialog-title"><form onSubmit={submit}>
      <div className="pf-section-heading"><h2 id="pf-dialog-title">{dialogTitles[dialog.mode]}</h2><button type="button" className="pf-button" aria-label="关闭对话框" onClick={closeDialog}>×</button></div>
      {dialog.mode.startsWith('delete') ? <p>将“{dialog.file?.name || project?.name}”移入回收站{dialog.mode === 'deleteProject' ? `，包含 ${project?.files.length || 0} 个文件及其版本记录` : ''}。之后可从回收站恢复。</p> : dialog.mode === 'saveVersion' ? <><p>为“{dialog.file.name}”保存一份独立快照。</p><label>版本说明<textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={500} placeholder="例如：调整轴长，确认安装间隙" autoFocus /></label></> : <label>名称<input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} required autoFocus placeholder={dialog.mode.includes('Project') ? '输入项目名称' : '输入文件名称'} /></label>}
      {dialog.mode === 'duplicateFile' && <p className="pf-muted">复制当前工作草稿，两份文件可独立修改。新副本从自己的版本记录开始。</p>}
      {dialog.mode === 'createFile' && <><p className="pf-muted">所属项目：{dialog.file?.projectName || project?.name}</p><label>文件类型<select aria-label="文件类型" value={type} onChange={(event) => { setType(event.target.value); setSource(['工程图', '装配体'].includes(event.target.value) && activeFile?.snapshot?.model ? 'current' : 'blank') }}>{FILE_TYPES.map((item) => <option key={item}>{item}</option>)}</select></label>{type !== '文档' && <label>初始设计<select value={source} onChange={(event) => setSource(event.target.value)}><option value="blank">空白文件</option><option value="current" disabled={!activeFile?.snapshot?.model}>复制当前文件的设计</option></select></label>}<p className="pf-muted">新文件拥有独立内容和版本。复制设计后，两份文件可分别修改。</p></>}
      {error && <p className="pf-error" role="alert">{error}</p>}
      <div className="pf-dialog-actions"><button type="button" className="pf-button" disabled={submitting} onClick={closeDialog}>取消</button><button type="submit" className={`pf-button ${dialog.mode.startsWith('delete') ? 'pf-danger' : 'pf-primary'}`} disabled={submitting}>{submitting ? '处理中…' : dialog.mode.startsWith('delete') ? '移入回收站' : dialog.mode === 'saveVersion' ? '保存版本' : '确定'}</button></div>
    </form></dialog>}
  </div>
}
