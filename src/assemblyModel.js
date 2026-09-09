// Nominal dimensions for a small, editable local library. These are geometry
// envelopes, not thread forms, bearing races, or procurement certificates.
export const standardParts = Object.freeze([
  { catalogId: 'bearing-6204', name: '深沟球轴承', spec: '6204 · 20 × 47 × 14', group: '轴承', shape: 'ring', dimensions: { innerDiameter: 20, outerDiameter: 47, length: 14 }, source: '内置公称尺寸 · 6204' },
  { catalogId: 'bearing-6205', name: '深沟球轴承', spec: '6205 · 25 × 52 × 15', group: '轴承', shape: 'ring', dimensions: { innerDiameter: 25, outerDiameter: 52, length: 15 }, source: '内置公称尺寸 · 6205' },
  { catalogId: 'bearing-6206', name: '深沟球轴承', spec: '6206 · 30 × 62 × 16', group: '轴承', shape: 'ring', dimensions: { innerDiameter: 30, outerDiameter: 62, length: 16 }, source: '内置公称尺寸 · 6206' },
  { catalogId: 'socket-m6-20', name: '内六角圆柱头螺钉', spec: 'M6 × 20', group: '紧固件', shape: 'bolt', dimensions: { outerDiameter: 6, length: 20, headDiameter: 10, headLength: 6 }, source: '内置公称包络 · M6 × 20' },
  { catalogId: 'socket-m8-30', name: '内六角圆柱头螺钉', spec: 'M8 × 30', group: '紧固件', shape: 'bolt', dimensions: { outerDiameter: 8, length: 30, headDiameter: 13, headLength: 8 }, source: '内置公称包络 · M8 × 30' },
  { catalogId: 'washer-m8', name: '平垫圈', spec: 'M8 · 8.4 × 16 × 1.6', group: '紧固件', shape: 'ring', dimensions: { innerDiameter: 8.4, outerDiameter: 16, length: 1.6 }, source: '内置公称尺寸 · M8 平垫圈' },
  { catalogId: 'pin-6-24', name: '圆柱销', spec: 'Ø6 × 24', group: '定位件', shape: 'cylinder', dimensions: { innerDiameter: 0, outerDiameter: 6, length: 24 }, source: '内置公称尺寸 · Ø6 × 24' },
])

let nextId = 0
const uniqueId = () => globalThis.crypto?.randomUUID?.() || `part-${Date.now().toString(36)}-${++nextId}`
const finite = (value) => (typeof value === 'number' || typeof value === 'string') && String(value).trim() !== '' && Number.isFinite(Number(value))
const positive = (value) => finite(value) && Number(value) > 0
const vec = (value = {}) => Object.fromEntries(['x', 'y', 'z'].map((key) => [key, finite(value[key]) ? Number(value[key]) : 0]))
const round = (value) => Number(value.toFixed(4))

export function validatePartDefinition(part) {
  const errors = []
  const d = part?.dimensions || {}
  if (!String(part?.name || '').trim()) errors.push('请输入零件名称。')
  if (!['ring', 'cylinder', 'bolt'].includes(part?.shape)) errors.push('不支持该零件形状。')
  for (const [key, label] of [['outerDiameter', '外径'], ['length', '长度']]) {
    if (!positive(d[key]) || Number(d[key]) > 100000) errors.push(`${label}须为大于 0 且不超过 100000 mm 的有限数值。`)
  }
  if (part?.shape === 'ring' && (!positive(d.innerDiameter) || Number(d.innerDiameter) >= Number(d.outerDiameter))) errors.push('内径须大于 0 且小于外径。')
  if (part?.shape === 'bolt' && (!positive(d.headLength) || !positive(d.headDiameter) || Number(d.headDiameter) < Number(d.outerDiameter))) errors.push('螺钉头部长度须大于 0，头部直径不得小于杆径。')
  return errors
}

export function createPartInstance(part, options = {}) {
  const errors = validatePartDefinition(part)
  if (errors.length) throw new Error(errors.join(' '))
  return {
    id: uniqueId(),
    catalogId: part.catalogId || `custom-${uniqueId()}`,
    name: String(part.name).trim(),
    spec: String(part.spec || `Ø${part.dimensions.outerDiameter} × ${part.dimensions.length}`),
    group: part.group || '自定义',
    source: part.source || '用户自定义尺寸',
    shape: part.shape,
    units: 'mm',
    dimensions: Object.fromEntries(Object.entries(part.dimensions).map(([key, value]) => [key, Number(value)])),
    position: vec(options.position),
    rotation: vec(options.rotation),
  }
}

