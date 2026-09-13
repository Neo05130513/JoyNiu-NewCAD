import { useEffect, useRef, useState } from 'react'
import { cadGenerationIsCurrent, cadParameterRows, cadProjectionSummary, cadRunPresentation, cadPrimaryAction, cadSourceQuestionReviews, cadTraceMessage } from './cadAgentState.js'
import { cadAgent, cadArtifactUrl } from './cadAgentClient.js'
import { refreshProjectionFile } from './sourceFileAccess.js'
import { downloadBlob, readableError } from './workspaceFeedback.js'
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
  const attention = report.rows.filter((row) => row.state !== 'supported')
  const supported = report.rows.filter((row) => row.state === 'supported')
  const renderRow = (row) => <div className={`cad-acceptance-item ${row.state === 'mismatch' ? 'failed' : ''}`} key={row.view}>
    <div><b>{row.name}</b><span>{row.label}</span></div>
    {row.findings.map((finding, index) => <p key={index}>{finding}</p>)}
  </div>
  if (!dirty && !attention.length && supported.length > 0) return <details className="cad-check-details" aria-label="原图轮廓核对">
    <summary>轮廓核对 · {report.summary}</summary>
    {supported.map(renderRow)}
    <p className="cad-agent-note">{report.note}</p>
  </details>
  return <section className="cad-acceptance-checks" aria-label="原图轮廓核对">
    <h4>{dirty ? '修改前的轮廓核对' : '原图轮廓核对'}</h4>
    <p>{report.summary}</p>
    {attention.map(renderRow)}
    {supported.length > 0 && <details className="cad-check-details"><summary>查看 {supported.length} 个已测视图</summary>{supported.map(renderRow)}</details>}
    <p className="cad-agent-note">{report.note}</p>
  </section>
}

export function CadAgentSummary({ model, busy, onConfirm, onAnswer, compact = false, showAction = true }) {
  const run = model.agentRun || {}
  const questions = (Array.isArray(run.questions) ? run.questions : []).filter((question) => typeof question === 'string' && question.trim())
  const presentation = cadRunPresentation(run, busy, model.cadPlan)
  const action = cadPrimaryAction(model)
  const hasRecords = Boolean(run.sourceTranscription || cadSourceQuestionReviews(run.sourceQuestionReviews).length || run.trace?.length)
  const quietSuccess = compact && presentation.confirmed && !questions.length
  return <section className={`review-banner cad-agent-summary ${compact ? 'cad-agent-summary-compact' : ''} ${presentation.confirmed ? 'confirmed' : 'needs-review'}`} data-testid="cad-agent-review">
    <div className="review-banner-icon">{presentation.confirmed ? '✓' : busy ? '…' : '?'}</div>
    <div className="review-banner-copy"><b>{presentation.title}<small className="cad-summary-revision">第 {run.revision || 1} 版</small></b>{!quietSuccess && <span>{presentation.message}</span>}</div>
    {questions.length > 0 && <div className="cad-agent-questions" role="status">{busy && <small>已保存版本中的问题，本轮完成后更新</small>}{questions.slice(0, 2).map((question, index) => <p key={index}>{index + 1}. {question}</p>)}{questions.length > 2 && <details><summary>查看其余 {questions.length - 2} 项问题</summary>{questions.slice(2).map((question, index) => <p key={index}>{index + 3}. {question}</p>)}</details>}</div>}
    {showAction && (run.status !== 'ready' || run.dirty || run.deliveryBlockedReason) && <button type="button" className="primary-button" disabled={busy} onClick={action.kind === 'answer' ? onAnswer : onConfirm}>{action.label}</button>}
    {(hasRecords || quietSuccess && presentation.message) && <details className="cad-run-details" key={`${run.runId || ''}:${run.revision || 1}`}>
      <summary>{compact ? '版本与检查记录' : '查看识别与检查记录'}</summary>
      <div className="cad-run-details-content">
        {quietSuccess && presentation.message && <p className="cad-agent-note">{presentation.message}</p>}
        {hasRecords && <p className="cad-agent-note">以下记录属于已保存的第 {run.revision || 1} 版，用于追溯本次处理过程。</p>}
        <CadSourceTranscription transcription={run.sourceTranscription} />
        <CadSourceQuestionReviews reviews={run.sourceQuestionReviews} />
        {!!run.trace?.length && <details className="cad-agent-trace"><summary>查看建模与检查过程（{run.trace.length} 步）</summary><ol>{run.trace.map((item, index) => <li key={index}>{cadTraceMessage(item)}</li>)}</ol></details>}
      </div>
    </details>}
  </section>
}

