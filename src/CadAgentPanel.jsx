import { cadGenerationIsCurrent, cadParameterRows, cadProjectionSummary, cadRunPresentation, cadPrimaryAction, cadSourceQuestionReviews, cadTraceMessage } from './cadAgentState.js'
import { cadArtifactUrl } from './cadAgentClient.js'
import './cad-agent.css'

const operationNames = { box: '长方体', cylinder: '圆柱', cone: '圆锥', sphere: '球体', sketch: '草图', extrude: '拉伸', revolve: '旋转', union: '合并', fuse: '合并', cut: '切除', intersect: '交集', fillet: '圆角', chamfer: '倒角', transform: '定位', translate: '平移', rotate: '旋转定位', hole: '打孔' }
const display = (value) => value === null || value === undefined ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const rounded = (value) => Number.isFinite(Number(value)) ? Number(Number(value).toFixed(3)).toString() : '—'
const reviewNames = { consistent: 'AI 已完成投影对照', mismatch: '发现图纸差异', uncertain: '仍有待确认信息', not_applicable: '按文字要求建模', human_confirmed: '已按人工确认的尺寸重建', unverified: '尚未完成图纸对照' }

function CadSourceTranscription({ transcription }) {
  if (!transcription) return null
  const annotations = Array.isArray(transcription.annotations) ? transcription.annotations : []
  const questions = Array.isArray(transcription.questions) ? transcription.questions : []
  const confidenceNames = { high: '高', medium: '中', low: '低', uncertain: '不确定' }
  return <details className="cad-source-transcription">
    <summary>查看原图标注转录（{annotations.length}项）<small>AI 原图转录 · 待核对</small></summary>
    <div className="cad-source-transcription-content">
      <p className="cad-source-note">以下是 AI 从原图读取的候选文字与尺寸基准，尚未作为已确认参数或实测结果。</p>
      {!annotations.length && <p>{transcription.status === 'failed' ? '本次原图转录未完成，没有可核对的标注候选。' : '本次没有可展示的原图标注候选。'}</p>}
      {annotations.map((annotation, index) => <article key={annotation.id || index} className="cad-source-annotation">
        <div className="cad-source-annotation-heading"><b>{annotation.text || '文字未辨清'}</b><span>AI 参考置信度：{confidenceNames[annotation.confidence] || '不确定'}</span></div>
        {(annotation.view || annotation.location) && <p>视图 / 位置：{[annotation.view, annotation.location].filter(Boolean).join(' · ')}</p>}
        {annotation.endpointsOrDatum && <p>尺寸两端 / 基准：{annotation.endpointsOrDatum}</p>}
        {(Array.isArray(annotation.questions) ? annotation.questions : []).map((question, questionIndex) => <p className="cad-source-question" key={questionIndex}>待核对：{question}</p>)}
      </article>)}
      {questions.map((question, index) => <p className="cad-source-question" key={index}>待核对：{question}</p>)}
    </div>
  </details>
}

function CadSourceQuestionReviews({ reviews }) {
  const reports = cadSourceQuestionReviews(reviews)
  if (!reports.length) return null
  const confidenceNames = { high: '高', medium: '中', low: '低', uncertain: '不确定' }
  return <details className="cad-source-transcription" aria-label="提问前原图复读">
    <summary>提问前原图复读（{reports.length} 次）<small>AI 候选依据 · 待核对</small></summary>
    <div className="cad-source-transcription-content">
      <p className="cad-source-note">AI 在提问前重新查看原图得到的候选依据。以下内容不是用户回答，也不是已确认尺寸，仍需建模与实测核对。</p>
      {reports.map((report, reportIndex) => <section key={report.inputFingerprint || reportIndex}>
        <h4>第 {reportIndex + 1} 次复读 · {report.status === 'failed' ? '未完成' : '候选待核对'}</h4>
        {(Array.isArray(report.questions) ? report.questions : []).map((question, index) => {
          const answer = (Array.isArray(report.answers) ? report.answers : []).find((item) => item.questionIndex === index)
          const found = answer?.status === 'answered' && typeof answer.answer === 'string'
          return <article className="cad-source-annotation" key={index}>
            <div className="cad-source-annotation-heading"><b>{question}</b><span>{found ? '找到候选依据' : '仍未确定'}</span></div>
            {found && <p>候选解释：{answer.answer}</p>}
            {answer?.source?.location && <p>原图位置：{answer.source.location}</p>}
            {answer?.source?.evidence && <p>图纸依据：{answer.source.evidence}</p>}
            {answer?.confidence && <small>AI 参考置信度：{confidenceNames[answer.confidence] || '不确定'}</small>}
          </article>
        })}
      </section>)}
    </div>
  </details>
}

