import { cadParameterLabel, cadParameterUnit } from './cadParameterLabels.js'
import { drawingPreprocessFailure } from './drawingPreprocessFailure.js'

export const isFeatureModel = (model) => model?.kind === 'feature_model'
export const shouldUseCadAgent = (model, files = []) => files.length > 0 || !model?.kind || isFeatureModel(model)
export const isLegacyDrawingDraft = (model, drawingJob) => Boolean(!isFeatureModel(model) && drawingJob?.evidence)
export const normalizeCadWorkspaceMode = (mode) => ['图纸转 3D', '图纸转3D'].includes(mode) ? '3D 建模' : mode

export function cadLegacySourceFiles(drawingJob) {
  // Persisted metadata is useful for reselection, but cannot stand in for the
  // original bytes that the independent drawing reader needs.
  return typeof File !== 'undefined' && drawingJob?.file instanceof File ? [drawingJob.file] : []
}

export function cadRetryFiles(turn, model, workspaceId) {
  if (turn.workspaceId && turn.workspaceId !== workspaceId) return null
  if (!turn.cadRun) return turn.files || []
  // A result binds the retry to durable source files and its saved draft.
  // Never reinterpret that retry as a new upload, or replay it onto another run.
  if (turn.cadRun.runId !== model?.agentRun?.runId || turn.cadRun.revision !== model.agentRun.revision) return null
  return []
}

export function cadTerminalResult(result) {
  return result?.status === 'interrupted' ? { ...result, status: 'failed', message: result.message || '后台处理已中断，已保存的图纸与草稿可以继续处理。' } : result
}

// Progress describes the current operation, never a successful model. Source
// transcription and saved plans are candidates until execution and review.
export function cadProgressFromEvent(payload = {}, previous = {}) {
  previous ||= {}
  const stage = payload.stage || previous.stage || 'started'
  const labels = {
    queued: '正在排队', cancel_requested: '正在取消任务', cancelled: '任务已取消',
    started: '正在准备图纸与建模要求', preparing: '正在准备原始图纸',
    source_transcription: '正在读取原图标注', source_transcription_waiting: '正在等待原图转录',
    source_region_transcribed: '已取得部分区域的标注候选 · 待核对',
    source_transcription_complete: '原图转录已返回 · 候选待核对', source_transcription_reused: '已读取原图转录候选 · 待核对',
    source_transcription_incomplete: '原图转录未完成', observe_source: '正在核对尺寸与结构依据',
    source_question_review: '正在回原图核对待询问信息', source_question_resolved: '已找到部分图纸依据，继续建模核对',
    source_spatial: '正在推断原图的空间关系', source_spatial_interpretation: '正在推断原图的空间关系', source_spatial_waiting: '正在核对空间关系候选',
    source_spatial_complete: '空间关系候选已返回 · 尚待建模验证', source_spatial_reused: '已读取空间关系候选 · 尚待建模验证',
    source_spatial_unavailable: '辅助空间分析未完成 · 继续依据原图核对',
    inspect_source: '正在核对原图局部', record_observations: '正在整理尺寸与特征依据',
    observations_recorded: '已记录尺寸依据 · 等待建模检查', observations_rejected: '正在修正尺寸依据',
    planning: '正在规划建模与修正', agent_working: '正在推理 · 尚无新的几何结果',
    provider_retry: '上游响应中断 · 正在继续重试',
    plan_saved: '建模草稿已保存 · 等待执行', execute_plan: '正在构建并测量实体',
    inspect_draft: '正在检查草稿几何 · 尚无交付模型',
    independent_drawing_review: '正在独立比对原图与实体投影',
    projection_compare: '正在跨视图核对实体轮廓', geometry_repair: '正在按发现的差异修正实体',
    inspect_geometry: payload.errors?.length ? '几何执行未通过 · 正在修正' : '正在核对实体与投影',
    needs_input: '等待补充信息', review_required: '等待核对尺寸与建模结果', failed: '本轮未完成，可继续修正',
  }
  const sourceStage = stage.startsWith('source_transcription') || stage.startsWith('source_spatial') || ['source_region_transcribed', 'started', 'preparing', 'observe_source', 'inspect_source', 'record_observations', 'observations_recorded', 'observations_rejected'].includes(stage)
  const savedPhase = ['recognize', 'generate', 'review'].includes(payload.phase) ? payload.phase : null
  const auxiliaryDuringBuild = stage.startsWith('source_spatial') && (payload.geometryGenerated === true || previous.phase === 'generate' || savedPhase === 'generate')
  const phase = stage === 'review_required' ? 'review'
    : auxiliaryDuringBuild ? 'generate' : sourceStage ? 'recognize'
      : ['execute_plan', 'inspect_geometry', 'inspect_draft', 'independent_drawing_review', 'projection_compare', 'geometry_repair'].includes(stage) || payload.geometryGenerated === true ? 'generate'
        : previous.phase || savedPhase || 'recognize'
  return { stage, phase, label: labels[stage] || '正在处理本轮建模', message: payload.message || payload.summary || labels[stage] || '正在处理本轮建模' }
}

