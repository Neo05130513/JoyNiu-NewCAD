import { hasAdminPermission } from './adminPermissions.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import { createBillingActionGate, formatBillingDate } from './billingClient.js'
import { createSupportIntentKeys, emptySupportDraft, supportAssignmentPayload, supportCategories, supportClient, supportCreatePayload, supportMessagePayload, supportNotePayload, supportPageOffset, supportPriorities, supportQueryState, supportReplyDraft, supportReplyTemplates, supportSessionKey, supportStatuses, supportTransitions, supportUpdatePayload, supportViewFilters, supportViews } from './supportClient.js'
import './support.css'
import { DataReceiptForm, DataRequestFields, DataRequestSummary } from './SupportDataRequest.jsx'

const PAGE_SIZE = 20
const MESSAGE_SIZE = 50
const statusName = value => supportStatuses[value] || '状态待确认'

function SupportDialog({ title, busy, children, onClose }) {
  const ref = useRef(null)
  useEffect(() => { const focused = document.activeElement; ref.current?.showModal(); return () => focused?.focus?.() }, [])
  return <dialog className="support-dialog" ref={ref} aria-label={title} onCancel={event => { event.preventDefault(); if (!busy) onClose() }}><header><h2>{title}</h2><button type="button" disabled={busy} aria-label="关闭弹窗" onClick={onClose}>×</button></header>{children}</dialog>
}

function NewTicketDialog({ busy, error, onClose, onSubmit, initialCategory = 'other' }) {
  const [draft, setDraft] = useState(() => ({ ...emptySupportDraft(), category: initialCategory, subject: initialCategory === 'data_deletion' ? '数据删除申请' : '' }))
  const [validation, setValidation] = useState('')
  const set = key => event => setDraft(previous => ({ ...previous, [key]: event.target.value }))
  const submit = event => { event.preventDefault(); setValidation(''); try { onSubmit(supportCreatePayload(draft)) } catch (failure) { setValidation(failure.message) } }
  return <SupportDialog title="新建支持工单" busy={busy} onClose={onClose}><form onSubmit={submit}><fieldset disabled={busy}>
    <label>问题标题<input autoFocus required maxLength={120} value={draft.subject} onChange={set('subject')} /></label>
    <label>问题分类<select value={draft.category} onChange={set('category')}>{Object.entries(supportCategories).map(([key, name]) => <option value={key} key={key}>{name}</option>)}</select></label>
    {draft.category === 'data_deletion' && <DataRequestFields value={draft.dataRequest} onChange={dataRequest => setDraft(previous => ({ ...previous, dataRequest }))} />}
    <label>问题说明<textarea required maxLength={5000} rows={6} value={draft.body} onChange={set('body')} placeholder="说明发生了什么、预期结果和可重现的操作步骤。请勿填写登录密码或支付凭据。" /></label>
    <label>关联任务编号（可选）<input maxLength={128} value={draft.runId} onChange={event => setDraft(previous => ({ ...previous, runId: event.target.value, drawingConsent: event.target.value.trim() ? previous.drawingConsent : false }))} placeholder="从“我的任务”复制本次记录编号" /></label>
    <label className="support-check"><input type="checkbox" disabled={!draft.runId.trim()} checked={draft.drawingConsent} onChange={event => setDraft(previous => ({ ...previous, drawingConsent: event.target.checked }))} />我明确同意就关联任务的附图提供后续人工处理依据，可随时撤回。</label>
    <p className="support-note">此授权只记录你的意愿，当前工单不向管理员开放原图下载，也不改变任务或原图的访问权限。不授权也可以提交问题。</p>
  </fieldset>{(validation || error) && <p className="support-error" role="alert">{validation || error}</p>}<footer><button type="button" disabled={busy} onClick={onClose}>取消</button><button className="support-primary" disabled={busy} type="submit">{busy ? '正在创建…' : '提交工单'}</button></footer></form></SupportDialog>
}

