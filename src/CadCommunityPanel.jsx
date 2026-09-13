import { useEffect, useRef, useState } from 'react'
import { cadCommunityClient } from './cadCommunityClient.js'
import { createClientId } from './clientId.js'
import { downloadBlob, readableError } from './workspaceFeedback.js'
import CadModelViewport from './CadModelViewport.jsx'
import './cad-community.css'

function CommunityPreview({ resource, token, active, accountKey }) {
  const [loaded, setLoaded] = useState(null), [error, setError] = useState('')
  const credentials = useRef(token); credentials.current = token
  const key = `${accountKey || ''}:${resource.id}`
  useEffect(() => {
    const controller = new AbortController(); setLoaded(null); setError('')
    if (active) cadCommunityClient.artifact(() => credentials.current, resource.id, 'glb', controller.signal)
      .then(blob => { if (!controller.signal.aborted) { if (!(blob instanceof Blob) || !blob.size) throw new Error('模型预览为空。'); setLoaded({ key, fallback: { blob } }) } })
      .catch(reason => { if (!controller.signal.aborted) setError(readableError(reason)) })
    return () => controller.abort()
  }, [key, active])
  return <div className="cad-community-preview">{error ? <p role="alert">{error}</p> : loaded?.key === key ? <CadModelViewport active={active} fallback={loaded.fallback} pickKind="none" documentScope={key} previewHint="旋转查看模型；打开独立副本后可继续编辑。" /> : <p role="status">读取社区模型…</p>}</div>
}