export function duplicatePartInstance(item) {
  return createPartInstance(item, { position: { ...item.position, y: Number(item.position?.y || 0) + Number(item.dimensions.outerDiameter) + 10 }, rotation: item.rotation })
}

export function validateTransform(position, rotation) {
  return ['x', 'y', 'z'].flatMap((axis) => {
    const errors = []
    if (!finite(position?.[axis]) || Math.abs(Number(position[axis])) > 1000000) errors.push(`${axis.toUpperCase()} 位置须为 ±1000000 mm 范围内的有限数值。`)
    if (!finite(rotation?.[axis]) || Math.abs(Number(rotation[axis])) > 360000) errors.push(`${axis.toUpperCase()} 旋转须为 ±360000° 范围内的有限数值。`)
    return errors
  })
}

export function applyPartTransform(item, position, rotation) {
  const errors = validateTransform(position, rotation)
  if (errors.length) throw new Error(errors.join(' '))
  return { ...item, position: vec(position), rotation: vec(rotation) }
}

// XYZ Euler rotation, shared with THREE.Euler's default order.
export function rotateVector(point, rotation = {}) {
  const r = vec(rotation)
  const [a, b, c] = [r.x, r.y, r.z].map((value) => value * Math.PI / 180)
  const ca = Math.cos(a); const sa = Math.sin(a); const cb = Math.cos(b); const sb = Math.sin(b); const cc = Math.cos(c); const sc = Math.sin(c)
  const { x, y, z } = point
  return {
    x: cb * cc * x - cb * sc * y + sb * z,
    y: (sa * sb * cc + ca * sc) * x + (ca * cc - sa * sb * sc) * y - sa * cb * z,
    z: (sa * sc - ca * sb * cc) * x + (sa * cc + ca * sb * sc) * y + ca * cb * z,
  }
}

export function partLocalBounds(item) {
  const d = item.dimensions
  const radius = Math.max(Number(d.outerDiameter), Number(d.headDiameter || 0)) / 2
  return { min: { x: -Number(d.length) / 2 - Number(d.headLength || 0), y: -radius, z: -radius }, max: { x: Number(d.length) / 2, y: radius, z: radius } }
}

export function instanceBounds(item) {
  const local = partLocalBounds(item)
  const min = { x: Infinity, y: Infinity, z: Infinity }; const max = { x: -Infinity, y: -Infinity, z: -Infinity }
  const position = vec(item.position)
  for (const x of [local.min.x, local.max.x]) for (const y of [local.min.y, local.max.y]) for (const z of [local.min.z, local.max.z]) {
    const point = rotateVector({ x, y, z }, item.rotation)
    for (const axis of ['x', 'y', 'z']) { min[axis] = Math.min(min[axis], point[axis] + position[axis]); max[axis] = Math.max(max[axis], point[axis] + position[axis]) }
  }
  return { min, max }
}

export function mainModelEnvelope(model = {}) {
  const kind = String(model?.kind || model?.recipeId || '').toLowerCase()
  let length; let width; let height; let axial = false
  if (['shaft', 'shaft_v1'].includes(kind)) { length = model.length; width = model.outerDiameter; height = model.outerDiameter; axial = true }
  else if (['arched_clevis_support', 'arched_clevis_support_v1'].includes(kind)) {
    length = Number(model.mountHoleCenterDistance) + Number(model.mountEarRadius) * 2; width = model.baseWidth; height = Number(model.earCenterHeight) + Number(model.earRadius)
  }
  else if (['stepped_tapered_nozzle', 'stepped_tapered_nozzle_with_insert_v1', 'stepped_tapered_nozzle_v1', 'tapered_nozzle_with_insert'].includes(kind)) {
    length = model.mainLength; width = Math.max(Number(model.headLeftDiameter), Number(model.headRightDiameter), Number(model.neckDiameter), Number(model.tipDiameter)); height = width; axial = true
  } else if (['bracket', 'bracket_support_v1', 'split_clamp_support', 'split_clamp_support_v1', 'split_clamp_pedestal', 'clamp_pedestal', 'circular_clamp', 'circular_clamp_v1'].includes(kind)) {
    length = model.baseLength; width = model.baseWidth; height = model.totalHeight
  } else return null
  if (![length, width, height].every(positive)) return null
  length = Number(length); width = Number(width); height = Number(height)
  return { kind, axial, length, width, height, min: { x: axial ? 0 : -length / 2, y: -width / 2, z: axial ? -height / 2 : 0 }, max: { x: axial ? length : length / 2, y: width / 2, z: axial ? height / 2 : height } }
}