export function cadRunPresentation(run = {}, busy = false, plan) {
  const titles = { needs_input: '等待补充信息', review_required: '待确认与交付', ready: '已确认并生成', failed: '本轮未完成', cancelled: '任务已取消' }
  return {
    title: busy ? '本轮仍在处理' : run.deliveryBlockedReason ? '需重新检查' : incompleteDrawingReview(run) ? '图纸复核未完成' : titles[run.status] || '特征建模',
    confirmed: !busy && !run.dirty && !run.deliveryBlockedReason && run.status === 'ready',
    savedResult: busy,
    message: busy
      ? `下方显示已保存的第 ${run.revision || 1} 版内容，本轮尚未完成；新的检查结果返回后会更新。`
      : run.deliveryBlockedReason ? `${run.deliveryBlockedReason} 请重新检查当前模型后再确认交付。`
        : run.dirty ? '参数已修改；先检查并更新预览，再确认交付文件。' : run.status === 'failed' ? cadFailureSummary(run, plan) : run.message,
  }
}

export function cadFailureSummary(run = {}, plan = run.plan) {
  // A fresh upload may fail before replaying an older draft or source reading.
  // Its source error must not be replaced by a stale model/review failure.
  const sourceFailure = drawingPreprocessFailure(run.provider?.lastErrorCode)
  if (sourceFailure) {
    const sourceSaved = Boolean(run.sourceFiles?.length || run.sourceDocuments?.length)
    return `${sourceFailure.message}${sourceSaved ? '原图与处理记录已保留。' : ''}`
  }
  const reviewFailed = incompleteDrawingReview(run)
  const readingFailed = run.sourceTranscription?.status === 'failed'
    || (Array.isArray(run.trace) && run.trace.some((item) => item.action === 'read_source' && item.result?.status === 'failed'))
  const inspection = run.inspection
  const entity = inspection?.valid === true && inspection.kernelBacked === true && inspection.engine === 'cadquery-occt'
    && inspection.solidCount > 0 && Array.isArray(run.artifacts) && run.artifacts.some((item) => String(item?.format).toLowerCase() === 'glb')
  const stage = reviewFailed ? '图纸复核' : !plan && readingFailed ? '原图读取' : entity ? '图纸核对' : plan ? '实体生成' : '建模计划'
  const code = reviewFailed ? run.drawingReview.independentReview.errorCode
    : readingFailed ? run.sourceTranscription?.errorCode || run.provider?.lastErrorCode : run.provider?.lastErrorCode
  const reasons = { timeout: '响应超时', time_limit: '尚未完成，本轮时间已用尽', turn_limit: '尚未完成，已达到本轮处理次数限制',
    transport: '连接中断', invalid_stream: '连接中断', incomplete: '输出未完成', empty_response: '没有返回结果',
    invalid_json: '未返回完整有效的结果', invalid_response: '未返回完整有效的结果', output_limit: '输出超出限制',
    independent_review_failed: '调用未完成', provider_failure: '服务暂未完成处理', upstream_error: '上游服务返回错误' }
  // Preserve a more specific server explanation when no known code identifies the failure.
  if (!reasons[code]) return run.message || `${stage}尚未完成，可继续处理。`
  const measured = (Date.parse(run.completedAt) - Date.parse(run.createdAt)) / 1000
  const elapsed = Number.isFinite(run.elapsedSeconds) && run.elapsedSeconds >= 0 ? run.elapsedSeconds : measured
  const duration = Number.isFinite(elapsed) && elapsed > 0
    ? `，本轮用时${elapsed >= 60 ? `约 ${Math.max(1, Math.round(elapsed / 60))} 分钟` : `约 ${Math.round(elapsed)} 秒`}` : ''
  const sourceSaved = Boolean(run.sourceFiles?.length || run.sourceDocuments?.length || run.sourceTranscription)
  const saved = entity ? '实体草稿与处理记录已保存，完成图纸复核并确认后才能交付。'
    : plan ? '建模草稿与处理记录已保存，可继续处理。'
      : `${sourceSaved ? '原图' : '要求'}与处理记录已保存，尚未生成模型，可继续处理。`
  return `${stage}阶段${reasons[code]}${duration}。${saved}`
}