export default function CadCommunityPanel({ record, token, accountKey, active = true, initialMode = 'browse', onOpen, onClose }) {
  const identity = accountKey || token || '', key = `${identity}:${record?.id || ''}:${record?.revision || ''}:${active}`
  const [mode, setMode] = useState(initialMode), [name, setName] = useState(record?.name || ''), [description, setDescription] = useState(''), [consent, setConsent] = useState(false)
  const [query, setQuery] = useState(''), [search, setSearch] = useState(''), [mine, setMine] = useState(false), [loaded, setLoaded] = useState(null), [selected, setSelected] = useState(''), [withdrawId, setWithdrawId] = useState('')
  const [error, setError] = useState(''), [status, setStatus] = useState(''), [busy, setBusy] = useState(''), [reload, setReload] = useState(0)
  const stamp = useRef(key), credentials = useRef(token), operation = useRef(null), retry = useRef(null)
  stamp.current = key; credentials.current = token
  const listKey = `${identity}:${search}:${mine}`, entries = loaded?.key === listKey ? loaded.items : []
  const canPublish = Boolean(active && token && record?.status === 'built' && name.trim() && consent)
  useEffect(() => { setName(record?.name || ''); setDescription(''); setConsent(false); setError(''); setStatus(''); setSelected(''); setWithdrawId(''); retry.current = null }, [identity, record?.id])
  useEffect(() => { setMine(false) }, [identity])
  useEffect(() => { setConsent(false) }, [record?.revision])
  useEffect(() => {
    operation.current?.abort(); operation.current = null; setBusy('')
    return () => { operation.current?.abort(); operation.current = null }
  }, [key])
  useEffect(() => {
    const controller = new AbortController()
    if (!active || (mine && !token)) return () => controller.abort()
    cadCommunityClient.list(() => credentials.current, { query: search, mine }, controller.signal)
      .then(value => { if (!controller.signal.aborted) setLoaded({ key: listKey, items: value.items || [], hasMore: value.hasMore }) })
      .catch(reason => { if (!controller.signal.aborted) setError(readableError(reason)) })
    return () => controller.abort()
  }, [listKey, active, reload, Boolean(token)])
  const perform = async (label, fn) => {
    if (!active || operation.current) return
    const controller = new AbortController(), expected = stamp.current
    operation.current = controller; setBusy(label); setError(''); setStatus('')
    const valid = () => !controller.signal.aborted && stamp.current === expected
    try { await fn(controller.signal, valid) }
    catch (reason) { if (valid()) setError(readableError(reason)) }
    finally { if (operation.current === controller) { operation.current = null; if (valid()) setBusy('') } }
  }
  const publish = event => {
    event.preventDefault(); if (!canPublish) return
    const body = { featureId: record.id, revision: record.revision, name: name.trim(), description: description.trim(), consent: 'public-copy-download' }
    const fingerprint = JSON.stringify({ identity, ...body })
    if (retry.current?.fingerprint !== fingerprint) retry.current = { fingerprint, requestId: createClientId() }
    perform('publish', async (signal, valid) => {
      const value = await cadCommunityClient.publish(() => credentials.current, { ...body, requestId: retry.current.requestId }, signal)
      if (!valid()) return
      if (!/^resource_[a-f0-9]{32}$/.test(value.id || '') || value.status !== 'published' || value.name !== body.name || value.sharing !== body.consent) throw new Error('社区发布结果校验失败，请刷新我的发布。')
      setStatus(`已公开发布“${value.name}”。`); setConsent(false); setMode('browse'); setMine(true); setSearch(''); setQuery(''); setSelected(value.id); setReload(n => n + 1)
    })
  }
  const download = (resource, kind) => perform(`download:${kind}`, async (signal, valid) => {
    const blob = await cadCommunityClient.artifact(() => credentials.current, resource.id, kind, signal)
    if (!valid()) return
    if (!(blob instanceof Blob) || !blob.size) throw new Error('社区文件为空。')
    downloadBlob(blob, `${resource.name.replace(/[\\/\u0000-\u001f]/g, '_')}.${kind}`, blob.type)
    setStatus(`${kind.toUpperCase()} 文件已准备下载。`)
  })
  const withdraw = resource => perform('withdraw', async (signal, valid) => {
    const result = await cadCommunityClient.withdraw(() => credentials.current, resource.id, signal)
    if (!valid()) return
    if (result.id !== resource.id || result.status !== 'withdrawn') throw new Error('撤下结果校验失败，请刷新列表。')
    setStatus('资源已撤下，新的下载和副本创建已停止。'); setSelected(''); setWithdrawId(''); setReload(n => n + 1)
  })
  return <section className="cad-editor-panel cad-community-panel" aria-label="AI 资源社区"><header><h2>AI 资源社区</h2><button onClick={onClose}>关闭</button></header>
    <nav className="cad-community-tabs"><button aria-pressed={mode === 'browse'} onClick={() => setMode('browse')}>浏览资源</button><button aria-pressed={mode === 'publish'} onClick={() => setMode('publish')}>发布当前模型</button></nav>
    {mode === 'publish' ? <form onSubmit={publish}>
      <p className="cad-panel-help">{record?.status === 'built' ? `发布当前已保存版本 r${record.revision}。` : '请先完成并保存当前模型，再发布；仍可浏览社区资源。'}</p>
      <label>资源名称<input aria-label="社区资源名称" value={name} maxLength={180} disabled={Boolean(busy)} onChange={event => setName(event.target.value)} /></label>
      <label>资源说明<textarea aria-label="社区资源说明" value={description} maxLength={2000} rows={3} disabled={Boolean(busy)} onChange={event => setDescription(event.target.value)} /></label>
      <label className="cad-community-consent"><input type="checkbox" aria-label="确认公开共享模型" checked={consent} disabled={Boolean(busy)} onChange={event => setConsent(event.target.checked)} /><span>我确认公开名称、说明、几何、参数、特征名称、标注文字及必要导入实体，允许所有访问者浏览和下载，登录用户可创建可编辑副本。原项目、源图纸和账号记录不公开。</span></label>
      <p className="cad-panel-help">默认只保存在自己的图档中。发布后可撤下，已下载的文件和已创建的副本不会被收回。请仅发布有权共享的内容。</p>
      <button className="primary-button" type="submit" disabled={!canPublish || Boolean(busy)}>{busy === 'publish' ? '正在发布…' : '确认公开发布'}</button>{!token && <p role="status">请登录后发布。</p>}
    </form> : <>
      <form className="cad-community-search" onSubmit={event => { event.preventDefault(); setSearch(query.trim()); setReload(n => n + 1) }}><input aria-label="搜索社区资源" value={query} maxLength={180} onChange={event => setQuery(event.target.value)} /><button type="submit" disabled={!active || Boolean(busy)}>搜索</button></form>
      <label className="cad-community-consent"><input type="checkbox" checked={mine} aria-label="只看我的发布" disabled={!token || Boolean(busy)} onChange={event => { setMine(event.target.checked); setSelected(''); setWithdrawId('') }} />只看我的发布</label>
      <div className="cad-community-resources">{entries.map(resource => <article key={resource.id}><button className="cad-community-resource-title" aria-expanded={selected === resource.id} onClick={() => { setSelected(previous => previous === resource.id ? '' : resource.id); setWithdrawId('') }}><b>{resource.name}</b><small>{resource.status === 'withdrawn' ? '已撤下' : `${resource.inspection?.solidCount ?? '—'} 个实体`}</small></button>
        {selected === resource.id && <><p>{resource.description || '暂无说明'}</p>{resource.status === 'published' && <>
          <CommunityPreview resource={resource} token={token} active={active} accountKey={identity} />
          <div className="cad-community-actions"><button className="primary-button" disabled={!token || !active || Boolean(busy) || !onOpen} onClick={() => perform('open', async (_signal, valid) => { if (valid()) await onOpen?.({ resourceId: resource.id, name: resource.name }) })}>作为独立副本编辑</button>{['step', 'glb'].map(kind => <button key={kind} disabled={!active || Boolean(busy)} onClick={() => download(resource, kind)}>下载 {kind.toUpperCase()}</button>)}</div>
          <p className="cad-panel-help">公开模型未经原图一致性或生产适用性确认。</p>{resource.canWithdraw && (withdrawId === resource.id ? <div className="cad-community-actions"><button disabled={Boolean(busy)} onClick={() => withdraw(resource)}>确认撤下</button><button disabled={Boolean(busy)} onClick={() => setWithdrawId('')}>取消撤下</button></div> : <button disabled={Boolean(busy)} onClick={() => setWithdrawId(resource.id)}>撤下此资源</button>)}
        </>}</>}
      </article>)}{loaded?.key !== listKey ? <p role="status">正在读取资源…</p> : !entries.length ? <p className="cad-panel-help">暂无匹配的社区资源。</p> : loaded.hasMore && <p className="cad-panel-help">仅显示最近 200 项，请搜索缩小范围。</p>}</div>
    </>}{error && <p role="alert">{error}</p>}{status && <p role="status">{status}</p>}
  </section>
}
