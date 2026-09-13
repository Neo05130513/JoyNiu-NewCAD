import { hasAdminPermission } from './adminPermissions.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import { billingPolicyClient, billingPolicyDraftPayload, billingPolicyEnableBlock, billingPolicyStatePayload, billingPolicyStatuses, emptyBillingPolicyDraft, emptyBillingRate, insufficientBalancePolicies, policyRateFields, officialBillingPolicyDraft, formatPolicyRate, settlementReasonLabel, settlementStatusLabel } from './billingPolicyClient.js'
import { createBillingActionGate, formatBillingDate, formatCreditUnits } from './billingClient.js'
import './billing-policy.css'

const PAGE_SIZE = 20
const count = formatCreditUnits
const price = formatPolicyRate

function PolicyRates({ rates = [], apiPricing }) {
  const fields = apiPricing ? { ...policyRateFields, cacheWriteUnitsPerMillion: '缓存写入' } : policyRateFields
  return rates.length ? <div className="policy-table-wrap"><table><thead><tr><th>供应商 / 模型</th>{Object.entries(fields).map(([key, name]) => <th key={key}>{name}<small>积分 / 百万 token</small></th>)}</tr></thead><tbody>{rates.map(row => <tr key={`${row.provider}:${row.model}`}><td><b>{row.provider}</b><code>{row.model}</code></td>{Object.keys(fields).map(key => <td key={key}>{price(row[key])}</td>)}</tr>)}</tbody></table></div> : <p className="policy-empty">此版本尚未配置模型费率。</p>
}

function PricingSummary({ pricing }) {
  if (!pricing) return null
  return <div className="policy-conversion-summary"><strong>10 元 = 1000 积分 · {pricing.multiplier} 倍</strong><span>官方 API 标准价（美元） × {pricing.usdCnyRate} 结算汇率 × 100 积分/元 × {pricing.multiplier} 倍</span><small>汇率随规则版本固定，不是实时银行报价。普通输入、缓存读取、缓存写入和输出分别计算；推理已包含在输出中。</small><a href={pricing.sourceUrl || 'https://developers.openai.com/api/docs/pricing'} target="_blank" rel="noreferrer">官方价格依据 · {pricing.verifiedAt || pricing.catalogVersion} ↗</a></div>
}

function OfficialPriceEditor({ draft, catalog, busy, setDraft, updateRate }) {
  if (!catalog) return <p>正在读取官方价格目录…</p>
  const update = (key, value) => setDraft(previous => ({ ...previous, apiPricing: { ...previous.apiPricing, [key]: value } }))
  return <fieldset disabled={busy}><legend>官方 API 价格折算</legend>
    <div className="policy-pricing-inputs"><label>收费倍率<input aria-label="收费倍率" required type="number" min="0.000001" max="100" step="0.000001" value={draft.apiPricing.multiplier} onChange={event => update('multiplier', event.target.value)} /><small>1 倍按基价折算；2 倍为基价的两倍</small></label><label>美元兑人民币结算汇率<input aria-label="美元兑人民币结算汇率" required type="number" min="0.000001" max="100" step="0.000001" value={draft.apiPricing.usdCnyRate} onChange={event => update('usdCnyRate', event.target.value)} /><small>初始 7 为固定结算参数，可按经营需要调整</small></label></div>
    <PricingSummary pricing={draft.apiPricing} />
    {draft.rates.map((row, index) => <div className="policy-rate-editor" key={index}><div className="policy-rate-model"><label>调用来源<select value={row.provider} onChange={event => updateRate(index, 'provider', event.target.value)}><option value="codex">服务器 Codex</option><option value="remote">远程 API</option></select></label><label>官方模型<select value={row.model} onChange={event => updateRate(index, 'model', event.target.value)}>{catalog.models.map(model => <option key={model.model} value={model.model}>{model.model}</option>)}</select></label></div><button type="button" onClick={() => setDraft(previous => ({ ...previous, rates: previous.rates.filter((_, n) => n !== index) }))}>移除来源</button></div>)}
    <button type="button" onClick={() => setDraft(previous => ({ ...previous, rates: [...previous.rates, { provider: 'remote', model: catalog.models[0].model }] }))}>＋ 添加调用来源</button>
    <div className="policy-table-wrap"><table><thead><tr><th>官方基价 / 百万 Token</th><th>普通输入</th><th>缓存读取</th><th>缓存写入</th><th>输出</th></tr></thead><tbody>{catalog.models.map(row => <tr key={row.model}><td>{row.model}</td>{['inputUsdPerMillion', 'cachedInputUsdPerMillion', 'cacheWriteUsdPerMillion', 'outputUsdPerMillion'].map(key => <td key={key}>${row[key]}</td>)}</tr>)}</tbody></table></div>
    <p className="policy-hint">单次输入超过 272,000 Token 时，输入及缓存价格 ×2、输出 ×1.5。若上游仅提供汇总用量，无法确认的长上下文和缓存写入附加费不向客户加收，账单保留说明。</p>
  </fieldset>
}

