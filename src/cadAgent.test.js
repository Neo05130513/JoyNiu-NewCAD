import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { cadAgent, cadArtifactUrl, readCadAgentEvents } from './cadAgentClient.js'
import { configuredAiProvider, workspaceAiProvider, aiProviderPresentation } from './aiProviderState.js'
import { cadModelFromResult, cadGenerationFromResult, cadGenerationIsCurrent, cadParameterValues, cadPlanSignature, cadConfirmationParameters, cadParameterEditMessage, cadRequestState, cadExplicitMaterial, cadProgressFromEvent, cadWorkflowSnapshot, cadPrimaryAction, cadRetryMessage, cadRunPresentation, cadProjectionSummary, cadTraceMessage, cadRetryFiles, cadTerminalResult, editCadParameter, shouldUseCadAgent, isLegacyDrawingDraft, normalizeCadWorkspaceMode, cadLegacySourceFiles, validateCadPlan } from './cadAgentState.js'
import * as ProjectStore from './projectStore.js'
import { generationMatchesModel } from './viewerState.js'
import { validateModelParameters } from './modelValidation.js'
import { recoverWorkspaceSnapshot, sanitizeWorkspaceSnapshot } from './projectStore.js'

const plan = {
  version: 'cad-plan-v1', name: '双孔板', units: 'mm',
  parameters: { width: { label: '板宽', value: 60, source: { view: 'top', text: '60' } }, thickness: { label: '厚度', value: null, question: '厚度是多少？' }, halfWidth: { value: null, expression: 'width / 2' } },
  features: [{ id: 'plate', op: 'box', size: ['width', 40, 'thickness'] }], result: 'plate',
}
const result = (overrides = {}) => ({ runId: 'cad_test', revision: 1, status: 'review_required', plan: { ...plan, parameters: { ...plan.parameters, thickness: { value: 9 } } }, message: '请核对当前模型', questions: [],
  inspection: { valid: true, solidCount: 1, bbox: { size: [60, 40, 9] }, drawingAgreement: 'not_checked', productionReady: false },
  artifacts: [{ format: 'glb', url: '/api/v1/cad-agent/runs/cad_test/1/artifacts/glb?access=test' }, { format: 'svg', view: 'front', url: '/api/v1/cad-agent/runs/cad_test/1/artifacts/view-front?access=test' }], ...overrides })

test('new drawings and blank designs use generic CAD while existing recipe edits remain compatible', () => {
  assert.equal(shouldUseCadAgent(null), true)
  assert.equal(shouldUseCadAgent({ kind: '' }), true)
  assert.equal(shouldUseCadAgent({ kind: 'feature_model' }), true)
  assert.equal(shouldUseCadAgent({ kind: 'arched_clevis_support' }), false)
  assert.equal(shouldUseCadAgent({ kind: 'arched_clevis_support' }, [{ name: 'new.jpg' }]), true)
})

test('a complete parameter draft builds STEP and GLB instead of falling through to STEP export', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const start = app.indexOf('  const primaryAction = () => {')
  const code = app.slice(start, app.indexOf('  // A queued drawing', start))
  const source = new File(['original bytes'], 'drawing.jpg', { type: 'image/jpeg' })
  const job = { status: 'idle', file: source, fileMeta: { name: source.name }, evidence: null }
  let builds = 0, exports = 0
  const context = vm.createContext({
    featureModel: false, chatAttachments: [], canBuildParameterDraft: true, drawingJob: job,
    rebuildCurrentModel: () => { builds += 1 }, exportFile: () => { exports += 1 },
  })
  vm.runInContext(`${code}\nprimaryAction()`, context)
  assert.equal(builds, 1)
  assert.equal(exports, 0)
  assert.equal(job.file, source)
  assert.equal(job.fileMeta.name, source.name)
  assert.equal(job.evidence, null)

  const snapshotStart = app.indexOf('function workflowSnapshot(')
  const snapshotCode = app.slice(snapshotStart, app.indexOf('function ChatMessageList', snapshotStart))
  const workflowContext = vm.createContext({ isLegacyDrawingDraft, evidenceAcceptedForPreview: (evidence) => evidence?.status === 'confirmed' })
  vm.runInContext(snapshotCode, workflowContext)
  const draft = { drawingJob: job, model: { kind: 'arched_clevis_support' }, modelValid: true }
  assert.equal(workflowContext.workflowSnapshot(draft).label, '参数草稿，准备生成')
  assert.equal(workflowContext.workflowSnapshot({ ...draft, isGenerating: true }).label, '正在生成 3D')
  assert.equal(workflowContext.workflowSnapshot({ ...draft, chatAttachments: [source] }).current, 'recognize')
  const legacyWorkflow = workflowContext.workflowSnapshot({ ...draft, drawingJob: { ...job, evidence: { status: 'candidate' } } })
  assert.equal(legacyWorkflow.current, 'recognize')
  assert.match(legacyWorkflow.label, /旧版图纸草稿/)

  const condition = app.match(/  const canBuildParameterDraft = (.+)/)[1]
  for (const status of ['queued', 'analyzing', 'error']) {
    let picks = 0
    const interruptedJob = { ...job, status, requiresFileReselection: true, interrupted: true }
    const interruptedContext = vm.createContext({
      hasModel: true, modelValid: true, evidence: null, generation: null, chatAttachments: [], drawingJob: interruptedJob,
      featureModel: false, legacyDrawing: false, rebuildCurrentModel: () => { throw new Error('must recover the interrupted drawing first') }, drawingInputRef: { current: { click: () => { picks += 1 } } },
    })
    vm.runInContext(`const canBuildParameterDraft = ${condition}\n${code}\nprimaryAction()`, interruptedContext)
    assert.equal(picks, 1)
    assert.notEqual(workflowContext.workflowSnapshot({ ...draft, drawingJob: interruptedJob }).label, '参数草稿，准备生成')
  }
})

test('a newly selected drawing takes precedence over restored source-reselection and old candidate gates', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const start = app.indexOf('  const primaryAction = () => {')
  const code = app.slice(start, app.indexOf('  // A queued drawing', start))
  let sends = 0, picks = 0
  const context = vm.createContext({
    chatAttachments: [new File(['new original'], 'replacement.jpg', { type: 'image/jpeg' })],
    featureModel: false, legacyDrawing: true, canBuildParameterDraft: false,
    drawingJob: { status: 'error', interrupted: true, requiresFileReselection: true, evidence: { status: 'candidate' } },
    drawingInputRef: { current: { click: () => { picks += 1 } } }, runGenerate: () => { sends += 1 },
  })
  vm.runInContext(`${code}\nprimaryAction()`, context)
  assert.equal(sends, 1)
  assert.equal(picks, 0)
})

test('legacy drawing migration uses original bytes, requests reselection for metadata, and normalizes retired workspaces', () => {
  const legacy = { kind: 'bracket', baseLength: 110 }
  const source = new File(['original drawing'], 'original.jpg', { type: 'image/jpeg' })
  const job = { evidence: { status: 'candidate' }, file: source, fileMeta: { name: source.name } }
  assert.equal(isLegacyDrawingDraft(legacy, job), true)
  assert.equal(isLegacyDrawingDraft({ kind: '' }, job), true)
  assert.equal(isLegacyDrawingDraft({ kind: 'feature_model' }, job), false)
  assert.equal(isLegacyDrawingDraft(legacy, { ...job, evidence: null }), false)
  assert.equal(cadLegacySourceFiles(job)[0], source)
  assert.deepEqual(cadLegacySourceFiles({ ...job, file: { name: source.name } }), [])
  assert.deepEqual(cadLegacySourceFiles({ fileMeta: job.fileMeta }), [])
  assert.equal(normalizeCadWorkspaceMode('图纸转 3D'), '3D 建模')
  assert.equal(normalizeCadWorkspaceMode('图纸转3D'), '3D 建模')
  assert.equal(normalizeCadWorkspaceMode('2D 工程图'), '2D 工程图')

  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const initialModeCode = app.match(/  const \[activeMode, setActiveMode\] = .+/)[0]
  const restored = vm.createContext({ initialSnapshot: { activeMode: '图纸转 3D', drawingJob: job }, useState: (mode) => [mode, () => {}], normalizeCadWorkspaceMode })
  vm.runInContext(`${initialModeCode}\nglobalThis.restoredMode = activeMode`, restored)
  assert.equal(restored.restoredMode, '3D 建模')
  const start = app.indexOf('  const migrateLegacyDrawing = () => {')
  const code = app.slice(start, app.indexOf('  const primaryLabel', start))
  let picks = 0, sends = 0
  const notices = []
  const context = vm.createContext({ interactionBusy: false, drawingJob: { ...job, file: null }, cadLegacySourceFiles,
    remodelLegacyDrawing: (files) => { assert.equal(files[0], source); sends += 1 }, showToast: (text) => notices.push(text), legacyDrawingInputRef: { current: { click: () => { picks += 1 } } } })
  vm.runInContext(`${code}\nmigrateLegacyDrawing()`, context)
  assert.equal(picks, 1)
  assert.equal(sends, 0)
  assert.match(notices[0], /重新选择原图/)
  context.drawingJob = job
  vm.runInContext('migrateLegacyDrawing()', context)
  assert.equal(sends, 1)
  assert.equal(job.evidence.status, 'candidate')
})

