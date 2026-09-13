import { useEffect, useId, useRef, useState } from 'react'
import { hasAdminPermission } from './adminPermissions.js'
import { createClientId } from './clientId.js'
import { adminOperations, adminDate, adminNumber, adminHandlingNames, adminPriorityNames, adminTrendDays, adminTrendPoints, adminTrendGeometry, adminCrmPayload, adminHandlingPayload, adminEntryPayload, createAdminMutationIdentity, createAdminRequestGate, createAdminActionGate, adminPageOffset, adminFailureAdvice } from './adminOperationsClient.js'

export function DetailTabs({ tabs, initial = tabs[0]?.id }) {
  const [selected, setSelected] = useState(initial), prefix = useId(), refs = useRef([])
  const active = tabs.some(tab => tab.id === selected) ? selected : tabs[0]?.id
  const move = (event, index) => {
    let next
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = tabs.length - 1
    if (next !== undefined) { event.preventDefault(); setSelected(tabs[next].id); refs.current[next]?.focus() }
  }
  return <div className="ao-detail-tabs"><div role="tablist" aria-label="详情分类" className="ao-tabs">{tabs.map((tab, i) => <button key={tab.id} ref={element => { refs.current[i] = element }} role="tab" id={`${prefix}-${tab.id}`} aria-controls={`${prefix}-${tab.id}-panel`} aria-selected={active === tab.id} tabIndex={active === tab.id ? 0 : -1} onKeyDown={event => move(event, i)} onClick={() => setSelected(tab.id)}>{tab.label}</button>)}</div>{tabs.map(tab => <div key={tab.id} role="tabpanel" id={`${prefix}-${tab.id}-panel`} aria-labelledby={`${prefix}-${tab.id}`} hidden={active !== tab.id} tabIndex={0}>{tab.content}</div>)}</div>
}

export function OperationsTrend({ trend, params = {}, onNavigate, loading }) {
  const [field, setField] = useState('tasks'), [showTable, setShowTable] = useState(false)
  const rows = adminTrendPoints(trend), days = adminTrendDays(params.days ?? trend?.days), titleId = useId()
  const choices = { tasks: '新建任务', success: '当前已确认完成', failed: '当前失败', calls: '已记录调用' }, chart = adminTrendGeometry(rows, field)
  const count = values => values.every(value => typeof value === 'number') ? values.reduce((sum, value) => sum + value, 0) : null
  return <section className="ao-panel ao-trend"><header><div><span className="ao-eyebrow">业务观察</span><h2>任务与调用趋势</h2><p>北京时间自然日 · 任务按创建日期分组，显示这些任务当前的状态；调用按实际记录日期分组。</p></div><div className="ao-segment" aria-label="趋势时间范围">{[7, 30, 90].map(day => <button key={day} aria-pressed={days === day} disabled={loading} onClick={() => onNavigate('overview', { days: day })}>近 {day} 日</button>)}</div></header>
    {!rows.length ? <p className="ao-empty">尚未取得该时段的真实趋势数据。请刷新后查看。</p> : <><div className="ao-trend-metrics">{Object.entries(choices).map(([key, label]) => <button key={key} aria-pressed={field === key} onClick={() => setField(key)}><span>{label}</span><strong>{adminNumber(count(rows.map(row => row[key])))}</strong><small>{key === 'calls' ? '不同于自动重试次数' : key === 'success' ? '仅 ready 状态' : '以当前服务数据为准'}</small></button>)}</div><div className="ao-chart"><svg viewBox="0 0 840 200" role="img" aria-labelledby={titleId}><title id={titleId}>{`${choices[field]}，${rows[0].date} 至 ${rows.at(-1).date}；完整数据可展开下方表格。`}</title>{[0, .5, 1].map(fraction => <g key={fraction}><line x1="32" x2="824" y1={172 - fraction * 152} y2={172 - fraction * 152} stroke="#e8eef5" /><text x="25" y={176 - fraction * 152} textAnchor="end" fontSize="10" fill="#7b8ca1">{Number((chart.max * fraction).toFixed(1))}</text></g>)}{chart.paths.map((path, i) => <path key={i} d={path} fill="none" stroke={field === 'failed' ? '#d66b54' : '#2776df'} strokeWidth="2.5" />)}{chart.coordinates.map((point, i) => point && <circle key={i} cx={point.x} cy={point.y} r={rows.length > 35 ? 2 : 3} fill={field === 'failed' ? '#d66b54' : '#2776df'}><title>{`${point.date}：${point.value}`}</title></circle>)}<text x="32" y="195" fontSize="11" fill="#71849d">{rows[0].date}</text><text x="824" y="195" textAnchor="end" fontSize="11" fill="#71849d">{rows.at(-1).date}</text></svg></div><div className="ao-chart-foot"><p>缺失值保持未知，不补零。待确认、待补充均不计为完成；完成不代表制造保证。{trend.undatedTasks > 0 && `另有 ${trend.undatedTasks} 条历史任务缺少日期，未进入曲线。`}</p><button onClick={() => setShowTable(value => !value)} aria-expanded={showTable}>{showTable ? '收起明细' : '查看每日数据'}</button></div>{showTable && <div className="ao-table-scroll" tabIndex={0}><table><caption className="ao-sr-only">真实每日趋势数据</caption><thead><tr><th>日期</th><th>新建任务</th><th>当前完成</th><th>当前待确认</th><th>当前待补充</th><th>当前失败</th><th>调用记录</th><th>未完整用量</th></tr></thead><tbody>{rows.map(row => <tr key={row.date}><td>{row.date}</td>{['tasks','success','reviewRequired','needsInput','failed','calls','unknownUsageCalls'].map(key => <td key={key}>{adminNumber(row[key])}</td>)}</tr>)}</tbody></table></div>}</>}
  </section>
}

