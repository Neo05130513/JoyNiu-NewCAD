import test from 'node:test'
import assert from 'node:assert/strict'
import {
  PROJECT_STORE_KEY, loadProjectStore, persistProjectStore, createProject, createProjectFile,
  getActiveProject, getActiveFile, getProjectFile, getFileSnapshot, selectProject, selectProjectFile,
  updateFileSnapshot, saveFileVersion, restoreFileVersion, renameProject, renameProjectFile,
  sanitizeWorkspaceSnapshot, recoverWorkspaceSnapshot, projectStatistics,
} from './projectStore.js'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)) }
}
function fresh() { return loadProjectStore(memoryStorage()) }

test('new visitor gets one real independent project/file, without the old demo file list', () => {
  const store = loadProjectStore(memoryStorage(), { defaultProjects: [{ id: 'p1', name: 'demo' }, { id: 'p2', name: 'demo2' }], defaultFiles: [{ name: 'fake.pdf', type: '工程图' }] })
  assert.equal(store.projects.length, 1)
  assert.equal(getActiveProject(store).files.length, 1)
  assert.equal(getActiveFile(store).type, '零件')
  assert.equal(getActiveFile(store).snapshot.model.kind, 'shaft')
  assert.deepEqual(getActiveFile(store).versions, [])
})

test('projects and files retain independent snapshots when switched or edited', () => {
  let store = fresh()
  const firstProject = store.activeProjectId
  const firstFile = store.activeFileId
  const changed = getFileSnapshot(store)
  changed.model.outerDiameter = 36
  store = updateFileSnapshot(store, firstProject, firstFile, changed)
  store = createProject(store, { name: '项目 B' })
  assert.equal(getActiveFile(store).snapshot.model.outerDiameter, 24)
  const secondProject = store.activeProjectId
  const secondFile = store.activeFileId
  store = createProjectFile(store, secondProject, { name: '另一个零件', type: '零件' })
  const thirdFile = store.activeFileId
  const thirdSnapshot = getFileSnapshot(store)
  thirdSnapshot.model.length = 128
  store = updateFileSnapshot(store, secondProject, thirdFile, thirdSnapshot)
  store = selectProjectFile(store, secondProject, secondFile)
  assert.equal(getFileSnapshot(store).model.length, 70)
  store = selectProject(store, firstProject)
  assert.equal(getFileSnapshot(store).model.outerDiameter, 36)
  store = selectProject(store, secondProject)
  assert.equal(store.activeFileId, secondFile)
  assert.ok(getActiveFile(store).lastOpenedAt)
  assert.equal(getProjectFile(store, secondProject, thirdFile).snapshot.model.length, 128)
})

test('opening a target file returns its contents, and callers cannot mutate stored contents by reference', () => {
  let store = fresh()
  const project = store.activeProjectId
  const originalFile = store.activeFileId
  store = createProjectFile(store, project, { name: '设计说明', type: '文档', snapshot: { documentText: '孔径是 10 mm', model: null } })
  const documentId = store.activeFileId
  assert.equal(getFileSnapshot(store).documentText, '孔径是 10 mm')
  assert.equal(getFileSnapshot(store).model, null)
  assert.equal(getFileSnapshot(store, project, originalFile).model.outerDiameter, 24)
  const snapshot = getFileSnapshot(store, project, originalFile)
  snapshot.model.outerDiameter = 999
  assert.equal(getFileSnapshot(store, project, originalFile).model.outerDiameter, 24)
  assert.equal(getFileSnapshot(store, 'wrong-project', documentId), null)
})

test('saved versions are immutable deep snapshots and restoring only updates the working draft', () => {
  let store = fresh()
  const projectId = store.activeProjectId
  const fileId = store.activeFileId
  const snapshot = getFileSnapshot(store)
  snapshot.model.length = 85
  snapshot.assemblyItems = [{ id: 'item1', position: { x: 3 } }]
  store = saveFileVersion(store, projectId, fileId, { snapshot, note: '确认轴长' })
  const firstVersion = getActiveFile(store).versions[0]
  snapshot.model.length = 999
  snapshot.assemblyItems[0].position.x = 9
  assert.equal(firstVersion.snapshot.model.length, 85)
  assert.equal(firstVersion.snapshot.assemblyItems[0].position.x, 3)
  store = saveFileVersion(store, projectId, fileId, { snapshot })
  assert.equal(getActiveFile(store).versions[1].label, 'v02')
  const priorStore = store
  store = restoreFileVersion(store, projectId, fileId, firstVersion.id)
  assert.equal(getActiveFile(store).snapshot.model.length, 85)
  assert.equal(getActiveFile(priorStore).snapshot.model.length, 999)
  assert.equal(getActiveFile(store).versions.length, 2)
  assert.equal(getActiveFile(store).versions[1].snapshot.model.length, 999)
  const restored = getFileSnapshot(store)
  restored.assemblyItems[0].position.x = 20
  store = updateFileSnapshot(store, projectId, fileId, restored)
  assert.equal(getActiveFile(store).versions[0].snapshot.assemblyItems[0].position.x, 3)
})

