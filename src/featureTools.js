import { cloneFeatureValue, newFeature } from './directFeatureModel.js'

// Operations attach to the chosen output, not an unrelated last tool body.
export function appendFeatureTool(draft, op, mode = 'new') {
  const next = cloneFeatureValue(draft), features = next.plan.features
  const base = next.plan.result
  if (!features.some(item => item.id === base) || next.suppressed.includes(base)) throw new Error('请先选择有效的输出实体。')
  if (!['new', 'union', 'cut', 'intersect'].includes(mode)) throw new Error('请选择新建、添加、移除或相交。')
  const feature = newFeature(op, features)
  if ('input' in feature) feature.input = base
  if ('inputs' in feature && feature.inputs.length < 2) throw new Error('布尔运算需要两个已有实体；也可用“拉伸切除”创建工具轮廓。')
  const combine = !('input' in feature) && !('inputs' in feature) && mode !== 'new'
  if (features.length + (combine ? 2 : 1) > 128) throw new Error('特征数量已达到上限。')
  features.push(feature)
  next.plan.result = feature.id
  if (combine) {
    const boolean = newFeature(mode, features)
    boolean.inputs = [base, feature.id]
    features.push(boolean)
    next.plan.result = boolean.id
  }
  next.changeNote = `新增${feature.label}${combine ? `，${{union:'添加到',cut:'切除',intersect:'相交于'}[mode]}当前实体` : ''}`
  return { draft: next, selected: feature.id }
}