function useWrite(context) {
  const [state, setState] = useState({ busy: false, error: '', notice: '' }), gate = useRef(createAdminActionGate()), currentScope = useRef(context?.scope)
  currentScope.current = context?.scope
  useEffect(() => () => gate.current.abort(), [context?.scope])
  const run = async callback => {
    if (!context) return false
    const request = gate.current.begin(context.scope)
    if (!request) return false
    setState({ busy: true, error: '', notice: '' })
    try {
      await callback(request.signal)
      if (!request.isCurrent(currentScope.current)) return false
      setState({ busy: false, error: '', notice: '已保存到服务端。' }); context.onSaved?.(); return true
    } catch (error) {
      if (request.isCurrent(currentScope.current)) {
        setState({ busy: false, error: error.message || '保存未完成，请核对状态后重试。', notice: '' })
        if (error.status === 409) context.onSaved?.() // Read the latest version while preserving the operator's draft.
      }
      return false
    } finally { request.finish() }
  }
  return [state, run, error => setState(value => ({ ...value, error, notice: '' }))]
}

function Feedback({ state }) { return <>{state.error && <p className="ao-error" role="alert">{state.error}</p>}{state.notice && <p className="ao-notice" role="status">{state.notice}</p>}</> }
const profileDraft = value => ({ company: value?.company || '', contactName: value?.contactName || '', phone: value?.phone || '', tags: (value?.tags || []).join('，'), internalNote: value?.internalNote || '', expectedVersion: value?.version, reason: '' })
export function CustomerCrm({ crm, customerId, user, context }) {
  const [draft, setDraft] = useState(() => profileDraft(crm)), [dirty, setDirty] = useState(false), [state, write, error] = useWrite(context), identity = useRef(createAdminMutationIdentity(createClientId))
  useEffect(() => { if (!dirty) setDraft(profileDraft(crm)) }, [crm, dirty])
  const available = Number.isSafeInteger(crm?.version), editable = hasAdminPermission(user, 'admin:customer-manage') && available && Boolean(context)
  const stale = available && draft.expectedVersion !== crm.version
  const change = key => event => { setDirty(true); setDraft(value => ({ ...value, [key]: event.target.value })) }
  const submit = async event => {
    event.preventDefault()
    try {
      const key = identity.current.forPayload(draft), body = adminCrmPayload({ ...draft, idempotencyKey: key })
      const saved = await write(async signal => { const result = await adminOperations.crm(context.token, customerId, body, signal); if (!Number.isSafeInteger(result.crm?.version)) throw new Error('尚未确认档案保存结果，请刷新核对；重试将复用操作编号。') })
      if (saved) { identity.current.reset(); setDirty(false) }
    } catch (failure) { error(failure.message) }
  }
  return <section className="ao-panel"><header><div><h2>客户档案</h2><p>运营内部使用的联系信息与备注，修改保留操作记录。</p></div><span className="ao-subtext">{available ? `版本 ${crm.version} · ${adminDate(crm.updatedAt)}` : '未取得档案数据'}</span></header>{!available ? <p className="ao-empty">后台尚未提供此客户的档案，请刷新后查看。</p> : !editable ? <dl className="ao-info">{[['公司',crm.company],['联系人',crm.contactName],['联系电话',crm.phone],['标签',(crm.tags || []).join('、')],['内部备注',crm.internalNote]].map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value || '未填写'}</dd></div>)}</dl> : <form className="ao-editor" onSubmit={submit}><fieldset disabled={state.busy}><div className="ao-form-grid">{[['company','公司名称',120],['contactName','联系人',80],['phone','联系电话',40],['tags','客户标签（逗号分隔，最多12项）',300]].map(([key,label,max]) => <label key={key}>{label}<input value={draft[key]} maxLength={max} onChange={change(key)} autoComplete="off" /></label>)}</div><label>内部备注<textarea maxLength={2000} rows={4} value={draft.internalNote} onChange={change('internalNote')} placeholder="填写服务背景或需持续关注的事项" /></label><label>本次修改原因<textarea required minLength={5} maxLength={500} rows={2} value={draft.reason} onChange={change('reason')} placeholder="至少5字，保存到操作审计" /></label>{stale && <div className="ao-warning">档案版本已经变化。当前输入仍保留，请核对最新记录后决定是否重新编辑。<button type="button" onClick={() => { setDraft(profileDraft(crm)); setDirty(false); identity.current.reset() }}>载入最新并放弃此表单改动</button></div>}<Feedback state={state} /><footer><span>档案不会自动发送给客户。</span><button className="ao-primary" disabled={!dirty || stale} type="submit">{state.busy ? '正在保存…' : '保存客户档案'}</button></footer></fieldset></form>}</section>
}

