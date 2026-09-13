import { useEffect, useRef, useState } from 'react'
import { emptyCadDraft } from './cadEditorTransactions.js'
import CadEditorSurface from './CadEditorSurface.jsx'
import { directFeatureClient } from './directFeatureClient.js'
import { createClientId } from './clientId.js'
import { matchesCadDocumentSource } from './cadDocumentSession.js'
import { cacheFeatureWorkspace, cachedFeatureWorkspace, draftFromFeatureRecord, featureWorkspaceKey, cloneFeatureValue, featureErrorFeedback, initialFeatureDraft, workspacePayload } from './directFeatureModel.js'
import { downloadBlob, readableError } from './workspaceFeedback.js'

export default function DirectFeatureWorkspace({ token, accountKey, model, generation, showToast, initialPlan, initialPlanKey, initialDraft, initialReference, sourceFileId, sourceProjectId, documents, onOpenDocument, sourceLabel, onSaved, onBack, onCreate, onNavigate, onNewDocument, onOpenPdm, onOpenCommunity, accountName, creditBalance, active = true }) {
  const [draft, setDraft] = useState(() => cloneFeatureValue(initialDraft || (initialPlan ? initialFeatureDraft(initialPlan) : emptyCadDraft()))), [record, setRecord] = useState(null)
  const [selected, setSelected] = useState(draft.plan.features[0]?.id)
  const [busy, setBusy] = useState(''), [error, setErrorState] = useState(''), [saved, setSaved] = useState([]), [versions, setVersions] = useState([])
  const [errorDetails, setErrorDetails] = useState('')
  const setError = message => { setErrorState(message); setErrorDetails('') }
  const [dirty, setDirty] = useState(true), [history, setHistory] = useState([]), [future, setFuture] = useState([])
  const [pendingSwitch, setPendingSwitch] = useState(null), [lastBuilt, setLastBuilt] = useState(null)
  const pendingCommit = useRef(null)
  const scope = accountKey || token || null
  const cacheIdentity = sourceFileId ? JSON.stringify([sourceProjectId || '', sourceFileId, initialPlanKey || 'manual']) : initialPlanKey
  const controller = useRef(null), epoch = useRef(0), snapshot = useRef(null), priorScope = useRef(scope), credential = useRef(token)
  credential.current = token
  const currentToken = () => credential.current
  snapshot.current = {draft, record, selected, dirty, history, future, versions, lastBuilt}
  useEffect(() => {
    const key = featureWorkspaceKey(token, cacheIdentity, accountKey)
    const carried = scope && !priorScope.current ? cachedFeatureWorkspace(featureWorkspaceKey(null, cacheIdentity)) : null
    const cached = cachedFeatureWorkspace(key) || carried
    priorScope.current = scope
    epoch.current += 1; pendingCommit.current = null; controller.current?.abort(); controller.current = null; setBusy(''); setSaved([]); setError(''); setPendingSwitch(null)
    const next = cached?.draft || cloneFeatureValue(initialDraft || (initialPlan ? initialFeatureDraft(initialPlan) : emptyCadDraft()))
    setDraft(next); setRecord(cached?.record || null); setSelected(cached?.selected || next.plan.features[0]?.id); setDirty(cached?.dirty ?? true)
    setHistory(cached?.history || []); setFuture(cached?.future || []); setVersions(cached?.versions || []); setLastBuilt(cached?.lastBuilt || null)
    // Clean caches are only a display hint: another tab can publish a newer
    // revision. Keep dirty work local until the user explicitly reloads it.
    const reopenId = cached?.dirty === false ? cached.record?.id || initialReference?.featureId : !cached ? initialReference?.featureId : null
    if (reopenId && token) {
      const abort = new AbortController(), current = epoch.current
      controller.current = abort; setBusy('正在打开模型特征…')
      directFeatureClient.get(currentToken, reopenId, undefined, abort.signal).then(value => {
        if (abort.signal.aborted || epoch.current !== current) return
        if (value.fileId !== sourceFileId) throw new Error('特征设计与来源文件不一致。')
        apply(value)
        if (initialReference?.revision && value.revision !== initialReference.revision && !cached) setError(`已打开最新保存版本 r${value.revision}，项目预览引用的是 r${initialReference.revision}。`)
        return directFeatureClient.versions(currentToken, value.id, abort.signal).then(async list => {
          if (abort.signal.aborted || epoch.current !== current) return
          setVersions(list.items)
          const previous = value.status !== 'built' && list.items.find(item => item.status === 'built')
          if (previous) {
            const built = await directFeatureClient.get(currentToken, value.id, previous.revision, abort.signal)
            if (!abort.signal.aborted && epoch.current === current) setLastBuilt(built)
          }
        })
      }).catch(reason => { if (!abort.signal.aborted && epoch.current === current) setError(readableError(reason)) })
        .finally(() => { if (epoch.current === current) { controller.current = null; setBusy('') } })
    }
    return () => { cacheFeatureWorkspace(key, snapshot.current); controller.current?.abort() }
  }, [scope, cacheIdentity])
  useEffect(() => {
    const current = epoch.current, abort = new AbortController()
    if (token) directFeatureClient.list(currentToken, abort.signal).then(result => { if (epoch.current === current && !abort.signal.aborted) setSaved(result.items) }).catch(reason => { if (!abort.signal.aborted && epoch.current === current) setError(readableError(reason)) })
    return () => abort.abort()
  }, [token, scope, initialPlanKey])
  useEffect(() => {
    const warn = event => { if (snapshot.current?.dirty && (snapshot.current.history.length || snapshot.current.future.length)) {event.preventDefault(); event.returnValue = ''} }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [])
  const replaceDraft = next => { setDraft(next); setSelected(current => next.plan.features.some(item => item.id === current) ? current : next.plan.features[0]?.id) }
  const edit = next => { setHistory(items => [...items, cloneFeatureValue(draft)].slice(-50)); setFuture([]); replaceDraft(next); setDirty(true); setError('') }
  const safely = action => { try { action() } catch (reason) { setError(reason.message) } }
  const undo = () => { if (!history.length) return; setFuture(items => [cloneFeatureValue(draft), ...items]); replaceDraft(history.at(-1)); setHistory(items => items.slice(0, -1)); setDirty(true) }
  const redo = () => { if (!future.length) return; setHistory(items => [...items, cloneFeatureValue(draft)]); replaceDraft(future[0]); setFuture(items => items.slice(1)); setDirty(true) }
  const apply = result => { setRecord(result); setDraft(draftFromFeatureRecord(result)); setSaved(items => [result, ...items.filter(item => item.id !== result.id)]); setVersions(items => [{revision: result.revision, status: result.status, changeNote: result.changeNote}, ...items.filter(item => item.revision !== result.revision)]); setLastBuilt(previous => result.status === 'built' ? result : previous?.id === result.id ? previous : null); setDirty(false); setSelected(current => result.plan.features.some(feature => feature.id === current) ? current : result.plan.features[0]?.id) }
  const operation = async (label, callback) => {
    if (controller.current) return
    const abort = new AbortController(), current = epoch.current
    controller.current = abort; setBusy(label); setError('')
    try { return await callback(abort.signal, () => !abort.signal.aborted && epoch.current === current) }
    catch (reason) { if (!abort.signal.aborted && epoch.current === current) { const feedback = featureErrorFeedback(reason); if (reason.featureId) setSelected(reason.featureId); const message = readableError({message: feedback.message, status: reason.status}); setError(message); setErrorDetails(feedback.detail); showToast?.(message, 'error') } }
    finally { if (epoch.current === current) { setBusy(''); controller.current = null } }
  }
  const commit = next => operation('正在完成并保存…', async (signal, current) => {
    if (initialReference?.featureId && !record) throw new Error('请先成功打开已保存的模型特征。')
    const payload = workspacePayload(next, record, sourceFileId || model?.fileId || model?.id || initialPlanKey || 'manual-design')
    const key = JSON.stringify([record?.id, payload])
    if (pendingCommit.current?.key !== key) pendingCommit.current = { key, requestId: createClientId() }
    const value = await directFeatureClient.commit(currentToken, record, {...payload, requestId:pendingCommit.current.requestId}, signal)
    if (!current()) return false
    setHistory(items => [...items, cloneFeatureValue(draft)].slice(-50)); setFuture([])
    apply(value); onSaved?.(value); onCreate?.({...value,designId:value.id}); pendingCommit.current = null
    showToast?.('模型已保存')
    return value
  })
  const saveDraft = next => operation('正在保存草图与参考几何…', async (signal, current) => {
    if(initialReference?.featureId&&!record)throw new Error('请先成功打开已保存的模型特征。')
    const value = await directFeatureClient.save(currentToken, record, workspacePayload(next, record, sourceFileId || initialPlanKey || 'manual-design'), signal)
    if (!current()) return false
    setHistory(items=>[...items,cloneFeatureValue(draft)].slice(-50));setFuture([]);apply(value); onSaved?.(value); return value
  })
  const saveOrBuild = build => build && draft.plan.features.length ? commit(draft) : saveDraft(draft)
  const requestSwitch = (label, action) => {
    if (typeof action !== 'function') return false
    if (controller.current) { setError('当前操作仍在处理，请完成后再切换图档。'); return false }
    const current = snapshot.current, stamp = epoch.current
    if (current.dirty && (current.history.length || current.future.length)) {
      setPendingSwitch({ label, action: () => {
        if (epoch.current !== stamp) return false
        const clean = current.record ? draftFromFeatureRecord(current.record) : cloneFeatureValue(initialDraft || (initialPlan ? initialFeatureDraft(initialPlan) : emptyCadDraft()))
        // The effect cleanup caches this snapshot when the source changes.
        // Discard must not resurrect abandoned edits on the next tab visit.
        snapshot.current = { ...current, draft: clean, dirty: !current.record, history: [], future: [] }
        setDraft(clean); setDirty(!current.record); setHistory([]); setFuture([]); pendingCommit.current = null
        return action()
      } })
      return false
    }
    return action()
  }
  const scopedDocuments = Array.isArray(documents) ? documents.filter(document => matchesCadDocumentSource(document, { accountKey: scope, projectId: sourceProjectId })) : undefined
  const openDocument = document => {
    const actual = scopedDocuments?.find(item => item.fileId === document?.fileId && item.projectId === document?.projectId && item.accountKey === document?.accountKey)
    if (!actual) { setError('图档不属于当前账号和项目。'); return false }
    if (actual.fileId === sourceFileId) return true
    return requestSwitch('切换图档', () => onOpenDocument?.(actual))
  }
  const startDraft = next => { setRecord(null); setVersions([]); setLastBuilt(null); setHistory([]); setFuture([]); setDraft(next); setSelected(next.plan.features[0]?.id); setDirty(true); setError('') }
  const load = id => operation('正在加载设计…', async (signal, current) => {
    const value = await directFeatureClient.get(currentToken, id, null, signal)
    if (!current()) return
    if (sourceFileId && value.fileId !== sourceFileId) throw new Error('特征设计与来源文件不一致。')
    apply(value); setVersions([]); setHistory([]); setFuture([])
    try {
      const list = await directFeatureClient.versions(currentToken, id, signal)
      if (!current()) return
      setVersions(list.items)
      const previous = value.status !== 'built' && list.items.find(item => item.status === 'built')
      if (previous) { const built = await directFeatureClient.get(currentToken, id, previous.revision, signal); if (current()) setLastBuilt(built) }
    } catch (reason) { if (current()) setError(`设计已打开，历史版本暂时无法完整读取：${readableError(reason)}`) }
  })
  const download = (format, source = record) => operation('正在准备下载…', async (signal, current) => { const blob = await directFeatureClient.download(currentToken, source, format, signal); if (current()) downloadBlob(blob, `${source.name || draft.name}-r${source.revision}.${format === 'plan' ? 'json' : format}`, blob.type) })
  const unavailable = Boolean(initialReference?.featureId && !record)
  const previewRecord = record?.status === 'built' ? record : lastBuilt?.id === record?.id ? lastBuilt : null
  const fallback = previewRecord?.artifacts?.glb ? {glbUrl:previewRecord.artifacts.glb.url,token} : null
  const restore = revision => requestSwitch('恢复历史版本', () => operation('正在读取版本…', async (signal, current) => {
    const value = await directFeatureClient.get(currentToken, record.id, revision, signal)
    if(current()) edit({...draftFromFeatureRecord(value),changeNote:`从 r${revision} 恢复并继续修改`})
  }))
  return <>
    <CadEditorSurface key={`${scope}:${cacheIdentity || 'manual'}`} scope={`${scope}:${cacheIdentity || 'manual'}`} draft={draft} record={record} token={token} active={active}
      busy={busy} error={error} errorDetails={errorDetails} unavailable={unavailable} dirty={dirty} selected={selected} onSelected={setSelected}
      onEdit={edit} onComplete={commit} onSaveDraft={saveDraft} onOpenPdm={onOpenPdm ? entry=>requestSwitch('打开 PDM 版本',()=>operation('正在打开 PDM 版本…',(signal,current)=>onOpenPdm(entry,{signal,current}))) : undefined} onOpenCommunity={onOpenCommunity ? entry=>requestSwitch('打开社区资源',()=>operation('正在打开资源副本…',(signal,current)=>onOpenCommunity(entry,{signal,current}))) : undefined} onSave={saveOrBuild} onUndo={undo} onRedo={redo} canUndo={history.length>0} canRedo={future.length>0}
      onBack={onBack ? ()=>requestSwitch('离开此图档',onBack) : undefined} onNavigate={onNavigate ? mode=>requestSwitch('离开此图档',()=>onNavigate(mode)) : undefined} onNewDocument={onNewDocument ? ()=>requestSwitch('新建图档',onNewDocument) : undefined} onOpenDocument={openDocument} documents={scopedDocuments} onLoad={id=>requestSwitch('打开其他设计',()=>load(id))} onRetry={()=>load(initialReference.featureId)}
      saved={saved} versions={versions} onRestore={restore} onReload={()=>requestSwitch('重新读取已保存版本',()=>load(record?.id || initialReference?.featureId))} onDownload={download} sourceLabel={sourceLabel} sourceFileId={sourceFileId} sourceProjectId={sourceProjectId} accountName={accountName} creditBalance={creditBalance} fallback={fallback}/>
    {pendingSwitch && <div className="ce-dialog-backdrop"><section role="alertdialog" aria-label="未保存修改"><b>当前设计有未保存修改</b><p>继续{pendingSwitch.label}会替换当前草稿。</p><button onClick={()=>setPendingSwitch(null)}>继续编辑</button><button onClick={()=>{const action=pendingSwitch.action;setPendingSwitch(null);action()}}>放弃修改并继续</button></section></div>}
  </>
}
