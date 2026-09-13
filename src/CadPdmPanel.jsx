import { useEffect, useRef, useState } from 'react'
import { cadPdmClient } from './cadPdmClient.js'
import { createClientId } from './clientId.js'
import './cad-editor-panels.css'

export default function CadPdmPanel({ record, token, accountKey, active = true, onOpen, onSaved, onClose }) {
  const identity = accountKey || token || '', key = `${identity}:${record?.id || ''}:${record?.revision || ''}:${active}`
  const [projects, setProjects] = useState({ identity: '', items: [] }), [projectId, setProjectId] = useState(''), [documents, setDocuments] = useState({ key: '', items: [] })
  const [name, setName] = useState(record?.name || ''), [projectName, setProjectName] = useState(''), [error, setError] = useState(''), [status, setStatus] = useState(''), [busy, setBusy] = useState(''), [reload, setReload] = useState(0)
  const current = useRef(key), credentials = useRef(token), mutation = useRef(null), retry = useRef(null)
  current.current = key; credentials.current = token
  const visibleProjects = projects.identity === identity ? projects.items : []
  const documentKey = `${identity}:${projectId}`, entries = documents.key === documentKey ? documents.items : []
  const canSave = Boolean(token && active && record?.status === 'built' && visibleProjects.find(item => item.id === projectId)?.canWrite)
  useEffect(() => { setName(record?.name || ''); setError(''); setStatus('') }, [identity, record?.id])
  useEffect(() => {
    const controller = new AbortController(), stamp = identity
    if (!token || !active) return () => controller.abort()
    cadPdmClient.projects(() => credentials.current, controller.signal).then(value => {
      if (controller.signal.aborted) return
      setProjects({ identity: stamp, items: value.items || [] })
      setProjectId(previous => value.items?.some(item => item.id === previous) ? previous : value.items?.[0]?.id || '')
    }).catch(reason => { if (!controller.signal.aborted) setError(reason.message) })
    return () => controller.abort()
  }, [identity, active, reload, Boolean(token)])
  useEffect(() => {
    const controller = new AbortController(), stamp = documentKey
    setDocuments({ key: stamp, items: [] })
    if (token && active && projectId) cadPdmClient.documents(() => credentials.current, projectId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDocuments({ key: stamp, items: value.items || [] }) })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message) })
    return () => controller.abort()
  }, [documentKey, active, reload, Boolean(token)])
  useEffect(() => {
    mutation.current?.abort(); mutation.current = null; setBusy('')
    return () => { mutation.current?.abort(); mutation.current = null }
  }, [key])
  const perform = async (label, fn) => {
    if (mutation.current || !active || !token) return
    const controller = new AbortController(), stamp = current.current
    mutation.current = controller; setBusy(label); setError(''); setStatus('')
    try { await fn(controller.signal, () => !controller.signal.aborted && current.current === stamp) }
    catch (reason) { if (!controller.signal.aborted && current.current === stamp) setError(reason.message || 'PDM 操作失败。') }
    finally { if (mutation.current === controller) { mutation.current = null; if (current.current === stamp) setBusy('') } }
  }
  const save = event => {
    event.preventDefault()
    if (!canSave) return
    const chosenName = name.trim()
    if (!chosenName) { setError('请填写保存名称。'); return }
    const body = { projectId, name: chosenName, featureId: record.id, revision: record.revision,
      expectedCurrentRevision: entries.find(entry => entry.document.name === chosenName)?.document.current_revision || 0 }
    const fingerprint = JSON.stringify(body)
    if (retry.current?.fingerprint !== fingerprint) retry.current = { fingerprint, requestId: createClientId() }
    perform('save', async (signal, valid) => {
      const result = await cadPdmClient.save(() => credentials.current, { ...body, requestId: retry.current.requestId }, signal)
      if (!valid()) return
      if (result.source?.featureId !== body.featureId || result.source?.revision !== body.revision || result.document?.project_id !== body.projectId) throw new Error('保存结果的项目或来源版本不一致。')
      setStatus(`已将 r${body.revision} 保存到 PDM v${result.version.revision} 和工程库。`)
      setReload(value => value + 1); onSaved?.(result)
    })
  }
  const createProject = () => perform('project', async (signal, valid) => {
    if (!projectName.trim()) throw new Error('请填写新项目名称。')
    const result = await cadPdmClient.createProject(() => credentials.current, projectName.trim(), signal)
    if (!valid()) return
    setProjects(previous => ({ identity, items: [...(previous.identity === identity ? previous.items : []), { id: result.id, name: result.name, canWrite: true }] }))
    setProjectId(result.id); setProjectName('')
  })
  return <section className="cad-editor-panel" aria-label="保存到 PDM"><header><h2>PDM 与工程库</h2>{onClose && <button onClick={onClose} disabled={Boolean(busy)}>关闭</button>}</header>
    {!token ? <p role="status">请登录后保存或打开工程文件。</p> : <>
      <form onSubmit={save}><label>项目<select aria-label="PDM 项目" value={visibleProjects.some(item => item.id === projectId) ? projectId : ''} onChange={event => setProjectId(event.target.value)} disabled={!active || Boolean(busy)}><option value="">选择项目</option>{visibleProjects.map(item => <option key={item.id} value={item.id}>{item.name}{item.canWrite ? '' : '（只读）'}</option>)}</select></label>
        <label>保存名称<input aria-label="PDM 保存名称" value={name} maxLength={180} onChange={event => setName(event.target.value)} disabled={!active || Boolean(busy)} /></label>
        <p className="cad-panel-help">{record?.status === 'built' ? `保存当前已提交的精确版本 r${record.revision}，包含 STEP、三维预览及可编辑特征。` : '请先完成实体重建；下方仍可打开已有 PDM 版本。'}</p>
        <button className="primary-button" type="submit" disabled={!canSave || Boolean(busy)}>{busy === 'save' ? '正在保存实体…' : '保存当前版本'}</button>
      </form>
      <details><summary>新建 PDM 项目</summary><div className="cad-panel-inline"><label>项目名称<input aria-label="新 PDM 项目名称" value={projectName} maxLength={180} onChange={event => setProjectName(event.target.value)} disabled={!active || Boolean(busy)} /></label><button disabled={!active || Boolean(busy)} onClick={createProject}>新建</button></div></details>
      <div className="cad-pdm-documents"><header><b>已保存版本</b><button disabled={!active || Boolean(busy)} onClick={() => setReload(value => value + 1)}>刷新</button></header>{entries.map(entry => <details key={entry.document.id}><summary>{entry.document.name} · {entry.versions.length} 个版本</summary>{entry.versions.map(version => <div className="cad-pdm-version" key={version.id}><div>v{version.revision}<small>来自模型 r{version.source.revision}</small></div><button disabled={!active || Boolean(busy) || !onOpen} onClick={() => perform(version.id, async (_signal, valid) => { if (valid()) await onOpen?.({ versionId: version.id, name: entry.document.name, sourceRevision: version.source.revision }) })}>打开此版本</button></div>)}</details>)}{projectId && !entries.length && <p className="cad-panel-help">此项目暂无编辑器 CAD 版本。</p>}</div>
    </>}{error && <p role="alert">{error}</p>}{status && <p role="status">{status}</p>}
  </section>
}