function ChangeDialog({ action, busy, error, onClose, onSubmit }) {
  const [reason, setReason] = useState('')
  const [targetStatus, setTargetStatus] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [validation, setValidation] = useState('')
  const title = action.kind === 'status' ? '修改工单处理状态' : action.kind === 'close' ? '关闭工单' : action.consent ? '确认附图授权' : '撤回附图授权'
  const submit = event => {
    event.preventDefault(); setValidation('')
    try {
      if (action.kind === 'consent' && action.consent && !confirmed) throw new Error('请明确勾选授权确认。')
      onSubmit(supportUpdatePayload(action.ticket, action.kind === 'status' ? { status: targetStatus, reason } : action.kind === 'close' ? { status: 'closed' } : { drawingConsent: action.consent }, action.kind === 'status'))
    } catch (failure) { setValidation(failure.message) }
  }
  return <SupportDialog title={title} busy={busy} onClose={onClose}><form onSubmit={submit}><p><b>{action.ticket.number}</b><br />{action.ticket.subject}</p>
    {action.kind === 'status' ? <><label>新的处理状态<select required value={targetStatus} disabled={busy} onChange={event => setTargetStatus(event.target.value)}><option value="">请选择状态</option>{supportTransitions(action.ticket.status).map(value => <option value={value} key={value}>{statusName(value)}</option>)}</select></label><label>处理说明<textarea autoFocus required rows={4} maxLength={500} value={reason} disabled={busy} onChange={event => setReason(event.target.value)} /></label></> : action.kind === 'close' ? <p>关闭后保留历史说明与处理记录。关闭的工单不能追加说明；如需再次处理，可新建工单并引用此编号。</p> : <><p>{action.consent ? '该授权仅用于记录你同意后续人工处理关联附图的意愿，不开放当前原图下载权限。' : '撤回后工单会立即记录为未授权。已有授权及撤回记录作为历史保留，不再代表当前授权。'}</p>{action.consent && <label className="support-check"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />我明确同意上述授权，并知悉可随时撤回。</label>}</>}
    {(validation || error) && <p className="support-error" role="alert">{validation || error}</p>}<footer><button type="button" disabled={busy} onClick={onClose}>返回</button><button type="submit" className="support-primary" disabled={busy || (action.kind === 'consent' && action.consent && !confirmed)}>{busy ? '正在保存…' : '确认保存'}</button></footer></form></SupportDialog>
}