test('persistence round trip keeps stable ids, selected file, documents, parameters and versions', () => {
  const storage = memoryStorage()
  let store = fresh()
  store = createProjectFile(store, store.activeProjectId, { name: '加工说明', type: '文档', snapshot: { documentText: '保留公差与表面处理说明。' } })
  store = saveFileVersion(store, store.activeProjectId, store.activeFileId, { note: '首版说明' })
  persistProjectStore(store, storage)
  const loaded = loadProjectStore(storage)
  assert.equal(loaded.activeProjectId, store.activeProjectId)
  assert.equal(loaded.activeFileId, store.activeFileId)
  assert.equal(getActiveFile(loaded).snapshot.documentText, '保留公差与表面处理说明。')
  assert.equal(getActiveFile(loaded).versions[0].id, getActiveFile(store).versions[0].id)
  assert.ok(storage.getItem(PROJECT_STORE_KEY))
})

test('legacy migration binds the only real model to f1 in selected project; other records are unavailable', () => {
  const storage = memoryStorage({
    'joyniu-projects': JSON.stringify([{ id: 'p1', name: 'A' }, { id: 'p2', name: 'B' }]),
    'joyniu-selected-project': 'B',
    'joyniu-files': JSON.stringify([{ id: 'f4', name: '说明', type: '文档' }, { id: 'f1', name: '当前轴', type: '零件', version: 'v04' }, { id: 'f2', name: '图面', type: '工程图' }, { id: 'f3', name: '组件', type: '装配体' }]),
    'joyniu-model': JSON.stringify({ name: '当前轴', kind: 'shaft', outerDiameter: 36, length: 85 }),
    'joyniu-messages': JSON.stringify([{ role: 'user', text: '把轴长设为85' }]),
  })
  let store = loadProjectStore(storage)
  assert.equal(store.activeProjectId, 'p2')
  assert.equal(store.projects.find((project) => project.id === 'p1').files.length, 0)
  assert.equal(getActiveFile(store).name, '当前轴')
  assert.equal(getActiveFile(store).snapshot.model.length, 85)
  assert.equal(getActiveFile(store).versions.length, 0, 'a fake v04 label must not become invented history')
  for (const file of getActiveProject(store).files.filter((item) => item.id !== store.activeFileId)) {
    assert.equal(file.contentUnavailable, true)
    assert.equal(file.snapshot.model, null)
    assert.equal(getFileSnapshot(store, 'p2', file.id), null)
    assert.strictEqual(saveFileVersion(store, 'p2', file.id), store)
  }
  persistProjectStore(store, storage)
  store = loadProjectStore(storage)
  assert.equal(getActiveFile(store).snapshot.model.outerDiameter, 36)
})

test('File/Blob, preview URLs, attachment bytes and credentials never persist', () => {
  const blob = new Blob(['private drawing'], { type: 'image/svg+xml' })
  Object.defineProperty(blob, 'name', { value: 'original.svg' })
  const clean = sanitizeWorkspaceSnapshot({
    model: { name: 'test' }, drawingJob: { file: blob, previewUrl: 'blob:preview', status: 'analyzing' },
    token: 'secret', platform: { token: 'another-secret' }, nested: { accessToken: 'also-secret', apiKey: 'key', imageBase64: 'bytes' },
    chatAttachments: [blob], generation: { artifacts: [{ downloadUrl: 'http://localhost:8011/artifact/glb' }] },
  })
  const text = JSON.stringify(clean)
  assert.equal(clean.drawingJob.fileMeta.name, 'original.svg')
  assert.equal(clean.drawingJob.file, null)
  assert.equal(clean.drawingJob.previewUrl, '')
  assert.equal(clean.chatAttachments.length, 0)
  assert.ok(!text.includes('secret'))
  assert.ok(!text.includes('bytes'))
  assert.ok(!text.includes('blob:'))
  assert.equal(clean.generation.artifacts[0].downloadUrl, 'http://localhost:8011/artifact/glb')
})