export function cadSourceQuestionReviews(value) {
  return (Array.isArray(value) ? value : []).slice(-2).filter((report) => report && report.scope === 'source_question_reread'
    && ['succeeded', 'failed'].includes(report.status) && report.candidateEvidence === true && report.verified === false)
    .map((report) => structuredClone(report))
}

export function cadPrimaryAction(model = {}) {
  model ||= {}
  const run = model.agentRun || {}
  if (run.status === 'cancelled') return { kind: 'retry', label: '继续建模', hint: '本次任务已取消，已保存的原图和草稿可用于继续建模。' }
  const questions = (Array.isArray(run.questions) ? run.questions : []).filter((question) => typeof question === 'string' && question.trim())
  if (run.dirty) return { kind: 'confirm', label: '检查修改并更新预览' }
  if (['needs_input', 'failed', 'interrupted'].includes(run.status) && questions.length) {
    return { kind: 'answer', label: '回答问题', hint: `请在对话中回答：${questions[0]}` }
  }
  if (incompleteDrawingReview(run)) return { kind: 'retry', label: '继续图纸复核', hint: '实体草稿已保存。继续核对原图与实体，复核完成并确认前不能交付。' }
  if (['failed', 'interrupted', 'needs_input'].includes(run.status) || (!model.cadPlan && run.runId)) {
    const readingFailed = !model.cadPlan && (run.sourceTranscription?.status === 'failed'
      || run.trace?.some((item) => item.action === 'read_source' && item.result?.status === 'failed'))
    const label = readingFailed ? '重试读取' : '重试建模'
    return { kind: 'retry', label, hint: readingFailed
      ? '原图读取未完成。点击“重试读取”，继续使用本项目已保存的原图。'
      : '本轮建模未完成。点击“重试建模”，继续使用已保存的要求与草稿。' }
  }
  if (!model.cadPlan) return { kind: 'answer', label: '补充建模要求', hint: '请在对话中描述零件的结构与尺寸。' }
  if (run.deliveryBlockedReason) return { kind: 'confirm', label: '重新检查模型' }
  return run.status === 'ready' ? { kind: 'ready', label: '当前版本已确认' } : { kind: 'confirm', label: '确认数据并生成' }
}

export function cadRetryMessage(model) {
  if (cadPrimaryAction(model).label === '继续图纸复核') return '请继续复核当前已保存的原图与实体草稿，完成上一轮未完成的独立图纸复核。保持尺寸和设计要求；若发现实际差异，再说明并修正，不得跳过复核或直接确认交付。'
  const reading = cadPrimaryAction(model).label === '重试读取'
  return `${reading ? '请重试读取当前项目已保存的原图' : '请继续使用当前已保存的建模要求与草稿'}，完成上一轮未完成的建模与检查。这次只重试，不更改尺寸或设计要求；信息确实不明确时请提出具体问题。`
}

export function cadProjectionSummary(comparison) {
  if (!comparison || typeof comparison !== 'object') return null
  const names = { front: '前视图', top: '俯视图', right: '右视图' }
  const rows = (Array.isArray(comparison.views) ? comparison.views : []).filter((view) => view && names[view.view]).map((view) => {
    const state = view.status === 'mismatch' && view.reliability === 'high' ? 'mismatch' : view.status === 'supported' ? 'supported' : 'uncertain'
    const findings = state === 'mismatch' && Array.isArray(view.differenceRegions)
      ? view.differenceRegions.map((region) => region?.finding).filter((finding) => typeof finding === 'string' && finding.trim()).slice(0, 2).map((finding) => finding.slice(0, 240)) : []
    return { view: view.view, name: names[view.view], state, label: state === 'mismatch' ? '明确轮廓差异' : state === 'supported' ? '所测轮廓未见差异' : '核对范围不足', findings }
  })
  const mismatchCount = rows.filter((row) => row.state === 'mismatch').length
  const supportedCount = rows.filter((row) => row.state === 'supported').length
  const uncertainCount = rows.length - mismatchCount - supportedCount
  const summary = mismatchCount ? `${mismatchCount} 个视图发现明确轮廓差异`
    : supportedCount ? `${supportedCount} 个视图所测轮廓未见差异${uncertainCount ? `；${uncertainCount} 个视图核对范围不足` : ''}`
      : '轮廓核对范围不足'
  return { summary, rows, mismatchCount, supportedCount, uncertainCount, note: '仅核对已定位的轮廓，不代表整张图纸的尺寸与特征全部一致。' }
}