function CadParameterField({ row, run, busy, onParameterChange }) {
  const source = row.source?.drawingSource || row.source
  const evidence = typeof source === 'string' ? source : source?.description || source?.text || source?.evidence || source?.view || source?.type
  const derivedValue = !run.dirty && row.expression ? run.resolvedParameters?.[row.key] : null
  return <div className={`cad-parameter ${row.value == null && !row.expression ? 'missing' : ''}`}>
    <label><span>{row.label}{row.expression && <small>由尺寸关系计算</small>}</span>
      <div><input aria-label={row.label} type="number" step="any" value={row.value ?? derivedValue ?? ''} placeholder={row.expression ? '重建后计算' : '待补全'} disabled={busy || Boolean(row.expression)} onChange={(event) => onParameterChange(row.key, event.target.value)} /><span>{row.unit}</span></div>
    </label>
    {row.question && <small className="cad-source-question">{row.question}</small>}
    {evidence && <details className="cad-parameter-source"><summary>{row.source?.drawingSource ? '人工修改 · 查看原图依据' : '尺寸来源'}</summary><small>{display(evidence)}</small></details>}
  </div>
}

export default function CadAgentPanel({ model, tab, busy, onParameterChange, onConfirm, onAsk, generation }) {
  const run = model.agentRun || {}, plan = model.cadPlan
  const presentation = cadRunPresentation(run, busy, model.cadPlan)
  const action = cadPrimaryAction(model)
  const rows = cadParameterRows(plan), features = plan?.features || []
  const inspection = run.inspection
  if (tab === '特征') return <div className="inspector-content cad-agent-panel">
    <h3>建模特征 · {features.length}</h3>
    {features.length ? <ol className="cad-feature-list">{features.map((feature) => <li key={feature.id}><details>
      <summary>{feature.name || operationNames[feature.op] || feature.op}</summary>
      <small>特征编号：{feature.id}</small>
      <button type="button" className="secondary-button" disabled={busy} onClick={() => onAsk(`请修改“${feature.name || feature.id}”这个特征：`)}>通过对话修改</button>
    </details></li>)}</ol> : <p>结构确认后，建模步骤将显示在这里。</p>}
  </div>
  if (tab === '检查') {
    const checks = inspection?.checks || inspection?.issues || []
    const entries = Array.isArray(checks) ? checks : Object.entries(checks).map(([name, passed]) => ({ name, passed }))
    const review = run.drawingReview
    const metrics = inspection?.metrics || inspection || {}
    const metricRows = [['实体数量', metrics.solidCount], ['面数量', metrics.faceCount], ['包络尺寸 / mm', metrics.bbox?.size?.map(rounded).join(' × ')], ['体积 / mm³', metrics.volumeMm3 == null ? null : rounded(metrics.volumeMm3)]]
    const acceptance = inspection?.acceptance
    const passed = entries.filter((check) => check.passed === true)
    const attention = entries.filter((check) => check.passed !== true)
    const requirements = Array.isArray(acceptance?.checks) ? acceptance.checks : []
    const passedRequirements = requirements.filter((check) => check.passed === true)
    const reviewDifferences = Array.isArray(review?.differences) ? review.differences : []
    const reviewObservations = Array.isArray(review?.observations) ? review.observations : []
    const reviewMessage = typeof review === 'string' ? review : review?.message || review?.summary || reviewNames[review?.status] || '等待核对'
    const quietReview = ['consistent', 'human_confirmed', 'not_applicable'].includes(review?.status)
      && !reviewDifferences.length && review?.independentReview?.status !== 'failed'
    const checkRow = (check, index) => <div className="check-row" key={check.id || index}><span>{check.label || check.name || check.message || check.id}</span><span className={`check-status ${check.passed === true ? 'pass' : 'warn'}`}>{check.passed === true ? '通过' : check.passed === false ? '未通过' : display(check.status || '待核查')}</span></div>
    const requirementRow = (check, index) => <div key={check.id || index} className={`cad-acceptance-item ${check.passed === false ? 'failed' : ''}`}><div><b>{check.label || check.id}</b><span>{check.passed === true ? '通过' : check.passed === false ? '不符' : '待确认'}</span></div><small>要求：{display(check.expected)} · 实测：{display(check.actual)}</small>{check.message && <p>{check.message}</p>}</div>
    return <div className="inspector-content cad-agent-panel">
      {(presentation.savedResult || run.dirty || !inspection) && <h3>{presentation.savedResult ? '本轮处理中 · 已保存的检查' : run.dirty ? '参数已修改，等待重新检查' : '尚未生成可检查的实体'}</h3>}
      {presentation.savedResult && <p className="cad-agent-note">{presentation.message}</p>}
      {inspection && <>
        <div className="cad-check-summary"><b>{inspection.valid === true && inspection.solidCount > 0 ? '实体几何检查通过' : '实体检查尚未通过'}</b><small>第 {run.revision} 版</small></div>
        {attention.map(checkRow)}
        <details className="cad-check-details" key={`geometry:${run.runId}:${run.revision}`}>
          <summary>实体数据{passed.length > 0 ? ` · ${passed.length} 项检查通过` : ''}</summary>
          {passed.map(checkRow)}
          <dl className="cad-inspection-metrics">{metricRows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{display(value)}</dd></div>)}</dl>
        </details>
      </>}
      {acceptance && (acceptance.status === 'passed' && passedRequirements.length > 0 && passedRequirements.length === requirements.length
        ? <details className="cad-check-details" key={`requirements:${run.runId}:${run.revision}`}><summary>尺寸实测 · {passedRequirements.length} 项通过</summary>{passedRequirements.map(requirementRow)}</details>
        : <section className="cad-acceptance-checks">
        <h4>尺寸与结构</h4>
        <p>{({ passed: '已检查的要求均符合当前实体', failed: '存在不符合要求的尺寸或结构', needs_input: '部分要求需要补充依据', not_checked: '尚无可实测的要求' })[acceptance.status] || '待核查'}</p>
        {requirements.filter((check) => check.passed !== true).map(requirementRow)}
        {passedRequirements.length > 0 && <details className="cad-check-details" key={`requirements:${run.runId}:${run.revision}`}><summary>查看 {passedRequirements.length} 项通过的实测记录</summary>{passedRequirements.map(requirementRow)}</details>}
      </section>)}
      <CadProjectionComparison comparison={run.projectionComparison || review?.projectionComparison} dirty={run.dirty} />
      {review && <section className="cad-drawing-review">
        {quietReview ? <details className="cad-check-details" key={`review:${run.runId}:${run.revision}`}><summary>原图对照记录</summary><p>{reviewMessage}</p>{reviewObservations.map((item, index) => <p key={index}>{item}</p>)}</details>
          : <><h4>与原图对照</h4><p>{reviewMessage}</p>{reviewDifferences.map((item, index) => <p className="parameter-error" key={index}>{item}</p>)}{reviewObservations.length > 0 && <details className="cad-check-details"><summary>查看对照依据</summary>{reviewObservations.map((item, index) => <p key={index}>{item}</p>)}</details>}</>}
      </section>}
      <p className="cad-agent-note">实体检查确认建模结果可用；图纸歧义仍需按尺寸来源核对。</p>
      <button className="secondary-button full" disabled={busy} onClick={action.kind === 'retry' || action.kind === 'answer' ? onConfirm : () => onAsk('请检查当前实体与原图的尺寸及结构差异，并修正发现的问题。')}>{action.kind === 'retry' || action.kind === 'answer' ? action.label : '检查并修正'}</button>
    </div>
  }
  return <div className="inspector-content cad-agent-panel"><div className="cad-parameter-heading"><h3>可编辑尺寸</h3><span>{presentation.title}</span></div>{!rows.length && <p>{action.hint || '建模尺寸尚未整理完成，可继续通过对话完善。'}</p>}<div className="cad-parameter-grid">{rows.map((row) => <CadParameterField key={row.key} row={row} run={run} busy={busy} onParameterChange={onParameterChange} />)}</div>{(plan || run.runId) && action.kind !== 'ready' && <button type="button" className="primary-button full" disabled={busy} onClick={onConfirm}>{busy ? '处理中…' : action.label}</button>}<p className="cad-agent-note">修改尺寸后，需要检查并更新实体。</p>{generation?.artifacts?.filter((item) => ['svg', 'png'].includes(item.format)).length > 0 && <details className="cad-check-details"><summary>视图说明</summary><button className="secondary-button full" disabled={busy} onClick={() => onAsk('请解释当前实体的三视图与原图对应关系。')}>询问视图对应关系</button></details>}</div>
}

