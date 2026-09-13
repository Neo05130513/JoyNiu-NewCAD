import { adminFinanceViews, financeFilters, financeCsv } from './adminFinance.js'
import { billingAdminCapabilities } from './adminPermissions.js'
import { useCallback, useEffect, useRef, useState } from 'react'
import QRCode from 'qrcode'
import { createClientId } from './clientId.js'
import { adminOperations } from './adminOperationsClient.js'
import { billingPolicyStatuses, insufficientBalancePolicies, settlementReasonLabel, settlementStatusLabel } from './billingPolicyClient.js'
import { billingClient, billingSessionKey, billingStatusLabel, billingLedgerLabel, billingLedgerKindLabel, formatBillingRate, billingPricingNote, walletDueNotice, canReviewBillingRequest, createBillingActionGate, formatBillingDate, formatCreditUnits, formatFen, parseCreditUnits, parseMoneyFen, paymentCheckout, refundResultNotice, shouldPollOrder } from './billingClient.js'
import './billing.css'

const EMPTY_PARAMS = Object.freeze({})
const PAGE_SIZE = 20
const userTabs = [['orders', '订单记录'], ['ledger', '积分明细'], ['settlements', '任务结算'], ['refund-requests', '退款申请'], ['invoice-requests', '发票申请']]
const adminTabs = [['orders', '全部订单'], ['ledger', '积分流水'], ['settlements', '任务结算'], ['refund-requests', '退款处理'], ['invoice-requests', '发票处理'], ['usage', '模型调用成本'], ['audit', '操作审计']]
const count = formatCreditUnits
const money = formatFen

function Status({ value }) { return <span className={`billing-status billing-status-${value}`}>{billingStatusLabel(value)}</span> }

function Modal({ title, busy = false, onClose, children }) {
  const dialog = useRef(null)
  const returnFocus = useRef(null)
  useEffect(() => {
    returnFocus.current = document.activeElement
    dialog.current?.showModal()
    return () => { returnFocus.current?.focus?.() }
  }, [])
  return <dialog ref={dialog} className="billing-dialog" aria-label={title} onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }}>
    <div className="billing-dialog-heading"><h2>{title}</h2><button type="button" aria-label="关闭弹窗" disabled={busy} onClick={onClose}>×</button></div>{children}
  </dialog>
}

function CheckoutDialog({ order, onClose, onRefresh, busy, pollingNote, onlinePaymentEnabled }) {
  const checkout = paymentCheckout(order, Date.now(), onlinePaymentEnabled === true)
  const [qr, setQr] = useState('')
  const [qrError, setQrError] = useState('')
  useEffect(() => {
    let current = true
    setQr(''); setQrError('')
    if (checkout.canPay) QRCode.toDataURL(checkout.url, { errorCorrectionLevel: 'M', margin: 3, width: 280 })
      .then((value) => { if (current) setQr(value) }).catch(() => { if (current) setQrError('支付二维码未能显示，请使用下方支付链接或稍后重试。') })
    return () => { current = false }
  }, [checkout.url, checkout.canPay])
  return <Modal title={order.provider === 'offline' ? '线下充值记录' : order.status === 'paid' ? '支付已确认' : '订单详情'} onClose={onClose}>
    <div className="billing-checkout"><p>{order.package?.name}</p><strong>{money(order.amountFen)}</strong><p>{count(order.creditUnits)} 积分</p><Status value={order.status} />
      {checkout.canPay && <>{qr ? <img src={qr} width="280" height="280" alt="此订单的真实支付二维码" /> : !qrError && <p role="status">正在生成支付二维码…</p>}
        <p>使用支付应用扫描二维码，或打开支付页面。</p><a className="billing-primary" href={checkout.url} target="_blank" rel="noopener noreferrer">打开支付链接 ↗</a></>}
      {checkout.expired && order.status !== 'paid' && <p className="billing-warning">支付链接已过期，请先刷新订单确认结果。需要再次购买时请创建新订单。</p>}
      {order.provider === 'offline' ? <p>到账凭证：{order.offlineReceipt?.externalReference}<br />收款说明：{order.offlineReceipt?.reason}<br />登记时间：{formatBillingDate(order.offlineReceipt?.confirmedAt)}<br />按 10 元 = 1000 积分登记；新增积分优先补缴已完成任务。</p> : order.status === 'paid' ? <p className="billing-success">支付渠道已确认收款，积分已记入此账号。</p>
        : <p className="billing-muted">{order.status === 'payment_unavailable' ? '支付渠道暂不可用，订单记录已保留。未确认支付前不会增加积分。' : '支付结果由服务端确认，可关闭弹窗后在订单记录中继续查看。'}</p>}
      {qrError && <p role="alert">{qrError}</p>}{pollingNote && <p role="status">{pollingNote}</p>}
      <dl className="billing-order-meta"><dt>订单编号</dt><dd>{order.id}</dd><dt>创建时间</dt><dd>{formatBillingDate(order.createdAt)}</dd>{order.checkout?.expiresAt && <><dt>支付有效期</dt><dd>{formatBillingDate(order.checkout.expiresAt)}</dd></>}</dl>
      <button type="button" disabled={busy} onClick={onRefresh}>{busy ? '正在查询…' : '刷新支付状态'}</button>
    </div>
  </Modal>
}