function CadProjectionComparison({ comparison, dirty }) {
  const report = cadProjectionSummary(comparison)
  if (!report) return null
  return <section className="cad-acceptance-checks" aria-label="原图轮廓核对">
    <h4>{dirty ? '修改前的轮廓核对' : '原图轮廓核对'}</h4>
    <p>{report.summary}</p>
    {report.rows.map((row, index) => <div className={`cad-acceptance-item ${row.state === 'mismatch' ? 'failed' : ''}`} key={`${row.view}-${index}`}>
      <div><b>{row.name}</b><span>{row.label}</span></div>
      {row.findings.map((finding, findingIndex) => <p key={findingIndex}>{finding}</p>)}
    </div>)}
    <p className="cad-agent-note">{report.note}</p>
  </section>
}

export function CadAgentSummary({ model, busy, onConfirm, onAnswer }) {
  const run = model.agentRun || {}
  const questions = run.questions || []
  const presentation = cadRunPresentation(run, busy)
  const action = cadPrimaryAction(model)
  return <section className={`review-banner cad-agent-summary ${presentation.confirmed ? 'confirmed' : 'needs-review'}`} data-testid="cad-agent-review">
    <div className="review-banner-icon">{presentation.confirmed ? '✓' : busy ? '…' : '?'}</div>
    <div className="review-banner-copy"><b>{presentation.title} · 第 {run.revision || 1} 版</b><span>{presentation.message}</span></div>
    {questions.length > 0 && <div className="cad-agent-questions" role="status">{questions.slice(0, 2).map((question, index) => <p key={index}>{index + 1}. {question}</p>)}{questions.length > 2 && <details><summary>查看其余 {questions.length - 2} 项问题</summary>{questions.slice(2).map((question, index) => <p key={index}>{index + 3}. {question}</p>)}</details>}</div>}
    {(run.status !== 'ready' || run.deliveryBlockedReason) && <button type="button" className="primary-button" disabled={busy} onClick={action.kind === 'answer' ? onAnswer : onConfirm}>{action.label}</button>}
    <CadSourceTranscription transcription={run.sourceTranscription} />
    <CadSourceQuestionReviews reviews={run.sourceQuestionReviews} />
    {!!run.trace?.length && <details className="cad-agent-trace"><summary>查看建模与检查过程（{run.trace.length} 步）</summary><ol>{run.trace.map((item, index) => <li key={index}>{cadTraceMessage(item)}</li>)}</ol></details>}
  </section>
}