export function cadWorkflowSnapshot({ model, progress, busy, error }) {
  if (busy) return { current: progress?.phase || 'recognize', label: progress?.label || '正在准备建模要求' }
  const run = model?.agentRun || {}
  if (run.status === 'cancelled') return { current: model?.cadPlan ? 'generate' : 'recognize', label: '任务已取消，可继续建模' }
  if (run.status === 'failed' || error) return { current: progress?.phase === 'generate' || model?.cadPlan ? 'generate' : 'recognize', label: `本轮未完成，可${cadPrimaryAction(model).kind === 'retry' ? cadPrimaryAction(model).label : '继续处理'}` }
  if (!run.status) return { current: progress?.phase || 'recognize', label: '本轮未完成，可重新发送' }
  if (run.deliveryBlockedReason) return { current: 'generate', label: '当前模型需重新检查' }
  if (run.dirty) return { current: 'generate', label: '修改待检查' }
  if (run.status === 'needs_input') return { current: 'recognize', label: '等待补充信息' }
  if (run.status === 'ready') return { current: 'edit', label: '实体已生成，可继续修改' }
  return { current: 'review', label: '确认尺寸与建模结果' }
}

export function cadTraceMessage(item) {
  if (typeof item === 'string') return item
  const action = item.action || item.stage
  const failed = item.result?.status === 'failed' || item.result?.errors?.length > 0
  if (action === 'read_source') return failed ? '原图转录未完成' : '原图转录候选已返回 · 待核对'
  if (action === 'source_question_review') return item.status === 'failed' || failed ? '提问前原图复读未完成' : '提问前原图复读候选已返回 · 待核对'
  if (action === 'interpret_source_spatial') return failed ? '空间关系推断未完成' : '空间关系候选已记录 · 尚待实体核对'
  if (action === 'projection_compare') {
    const comparison = cadProjectionSummary(item.result)
    return comparison?.mismatchCount ? `${comparison.summary} · 待修正`
      : comparison?.supportedCount ? `${comparison.summary} · 仍需核对尺寸与未测特征`
        : '轮廓核对范围不足 · 未形成明确差异结论'
  }
  if (action === 'edit_plan') return failed ? '建模草稿修改未通过' : '建模草稿已保存 · 尚待执行'
  if (action === 'record_observations') return failed ? '尺寸与特征依据未通过检查' : '已记录尺寸与特征依据 · 尚非实测结果'
  if (action === 'independent_drawing_review') return failed || ['mismatch', 'uncertain'].includes(item.result?.status) ? '独立图纸复核未通过 · 待修正' : '独立比对原图与实体投影'
  const names = { inspect_source: '核对原图局部', execute_plan: failed ? '实体构建未通过' : '构建并测量实体', inspect_geometry: '检查实体与投影', ask_user: '请求补充信息', finish: failed ? '建模结果尚未通过核对' : '核对建模结果', human_confirmation: '确认当前尺寸与实体', provider_error: '建模服务未完成' }
  return names[action] || item.message || item.summary || '建模检查步骤'
}

export function cadExplicitMaterial(model) {
  if (!isFeatureModel(model)) return ''
  const value = model.cadPlan?.material || model.agentRun?.material || (model.materialSource === 'explicit' ? model.material : '')
  return typeof value === 'string' ? value.trim() : ''
}

export function cadParameterRows(plan) {
  const parameters = plan?.parameters || {}
  return (Array.isArray(parameters) ? parameters.map((item) => [item.id || item.name || item.key, item]) : Object.entries(parameters))
    .filter(([key]) => Boolean(key))
    .map(([key, item], index) => {
      const row = item !== null && typeof item === 'object' ? item : { value: item }
      return { ...row, key, label: cadParameterLabel(key, row, index), unit: cadParameterUnit(key, row, plan?.units || 'mm') }
    })
}

