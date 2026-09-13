import test from 'node:test'
import assert from 'node:assert/strict'
import {
  PROJECT_STORE_KEY, loadProjectStore, persistProjectStore, createProject, createProjectFile,
  getActiveProject, getActiveFile, getProjectFile, getFileSnapshot, selectProject, selectProjectFile,
  updateFileSnapshot, saveFileVersion, restoreFileVersion, renameProject, renameProjectFile,
  sanitizeWorkspaceSnapshot, recoverWorkspaceSnapshot, createWorkspaceSnapshot, projectStatistics,
  duplicateProjectFile, deleteProjectFile, deleteProject, restoreTrashedItem,
  exportProjectBackup, importProjectBackup, importProjectBackupFile, PROJECT_BACKUP_FORMAT, MAX_BACKUP_BYTES,
} from './projectStore.js'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)) }
}
const shaftModel = { name: '加工轴', kind: 'shaft', outerDiameter: 24, length: 70, holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40, material: '45# 钢' }
function fresh(model) { return loadProjectStore(memoryStorage(), { defaultModel: model }) }

test('new visitor gets an independent blank project/file without a default model or demo list', () => {
  const store = loadProjectStore(memoryStorage(), { defaultProjects: [{ id: 'p1', name: 'demo' }, { id: 'p2', name: 'demo2' }], defaultFiles: [{ name: 'fake.pdf', type: '工程图' }] })
  assert.equal(store.projects.length, 1)
  assert.equal(getActiveProject(store).files.length, 1)
  assert.equal(getActiveFile(store).type, '零件')
  assert.equal(getActiveFile(store).snapshot.model, null)
  assert.deepEqual(getActiveFile(store).versions, [])
})

test('blank snapshots remain null while explicitly supplied models and snapshots are preserved', () => {
  for (const type of ['零件', '工程图', '装配体', '文档']) {
    assert.equal(createWorkspaceSnapshot({ type }).model, null)
    assert.equal(createWorkspaceSnapshot({ type, model: null }).model, null)
  }
  assert.equal(createWorkspaceSnapshot({ model: shaftModel, snapshot: {} }).model, null)
  assert.equal(createWorkspaceSnapshot({ model: shaftModel, snapshot: { model: null } }).model, null)
  const modelSnapshot = createWorkspaceSnapshot({ name: '复制的轴', model: shaftModel })
  assert.equal(modelSnapshot.model.name, '复制的轴')
  assert.equal(modelSnapshot.model.outerDiameter, 24)
  modelSnapshot.model.outerDiameter = 99
  assert.equal(shaftModel.outerDiameter, 24)
  const source = { model: { ...shaftModel, length: 120 }, assemblyItems: [{ id: 'part-1', transform: { x: 8 } }] }
  const fromCurrent = createWorkspaceSnapshot({ snapshot: source })
  assert.deepEqual(fromCurrent.model, source.model)
  fromCurrent.assemblyItems[0].transform.x = 90
  assert.equal(source.assemblyItems[0].transform.x, 8)
})

test('new blank projects and every blank file type stay empty when switching from a real model', () => {
  let store = fresh(shaftModel)
  const originalProject = store.activeProjectId, originalFile = store.activeFileId
  store = createProject(store, { name: '空白项目' })
  const blankProject = store.activeProjectId, blankFile = store.activeFileId
  assert.equal(getFileSnapshot(store).model, null)
  for (const type of ['零件', '工程图', '装配体', '文档']) {
    store = createProjectFile(store, blankProject, { name: `空白${type}`, type })
    assert.equal(getFileSnapshot(store).model, null, type)
  }
  store = selectProjectFile(store, originalProject, originalFile)
  assert.equal(getFileSnapshot(store).model.outerDiameter, 24)
  store = selectProjectFile(store, blankProject, blankFile)
  assert.equal(getFileSnapshot(store).model, null)
  store = renameProjectFile(store, blankProject, blankFile, '仍是空白')
  assert.equal(getFileSnapshot(store).model, null)
})