export function initialPartPosition(model, existingItems = [], part) {
  const envelope = mainModelEnvelope(model)
  const rightEdge = existingItems.reduce((edge, item) => {
    if (validatePartDefinition(item).length || validateTransform(item.position, item.rotation).length) return edge
    return Math.max(edge, instanceBounds(item).max.y)
  }, envelope?.max.y || 0)
  return { x: envelope?.axial ? envelope.length / 2 : 0, y: rightEdge + Number(part.dimensions.outerDiameter) / 2 + 15, z: envelope?.axial ? 0 : Number(part.dimensions.outerDiameter) / 2 }
}

export function boundsOverlap(a, b) {
  return ['x', 'y', 'z'].every((axis) => Math.min(a.max[axis], b.max[axis]) - Math.max(a.min[axis], b.min[axis]) > 1e-7)
}

export function assemblyFingerprint(model, items) { return JSON.stringify([model, items]) }

export function checkAssembly(model, items = []) {
  const issues = []
  const bounds = []
  const fits = []
  const envelope = mainModelEnvelope(model)
  const hasModel = Boolean(model?.kind || model?.recipeId)
  if (hasModel && !envelope) issues.push({ severity: 'error', code: 'model-envelope', message: '当前模型类型或外形尺寸无效，无法计算主件包络。' })
  if (!hasModel && !items.length) issues.push({ severity: 'info', code: 'empty-assembly', message: '当前装配为空，请先创建主件或插入标准件。' })
  const ids = new Set()
  for (const item of items) {
    const errors = [...validatePartDefinition(item), ...validateTransform(item.position, item.rotation)]
    if (!item.id || ids.has(item.id)) errors.push('实例标识缺失或重复。')
    ids.add(item.id)
    if (errors.length) { issues.push({ severity: 'error', code: 'invalid-instance', itemId: item.id, message: `${item.name || '未命名实例'}：${errors.join(' ')}` }); continue }
    const bound = instanceBounds(item)
    bounds.push({ ...bound, item })
    if (envelope && boundsOverlap(envelope, bound)) issues.push({ severity: 'warning', code: 'envelope-overlap', itemId: item.id, message: `${item.name} ${item.spec} 与当前模型包络重叠；孔、槽及曲面未参与实体干涉判断。` })
    if (envelope && ['shaft', 'shaft_v1'].includes(envelope.kind) && item.shape === 'ring') {
      const axis = rotateVector({ x: 1, y: 0, z: 0 }, item.rotation)
      const parallel = Math.hypot(axis.y, axis.z) <= 1e-7
      const offset = Math.hypot(Number(item.position.y), Number(item.position.z))
      const axialOverlap = parallel && Math.min(bound.max.x, envelope.max.x) - Math.max(bound.min.x, envelope.min.x) > 1e-7
      const clearance = (Number(item.dimensions.innerDiameter) - Number(model.outerDiameter)) / 2
      const status = !parallel || offset > 1e-6 ? 'unaligned' : !axialOverlap ? 'separated' : clearance < 0 ? 'too-small' : clearance === 0 ? 'nominal-contact' : 'clearance'
      fits.push({ itemId: item.id, status, axisOffset: round(offset), radialClearance: round(clearance), axialOverlap })
      if (status === 'too-small') issues.push({ severity: 'error', code: 'shaft-fit', itemId: item.id, message: `${item.name}内径 Ø${item.dimensions.innerDiameter} 小于轴外径 Ø${model.outerDiameter}，同轴处径向重叠 ${round(-clearance)} mm。` })
    }
  }
  for (let i = 0; i < bounds.length; i++) for (let j = i + 1; j < bounds.length; j++) {
    if (boundsOverlap(bounds[i], bounds[j])) issues.push({ severity: 'warning', code: 'instance-overlap', itemIds: [bounds[i].item.id, bounds[j].item.id], message: `${bounds[i].item.name}（${i + 1}）与 ${bounds[j].item.name}（${j + 1}）包络重叠，需进一步检查实体。` })
  }
  const instanceCount = items.length + (envelope ? 1 : 0)
  const scope = instanceCount === 0
    ? '当前装配没有可检查的实例，尚未进行重叠检查。'
    : envelope
      ? '旋转后的轴对齐包络盒；直轴与环件同轴时的公称径向间隙。未进行实体布尔干涉、螺纹或公差校验。'
      : '已插入实例的数据与旋转后包络盒；没有主件，不检查主件配合。未进行实体布尔干涉、螺纹或公差校验。'
  return { fingerprint: assemblyFingerprint(model, items), checkedAt: new Date().toISOString(), issues, fits, instanceCount, scope }
}
