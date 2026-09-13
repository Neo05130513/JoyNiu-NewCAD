import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as ProjectStore from './projectStore.js'
import { describeModelEditing } from './modelEditing.js'
import { cadPlanSignature, cadGenerationIsCurrent, isFeatureModel } from './cadAgentState.js'
import { validateModelParameters } from './modelValidation.js'
import { createManualFeatureReference, applyManualFeatureReference, manualFeatureRecordMatchesReference } from './manualFeatureReference.js'

const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
function section(start, end) {
  const from = app.indexOf(start), to = app.indexOf(end, from + start.length)
  assert.ok(from >= 0 && to > from, `Missing handler boundary: ${start}`)
  return app.slice(from, to)
}
const bridgeCode = section('  const modelEditing = describeModelEditing(', '  const canSwitch = () => {')
function fixture() {
  const plan = { version: 'cad-plan-v1', name: '原 AI 长方体', units: 'mm', parameters: {}, features: [{ id: 'body', op: 'box', size: [40, 20, 10], origin: [0, 0, 0] }], result: 'body' }
  const runId = `cad_${'a'.repeat(32)}`
  return { model: { kind: 'feature_model', name: plan.name, cadPlan: plan, agentRun: { runId, revision: 2, status: 'ready' } },
    generation: { kind: 'feature_model', runId, revision: 2, planSignature: cadPlanSignature(plan), validation: { productionReady: true } },
    drawingJob: { status: 'ready' }, prompt: '未发送的原 AI 修改要求', messages: [{ role: 'ai', text: '原 AI 核验记录' }] }
}
function harness() {
  const snapshot = fixture(), notices = []
  let store = ProjectStore.loadProjectStore({ getItem: () => null })
  store = ProjectStore.updateFileSnapshot(store, store.activeProjectId, store.activeFileId, snapshot)
  const state = { store, editingTarget: null, mode: '3D 建模', showOriginal: true, flushes: 0 }
  const source = { accountKey: 'alice', projectId: store.activeProjectId, fileId: store.activeFileId }
  const context = vm.createContext({ ProjectStore, describeModelEditing, createManualFeatureReference, applyManualFeatureReference,
    accountKey: 'alice', workspaceStore: store, activeFile: ProjectStore.getActiveFile(store), editingTarget: null,
    model: snapshot.model, generation: snapshot.generation, currentSnapshotRef: { current: ProjectStore.getActiveFile(store).snapshot },
    setActiveCase: () => {}, setActiveMode: value => { state.mode = value }, setShowOriginalModel: value => { state.showOriginal = value },
    setEditingTarget: value => { state.editingTarget = typeof value === 'function' ? value(state.editingTarget) : value; context.editingTarget = state.editingTarget },
    showToast: (...args) => notices.push(args), readableError: error => error.message,
    flushWorkspace: () => {
      state.flushes++
      state.store = ProjectStore.updateFileSnapshot(state.store, state.store.activeProjectId, state.store.activeFileId, context.currentSnapshotRef.current)
      return state.store
    },
    commitStore: next => { state.store = next; context.workspaceStore = next },
    restoreWorkspace: (next, mode) => { state.store = next; state.mode = mode; context.currentSnapshotRef.current = ProjectStore.getActiveFile(next).snapshot },
  })
  vm.runInContext(`${bridgeCode}\nObject.assign(globalThis,{open:openModelEditor,save:saveEditedModel,back:returnFromModelEditor})`, context)
  const savedRecord = (revision, status = 'built') => ({ id: 'feature_linked', name: '手工长方体', fileId: source.fileId, revision, status,
    sourceRun: { runId: snapshot.model.agentRun.runId, revision: 2 }, artifacts: {} })
  return { context, state, source, snapshot, notices, savedRecord }
}