test('interrupted persisted work becomes an actionable error requiring source reselection', () => {
  const source = { drawingJob: { status: 'generating', fileMeta: { name: 'part.dwg' } }, generation: { pendingDrawing: true }, messages: [{ role: 'ai', status: 'streaming', text: '' }], aiConversation: { previousResponseId: 'old', turnStatus: 'streaming' }, isGenerating: true }
  const recovered = recoverWorkspaceSnapshot(source)
  assert.equal(source.drawingJob.status, 'generating')
  assert.equal(recovered.drawingJob.status, 'error')
  assert.equal(recovered.drawingJob.requiresFileReselection, true)
  assert.match(recovered.drawingJob.error, /重新选择原图/)
  assert.equal(recovered.generation.stale, true)
  assert.equal(recovered.generation.pendingDrawing, false)
  assert.equal(recovered.isGenerating, false)
  assert.equal(recovered.messages[0].status, 'interrupted')
  assert.equal(recovered.aiConversation.previousResponseId, '')
  const recoveredAgain = recoverWorkspaceSnapshot(recovered)
  assert.equal(recoveredAgain.drawingJob.requiresFileReselection, true)
  assert.equal(recoveredAgain.drawingJob.interrupted, true)
})

test('confirmed evidence and generated entities survive restore without being gated on original file', () => {
  for (const status of ['ready', 'generated']) {
    const snapshot = { drawingJob: { status, evidence: { status: 'confirmed', parameters: { length: 85 } }, fileMeta: { name: 'source.dwg' }, requiresFileReselection: true }, generation: status === 'generated' ? { validation: { productionReady: true }, stale: false } : null }
    const recovered = recoverWorkspaceSnapshot(snapshot)
    assert.equal(recovered.drawingJob.sourceFileUnavailable, true)
    assert.equal(recovered.drawingJob.requiresFileReselection, false)
    assert.equal(recovered.drawingJob.status, status)
    assert.deepEqual(recovered.drawingJob.evidence, snapshot.drawingJob.evidence)
    assert.deepEqual(recovered.generation, snapshot.generation)
  }
})

test('editable pending evidence can be confirmed after restore, but failed analysis requires source', () => {
  const candidate = recoverWorkspaceSnapshot({ drawingJob: { status: 'ready', evidence: { status: 'pending', candidateParameters: { length: 85 } }, fileMeta: { name: 'source.svg' } } })
  assert.equal(candidate.drawingJob.requiresFileReselection, false)
  assert.equal(candidate.drawingJob.sourceFileUnavailable, true)
  const failed = recoverWorkspaceSnapshot({ drawingJob: { status: 'error', evidence: null, error: 'AI 分析失败', fileMeta: { name: 'source.svg' } } })
  assert.equal(failed.drawingJob.requiresFileReselection, true)
  assert.equal(failed.drawingJob.error, 'AI 分析失败')
})

test('renames affect the target and draft name, never an old saved version', () => {
  let store = fresh()
  const projectId = store.activeProjectId
  const fileId = store.activeFileId
  const originalName = getActiveFile(store).snapshot.model.name
  store = saveFileVersion(store, projectId, fileId)
  store = renameProject(store, projectId, '新项目名称')
  store = renameProjectFile(store, projectId, fileId, '加工轴')
  assert.equal(getActiveProject(store).name, '新项目名称')
  assert.equal(getActiveFile(store).name, '加工轴')
  assert.equal(getActiveFile(store).snapshot.model.name, '加工轴')
  assert.equal(getActiveFile(store).versions[0].snapshot.model.name, originalName)
  assert.equal(projectStatistics(getActiveProject(store)).versions, 1)
})

test('storage failure is surfaced to the caller and valid source store stays intact', () => {
  const store = fresh()
  assert.throws(() => persistProjectStore(store, { setItem() { throw new Error('Quota exceeded') } }), /Quota exceeded/)
  assert.equal(getActiveFile(store).snapshot.model.outerDiameter, 24)
})
