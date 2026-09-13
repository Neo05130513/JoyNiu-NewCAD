import { emptyCadDraft } from './cadEditorTransactions.js'
import { describeModelEditing } from './modelEditing.js'
import { normalizeManualFeatureReference } from './manualFeatureReference.js'
import { getProjectFile } from './projectStore.js'

const identity = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,180}$/.test(value)

export function emptyCadDocumentSnapshot() {
  return { cadEditorDocument: true, model: null, generation: null, activeMode: '特征编辑', workspaceModeVersion: 2 }
}

/** Resolve only files in the current account's project store. A corrupt or
 * foreign manual reference must never silently fall back to another model. */
export function cadDocumentTarget(store, { accountKey, projectId, fileId, previousTarget } = {}) {
  if (![accountKey, projectId, fileId].every(identity)) return null
  const file = getProjectFile(store, projectId, fileId)
  if (!file || file.projectId !== projectId || file.contentUnavailable || file.type === '文档') return null
  const snapshot = file.snapshot || {}, source = { accountKey, projectId, fileId }
  const reference = snapshot.manualFeatureReference ? normalizeManualFeatureReference(snapshot.manualFeatureReference) : null
  if (snapshot.manualFeatureReference && (!reference || reference.accountKey !== accountKey || reference.sourceProjectId !== projectId || reference.sourceFileId !== fileId)) return null
  const marked = snapshot.cadEditorDocument === true
  if (!reference && !marked && !snapshot.model?.cadPlan) return null
  const entry = describeModelEditing({ ...source, model: snapshot.model, generation: snapshot.generation })
  if (!reference && !entry.editable && !(marked && !snapshot.model?.cadPlan)) return null
  const previousMatches = previousTarget?.accountKey === accountKey && previousTarget?.projectId === projectId
    && previousTarget?.fileId === fileId && previousTarget?.savedFeatureId === reference?.featureId
  const initialPlanKey = marked ? `cad-document:${projectId}:${fileId}`
    : reference ? previousMatches ? previousTarget.initialPlanKey : `saved-feature:${fileId}:${reference.featureId}` : entry.initialPlanKey
  return { ...source, name: file.name, model: snapshot.model || null, generation: snapshot.generation || null,
    initialDraft: entry.initialDraft || emptyCadDraft(file.name), initialPlan: snapshot.model?.cadPlan,
    initialPlanKey, savedFeatureId: reference?.featureId || null, reference,
    status: reference?.status || (entry.editable ? 'plan' : 'empty') }
}

export function cadProjectDocuments(store, { accountKey, projectId } = {}) {
  const project = store?.projects?.find(item => item.id === projectId)
  return (project?.files || []).flatMap(file => {
    const target = cadDocumentTarget(store, { accountKey, projectId, fileId: file.id })
    return target ? [{ accountKey, projectId, fileId: file.id, name: file.name, status: target.status }] : []
  })
}

export function matchesCadDocumentSource(document, { accountKey, projectId, fileId } = {}) {
  return Boolean(document && identity(accountKey) && document.accountKey === accountKey
    && document.projectId === projectId && identity(document.fileId) && (!fileId || document.fileId === fileId))
}