test('blank files survive persistence and version restores without reviving a previous model', () => {
  const storage = memoryStorage()
  let store = fresh()
  const projectId = store.activeProjectId, fileId = store.activeFileId
  store = saveFileVersion(store, projectId, fileId, { note: '空白起点' })
  const blankVersionId = getActiveFile(store).versions[0].id
  store = saveFileVersion(store, projectId, fileId, { snapshot: { model: { ...shaftModel, length: 100 } }, note: '创建了轴' })
  const modelVersionId = getActiveFile(store).versions[1].id
  store = restoreFileVersion(store, projectId, fileId, blankVersionId)
  assert.equal(getFileSnapshot(store).model, null)
  persistProjectStore(store, storage)
  store = loadProjectStore(storage, { defaultModel: shaftModel })
  assert.equal(store.activeFileId, fileId)
  assert.equal(getFileSnapshot(store).model, null)
  assert.equal(getActiveFile(store).versions[0].snapshot.model, null)
  assert.equal(getActiveFile(store).versions[1].snapshot.model.length, 100)
  store = restoreFileVersion(store, projectId, fileId, modelVersionId)
  assert.equal(getFileSnapshot(store).model.length, 100)
})

test('creating from an explicit current snapshot preserves its model and independent contents', () => {
  const source = { model: { ...shaftModel, outerDiameter: 36 }, messages: [{ role: 'ai', text: '已创建模型' }] }
  let store = fresh()
  store = createProject(store, { name: '从模型创建的项目', snapshot: source })
  assert.equal(getFileSnapshot(store).model.outerDiameter, 36)
  store = createProjectFile(store, store.activeProjectId, { name: '副本', snapshot: source })
  source.model.outerDiameter = 99
  source.messages[0].text = '外部修改'
  assert.equal(getFileSnapshot(store).model.outerDiameter, 36)
  assert.equal(getFileSnapshot(store).messages[0].text, '已创建模型')
})

test('metadata-only legacy projects never invent a model, even with a caller template', () => {
  const storage = memoryStorage({
    'joyniu-projects': JSON.stringify([{ id: 'legacy-p', name: '旧项目' }]),
    'joyniu-files': JSON.stringify([{ id: 'f1', name: '仅有文件名', type: '零件' }]),
  })
  let store = loadProjectStore(storage, { defaultModel: shaftModel })
  assert.equal(getActiveFile(store).contentUnavailable, undefined)
  assert.equal(getFileSnapshot(store).model, null)
  assert.ok(getActiveProject(store).files.some((file) => file.contentUnavailable && file.snapshot.model === null))
  persistProjectStore(store, storage)
  store = loadProjectStore(storage)
  assert.equal(getFileSnapshot(store).model, null)
})

test('persisted snapshots with an omitted model recover as blank and keep real model versions', () => {
  const storage = memoryStorage({ [PROJECT_STORE_KEY]: JSON.stringify({
    schemaVersion: 1, activeProjectId: 'p', activeFileId: 'f', projects: [{ id: 'p', name: '已有项目', files: [{
      id: 'f', name: '空白草稿', type: '零件', snapshot: { prompt: '待设计' },
      versions: [{ id: 'v', number: 1, snapshot: { model: shaftModel } }],
    }] }],
  }) })
  const store = loadProjectStore(storage, { defaultModel: { ...shaftModel, length: 999 } })
  assert.equal(getFileSnapshot(store).model, null)
  assert.equal(getFileSnapshot(store).prompt, '待设计')
  assert.deepEqual(getActiveFile(store).versions[0].snapshot.model, shaftModel)
})