function StateDialog({ action, busy, error, onClose, onSubmit }) {
  const dialog = useRef(null)
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [validation, setValidation] = useState('')
  useEffect(() => { const previous = document.activeElement; dialog.current?.showModal(); return () => previous?.focus?.() }, [])
  const title = action.enabled ? '启用所选计费规则' : '关闭积分计费'
  const submit = event => {
    event.preventDefault(); setValidation('')
    try { onSubmit(billingPolicyStatePayload({ ...action, reason, confirmed })) }
    catch (failure) { setValidation(failure.message) }
  }
  return <dialog className="policy-dialog" ref={dialog} aria-label={title} onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <header><h2>{title}</h2><button type="button" aria-label="关闭弹窗" disabled={busy} onClick={onClose}>×</button></header>
    <form onSubmit={submit}>
      <p>{action.enabled ? '启用后，新任务按此版本记录计费规则，并在本次任务实际终态按供应商确认的用量结算。' : '关闭后新任务不收取积分；已经登记的任务结算按服务端规则处理。此操作会保留历史规则与账单。'}</p>
      <div className="policy-version-summary"><code>{action.version?.id || '未选规则版本'}</code><span>余额不足：{insufficientBalancePolicies[action.version?.insufficientBalancePolicy] || '未选择'}</span><span>收费终态：{action.version?.billableStatuses?.map(value => billingPolicyStatuses[value] || value).join('、') || '未选择'}</span></div>
      {action.enabled && <><PricingSummary pricing={action.version?.apiPricing} /><PolicyRates rates={action.version?.rates} apiPricing={action.version?.apiPricing} /></>}
      <label>变更原因<textarea autoFocus required maxLength={500} rows={3} value={reason} onChange={event => setReason(event.target.value)} disabled={busy} /></label>
      {action.enabled && <label className="policy-check"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy} />我已核对费率、收费终态和余额不足策略，确认启用实际积分结算。</label>}
      {(validation || error) && <p className="policy-error" role="alert">{validation || error}</p>}
      <footer><button type="button" disabled={busy} onClick={onClose}>返回</button><button type="submit" className={action.enabled ? 'policy-primary' : 'policy-danger'} disabled={busy || (action.enabled && !confirmed)}>{busy ? '正在提交…' : action.enabled ? '确认启用计费' : '确认关闭计费'}</button></footer>
    </form>
  </dialog>
}