test('source-reader waits and draft saves never advance the workflow to generated or confirmed geometry', () => {
  let progress
  for (const stage of ['started', 'source_transcription', 'source_transcription_waiting', 'source_region_transcribed', 'source_transcription_complete', 'observe_source', 'agent_working', 'plan_saved']) {
    progress = cadProgressFromEvent({ stage, geometryGenerated: false }, progress)
    for (const model of [{ kind: '' }, cadModelFromResult(result({ status: 'ready' }))]) {
      const workflow = cadWorkflowSnapshot({ model, progress, busy: true })
      assert.equal(workflow.current, 'recognize')
      assert.doesNotMatch(workflow.label, /实体已生成|已确认/)
    }
  }
  progress = cadProgressFromEvent({ stage: 'execute_plan' }, progress)
  assert.equal(cadWorkflowSnapshot({ progress, busy: true }).current, 'generate')
  progress = cadProgressFromEvent({ stage: 'inspect_geometry', errors: [{ message: '孔超出边界' }] }, progress)
  assert.match(progress.label, /未通过/)
  progress = cadProgressFromEvent({ stage: 'agent_working', geometryGenerated: true }, progress)
  assert.equal(progress.phase, 'generate')
  assert.match(progress.label, /尚无新的几何结果/)
  const regionProgress = cadProgressFromEvent({ stage: 'source_region_transcribed', message: '已取得 2/3 个内容区域的标注候选，尚未验证尺寸或生成模型', geometryGenerated: false }, progress)
  assert.equal(regionProgress.phase, 'recognize')
  assert.match(regionProgress.label, /候选.*待核对/)
  assert.match(regionProgress.message, /2\/3/)
  const draftProgress = cadProgressFromEvent({ stage: 'inspect_draft', geometryGenerated: false }, progress)
  assert.match(draftProgress.label, /检查草稿几何.*尚无交付模型/)
  assert.doesNotMatch(draftProgress.label, /已生成|已确认/)
  assert.equal(cadWorkflowSnapshot({ model: cadModelFromResult(result()), busy: false }).current, 'review')
  assert.match(cadWorkflowSnapshot({ model: cadModelFromResult(result({ status: 'failed' })), busy: false }).label, /本轮未完成/)
  assert.match(cadWorkflowSnapshot({ model: cadModelFromResult(result({ status: 'ready' })), busy: false, error: '本次连接已断开' }).label, /本轮未完成/)
  assert.match(cadWorkflowSnapshot({ model: { kind: '' }, progress, busy: false }).label, /本轮未完成/)
})

test('source and plan trace entries distinguish candidates and rejected operations from actual geometry', () => {
  assert.match(cadTraceMessage({ action: 'read_source', message: '成功', result: { status: 'succeeded' } }), /候选.*待核对/)
  assert.match(cadTraceMessage({ action: 'read_source', result: { status: 'failed' } }), /未完成/)
  assert.match(cadTraceMessage({ action: 'edit_plan', result: { status: 'saved' } }), /草稿.*尚待执行/)
  assert.match(cadTraceMessage({ action: 'edit_plan', result: { status: 'failed' } }), /未通过/)
  assert.match(cadTraceMessage({ action: 'record_observations', result: { status: 'recorded' } }), /尚非实测结果/)
  assert.match(cadTraceMessage({ action: 'finish', result: { errors: [{ message: '还未检查投影' }] } }), /尚未通过/)
})

test('spatial candidates and cross-view repair describe current work without certifying a whole drawing', () => {
  const model = cadModelFromResult(result({ status: 'ready', revision: 2 }))
  const before = structuredClone(model)
  for (const stage of ['source_spatial', 'source_spatial_interpretation', 'source_spatial_waiting', 'source_spatial_complete', 'source_spatial_reused']) {
    const progress = cadProgressFromEvent({ stage, status: 'succeeded', geometryGenerated: false }, { phase: 'recognize' })
    assert.equal(progress.phase, 'recognize')
    assert.doesNotMatch(cadWorkflowSnapshot({ model, progress, busy: true }).label, /图纸.*通过|已确认|已生成/)
  }
  for (const stage of ['projection_compare', 'geometry_repair']) {
    const progress = cadProgressFromEvent({ stage, status: 'succeeded' }, { phase: 'review' })
    assert.equal(progress.phase, 'generate')
    assert.equal(cadWorkflowSnapshot({ model, progress, busy: true }).current, 'generate')
  }
  assert.deepEqual(model, before)
  assert.match(cadTraceMessage({ action: 'interpret_source_spatial', result: { status: 'succeeded' } }), /候选.*尚待/)
  assert.match(cadTraceMessage({ action: 'projection_compare', result: { status: 'succeeded' } }), /核对范围不足/)
  assert.match(cadTraceMessage({ action: 'projection_compare', result: { status: 'mismatch', views: [{ view: 'front', status: 'mismatch', reliability: 'high' }] } }), /差异.*修正/)
})

test('late auxiliary spatial results preserve the main geometry phase without claiming successful analysis', () => {
  for (const stage of ['source_spatial_complete', 'source_spatial_unavailable']) {
    const alreadyBuilding = cadProgressFromEvent({ stage, geometryGenerated: false }, { phase: 'generate' })
    assert.equal(alreadyBuilding.phase, 'generate')
    assert.equal(cadProgressFromEvent({ stage, geometryGenerated: true }, { phase: 'recognize' }).phase, 'generate')
    assert.equal(cadProgressFromEvent({ stage, geometryGenerated: false }, { phase: 'recognize' }).phase, 'recognize')
    assert.equal(cadProgressFromEvent(alreadyBuilding).phase, 'generate')
    assert.equal(cadWorkflowSnapshot({ progress: alreadyBuilding, busy: true }).current, 'generate')
  }
  const unavailable = cadProgressFromEvent({ stage: 'source_spatial_unavailable' })
  assert.match(unavailable.label, /未完成.*继续依据原图/)
  assert.doesNotMatch(unavailable.label, /已返回|通过|已确认/)
})

test('projection summaries distinguish reliable differences from limited scope without certifying the entire drawing', () => {
  const comparison = { scope: 'original_drawing', status: 'mismatch', planHash: 'server-only-hash',
    views: [
      { view: 'front', status: 'mismatch', reliability: 'high', metrics: { p90DistancePixels: 20 }, differenceRegions: [{ finding: '上侧出现原图不支持的横边', sourceRegion: [0, 0, 1, 1] }] },
      { view: 'top', status: 'supported', reliability: 'high' },
      { view: 'right', status: 'mismatch', reliability: 'uncertain', differenceRegions: [{ finding: '不可靠的疑似差异' }] },
    ] }
  const summary = cadProjectionSummary(comparison)
  assert.equal(summary.mismatchCount, 1)
  assert.equal(summary.supportedCount, 1)
  assert.equal(summary.uncertainCount, 1)
  assert.equal(summary.summary, '1 个视图发现明确轮廓差异')
  assert.deepEqual(summary.rows.map((row) => row.name), ['前视图', '俯视图', '右视图'])
  assert.deepEqual(summary.rows[0].findings, ['上侧出现原图不支持的横边'])
  assert.deepEqual(summary.rows[2].findings, [])
  assert.equal(summary.rows[1].label, '所测轮廓未见差异')
  assert.equal(summary.rows[2].label, '核对范围不足')
  assert.doesNotMatch(JSON.stringify(summary), /p90DistancePixels|sourceRegion|server-only-hash|不可靠的疑似差异/)

  const supported = { ...comparison, status: 'uncertain', noDetectedContourDifference: true, views: comparison.views.slice(1, 2) }
  assert.match(cadProjectionSummary(supported).summary, /所测轮廓未见差异/)
  assert.doesNotMatch(cadTraceMessage({ action: 'projection_compare', result: supported }), /通过|整体一致|完全一致/)
  assert.equal(cadProjectionSummary({ status: 'uncertain', views: [] }).summary, '轮廓核对范围不足')
  assert.equal(cadProjectionSummary(null), null)

  const model = cadModelFromResult(result({ status: 'ready', projectionComparison: supported, drawingReview: { status: 'consistent', projectionComparison: supported } }))
  const recovered = recoverWorkspaceSnapshot({ model })
  assert.equal(recovered.model.agentRun.projectionComparison.noDetectedContourDifference, true)
  assert.equal(recovered.model.agentRun.drawingReview.projectionComparison.planHash, comparison.planHash)
  assert.equal(cadRunPresentation(recovered.model.agentRun).confirmed, true)
  assert.equal(recovered.model.agentRun.status, 'ready')
})