test('opening model editing binds the source and exact independent draft without changing AI approval', () => {
  const { context, state, source, snapshot } = harness()
  context.open()
  assert.equal(state.mode, '特征编辑'); assert.equal(state.flushes, 1)
  assert.equal(state.editingTarget.fileId, source.fileId); assert.equal(state.editingTarget.projectId, source.projectId)
  assert.deepEqual(state.editingTarget.initialDraft.plan, snapshot.model.cadPlan)
  assert.deepEqual(state.editingTarget.initialDraft.sourceRun, { runId: snapshot.model.agentRun.runId, revision: 2 })
  assert.equal(state.editingTarget.reference, null)
  assert.deepEqual(ProjectStore.getActiveFile(state.store).snapshot.model, snapshot.model)
  assert.deepEqual(ProjectStore.getActiveFile(state.store).snapshot.generation, snapshot.generation)
})

test('first draft save links the source and reopening retains the same editor session and server identity', () => {
  const { context, state, snapshot, savedRecord } = harness()
  context.open(); const source = state.editingTarget, key = source.initialPlanKey
  context.save(savedRecord(1, 'draft'), source)
  const reference = ProjectStore.getActiveFile(state.store).snapshot.manualFeatureReference
  assert.equal(reference.featureId, 'feature_linked'); assert.equal(reference.status, 'draft')
  assert.equal(state.showOriginal, false); assert.equal(state.editingTarget.savedFeatureId, 'feature_linked')
  assert.deepEqual(context.currentSnapshotRef.current.model, snapshot.model)
  assert.deepEqual(context.currentSnapshotRef.current.generation, snapshot.generation)
  context.open(reference)
  assert.equal(state.editingTarget.initialPlanKey, key, 'Returning to an unsaved editor must retain its cached draft')
  assert.equal(state.editingTarget.reference.featureId, 'feature_linked')
  assert.equal(state.editingTarget.reference.revision, 1)
})

test('late older save callbacks cannot bypass reference protection through the live autosave snapshot', () => {
  const { context, state, savedRecord } = harness()
  context.open(); const source = state.editingTarget
  context.save(savedRecord(4), source)
  context.save(savedRecord(3, 'draft'), source)
  assert.equal(ProjectStore.getActiveFile(state.store).snapshot.manualFeatureReference.revision, 4)
  assert.equal(context.currentSnapshotRef.current.manualFeatureReference.revision, 4, 'A rejected callback must not poison the next autosave')
  context.flushWorkspace()
  assert.equal(ProjectStore.getActiveFile(state.store).snapshot.manualFeatureReference.revision, 4)
})

test('a build finishing after project navigation writes its captured source without replacing the active draft', () => {
  const { context, state, source, savedRecord } = harness()
  context.open(); const target = state.editingTarget
  const elsewhere = ProjectStore.createProject(state.store, { name: '其他项目' })
  context.commitStore(elsewhere)
  context.currentSnapshotRef.current = { ...ProjectStore.getActiveFile(elsewhere).snapshot, prompt: '另一个文件未发送的文字' }
  context.save(savedRecord(2), target)
  assert.equal(state.store.activeFileId, elsewhere.activeFileId)
  assert.equal(ProjectStore.getActiveFile(state.store).snapshot.prompt, '另一个文件未发送的文字')
  assert.equal(ProjectStore.getActiveFile(state.store).snapshot.manualFeatureReference, undefined)
  assert.equal(context.currentSnapshotRef.current.manualFeatureReference, undefined)
  assert.equal(ProjectStore.getProjectFile(state.store, source.projectId, source.fileId).snapshot.manualFeatureReference.featureId, 'feature_linked')
})

test('foreign source references and old-account save callbacks cannot open or alter the current project', () => {
  const { context, state, source, notices, savedRecord } = harness()
  const reference = createManualFeatureReference(savedRecord(2), source)
  context.open({ ...reference, accountKey: 'bob' })
  context.open({ ...reference, sourceFileId: 'different-file' })
  assert.equal(state.editingTarget, null); assert.equal(state.flushes, 0); assert.equal(notices.length, 2)
  context.save(savedRecord(2), { ...source, accountKey: 'bob' })
  assert.equal(state.flushes, 0); assert.equal(ProjectStore.getActiveFile(state.store).snapshot.manualFeatureReference, undefined)
})

