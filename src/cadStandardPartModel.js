import standardParts from '../apps/api/app/cad_standard_part_catalog.json' with { type: 'json' }
import { createClientId } from './clientId.js'

export { standardParts as cadStandardParts }

/** Build real CAD features. Existing objects and expressions remain intact. */
export function insertCadStandardPart(plan, catalogId, { position = [0, 0, 0], axis = 'Z', instanceId } = {}) {
  const part = standardParts.find(item => item.catalogId === catalogId)
  if (!part) throw new Error('请选择有效的标准件规格。')
  if (!Array.isArray(position) || position.length !== 3 || position.some(value => typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > 1e6)) throw new Error('位置须为 ±1000000 mm 内的 X、Y、Z 数值。')
  if (!['X', 'Y', 'Z'].includes(axis)) throw new Error('请选择标准件轴向。')
  if (!plan || plan.version !== 'cad-plan-v1' || plan.units !== 'mm' || !Array.isArray(plan.features)) throw new Error('当前文件没有可编辑的 CAD 计划。')
  const next = structuredClone(plan), additions = [], parameterNames = [], ids = new Set(next.features.map(item => item.id))
  next.parameters ||= {}
  const prefix = `sp_${instanceId || createClientId().replace(/[^A-Za-z0-9]/g, '').slice(0, 20)}`
  if (!/^sp_[A-Za-z0-9_]{1,28}$/.test(prefix) || [...ids, ...Object.keys(next.parameters)].some(id => id.startsWith(prefix + '_'))) throw new Error('标准件实例编号冲突，请重新插入。')
  const label = `${part.name} ${part.spec}`
  const parameter = (name, value, title) => {
    const key = `${prefix}_${name}`
    next.parameters[key] = { value, label: `${label} · ${title}`, source: { type: 'user', text: `${part.source}；${part.sourceUrl}` } }
    parameterNames.push(key); return key
  }
  const dimension = {}
  const dimensionLabels = { outerDiameter: '外径 mm', innerDiameter: '内径 mm', length: '长度 mm', headDiameter: '头部外径 mm', headLength: '头高 mm', pitch: '螺距 mm', threadLength: '螺纹长度 mm', socketAcrossFlats: '内六角对边 mm', socketDepth: '内六角深度 mm', headChamfer: '头部倒角 mm', tipChamfer: '螺钉端部倒角 mm', endChamfer: '销端部长度 mm', pitchDiameter: '滚珠节圆 mm', ballDiameter: '球径 mm', ballCount: '滚珠数', innerRingDiameter: '内圈外径 mm', outerRingDiameter: '外圈内径 mm', edgeRadius: '圈边圆角 mm', grooveRatio: '滚道曲率系数', radialClearance: '径向游隙 mm', cageThickness: '保持架厚度 mm' }
  for (const [name, value] of Object.entries(part.dimensions)) if (value > 0) dimension[name] = parameter(name, value, dimensionLabels[name])
  const translation = position.map((value, index) => parameter(['x', 'y', 'z'][index], value, `${['X', 'Y', 'Z'][index]} 位置 mm`))
  const featureLabels = { geometry: '实体', axis: '轴向', position: '定位', bodies: '组合' }
  const add = (name, feature) => { const id = `${prefix}_${name}`; additions.push({ id, label: `${label} · ${featureLabels[name]}`, ...feature }); return id }
  let result = add('geometry', { op: 'standard_part', catalogId, dimensions: dimension })
  if (axis !== 'Z') result = add('axis', { op: 'rotate', input: result, axisStart: [0, 0, 0], axisEnd: axis === 'X' ? [0, 1, 0] : [1, 0, 0], angle: parameter('angle', axis === 'X' ? 90 : -90, '轴向角度 °') })
  result = add('position', { op: 'translate', input: result, vector: translation })
  if (next.features.length) {
    if (!ids.has(next.result)) throw new Error('当前模型结果引用已失效，请先修复特征。')
    result = add('bodies', { op: 'compound', inputs: [next.result, result] })
  }
  next.features.push(...additions); next.result = result
  if (next.features.length > 128 || Object.keys(next.parameters).length > 100) throw new Error('插入后超过当前特征或参数上限，请拆分设计。')
  return { plan: next, label, catalogId, featureIds: additions.map(item => item.id), parameterNames }
}
