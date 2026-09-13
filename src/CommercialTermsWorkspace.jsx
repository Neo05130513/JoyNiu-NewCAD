import { hasAdminPermission } from './adminPermissions.js'
import { useEffect, useRef, useState } from 'react'
import { commercialTerms } from './commercialTermsClient.js'
import { createClientId } from './clientId.js'
import './commercial-terms.css'

const kinds = { service: '服务条款', privacy: '隐私与图纸数据说明', credits: '积分与退款规则' }
const blank = () => ({ kind: 'service', title: '', version: '', body: '' })
function TermsSession({ account, onLogin, admin, compact }) {
  const token = account?.session?.access_token
  const [data, setData] = useState(null)
  const [accepted, setAccepted] = useState(false)
  const [checked, setChecked] = useState(false)
  const [reviewed, setReviewed] = useState(false)
  const [draft, setDraft] = useState(blank)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)
  const inFlight = useRef(false)
  const key = useRef({ signature: '', value: '' })
  const current = useRef(token)
  current.current = token
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    setError(''); setData(null); setAccepted(false); setChecked(false)
    Promise.all([commercialTerms.read(controller.signal), token ? commercialTerms.acceptance(token, controller.signal) : null]).then(([terms, acceptance]) => {
      if (controller.signal.aborted) return
      setData(terms); setAccepted(acceptance?.accepted === true)
    }).catch(cause => { if (!controller.signal.aborted) setError(cause.message) })
    return () => controller.abort()
  }, [token, reload])
  async function accept() {
    if (!checked || !data?.ready || !token || inFlight.current) return
    inFlight.current = true; setBusy(true); setError('')
    const documentIds = data.documents.map(document => document.id).sort()
    const signature = JSON.stringify(documentIds)
    if (key.current.signature !== signature) key.current = { signature, value: createClientId() }
    try {
      const response = await commercialTerms.accept(token, documentIds, key.current.value)
      if (response?.accepted !== true || !response.receipt?.acceptanceId) throw new Error('尚未取得条款接受记录，请刷新确认后重试。')
      if (mounted.current && current.current === token) { setAccepted(true); setNotice('已保存你接受的条款版本和时间。') }
    } catch (cause) { if (mounted.current && current.current === token) setError(cause.message) }
    finally { if (mounted.current) { setBusy(false); inFlight.current = false } }
  }
  async function publish(event) {
    event.preventDefault()
    if (!reviewed || inFlight.current || !data) return
    inFlight.current = true; setBusy(true); setError(''); setNotice('')
    try {
      await commercialTerms.publish(token, { ...draft, expectedRevision: data.revision })
      if (mounted.current && current.current === token) { setDraft(blank()); setReviewed(false); setNotice('新版本已发布，旧版本与接受记录仍保留。'); setReload(value => value + 1) }
    } catch (cause) { if (mounted.current && current.current === token) setError(cause.message) }
    finally { if (mounted.current) { setBusy(false); inFlight.current = false } }
  }
  return <section className={`commercial-terms ${compact ? 'compact' : ''}`} aria-label={admin ? '条款版本管理' : '服务与积分规则'}>
    <header><div><h1>{admin ? '条款版本管理' : '服务与积分规则'}</h1><p>{admin ? '发布经过审核的正式文本；经营主体、支持安排、数据保留期限和退款约定应填写完整。' : '线下充值和使用收费服务前，请阅读服务范围、图纸数据处理和积分规则。'}</p></div><button onClick={() => setReload(value => value + 1)} disabled={busy}>刷新条款</button></header>
    {error && <p className="commercial-terms-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!data && !error && <p>正在读取条款版本…</p>}
    {data && <>
      {!data.ready && <p className="commercial-terms-note">正式服务条款、隐私说明和积分规则尚未全部发布，请联系运营确认。收费任务须待正式文本齐全并完成接受后开放；当前线上付款关闭。</p>}
      <div className="commercial-terms-documents">{data.documents.map(document => <details key={document.id}><summary>{kinds[document.kind]} · {document.version}</summary><h2>{document.title}</h2><p className="commercial-terms-body">{document.body}</p><small>版本 {document.version} · 发布于 {new Date(document.publishedAt).toLocaleString('zh-CN')}</small></details>)}</div>
      {!admin && data.ready && (accepted ? <p className="commercial-terms-accepted">✓ 已接受当前版本，可在这里随时查阅。</p> : token ? <div className="commercial-terms-consent"><label><input type="checkbox" checked={checked} disabled={busy} onChange={event => setChecked(event.target.checked)} />我已阅读并同意以上服务条款、隐私说明及积分规则。</label><button className="primary-button" disabled={!checked || busy} onClick={accept}>{busy ? '正在保存…' : '同意并保存'}</button></div> : <button className="primary-button" onClick={onLogin}>登录后接受条款</button>)}
    </>}
    {admin && <form className="commercial-terms-form" onSubmit={publish}><h2>发布新版本</h2><label>文档类型<select value={draft.kind} onChange={event => setDraft(value => ({ ...value, kind: event.target.value }))}>{Object.entries(kinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>标题<input required maxLength={120} value={draft.title} onChange={event => setDraft(value => ({ ...value, title: event.target.value }))} /></label><label>版本号<input required maxLength={40} placeholder="由运营确定版本号" value={draft.version} onChange={event => setDraft(value => ({ ...value, version: event.target.value }))} /></label><label>正式正文<textarea required rows={12} maxLength={100000} value={draft.body} onChange={event => setDraft(value => ({ ...value, body: event.target.value }))} /></label><label className="commercial-terms-review"><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} />我已完成内容审核，确认将此版本提供给客户阅读。</label><button className="primary-button" disabled={busy || !reviewed || !data}>发布新版本</button></form>}
  </section>
}
export default function CommercialTermsWorkspace({ account, onLogin, admin = false, compact = false }) {
  if (admin && !hasAdminPermission(account.session?.user, 'terms:manage')) return <section className="commercial-terms"><h1>条款版本管理</h1><p>当前账号没有管理权限。</p></section>
  return <TermsSession key={`${account.session?.user?.id || 'guest'}:${admin}`} {...{ account, onLogin, admin, compact }} />
}
