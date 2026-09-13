import { useState } from 'react'
import { dataReceiptOutcomes, dataReceiptPayload, dataRequestScopes } from './supportDataRequest.js'

export function DataRequestFields({ value, onChange }) {
  return <fieldset className="support-data-request"><legend>需要申请删除的数据</legend>
    {Object.entries(dataRequestScopes).map(([scope, name]) => <label className="support-check" key={scope}><input type="checkbox" checked={value.scopes.includes(scope)} onChange={event => onChange({ ...value, scopes: event.target.checked ? [...value.scopes, scope] : value.scopes.filter(item => item !== scope) })} />{name}</label>)}
    <label>数据范围说明<textarea required rows={3} maxLength={2000} value={value.scopeDescription} onChange={event => onChange({ ...value, scopeDescription: event.target.value })} placeholder="填写项目或任务编号、文件名、时间范围；如涉及整个账号请明确说明。" /></label>
    <label className="support-check"><input type="checkbox" checked={value.acknowledged} onChange={event => onChange({ ...value, acknowledged: event.target.checked })} />我知悉这是人工处理申请，提交或关闭工单不会立即删除数据；处理前需核对范围、备份及需保留的记录。</label>
    <p className="support-note">回收站、撤销原图链接与物理删除是不同操作。保存期限以已发布的隐私说明和实际处理回执为准；未公布时由运营先明确告知。请先导出仍需保存的成果。</p>
  </fieldset>
}

export function DataRequestSummary({ ticket }) {
  if (ticket?.category !== 'data_deletion') return null
  return <section className="support-context" aria-label="数据删除申请与回执"><h3>数据删除申请</h3>
    <p>申请范围：{ticket.dataRequest?.scopes.map(scope => dataRequestScopes[scope] || scope).join('、') || '范围信息尚未提供'}</p><p>{ticket.dataRequest?.scopeDescription}</p>
    <p className="support-note">工单状态表示处理进度；删除结果以人工回执中的范围为准。提交、关闭工单或撤回附图授权都不会自动删除数据。如需调整或撤回申请，请追加说明，由运营确认处理进度。</p>
    {!ticket.dataReceipts?.length && <p>尚无人工处理回执，不能据此认定数据已删除。</p>}
    {ticket.dataReceipts?.map(receipt => <article className="support-data-receipt" key={receipt.id}><h4>{dataReceiptOutcomes[receipt.outcome] || receipt.outcome}</h4><small>{new Date(receipt.createdAt).toLocaleString('zh-CN')} · {receipt.authorName} · {receipt.id}</small>
      <dl>{[['实际处理范围', receipt.handledScope], ['仍保留的数据与原因', receipt.retainedScope], ['保留期限与备份安排', receipt.retentionPlan], ['处理记录编号', receipt.evidenceRef]].map(([label, text]) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}</dl>
    </article>)}
  </section>
}

export function DataReceiptForm({ ticket, busy, onSubmit }) {
  const [baseRevision, setBaseRevision] = useState(ticket.revision)
  const [draft, setDraft] = useState({ outcome: 'pending_confirmation', handledScope: '', retainedScope: '', retentionPlan: '', evidenceRef: '', confirmed: false })
  const [error, setError] = useState('')
  const stale = baseRevision !== ticket.revision
  const submit = event => { event.preventDefault(); setError(''); try { if (stale) throw new Error('工单已有新内容，请先核对最新记录。'); onSubmit(dataReceiptPayload({ ...ticket, revision: baseRevision }, draft)) } catch (cause) { setError(cause.message) } }
  return <details className="support-context"><summary>登记人工数据处理回执（客户可见）</summary><form className="support-reply" onSubmit={submit}><fieldset disabled={busy || ticket.status === 'closed'}>
    <p className="support-note">此操作只保存实际处理记录，不执行删除。完成或部分完成时，先核对实际文件、数据库和备份处理证据；未执行请如实选择。本表单不会发送站外通知。</p>
    {stale && <p className="support-error" role="alert">工单已有新内容，当前回执草稿已保留。<button type="button" onClick={() => { setBaseRevision(ticket.revision); setDraft(value => ({ ...value, confirmed: false })); setError('') }}>已核对最新工单，继续编辑回执</button></p>}
    <label>实际处理结果<select value={draft.outcome} onChange={event => setDraft(value => ({ ...value, outcome: event.target.value }))}>{Object.entries(dataReceiptOutcomes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    {[
      ['handledScope', '已处理或待处理范围', '填写实际文件、项目、任务范围和操作；尚未执行请说明待核对内容。', 2000],
      ['retainedScope', '仍保留的数据与原因', '列明账本、审计、工单或备份中仍保留的信息与原因；没有时明确填写“无”。', 2000],
      ['retentionPlan', '保留期限与备份处理安排', '填写已确认期限、到期处理安排与备份覆盖范围；未确定时说明待确认事项，不承诺立即清除所有副本。', 2000],
      ['evidenceRef', '可核对的处理记录编号', '填写内部核验记录或清单编号；不要填写密钥、原图访问链接或服务器路径。', 500],
    ].map(([key, label, placeholder, maximum]) => <label key={key}>{label}<textarea required rows={3} maxLength={maximum} value={draft[key]} placeholder={placeholder} onChange={event => setDraft(value => ({ ...value, [key]: event.target.value }))} /></label>)}
    <label className="support-check"><input type="checkbox" checked={draft.confirmed} onChange={event => setDraft(value => ({ ...value, confirmed: event.target.checked }))} />已核对实际处理情况，确认将上述范围、保留安排和记录编号提供给此客户。</label>
    {error && <p role="alert" className="support-error">{error}</p>}<button disabled={!draft.confirmed || stale} type="submit">{busy ? '正在记录…' : '保存处理回执'}</button>
  </fieldset></form></details>
}
