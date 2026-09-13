import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { cadGenerationFromResult, cadGenerationIsCurrent, cadModelFromResult, cadFailureSummary, cadPrimaryAction, cadRetryMessage, cadRunPresentation } from './cadAgentState.js'
import { generationMatchesModel } from './viewerState.js'
import { createProject, loadProjectStore, getFileSnapshot, recoverWorkspaceSnapshot, sanitizeWorkspaceSnapshot, PROJECT_STORE_KEY } from './projectStore.js'

const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const payload = (errorCode = 'invalid_json') => ({
  runId: 'cad_review', revision: 2, status: 'failed',
  message: '实体已生成，但独立图纸复核未完成。结果与草稿已保存，可继续复核。', questions: [],
  plan: { version: 'cad-plan-v1', name: '复核草稿', units: 'mm', parameters: { width: { value: 40 } }, features: [{ id: 'body', op: 'box', size: ['width', 20, 8] }], result: 'body' },
  inspection: { valid: true, kernelBacked: true, engine: 'cadquery-occt', solidCount: 1, productionReady: false },
  drawingReview: { status: 'uncertain', source: 'independent_drawing_review', humanConfirmed: false,
    planHash: 'current-execution-hash', observations: [], differences: [], questions: [],
    independentReview: { status: 'uncertain', source: 'independent_drawing_review', errorCode,
      planHash: 'current-execution-hash', observations: [], differences: [], questions: [] } },
  projectionComparison: { status: 'uncertain', views: [{ view: 'front', status: 'uncertain' }] },
  artifacts: [
    { format: 'step', url: '/api/v1/cad-agent/runs/cad_review/2/artifacts/step?access=signed' },
    { format: 'glb', url: '/api/v1/cad-agent/runs/cad_review/2/artifacts/glb?access=signed' },
    { format: 'svg', view: 'front', url: '/api/v1/cad-agent/runs/cad_review/2/artifacts/view-front?access=signed' },
    { format: 'svg', view: 'top', url: '/api/v1/cad-agent/runs/older/1/artifacts/view-top' },
  ],
})

test('timeout and invalid JSON from independent review retain the current real draft without approving delivery', () => {
  for (const error of ['timeout', 'invalid_json', 'independent_review_failed']) {
    const result = payload(error), model = cadModelFromResult(result)
    const generation = cadGenerationFromResult(result, model.cadPlan)
    assert.equal(model.agentRun.status, 'failed')
    assert.equal(model.agentRun.drawingReview.humanConfirmed, false)
    assert.equal(generation.reviewIncomplete, true)
    assert.equal(generation.previewOnly, true)
    assert.equal(generation.validation.productionReady, false)
    assert.deepEqual(generation.artifacts.map((item) => item.format), ['glb', 'svg'])
    assert.match(generation.artifacts[0].downloadUrl, /access=signed$/)
    assert.equal(generationMatchesModel(model, generation), true)
    assert.equal(cadGenerationIsCurrent({ ...model, agentRun: { ...model.agentRun, revision: 3 } }, generation), false)
    assert.equal(cadRunPresentation(model.agentRun).confirmed, false)
    assert.equal(cadRunPresentation(model.agentRun).title, '图纸复核未完成')
    assert.equal(cadPrimaryAction(model).kind, 'retry')
    assert.equal(cadPrimaryAction(model).label, '继续图纸复核')
    assert.match(cadRetryMessage(model), /不得跳过复核或直接确认交付/)
  }
})

test('failed drafts require valid current geometry, an explicit review call error and no drawing mismatch', () => {
  const cases = [
    (value) => { value.inspection = null },
    (value) => { value.inspection.valid = false },
    (value) => { value.inspection.kernelBacked = false },
    (value) => { value.inspection.engine = 'faceted-fallback' },
    (value) => { value.inspection.solidCount = 0 },
    (value) => { value.inspection.solidCount = Infinity },
    (value) => { value.plan.parameters.width.value = null },
    (value) => { value.plan = null },
    (value) => { value.dirty = true },
    (value) => { value.stale = true },
    (value) => { value.drawingReview.status = 'mismatch' },
    (value) => { value.drawingReview.independentReview.status = 'mismatch' },
    (value) => { value.drawingReview.differences = ['缺少孔'] },
    (value) => { value.drawingReview.independentReview.differences = ['孔位不符'] },
    (value) => { value.projectionComparison.status = 'mismatch' },
    (value) => { value.projectionComparison.views[0].status = 'mismatch' },
    (value) => { value.drawingReview.projectionComparison = { views: [{ status: 'mismatch' }] } },
    (value) => { value.drawingReview.independentReview.errorCode = '' },
    (value) => { value.drawingReview.source = 'self-review' },
    (value) => { value.drawingReview.independentReview.source = 'self-review' },
    (value) => { value.drawingReview.humanConfirmed = true },
    (value) => { delete value.drawingReview.planHash },
    (value) => { value.drawingReview.independentReview.planHash = 'older-plan' },
    (value) => { value.artifacts = [] },
    (value) => { value.artifacts[1].url = '/api/v1/cad-agent/runs/older/2/artifacts/glb' },
    (value) => { value.artifacts[1].url = '/api/v1/cad-agent/runs/cad_review/1/artifacts/glb' },
    (value) => { value.artifacts[1].url = '/unrelated?next=/cad-agent/runs/cad_review/2/artifacts/glb' },
    (value) => { value.artifacts[1].url = 'javascript:alert(1)' },
  ]
  for (const mutate of cases) {
    const value = payload(); mutate(value)
    assert.equal(cadGenerationFromResult(value, value.plan), null, String(mutate))
  }
  const value = payload()
  assert.equal(cadGenerationFromResult(value, { ...value.plan, result: 'changed-feature' }), null)
})

