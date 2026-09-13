import { getProjectFile, updateFileSnapshot } from './projectStore.js'

const identifier = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,180}$/.test(value)
const revisionNumber = value => Number.isSafeInteger(value) && value > 0
const sourceReference = value => {
  if (!value) return null
  if (!identifier(value.runId) || !revisionNumber(value.revision)) throw new Error('手工版本的来源任务无效。')
  return { runId: value.runId, revision: value.revision,
    ...(typeof value.statusAtFork === 'string' ? { statusAtFork: value.statusAtFork.slice(0, 50) } : {}),
    ...(/^[a-f0-9]{64}$/i.test(value.planSha256 || '') ? { planSha256: value.planSha256 } : {}) }
}

/** A durable pointer to a saved manual version, never a copy of AI approval. */
export function createManualFeatureReference(record, { accountKey, projectId, fileId } = {}) {
  if (!identifier(accountKey) || !identifier(projectId) || !identifier(fileId)) throw new Error('缺少手工修改的来源账号或文件。')
  if (!record || !['draft', 'built'].includes(record.status) || !identifier(record.id) || !record.id.startsWith('feature_') || !revisionNumber(record.revision)) throw new Error('请先保存手工设计，再关联到来源文件。')
  if (record.fileId !== fileId) throw new Error('手工设计不属于这个来源文件，未关联。')
  const shared = record.sharedDesign
  const sharedMatches = record.status === 'built' && identifier(shared?.id) && shared.sourceFeature?.id === record.id && shared.sourceFeature?.revision === record.revision
  return { schemaVersion: 1, kind: 'manual_feature', accountKey, sourceProjectId: projectId, sourceFileId: fileId,
    featureId: record.id, revision: record.revision, status: record.status, name: String(record.name || '手工修改').slice(0, 180),
    sourceRun: sourceReference(record.sourceRun), ...(sharedMatches ? { sharedDesignId: shared.id } : {}),
    drawingAgreement: 'not_checked' }
}

export function normalizeManualFeatureReference(value) {
  if (!value || value.schemaVersion !== 1 || value.kind !== 'manual_feature') return null
  try {
    const clean = createManualFeatureReference({ id: value.featureId, revision: value.revision, status: value.status || 'built',
      fileId: value.sourceFileId, name: value.name, sourceRun: value.sourceRun },
    { accountKey: value.accountKey, projectId: value.sourceProjectId, fileId: value.sourceFileId })
    if (clean.status === 'built' && identifier(value.sharedDesignId)) clean.sharedDesignId = value.sharedDesignId
    return clean
  } catch { return null }
}

/** Match a fetched immutable version before showing or downloading its geometry. */
export function manualFeatureRecordMatchesReference(record, reference) {
  const value = normalizeManualFeatureReference(reference)
  if (!value || record?.status !== value.status || record.id !== value.featureId || record.revision !== value.revision || record.fileId !== value.sourceFileId) return false
  try {
    if (JSON.stringify(sourceReference(record.sourceRun)) !== JSON.stringify(value.sourceRun)) return false
  } catch { return false }
  if (value.sharedDesignId && (record.sharedDesign?.id !== value.sharedDesignId
    || record.sharedDesign?.sourceFeature?.id !== value.featureId || record.sharedDesign?.sourceFeature?.revision !== value.revision)) return false
  return true
}

/** Update the captured source file even if another file is currently open. */
export function applyManualFeatureReference(store, reference, { accountKey } = {}) {
  const value = normalizeManualFeatureReference(reference)
  if (!value || !accountKey || accountKey !== value.accountKey) return store
  const file = getProjectFile(store, value.sourceProjectId, value.sourceFileId)
  if (!file || file.contentUnavailable || file.type === '文档') return store
  const previous = normalizeManualFeatureReference(file.snapshot?.manualFeatureReference)
  if (previous?.featureId === value.featureId && previous.revision > value.revision) return store
  return updateFileSnapshot(store, value.sourceProjectId, value.sourceFileId, { ...file.snapshot, manualFeatureReference: value })
}