function CadProjectionFigure({ artifact, label, runId, revision, token }) {
  const [attempt, setAttempt] = useState(0)
  const [zoom, setZoom] = useState(1)
  const [failed, setFailed] = useState(false)
  const [loading, setLoading] = useState(true)
  const [downloading, setDownloading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [renewal, setRenewal] = useState(null)
  const [feedback, setFeedback] = useState('')
  const downloadRef = useRef(null)
  const automaticRenewal = useRef(false)
  const incomingUrl = cadArtifactUrl(artifact)
  const url = renewal?.baseUrl === incomingUrl ? cadArtifactUrl(renewal.artifact) : incomingUrl
  useEffect(() => {
    setDownloading(false); setRefreshing(false); automaticRenewal.current = false
    return () => { downloadRef.current?.abort(); downloadRef.current = null }
  }, [runId, revision, artifact.id, token])
  useEffect(() => { setFailed(false); setLoading(true); setFeedback('') }, [url])
  const renew = async () => {
    if (downloadRef.current) return
    const controller = new AbortController()
    downloadRef.current = controller
    setRefreshing(true); setFeedback('')
    try {
      const updated = await refreshProjectionFile({ runId, revision, artifactId: artifact.id, token, signal: controller.signal })
      if (downloadRef.current !== controller || controller.signal.aborted) return
      setRenewal({ baseUrl: incomingUrl, artifact: updated })
      setFailed(false); setLoading(true); setAttempt(value => value + 1)
    } catch (error) { if (downloadRef.current === controller && !controller.signal.aborted) setFeedback(readableError(error)) }
    finally { if (downloadRef.current === controller) { downloadRef.current = null; setRefreshing(false) } }
  }
  const download = async () => {
    if (downloadRef.current) return
    const controller = new AbortController()
    downloadRef.current = controller
    const timer = setTimeout(() => controller.abort(), 45_000)
    setDownloading(true); setFeedback('')
    try {
      const { blob, mimeType, artifact: downloaded } = await cadAgent.downloadArtifact({ token, signal: controller.signal, runId, revision, artifactId: artifact.id })
      if (downloadRef.current !== controller || controller.signal.aborted) return
      downloadBlob(blob, downloaded.filename || `实体投影.${downloaded.format}`, mimeType || 'application/octet-stream')
      setFeedback('投影图已准备下载')
    } catch (error) { if (downloadRef.current === controller) setFeedback(error.name === 'AbortError' ? '下载超时，请重试。' : readableError(error)) }
    finally { clearTimeout(timer); if (downloadRef.current === controller) { downloadRef.current = null; setDownloading(false) } }
  }
  return <figure className="cad-projection-figure">
    <figcaption>{label}</figcaption>
    <div className="cad-projection-actions"><button disabled={zoom <= 1} aria-label={`缩小${label}`} onClick={() => setZoom((value) => Math.max(1, value - .5))}>−</button><span>{Math.round(zoom * 100)}%</span><button disabled={zoom >= 4} aria-label={`放大${label}`} onClick={() => setZoom((value) => Math.min(4, value + .5))}>＋</button><button onClick={() => setZoom(1)}>适应图纸</button><button disabled={downloading || refreshing} onClick={download}>{downloading ? '正在下载…' : '下载投影图'}</button></div>
    {failed ? <div className="cad-projection-error" role="alert"><p>投影图暂时无法加载，已保留模型与参数。</p><button className="secondary-button" disabled={refreshing || downloading} onClick={renew}>{refreshing ? '正在加载…' : '重新加载投影图'}</button></div> : <div className="cad-projection-scroll" tabIndex={0} aria-label={`${label}，放大后可滚动查看`}>
      {loading && <span role="status">正在加载投影图…</span>}
      <img key={`${url}:${attempt}`} src={attempt ? `${url}${url.includes('?') ? '&' : '?'}previewRetry=${attempt}` : url} style={{ width: `${zoom * 100}%`, maxWidth: 'none' }} alt={label} onLoad={() => setLoading(false)} onError={() => {
        setLoading(false); setFailed(true)
        if (!automaticRenewal.current && !downloadRef.current) { automaticRenewal.current = true; renew() }
      }} />
    </div>}
    {feedback && <p role="status">{feedback}</p>}
  </figure>
}

export function CadAgentDrawing({ model, generation, busy = false, onBack, onExport, token }) {
  const currentGeneration = cadGenerationIsCurrent(model, generation)
  const artifacts = currentGeneration ? (generation.artifacts || []).filter((item) => ['svg', 'png'].includes(item.format)) : []
  const views = { front: '前视图', top: '俯视图', right: '右视图', isometric: '等轴测' }
  const presentation = cadRunPresentation(model.agentRun, busy, model.cadPlan)
  const reviewIncomplete = currentGeneration && generation?.reviewIncomplete
  const title = busy ? artifacts.length ? '本轮处理中 · 上一版本投影' : '本轮处理中 · 等待实体投影' : reviewIncomplete ? '实体草稿 · 图纸复核未完成' : '实体投影视图'
  const description = busy ? artifacts.length ? presentation.message : '本轮仍在处理，新的实体投影完成后会在此更新。' : reviewIncomplete ? '当前投影来自已生成的实体草稿；图纸复核未完成，完成复核并确认后才能交付。' : '从当前 CAD 实体生成，用于对照原图；修改参数后需重新生成。'
  const canExport = currentGeneration && generation?.validation?.productionReady && presentation.confirmed
  return <section className="secondary-workspace cad-agent-drawing"><div className="secondary-heading"><div><h1>{model.name} · {title}</h1><p role={busy ? 'status' : undefined}>{description}</p></div><button className="secondary-button" onClick={onBack}>返回建模</button></div>{artifacts.length ? <div className="cad-projection-grid">{artifacts.map((artifact, index) => <CadProjectionFigure key={`${generation.runId}:${generation.revision}:${artifact.id || index}`} artifact={artifact} runId={generation.runId} revision={generation.revision} token={token} label={`${busy ? '上一版本 · ' : ''}${views[artifact.view] || artifact.filename || '实体投影视图'}`} />)}</div> : <p role="status">{busy ? '本轮仍在处理，尚无可展示的实体投影视图。' : '当前没有实体投影视图，请完成建模与检查后查看。'}</p>}{canExport && <button className="primary-button" disabled={busy} onClick={() => onExport('step')}>导出当前 STEP 实体</button>}</section>
}