export function cadParameterValues(plan) {
  return Object.fromEntries(cadParameterRows(plan).map((row) => [row.key, row.value]))
}

export function cadConfirmationParameters(plan) {
  return Object.fromEntries(cadParameterRows(plan).filter((row) => !row.expression).map((row) => [row.key, row.value]))
}

export function cadParameterEditMessage(model) {
  const baseline = model.agentRun?.parameterBaseline || model.agentRun?.resolvedParameters || {}
  const changes = cadParameterRows(model.cadPlan).filter((row) => !row.expression && row.value !== baseline[row.key])
    .map((row) => `“${row.label}”从${baseline[row.key] == null ? '未确定' : baseline[row.key]}改为${row.value == null ? '待确定' : row.value}`)
  return changes.length
    ? `我在参数面板将${changes.join('；')}。保持其他尺寸与结构，请按修改后的设计重新建模和检查，并更新预览。`
    : '请按当前保存的尺寸与结构重新建模和检查，并更新预览。'
}

// The server restores evidence and tool history by revision. Replaying the
// complete public trace duplicates that history and eventually exceeds the
// request limit, even when the user only changes one dimension.
export function cadRequestState(model = {}) {
  return {
    kind: model.kind || '', name: model.name, material: cadExplicitMaterial(model), units: model.units,
    ...(model.cadPlan ? { cadPlan: model.cadPlan } : {}),
    ...(model.agentRun?.runId ? { agentRun: { runId: model.agentRun.runId, revision: model.agentRun.revision } } : {}),
  }
}

export function editCadParameter(model, key, value) {
  const plan = structuredClone(model.cadPlan || {})
  const parameters = plan.parameters || {}
  const nextValue = value === '' ? null : Number(value)
  const manualSource = (source) => ({ ...(source && typeof source === 'object' ? source : typeof source === 'string' ? { text: source } : {}),
    type: 'manual', ...(source ? { drawingSource: source.drawingSource || source } : {}) })
  if (Array.isArray(parameters)) {
    plan.parameters = parameters.map((item) => (item.id || item.name || item.key) === key ? { ...item, value: nextValue, source: manualSource(item.source) } : item)
  } else {
    const previous = parameters[key]
    plan.parameters = { ...parameters, [key]: { ...(previous !== null && typeof previous === 'object' ? previous : {}), value: nextValue, source: manualSource(previous?.source) } }
  }
  return { ...model, cadPlan: plan, updatedAt: '刚刚', agentRun: { ...model.agentRun, status: 'review_required', dirty: true, artifacts: [], inspection: null } }
}

export function cadPlanSignature(plan) {
  // Object order is not part of geometry identity. Array order is feature order.
  const ordered = (value) => Array.isArray(value) ? value.map(ordered) : value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, ordered(value[key])])) : value
  return JSON.stringify(ordered(plan || null))
}

export function cadGenerationIsCurrent(model, generation) {
  return Boolean(isFeatureModel(model) && generation && !generation.stale && !model.agentRun?.dirty
    && generation.kind === 'feature_model' && generation.runId === model.agentRun?.runId
    && generation.revision === model.agentRun?.revision && generation.planSignature === cadPlanSignature(model.cadPlan))
}

export function cadModelFromResult(result, previous = {}) {
  const plan = Object.hasOwn(result, 'plan') ? result.plan : previous.cadPlan || null
  const { plan: _plan, ...run } = result
  const material = String(plan?.material || result.material || cadExplicitMaterial(previous) || '').trim()
  return {
    kind: 'feature_model', name: plan?.name || result.name || previous.name || '新建零件', material, materialSource: material ? 'explicit' : 'unspecified',
    units: plan?.units || 'mm', updatedAt: '刚刚', cadPlan: plan,
    agentRun: { ...run, sourceQuestionReviews: cadSourceQuestionReviews(run.sourceQuestionReviews), parameterBaseline: cadParameterValues(plan), dirty: false },
  }
}