test('explicit AI text and attachment entry reveal chat while keeping the saved manual reference', () => {
  const textCode = section('  const startTextDesign = ', '  // Keep unfinished tool drafts')
  const attachmentCode = section('  const attachDrawingToConversation = ', '  const remodelLegacyDrawing = ')
  const state = { showOriginal: false, prompt: '', attachments: [], mode: '首页' }
  const manualReference = { featureId: 'feature_saved', revision: 2 }
  const context = vm.createContext({ activeFile: { id: 'file_a', type: '零件', snapshot: { manualFeatureReference: manualReference } },
    isGenerating: false, isAccepting: false, chatAbortRef: { current: null }, cadConfirmRef: { current: null }, drawingJobRef: { current: null },
    normalizeFilesInput: files => files, setShowOriginalModel: value => { state.showOriginal = value },
    setPrompt: value => { state.prompt = value }, setChatAttachments: value => { state.attachments = value },
    setActiveMode: value => { state.mode = value }, showToast: () => {},
    createFile: () => assert.fail('An existing source file must not be duplicated'),
  })
  vm.runInContext(`${textCode}\n${attachmentCode}\nObject.assign(globalThis,{start:startTextDesign,attach:attachDrawingToConversation})`, context)
  context.start('把原 AI 模型再加一个孔')
  assert.equal(state.showOriginal, true, 'New text must reveal the AI composer instead of the manual-result panel')
  assert.equal(state.prompt, '把原 AI 模型再加一个孔')
  state.showOriginal = false
  const original = new File(['original bytes'], 'next.dxf', { type: 'application/dxf' })
  context.attach([original], '这张图的厚度是 8 mm')
  assert.equal(state.showOriginal, true, 'An accepted attachment must reveal the send action')
  assert.equal(state.attachments[0], original); assert.equal(state.prompt, '这张图的厚度是 8 mm')
  assert.equal(context.activeFile.snapshot.manualFeatureReference, manualReference)
  state.showOriginal = false
  context.attach([new File(['bad'], 'bad.txt', { type: 'text/plain' })], '不应应用')
  assert.equal(state.showOriginal, false, 'Rejected uploads must keep the selected manual version visible')
})

function exportHarness() {
  const base = harness(), { context, source, savedRecord } = base
  const record = savedRecord(7), reference = createManualFeatureReference(record, source)
  const snapshot = { ...context.currentSnapshotRef.current, manualFeatureReference: reference,
    generation: { ...context.currentSnapshotRef.current.generation, artifacts: [{ id: 'ai-step', format: 'step' }] } }
  const calls = [], downloads = []
  Object.assign(context, { AbortController, manualFeatureRecordMatchesReference, validateModelParameters, cadGenerationIsCurrent, isFeatureModel,
    workspaceIdRef: { current: `${source.projectId}:${source.fileId}` }, modelInteractionRevisionRef: { current: 1 },
    accountTokenRef: { current: 'current-token' }, resourceDownloadsRef: { current: new Set() }, showOriginalModel: false,
    currentSnapshotRef: { current: snapshot }, downloadBlob: (...args) => downloads.push(args),
    directFeatureClient: {
      get: async (token, id, revision, signal) => { calls.push(['manual-get', token(), id, revision, signal]); return record },
      download: async (token, value, format, signal) => { calls.push(['manual-download', token(), value.id, value.revision, format, signal]); return new Blob([`MANUAL r${value.revision} ${format}`], { type: format === 'step' ? 'application/step' : 'model/gltf-binary' }) },
    },
    cadAgent: { downloadArtifact: async request => { calls.push(['ai-download', request.runId, request.revision, request.format]); return { blob: new Blob(['ORIGINAL AI STEP']), mimeType: 'application/step' } } },
  })
  const gates = section('function productionArtifactsAvailable(generation) {', '\nconst evidenceAcceptedForPreview')
  const code = section('  const exportFile = async ', '  const downloadProjectFile = ')
  vm.runInContext(`${gates}\n${code}\nglobalThis.exportFile = exportFile`, context)
  return { ...base, record, reference, snapshot, calls, downloads }
}