export function TaskHandling({ handling, runId, user, context }) {
  const [draft, setDraft] = useState(() => ({ status: handling?.status || '', priority: handling?.priority || '', expectedVersion: handling?.version, reason: '' })), [dirty, setDirty] = useState(false), [state, write, error] = useWrite(context), identity = useRef(createAdminMutationIdentity(createClientId))
  useEffect(() => { if (!dirty) setDraft({ status: handling?.status || '', priority: handling?.priority || '', expectedVersion: handling?.version, reason: '' }) }, [handling, dirty])
  const available = Number.isSafeInteger(handling?.version), stale = available && draft.expectedVersion !== handling.version, editable = available && Boolean(context) && hasAdminPermission(user, 'admin:task-followup')
  const submit = async event => { event.preventDefault(); try { const body = adminHandlingPayload({ ...draft, idempotencyKey: identity.current.forPayload(draft) }); if (await write(async signal => { const result = await adminOperations.handling(context.token, runId, body, signal); if (!Number.isSafeInteger(result.handling?.version)) throw new Error('尚未确认保存结果，请刷新后核对。') })) { identity.current.reset(); setDirty(false) } } catch (failure) { error(failure.message) } }
  return <section className="ao-panel"><header><div><h2>人工处理安排</h2><p>优先级用于运营跟进，不改变 AI 队列顺序；“已处理”也不会把失败模型改为完成。</p></div></header>{!available ? <p className="ao-empty">尚未取得人工处理记录。</p> : !editable ? <dl className="ao-info"><div><dt>人工状态</dt><dd>{adminHandlingNames[handling.status] || '未记录'}</dd></div><div><dt>优先级</dt><dd>{adminPriorityNames[handling.priority] || '未记录'}</dd></div><div><dt>最近更新</dt><dd>{adminDate(handling.updatedAt)}</dd></div></dl> : <form className="ao-editor" onSubmit={submit}><fieldset disabled={state.busy}><div className="ao-form-grid">{[['status','人工处理状态',adminHandlingNames],['priority','运营优先级',adminPriorityNames]].map(([key,label,choices]) => <label key={key}>{label}<select value={draft[key]} onChange={event => { setDirty(true); setDraft(value => ({ ...value, [key]: event.target.value })) }}>{Object.entries(choices).map(([id,name]) => <option key={id} value={id}>{name}</option>)}</select></label>)}</div><label>本次安排说明<textarea required minLength={5} maxLength={500} rows={3} value={draft.reason} onChange={event => { setDirty(true); setDraft(value => ({ ...value, reason: event.target.value })) }} placeholder="说明安排依据，至少5字" /></label>{stale && <p className="ao-warning">处理安排已被更新，当前输入保留。<button type="button" onClick={() => { setDraft({ status: handling.status, priority: handling.priority, expectedVersion: handling.version, reason: '' }); setDirty(false); identity.current.reset() }}>载入最新并放弃此表单改动</button></p>}<Feedback state={state} /><footer><span>不会触发建模、重试或通知。</span><button className="ao-primary" disabled={!dirty || stale}>{state.busy ? '正在保存…' : '保存处理安排'}</button></footer></fieldset></form>}</section>
}