export function cadGenerationFromResult(result, plan) {
  if (!result) return null
  const inspection = result.inspection || {}
  const validDraft = validateCadPlan({ kind: 'feature_model', cadPlan: plan }).valid
    && inspection.valid === true && inspection.kernelBacked === true
    && inspection.engine === 'cadquery-occt' && Number.isInteger(inspection.solidCount) && inspection.solidCount > 0
  const inspectableQuestion = result.status === 'needs_input' && validDraft
  const reviewIncomplete = incompleteDrawingReview(result) && validDraft
    && (!Object.hasOwn(result, 'plan') || cadPlanSignature(result.plan) === cadPlanSignature(plan))
    && (!result.parameterBaseline || cadPlanSignature(result.parameterBaseline) === cadPlanSignature(cadParameterValues(plan)))
  if (!['review_required', 'ready'].includes(result.status) && !inspectableQuestion && !reviewIncomplete) return null
  const artifacts = (Array.isArray(result.artifacts) ? result.artifacts : []).filter(Boolean)
    .map((item) => ({ ...item, format: String(item.format || '').toLowerCase(), downloadUrl: item.url || item.downloadUrl || item.download_url }))
    .filter((item) => (!(result.deliveryBlockedReason || reviewIncomplete) || item.format !== 'step')
      && (!reviewIncomplete || currentRunArtifact(item, result)))
  const glb = artifacts.find((item) => item.format === 'glb')
  if (!glb) return null
  if ((inspectableQuestion || reviewIncomplete) && !currentRunArtifact(glb, result)) return null
  return {
    kind: 'feature_model', requestId: result.runId, runId: result.runId, revision: result.revision, planSignature: cadPlanSignature(plan),
    engine: 'CadQuery / OCCT', parameters: cadParameterValues(plan), artifacts, stale: false, previewOnly: result.status !== 'ready' || Boolean(result.deliveryBlockedReason),
    ...(reviewIncomplete ? { reviewIncomplete: true } : {}),
    validation: { productionReady: result.status === 'ready' && !result.deliveryBlockedReason, valid: inspection.valid ?? inspection.passed ?? false, metrics: inspection.metrics || inspection },
  }
}

function drawingMismatch(report) {
  return Boolean(report && (report.status === 'mismatch' || report.differences?.length
    || (Array.isArray(report.views) && report.views.some(drawingMismatch))))
}

function incompleteDrawingReview(run) {
  const review = run?.drawingReview, independent = review?.independentReview
  return Boolean(run?.status === 'failed' && !run.dirty && !run.stale
    && review?.status === 'uncertain' && review.source === 'independent_drawing_review' && review.humanConfirmed === false
    && independent?.status === 'uncertain' && independent.source === 'independent_drawing_review'
    && typeof independent.errorCode === 'string' && independent.errorCode.trim()
    && typeof review.planHash === 'string' && review.planHash && review.planHash === independent.planHash
    && ![review, independent, run.projectionComparison, review.projectionComparison, independent.projectionComparison].some(drawingMismatch))
}

function currentRunArtifact(artifact, run) {
  if (typeof run.runId !== 'string' || !run.runId || !Number.isInteger(run.revision) || run.revision < 1) return false
  try {
    const url = new URL(artifact.downloadUrl, 'http://cad-artifact.invalid')
    if (!['http:', 'https:'].includes(url.protocol)) return false
    const marker = `/cad-agent/runs/${encodeURIComponent(run.runId)}/${run.revision}/artifacts/`
    const index = url.pathname.indexOf(marker)
    if (index < 0) return false
    const key = url.pathname.slice(index + marker.length)
    return /^[A-Za-z0-9_-]+$/.test(key) && (artifact.format !== 'glb' || key === 'glb')
  } catch { return false }
}

export function validateCadPlan(model) {
  const plan = model?.cadPlan
  const errors = []
  if (!plan) errors.push({ field: 'plan', message: cadPrimaryAction(model).hint || '建模尚未完成，请继续建模并检查。' })
  else if (!plan.features?.length || !plan.result) errors.push({ field: 'features', message: '建模结构尚未完整，请继续说明零件的结构。' })
  for (const row of cadParameterRows(plan)) {
    if (row.expression) continue
    if (row.value === null || row.value === undefined || row.value === '' || typeof row.value === 'boolean' || !Number.isFinite(Number(row.value))) errors.push({ field: row.key, message: `请补全${row.label}。` })
  }
  return { valid: errors.length === 0, errors, unsupported: false, kind: 'feature_model' }
}