test('app applies a failed review result as an inspectable draft while preserving the failure and delivery lock', () => {
  const state = { model: null, generation: null, job: {} }
  const handlers = app.slice(app.indexOf('const applyCadResult ='), app.indexOf('  const sendCadConversation ='))
  const context = vm.createContext({ cadModelFromResult, cadFailureSummary, cadGenerationFromResult, modelRef: { current: null },
    setModel: (value) => { state.model = value }, setGeneration: (value) => { state.generation = value },
    setDrawingJob: (update) => { state.job = update(state.job) }, setActivePanel: () => {} })
  vm.runInContext(`${handlers}\nthis.applyResult = applyCadResult`, context)
  context.applyResult(payload(), {})
  assert.equal(state.job.status, 'error')
  assert.match(state.job.error, /图纸复核阶段未返回完整有效的结果/)
  assert.equal(state.model.agentRun.message, payload().message)
  assert.equal(state.model.agentRun.status, 'failed')
  assert.equal(generationMatchesModel(state.model, state.generation), true)
  const availability = app.slice(app.indexOf('function productionArtifactsAvailable('), app.indexOf('const evidenceAcceptedForPreview'))
  assert.equal(vm.runInNewContext(`${availability}; productionArtifactsAvailable(generation)`, { generation: state.generation }), false)
  const badge = app.match(/<div className=\{`model-context-badge.*?\/>\{(.*?)\}<\/div>/)[1]
  const text = vm.runInNewContext(badge, { pendingCadTask: false, featureModel: true, generation: state.generation, reviewIncompleteDraft: true })
  assert.equal(text, '实体草稿 · 图纸复核未完成')
  assert.doesNotMatch(text, /尚无实体|待生成|已验证/)
})

test('old null-generation snapshots recover a review-only draft when opened and after a storage reload', () => {
  const model = cadModelFromResult(payload())
  const snapshot = { model, generation: null, drawingJob: { status: 'error', error: payload().message } }
  const clean = sanitizeWorkspaceSnapshot(snapshot)
  assert.equal(clean.generation, null)
  const restored = recoverWorkspaceSnapshot(JSON.parse(JSON.stringify(clean)))
  assert.equal(restored.generation.reviewIncomplete, true)
  assert.equal(restored.generation.validation.productionReady, false)
  assert.equal(restored.model.agentRun.status, 'failed')
  assert.equal(restored.drawingJob.error, snapshot.drawingJob.error)
  const store = createProject({ projects: [], schemaVersion: 1 }, { name: '保存的失败复核', snapshot })
  const storage = { getItem: (key) => key === PROJECT_STORE_KEY ? JSON.stringify(store) : null }
  const reopened = getFileSnapshot(loadProjectStore(storage))
  assert.equal(generationMatchesModel(reopened.model, reopened.generation), true)
  assert.equal(reopened.generation.artifacts.some((item) => item.format === 'step'), false)
})

test('recovery never reconstructs edited, stale, interrupted, running or mismatched snapshots', () => {
  const fresh = () => ({ model: cadModelFromResult(payload()), generation: null, drawingJob: { status: 'error' } })
  const cases = [
    (snapshot) => { snapshot.model.agentRun.dirty = true },
    (snapshot) => { snapshot.model.agentRun.stale = true },
    (snapshot) => { snapshot.model.stale = true },
    (snapshot) => { snapshot.model.cadPlan.parameters.width.value = 60 },
    (snapshot) => { snapshot.model.agentRun.drawingReview.status = 'mismatch' },
    (snapshot) => { snapshot.drawingJob.interrupted = true },
    (snapshot) => { snapshot.drawingJob.status = 'generating' },
    (snapshot) => { snapshot.drawingJob.cadTask = { runId: 'new-operation', status: 'running' } },
  ]
  for (const mutate of cases) {
    const snapshot = fresh(); mutate(snapshot)
    assert.equal(recoverWorkspaceSnapshot(snapshot).generation, null, String(mutate))
  }
  const snapshot = fresh()
  snapshot.generation = { stale: true, artifacts: [] }
  assert.deepEqual(recoverWorkspaceSnapshot(snapshot).generation, snapshot.generation)
})

test('draft projections carry the incomplete-review label and cannot export STEP', () => {
  const panel = readFileSync(new URL('./CadAgentPanel.jsx', import.meta.url), 'utf8')
  const start = panel.indexOf('export function CadAgentDrawing(')
  const declarations = panel.slice(start, panel.indexOf('  return <section', start)).replace('export ', '')
  const context = vm.createContext({ cadGenerationIsCurrent, cadRunPresentation })
  vm.runInContext(`${declarations}return { title, description, canExport, artifacts }; }`, context)
  const result = payload(), model = cadModelFromResult(result), generation = cadGenerationFromResult(result, model.cadPlan)
  const display = context.CadAgentDrawing({ model, generation })
  assert.equal(display.title, '实体草稿 · 图纸复核未完成')
  assert.match(display.description, /完成复核并确认后才能交付/)
  assert.equal(display.artifacts.length, 1)
  assert.equal(display.canExport, false)
})