export function OperationsNotes({ kind, recordId, initial, user, context }) {
  const [page, setPage] = useState(initial), [loading, setLoading] = useState(false), [readError, setReadError] = useState(''), [content, setContent] = useState(''), [state, write, error] = useWrite(context)
  const gate = useRef(createAdminRequestGate()), identity = useRef(createAdminMutationIdentity(createClientId)), scope = useRef(context?.scope); scope.current = context?.scope
  useEffect(() => { setPage(initial) }, [initial])
  useEffect(() => () => gate.current.abort(), [context?.scope])
  const customer = kind === 'customers', editable = context && hasAdminPermission(user, customer ? 'admin:customer-manage' : 'admin:task-followup')
  const load = async offset => {
    if (!context) return
    const request = gate.current.begin(`${context.scope}:${offset}`), started = context.scope
    setLoading(true); setReadError('')
    try { const result = await adminOperations.entries(context.token, kind, recordId, offset, request.signal); if (request.isCurrent() && scope.current === started) { const actual = adminPageOffset(result.total, result.offset, result.limit); if (actual !== result.offset) { load(actual); return } setPage(result) } }
    catch (failure) { if (request.isCurrent() && scope.current === started) setReadError(failure.message) }
    finally { if (request.isCurrent() && scope.current === started) setLoading(false) }
  }
  const submit = async event => { event.preventDefault(); try { const body = adminEntryPayload(content, identity.current.forPayload({ content: content.trim() })); if (await write(async signal => { const result = await adminOperations.append(context.token, kind, recordId, body, signal); if (!result.entry?.id) throw new Error('尚未确认记录是否保存，请刷新核对；重试会复用操作编号。') })) { setContent(''); identity.current.reset(); load(0) } } catch (failure) { error(failure.message) } }
  return <section className="ao-panel"><header><div><h2>{customer ? '客户跟进记录' : '任务处理备注'}</h2><p>保留实际记录人和时间；在此记录说明，不会发送邮件、短信或模拟客服回复。</p></div>{context && <button disabled={loading} onClick={() => load(page?.offset || 0)}>刷新历史</button>}</header>{editable && <form className="ao-editor ao-compose" onSubmit={submit}><fieldset disabled={state.busy}><label>{customer ? '新增跟进' : '追加处理说明'}<textarea maxLength={2000} required rows={3} value={content} onChange={event => setContent(event.target.value)} placeholder="填写已核对的信息、实际处理过程或下一步安排；不要粘贴凭据或原图链接" /></label><Feedback state={state} /><footer><span>{content.length} / 2000 字</span><button className="ao-primary" disabled={!content.trim()}>{state.busy ? '正在记录…' : '保存记录'}</button></footer></fieldset></form>}{readError && <p className="ao-error" role="alert">{readError}</p>}<ol className="ao-timeline">{(page?.items || []).map(entry => <li key={entry.id}><span className="ao-timeline-dot" /><div><header><strong>{entry.createdBy || '未记录操作人'}</strong><time>{adminDate(entry.createdAt)}</time></header><p>{entry.content}</p><small>记录编号 {entry.id}</small></div></li>)}</ol>{!page?.items?.length && <p className="ao-empty">{page ? '暂无跟进记录。' : '尚未取得记录。'}</p>}{page && <nav className="ao-pager" aria-label={customer ? '客户跟进分页' : '任务备注分页'}><span>共 {adminNumber(page.total)} 条 · 当前显示 {page.items.length} 条</span><div><button disabled={loading || !context || page.offset === 0} onClick={() => load(Math.max(0, page.offset - page.limit))}>较新记录</button><button disabled={loading || !context || page.offset + page.limit >= page.total} onClick={() => load(page.offset + page.limit)}>较早记录</button></div></nav>}</section>
}

export function FailureAdvice({ code, status }) { const advice = adminFailureAdvice(code, status); return <aside className="ao-advice"><span className="ao-eyebrow">人工排查建议</span><h3>{advice.title}</h3><ol>{advice.steps.map(step => <li key={step}>{step}</li>)}</ol></aside> }
