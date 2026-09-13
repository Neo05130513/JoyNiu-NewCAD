import test from 'node:test'
import assert from 'node:assert/strict'
import * as ProjectStore from './projectStore.js'
import { createManualFeatureReference, normalizeManualFeatureReference, manualFeatureRecordMatchesReference, applyManualFeatureReference } from './manualFeatureReference.js'

const scope = { accountKey: 'alice', projectId: 'project_a', fileId: 'file_a' }
const record = (changes = {}) => ({ id: 'feature_a', revision: 2, name: '手工法兰', status: 'built', fileId: 'file_a',
  sourceRun: { runId: 'cad_source', revision: 4, statusAtFork: 'ready', planSha256: 'a'.repeat(64) },
  sharedDesign: { id: 'design_a', sourceFeature: { id: 'feature_a', revision: 2 } },
  artifacts: { step: { url: '/private-step' }, glb: { url: '/private-glb' } }, ...changes })

test('manual references contain exact version and provenance without inheriting AI approval or file URLs', () => {
  const value = createManualFeatureReference({ ...record(), token: 'secret', productionReady: true, drawingAgreement: 'matched' }, scope)
  assert.equal(value.status, 'built'); assert.equal(value.revision, 2); assert.equal(value.featureId, 'feature_a')
  assert.equal(value.sharedDesignId, 'design_a'); assert.equal(value.sourceRun.statusAtFork, 'ready')
  assert.equal(value.drawingAgreement, 'not_checked'); assert.equal(value.productionReady, undefined)
  assert.doesNotMatch(JSON.stringify(value), /secret|private-step|private-glb|artifacts/)
  assert.deepEqual(normalizeManualFeatureReference({ ...value, token: 'secret', previewUrl: 'blob:fake' }), value)
  assert.equal(manualFeatureRecordMatchesReference(record(), value), true)
})

test('saved drafts remain reopenable but cannot retain a previous built preview', () => {
  const draft = record({ revision: 3, status: 'draft' })
  const value = createManualFeatureReference(draft, scope)
  assert.equal(value.status, 'draft'); assert.equal(value.sharedDesignId, undefined)
  assert.equal(normalizeManualFeatureReference({ ...value, sharedDesignId: 'design_old' }).sharedDesignId, undefined)
  assert.equal(manualFeatureRecordMatchesReference(draft, value), true)
  assert.equal(manualFeatureRecordMatchesReference({ ...draft, status: 'built' }, value), false)
})

test('references reject an unrelated source file, invalid revisions and fetched provenance mismatches', () => {
  assert.throws(() => createManualFeatureReference(record(), { ...scope, fileId: 'other' }), /来源文件/)
  assert.throws(() => createManualFeatureReference(record({ status: 'failed' }), scope), /保存手工设计/)
  assert.throws(() => createManualFeatureReference(record({ revision: 0 }), scope), /保存手工设计/)
  assert.throws(() => createManualFeatureReference(record(), { ...scope, accountKey: '' }), /来源账号/)
  const value = createManualFeatureReference(record(), scope)
  for (const other of [record({ id: 'feature_b' }), record({ revision: 3 }), record({ fileId: 'other' }),
    record({ sourceRun: { ...record().sourceRun, revision: 5 } }), record({ sharedDesign: { ...record().sharedDesign, sourceFeature: { id: 'feature_b', revision: 2 } } })]) {
    assert.equal(manualFeatureRecordMatchesReference(other, value), false)
  }
})

function sourceStore() {
  let store = ProjectStore.loadProjectStore({ getItem: () => null })
  const projectId = store.activeProjectId, fileId = store.activeFileId
  store = ProjectStore.updateFileSnapshot(store, projectId, fileId, {
    model: { kind: 'feature_model', name: '原 AI 模型', cadPlan: { parameters: {}, features: [] }, agentRun: { runId: 'cad_source', revision: 4, status: 'ready' } },
    generation: { kind: 'feature_model', revision: 4, runId: 'cad_source', validation: { productionReady: true } }, messages: [{ role: 'ai', text: '原 AI 核验记录' }],
  })
  return { store, origin: { accountKey: 'alice', projectId, fileId } }
}

test('a completed edit updates its captured file while another project is active and preserves AI state', () => {
  const { store, origin } = sourceStore()
  const original = ProjectStore.getProjectFile(store, origin.projectId, origin.fileId).snapshot
  const elsewhere = ProjectStore.createProject(store, { name: '另一个项目' })
  const value = createManualFeatureReference(record({ fileId: origin.fileId }), origin)
  const next = applyManualFeatureReference(elsewhere, value, { accountKey: 'alice' })
  assert.equal(next.activeProjectId, elsewhere.activeProjectId); assert.equal(next.activeFileId, elsewhere.activeFileId)
  const saved = ProjectStore.getProjectFile(next, origin.projectId, origin.fileId).snapshot
  assert.deepEqual(saved.model, original.model); assert.deepEqual(saved.generation, original.generation); assert.deepEqual(saved.messages, original.messages)
  assert.deepEqual(saved.manualFeatureReference, value)
  assert.equal(ProjectStore.getActiveFile(next).snapshot.manualFeatureReference, undefined)
  assert.equal(original.manualFeatureReference, undefined)
})

test('account mismatch, removed files and late older revisions cannot overwrite the link', () => {
  const { store, origin } = sourceStore()
  const value = createManualFeatureReference(record({ fileId: origin.fileId }), origin)
  assert.equal(applyManualFeatureReference(store, value, { accountKey: 'bob' }), store)
  assert.equal(applyManualFeatureReference(store, value), store)
  const removed = ProjectStore.deleteProjectFile(store, origin.projectId, origin.fileId)
  assert.equal(applyManualFeatureReference(removed, value, { accountKey: 'alice' }), removed)
  const newer = createManualFeatureReference(record({ fileId: origin.fileId, status: 'draft', revision: 3 }), origin)
  const next = applyManualFeatureReference(store, newer, { accountKey: 'alice' })
  assert.equal(applyManualFeatureReference(next, value, { accountKey: 'alice' }), next)
})

test('the real project serializer, recovery and history preserve a manual draft reference independently', () => {
  const { store, origin } = sourceStore()
  const value = createManualFeatureReference(record({ status: 'draft', revision: 3, fileId: origin.fileId }), origin)
  const next = applyManualFeatureReference(store, value, { accountKey: 'alice' })
  const snapshot = ProjectStore.getActiveFile(next).snapshot
  const restored = ProjectStore.recoverWorkspaceSnapshot(JSON.parse(JSON.stringify(ProjectStore.sanitizeWorkspaceSnapshot(snapshot))))
  assert.deepEqual(restored.manualFeatureReference, value)
  assert.deepEqual(restored.model, snapshot.model); assert.deepEqual(restored.generation, snapshot.generation)
  const versioned = ProjectStore.saveFileVersion(next, origin.projectId, origin.fileId, { note: '保存手工草稿关联' })
  assert.deepEqual(ProjectStore.getActiveFile(versioned).versions[0].snapshot.manualFeatureReference, value)
})