test('a previous ready result is labelled as saved history while a new operation is still running', () => {
  const run = { runId: 'previous-run', revision: 2, status: 'ready', message: '已确认并生成' }
  const busy = cadRunPresentation(run, true)
  assert.equal(busy.confirmed, false)
  assert.equal(busy.savedResult, true)
  assert.equal(busy.title, '本轮仍在处理')
  assert.match(busy.message, /已保存的第 2 版.*本轮尚未完成/)
  assert.equal(cadRunPresentation(run, false).confirmed, true)
  assert.equal(cadRunPresentation({ ...run, dirty: true }, false).confirmed, false)
  assert.equal(cadRunPresentation({ ...run, status: 'review_required' }, false).confirmed, false)
  assert.equal(run.status, 'ready')
})

test('a server-blocked ready result keeps its real preview but cannot appear deliverable', () => {
  const blocked = result({ status: 'ready', deliveryBlockedReason: '当前轮廓核对记录不属于此模型。',
    artifacts: [...result().artifacts, { format: 'step', url: '/api/v1/cad-agent/runs/cad_test/1/artifacts/step?access=test', productionReady: true }] })
  const model = cadModelFromResult(blocked)
  const generation = cadGenerationFromResult(blocked, model.cadPlan)
  assert.equal(generation.validation.productionReady, false)
  assert.equal(generation.previewOnly, true)
  assert.equal(generation.artifacts.some((artifact) => artifact.format === 'glb'), true)
  assert.equal(generation.artifacts.some((artifact) => artifact.format === 'step'), false)
  assert.equal(cadRunPresentation(model.agentRun).confirmed, false)
  assert.equal(cadRunPresentation(model.agentRun).title, '需重新检查')
  assert.match(cadRunPresentation(model.agentRun).message, /当前轮廓核对记录/)
  assert.equal(cadWorkflowSnapshot({ model }).current, 'generate')
  assert.match(cadWorkflowSnapshot({ model }).label, /需重新检查/)
  const saved = recoverWorkspaceSnapshot({ model, generation })
  assert.equal(saved.generation.validation.productionReady, false)
  assert.equal(saved.model.agentRun.deliveryBlockedReason, blocked.deliveryBlockedReason)
})

test('2D views label saved projections and hide delivery during active, paused background and confirmation work', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const busyExpression = app.match(/<CadAgentDrawing[^>]+busy=\{([^}]+)\}/)[1]
  const panel = readFileSync(new URL('./CadAgentPanel.jsx', import.meta.url), 'utf8')
  const start = panel.indexOf('export function CadAgentDrawing(')
  const declarations = panel.slice(start, panel.indexOf('  return <section', start)).replace('export ', '')
  const context = vm.createContext({ cadGenerationIsCurrent, cadRunPresentation })
  vm.runInContext(`${declarations}return { title, description, canExport, artifacts }; }`, context)
  const ready = result({ status: 'ready' })
  const model = cadModelFromResult(ready), generation = cadGenerationFromResult(ready, model.cadPlan)
  const idle = { isGenerating: false, isAccepting: false, drawingJob: null }
  for (const operation of [{ isGenerating: true }, { isAccepting: true }, { drawingJob: { cadTask: { status: 'running', paused: true } } }]) {
    const busy = vm.runInNewContext(`Boolean(${busyExpression})`, { ...idle, ...operation })
    const display = context.CadAgentDrawing({ model, generation, busy })
    assert.equal(display.canExport, false)
    assert.equal(display.artifacts.length, 1)
    assert.match(display.title, /本轮处理中.*上一版本/)
    assert.match(display.description, /已保存的第 1 版.*本轮尚未完成/)
  }
  const busy = vm.runInNewContext(`Boolean(${busyExpression})`, idle)
  assert.equal(context.CadAgentDrawing({ model, generation, busy }).canExport, true)
  const preview = result(), previewModel = cadModelFromResult(preview)
  assert.equal(context.CadAgentDrawing({ model: previewModel, generation: cadGenerationFromResult(preview, previewModel.cadPlan), busy }).canExport, false)
  const empty = context.CadAgentDrawing({ model: previewModel, generation: null, busy: true })
  assert.equal(empty.artifacts.length, 0)
  assert.match(empty.title, /等待实体投影/)
  assert.doesNotMatch(empty.description, /已保存|已生成|已完成/)
})

test('independent drawing review remains in progress and mismatches preserve differences without completed geometry', () => {
  const progress = cadProgressFromEvent({ stage: 'independent_drawing_review', geometryGenerated: true }, { phase: 'review' })
  assert.equal(progress.label, '正在独立比对原图与实体投影')
  const current = cadWorkflowSnapshot({ model: cadModelFromResult(result({ status: 'ready' })), progress, busy: true })
  assert.equal(current.current, 'generate')
  assert.doesNotMatch(current.label, /已生成|已完成|已确认/)
  const drawingReview = { status: 'mismatch', message: '独立比对发现缺失特征', observations: ['底板轮廓相符'], differences: ['实体缺少图纸左侧通孔'] }
  const failed = result({ status: 'failed', drawingReview })
  const model = cadModelFromResult(failed)
  assert.deepEqual(model.agentRun.drawingReview.differences, ['实体缺少图纸左侧通孔'])
  assert.equal(cadGenerationFromResult(failed, model.cadPlan), null)
  assert.match(cadWorkflowSnapshot({ model, progress, busy: false }).label, /未完成.*重试建模/)
  assert.match(cadTraceMessage({ action: 'independent_drawing_review', result: drawingReview }), /未通过.*待修正/)
  assert.match(cadTraceMessage({ action: 'independent_drawing_review', result: { status: 'failed' } }), /未通过/)
})

test('a failed streamed CAD result is saved as failure, clears prior artifacts, and retains source candidates for continuation', async () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const handlers = app.slice(app.indexOf('const applyCadResult = (result, previous, sourceFile = null) => {'), app.indexOf('const confirmCadModel = async () => {'))
  const initial = cadModelFromResult(result({ status: 'ready' }))
  const state = { model: initial, generation: cadGenerationFromResult(result({ status: 'ready' }), initial.cadPlan), job: { status: 'generated' }, conversation: {}, messages: [], busy: false }
  const update = (key) => (value) => { state[key] = typeof value === 'function' ? value(state[key]) : value }
  const failed = result({ status: 'failed', message: '原图转录未完成', plan: null, artifacts: [], sourceTranscription: { status: 'failed', verified: false, annotations: [] } })
  let id = 0
  const context = vm.createContext({
    AbortController, aiRequestRef: { current: 0 }, workspaceIdRef: { current: 'workspace-test' }, modelInteractionRevisionRef: { current: 0 },
    modelRef: { current: initial }, chatAbortRef: { current: null }, cadConfirmRef: { current: null }, drawingJobRef: { current: state.job }, isAccepting: false, setLastAiTurn: () => {}, chatId: () => `id-${++id}`, chatMessage: (role, text, extras) => ({ role, text, ...extras }),
    setMessages: update('messages'), setIsGenerating: update('busy'), setAiConversation: update('conversation'), setModel: update('model'), setGeneration: update('generation'), setDrawingJob: update('job'), setActivePanel: () => {},
    cadModelFromResult, cadGenerationFromResult, cadRequestState, cadProgressFromEvent, cadTerminalResult, configuredAiProvider,
    platform: {}, messages: [], conversationHistory: () => [], readableError: (error) => error.message,
    cadAgent: { run: async ({ onEvent }) => {
      onEvent('progress', { stage: 'source_transcription_waiting', message: '独立读图仍在进行', geometryGenerated: false })
      assert.equal(state.conversation.cadProgress.phase, 'recognize')
      assert.match(state.conversation.statusMessage, /仍在进行/)
      return failed
    } },
  })
  vm.runInContext(`${handlers}\nglobalThis.send = sendCadConversation`, context)
  assert.equal(await context.send('请重新读取原图'), false)
  assert.equal(state.busy, false)
  assert.equal(state.job.status, 'error')
  assert.equal(state.job.error, failed.message)
  assert.equal(state.conversation.turnStatus, 'error')
  assert.equal(state.conversation.error, failed.message)
  assert.equal(state.messages.at(-1).status, 'error')
  assert.equal(state.generation, null)
  assert.equal(state.model.cadPlan, null)
  assert.equal(state.model.agentRun.sourceTranscription.verified, false)
  const footerExpression = app.match(/<i className="live-dot" \/> \{([^}]+)\} · \{model.updatedAt\}/)[1]
  const footerState = { legacyDrawing: false, featureModel: true, generation: null, model: state.model }
  assert.equal(vm.runInNewContext(footerExpression, footerState), '原图与任务状态已保存')
  assert.equal(vm.runInNewContext(footerExpression, { ...footerState, model: { ...state.model, cadPlan: plan } }), '尺寸与建模计划已保存')
  const restored = recoverWorkspaceSnapshot({ model: state.model, generation: state.generation, drawingJob: { ...state.job, fileMeta: { name: 'original.jpg' } }, aiConversation: state.conversation })
  assert.equal(restored.drawingJob.requiresFileReselection, false)
  assert.equal(restored.aiConversation.cadProgress, null)
  assert.equal(restored.aiConversation.turnStatus, 'idle')
  assert.equal(cadRequestState(restored.model).agentRun.runId, failed.runId)
})

