import { cloneFeatureValue } from './directFeatureModel.js'

/** Remove only states for bodies that this exact operation explicitly removes. */
export function bodyStatesAfterOperation(plan, feature, bodies, originalStates = plan.bodyStates) {
  const next = cloneFeatureValue(plan)
  if (originalStates === undefined) delete next.bodyStates
  else next.bodyStates = cloneFeatureValue(originalStates)
  if (feature.op !== 'body_edit' || !['delete', 'keep'].includes(feature.action)) return next
  if (!Array.isArray(bodies) || !bodies.length || !feature.bodies?.length || feature.bodies.some(selected => !bodies.some(body => body.index === selected.index && body.signature === selected.signature))) {
    throw new Error('实体预览已变化，请重新选择要保留或删除的实体。')
  }
  const selected = new Set(feature.bodies.map(body => body.signature))
  const removed = new Set(bodies.filter(body => feature.action === 'delete' ? selected.has(body.signature) : !selected.has(body.signature)).map(body => body.signature))
  if (next.bodyStates) next.bodyStates = next.bodyStates.filter(state => !removed.has(state.signature))
  return next
}

const referenceKeys = new Set(['parent', 'planeReference', 'axisReference', 'curveReference'])
const referencesId = (value, id) => value && typeof value === 'object' && Object.entries(value).some(([key, entry]) => referenceKeys.has(key) && entry === id || entry && typeof entry === 'object' && referencesId(entry, id))

export function removeCadReference(plan, id) {
  if (!(plan.references || []).some(reference => reference.id === id)) throw new Error('参考几何已不存在，请刷新图档。')
  const dependents = [...(plan.references || []).filter(reference => reference.id !== id), ...(plan.sketches || []), ...plan.features]
    .filter(item => referencesId(item, id))
  if (dependents.length) throw new Error(`该参考几何仍被使用（${dependents.map(item => item.label || item.id).join('、')}），请先修改关联项。`)
  const next = cloneFeatureValue(plan)
  next.references = next.references.filter(reference => reference.id !== id)
  return next
}