test('projects and files retain independent snapshots when switched or edited', () => {
  let store = fresh(shaftModel)
  const firstProject = store.activeProjectId
  const firstFile = store.activeFileId
  const changed = getFileSnapshot(store)
  changed.model.outerDiameter = 36
  store = updateFileSnapshot(store, firstProject, firstFile, changed)
  store = createProject(store, { name: '项目 B', model: shaftModel })
  assert.equal(getActiveFile(store).snapshot.model.outerDiameter, 24)
  const secondProject = store.activeProjectId
  const secondFile = store.activeFileId
  store = createProjectFile(store, secondProject, { name: '另一个零件', type: '零件', model: shaftModel })
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
  let store = fresh(shaftModel)
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
  let store = fresh(shaftModel)
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
  let store = fresh(shaftModel)
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

test('durable artifact capability tokens survive while account credentials are removed', () => {
  const clean = sanitizeWorkspaceSnapshot({ token: 'login-secret', auth: { token: 'auth-secret' },
    model: { name: 'part', token: 'model-capability', accessToken: 'login-secret' },
    generation: { artifacts: [{ token: 'download-capability', downloadUrl: 'https://cad.test/model.glb' }] },
    drawingJob: { cadTask: { requestId: 'request-accepted', status: 'queued' } },
  })
  assert.equal(clean.token, undefined)
  assert.equal(clean.auth.token, undefined)
  assert.equal(clean.model.accessToken, undefined)
  assert.equal(clean.model.token, 'model-capability')
  assert.equal(clean.generation.artifacts[0].token, 'download-capability')
  const restored = recoverWorkspaceSnapshot(clean)
  assert.equal(restored.drawingJob.cadTask.status, 'queued')
  assert.equal(restored.drawingJob.requiresFileReselection, false)
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

test('complete idle parameter edits remain buildable after reload with source provenance retained', () => {
  const model = { kind: 'arched_clevis_support', archOuterRadius: 28, archInnerRadius: 16,
    baseWidth: 50, baseThickness: 9, earRadius: 15, earHoleDiameter: 13,
    earCenterHeight: 40, earThickness: 10, earGap: 30, mountEarRadius: 15,
    mountHoleDiameter: 13, mountHoleCenterDistance: 80, units: 'mm' }
  const source = { model, drawingJob: { status: 'idle', evidence: null,
    fileMeta: { name: 'source.jpg', size: 100 }, requiresFileReselection: true } }
  const recovered = recoverWorkspaceSnapshot(source)
  for (const snapshot of [recovered, recoverWorkspaceSnapshot(recovered)]) {
    assert.equal(snapshot.drawingJob.requiresFileReselection, false)
    assert.equal(snapshot.drawingJob.sourceFileUnavailable, true)
    assert.deepEqual(snapshot.drawingJob.fileMeta, source.drawingJob.fileMeta)
    assert.deepEqual(snapshot.model, model)
  }
  for (const model of [null, { kind: 'arched_clevis_support' }, { ...source.model, earHoleDiameter: 50 }]) {
    assert.equal(recoverWorkspaceSnapshot({ ...source, model }).drawingJob.requiresFileReselection, true)
  }
})

test('valid old dimensions do not bypass recovery of a pending or failed drawing analysis', () => {
  for (const status of ['queued', 'analyzing', 'generating', 'error']) {
    const snapshot = { model: shaftModel, drawingJob: { status, evidence: null, fileMeta: { name: 'new-source.jpg' } } }
    const recovered = recoverWorkspaceSnapshot(snapshot)
    assert.equal(recovered.drawingJob.requiresFileReselection, true, status)
    assert.equal(recoverWorkspaceSnapshot(recovered).drawingJob.requiresFileReselection, true, status)
  }
})

test('renames affect the target and draft name, never an old saved version', () => {
  let store = fresh(shaftModel)
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
  const store = fresh(shaftModel)
  assert.throws(() => persistProjectStore(store, { setItem() { throw new Error('Quota exceeded') } }), /Quota exceeded/)
  assert.equal(getActiveFile(store).snapshot.model.outerDiameter, 24)
})

test('duplicate creates an independent draft and new version history while retaining drawing references', () => {
  let store = fresh(shaftModel)
  const projectId = store.activeProjectId, originalId = store.activeFileId
  store = updateFileSnapshot(store, projectId, originalId, { model: shaftModel, prompt: '待发送要求', drawingJob: { fileMeta: { name: 'drawing.pdf' } } })
  store = saveFileVersion(store, projectId, originalId, { note: '原文件版本' })
  const copy = duplicateProjectFile(store, projectId, originalId)
  assert.notEqual(copy.activeFileId, originalId)
  assert.equal(getActiveFile(copy).name, '零件 01（副本）')
  assert.equal(getActiveFile(copy).snapshot.model.name, '零件 01（副本）')
  assert.equal(getActiveFile(copy).snapshot.prompt, '待发送要求')
  assert.equal(getActiveFile(copy).snapshot.drawingJob.fileMeta.name, 'drawing.pdf')
  assert.equal(getActiveFile(copy).versions.length, 0)
  const edited = updateFileSnapshot(copy, projectId, copy.activeFileId, { model: { ...shaftModel, length: 125 } })
  assert.equal(getProjectFile(edited, projectId, originalId).snapshot.model.length, 70)
  assert.equal(getProjectFile(edited, projectId, originalId).versions.length, 1)
  const another = duplicateProjectFile(copy, projectId, originalId)
  assert.equal(getActiveFile(another).name, '零件 01（副本 2）')
})

test('creating another project remembers the previous project last-opened file after reload', () => {
  let store = fresh(shaftModel)
  const projectId = store.activeProjectId
  store = createProjectFile(store, projectId, { name: '最后打开的说明', type: '文档' })
  const fileId = store.activeFileId
  store = createProject(store, { name: '另一个项目' })
  const storage = memoryStorage(); persistProjectStore(store, storage)
  store = selectProject(loadProjectStore(storage), projectId)
  assert.equal(store.activeFileId, fileId)
})

test('file deletion is reversible with snapshots and versions preserved across local persistence', () => {
  let store = fresh(shaftModel)
  const projectId = store.activeProjectId, removedId = store.activeFileId
  store = saveFileVersion(store, projectId, removedId, { note: '已核对尺寸' })
  store = createProjectFile(store, projectId, { name: '说明', type: '文档', snapshot: { documentText: '安装说明' } })
  const nextId = store.activeFileId
  store = selectProjectFile(store, projectId, removedId)
  store = deleteProjectFile(store, projectId, removedId)
  assert.equal(store.activeFileId, nextId)
  assert.equal(getProjectFile(store, projectId, removedId), null)
  assert.equal(store.trash[0].item.versions.length, 1)
  const storage = memoryStorage(); persistProjectStore(store, storage)
  const recovered = restoreTrashedItem(loadProjectStore(storage), store.trash[0].id)
  assert.equal(recovered.activeFileId, removedId)
  assert.equal(getActiveFile(recovered).snapshot.model.length, 70)
  assert.equal(getActiveFile(recovered).versions[0].note, '已核对尺寸')
  assert.equal(recovered.trash.length, 0)
})

test('deleting the last file keeps a usable empty project and deleting the last project creates a blank workspace', () => {
  let store = fresh(shaftModel)
  const projectId = store.activeProjectId, fileId = store.activeFileId
  store = deleteProjectFile(store, projectId, fileId)
  assert.equal(getActiveProject(store).files.length, 0)
  assert.equal(store.activeFileId, null)
  store = deleteProject(store, projectId)
  assert.equal(store.projects.length, 1)
  assert.equal(getActiveFile(store).snapshot.model, null)
  assert.equal(store.trash.length, 2)
  const restored = restoreTrashedItem(store, store.trash.find((item) => item.kind === 'file').id)
  assert.equal(getActiveFile(restored).snapshot.model.length, 70)
})

test('workspace backup appends independent projects, documents and immutable version history', () => {
  let original = fresh(shaftModel)
  original = saveFileVersion(original, original.activeProjectId, original.activeFileId, { note: '版本 A' })
  original = createProjectFile(original, original.activeProjectId, { name: '设计说明', type: '文档', snapshot: { documentText: '核对孔距', token: 'private', controller: {} } })
  const backup = exportProjectBackup(original)
  assert.equal(backup.format, PROJECT_BACKUP_FORMAT)
  assert.equal(JSON.stringify(backup).includes('private'), false)
  const before = JSON.stringify(original)
  const imported = importProjectBackup(original, JSON.stringify(backup))
  assert.equal(JSON.stringify(original), before)
  assert.equal(imported.projects.length, 2)
  const project = getActiveProject(imported)
  assert.notEqual(project.id, original.activeProjectId)
  assert.equal(project.name, '我的第一个项目（导入）')
  assert.equal(project.files[1].snapshot.documentText, '核对孔距')
  assert.notEqual(project.files[0].id, original.projects[0].files[0].id)
  assert.notEqual(project.files[0].versions[0].id, original.projects[0].files[0].versions[0].id)
  assert.equal(project.files[0].versions[0].note, '版本 A')
  const restored = restoreFileVersion(imported, project.id, project.files[0].id, project.files[0].versions[0].id)
  assert.equal(getProjectFile(restored, project.id, project.files[0].id).snapshot.model.length, 70)
})

test('backup includes recoverable deleted files and reconnects them to the imported project', () => {
  let store = fresh(shaftModel)
  store = deleteProjectFile(store, store.activeProjectId, store.activeFileId)
  const imported = importProjectBackup(fresh(), exportProjectBackup(store))
  const importedProjectId = imported.activeProjectId
  const record = imported.trash[0]
  assert.equal(record.projectId, importedProjectId)
  const restored = restoreTrashedItem(imported, record.id)
  assert.equal(restored.activeProjectId, importedProjectId)
  assert.equal(restored.projects.length, imported.projects.length)
  assert.equal(getActiveFile(restored).snapshot.model.length, 70)
})

test('backup reconnects a separately deleted file after its deleted parent project is restored', () => {
  let store = fresh(shaftModel)
  store = deleteProjectFile(store, store.activeProjectId, store.activeFileId)
  store = deleteProject(store, store.activeProjectId)
  let imported = importProjectBackup(fresh(), exportProjectBackup(store))
  const fileRecord = imported.trash.find((item) => item.kind === 'file')
  const projectRecord = imported.trash.find((item) => item.kind === 'project')
  assert.equal(fileRecord.projectId, projectRecord.item.id)
  imported = restoreTrashedItem(imported, projectRecord.id)
  const restored = restoreTrashedItem(imported, fileRecord.id)
  assert.equal(restored.activeProjectId, projectRecord.item.id)
  assert.equal(getActiveFile(restored).snapshot.model.length, 70)
})

test('existing individual JSON downloads can be imported as independent files', () => {
  const store = fresh()
  const imported = importProjectBackup(store, { schemaVersion: 1, name: '说明', type: '文档', snapshot: { documentText: '装配要求' } })
  assert.equal(getActiveFile(imported).name, '说明')
  assert.equal(getActiveFile(imported).snapshot.documentText, '装配要求')
  assert.equal(store.projects.length, 1)
})

test('older settings backups without a format marker remain importable', () => {
  let original = fresh(shaftModel)
  original = saveFileVersion(original, original.activeProjectId, original.activeFileId, { note: '旧版备份' })
  const bareBackup = JSON.parse(JSON.stringify(original))
  assert.equal(bareBackup.format, undefined)
  const imported = importProjectBackup(fresh(), bareBackup)
  assert.equal(getActiveFile(imported).snapshot.model.length, 70)
  assert.equal(getActiveFile(imported).versions[0].note, '旧版备份')
})

test('restoring a same-named file keeps the draft model name aligned without renaming historical versions', () => {
  let store = fresh(shaftModel)
  const originalName = getActiveFile(store).name
  store = saveFileVersion(store, store.activeProjectId, store.activeFileId)
  store = deleteProjectFile(store, store.activeProjectId, store.activeFileId)
  store = createProjectFile(store, store.activeProjectId, { name: originalName })
  store = restoreTrashedItem(store, store.trash[0].id)
  assert.equal(getActiveFile(store).name, `${originalName}（恢复）`)
  assert.equal(getActiveFile(store).snapshot.model.name, getActiveFile(store).name)
  assert.equal(getActiveFile(store).versions[0].snapshot.model.name, originalName)
})

test('malformed model collections are rejected before they can crash the workspace', () => {
  for (const model of [{}, { kind: 8 }, { kind: 'feature_model', cadPlan: { parameters: 'bad' } },
    { kind: 'feature_model', cadPlan: { parameters: [null] } }, { kind: 'feature_model', cadPlan: { features: {} } },
    { kind: 'feature_model', cadPlan: { features: [null] } },
    { kind: 'feature_model', agentRun: { sourceTranscription: { annotations: [null] } } }]) {
    assert.throws(() => importProjectBackup(fresh(), { schemaVersion: 1, name: '错误模型', type: '零件', snapshot: { model } }))
  }
  const model = { kind: 'feature_model', cadPlan: { parameters: { length: { value: 20, label: '长度' } },
    features: [{ id: 'body', op: 'box', size: ['length', 10, 10] }], result: 'body' } }
  assert.deepEqual(getActiveFile(importProjectBackup(fresh(), { schemaVersion: 1, name: '有效模型', type: '零件', snapshot: { model } })).snapshot.model, model)
})

test('malformed or oversized backup leaves existing projects untouched', () => {
  const store = fresh(shaftModel), before = JSON.stringify(store)
  for (const input of ['not json', { schemaVersion: 9 }, { schemaVersion: 1, projects: [] },
    { schemaVersion: 1, name: 'bad', type: '文档', snapshot: { model: 'not a model' } },
    { format: PROJECT_BACKUP_FORMAT, schemaVersion: 1, projects: [{ name: 'bad', files: [null] }] },
    ' '.repeat(MAX_BACKUP_BYTES + 1)]) {
    assert.throws(() => importProjectBackup(store, input))
    assert.equal(JSON.stringify(store), before)
  }
  assert.equal(sanitizeWorkspaceSnapshot(null).model, null)
})

test('deleted siblings and parent restore into one project in any order, including after reload or backup import', () => {
  const orders = [['零件 01', '说明', 'project'], ['零件 01', 'project', '说明'], ['说明', '零件 01', 'project'],
    ['说明', 'project', '零件 01'], ['project', '零件 01', '说明'], ['project', '说明', '零件 01']]
  for (const mode of ['memory', 'reload', 'backup']) for (const order of orders) {
    let store = fresh(shaftModel)
    const parent = store.activeProjectId, part = store.activeFileId
    store = saveFileVersion(store, parent, part, { note: '保留的尺寸版本' })
    store = createProjectFile(store, parent, { name: '说明', type: '文档', snapshot: { documentText: '保留公差说明' } })
    const document = store.activeFileId
    store = createProjectFile(store, parent, { name: '草图', type: '工程图' })
    store = deleteProjectFile(store, parent, part)
    store = deleteProjectFile(store, parent, document)
    store = deleteProject(store, parent)
    const before = JSON.stringify(store)
    for (const [index, item] of order.entries()) {
      const record = store.trash.find((record) => item === 'project' ? record.kind === 'project' : record.kind === 'file' && record.item.name === item)
      const original = JSON.stringify(store)
      const restored = restoreTrashedItem(store, record.id)
      assert.equal(JSON.stringify(store), original, 'restoring must not mutate the prior snapshot')
      store = restored
      if (index === 0 && mode === 'reload') {
        const storage = memoryStorage(); persistProjectStore(store, storage); store = loadProjectStore(storage)
      }
      if (index === 0 && mode === 'backup') store = importProjectBackup(fresh(), exportProjectBackup(store))
    }
    const result = store.projects.find((project) => project.files.some((file) => file.name === '说明'))
    assert.deepEqual(result.files.map((file) => file.name).sort(), ['草图', '零件 01', '说明'].sort(), `${mode}: ${order}`)
    assert.equal(result.files.find((file) => file.name === '说明').snapshot.documentText, '保留公差说明')
    assert.equal(result.files.find((file) => file.name === '零件 01').versions[0].note, '保留的尺寸版本')
    assert.equal(store.trash.length, 0)
    assert.equal(new Set(store.projects.map((project) => project.id)).size, store.projects.length)
    assert.ok(before.includes('保留公差说明'))
  }
})

test('restoring a parent keeps edits and new files made after a child was recovered', () => {
  let store = fresh(shaftModel)
  const parent = store.activeProjectId, part = store.activeFileId
  store = createProjectFile(store, parent, { name: '项目原说明', type: '文档', snapshot: { documentText: '旧内容' } })
  store = deleteProjectFile(store, parent, part)
  store = deleteProject(store, parent)
  store = restoreTrashedItem(store, store.trash.find((record) => record.kind === 'file').id)
  store = updateFileSnapshot(store, parent, part, { model: { ...shaftModel, length: 150 } })
  store = createProjectFile(store, parent, { name: '恢复后新增', type: '文档', snapshot: { documentText: '新内容' } })
  store = restoreTrashedItem(store, store.trash.find((record) => record.kind === 'project').id)
  assert.equal(getProjectFile(store, parent, part).snapshot.model.length, 150)
  assert.equal(getActiveProject(store).files.length, 3)
  assert.equal(getActiveProject(store).files.find((file) => file.name === '恢复后新增').snapshot.documentText, '新内容')
  assert.equal(getActiveProject(store).files.find((file) => file.name === '项目原说明').snapshot.documentText, '旧内容')
})

test('empty cloud project lists retain trash and account migration metadata on normalization', () => {
  let store = fresh(shaftModel)
  store = deleteProjectFile(store, store.activeProjectId, store.activeFileId)
  store = { ...store, projects: [], activeProjectId: null, activeFileId: null, legacyImportState: 'completed' }
  const storage = memoryStorage(); persistProjectStore(store, storage)
  const loaded = loadProjectStore(storage)
  assert.equal(loaded.projects.length, 1)
  assert.equal(getActiveFile(loaded).snapshot.model, null)
  assert.equal(loaded.legacyImportState, 'completed')
  assert.deepEqual(loaded.trash, store.trash)
  assert.equal(getActiveFile(restoreTrashedItem(loaded, loaded.trash[0].id)).snapshot.model.length, 70)
})

test('reload selects a usable remembered file instead of unavailable legacy metadata', () => {
  let store = fresh()
  const unavailableId = store.activeFileId
  store.projects[0].files[0].contentUnavailable = true
  store = createProjectFile(store, store.activeProjectId, { name: '可编辑说明', type: '文档', snapshot: { documentText: '仍可编辑' } })
  const availableId = store.activeFileId
  for (const selected of [unavailableId, 'missing-file']) {
    const storage = memoryStorage(); persistProjectStore({ ...store, activeFileId: selected }, storage)
    const loaded = loadProjectStore(storage)
    assert.equal(loaded.activeFileId, availableId)
    assert.equal(getFileSnapshot(loaded).documentText, '仍可编辑')
  }
})

test('backup refuses ambiguous duplicate live project identities without changing existing data', () => {
  const store = fresh(), before = JSON.stringify(store)
  const backup = exportProjectBackup(store)
  backup.projects.push({ ...backup.projects[0], name: '同标识的另一项目' })
  assert.throws(() => importProjectBackup(store, backup), /标识重复/)
  assert.equal(JSON.stringify(store), before)
})

test('backup file finishing after account or page exit cannot invoke the importer', async () => {
  let finish, imports = 0
  const file = { name: '设计备份.json', size: 10, text: () => new Promise((resolve) => { finish = resolve }) }
  const controller = new AbortController()
  const pending = importProjectBackupFile(file, () => { imports++; return true }, { signal: controller.signal })
  controller.abort()
  finish(JSON.stringify(exportProjectBackup(fresh(shaftModel))))
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(imports, 0)
})

test('backup file import propagates a rejected action and validates before reading', async () => {
  const payload = exportProjectBackup(fresh())
  const file = { name: '项目.JSON', size: 10, text: async () => JSON.stringify(payload) }
  let imported
  await importProjectBackupFile(file, (value) => { imported = value; return true })
  assert.deepEqual(imported, payload)
  await assert.rejects(importProjectBackupFile(file, () => false), /尚未导入/)
  for (const invalid of [{ ...file, name: '项目.txt' }, { ...file, size: MAX_BACKUP_BYTES + 1 }]) {
    await assert.rejects(importProjectBackupFile({ ...invalid, text: () => { assert.fail('invalid file must not be read') } }, () => true))
  }
  await assert.rejects(importProjectBackupFile({ ...file, text: async () => '{' }, () => true), /有效的 JSON/)
})