test('generic CAD does not inherit an empty-project material default or revive an unproven saved default', () => {
  const blank = { kind: '', material: '45# 钢' }
  const model = cadModelFromResult(result(), blank)
  assert.equal(model.material, '')
  assert.equal(cadRequestState(blank).material, '')
  assert.equal(cadExplicitMaterial(model), '')
  assert.equal(cadExplicitMaterial({ kind: 'feature_model', material: '45# 钢' }), '')
  const specified = cadModelFromResult(result({ material: 'AL6061 铝合金' }))
  assert.equal(cadExplicitMaterial(specified), 'AL6061 铝合金')
  assert.equal(cadExplicitMaterial(cadModelFromResult(result(), specified)), 'AL6061 铝合金')
  assert.equal(cadRequestState(specified).material, 'AL6061 铝合金')
  assert.equal(blank.material, '45# 钢')
})

test('unknown dimensions remain blank, expressions stay derived and kernel checks do not unlock delivery', () => {
  const needsInput = cadModelFromResult(result({ status: 'needs_input', plan }))
  assert.equal(needsInput.cadPlan.parameters.thickness.value, null)
  assert.deepEqual(validateModelParameters(needsInput).errors.map((item) => item.field), ['thickness'])
  assert.equal(cadGenerationFromResult(result({ status: 'needs_input', plan }), plan), null)
  const candidate = result(), model = cadModelFromResult(candidate)
  assert.equal(validateCadPlan(model).valid, true)
  const generation = cadGenerationFromResult(candidate, model.cadPlan)
  assert.equal(generation.validation.productionReady, false)
  assert.equal(generationMatchesModel(model, generation), true)
  assert.equal(generation.artifacts.find((item) => item.format === 'svg').view, 'front')
})

test('a structural question can show its current real CAD draft while STEP remains locked', () => {
  const payload = result({ status: 'needs_input', questions: ['请核对两孔位置是否符合要求。'], inspection: { valid: true, kernelBacked: true, engine: 'cadquery-occt', solidCount: 1 } })
  const model = cadModelFromResult(payload)
  const draft = cadGenerationFromResult(payload, model.cadPlan)
  assert.ok(draft)
  assert.equal(draft.previewOnly, true)
  assert.equal(draft.validation.productionReady, false)
  assert.equal(generationMatchesModel(model, draft), true)
  for (const inspection of [
    { ...payload.inspection, valid: false },
    { ...payload.inspection, kernelBacked: false },
    { ...payload.inspection, kernelBacked: undefined },
    { ...payload.inspection, engine: 'faceted-fallback' },
    { ...payload.inspection, solidCount: 0 },
  ]) assert.equal(cadGenerationFromResult({ ...payload, inspection }, model.cadPlan), null)
  assert.equal(cadGenerationFromResult({ ...payload, artifacts: [] }, model.cadPlan), null)
  assert.equal(cadGenerationFromResult({ ...payload, artifacts: [{ format: 'glb', url: '/api/v1/cad-agent/runs/older/1/artifacts/glb' }] }, model.cadPlan), null)
  assert.equal(cadGenerationFromResult({ ...payload, status: 'failed' }, model.cadPlan), null)
  assert.equal(cadGenerationFromResult({ ...payload, plan }, plan), null)
})

test('manual confirmation omits derived expressions and preserves explicit unknowns until user completion', () => {
  assert.deepEqual(cadConfirmationParameters(plan), { width: 60, thickness: null })
  const filled = editCadParameter(cadModelFromResult(result({ status: 'needs_input', plan })), 'thickness', '9')
  assert.deepEqual(cadConfirmationParameters(filled.cadPlan), { width: 60, thickness: 9 })
  assert.equal(validateCadPlan(filled).valid, true)
})

test('continuing a project sends editable geometry and its server revision without replaying large tool traces', () => {
  const model = cadModelFromResult(result({ trace: [{ huge: 'x'.repeat(800000) }] }))
  const state = cadRequestState(model)
  assert.deepEqual(state.agentRun, { runId: 'cad_test', revision: 1 })
  assert.equal(state.cadPlan, model.cadPlan)
  assert.equal(JSON.stringify(state).includes('huge'), false)
  assert.ok(JSON.stringify(state).length < 1000)
})

test('the actual confirmation action sends edited dimensions through the agent before any STEP confirmation', async () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const handler = app.slice(app.indexOf('const confirmCadModel = async () => {'), app.indexOf('const sendAiConversation = async (text, filesInput = []) => {'))
  const initial = cadModelFromResult(result({ status: 'ready' }))
  const edited = editCadParameter(initial, 'width', '84')
  const calls = { runs: [], confirmations: [], messages: [] }
  const context = vm.createContext({
    isGenerating: false, isAccepting: false, modelRef: { current: edited }, chatAbortRef: { current: null }, cadConfirmRef: { current: null }, drawingJobRef: { current: {} }, validateModelParameters,
    cadPrimaryAction, cadRetryMessage, cadParameterEditMessage, cadConfirmationParameters, setLastAiTurn: (turn) => calls.messages.push(turn),
    sendCadConversation: async (message) => { calls.runs.push(message); return true },
    cadAgent: { confirm: async (payload) => calls.confirmations.push(payload) },
    showToast: () => { throw new Error('valid edited plan must reach the agent') },
  })
  vm.runInContext(`${handler}\nglobalThis.confirmModel = confirmCadModel`, context)
  assert.equal(await context.confirmModel(), true)
  assert.equal(calls.confirmations.length, 0)
  assert.equal(calls.runs.length, 1)
  assert.match(calls.runs[0], /板宽.*从60改为84/)
  assert.match(calls.runs[0], /保持其他尺寸与结构/)
  assert.equal(calls.messages[0].prompt, calls.runs[0])
})

test('editing one parameter invalidates actual artifacts and comparison without discarding the original evidence', () => {
  const ready = result({ status: 'ready' }), model = cadModelFromResult(ready)
  const generation = cadGenerationFromResult(ready, model.cadPlan)
  const edited = editCadParameter(model, 'width', '80')
  assert.equal(cadParameterValues(edited.cadPlan).width, 80)
  assert.equal(model.cadPlan.parameters.width.value, 60)
  assert.equal(edited.cadPlan.parameters.width.source.view, 'top')
  assert.equal(edited.cadPlan.parameters.width.source.type, 'manual')
  assert.equal(edited.agentRun.dirty, true)
  assert.equal(edited.agentRun.status, 'review_required')
  assert.equal(edited.agentRun.inspection, null)
  assert.deepEqual(edited.agentRun.artifacts, [])
  assert.equal(generationMatchesModel(edited, generation), false)
  assert.equal(validateCadPlan(editCadParameter(model, 'width', '')).valid, false)
  assert.equal(edited.cadPlan.parameters.halfWidth.expression, 'width / 2')
})