export default function CadAgentPanel({ model, tab, busy, onParameterChange, onConfirm, onAsk, generation }) {
  const run = model.agentRun || {}, plan = model.cadPlan
  const presentation = cadRunPresentation(run, busy)
  const action = cadPrimaryAction(model)
  const rows = cadParameterRows(plan), features = plan?.features || []
  const inspection = run.inspection
  if (tab === '特征') return <div className="inspector-content cad-agent-panel"><h3>建模特征 · {features.length}</h3>{features.length ? <ol className="cad-feature-list">{features.map((feature) => <li key={feature.id}><b>{feature.name || operationNames[feature.op] || feature.op}</b><small>{feature.id}</small><button type="button" className="secondary-button" disabled={busy} onClick={() => onAsk(`请修改“${feature.name || feature.id}”这个特征：`)}>通过对话修改</button></li>)}</ol> : <p>结构确认后，建模步骤将显示在这里。</p>}</div>
  if (tab === '检查') {
    const checks = inspection?.checks || inspection?.issues || []
    const entries = Array.isArray(checks) ? checks : Object.entries(checks).map(([name, passed]) => ({ name, passed }))
    const review = run.drawingReview
    const metrics = inspection?.metrics || inspection || {}
    const metricRows = [['实体数量', metrics.solidCount], ['面数量', metrics.faceCount], ['包络尺寸 / mm', metrics.bbox?.size?.map(rounded).join(' × ')], ['体积 / mm³', metrics.volumeMm3 == null ? null : rounded(metrics.volumeMm3)]]
    const acceptance = inspection?.acceptance
    return <div className="inspector-content cad-agent-panel"><h3>{presentation.savedResult ? '本轮处理中 · 已保存的检查' : run.dirty ? '参数已修改，等待重新检查' : inspection ? '实体检查结果' : '尚未生成可检查的实体'}</h3>{presentation.savedResult && <p className="cad-agent-note">{presentation.message}</p>}{inspection && <><div className="cad-check-summary"><b>{inspection.valid === true && inspection.solidCount > 0 ? '实体几何检查通过' : '实体检查尚未通过'}</b><small>第 {run.revision} 版</small></div>{entries.map((check, index) => <div className="check-row" key={index}><span>{check.label || check.name || check.message || check.id}</span><span className={`check-status ${check.passed === true ? 'pass' : 'warn'}`}>{check.passed === true ? '通过' : check.passed === false ? '未通过' : display(check.status || '待核查')}</span></div>)}<dl className="cad-inspection-metrics">{metricRows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{display(value)}</dd></div>)}</dl></>}{acceptance && <section className="cad-acceptance-checks"><h4>图纸与要求的实测核对</h4><p>{({ passed: '已检查的要求均符合当前实体', failed: '存在不符合要求的尺寸或结构', needs_input: '部分要求需要补充依据', not_checked: '尚无可实测的要求' })[acceptance.status] || '待核查'}</p>{(acceptance.checks || []).map((check) => <div key={check.id} className={`cad-acceptance-item ${check.passed === false ? 'failed' : ''}`}><div><b>{check.label || check.id}</b><span>{check.passed === true ? '通过' : check.passed === false ? '不符' : '待确认'}</span></div><small>要求：{display(check.expected)} · 实测：{display(check.actual)}</small>{check.message && <p>{check.message}</p>}</div>)}</section>}<CadProjectionComparison comparison={run.projectionComparison || review?.projectionComparison} dirty={run.dirty} />{review && <section className="cad-drawing-review"><h4>与原图对照</h4><p>{typeof review === 'string' ? review : review.message || review.summary || reviewNames[review.status] || '等待核对'}</p>{(review.observations || []).map((item, index) => <p key={`observation-${index}`}>{item}</p>)}{(review.differences || []).map((item, index) => <p className="parameter-error" key={`difference-${index}`}>{item}</p>)}</section>}<p className="cad-agent-note">实体检查确认建模结果可用；图纸歧义仍需按尺寸来源核对。</p><button className="secondary-button full" disabled={busy} onClick={action.kind === 'retry' || action.kind === 'answer' ? onConfirm : () => onAsk('请检查当前实体与原图的尺寸及结构差异，并修正发现的问题。')}>{action.kind === 'retry' || action.kind === 'answer' ? action.label : '检查并修正'}</button></div>
  }
  return <div className="inspector-content cad-agent-panel"><div className="cad-parameter-heading"><h3>{model.name}</h3><span>{presentation.title}</span></div>{!rows.length && <p>{action.hint || '建模尺寸尚未整理完成，可继续通过对话完善。'}</p>}{rows.map((row) => {
    const source = row.source
    const evidence = typeof source === 'string' ? source : source?.description || source?.text || source?.evidence || source?.view || source?.type
    const derivedValue = !run.dirty && row.expression ? run.resolvedParameters?.[row.key] : null
    return <label className={`cad-parameter ${row.value == null && !row.expression ? 'missing' : ''}`} key={row.key}><span>{row.label}{row.expression && <small>由尺寸关系计算</small>}</span><div><input aria-label={row.label} type="number" step="any" value={row.value ?? derivedValue ?? ''} placeholder={row.expression ? '重建后计算' : '待补全'} disabled={busy || Boolean(row.expression)} onChange={(event) => onParameterChange(row.key, event.target.value)} /><span>{row.unit}</span></div>{(evidence || row.question) && <small>{row.question || display(evidence)}</small>}</label>
  })}{(plan || run.runId) && <button type="button" className="primary-button full" disabled={busy || action.kind === 'ready'} onClick={onConfirm}>{busy ? '处理中…' : action.label}</button>}<p className="cad-agent-note">参数来自本项目的图纸或描述。修改后会重新构建实际实体。</p>{generation?.artifacts?.filter((item) => ['svg', 'png'].includes(item.format)).length > 0 && <button className="secondary-button full" onClick={() => onAsk('请解释当前实体的三视图与原图对应关系。')}>询问视图对应关系</button>}</div>
}

export function CadAgentDrawing({ model, generation, busy = false, onBack, onExport }) {
  const currentGeneration = cadGenerationIsCurrent(model, generation)
  const artifacts = currentGeneration ? (generation.artifacts || []).filter((item) => ['svg', 'png'].includes(item.format)) : []
  const views = { front: '前视图', top: '俯视图', right: '右视图', isometric: '等轴测' }
  const presentation = cadRunPresentation(model.agentRun, busy)
  const title = busy ? artifacts.length ? '本轮处理中 · 上一版本投影' : '本轮处理中 · 等待实体投影' : '实体投影视图'
  const description = busy ? artifacts.length ? presentation.message : '本轮仍在处理，新的实体投影完成后会在此更新。' : '从当前 CAD 实体生成，用于对照原图；修改参数后需重新生成。'
  const canExport = currentGeneration && generation?.validation?.productionReady && presentation.confirmed
  return <section className="secondary-workspace cad-agent-drawing"><div className="secondary-heading"><div><h1>{model.name} · {title}</h1><p role={busy ? 'status' : undefined}>{description}</p></div><button className="secondary-button" onClick={onBack}>返回建模</button></div>{artifacts.length ? <div className="cad-projection-grid">{artifacts.map((artifact, index) => <figure key={`${artifact.view}-${index}`}><img src={cadArtifactUrl(artifact)} alt={views[artifact.view] || artifact.filename || '实体投影视图'} /><figcaption>{busy ? '上一版本 · ' : ''}{views[artifact.view] || artifact.filename || '实体投影视图'}</figcaption></figure>)}</div> : <p role="status">{busy ? '本轮仍在处理，尚无可展示的实体投影视图。' : '当前没有实体投影视图，请完成建模与检查后查看。'}</p>}{canExport && <button className="primary-button" onClick={() => onExport('step')}>导出当前 STEP 实体</button>}</section>
}