function PolicySession({ session }) {
  const token = session.access_token
  const currentToken = useRef(token); currentToken.current = token
  const lifetime = useRef(null)
  const sequence = useRef(0)
  const draftInitialized = useRef(false)
  const mutationGate = useRef(createBillingActionGate())
  const [overview, setOverview] = useState(null)
  const [selectedId, setSelectedId] = useState('')
  const [draft, setDraft] = useState(emptyBillingPolicyDraft)
  const [records, setRecords] = useState(null)
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [action, setAction] = useState(null)
  const [actionError, setActionError] = useState('')
  const isCurrent = (expectedToken, signal) => currentToken.current === expectedToken && lifetime.current?.signal === signal && !signal?.aborted

  const refresh = useCallback(async () => {
    const signal = lifetime.current.signal
    const request = ++sequence.current
    setLoading(true); setError('')
    try {
      const [status, settlements] = await Promise.all([billingPolicyClient.overview(token, signal), billingPolicyClient.settlements(token, { offset: page * PAGE_SIZE, signal })])
      if (!isCurrent(token, signal) || request !== sequence.current) return
      if (!Array.isArray(settlements?.items) || !Number.isSafeInteger(settlements.total) || settlements.total < 0) throw new Error('结算记录响应无效，请刷新重试。')
      setOverview(status)
      if (!draftInitialized.current && status.officialCatalog) {
        setDraft(officialBillingPolicyDraft(status.officialCatalog, status.versions.find(version => version.id === status.versionId)))
        draftInitialized.current = true
      }
      setSelectedId(previous => status.versions.some(version => version.id === previous) ? previous : status.versionId || '')
      const lastPage = Math.max(0, Math.ceil(settlements.total / PAGE_SIZE) - 1)
      if (page > lastPage) { setPage(lastPage); return }
      setRecords(settlements)
    } catch (failure) { if (isCurrent(token, signal) && request === sequence.current && failure.name !== 'AbortError') setError(failure.message) }
    finally { if (isCurrent(token, signal) && request === sequence.current) setLoading(false) }
  }, [token, page])
  const latestRefresh = useRef(refresh); latestRefresh.current = refresh
  useEffect(() => { const controller = new AbortController(); lifetime.current = controller; return () => { controller.abort(); sequence.current++ } }, [])
  useEffect(() => { refresh() }, [refresh])

  function mutate(operation, commit) {
    if (mutationGate.current.has('policy-mutation')) return
    const signal = lifetime.current.signal
    setBusy(true); setError(''); setActionError(''); setNotice('')
    return mutationGate.current.run('policy-mutation', async () => {
      try {
        const result = await operation(signal)
        if (!isCurrent(token, signal)) return
        commit(result)
        await latestRefresh.current()
      } catch (failure) {
        if (isCurrent(token, signal) && failure.name !== 'AbortError') {
          const message = failure.status === 409 ? `${failure.message} 输入内容已保留；请刷新状态后重新确认操作。` : failure.message
          setError(message); setActionError(message)
        }
      } finally { if (lifetime.current?.signal === signal && !signal.aborted) setBusy(false) }
    })
  }

  const selected = overview?.versions.find(version => version.id === selectedId)
  const currentVersion = overview?.versions.find(version => version.id === overview.versionId)
  const enableBlock = billingPolicyEnableBlock(selected, overview)
  const updateRate = (index, key, value) => setDraft(previous => ({ ...previous, rates: previous.rates.map((row, rowIndex) => rowIndex === index ? { ...row, [key]: value } : row) }))
  const saveDraft = event => {
    event.preventDefault()
    let payload
    try { payload = billingPolicyDraftPayload(draft) }
    catch (failure) { setError(failure.message); return }
    mutate(signal => billingPolicyClient.saveVersion(token, payload, signal), version => {
      if (!version?.id) throw new Error('保存响应未返回规则版本，请刷新确认是否已保存。')
      setOverview(previous => previous ? { ...previous, versions: [version, ...previous.versions.filter(item => item.id !== version.id)] } : previous)
      setSelectedId(version.id); setDraft(officialBillingPolicyDraft(overview.officialCatalog, version)); setNotice('新规则版本已保存，尚未变更当前计费状态。')
    })
  }
  const openState = enabled => {
    setActionError(''); setAction({ overview, version: enabled ? selected : currentVersion || (overview?.versionId ? { id: overview.versionId } : null), enabled })
  }
  const applyState = payload => mutate(signal => billingPolicyClient.setState(token, payload, signal), result => {
    if (result?.enabled !== payload.enabled || result?.versionId !== payload.versionId || !Number.isSafeInteger(result?.revision) || result.revision <= payload.revision) throw new Error('服务端返回的计费状态与请求不一致，请刷新核对。')
    setOverview(previous => ({ ...previous, ...result })); setAction(null)
    setNotice(result.enabled ? '服务端已确认启用所选计费规则。' : '服务端已确认积分计费关闭。')
  })

  return <section className="billing-policy-workspace" aria-label="计费规则管理">
    <header><div><span className="policy-eyebrow">JOYNIU CAD · BILLING POLICY</span><h1>计费规则</h1><p>按官方 API 价格折算积分，调整倍率后生成新版本，旧任务和历史账单保持原规则。</p></div><button disabled={loading || busy} onClick={refresh}>{loading ? '正在读取…' : '刷新状态'}</button></header>
    {error && <p className="policy-error" role="alert">{error}</p>}{notice && <p className="policy-notice" role="status">{notice}</p>}
    <section className="policy-card policy-current"><div><span className={`policy-badge ${overview?.enabled ? 'enabled' : ''}`}>{overview ? overview.enabled ? '积分计费已启用' : '积分计费未启用' : '计费状态待确认'}</span><p>{overview?.message || '正在向服务器读取当前计费状态。'}</p><small>当前规则：{overview?.versionId || '未选择'}{overview && ` · 状态版本 ${overview.revision}`}</small></div><button className="policy-danger" disabled={!overview || busy} onClick={() => openState(false)}>关闭计费</button></section>
    <div className="policy-layout">
      <section className="policy-card"><div className="policy-section-heading"><div><h2>新建规则版本</h2><p>充值固定 10 元 1000 积分。修改倍率或结算汇率，保存并启用后对新任务生效。</p></div></div>
        <form className="policy-draft" onSubmit={saveDraft}>
          {draft.apiPricing ? <OfficialPriceEditor draft={draft} catalog={overview?.officialCatalog} busy={busy} setDraft={setDraft} updateRate={updateRate} /> : <fieldset disabled={busy}><legend>模型费率 · 积分 / 百万 token</legend>
            {!draft.rates.length && <p className="policy-hint">尚未添加模型费率；可保存待定版本。</p>}
            {draft.rates.map((row, index) => <div className="policy-rate-editor" key={index}><div className="policy-rate-heading"><b>模型 {index + 1}</b><button type="button" aria-label={`删除模型费率 ${index + 1}`} onClick={() => setDraft(previous => ({ ...previous, rates: previous.rates.filter((_, rowIndex) => rowIndex !== index) }))}>删除</button></div><div className="policy-rate-model"><label>供应商<input required maxLength={100} value={row.provider} onChange={event => updateRate(index, 'provider', event.target.value)} /></label><label>模型名称<input required maxLength={128} value={row.model} onChange={event => updateRate(index, 'model', event.target.value)} /></label></div><div className="policy-rate-values">{Object.entries(policyRateFields).map(([key, name]) => <label key={key}>{name}<input required inputMode="numeric" pattern="(?:0|[1-9][0-9]*)" value={row[key]} onChange={event => updateRate(index, key, event.target.value)} /></label>)}</div></div>)}
            <button type="button" disabled={draft.rates.length >= 100} onClick={() => setDraft(previous => ({ ...previous, rates: [...previous.rates, emptyBillingRate()] }))}>＋ 添加模型费率</button>
          </fieldset>}
          <fieldset disabled={busy || Boolean(draft.apiPricing)}><legend>收费终态 · 需明确选择</legend><div className="policy-status-choices">{Object.entries(billingPolicyStatuses).map(([value, name]) => <label className="policy-check" key={value}><input type="checkbox" checked={draft.billableStatuses.includes(value)} onChange={event => setDraft(previous => ({ ...previous, billableStatuses: event.target.checked ? [...previous.billableStatuses, value] : previous.billableStatuses.filter(item => item !== value) }))} />{name}</label>)}</div><p className="policy-hint">当前约定：仅成功生成候选模型或完成建模的轮次收费。等待补充、失败、取消和中断的轮次不收费。</p></fieldset>
          <label>余额不足策略<select disabled={busy || Boolean(draft.apiPricing)} value={draft.insufficientBalancePolicy} onChange={event => setDraft(previous => ({ ...previous, insufficientBalancePolicy: event.target.value }))}><option value="">未配置，请明确选择</option>{Object.entries(insufficientBalancePolicies).map(([value, name]) => <option key={value} value={value}>{name}{overview?.supportedInsufficientBalancePolicies.includes(value) ? '' : '（服务端暂不支持启用）'}</option>)}</select></label>
          <p className="policy-hint">记录待补缴：先扣现有余额，余款记为待补缴；充值自动补缴，存在待付时不能发起新任务。最多扣余额：只扣至 0，差额由平台承担并记录。</p>
          <label>新建原因<textarea required maxLength={500} rows={3} disabled={busy} value={draft.reason} onChange={event => setDraft(previous => ({ ...previous, reason: event.target.value }))} /></label>
          <footer><span>保存版本不会自动启用计费。</span><button type="submit" className="policy-primary" disabled={busy || !overview}>{busy ? '正在提交…' : '保存新版本'}</button></footer>
        </form>
      </section>
      <section className="policy-card"><div className="policy-section-heading"><div><h2>规则版本与启用</h2><p>选择已保存版本，核对后单独启用。</p></div></div><div className="policy-version-panel">
        <label>查看规则版本<select disabled={!overview || busy} value={selectedId} onChange={event => setSelectedId(event.target.value)}><option value="">请选择已保存版本</option>{overview?.versions.map(version => <option key={version.id} value={version.id}>{version.id === overview.versionId ? '当前 · ' : ''}{formatBillingDate(version.createdAt)} · {version.id}</option>)}</select></label>
        {selected ? <><div className="policy-version-summary"><code>{selected.id}</code><span>创建时间：{formatBillingDate(selected.createdAt)}</span><span>操作人：{selected.createdBy}</span><span>原因：{selected.reason}</span><span>余额不足：{insufficientBalancePolicies[selected.insufficientBalancePolicy] || '未选择'}</span><span>收费终态：{selected.billableStatuses.map(value => billingPolicyStatuses[value] || value).join('、') || '未选择'}</span></div><PricingSummary pricing={selected.apiPricing} /><PolicyRates rates={selected.rates} apiPricing={selected.apiPricing} /><button type="button" disabled={busy} onClick={() => { setDraft(officialBillingPolicyDraft(overview.officialCatalog, selected)); setNotice('已复制到左侧，可修改倍率后保存为新版本。') }}>以此版本调整倍率</button></> : <p className="policy-empty">{overview?.versions.length ? '请选择规则版本。' : '尚无已保存的规则版本。'}</p>}
        {enableBlock && <p className="policy-hint">{enableBlock}</p>}
        <button type="button" className="policy-primary" disabled={busy || !overview || Boolean(enableBlock) || (overview.enabled && selectedId === overview.versionId)} onClick={() => openState(true)}>{overview?.enabled && selectedId === overview.versionId ? '此版本正在启用' : '核对并启用所选版本'}</button>
        <p className="policy-hint">按任务本次实际调用汇总积分后向上取整一次。缺少供应商用量或模型费率的结算由服务端保留待核查。</p>
      </div></section>
    </div>
    <section className="policy-card policy-settlements"><div className="policy-section-heading"><div><h2>任务结算记录</h2><p>仅显示服务端记录，待结算项不会显示为已扣积分。</p></div><span>{records ? `${records.total} 条` : '尚未加载'}</span></div>
      {!records ? <p className="policy-empty">{loading ? '正在加载结算记录…' : '结算记录尚未加载，请刷新重试。'}</p> : !records.items.length ? <p className="policy-empty">暂无任务结算记录。</p> : <div className="policy-table-wrap"><table><thead><tr><th>任务 / 本次尝试</th><th>账号</th><th>任务终态</th><th>结算状态</th><th>规则核算积分</th><th>实际扣除积分</th><th>待补缴 / 平台承担</th><th>规则 / 说明</th></tr></thead><tbody>{records.items.map(record => <tr key={record.attemptId}><td><code>{record.jobId}</code><small>{record.attemptId}</small></td><td><code>{record.ownerId}</code></td><td>{billingPolicyStatuses[record.terminalStatus] || record.terminalStatus || '—'}</td><td>{settlementStatusLabel(record.status)}</td><td>{count(record.expectedCreditUnits)}</td><td>{count(record.chargedUnits)}</td><td>{count(record.deferredUnits)} / {count(record.platformAbsorbedUnits)}</td><td><code>{record.versionId || '未启用规则'}</code><small>{settlementReasonLabel(record.reason)}</small>{record.apiPricing && <details><summary>查看费用明细</summary><PricingSummary pricing={record.apiPricing} /><span>折算基价 ${record.apiEquivalentUsd} · 精确积分 {record.creditUnitsExact} · 本轮合计向上取整一次</span>{record.callBreakdown?.map(call => <p key={call.callId}>{call.model}：输入 {call.inputTokens} / 缓存 {call.cachedInputTokens} / 输出 {call.outputTokens} · {call.creditUnitsExact} 积分{call.notes?.length > 0 && '（未确认的附加费用已免收）'}</p>)}</details>}</td></tr>)}</tbody></table></div>}
      {records && <nav className="policy-pagination" aria-label="结算记录分页"><span>第 {page + 1} / {Math.max(1, Math.ceil(records.total / PAGE_SIZE))} 页</span><div><button disabled={loading || page === 0} onClick={() => setPage(previous => previous - 1)}>上一页</button><button disabled={loading || (page + 1) * PAGE_SIZE >= records.total} onClick={() => setPage(previous => previous + 1)}>下一页</button></div></nav>}
    </section>
    {action && <StateDialog action={action} busy={busy} error={actionError} onClose={() => setAction(null)} onSubmit={applyState} />}
  </section>
}

export default function AdminBillingPolicyWorkspace({ account, onLogin }) {
  const session = account?.session
  if (!session?.access_token) return <section className="billing-policy-workspace"><h1>计费规则</h1><p>登录管理员账号后继续。</p><button className="policy-primary" onClick={onLogin}>登录账号</button></section>
  if (!hasAdminPermission(session.user, 'billing:policy')) return <section className="billing-policy-workspace"><h1>计费规则</h1><p role="alert">当前账号没有计费规则管理权限。</p></section>
  return <PolicySession key={session.user.id} session={session} />
}