test('only matching run, revision and complete plan can be rendered as the current artifact', () => {
  const payload = result({ status: 'ready' }), model = cadModelFromResult(payload)
  const generation = cadGenerationFromResult(payload, model.cadPlan)
  assert.equal(generationMatchesModel(model, generation), true)
  assert.equal(generationMatchesModel(model, { ...generation, revision: 0 }), false)
  assert.equal(generationMatchesModel(model, { ...generation, runId: 'other' }), false)
  assert.equal(generationMatchesModel({ ...model, cadPlan: { ...model.cadPlan, result: 'different_feature' } }, generation), false)
  assert.equal(cadGenerationFromResult(result({ status: 'failed' }), plan), null)
  assert.equal(cadModelFromResult(result({ plan: null }), model).cadPlan, null)
  assert.equal(cadPlanSignature({ a: 1, b: [2, 3] }), cadPlanSignature({ b: [2, 3], a: 1 }))
  assert.notEqual(cadPlanSignature({ b: [2, 3] }), cadPlanSignature({ b: [3, 2] }))
})

test('saving and reopening keeps model, parameter sources, run revision and scoped artifact URLs', () => {
  const payload = result({ status: 'ready', revision: 3 }), model = cadModelFromResult(payload)
  const snapshot = { model, generation: cadGenerationFromResult(payload, model.cadPlan), drawingJob: { status: 'generated', fileMeta: { name: 'drawing.jpg' } } }
  const restored = recoverWorkspaceSnapshot(JSON.parse(JSON.stringify(sanitizeWorkspaceSnapshot(snapshot))))
  assert.deepEqual(restored.model, model)
  assert.equal(restored.drawingJob.requiresFileReselection, false)
  assert.equal(generationMatchesModel(restored.model, restored.generation), true)
  assert.match(restored.generation.artifacts[0].downloadUrl, /access=test/)
  const unresolved = recoverWorkspaceSnapshot({ ...snapshot, model: cadModelFromResult(result({ status: 'needs_input', plan })), generation: null, drawingJob: { status: 'ready', fileMeta: { name: 'drawing.jpg' } } })
  assert.equal(unresolved.drawingJob.requiresFileReselection, false)
  assert.equal(unresolved.model.agentRun.runId, 'cad_test')
})

test('source transcription remains unverified candidate evidence after failure, persistence and reopening', () => {
  const transcription = { version: 'cad-source-reader-v1', status: 'succeeded', candidateEvidence: true, verified: false,
    annotations: [{ id: 'annotation-1', imageId: 'source-0-original', text: '40', view: '主视图', location: '左侧竖向标注', endpointsOrDatum: '底板下表面到孔中心线', confidence: 'high', questions: ['请核对下端引线的基准面。'] }],
    structureObservations: [], questions: ['请确认模糊标注。'] }
  const payload = result({ status: 'failed', plan: null, artifacts: [], sourceTranscription: transcription })
  const model = cadModelFromResult(payload)
  const restored = recoverWorkspaceSnapshot(JSON.parse(JSON.stringify(sanitizeWorkspaceSnapshot({ model, generation: null, drawingJob: { status: 'ready', fileMeta: { name: 'drawing.jpg' } } }))))
  assert.deepEqual(restored.model.agentRun.sourceTranscription, transcription)
  assert.equal(restored.model.agentRun.sourceTranscription.verified, false)
  assert.equal(restored.model.cadPlan, null)
  assert.equal(restored.generation, null)
  assert.equal(cadGenerationFromResult(payload, null), null)
  assert.equal(cadRequestState(restored.model).agentRun.runId, payload.runId)
  assert.equal(cadRequestState(restored.model).sourceTranscription, undefined)
})

function stream(chunks) {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({ start(controller) { chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk))); controller.close() } }), { headers: { 'Content-Type': 'text/event-stream' } })
}

test('CAD stream tolerates split frames and keepalives but requires a real final result', async () => {
  const events = []
  const response = stream(['event: pro', 'gress\r\ndata: {"message":"正在检查"}\r\n\r\n: keepalive\n\n', 'event: result\ndata: {"status":"needs_input","questions":["厚度？"]}'])
  assert.deepEqual(await readCadAgentEvents(response, (event, payload) => events.push([event, payload])), { status: 'needs_input', questions: ['厚度？'] })
  assert.equal(events[0][0], 'progress')
  await assert.rejects(readCadAgentEvents(stream(['event: progress\ndata: {"message":"处理中"}\n\n'])), /没有收到结果/)
  await assert.rejects(readCadAgentEvents(stream(['event: error\ndata: {"message":"几何未通过"}\n\n'])), /几何未通过/)
  await assert.rejects(readCadAgentEvents(stream(['event: result\ndata: {invalid}\n\n'])), /不完整的事件/)
})

test('a complete terminal result survives a subsequent socket failure', async () => {
  let reads = 0
  const response = new Response(new ReadableStream({ pull(controller) {
    if (++reads === 1) controller.enqueue(new TextEncoder().encode('event: result\ndata: {"status":"failed","runId":"saved-run","revision":1,"plan":null}\n\n'))
    else controller.error(new Error('socket reset after result'))
  } }), { headers: { 'Content-Type': 'text/event-stream' } })
  const saved = await readCadAgentEvents(response)
  assert.equal(saved.runId, 'saved-run')
  assert.equal(saved.status, 'failed')
})

test('confirmation errors display nested geometry failures and never produce a success state', async () => {
  await assert.rejects(readCadAgentEvents(new Response(JSON.stringify({ detail: { message: '模型校核未通过', errors: [{ message: '孔超出边界' }] } }), { status: 422, headers: { 'Content-Type': 'application/json' } })), /模型校核未通过；孔超出边界/)
})

test('run transmits actual files, state and history; confirmation is bound to its revision', async (t) => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify(result()), { headers: { 'Content-Type': 'application/json' } }) })
  const file = new File(['actual bytes'], 'original.jpg', { type: 'image/jpeg' })
  const model = cadModelFromResult(result())
  await cadAgent.run({ message: '孔改大', files: [file], modelState: model, history: [{ role: 'user', content: '原要求' }], token: 'test-only' })
  assert.match(calls[0].url, /\/cad-agent\/run$/)
  assert.equal(calls[0].options.body.getAll('files')[0].name, 'original.jpg')
  assert.equal(JSON.parse(calls[0].options.body.get('modelState')).agentRun.runId, 'cad_test')
  assert.equal(JSON.parse(calls[0].options.body.get('history'))[0].content, '原要求')
  assert.equal(calls[0].options.headers.Authorization, 'Bearer test-only')
  await cadAgent.confirm({ runId: 'cad_test', revision: 3, parameters: { width: 80 } })
  assert.deepEqual(JSON.parse(calls[1].options.body), { runId: 'cad_test', revision: 3, parameters: { width: 80 } })
  await cadAgent.getRun({ runId: 'cad_test', token: 'test-only' })
  assert.match(calls[2].url, /\/cad-agent\/runs\/cad_test$/)
  assert.equal(calls[2].options.headers.Authorization, 'Bearer test-only')
  assert.match(cadArtifactUrl({ url: '/api/v1/cad-agent/runs/x/1/artifacts/glb?access=sample' }), /\/api\/v1\/cad-agent\/runs\/x\/1\/artifacts\/glb\?access=sample$/)
  assert.equal(cadArtifactUrl({ url: 'javascript:alert(1)' }), '')
})