export function SupportAssignmentForm({ ticket, assignees, busy, onSubmit }) {
  const [assignedTo, setAssignedTo] = useState(ticket.assignedTo || '')
  const [priority, setPriority] = useState(ticket.priority || 'normal')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  useEffect(() => { if (!reason) { setAssignedTo(ticket.assignedTo || ''); setPriority(ticket.priority || 'normal') } }, [ticket.assignedTo, ticket.priority])
  const staleAssignee = assignedTo && !assignees?.some(item => item.id === assignedTo)
  return <form className="support-assignment" onSubmit={event => { event.preventDefault(); setError(''); try { onSubmit(supportAssignmentPayload(ticket, { assignedTo, priority, reason })) } catch (failure) { setError(failure.message) } }}>
    <div><label>工单负责人<select value={assignedTo} disabled={busy || !assignees} onChange={event => setAssignedTo(event.target.value)}><option value="">未分配</option>{staleAssignee && <option value={assignedTo} disabled>{ticket.assignee?.displayName || assignedTo}（当前不可分派）</option>}{assignees?.map(item => <option key={item.id} value={item.id}>{item.displayName} · {item.email}</option>)}</select></label><label>优先级<select value={priority} disabled={busy} onChange={event => setPriority(event.target.value)}>{Object.entries(supportPriorities).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label></div>
    <label>分派说明<input required maxLength={500} disabled={busy} value={reason} onChange={event => setReason(event.target.value)} placeholder="记录调整负责人或优先级的原因，仅后台可见" /></label>
    {error && <p className="support-error" role="alert">{error}</p>}<button type="submit" disabled={busy || !assignees || staleAssignee || !reason.trim()}>{busy ? '正在保存…' : '保存分派'}</button>
  </form>
}

export function SupportInternalNotes({ notes, draft, busy, loading, onDraft, onSubmit, onPage }) {
  return <div className="support-internal-panel"><p className="support-internal-label">内部协作 · 仅工单管理员可见，不会发送给客户</p>
    {!notes ? <p className="support-empty">{loading ? '正在读取内部备注…' : '备注尚未加载，请刷新工单。'}</p> : <><div className="support-thread">{notes.items.length ? notes.items.map(item => <article key={item.id} className="support-internal-note"><header><b>{item.authorName}</b><time>{formatBillingDate(item.createdAt)}</time></header><p>{item.body}</p></article>) : <p className="support-empty">暂无内部备注，可记录排查依据、交接事项与处理结论。</p>}</div>
      {notes.total > PAGE_SIZE && <nav className="support-pagination" aria-label="内部备注分页"><button disabled={busy || loading || notes.offset === 0} onClick={() => onPage(Math.max(0, notes.offset - PAGE_SIZE))}>较新备注</button><span>第 {Math.floor(notes.offset / PAGE_SIZE) + 1} / {Math.ceil(notes.total / PAGE_SIZE)} 页</span><button disabled={busy || loading || notes.offset + PAGE_SIZE >= notes.total} onClick={() => onPage(notes.offset + PAGE_SIZE)}>更早备注</button></nav>}</>}
    <form className="support-reply" onSubmit={onSubmit}><label>新增内部备注<textarea required maxLength={5000} rows={5} disabled={busy} value={draft} onChange={event => onDraft(event.target.value)} placeholder="内部备注与客户沟通分开保存。请勿填写账号密码或服务密钥。" /></label><button type="submit" disabled={busy || !draft.trim()}>{busy ? '正在保存…' : '保存内部备注'}</button></form>
  </div>
}

function SupportSession({ session, admin, initialTicketId, initialFilters }) {
  const token = session.access_token
  const currentToken = useRef(token); currentToken.current = token
  const lifetime = useRef(null)
  const listSequence = useRef(0), detailSequence = useRef(0), assigneeSequence = useRef(0)
  const gate = useRef(createBillingActionGate()), intentKeys = useRef(createSupportIntentKeys())
  const [records, setRecords] = useState(null)
  const [listRevision, setListRevision] = useState(0)
  const [offset, setOffset] = useState(initialFilters.offset), [filter, setFilter] = useState(initialFilters.status)
  const [query, setQuery] = useState(initialFilters.q), [queryDraft, setQueryDraft] = useState(initialFilters.q), [assignedFilter, setAssignedFilter] = useState(initialFilters.assignedTo), [priorityFilter, setPriorityFilter] = useState(initialFilters.priority)
  const [assignees, setAssignees] = useState(null), [assigneeError, setAssigneeError] = useState('')
  const [assignmentRevision, setAssignmentRevision] = useState(0)
  const [notes, setNotes] = useState(null), [noteDraft, setNoteDraft] = useState(''), [detailTab, setDetailTab] = useState('conversation')
  const [selectedId, setSelectedId] = useState(() => typeof initialTicketId === 'string' ? initialTicketId : '')
  const currentSelected = useRef(selectedId); currentSelected.current = selectedId
  const [ticket, setTicket] = useState(null), [messages, setMessages] = useState(null), [audit, setAudit] = useState(null)
  const [loading, setLoading] = useState(true), [detailLoading, setDetailLoading] = useState(false), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [detailError, setDetailError] = useState(''), [actionError, setActionError] = useState(''), [notice, setNotice] = useState('')
  const [reply, setReply] = useState(''), [dialog, setDialog] = useState(null)
  const current = (expected, signal) => currentToken.current === expected && lifetime.current?.signal === signal && !signal?.aborted
  const validPage = page => Array.isArray(page?.items) && Number.isSafeInteger(page.total) && page.total >= 0

  const loadList = useCallback(async () => {
    const signal = lifetime.current.signal, sequence = ++listSequence.current
    setLoading(true); setError('')
    try {
      const page = await supportClient.list(token, { admin, status: filter, q: query, assignedTo: assignedFilter, priority: priorityFilter, offset, signal })
      if (!current(token, signal) || sequence !== listSequence.current) return
      if (!validPage(page)) throw new Error('工单列表响应无效，请刷新重试。')
      const bounded = supportPageOffset(page.total, offset)
      if (bounded !== offset) { setOffset(bounded); return }
      setRecords(page)
    } catch (failure) { if (current(token, signal) && sequence === listSequence.current && failure.name !== 'AbortError') setError(failure.message) }
    finally { if (current(token, signal) && sequence === listSequence.current) setLoading(false) }
  }, [token, admin, filter, query, assignedFilter, priorityFilter, offset, listRevision])

  const loadAssignees = useCallback(async () => {
    if (!admin) return
    const signal = lifetime.current.signal, sequence = ++assigneeSequence.current
    setAssigneeError('')
    try { const result = await supportClient.assignees(token, signal); if (current(token, signal) && sequence === assigneeSequence.current) { if (!Array.isArray(result?.items)) throw new Error('负责人列表响应无效。'); setAssignees(result.items) } }
    catch (failure) { if (current(token, signal) && sequence === assigneeSequence.current && failure.name !== 'AbortError') { setAssignees(null); setAssigneeError('负责人列表暂未加载，请刷新重试。') } }
  }, [token, admin])

  const loadDetail = useCallback(async (id, messageOffset = null, auditOffset = 0, noteOffset = 0) => {
    if (!id) return
    const signal = lifetime.current.signal, sequence = ++detailSequence.current
    const fresh = () => current(token, signal) && sequence === detailSequence.current && currentSelected.current === id
    setDetailLoading(true); setDetailError('')
    try {
      const value = await supportClient.ticket(token, id, { admin, signal })
      if (!fresh()) return
      if (value?.id !== id || !Number.isSafeInteger(value.revision) || !Number.isSafeInteger(value.messageCount)) throw new Error('工单详情响应无效，请刷新重试。')
      const targetOffset = messageOffset === null ? supportPageOffset(value.messageCount, Number.MAX_SAFE_INTEGER, MESSAGE_SIZE) : supportPageOffset(value.messageCount, messageOffset, MESSAGE_SIZE)
      const responses = await Promise.allSettled([supportClient.messages(token, id, { admin, offset: targetOffset, limit: MESSAGE_SIZE, signal }), ...(admin ? [supportClient.audit(token, id, { offset: auditOffset, signal }), supportClient.notes(token, id, { offset: noteOffset, signal })] : [])])
      if (!fresh()) return
      setTicket(value)
      if (responses[0].status === 'rejected') throw responses[0].reason
      if (!validPage(responses[0].value)) throw new Error('工单说明响应无效。')
      setMessages(responses[0].value)
      if (admin) {
        const errors = []
        if (responses[1].status === 'fulfilled' && validPage(responses[1].value)) setAudit(responses[1].value)
        else { setAudit(null); errors.push('审计记录暂未加载。') }
        if (responses[2].status === 'fulfilled' && validPage(responses[2].value)) setNotes(responses[2].value)
        else { setNotes(null); errors.push('内部备注暂未加载。') }
        if (errors.length) setDetailError(`${errors.join(' ')}请刷新重试。`)
      }
    } catch (failure) { if (fresh() && failure.name !== 'AbortError') setDetailError(failure.message) }
    finally { if (fresh()) setDetailLoading(false) }
  }, [token, admin])
  const latest = useRef({ loadList, loadDetail }); latest.current = { loadList, loadDetail }
  useEffect(() => { const controller = new AbortController(); lifetime.current = controller; return () => { controller.abort(); listSequence.current++; detailSequence.current++ } }, [])
  useEffect(() => { loadList() }, [loadList])
  useEffect(() => { loadAssignees() }, [loadAssignees])
  useEffect(() => {
    detailSequence.current++
    setSelectedId(typeof initialTicketId === 'string' ? initialTicketId : '')
    setReply(''); setNoteDraft(''); setDetailTab('conversation'); setActionError(''); setNotice('')
  }, [initialTicketId])
  useEffect(() => { setTicket(null); setMessages(null); setAudit(null); setNotes(null); if (selectedId) loadDetail(selectedId) }, [selectedId, loadDetail])

  function mutate(action, payload, operation, commit) {
    if (gate.current.has('support-write')) return
    const signal = lifetime.current.signal
    const selectedAtStart = selectedId
    const key = intentKeys.current.get(action, payload)
    setBusy(true); setActionError(''); setNotice('')
    return gate.current.run('support-write', async () => {
      try {
        const result = await operation(key, signal)
        if (!current(token, signal)) return
        if (action !== 'create' && currentSelected.current !== selectedAtStart) { intentKeys.current.complete(action, payload); await latest.current.loadList(); return }
        commit(result); intentKeys.current.complete(action, payload)
        await latest.current.loadList()
      } catch (failure) {
        if (current(token, signal) && (action === 'create' || currentSelected.current === selectedAtStart) && failure.name !== 'AbortError') setActionError(failure.status === 409 ? `${failure.message}，输入内容已保留；请刷新工单后重新确认。` : failure.message)
      } finally { if (lifetime.current?.signal === signal && !signal.aborted) setBusy(false) }
    })
  }
  const create = payload => mutate('create', payload, (key, signal) => supportClient.create(token, payload, key, signal), result => {
    if (!result?.id) throw new Error('服务端未返回工单编号，请刷新确认提交结果。')
    setDialog(null); setSelectedId(result.id); setReply(''); setNotice(`工单 ${result.number} 已创建。请在此查看后续回复。`)
  })
  const sendReply = event => {
    event.preventDefault()
    let payload
    try { payload = supportMessagePayload(reply) } catch (failure) { setActionError(failure.message); return }
    const id = selectedId
    mutate(`reply:${id}`, payload, (key, signal) => supportClient.append(token, id, payload, key, { admin, signal }), result => {
      if (result?.ticket?.id !== id || result?.message?.ticketId !== id) throw new Error('服务端未返回匹配的说明记录，请刷新确认。')
      setReply(''); setTicket(result.ticket); setNotice(admin ? '回复已记录，客户可在工单中查看。' : '补充说明已保存。')
      void latest.current.loadDetail(id)
    })
  }
  const applyChange = payload => {
    const id = dialog.ticket.id
    mutate(`update:${id}`, payload, (key, signal) => supportClient.update(token, id, payload, key, { admin, signal }), result => {
      if (result?.id !== id) throw new Error('服务端未返回匹配的工单，请刷新核对。')
      setTicket(result); setDialog(null); setNotice('工单变更已记录。'); void latest.current.loadDetail(id)
    })
  }
  const saveAssignment = payload => {
    const id = selectedId
    mutate(`assignment:${id}`, payload, (key, signal) => supportClient.update(token, id, payload, key, { admin: true, signal }), result => {
      if (result?.id !== id) throw new Error('返回的工单与当前分派不一致，请刷新核对。')
      setTicket(result); setAssignmentRevision(value => value + 1); setNotice('负责人和优先级已保存，分派说明仅在后台审计中记录。'); void latest.current.loadDetail(id)
    })
  }
  const saveNote = event => {
    event.preventDefault()
    let payload
    try { payload = supportNotePayload(ticket, noteDraft) } catch (failure) { setActionError(failure.message); return }
    const id = selectedId
    mutate(`note:${id}`, payload, (key, signal) => supportClient.appendNote(token, id, payload, key, signal), result => {
      if (result?.ticket?.id !== id || !result.noteId) throw new Error('返回的内部备注记录无效，请刷新核对。')
      setTicket(result.ticket); setNoteDraft(''); setNotice('内部备注已保存，客户无法查看。'); void latest.current.loadDetail(id)
    })
  }
  const saveDataReceipt = payload => {
    const id = selectedId
    mutate(`data-receipt:${id}`, payload, (key, signal) => supportClient.recordDataReceipt(token, id, payload, key, signal), result => {
      if (result?.id !== id || !result.dataReceipts?.length) throw new Error('未取得匹配的数据处理回执，请刷新确认。')
      setTicket(result); setNotice('人工处理回执已保存，客户可在本工单查看；本次操作没有执行数据删除。'); void latest.current.loadDetail(id)
    })
  }
  const view = Object.keys(supportViews).find(key => { const values = supportViewFilters(key); return filter === values.status && assignedFilter === values.assignedTo })
  const applyView = key => { const values = supportViewFilters(key); setFilter(values.status); setAssignedFilter(values.assignedTo); setOffset(0); setRecords(null); setListRevision(value => value + 1) }
  const openChange = action => { setActionError(''); setDialog(action) }
  const select = id => { if (busy) return; detailSequence.current++; setSelectedId(id); setReply(''); setNoteDraft(''); setDetailTab('conversation'); setActionError(''); setNotice('') }
  const refresh = () => { loadList(); loadAssignees(); if (selectedId) loadDetail(selectedId, messages?.offset, audit?.offset, notes?.offset) }
  const title = admin ? '客服工作台' : '支持与工单'
  return <section className={`support-workspace ${admin ? 'support-admin' : ''}`} aria-label={title}><header><div><span className="support-eyebrow">JOYNIU CAD · SUPPORT</span><h1>{title}</h1><p>{admin ? '分配待办、排查客户问题，让处理记录与客户沟通衔接起来。' : '提交问题，凭工单编号查看说明与后续回复。'}</p></div><div><button disabled={busy || loading || detailLoading} onClick={refresh}>刷新记录</button>{!admin && <button className="support-primary" disabled={busy} onClick={() => openChange({ kind: 'new' })}>＋ 新建工单</button>}</div></header>
    <p className="support-notice">工单保存在本服务中，请在此查看回复。当前不发送邮件或短信通知，也不承诺回复时效。</p>
    {!admin && <div className="support-context"><b>原图、模型或账号数据需要删除？</b><p>提交数据范围后由运营核对处理。工单会保留实际处理回执、仍保留的数据和备份安排，申请不会立即删除文件。</p><button disabled={busy} onClick={() => openChange({ kind: 'new', category: 'data_deletion' })}>申请删除数据</button></div>}
    {error && <p className="support-error" role="alert">{error}</p>}{notice && <p className="support-success" role="status">{notice}</p>}{actionError && !dialog && <p className="support-error" role="alert">{actionError}</p>}
    {admin && <><nav className="support-views" aria-label="工单快捷视图">{Object.entries(supportViews).map(([id, label]) => <button key={id} aria-pressed={view === id} disabled={busy} onClick={() => applyView(id)}><span>{label}</span><b>{records?.views?.[id] ?? '—'}</b><small>待处理与处理中</small></button>)}</nav><form className="support-filters" onSubmit={event => { event.preventDefault(); setQuery(queryDraft.trim()); setOffset(0); setRecords(null); setListRevision(value => value + 1) }}><label className="support-filter-search">搜索工单<input type="search" maxLength={128} value={queryDraft} disabled={busy} onChange={event => setQueryDraft(event.target.value)} placeholder="标题、工单编号、任务编号或客户 ID" /></label><label>负责人<select value={assignedFilter} disabled={busy} onChange={event => { setAssignedFilter(event.target.value); setOffset(0); setRecords(null); setListRevision(value => value + 1) }}><option value="">全部负责人</option><option value="me">分配给我</option><option value="unassigned">未分配</option>{assignees?.map(item => <option key={item.id} value={item.id}>{item.displayName}</option>)}</select></label><label>优先级<select value={priorityFilter} disabled={busy} onChange={event => { setPriorityFilter(event.target.value); setOffset(0); setRecords(null); setListRevision(value => value + 1) }}><option value="">全部优先级</option>{Object.entries(supportPriorities).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><button type="submit" disabled={busy}>搜索</button><button type="button" disabled={busy} onClick={() => { setQuery(''); setQueryDraft(''); setAssignedFilter(''); setPriorityFilter(''); setFilter(''); setOffset(0); setRecords(null); setListRevision(value => value + 1) }}>重置</button></form>{assigneeError && <p role="alert" className="support-error">{assigneeError}</p>}</>}
    <div className="support-layout"><section className="support-card support-list"><div className="support-list-heading"><label>工单状态<select value={filter} disabled={busy} onChange={event => { setFilter(event.target.value); setOffset(0); setRecords(null); setListRevision(value => value + 1) }}><option value="">全部状态</option>{admin && <option value="pending">待处理与处理中</option>}{Object.entries(supportStatuses).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label><span>{records ? `${records.total} 条工单` : '尚未加载'}</span></div>
      {!records ? <p className="support-empty">{loading ? '正在加载工单…' : '工单尚未加载，请刷新重试。'}</p> : !records.items.length ? <p className="support-empty">暂无符合条件的工单。</p> : <div className="support-ticket-list">{records.items.map(item => <button key={item.id} className={item.id === selectedId ? 'selected' : ''} disabled={busy} onClick={() => select(item.id)} aria-pressed={item.id === selectedId}><span className="support-ticket-top"><b>{item.subject}</b><small className={`support-status ${item.status}`}>{statusName(item.status)}</small></span><code>{item.number}</code><span>{supportCategories[item.category] || item.category} · {item.messageCount} 条说明</span>{admin && <div className="support-ticket-assignment"><span className={`support-priority ${item.priority}`}>{supportPriorities[item.priority] || '普通'}优先级</span><span>{item.assignee?.displayName || '未分配'}{item.assignee?.available === false ? ' · 需重新分派' : ''}</span></div>}<small>更新于 {formatBillingDate(item.updatedAt)}</small>{admin && <small>账号 {item.ownerId}</small>}</button>)}</div>}
      {records && <nav className="support-pagination" aria-label="工单列表分页"><button disabled={busy || loading || offset === 0} onClick={() => { setOffset(value => Math.max(0, value - PAGE_SIZE)); setRecords(null); setListRevision(value => value + 1) }}>上一页</button><span>{Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(records.total / PAGE_SIZE))}</span><button disabled={busy || loading || offset + PAGE_SIZE >= records.total} onClick={() => { setOffset(value => value + PAGE_SIZE); setRecords(null); setListRevision(value => value + 1) }}>下一页</button></nav>}
    </section><section className="support-card support-detail" aria-label="工单详情">
      {detailError && <p className="support-error" role="alert">{detailError}</p>}
      {!selectedId ? <div className="support-empty"><h2>选择一个工单</h2><p>查看已提交的问题、补充说明和实际处理进展。</p></div> : !ticket ? <p className="support-empty">{detailLoading ? '正在读取工单…' : '工单详情暂未加载，请刷新重试。'}</p> : <>
        <header><div><code>{ticket.number}</code><h2>{ticket.subject}</h2><p>{supportCategories[ticket.category]} · 创建于 {formatBillingDate(ticket.createdAt)}</p></div><span className={`support-status ${ticket.status}`}>{statusName(ticket.status)}</span></header>
        {ticket.statusNote && <p className="support-status-note">处理记录：{ticket.statusNote}</p>}
        <DataRequestSummary ticket={ticket} />
        {admin && ticket.category === 'data_deletion' && <DataReceiptForm key={`${ticket.id}:${ticket.dataReceipts?.length || 0}`} ticket={ticket} busy={busy} onSubmit={saveDataReceipt} />}
        {admin && <SupportAssignmentForm key={`${ticket.id}:${assignmentRevision}`} ticket={ticket} assignees={assignees} busy={busy} onSubmit={saveAssignment} />}
        <div className="support-context"><span>关联任务：<code>{ticket.runId || '未关联'}</code></span><span>当前附图授权：<b>{ticket.drawingConsent ? '已明确授权' : '未授权 / 已撤回'}</b></span><p>此处只记录授权状态，不提供原图下载，也不改变 CAD 数据访问权限。</p><div className="support-actions">{admin ? <button disabled={busy} onClick={() => openChange({ kind: 'status', ticket })}>修改处理状态</button> : <>{ticket.runId && <button disabled={busy} onClick={() => openChange({ kind: 'consent', ticket, consent: !ticket.drawingConsent })}>{ticket.drawingConsent ? '撤回附图授权' : '设置附图授权'}</button>}{ticket.status !== 'closed' && <button disabled={busy} onClick={() => openChange({ kind: 'close', ticket })}>关闭工单</button>}</>}</div></div>
        {admin && <nav className="support-detail-tabs" aria-label="工单内容"><button aria-pressed={detailTab === 'conversation'} onClick={() => setDetailTab('conversation')}>客户沟通 · {ticket.messageCount}</button><button aria-pressed={detailTab === 'internal'} onClick={() => setDetailTab('internal')}>内部备注 · {ticket.internalNoteCount ?? '—'}</button></nav>}
        {(!admin || detailTab === 'conversation') && <><div className="support-thread">{messages?.items.map(message => <article key={message.id} className={message.authorRole === 'admin' ? 'from-admin' : ''}><header><b>{message.authorRole === 'admin' ? '管理员回复' : '客户说明'} · {message.authorName}</b><time>{formatBillingDate(message.createdAt)}</time></header><p>{message.body}</p></article>)}</div>
        {messages && messages.total > MESSAGE_SIZE && <nav className="support-pagination" aria-label="工单说明分页"><button disabled={busy || detailLoading || messages.offset === 0} onClick={() => loadDetail(selectedId, messages.offset - MESSAGE_SIZE, audit?.offset)}>更早说明</button><span>第 {Math.floor(messages.offset / MESSAGE_SIZE) + 1} / {Math.ceil(messages.total / MESSAGE_SIZE)} 页</span><button disabled={busy || detailLoading || messages.offset + MESSAGE_SIZE >= messages.total} onClick={() => loadDetail(selectedId, messages.offset + MESSAGE_SIZE, audit?.offset)}>较新说明</button></nav>}
        {ticket.status === 'closed' ? <p className="support-status-note">工单已关闭，历史记录已保留。{admin ? '如需继续回复，请先重新开启处理。' : '如需再次处理，可新建工单并引用此编号；附图授权仍可撤回。'}</p> : <form className="support-reply" onSubmit={sendReply}>{admin && <label>常用回复<select value="" disabled={busy} onChange={event => { try { setReply(supportReplyDraft(reply, event.target.value)); setActionError('') } catch (failure) { setActionError(failure.message) } }}><option value="">选择后填入草稿，不会直接发送</option>{supportReplyTemplates.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}<label>{admin ? '回复客户（客户可见）' : '追加说明'}<textarea required maxLength={5000} rows={5} disabled={busy} value={reply} onChange={event => setReply(event.target.value)} placeholder={admin ? '填写实际处理结论或需要补充的信息。提交后客户可以查看。' : '补充发生情况、操作步骤或需要核对的内容。'} /></label><button type="submit" className="support-primary" disabled={busy || !reply.trim()}>{busy ? '正在提交…' : admin ? '保存并回复客户' : '保存补充说明'}</button></form>}</>}
        {admin && detailTab === 'internal' && <SupportInternalNotes notes={notes} draft={noteDraft} busy={busy} loading={detailLoading} onDraft={setNoteDraft} onSubmit={saveNote} onPage={value => loadDetail(selectedId, messages?.offset, audit?.offset, value)} />}
        {admin && audit && <details className="support-audit"><summary>操作审计 · {audit.total} 条</summary>{audit.items.map(item => <article key={item.id}><b>{({ 'ticket.created': '创建工单', 'message.created': '新增说明', 'status.changed': '修改状态', 'drawing_consent.changed': '修改附图授权', 'assignment.changed': '分派工单', 'note.created': '新增内部备注' })[item.action] || item.action}</b><small>{formatBillingDate(item.createdAt)} · {item.actorId}</small><pre>{JSON.stringify(item.details, null, 2)}</pre></article>)}<nav className="support-pagination" aria-label="工单审计分页"><button disabled={busy || detailLoading || audit.offset === 0} onClick={() => loadDetail(selectedId, messages?.offset, Math.max(0, audit.offset - PAGE_SIZE))}>上一页</button><span>{Math.floor(audit.offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(audit.total / PAGE_SIZE))}</span><button disabled={busy || detailLoading || audit.offset + PAGE_SIZE >= audit.total} onClick={() => loadDetail(selectedId, messages?.offset, audit.offset + PAGE_SIZE)}>下一页</button></nav></details>}
      </>}
    </section></div>
    {dialog?.kind === 'new' ? <NewTicketDialog initialCategory={dialog.category} busy={busy} error={actionError} onClose={() => setDialog(null)} onSubmit={create} /> : dialog && <ChangeDialog action={dialog} busy={busy} error={actionError} onClose={() => setDialog(null)} onSubmit={applyChange} />}
  </section>
}

export default function SupportWorkspace({ account, onLogin, admin = false, initialTicketId = '', params = {} }) {
  const session = account?.session
  if (!session?.access_token) return <section className="support-workspace"><h1>支持与工单</h1><p>登录后提交问题，并查看属于自己的处理记录。</p><button className="support-primary" onClick={onLogin}>登录账号</button></section>
  if (admin && !hasAdminPermission(session.user, 'support:manage')) return <section className="support-workspace"><h1>支持工单管理</h1><p role="alert">当前账号没有工单管理权限。</p></section>
  const initialFilters = supportQueryState(admin ? params : {})
  return <SupportSession key={`${supportSessionKey(session, admin)}:${JSON.stringify(initialFilters)}`} session={session} admin={admin} initialTicketId={initialTicketId} initialFilters={initialFilters} />
}
