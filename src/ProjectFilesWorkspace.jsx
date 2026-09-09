import { useEffect, useMemo, useRef, useState } from 'react'
import { FILE_TYPES, getActiveFile, getActiveProject, projectStatistics } from './projectStore.js'
import './project-files.css'

const typeIcons = { 零件: '◉', 工程图: '▱', 装配体: '◈', 文档: '≡' }
const dateLabel = (value) => {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
const sizeLabel = (snapshot) => {
  const bytes = new TextEncoder().encode(JSON.stringify(snapshot || {})).length
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`
}

function SnapshotDetails({ snapshot, title }) {
  const model = snapshot?.model
  return <div className="pf-snapshot-details">
    <h4>{title}</h4>
    {typeof snapshot?.documentText === 'string' ? <pre className="pf-document-preview">{snapshot.documentText || '此版本尚无正文。'}</pre> : <>
      <p>{model?.name || '空白文件 · 尚无模型'}{model?.kind ? ` · ${model.kind}` : ''}</p>
      <dl>{Object.entries(model || {}).filter(([key, value]) => !['name', 'kind', 'updatedAt'].includes(key) && ['number', 'string', 'boolean'].includes(typeof value)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>
      <p className="pf-muted">图纸：{snapshot?.drawingJob?.fileMeta?.name || '未附加'} · 装配实例：{snapshot?.assemblyItems?.length || 0} · 对话：{snapshot?.messages?.length || 0} 条</p>
    </>}
  </div>
}

/** Every file action receives its actual target; no action assumes the first row is the current file. */
export default function ProjectFilesWorkspace({
  store, onSelectProject, onCreateProject, onRenameProject, onCreateFile, onRenameFile,
  onOpenFile, onDownloadFile, onSaveVersion, onRestoreVersion, onUpdateDocument,
  storageError = '',
}) {
  const project = getActiveProject(store)
  const activeFile = getActiveFile(store)
  const [query, setQuery] = useState('')
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
  const dialogRef = useRef(null)
  const statistics = projectStatistics(project)
  const files = useMemo(() => (project?.files || []).filter((file) => `${file.name} ${file.type}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).slice().sort((a, b) => sort === 'name'
    ? a.name.localeCompare(b.name, 'zh-CN')
    : sort === 'type' ? a.type.localeCompare(b.type, 'zh-CN') || a.name.localeCompare(b.name, 'zh-CN')
      : String(b.updatedAt).localeCompare(String(a.updatedAt))), [project, query, sort])
  const versionFile = project?.files.find((file) => file.id === versionFileId)
  const previewVersion = versionFile?.versions.find((version) => version.id === previewVersionId)
  const documentFile = project?.files.find((file) => file.id === documentFileId && file.type === '文档')

  useEffect(() => {
    setVersionFileId(null); setPreviewVersionId(null); setDocumentFileId(null); setQuery(''); setError('')
  }, [project?.id])
  useEffect(() => {
    if (activeFile?.type === '文档') setDocumentFileId(activeFile.id)
  }, [activeFile?.id, activeFile?.type])
  useEffect(() => {
    if (dialog && dialogRef.current && !dialogRef.current.open) {
      dialogRef.current.showModal()
      dialogRef.current.querySelector('input:not([type="hidden"]), textarea, select')?.focus()
    }
  }, [dialog])

  const openDialog = (mode, file = null) => {
    setDialog({ mode, file }); setError(''); setNote('')
    setName(mode === 'renameProject' ? project?.name || '' : mode === 'renameFile' ? file?.name || '' : '')
    setType('零件'); setSource('blank')
  }
  const closeDialog = () => { dialogRef.current?.close(); setDialog(null); setError('') }
  const submit = async (event) => {
    event.preventDefault()
    try {
      if (dialog.mode === 'createProject') await onCreateProject?.(name.trim())
      if (dialog.mode === 'renameProject') await onRenameProject?.(project.id, name.trim())
      if (dialog.mode === 'createFile') await onCreateFile?.({ name: name.trim(), type, source })
      if (dialog.mode === 'renameFile') await onRenameFile?.(dialog.file.id, name.trim())
      if (dialog.mode === 'saveVersion') await onSaveVersion?.(dialog.file.id, note.trim())
      closeDialog()
    } catch (failure) { setError(failure?.message || '操作失败，请重试。') }
  }
  const openFile = (file) => {
    if (file.type === '文档') setDocumentFileId(file.id)
    onOpenFile?.(file)
  }
  const dialogTitles = { createProject: '新建项目', renameProject: '重命名项目', createFile: '新建文件', renameFile: '重命名文件', saveVersion: '保存设计版本' }

  return <div className="pf-workspace">
    <header className="pf-heading">
      <div><span className="pf-kicker">项目与文件</span><h1>{project?.name || '项目管理'}</h1><p>按项目保存设计、文档和版本快照</p></div>
      <div className="pf-heading-actions"><button type="button" className="pf-button" onClick={() => openDialog('createProject')}>＋ 新建项目</button><button type="button" className="pf-button pf-primary" disabled={!project} onClick={() => openDialog('createFile')}>＋ 新建文件</button></div>
    </header>
    <div className="pf-project-bar">
      <label>当前项目<select value={project?.id || ''} onChange={(event) => onSelectProject?.(event.target.value)}>{store.projects.map((item) => <option key={item.id} value={item.id}>{item.name}（{item.files.length} 个文件）</option>)}</select></label>
      <button type="button" className="pf-button" disabled={!project} onClick={() => openDialog('renameProject')}>重命名项目</button>
      <span className="pf-storage-status">{storageError ? '本机保存失败' : '存储位置：当前浏览器'}</span>
    </div>
    {storageError && <p className="pf-error" role="alert">{storageError}</p>}
    <div className="pf-statistics" aria-label="项目实际统计"><span><b>{statistics.files}</b> 文件</span>{FILE_TYPES.map((item) => <span key={item}>{item} <b>{statistics.types[item]}</b></span>)}<span>已存版本 <b>{statistics.versions}</b></span></div>

    <section className="pf-files-card" aria-label="项目文件列表">
      <div className="pf-toolbar"><label className="pf-search"><span>搜索文件</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名称或类型" /></label><label className="pf-sort">排序<select value={sort} onChange={(event) => setSort(event.target.value)}><option value="updated">最近修改</option><option value="name">文件名称</option><option value="type">文件类型</option></select></label></div>
      {files.length ? <ul className="pf-file-list">{files.map((file) => <li key={file.id} className={`pf-file-row ${file.id === activeFile?.id ? 'pf-active-file' : ''}`}>
        <div className="pf-file-identity"><span className="pf-file-icon" aria-hidden="true">{typeIcons[file.type]}</span><div><h2>{file.name}</h2><p>{file.type}{file.id === activeFile?.id ? ' · 当前打开' : ''} · {file.contentUnavailable ? '旧版仅记录文件名，内容不可用' : `${sizeLabel(file.snapshot)} 快照`}</p></div></div>
        <div className="pf-file-meta"><span>修改于 {dateLabel(file.updatedAt)}</span><span>{file.versions.length ? `${file.versions.length} 个已存版本 · 最新 ${file.versions.at(-1).label}` : '工作草稿 · 尚未保存版本'}</span></div>
        <div className="pf-file-actions" aria-label={`${file.name}的操作`}>
          <button type="button" className="pf-button pf-primary" disabled={file.contentUnavailable} onClick={() => openFile(file)}>{file.type === '文档' ? '编辑文档' : '打开'}</button>
          <button type="button" className="pf-button" disabled={file.contentUnavailable} onClick={() => onDownloadFile?.(file)}>下载</button>
          <button type="button" className="pf-button" onClick={() => openDialog('renameFile', file)}>重命名</button>
          <button type="button" className="pf-button" disabled={file.contentUnavailable} onClick={() => openDialog('saveVersion', file)}>保存版本</button>
          <button type="button" className="pf-button" aria-expanded={versionFileId === file.id} onClick={() => { setVersionFileId((current) => current === file.id ? null : file.id); setPreviewVersionId(null) }}>版本记录（{file.versions.length}）</button>
          {file.contentUnavailable && <button type="button" className="pf-button" onClick={() => { openDialog('createFile'); setName(`${file.name} · 新建`); setType(file.type) }}>新建此类内容</button>}
        </div>
      </li>)}</ul> : <div className="pf-empty"><b>{query ? '没有匹配的文件' : '这个项目还没有文件'}</b><p>{query ? '修改搜索条件，或清空搜索查看所有文件。' : '新建零件、工程图、装配或文档，开始独立保存设计。'}</p><button type="button" className="pf-button" onClick={() => query ? setQuery('') : openDialog('createFile')}>{query ? '清空搜索' : '新建文件'}</button></div>}
      <footer className="pf-list-footer">显示 {files.length} / {statistics.files} 个文件 · 浏览器存储不等于 PDM 同步</footer>
    </section>

    {documentFile && <section className="pf-document-card" aria-label="文档编辑器"><div className="pf-section-heading"><div><h2>{documentFile.name}</h2><p>正文随输入保存到此文件的工作草稿；保存版本后可回看。</p></div><button type="button" className="pf-button" onClick={() => setDocumentFileId(null)}>收起编辑器</button></div><label className="pf-document-label">文档正文<textarea value={documentFile.snapshot.documentText || ''} onChange={(event) => onUpdateDocument?.(documentFile.id, event.target.value)} placeholder="记录设计要求、尺寸说明或交付备注…" /></label><div className="pf-heading-actions"><button type="button" className="pf-button" onClick={() => onDownloadFile?.(documentFile)}>下载文档</button><button type="button" className="pf-button pf-primary" onClick={() => openDialog('saveVersion', documentFile)}>保存文档版本</button></div></section>}

    {versionFile && <section className="pf-versions-card" aria-label={`${versionFile.name}的版本记录`}><div className="pf-section-heading"><div><h2>{versionFile.name} · 版本记录</h2><p>每个版本保存独立快照；恢复只更新当前工作草稿，历史版本保持不变。</p></div><button type="button" className="pf-button" onClick={() => { setVersionFileId(null); setPreviewVersionId(null) }}>关闭记录</button></div>
      {versionFile.versions.length ? <ol className="pf-version-list">{[...versionFile.versions].reverse().map((version) => <li key={version.id}><div><b>{version.label}</b><span>{dateLabel(version.createdAt)}</span><p>{version.note || '未填写版本说明'}</p></div><div className="pf-file-actions"><button type="button" className="pf-button" onClick={() => setPreviewVersionId((current) => current === version.id ? null : version.id)}>查看快照</button><button type="button" className="pf-button" onClick={() => onDownloadFile?.({ ...versionFile, name: `${versionFile.name}-${version.label}`, snapshot: version.snapshot })}>下载此版本</button><button type="button" className="pf-button" onClick={() => onRestoreVersion?.(versionFile.id, version.id)}>恢复到工作草稿</button></div></li>)}</ol> : <div className="pf-empty"><p>尚未保存任何版本。当前草稿会继续独立保存。</p><button type="button" className="pf-button pf-primary" onClick={() => openDialog('saveVersion', versionFile)}>保存第一个版本</button></div>}
      {previewVersion && <SnapshotDetails snapshot={previewVersion.snapshot} title={`${previewVersion.label} · 只读快照`} />}
    </section>}

    {dialog && <dialog ref={dialogRef} className="pf-dialog" onCancel={closeDialog} aria-labelledby="pf-dialog-title"><form onSubmit={submit}>
      <div className="pf-section-heading"><h2 id="pf-dialog-title">{dialogTitles[dialog.mode]}</h2><button type="button" className="pf-button" aria-label="关闭对话框" onClick={closeDialog}>×</button></div>
      {dialog.mode === 'saveVersion' ? <><p>为“{dialog.file.name}”保存一份独立快照。</p><label>版本说明<textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={500} placeholder="例如：调整轴长，确认安装间隙" autoFocus /></label></> : <label>名称<input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} required autoFocus placeholder={dialog.mode.includes('Project') ? '输入项目名称' : '输入文件名称'} /></label>}
      {dialog.mode === 'createFile' && <><label>文件类型<select value={type} onChange={(event) => { setType(event.target.value); setSource(['工程图', '装配体'].includes(event.target.value) ? 'current' : 'blank') }}>{FILE_TYPES.map((item) => <option key={item}>{item}</option>)}</select></label>{type !== '文档' && <label>初始设计<select value={source} onChange={(event) => setSource(event.target.value)}><option value="blank">空白文件</option><option value="current" disabled={!activeFile?.snapshot?.model}>复制当前文件的设计</option></select></label>}<p className="pf-muted">新文件拥有独立内容和版本。复制设计后，两份文件可分别修改。</p></>}
      {error && <p className="pf-error" role="alert">{error}</p>}
      <div className="pf-dialog-actions"><button type="button" className="pf-button" onClick={closeDialog}>取消</button><button type="submit" className="pf-button pf-primary">{dialog.mode === 'saveVersion' ? '保存版本' : '确定'}</button></div>
    </form></dialog>}
  </div>
}