function appCadHarness(initial = cadModelFromResult(result())) {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const section = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)))
  const state = { model: initial, generation: null, job: {}, messages: [], conversation: {}, prompt: '把板宽改为84', attachments: [], notices: [], switches: [], busy: false, accepting: false }
  let store = ProjectStore.loadProjectStore({ getItem: () => null })
  store = ProjectStore.updateFileSnapshot(store, store.activeProjectId, store.activeFileId, { model: initial, generation: null, messages: [] })
  state.store = store
  let context, id = 0
  const update = (key, ref) => (value) => { state[key] = typeof value === 'function' ? value(state[key]) : value; if (ref) context[ref].current = state[key] }
  context = vm.createContext({
    AbortController, File, cadPrimaryAction, cadRetryMessage, cadAgent: {}, ProjectStore, cadModelFromResult, cadGenerationFromResult, cadRequestState, cadProgressFromEvent, cadTerminalResult, cadRetryFiles, cadPlanSignature, cadConfirmationParameters, cadParameterEditMessage, isLegacyDrawingDraft, cadLegacySourceFiles, isFeatureModel: (model) => model?.kind === 'feature_model', validateModelParameters, configuredAiProvider,
    aiRequestRef: { current: 0 }, workspaceIdRef: { current: 'workspace-test' }, modelInteractionRevisionRef: { current: 0 }, modelRef: { current: initial }, drawingJobRef: { current: state.job }, chatAbortRef: { current: null }, cadConfirmRef: { current: null }, lastAiTurnRef: { current: null }, storeRef: { current: store }, currentSnapshotRef: { current: ProjectStore.getFileSnapshot(store) },
    chatId: () => `id-${++id}`, chatMessage: (role, text, extras) => ({ role, text, ...extras }), messages: [], conversationHistory: () => [], readableError: (error) => error.message, normalizeFilesInput: (files) => files,
    activeFile: ProjectStore.getActiveFile(store), settings: {}, platform: {}, isGenerating: false, isAccepting: false, emptyModel: () => ({ kind: '', name: '空白文件' }), prompt: state.prompt, chatAttachments: [],
    setModel: update('model', 'modelRef'), setGeneration: update('generation'), setDrawingJob: update('job', 'drawingJobRef'), setMessages: update('messages'), setAiConversation: update('conversation'), setIsGenerating: update('busy'), setIsAccepting: update('accepting'), setLastAiTurn: update('lastTurn', 'lastAiTurnRef'), setPrompt: update('prompt'), setChatAttachments: update('attachments'),
    setActivePanel: () => {}, setActiveMode: () => {}, setDialog: (value) => state.switches.push(value), showToast: (...args) => state.notices.push(args),
    flushWorkspace: () => state.store, commitStore: (next) => { state.store = next; context.storeRef.current = next; return next }, restoreWorkspace: (...args) => state.switches.push(args),
  })
  vm.runInContext([
    section('const applyCadResult = (result, previous, sourceFile = null) => {', 'const sendAiConversation = async (text, filesInput = []) => {'),
    section('const retryAi = async () => {', 'const runModelChecks = async () => {'),
    section('const canSwitch = () => {', 'const renameLocalProject ='),
    section('const startNewConversation = () => {', 'const createBasicShaft = () => {'),
    section('const runGenerate = async () => {', 'const stopAiConversation = () => {'),
    section('const attachDrawingToConversation = (fileInput) => {', 'useEffect(() => {'),
    'Object.assign(globalThis, {send:sendCadConversation, confirm:confirmCadModel, retry:retryAi, newConversation:startNewConversation, attach:attachDrawingToConversation, remodel:remodelLegacyDrawing, createProject, openProjectFile, selectLocalProject, runGenerate})',
  ].join('\n'), context)
  context.sendAiConversation = (...args) => context.send(...args)
  return { state, context }
}

test('CAD send synchronizes configured, progress and terminal providers without changing the legacy service configuration', async () => {
  const { state, context } = appCadHarness()
  const configured = { name: 'codex-cli', mode: 'codex', model: 'gpt-6-astra', reasoningEffort: 'high', configured: true, streaming: false, authentication: 'cli-managed' }
  const relay = { mode: 'remote', model: 'gpt-5.6-sol', configured: true, streaming: true, cadProvider: configured }
  state.conversation = { serviceStatus: relay, status: relay }
  const actual = { ...configured, attempts: 1, lastErrorCode: '' }
  context.cadAgent.run = async ({ onEvent }) => {
    assert.equal(state.conversation.status, configured)
    assert.equal(state.conversation.providerRoute, 'cad')
    onEvent('progress', { stage: 'started', provider: actual })
    assert.equal(state.conversation.status, actual)
    return result({ provider: { ...actual, attempts: 2 } })
  }
  assert.equal(await context.send('创建长方体'), true)
  assert.equal(state.conversation.status.attempts, 2)
  assert.equal(state.conversation.turnStatus, 'complete')
  assert.equal(state.conversation.serviceStatus, relay)
  assert.equal(workspaceAiProvider(state.model, [], state.conversation).mode, 'codex')
  assert.equal(workspaceAiProvider({ kind: 'shaft' }, [], state.conversation).mode, 'remote')
  context.cadAgent.run = async () => result({ status: 'failed', provider: { ...actual, attempts: 3, lastErrorCode: 'timeout' }, message: '本轮处理超时', artifacts: [] })
  assert.equal(await context.send('继续处理'), false)
  assert.equal(state.conversation.status.lastErrorCode, 'timeout')
  const display = aiProviderPresentation(state.conversation.status, { error: state.conversation.error })
  assert.equal(display.ready, false)
  assert.match(display.label, /本机 Codex.*响应超时.*3 次/)
  assert.doesNotMatch(display.label, /中转站|SSE|在线/)
  context.newConversation()
  assert.equal(state.conversation.providerRoute, '')
  assert.equal(workspaceAiProvider(state.model, [], state.conversation), configured)
})

test('resuming a CAD run adopts its actual provider instead of the saved model historical engine', async () => {
  const { state, context } = appCadHarness(cadModelFromResult(result({ provider: { mode: 'remote', model: 'old-engine' } })))
  const provider = { name: 'codex-cli', mode: 'codex', model: 'gpt-6-astra', reasoningEffort: 'high', configured: true, streaming: false, authentication: 'cli-managed' }
  state.conversation = { serviceStatus: { mode: 'remote', cadProvider: provider } }
  context.cadAgent.waitForRun = async ({ onProgress }) => {
    onProgress({ runId: 'cad_test', revision: 1, stage: 'planning', provider: { ...provider, attempts: 1 } })
    assert.equal(state.conversation.status.name, 'codex-cli')
    return result({ provider: { ...provider, attempts: 2 } })
  }
  assert.equal(await context.send('恢复任务', [], { resumeTask: { runId: 'cad_test', revision: 1 } }), true)
  assert.equal(state.model.agentRun.provider.model, 'gpt-6-astra')
  assert.equal(state.conversation.status.attempts, 2)
})

test('the actual upload and send handlers use the CAD agent for blank, legacy and manually corrected projects', async () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const start = app.indexOf('  const sendAiConversation = async (text, filesInput = []) => {')
  const router = app.slice(start, app.indexOf('    const requestId = aiRequestRef.current + 1', start)) + '\n  }'
  for (const model of [{ kind: '' }, { kind: 'bracket', baseThickness: 8 }, { kind: 'arched_clevis_support', baseThickness: 9 }]) {
    const { state, context } = appCadHarness(model)
    const originalModel = structuredClone(model)
    const job = { status: model.kind === 'bracket' ? 'error' : 'idle', requiresFileReselection: model.kind === 'bracket', fileMeta: { name: 'previous.jpg' }, evidence: model.kind === 'bracket' ? { status: 'candidate' } : null }
    context.setDrawingJob(job)
    context.currentSnapshotRef.current = { model, drawingJob: job, generation: null, messages: [] }
    context.shouldUseCadAgent = shouldUseCadAgent
    context.api = { recognizeDrawing: () => { throw new Error('new uploads must not use legacy recognition') }, aiConversationStream: () => { throw new Error('new uploads must not use legacy conversation') } }
    const requests = []
    context.cadAgent.run = async (request) => { requests.push(request); return result({ runId: 'fresh-drawing-run' }) }
    vm.runInContext(router, context)
    const file = new File(['unmodified source bytes'], 'new-original.jpg', { type: 'image/jpeg' })
    context.attach([file])
    assert.equal(state.model, model)
    assert.deepEqual(model, originalModel)
    context.chatAttachments = state.attachments
    context.prompt = ''
    await context.runGenerate()
    assert.equal(requests.length, 1)
    assert.equal(requests[0].files[0], file)
    assert.equal(requests[0].modelState.kind, '')
    assert.equal(requests[0].history.length, 0)
    assert.match(requests[0].message, /建立可编辑的三维实体.*核对尺寸.*发现差异请修正/)
    assert.doesNotMatch(requests[0].message, /bracket|arched|baseThickness|Ø30/)
    assert.equal(state.model.kind, 'feature_model')
    assert.equal(state.job.evidence, null)
    if (model.kind) assert.deepEqual(ProjectStore.getActiveFile(state.store).versions[0].snapshot.model, originalModel)
  }
})