function FormDialog({ spec, busy, error, onClose, onSubmit, token }) {
  const [values, setValues] = useState(() => spec.type === 'package' ? {
    name: spec.item?.name || '', amount: spec.item ? (spec.item.amountFen / 100).toFixed(2) : '', credits: String(spec.item?.creditUnits || ''), active: spec.item?.active || false,
  } : spec.type === 'adjustment' ? { ownerId: spec.ownerId || '', credits: '', reason: '', category: 'compensation' }
    : spec.type === 'offline-recharge' ? { ownerId: spec.ownerId || '', amount: '', externalReference: '', reason: '', confirmed: false }
    : spec.type === 'offline-refund' ? { externalReference: '', reason: '', confirmed: false }
    : spec.type === 'refund' ? { amount: (spec.order.amountFen / 100).toFixed(2), reason: '' }
      : spec.type === 'invoice' ? { title: '', taxId: '', email: '' }
        : { status: spec.item.kind === 'refund' ? 'reviewing' : 'processing', note: '', externalReference: '' })
  const [validation, setValidation] = useState('')
  const [customerQuery, setCustomerQuery] = useState(spec.ownerId || '')
  const [customers, setCustomers] = useState(null)
  const [customerError, setCustomerError] = useState('')
  useEffect(() => {
    if (!['offline-recharge', 'adjustment'].includes(spec.type)) return
    const controller = new AbortController()
    setCustomerError('')
    const timer = setTimeout(() => adminOperations.read(token, 'customers', { q: customerQuery, active: 'true', limit: 20 }, controller.signal)
      .then(page => { if (!controller.signal.aborted) setCustomers(page.items) })
      .catch(failure => { if (!controller.signal.aborted) setCustomerError(failure.message) }), 250)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [customerQuery, spec.type, token])
  const idempotencyKeys = useRef(new Map())
  const set = (key) => (event) => setValues((current) => ({ ...current, [key]: event.target.type === 'checkbox' ? event.target.checked : event.target.value }))
  const field = (key, label, attributes = {}) => <label key={key}>{label}<input required value={values[key]} onChange={set(key)} {...attributes} /></label>
  const title = { package: spec.item ? '编辑积分套餐' : '新增积分套餐', adjustment: '赠送、补偿与调账', 'offline-recharge': '登记线下到账充值', 'offline-refund': '登记已完成的线下退款', refund: '申请退款', invoice: '申请发票', review: '处理申请', 'execute-refund': '确认执行真实退款' }[spec.type]
  async function submit(event) {
    event.preventDefault(); setValidation('')
    try {
      let payload
      if (spec.type === 'package') payload = { name: values.name.trim(), amountFen: parseMoneyFen(values.amount), creditUnits: parseCreditUnits(values.credits), active: values.active, ...(spec.item ? { version: spec.item.version } : {}) }
      else if (spec.type === 'adjustment') payload = { ownerId: values.ownerId.trim(), creditUnits: parseCreditUnits(values.credits, { signed: true }), reason: values.reason.trim(), category: values.category }
      else if (spec.type === 'offline-recharge') payload = { ownerId: values.ownerId, amountFen: parseMoneyFen(values.amount), externalReference: values.externalReference.trim(), reason: values.reason.trim(), receiptConfirmed: values.confirmed }
      else if (spec.type === 'offline-refund') payload = { externalReference: values.externalReference.trim(), reason: values.reason.trim(), refundConfirmed: values.confirmed, ...(spec.item ? { refundRequestId: spec.item.id } : {}) }
      else if (spec.type === 'refund') payload = { amountFen: parseMoneyFen(values.amount, { maximum: spec.order.amountFen }), reason: values.reason.trim() }
      else if (spec.type === 'invoice') payload = { title: values.title.trim(), taxId: values.taxId.trim(), email: values.email.trim() }
      else if (spec.type === 'execute-refund') payload = {}
      else payload = { status: values.status, note: values.note.trim(), externalReference: values.externalReference.trim() }
      if (['adjustment', 'refund', 'invoice', 'offline-recharge', 'offline-refund'].includes(spec.type)) {
        const fingerprint = JSON.stringify(payload)
        if (!idempotencyKeys.current.has(fingerprint)) idempotencyKeys.current.set(fingerprint, createClientId())
        payload.idempotencyKey = idempotencyKeys.current.get(fingerprint)
      }
      await onSubmit(payload)
    } catch (failure) { setValidation(failure.message) }
  }
  return <Modal title={title} onClose={onClose} busy={busy}><form className="billing-form" onSubmit={submit}>
    {['offline-recharge', 'adjustment'].includes(spec.type) && <><label>查找客户<input type="search" value={customerQuery} onChange={event => setCustomerQuery(event.target.value)} placeholder="姓名、邮箱或账号 ID" maxLength={128} autoFocus /></label><label>充值 / 调整目标客户<select required value={values.ownerId} onChange={set('ownerId')}><option value="">请选择核对后的客户</option>{values.ownerId && !(customers || []).some(customer => customer.id === values.ownerId) && <option value={values.ownerId}>{values.ownerId}</option>}{(customers || []).map(customer => <option key={customer.id} value={customer.id}>{customer.displayName} · {customer.email} · {customer.id}</option>)}</select></label>{customerError && <p className="billing-error" role="alert">{customerError}</p>}<p className="billing-muted">客户账号：{values.ownerId || '尚未选择'}。请与收款信息核对一致。</p></>}
    {spec.type === 'offline-recharge' && <>{field('amount', '实际到账金额 / 元', { inputMode: 'decimal', placeholder: '例如 10.00' })}<p className="billing-success">固定兑换：10 元 = 1000 积分。{(() => { try { return `本次登记 ${count(parseMoneyFen(values.amount))} 积分。` } catch { return '积分由实收金额自动计算。' } })()}</p></>}
    {spec.type === 'offline-refund' && <p>原充值订单：{spec.order?.id || spec.item?.orderId}<br />整单退款：{money(spec.order?.amountFen ?? spec.item?.data?.amountFen)}。登记后回收原单全部积分；余额不足时无法登记，请先核账。</p>}
    {['offline-recharge', 'offline-refund'].includes(spec.type) && <>{field('externalReference', spec.type === 'offline-recharge' ? '实际到账流水号 / 收款凭证编号' : '实际退款流水号 / 退款凭证编号', { maxLength: 200 })}<label>{spec.type === 'offline-recharge' ? '收款说明' : '退款说明'}<textarea required maxLength={1000} value={values.reason} onChange={set('reason')} /></label><p className="billing-muted">凭证编号在所有操作人之间唯一。登记后保留原始记录和审计；赠送或补偿请使用独立的积分调整入口。</p><label className="billing-checkbox"><input type="checkbox" required checked={values.confirmed} onChange={set('confirmed')} />{spec.type === 'offline-recharge' ? '我已核对客户和实际到账金额，确认登记充值' : '我已核实线下退款已实际完成，确认登记并回收积分；此操作不自动转账'}</label></>}
    {spec.type === 'package' && <>{field('name', '套餐名称', { maxLength: 100, autoFocus: true })}<div className="billing-form-grid">{field('amount', '售价 / 元', { inputMode: 'decimal', placeholder: '例如 99.00' })}{field('credits', '包含积分', { inputMode: 'numeric' })}</div><label className="billing-checkbox"><input type="checkbox" checked={values.active} onChange={set('active')} />保存后上架</label><p className="billing-muted">价格与积分只对新订单生效，已创建订单保留购买时的套餐内容。</p></>}
    {spec.type === 'adjustment' && <><label>调整类型<select value={values.category} onChange={set('category')}><option value="gift">赠送积分</option><option value="compensation">故障补偿</option><option value="adjustment">人工调账</option></select></label>{field('credits', '调整积分', { inputMode: 'numeric', placeholder: '增加填正整数，扣减填负整数' })}<label>调整原因<textarea required maxLength={1000} value={values.reason} onChange={set('reason')} /></label><p className="billing-muted">此入口不登记人民币收入。真实线下收款请使用“登记线下充值”；提交后立即入账并保留审计。</p></>}
    {spec.type === 'refund' && <><p>订单 {spec.order.id} · 实付 {money(spec.order.amountFen)}</p>{field('amount', '申请退款 / 元', { inputMode: 'decimal', autoFocus: true, readOnly: spec.order.provider === 'offline' })}{spec.order.provider === 'offline' && <p>线下充值目前仅支持整单退款，运营会核验可回收积分并在线下处理。</p>}<label>退款原因<textarea required maxLength={1000} value={values.reason} onChange={set('reason')} /></label><p className="billing-muted">提交申请后等待处理，退款进度可在退款申请中查看。</p></>}
    {spec.type === 'invoice' && <><p>订单 {spec.order.id} · {money(spec.order.amountFen)}</p>{field('title', '发票抬头', { maxLength: 200, autoFocus: true })}{field('taxId', '税号（个人可不填）', { required: false, maxLength: 50 })}{field('email', '收票邮箱', { type: 'email', maxLength: 254, autoComplete: 'email' })}</>}
    {spec.type === 'review' && <><p>{spec.item.kind === 'refund' ? `退款申请 · ${money(spec.item.data?.amountFen)}` : `发票申请 · ${spec.item.data?.title}`}<br />订单 {spec.item.orderId}</p><label>处理状态<select value={values.status} onChange={set('status')}>{(spec.item.kind === 'refund' ? ['reviewing', 'approved', 'rejected'] : ['processing', 'issued', 'rejected']).map((status) => <option value={status} key={status}>{billingStatusLabel(status)}</option>)}</select></label><label>处理说明<textarea required maxLength={1000} value={values.note} onChange={set('note')} /></label>{field('externalReference', spec.item.kind === 'invoice' ? '发票号码或外部凭证' : '外部处理凭证（可选）', { required: values.status === 'issued', maxLength: 200 })}{spec.item.kind === 'refund' && <p className="billing-muted">{spec.item.data?.provider === 'offline' ? '审核通过后，请先核账并在线下办理退款，再登记实际退款凭证；系统不自动转账。' : '审核通过后，由支付渠道执行退款。退款到账状态以渠道确认为准。'}</p>}</>}
    {spec.type === 'execute-refund' && <><p>订单 <b>{spec.item.orderId}</b><br />申请金额 <b>{money(spec.item.data?.amountFen)}</b></p><p>确认后将向真实支付渠道提交整单退款，并收回该订单的全部积分。服务端会核验实付金额与可回收积分；不满足条件时不会提交渠道。</p><label className="billing-checkbox"><input type="checkbox" required />我确认执行真实退款并收回此订单积分</label></>}
    {(validation || error) && <p className="billing-error" role="alert">{validation || error}</p>}
    <div className="billing-dialog-actions"><button type="button" disabled={busy} onClick={onClose}>取消</button><button className="billing-primary" type="submit" disabled={busy}>{busy ? '正在提交…' : spec.type === 'adjustment' ? '确认调整积分' : '确认提交'}</button></div>
  </form></Modal>
}

function Records({ kind, page, loading, admin, canManage, busy, onOrder, onRequest, onReview, onExecuteRefund, onReconcileRefund, onReconcileOrder, paymentQueryAvailable, refundEnabled, onlinePaymentEnabled, onOfflineRefund, onNavigate }) {
  const records = page?.items || []
  if (!page) return <p className="billing-empty">{loading ? '正在加载记录…' : '记录尚未加载，请刷新重试。'}</p>
  if (!records.length) return <p className="billing-empty">暂无{(admin ? adminTabs : userTabs).find(([key]) => key === kind)?.[1] || '记录'}。</p>
  if (kind === 'settlements') return <div className="billing-table-scroll"><table><thead><tr>{['任务 / 本次尝试', '任务终态', '结算状态', '规则核算积分', '实际扣除', '待补缴', '平台承担', '说明'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{records.map(item => <tr key={item.attemptId}><td><code>{item.jobId}</code><small>{item.attemptId}</small></td><td>{billingPolicyStatuses[item.terminalStatus] || item.terminalStatus || '—'}</td><td>{settlementStatusLabel(item.status)}</td><td>{count(item.expectedCreditUnits)}</td><td>{count(item.chargedUnits)}</td><td>{count(item.deferredUnits)}</td><td>{count(item.platformAbsorbedUnits)}</td><td>{settlementReasonLabel(item.reason)}<small>规则：{item.versionId || '未启用'}</small>{item.apiPricing && <details><summary>计费明细</summary><p>API 标准价折算金额 ${item.apiEquivalentUsd} × 汇率 {item.apiPricing.usdCnyRate} × 100 × 倍率 {item.apiPricing.multiplier} = {item.creditUnitsExact} 积分；本次汇总向上取整一次。</p>{(item.callBreakdown || []).map(call => <p key={call.callId}><code>{call.callId}</code><br />{call.model} · 输入 {count(call.inputTokens)} / 缓存读取 {count(call.cachedInputTokens)} / 缓存写入 {count(call.cacheWriteTokens)} / 输出 {count(call.outputTokens)}<br />{call.contextBand === 'long' ? '已按长上下文标准折算' : '普通上下文标准'} · ${call.apiEquivalentUsd} · {call.creditUnitsExact} 积分{(call.notes || []).map(note => <small key={note}>{billingPricingNote(note)}</small>)}</p>)}</details>}</td></tr>)}</tbody></table></div>
  if (kind === 'orders') return <div className="billing-table-scroll"><table><thead><tr>{['订单 / 时间', ...(admin ? ['账号'] : []), '套餐', '金额 / 积分', '状态', '操作'].map((text) => <th key={text}>{text}</th>)}</tr></thead><tbody>{records.map((order) => <tr key={order.id}><td><code>{order.id}</code><small>{formatBillingDate(order.createdAt)}</small></td>{admin && <td><button className="billing-customer-link" onClick={() => onNavigate?.("customers", { customerId: order.ownerId })}>{order.customerName || "查看客户"}</button><small>{order.ownerId}</small></td>}<td>{order.package?.name || '已存档套餐'}<small>{order.provider === 'offline' ? '线下实收' : '在线支付'}</small>{order.offlineReceipt?.externalReference && <small>凭证：{order.offlineReceipt.externalReference}</small>}</td><td>{money(order.amountFen)}<small>{count(order.creditUnits)} 积分</small></td><td><Status value={order.status} /></td><td><div className="billing-row-actions">{admin ? <><details><summary>订单详情</summary><p>渠道：{order.provider === 'offline' ? '线下到账登记' : order.provider || '—'}<br />凭证：{order.offlineReceipt?.externalReference || order.transactionId || '—'}<br />说明：{order.offlineReceipt?.reason || '—'}<br />更新：{formatBillingDate(order.updatedAt)}</p></details>{canManage && order.provider === 'offline' && order.status === 'paid' && <button disabled={busy} onClick={() => onOfflineRefund(order)}>登记线下退款</button>}{canManage && order.provider !== 'offline' && <button disabled={busy || !paymentQueryAvailable} onClick={() => onReconcileOrder(order)} title={!paymentQueryAvailable ? '真实支付查询渠道尚未配置' : ''}>查询支付结果</button>}</> : <><button disabled={busy} onClick={() => onOrder(order)}>{onlinePaymentEnabled && order.status === 'pending_payment' ? '继续支付' : '查看订单'}</button>{order.status === 'paid' && <><button disabled={busy} onClick={() => onRequest('refund', order)}>退款</button><button disabled={busy} onClick={() => onRequest('invoice', order)}>发票</button></>}</>}</div></td></tr>)}</tbody></table></div>
  if (kind === 'ledger') return <div className="billing-table-scroll"><table><thead><tr>{['时间', ...(admin ? ['客户'] : []), '事项', '积分变动', '变动后余额', '关联记录'].map((text) => <th key={text}>{text}</th>)}</tr></thead><tbody>{records.map((item) => <tr key={item.id}><td>{formatBillingDate(item.createdAt)}</td>{admin && <td><button className="billing-customer-link" onClick={() => onNavigate?.("customers", { customerId: item.ownerId })}>{item.customerName || "查看客户"}</button><small>{item.ownerId}</small></td>}<td>{billingLedgerKindLabel(item.kind)}<small>{billingLedgerLabel(item)}</small></td><td className={item.deltaUnits > 0 ? 'billing-positive' : ''}>{item.deltaUnits > 0 ? '+' : ''}{count(item.deltaUnits)}</td><td>{count(item.balanceAfter)}</td><td><code>{item.referenceId || '—'}</code></td></tr>)}</tbody></table></div>
  if (kind === 'refund-requests' || kind === 'invoice-requests') return <div className="billing-request-list">{records.map((item) => <article key={item.id}><header><div><b>{item.kind === 'refund' ? `退款 ${money(item.data?.amountFen)}` : item.data?.title}</b><small>{formatBillingDate(item.createdAt)}</small></div><Status value={item.status} /></header><p>订单 <code>{item.orderId}</code>{admin && <> · 账号 <code>{item.ownerId}</code></>}</p><p>{item.kind === 'refund' ? item.data?.reason : `收票邮箱：${item.data?.email}　税号：${item.data?.taxId || '未填写'}`}</p>{item.adminNote && <p>处理说明：{item.adminNote}</p>}{item.externalReference && <p>外部凭证：{item.externalReference}</p>}{admin && canManage && <div className="billing-row-actions">{canReviewBillingRequest(item) && <button disabled={busy} onClick={() => onReview(item)}>处理申请</button>}{item.kind === 'refund' && item.status === 'approved' && (item.data?.provider === 'offline' ? <button disabled={busy} onClick={() => onOfflineRefund(null, item)}>登记已完成的线下退款</button> : <button disabled={busy || !refundEnabled} onClick={() => onExecuteRefund(item)} title={!refundEnabled ? '真实退款渠道尚未开放' : ''}>{refundEnabled ? '执行真实退款' : '退款执行未开放'}</button>)}{item.kind === 'refund' && ['refund_processing', 'refund_abnormal'].includes(item.status) && <button disabled={busy} onClick={() => onReconcileRefund(item)}>查询退款结果</button>}</div>}</article>)}</div>
  if (kind === 'usage') return <div className="billing-table-scroll"><table><thead><tr>{['任务 / 账号', '模型 / 阶段', '输入 / 缓存', '输出 / 推理', '供应商成本', '时间'].map((text) => <th key={text}>{text}</th>)}</tr></thead><tbody>{records.map((item, index) => <tr key={`${item.callId}-${index}`}><td><code>{item.jobId}</code><small>{item.ownerId}</small><details><summary>调用详情</summary><p>调用：{item.callId}<br />尝试：{item.attemptId || '—'}<br />结果：{item.outcome}</p></details></td><td>{item.model}<small>{item.provider} · {item.stage}</small></td><td>{count(item.inputTokens)} / {count(item.cachedInputTokens)}</td><td>{count(item.outputTokens)} / {count(item.reasoningOutputTokens)}</td><td>{Number.isSafeInteger(item.costMicroUsd) ? `$${(item.costMicroUsd / 1_000_000).toFixed(6)}` : '待核账'}<small>{item.costSource === 'provider_reported' ? '供应商报告' : item.costSource === 'rate_card_calculated' ? `费率计算 · ${item.rateCardVersion || ''}` : '尚无已知成本'}</small></td><td>{formatBillingDate(item.createdAt)}</td></tr>)}</tbody></table></div>
  return <div className="billing-request-list">{records.map((item) => <article key={item.id}><header><b>{item.action}</b><small>{formatBillingDate(item.createdAt)}</small></header><p>操作人 <code>{item.actorId}</code> · 记录 <code>{item.resourceId}</code></p><details><summary>变更详情</summary><pre>{JSON.stringify(item.details, null, 2)}</pre></details></article>)}</div>
}

function BillingSession({ token, admin, capabilities, onWalletChange, adminView, params = EMPTY_PARAMS, onNavigate }) {
  const view = admin && adminView ? adminFinanceViews[adminView] : null
  const appliedFilters = financeFilters(params)
  const [draftFilters, setDraftFilters] = useState(appliedFilters)
  const showPackages = !admin || !view || adminView === 'packages'
  const showMetrics = admin && (!view || ['billing','orders','usage'].includes(adminView))
  const updateFilter = key => event => setDraftFilters(values => ({ ...values, [key]: event.target.value }))
  const exportPage = () => {
    if (!records?.items?.length) return
    const url = URL.createObjectURL(new Blob([financeCsv(tab, records.items)], { type: 'text/csv;charset=utf-8' }))
    const link = document.createElement('a'); link.href = url; link.download = `JoyNiu-${adminView || tab}-第${Math.floor(offset / PAGE_SIZE) + 1}页.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const states = { orders: ['creating','pending_payment','paid','payment_unavailable','cancelled','expired','refunded'], 'refund-requests': ['requested','reviewing','approved','rejected','refund_processing','refunded','refund_abnormal','refund_closed'], 'invoice-requests': ['requested','processing','issued','rejected'], ledger: ['offline_recharge','offline_refund','payment','usage','gift','compensation','adjustment','due_payment'], settlements: ['pending_settlement','settled','not_charged'] }
  const [status, setStatus] = useState(null)
  const [pricing, setPricing] = useState(null)
  const [wallet, setWallet] = useState(null)
  const [dashboard, setDashboard] = useState(null)
  const [packages, setPackages] = useState(null)
  const [tab, setTab] = useState(view?.kind || 'orders')
  const [offset, setOffset] = useState(Math.max(0, Math.floor(Number(params.offset) || 0)))
  const [records, setRecords] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [dialog, setDialog] = useState(null)
  const [dialogError, setDialogError] = useState('')
  const [selectedOrder, setSelectedOrder] = useState(null)
  const [pollingNote, setPollingNote] = useState('')
  const mounted = useRef(false)
  const currentToken = useRef(token)
  currentToken.current = token
  const controller = useRef(null)
  const sequence = useRef(0)
  const refreshCurrent = useRef(null)
  const gate = useRef(createBillingActionGate())
  const orderKeys = useRef(new Map())
  const callback = useRef(onWalletChange)
  callback.current = onWalletChange
  const selectedId = useRef(null)
  selectedId.current = selectedOrder?.id

  const refresh = useCallback(async () => {
    const run = ++sequence.current
    if (!mounted.current) return
    setLoading(true); setError('')
    const signal = controller.current.signal
    const results = await Promise.allSettled([
      billingClient.status(signal), showPackages ? billingClient.packages(token, { admin, signal }) : Promise.resolve({ items: [] }),
      admin ? showMetrics ? billingClient.dashboard(token, signal) : Promise.resolve(null) : billingClient.wallet(token, signal),
      view && !view.kind ? Promise.resolve({ items: [], total: 0 }) : billingClient.list(token, tab, { admin, offset, limit: PAGE_SIZE, signal, ...(admin ? appliedFilters : {}) }),
      admin && view ? Promise.resolve(null) : billingClient.pricing(signal),
    ])
    if (!mounted.current || currentToken.current !== token || run !== sequence.current) return
    const setters = [setStatus, (value) => setPackages(value.items || []), admin ? setDashboard : (value) => { setWallet(value); callback.current?.(value) }, setRecords, setPricing]
    const failures = []
    results.forEach((result, index) => { if (result.status === 'fulfilled') setters[index](result.value); else if (result.reason?.name !== 'AbortError') failures.push(result.reason?.message || '部分记录加载失败。') })
    if (results[4].status === 'rejected') setPricing(null)
    setError([...new Set(failures)].join(' ')); setLoading(false)
  }, [token, admin, tab, offset, adminView, params])
  refreshCurrent.current = refresh

  useEffect(() => { mounted.current = true; controller.current = new AbortController(); return () => { mounted.current = false; controller.current.abort(); sequence.current += 1 } }, [])
  useEffect(() => { refresh() }, [refresh])

  const perform = (key, operation, success, after) => gate.current.run(key, async () => {
    if (!mounted.current || currentToken.current !== token) return false
    setBusy(key); setError(''); setDialogError(''); setNotice('')
    try {
      const result = await operation(controller.current.signal)
      if (!mounted.current || currentToken.current !== token) return false
      after?.(result); if (success) setNotice(success)
      await refreshCurrent.current()
      return true
    } catch (failure) {
      if (mounted.current && currentToken.current === token && failure.name !== 'AbortError') { setError(failure.message); setDialogError(failure.message) }
      return false
    } finally { if (mounted.current) setBusy(current => current === key ? '' : current) }
  })

  const loadOrder = useCallback(async (orderId, signal, reconcile = false) => {
    const response = reconcile ? await billingClient.reconcileOrder(token, orderId, signal) : await billingClient.order(token, orderId, signal)
    const value = reconcile ? response?.order : response
    if (!value || value.id !== orderId || typeof value.status !== 'string') throw new Error('订单查询返回无效状态，请稍后重试。')
    if (!mounted.current || currentToken.current !== token || selectedId.current !== orderId) return value
    setSelectedOrder(value)
    if (value.status === 'paid') { orderKeys.current.delete(value.package?.id); setPollingNote('支付已确认，正在更新积分余额。'); await refreshCurrent.current() }
    return value
  }, [token])

  useEffect(() => {
    if (!selectedOrder?.id || !shouldPollOrder(selectedOrder)) return
    const polling = new AbortController()
    let timer, attempts = 0
    setPollingNote('正在自动查询支付状态…')
    const poll = async () => {
      if (polling.signal.aborted || !mounted.current) return
      if (document.visibilityState !== 'visible') { timer = setTimeout(poll, 4000); return }
      try {
        const order = await loadOrder(selectedOrder.id, polling.signal, Boolean(status?.paymentQueryAvailable && (!admin || capabilities.manage) && attempts % 4 === 0))
        if (!shouldPollOrder(order)) return
      } catch (failure) {
        if (polling.signal.aborted || !mounted.current) return
        setPollingNote('支付状态暂未查询成功，将继续重试；也可手动刷新。')
      }
      attempts += 1
      if (attempts < 60 && !polling.signal.aborted) timer = setTimeout(poll, 4000)
      else if (mounted.current) setPollingNote('自动查询已暂停，请手动刷新支付状态。')
    }
    timer = setTimeout(poll, 4000)
    return () => { polling.abort(); clearTimeout(timer) }
  }, [selectedOrder?.id, selectedOrder?.status, loadOrder, status?.paymentQueryAvailable])

  function buy(item) {
    if (!status?.purchaseEnabled || busy) return
    if (!orderKeys.current.has(item.id)) orderKeys.current.set(item.id, createClientId())
    perform(`buy:${item.id}`, (signal) => billingClient.createOrder(token, item.id, orderKeys.current.get(item.id), signal), '', (order) => { setSelectedOrder(order); setPollingNote('') })
  }
  function openForm(spec) {
    if (admin && ((spec.type === 'package' && !capabilities.policy) || (spec.type === 'adjustment' && !capabilities.adjust) || (['review', 'execute-refund', 'offline-recharge', 'offline-refund'].includes(spec.type) && !capabilities.manage))) return
    setDialogError(''); setDialog({ ...spec, key: createClientId() })
  }
  async function submitDialog(values) {
    if (admin && ((dialog.type === 'package' && !capabilities.policy) || (dialog.type === 'adjustment' && !capabilities.adjust) || (['review', 'execute-refund', 'offline-recharge', 'offline-refund'].includes(dialog.type) && !capabilities.manage))) return
    const operation = dialog.type === 'package' ? (signal) => billingClient.savePackage(token, values, dialog.item?.id, signal)
      : dialog.type === 'offline-recharge' ? (signal) => billingClient.offlineRecharge(token, values, signal)
      : dialog.type === 'offline-refund' ? (signal) => billingClient.offlineRefund(token, dialog.order?.id || dialog.item?.orderId, values, signal)
      : dialog.type === 'adjustment' ? (signal) => billingClient.adjust(token, values, signal)
        : dialog.type === 'execute-refund' ? (signal) => billingClient.executeRefund(token, dialog.item.id, signal)
        : dialog.type === 'review' ? (signal) => billingClient.reviewRequest(token, dialog.item.id, values, signal)
          : (signal) => billingClient.requestService(token, dialog.type, dialog.order.id, values, signal)
    await perform('dialog', operation, dialog.type === 'execute-refund' ? '' : dialog.type === 'adjustment' ? '积分调整已入账。' : '已保存，可在记录中查看处理结果。', result => { setDialog(null); if (dialog.type === 'execute-refund') setNotice(refundResultNotice(result)) })
  }

  return <section className="billing-workspace" aria-label={admin ? '商业运营管理' : '积分与订单'}>
    <header className="billing-heading"><div><span className="billing-eyebrow">JOYNIU CAD · {admin ? 'ADMINISTRATION' : 'ACCOUNT'}</span><h1>{view?.title || (admin ? '商业运营管理' : '积分与订单')}</h1><p>{view?.description || (admin ? '管理套餐、交易、售后与模型调用成本。' : '查看账号积分，管理线下充值记录、退款与发票。')}</p></div><div className="billing-heading-actions">{admin && capabilities.manage && (!view || ['billing', 'orders', 'credits'].includes(adminView)) && <button className="billing-primary" disabled={Boolean(busy) || status?.offlineRechargeEnabled !== true} onClick={() => openForm({ type: 'offline-recharge', ownerId: appliedFilters.ownerId })}>登记线下充值</button>}{admin && capabilities.adjust && (!view || adminView === 'credits') && <button disabled={Boolean(busy)} onClick={() => openForm({ type: 'adjustment', ownerId: appliedFilters.ownerId })}>赠送 / 补偿 / 调账</button>}<button disabled={loading || Boolean(busy)} onClick={refresh}>{loading ? '加载中…' : '刷新记录'}</button></div></header>
    {error && <p className="billing-error" role="alert">{error}</p>}{notice && <p className="billing-success" role="status">{notice}</p>}
    {!status?.purchaseEnabled && <p className="billing-warning" role="status">{status?.message || (loading ? '正在确认积分服务状态…' : '积分服务状态尚未确认，暂不可购买。')}{status && !status.paymentConfigured && ' 当前未配置真实支付渠道。'}</p>}
    {showMetrics ? <div className="billing-metrics">{[['已支付订单', dashboard ? count(dashboard.paidOrders) : '—'], ['净收入 / 人民币', dashboard ? money(dashboard.revenueFen) : '—'], ['已记录供应商成本', dashboard && Number.isSafeInteger(dashboard.knownCostMicroUsd) ? `$${(dashboard.knownCostMicroUsd / 1_000_000).toFixed(6)}` : '—'], ['待核账调用', dashboard ? count(dashboard.unknownCostCalls) : '—']].map(([label, value]) => <article key={label}><span>{label}</span><strong>{value}</strong></article>)}</div>
      : !admin ? <div className="billing-wallet"><div><span>当前积分余额</span><strong>{wallet ? count(wallet.creditUnits) : '—'}<small>积分</small></strong><p>{wallet ? (wallet.enabled ? '余额与每次变动都保存在当前账号。' : '当前建模任务不会扣除积分。') : '正在读取此账号的积分余额。'}</p></div><div className="billing-wallet-account"><span>账号 ID</span><code>{wallet?.ownerId || '—'}</code></div></div> : null}
    {admin && dashboard && <p className="billing-muted">实收总额 {money(dashboard.grossRevenueFen)}，已退款 {money(dashboard.refundedFen)}；其中线下实收 {money(dashboard.offlineGrossRevenueFen)}、线下退款 {money(dashboard.offlineRefundedFen)}。赠送 {count(dashboard.nonRevenueCreditUnits?.gift || 0)}、补偿 {count(dashboard.nonRevenueCreditUnits?.compensation || 0)} 积分，均不计入实收收入。{dashboard.costNote} 已记录 {count(dashboard.usageCalls)} 次调用，{count(dashboard.unknownUsageCalls)} 次用量待核对；待处理退款 {count(dashboard.openRefundRequests)} 件。</p>}
    {!admin && walletDueNotice(wallet) && <p className="billing-warning" role="status">{walletDueNotice(wallet)}</p>}
    {(!admin || !view) && <section className="billing-card"><div className="billing-section-heading"><div><h2>当前公开积分费率</h2><p>{pricing?.enabled ? `规则版本 ${pricing.versionId} · 每百万 token 的积分` : pricing ? '积分计费尚未启用。' : '公开费率尚未确认，请刷新核对。'}</p></div></div>
      {pricing?.enabled ? <><div className="billing-table-scroll"><table><thead><tr><th>供应商 / 模型</th><th>普通输入</th><th>缓存读取</th>{pricing.apiPricing && <th>缓存写入</th>}<th>输出（含推理）</th></tr></thead><tbody>{pricing.rates.map(row => <tr key={`${row.provider}:${row.model}`}><td>{row.provider}<small>{row.model}</small></td><td>{formatBillingRate(row.inputUnitsPerMillion)} 积分</td><td>{formatBillingRate(row.cachedInputUnitsPerMillion)} 积分</td>{pricing.apiPricing && <td>{formatBillingRate(row.cacheWriteUnitsPerMillion)} 积分</td>}<td>{formatBillingRate(row.outputUnitsPerMillion)} 积分</td></tr>)}</tbody></table></div><div className="billing-public-pricing">{pricing.apiPricing && <><p>收费折算：模型 API 标准美元价 × 美元兑人民币汇率 {formatBillingRate(pricing.apiPricing.usdCnyRate)} × 每元 100 积分 × 收费倍率 {formatBillingRate(pricing.apiPricing.multiplier)}。</p><p>价格目录核对日期：{pricing.apiPricing.verifiedAt}。<a href="https://developers.openai.com/api/docs/pricing" target="_blank" rel="noopener noreferrer">查看官方 API 标准价</a>。表格为普通上下文费率；可确认的单次请求超过模型阈值时按该规则的长上下文倍率处理。</p><p>缺少缓存写入或单次请求上下文明细时，不加收对应的未确认差价，并在结算明细注明。已知普通输入、缓存读取和输出仍按规则结算。展示费率最多 6 位小数，结算保留精确数值。</p></>}<p>收费终态：{pricing.billableStatuses.map(value => billingPolicyStatuses[value] || value).join('、')}。</p><p>余额不足：{insufficientBalancePolicies[pricing.insufficientBalancePolicy]}。{pricing.insufficientBalancePolicy === 'deferred_due' ? '先扣现有余额，余款记录为待补缴；充值优先补缴，存在待付时不能发起新的收费任务。' : '本次最多扣至余额为 0，差额由平台承担并留存记录。'}</p><p>任务按发起时登记的规则，以本次终态的实际调用用量结算，汇总后向上取整一次。以任务结算记录为准。</p></div></> : <p className="billing-empty">{pricing?.message || (loading ? '正在读取实际费率…' : '当前没有可展示的已启用费率。')}</p>}
    </section>}
    {!admin && status?.onlinePaymentEnabled !== true && <section className="billing-card billing-offline-help"><h2>线下办理充值</h2><p>10 元 = 1000 积分（1 元 = 100 积分）。请通过账号的服务对接人或工单联系运营，提供账号 ID 和充值金额；运营核实实际到账后登记。</p><p>登记后可在订单和积分明细中核对金额、积分与收款凭证。有待补缴积分时，充值会先补缴已完成任务。</p><p>当前未开放在线购买。退款请从原充值单提交整单申请，开票请填写抬头和收票邮箱，由运营处理。</p></section>}
    {showPackages && (admin || status?.onlinePaymentEnabled === true) && <section className="billing-card"><div className="billing-section-heading"><div><h2>{admin ? '积分套餐管理' : '购买积分'}</h2><p>{admin ? '新建套餐默认为下架状态，确认价格后再上架。' : '套餐价格与购买积分以创建订单时的内容为准；有待补缴时，充值将优先用于补缴。'}</p></div>{admin && capabilities.policy && <button className="billing-primary" disabled={Boolean(busy)} onClick={() => openForm({ type: 'package' })}>＋ 新增套餐</button>}</div>
      {packages === null ? <p className="billing-empty">{loading ? '正在加载套餐…' : '套餐加载失败，请刷新重试。'}</p> : !packages.length ? <p className="billing-empty">{admin ? '尚未创建套餐。' : '暂无可购买套餐。'}</p>
        : <div className="billing-packages">{packages.map((item) => <article key={item.id}><header><h3>{item.name}</h3>{admin && <span className={`billing-status ${item.active ? 'billing-status-paid' : ''}`}>{item.active ? '已上架' : '已下架'}</span>}</header><strong>{count(item.creditUnits)}<small> 积分</small></strong><p className="billing-package-price">{money(item.amountFen)}</p>{admin ? capabilities.policy && <div className="billing-row-actions"><button disabled={Boolean(busy)} onClick={() => openForm({ type: 'package', item })}>编辑</button><button disabled={Boolean(busy)} onClick={() => perform(`package:${item.id}`, (signal) => billingClient.savePackage(token, { version: item.version, active: !item.active }, item.id, signal), item.active ? '套餐已下架。' : '套餐已上架。')}>{item.active ? '下架' : '上架'}</button></div>
          : <button className="billing-primary" disabled={!status?.purchaseEnabled || Boolean(busy)} onClick={() => buy(item)}>{busy === `buy:${item.id}` ? '正在创建订单…' : status?.purchaseEnabled ? '购买此套餐' : '暂不可购买'}</button>}</article>)}</div>}
    </section>}
    {(!view || view.kind) && <section className="billing-card">{!view && <div className="billing-tabs" aria-label="账单记录分类">{(admin ? adminTabs : userTabs).map(([key, label]) => <button key={key} aria-pressed={tab === key} onClick={() => { if (tab !== key) { sequence.current += 1; setTab(key); setOffset(0); setRecords(null) } }}>{label}</button>)}</div>}
      {view && <><form className="admin-finance-filters" onSubmit={event => { event.preventDefault(); onNavigate(adminView, { ...financeFilters(draftFilters), offset: 0 }) }}>
        {tab === 'orders' && <label>收款方式<select value={draftFilters.source || ''} onChange={updateFilter('source')}><option value="">全部方式</option><option value="offline">线下实收</option><option value="online">在线支付</option></select></label>}<label>查找记录<input type="search" maxLength={128} placeholder="订单号、凭证号、客户编号或关联编号" value={draftFilters.q || ''} onChange={updateFilter('q')} /></label>
        <label>客户编号<input maxLength={160} placeholder="全部客户" value={draftFilters.ownerId || ''} onChange={updateFilter('ownerId')} /></label>
        {states[tab] && <label>{tab === 'ledger' ? '变动类型' : '记录状态'}<select value={draftFilters.status || ''} onChange={updateFilter('status')}><option value="">全部</option>{states[tab].map(value => <option key={value} value={value}>{tab === 'ledger' ? billingLedgerLabel({kind:value}) : tab === 'settlements' ? settlementStatusLabel(value) : billingStatusLabel(value)}</option>)}</select></label>}
        <label>开始日期<input type="date" value={draftFilters.dateFrom || ''} onChange={updateFilter('dateFrom')} /></label><label>结束日期<input type="date" min={draftFilters.dateFrom || undefined} value={draftFilters.dateTo || ''} onChange={updateFilter('dateTo')} /></label>
        <div><button className="billing-primary" disabled={loading || !!busy} type="submit">查询</button><button type="button" onClick={() => onNavigate(adminView, {})}>重置</button></div>
      </form><div className="admin-finance-resultbar"><span>按北京时间筛选 · {records ? `${records.total} 条符合条件` : '正在读取'}{showMetrics ? ' · 上方指标为全站累计' : ''}</span><button disabled={loading || !records?.items?.length} onClick={exportPage}>导出本页 CSV</button></div></>}
      <Records onlinePaymentEnabled={status?.onlinePaymentEnabled === true} onOfflineRefund={(order, item) => openForm({ type: 'offline-refund', order, item })} onNavigate={onNavigate} kind={tab} page={records} loading={loading} admin={admin} canManage={capabilities.manage} busy={Boolean(busy)} onOrder={(order) => { setSelectedOrder(order); setPollingNote('') }} onRequest={(type, order) => openForm({ type, order })} onReview={(item) => openForm({ type: 'review', item })} paymentQueryAvailable={status?.paymentQueryAvailable === true} onReconcileOrder={order => perform(`order:${order.id}`, signal => billingClient.reconcileOrder(token, order.id, signal), '', result => setNotice(`已核实订单状态：${billingStatusLabel(result.order?.status)}。`))} refundEnabled={status?.refundExecutionEnabled === true} onExecuteRefund={item => openForm({ type: 'execute-refund', item })} onReconcileRefund={item => perform(`refund:${item.id}`, signal => billingClient.reconcileRefund(token, item.id, signal), '', result => setNotice(refundResultNotice(result)))} />
      {records && <nav className="billing-pagination" aria-label="账单分页"><span>共 {records.total} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(records.total / PAGE_SIZE))} 页</span><div><button disabled={loading || offset === 0} onClick={() => { sequence.current += 1; view ? onNavigate(adminView, {...appliedFilters, offset: Math.max(0, offset - PAGE_SIZE)}) : setOffset(Math.max(0, offset - PAGE_SIZE)); setRecords(null) }}>上一页</button><button disabled={loading || offset + PAGE_SIZE >= records.total} onClick={() => { sequence.current += 1; view ? onNavigate(adminView, {...appliedFilters, offset: offset + PAGE_SIZE}) : setOffset(offset + PAGE_SIZE); setRecords(null) }}>下一页</button></div></nav>}
    </section>}
    {dialog && <FormDialog token={token} key={dialog.key} spec={dialog} busy={Boolean(busy)} error={dialogError} onClose={() => setDialog(null)} onSubmit={submitDialog} />}
    {selectedOrder && <CheckoutDialog onlinePaymentEnabled={status?.onlinePaymentEnabled === true} key={selectedOrder.id} order={selectedOrder} busy={busy === 'order-status'} pollingNote={pollingNote} onClose={() => { if (paymentCheckout(selectedOrder).expired || ['expired', 'cancelled'].includes(selectedOrder.status)) orderKeys.current.delete(selectedOrder.package?.id); setSelectedOrder(null) }} onRefresh={() => perform('order-status', (signal) => loadOrder(selectedOrder.id, signal, status?.paymentQueryAvailable === true && (!admin || capabilities.manage)), '')} />}
  </section>
}

export default function BillingWorkspace({ account, onLogin, admin = false, onWalletChange, adminView, params, onNavigate }) {
  const session = account?.session
  const token = session?.access_token
  if (!token) return <section className="billing-workspace billing-login"><span className="billing-eyebrow">JOYNIU CAD · ACCOUNT</span><h1>积分与订单</h1><p>登录后查看自己的积分余额、购买记录与售后申请。</p><button className="billing-primary" onClick={onLogin}>登录账号</button></section>
  const capabilities = billingAdminCapabilities(session.user)
  if (admin && !capabilities.read) return <section className="billing-workspace"><h1>商业运营管理</h1><p role="alert">当前账号没有管理权限。</p></section>
  // Account changes clear all records; rotating this account's token preserves
  // checkout intent and idempotency keys while stale token responses are ignored.
  return <BillingSession adminView={adminView} params={params} onNavigate={onNavigate} key={`${billingSessionKey(session, admin)}:${adminView || ""}:${JSON.stringify(params || {})}`} token={token} admin={admin} capabilities={capabilities} onWalletChange={onWalletChange} />
}