test('project exports read and verify the precise manual revision before downloading STEP or GLB', async () => {
  for (const format of ['step', 'glb']) {
    const { context, calls, downloads, snapshot } = exportHarness()
    // Project-row exports must keep their manual version even while another
    // view has explicitly selected its original AI model.
    context.showOriginalModel = true
    await context.exportFile(format, {}, { name: '来源零件', type: '零件', snapshot })
    assert.deepEqual(calls.map(call => call.slice(0, call[0] === 'manual-get' ? 4 : 5)), [
      ['manual-get', 'current-token', 'feature_linked', 7],
      ['manual-download', 'current-token', 'feature_linked', 7, format],
    ])
    assert.equal(calls[0][4], calls[1][5], 'Read and download share the same cancellation scope')
    assert.equal(await downloads[0][0].text(), `MANUAL r7 ${format}`)
    assert.equal(downloads[0][1], `来源零件-手工-r7.${format}`)
    assert.equal(context.resourceDownloadsRef.current.size, 0)
  }
})

test('manual drafts and foreign-account references block export without falling back to old AI files', async () => {
  for (const patch of [{ status: 'draft' }, { accountKey: 'bob' }]) {
    const { context, snapshot, calls, downloads, notices } = exportHarness()
    snapshot.manualFeatureReference = { ...snapshot.manualFeatureReference, ...patch }
    await context.exportFile('step')
    assert.deepEqual(calls, []); assert.deepEqual(downloads, [])
    assert.match(notices.at(-1)[0], /来源账号.*重建后再导出/)
    assert.equal(notices.at(-1)[1], 'error')
  }
})

test('a mismatched fetched manual revision is rejected before any manual or AI download', async () => {
  const { context, record, calls, downloads, notices } = exportHarness()
  context.directFeatureClient.get = async (_token, id, revision) => { calls.push(['manual-get', id, revision]); return { ...record, revision: 8 } }
  await context.exportFile('step')
  assert.deepEqual(calls, [['manual-get', 'feature_linked', 7]])
  assert.deepEqual(downloads, []); assert.match(notices.at(-1)[0], /手工版本与项目引用不一致/)
  assert.equal(context.resourceDownloadsRef.current.size, 0)
})

test('JSON export preserves both the original AI snapshot and saved manual reference without geometry requests', async () => {
  const { context, snapshot, calls, downloads, reference } = exportHarness()
  await context.exportFile('json')
  assert.deepEqual(calls, []); assert.equal(downloads.length, 1)
  const backup = JSON.parse(downloads[0][0])
  assert.deepEqual(backup.snapshot.model, snapshot.model)
  assert.deepEqual(backup.snapshot.generation, snapshot.generation)
  assert.deepEqual(backup.snapshot.manualFeatureReference, reference)
  assert.equal(backup.snapshot.model.agentRun.status, 'ready', 'Original confirmation remains attached only to its original model')
})

test('explicitly choosing the original model exports its AI revision without calling manual endpoints', async () => {
  const { context, calls, downloads, snapshot } = exportHarness()
  context.showOriginalModel = true
  await context.exportFile('step')
  assert.deepEqual(calls, [['ai-download', snapshot.model.agentRun.runId, 2, 'step']])
  assert.equal(await downloads[0][0].text(), 'ORIGINAL AI STEP')
  assert.doesNotMatch(downloads[0][1], /手工/)
})