test('the primary confirmation handler rechecks a blocked server version instead of repeating a rejected confirmation', async () => {
  const model = cadModelFromResult(result({ status: 'ready', deliveryBlockedReason: '旧检查记录需重新复核。' }))
  const { state, context } = appCadHarness(model)
  const calls = []
  context.cadAgent.confirm = async () => { throw new Error('must not repeat blocked confirmation') }
  context.cadAgent.run = async (request) => { calls.push(request); return result({ runId: 'rechecked-run' }) }
  assert.equal(await context.confirm(), true)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].files.length, 0)
  assert.equal(calls[0].modelState.agentRun.runId, 'cad_test')
  assert.equal(calls[0].modelState.agentRun.revision, 1)
  assert.match(calls[0].message, /重新检查.*原图.*实体投影/)
  assert.equal(state.model.agentRun.runId, 'rechecked-run')
  assert.equal(state.model.agentRun.status, 'review_required')
  assert.equal(state.model.agentRun.deliveryBlockedReason, undefined)
  assert.equal(state.generation.validation.productionReady, false)
})

test('explicit legacy remodeling archives the previous draft and sends isolated original files to the CAD agent', async () => {
  for (const reuseLiveFile of [true, false]) {
    const legacy = { kind: 'bracket', name: '旧图纸候选', baseLength: 110, baseThickness: 8, holeDiameter: 30 }
    const { state, context } = appCadHarness(legacy)
    const file = new File(['original drawing bytes'], 'original.jpg', { type: 'image/jpeg' })
    const job = { status: 'ready', evidence: { status: 'candidate', recipeId: 'bracket_support_v1' }, fileMeta: { name: file.name }, file: reuseLiveFile ? file : null }
    context.setDrawingJob(job)
    context.currentSnapshotRef.current = { model: legacy, drawingJob: job, generation: null, messages: [] }
    const requests = []
    context.cadAgent.run = async (request) => { requests.push(request); return result() }
    if (!reuseLiveFile) {
      assert.equal(await context.remodel(), false)
      assert.equal(requests.length, 0)
      assert.equal(state.model, legacy)
      assert.equal(ProjectStore.getActiveFile(state.store).versions.length, 0)
    }
    await context.remodel(reuseLiveFile ? undefined : [file])
    assert.equal(requests.length, 1)
    assert.equal(requests[0].files[0], file)
    assert.equal(requests[0].modelState.kind, '')
    assert.equal(requests[0].modelState.baseThickness, undefined)
    assert.equal(requests[0].modelState.agentRun, undefined)
    assert.equal(requests[0].history.length, 0)
    assert.doesNotMatch(requests[0].message, /110|bracket_support|Ø30/)
    assert.equal(state.model.kind, 'feature_model')
    assert.equal(state.job.file, file)
    const versions = ProjectStore.getActiveFile(state.store).versions
    assert.equal(versions.length, 1)
    assert.deepEqual(versions[0].snapshot.model, legacy)
    assert.equal(versions[0].snapshot.drawingJob.evidence.recipeId, 'bracket_support_v1')
    assert.equal(versions[0].snapshot.drawingJob.fileMeta.name, file.name)
  }
})

test('retrying a failed uploaded drawing resumes its durable draft; explicit replacement saves the old model once', async () => {
  const { state, context } = appCadHarness({ kind: '' })
  const calls = []
  context.cadAgent.run = async (request) => { calls.push(request); return result({ status: calls.length === 1 ? 'failed' : 'review_required', runId: `run-${calls.length}` }) }
  const files = [new File(['drawing'], 'original.jpg', { type: 'image/jpeg' })]
  await context.send('请读取这张图纸', files)
  assert.equal(state.lastTurn.cadRun.runId, 'run-1')
  await context.retry()
  assert.equal(calls[1].files.length, 0)
  assert.equal(calls[1].modelState.agentRun.runId, 'run-1')
  assert.equal(calls[1].modelState.cadPlan.name, '双孔板')
  assert.equal(ProjectStore.getActiveFile(state.store).versions.length, 0)

  const savedModel = state.model
  context.currentSnapshotRef.current = { model: savedModel, generation: state.generation, messages: state.messages }
  await context.send('请重新建立这张新图', files)
  assert.equal(calls[2].files.length, 1)
  assert.equal(calls[2].modelState.agentRun, undefined)
  const versions = ProjectStore.getActiveFile(state.store).versions
  assert.equal(versions.length, 1)
  assert.equal(versions[0].snapshot.model.agentRun.runId, 'run-2')
  const once = ProjectStore.saveBeforeDrawingReplacement(state.store, state.store.activeProjectId, state.store.activeFileId, context.currentSnapshotRef.current)
  assert.equal(ProjectStore.getActiveFile(once).versions.length, 1)
  assert.equal(ProjectStore.getFileSnapshot(ProjectStore.restoreFileVersion(once, once.activeProjectId, once.activeFileId, versions[0].id)).model.agentRun.runId, 'run-2')
})

test('confirmation blocks sending, attachments, new conversations and project switches synchronously', async () => {
  const { state, context } = appCadHarness()
  let resolve
  context.cadAgent.confirm = () => new Promise((done) => { resolve = done })
  context.cadAgent.run = () => { throw new Error('another run must not start during confirmation') }
  const confirming = context.confirm()
  assert.ok(context.cadConfirmRef.current)
  assert.equal(await context.send('板宽改为84'), false)
  await context.runGenerate()
  context.attach([new File(['replacement'], 'new.jpg', { type: 'image/jpeg' })])
  context.newConversation()
  context.createProject('不应创建')
  context.openProjectFile(context.activeFile)
  context.selectLocalProject('other')
  assert.equal(state.prompt, '把板宽改为84')
  assert.deepEqual(state.attachments, [])
  assert.deepEqual(state.switches, [])
  resolve(result({ status: 'ready', revision: 2 }))
  assert.equal(await confirming, true)
  assert.equal(state.model.agentRun.revision, 2)
  assert.equal(context.cadConfirmRef.current, null)
  assert.equal(state.accepting, false)
})

test('late or mismatched confirmation responses never overwrite another run or unlock delivery', async () => {
  for (const mode of ['current-run-changed', 'wrong-run', 'wrong-revision']) {
    const { state, context } = appCadHarness()
    let resolve
    context.cadAgent.confirm = () => new Promise((done) => { resolve = done })
    const confirming = context.confirm()
    if (mode === 'current-run-changed') { state.model = cadModelFromResult(result({ runId: 'new-run' })); context.modelRef.current = state.model }
    const expectedModel = state.model
    resolve(result({ status: 'ready', runId: mode === 'wrong-run' ? 'different-run' : 'cad_test', revision: mode === 'wrong-revision' ? 5 : 2 }))
    assert.equal(await confirming, false, mode)
    assert.equal(state.model, expectedModel, mode)
    assert.equal(state.model.agentRun.status, 'review_required')
    assert.equal(context.cadConfirmRef.current, null)
    assert.equal(state.accepting, false)
  }
})

test('a disconnected started run recovers its terminal result through GET without uploading or starting again', async () => {
  const { state, context } = appCadHarness({ kind: '' })
  let runs = 0, reads = 0
  context.cadAgent.run = async ({ onEvent }) => {
    runs += 1
    onEvent('progress', { stage: 'started', status: 'running', runId: 'cad_test', revision: 1 })
    assert.equal(state.job.cadTask.runId, 'cad_test')
    const saved = recoverWorkspaceSnapshot({ model: null, generation: null, drawingJob: state.job })
    assert.equal(saved.drawingJob.requiresFileReselection, false)
    assert.equal(saved.drawingJob.cadTask.status, 'running')
    throw new Error('连接断开')
  }
  context.cadAgent.waitForRun = async ({ runId }) => { reads += 1; assert.equal(runId, 'cad_test'); return result() }
  await context.send('请读图', [new File(['drawing'], 'drawing.jpg', { type: 'image/jpeg' })])
  assert.equal(runs, 1)
  assert.equal(reads, 1)
  assert.equal(state.job.cadTask, null)
  assert.equal(state.model.agentRun.status, 'review_required')
  assert.equal(state.lastTurn.cadRun.runId, 'cad_test')
})

test('saved background geometry repair retains its workflow phase across refresh and resumed planning', async () => {
  const first = appCadHarness({ kind: '' })
  first.context.cadAgent.run = async ({ onEvent }) => {
    onEvent('progress', { stage: 'started', runId: 'cad_test', revision: 1, status: 'running' })
    onEvent('progress', { stage: 'geometry_repair', message: '正在修正投影差异' })
    onEvent('progress', { stage: 'planning', message: '正在继续推理' })
    first.context.chatAbortRef.current.abort()
    throw new DOMException('Stopped', 'AbortError')
  }
  await first.context.send('请按图纸建模')
  const saved = recoverWorkspaceSnapshot({ model: first.state.model, generation: null, drawingJob: first.state.job })
  assert.equal(saved.drawingJob.cadTask.progress.phase, 'generate')
  assert.equal(cadProgressFromEvent(saved.drawingJob.cadTask.progress).phase, 'generate')
  const resumed = appCadHarness(saved.model)
  resumed.context.setDrawingJob(saved.drawingJob)
  let reads = 0
  resumed.context.cadAgent.run = async () => { throw new Error('refresh must not start another run') }
  resumed.context.cadAgent.waitForRun = async ({ onProgress, runId }) => {
    reads += 1
    assert.equal(runId, 'cad_test')
    onProgress({ runId, revision: 1, status: 'running', stage: 'agent_working' })
    assert.equal(resumed.state.conversation.cadProgress.phase, 'generate')
    assert.equal(resumed.state.job.cadTask.progress.phase, 'generate')
    assert.equal(resumed.state.generation, null)
    return result({ status: 'failed', message: '仍有图纸差异' })
  }
  await resumed.context.retry()
  assert.equal(reads, 1)
  assert.equal(resumed.state.model.agentRun.status, 'failed')
  assert.equal(resumed.state.generation, null)
})

test('stopping the wait retains a recoverable background task and interrupted drafts can continue', async () => {
  const { state, context } = appCadHarness({ kind: '' })
  context.cadAgent.run = async ({ onEvent }) => {
    onEvent('progress', { stage: 'started', runId: 'cad_test', revision: 1, status: 'running' })
    context.chatAbortRef.current.abort()
    throw new DOMException('Stopped', 'AbortError')
  }
  context.cadAgent.waitForRun = async () => { throw new DOMException('Stopped', 'AbortError') }
  await context.send('继续设计')
  assert.equal(state.job.cadTask.paused, true)
  assert.equal(state.model.kind, '')
  context.cadAgent.waitForRun = async () => result({ status: 'interrupted', message: '服务器重启，草稿可续跑', artifacts: [] })
  await context.retry()
  assert.equal(state.job.cadTask, null)
  assert.equal(state.model.agentRun.status, 'failed')
  assert.equal(state.model.cadPlan.name, '双孔板')
  assert.equal(state.generation, null)
  assert.equal(cadRetryFiles(state.lastTurn, state.model, 'workspace-test').length, 0)
})

test('GET polling reports live progress, waits for a terminal result and honors abort', async (t) => {
  let reads = 0
  const provider = { mode: 'codex', name: 'codex-cli', model: 'gpt-6-astra', streaming: false }
  t.mock.method(cadAgent, 'getRun', async () => ++reads === 1 ? { status: 'running', runId: 'cad_test', revision: 1, provider, progress: { stage: 'agent_working', message: '正在推理' } } : result())
  const progress = []
  assert.equal((await cadAgent.waitForRun({ runId: 'cad_test', intervalMs: 0, onProgress: (event) => progress.push(event) })).status, 'review_required')
  assert.equal(progress[0].stage, 'agent_working')
  assert.equal(progress[0].provider, provider)
  reads = 0
  const controller = new AbortController()
  await assert.rejects(cadAgent.waitForRun({ runId: 'cad_test', signal: controller.signal, intervalMs: 10000, onProgress: () => controller.abort() }), { name: 'AbortError' })
  assert.equal(reads, 1)
})

test('automatic workspace recovery starts once after StrictMode cleanup and skips user-paused tasks', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const start = app.indexOf('  useEffect(() => {\n    // Defer one tick')
  const hook = app.slice(start, app.indexOf('\n  useEffect(', start + 1))
  const timers = new Map(), calls = []
  let setup, timerId = 0
  const context = vm.createContext({
    useEffect: (callback) => { setup = callback }, activeFile: { id: 'file-1' }, drawingJobRef: { current: { cadTask: { status: 'running', runId: 'cad_test', revision: 1, prompt: '原要求' } } },
    chatAbortRef: { current: null }, cadConfirmRef: { current: null }, sendCadConversation: (...args) => calls.push(args),
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId }, clearTimeout: (id) => timers.delete(id),
  })
  vm.runInContext(hook, context)
  setup()()
  setup()
  assert.equal(timers.size, 1)
  for (const callback of timers.values()) callback()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][2].resumeTask.runId, 'cad_test')
  timers.clear()
  context.drawingJobRef.current.cadTask.paused = true
  setup()
  for (const callback of timers.values()) callback()
  assert.equal(calls.length, 1)
})

test('failed source reading retries the saved run directly without a plan, reupload or confirmation', async () => {
  const initial = cadModelFromResult(result({ runId: 'cad_source_failed', status: 'failed', plan: null, artifacts: [], questions: [],
    sourceFiles: [{ id: 'stored-original', filename: 'drawing.jpg' }], sourceTranscription: { status: 'failed', annotations: [] } }))
  const { state, context } = appCadHarness(initial)
  const requests = []
  context.cadAgent.run = async (request) => { requests.push(request); return result({ runId: 'retry-result' }) }
  context.cadAgent.confirm = async () => { throw new Error('an unfinished source must not be confirmed') }
  context.document = { querySelector: () => { throw new Error('retry must send, not focus an empty input') } }
  assert.equal(cadPrimaryAction(initial).label, '重试读取')
  assert.match(validateCadPlan(initial).errors[0].message, /重试读取/)
  assert.doesNotMatch(validateCadPlan(initial).errors[0].message, /回答.*问题|补全.*尺寸/)
  assert.equal(await context.confirm(), true)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].files.length, 0)
  assert.equal(requests[0].modelState.agentRun.runId, 'cad_source_failed')
  assert.equal(requests[0].modelState.agentRun.revision, 1)
  assert.equal(Object.hasOwn(requests[0].modelState, 'cadPlan'), false)
  assert.match(requests[0].message, /已保存的原图/)
  assert.match(requests[0].message, /不更改尺寸或设计要求/)
  assert.equal(state.model.agentRun.runId, 'retry-result')
})

test('failed partial geometry and empty-question needs_input retry the saved draft instead of asking imaginary questions', async () => {
  for (const status of ['failed', 'needs_input']) {
    const initial = cadModelFromResult(result({ status, plan, questions: [' ', ''], artifacts: [] }))
    const { context } = appCadHarness(initial)
    const requests = []
    context.cadAgent.run = async (request) => { requests.push(request); return result({ runId: 'retry-draft' }) }
    context.cadAgent.confirm = async () => { throw new Error('partial geometry is not delivery ready') }
    context.document = { querySelector: () => { throw new Error('no question exists') } }
    assert.equal(cadPrimaryAction(initial).label, '重试建模')
    await context.confirm()
    assert.equal(requests.length, 1)
    assert.equal(requests[0].modelState.cadPlan.parameters.thickness.value, null)
    assert.equal(requests[0].modelState.agentRun.runId, initial.agentRun.runId)
    assert.equal(requests[0].files.length, 0)
    assert.doesNotMatch(requests[0].message, /重新上传|厚度是|默认/)
  }
})

test('a genuine question keeps the answer action and never starts an automatic retry', async () => {
  for (const status of ['needs_input', 'failed']) {
    const initial = cadModelFromResult(result({ status, plan: null, questions: ['背面标注没有拍到，板厚是多少？'] }))
    const { state, context } = appCadHarness(initial)
    let focuses = 0
    context.document = { querySelector: () => ({ focus: () => { focuses += 1 } }) }
    context.cadAgent.run = async () => { throw new Error('wait for the actual answer') }
    context.cadAgent.confirm = async () => { throw new Error('missing information cannot be confirmed') }
    assert.equal(cadPrimaryAction(initial).kind, 'answer')
    await context.confirm()
    assert.equal(focuses, 1)
    assert.match(state.notices[0][0], /板厚是多少/)
  }
})

test('the top model action routes failed source reading to the shared retry handler', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const start = app.indexOf('  const primaryAction = () => {')
  const source = app.slice(start, app.indexOf('  // A queued drawing', start))
  const model = cadModelFromResult(result({ status: 'failed', plan: null, sourceTranscription: { status: 'failed' } }))
  let retries = 0
  const context = vm.createContext({ model, featureModel: true, agentStatus: 'failed', chatAttachments: [], productionReady: false,
    acceptDrawingData: () => { retries += 1 }, document: { querySelector: () => { throw new Error('must retry immediately') } } })
  vm.runInContext(`${source}\nprimaryAction()`, context)
  assert.equal(retries, 1)
})
