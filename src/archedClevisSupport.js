export const archedClevisSupportRequiredParameterKeys = [
  'archOuterRadius', 'archInnerRadius', 'baseWidth', 'baseThickness',
  'earRadius', 'earHoleDiameter', 'earCenterHeight', 'earThickness', 'earGap',
  'mountEarRadius', 'mountHoleDiameter', 'mountHoleCenterDistance',
]

export const archedClevisSupportParameterKeys = [...archedClevisSupportRequiredParameterKeys, 'material', 'units']

export const archedClevisSupportParameterLabels = {
  archOuterRadius: '拱座外圆半径', archInnerRadius: '底部拱孔半径',
  baseWidth: '支座总宽 Y', baseThickness: '安装耳板厚度',
  earRadius: '上耳外圆半径', earHoleDiameter: '上耳横孔直径',
  earCenterHeight: '上耳孔中心高度', earThickness: '单侧上耳厚度', earGap: '双耳净间距',
  mountEarRadius: '安装耳外圆半径', mountHoleDiameter: '安装孔直径',
  mountHoleCenterDistance: '安装孔中心距', material: '材料', units: '单位',
}

export const archedClevisSupportDefaults = {
  name: '圆弧双耳支座 · AI 候选', kind: 'arched_clevis_support', recipeId: 'arched_clevis_support_v1', type: '零件',
  archOuterRadius: 28, archInnerRadius: 16, baseWidth: 50, baseThickness: 9,
  earRadius: 15, earHoleDiameter: 13, earCenterHeight: 40, earThickness: 10, earGap: 30,
  mountEarRadius: 15, mountHoleDiameter: 13, mountHoleCenterDistance: 80,
  material: '45# 钢', units: 'mm', updatedAt: '刚刚',
}

export const archedClevisSupportDefinition = {
  kind: 'arched_clevis_support', recipeId: 'arched_clevis_support_v1', preview: archedClevisSupportDefaults,
  keys: archedClevisSupportParameterKeys, required: archedClevisSupportRequiredParameterKeys,
  labels: archedClevisSupportParameterLabels,
}

// A drawing candidate is evidence, so geometry may only come from the
// supplied parameters. Metadata can preserve a document name/material, but
// must not reintroduce missing dimensions from a template or old snapshot.
export function archedClevisSupportCandidateModel(parameters = {}, metadata = {}) {
  const identity = Object.fromEntries(['name', 'type', 'material', 'units', 'updatedAt']
    .map((key) => [key, metadata[key] ?? archedClevisSupportDefaults[key]]))
  const explicit = Object.fromEntries(archedClevisSupportParameterKeys
    .filter((key) => Object.hasOwn(parameters, key))
    .map((key) => [key, parameters[key]]))
  return { ...identity, ...explicit, kind: 'arched_clevis_support', recipeId: 'arched_clevis_support_v1' }
}

const finiteDimension = (value) => (typeof value === 'number' || (typeof value === 'string'
  && /^[+]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))) && Number.isFinite(Number(value))
const dimensionNumber = (value) => finiteDimension(value) && Number(value) > 0 ? Number(value) : NaN

// X is the installation-hole pitch direction, Y the two-ear axis, and Z
// rises from the base. Both concentric arch radii have their centre at Z=0.
// The bridge plane meets the outer circle at X=±earRadius; it is not a
// separately guessed drawing dimension.
export function archedClevisSupportDimensions(model) {
  const n = (key) => dimensionNumber(model?.[key])
  const bridgeHeight = Math.sqrt(n('archOuterRadius') ** 2 - n('earRadius') ** 2)
  return {
    baseLength: n('mountHoleCenterDistance') + 2 * n('mountEarRadius'),
    baseWidth: n('baseWidth'), totalHeight: n('earCenterHeight') + n('earRadius'),
    bridgeHeight,
    bridgeThickness: bridgeHeight - n('archInnerRadius'),
    baseJoinX: Math.sqrt(n('archOuterRadius') ** 2 - n('baseThickness') ** 2),
    earCenterY: (n('earGap') + n('earThickness')) / 2,
    earRangesY: [[-n('baseWidth') / 2, -n('earGap') / 2], [n('earGap') / 2, n('baseWidth') / 2]],
  }
}

export function validateArchedClevisSupport(model) {
  const kind = 'arched_clevis_support'
  const errors = []
  const check = (condition, field, message) => { if (!condition) errors.push({ field, message }) }
  const n = (key) => Number(model?.[key])
  for (const field of archedClevisSupportRequiredParameterKeys) {
    check(finiteDimension(model?.[field]) && n(field) > 0, field, `${archedClevisSupportParameterLabels[field]}必须是大于 0 的有限尺寸。`)
  }
  if (model?.units !== undefined) check(model.units === 'mm', 'units', '当前配方使用毫米，请先将尺寸换算为 mm。')
  const result = () => ({ valid: errors.length === 0, errors, unsupported: false, kind })
  if (errors.length) return result()
  check(n('archOuterRadius') > n('archInnerRadius'), 'archInnerRadius', '底部拱孔半径必须小于拱座外圆半径，以保留拱壁。')
  check(n('earRadius') < n('archOuterRadius'), 'earRadius', '上耳外圆半径必须小于拱座外圆半径，以形成连接平台。')
  check(n('earHoleDiameter') < 2 * n('earRadius'), 'earHoleDiameter', '上耳横孔直径必须小于上耳外径，以保留孔壁。')
  check(Math.abs(n('baseWidth') - 2 * n('earThickness') - n('earGap')) <= 1e-6, 'earGap', '支座总宽必须等于两侧上耳厚度之和 + 双耳净间距。')
  check(2 * n('mountEarRadius') <= n('baseWidth'), 'mountEarRadius', '安装耳外径不能超过支座总宽。')
  check(n('mountHoleDiameter') < 2 * n('mountEarRadius'), 'mountHoleDiameter', '安装孔直径必须小于安装耳外径，以保留孔壁。')
  check(n('mountHoleCenterDistance') / 2 - n('mountHoleDiameter') / 2 > n('archOuterRadius'), 'mountHoleCenterDistance', '安装孔必须位于拱座外侧，不能与拱壁相交。')
  if (n('earRadius') < n('archOuterRadius')) {
    const { bridgeHeight } = archedClevisSupportDimensions(model)
    check(bridgeHeight > n('archInnerRadius'), 'earRadius', '上耳宽度过大，连接平台与底部拱孔相交，请减小上耳半径或拱孔半径。')
    check(n('earCenterHeight') - n('earHoleDiameter') / 2 > bridgeHeight, 'earCenterHeight', '上耳横孔底部必须高于双耳之间的连接平台。')
    check(n('baseThickness') < bridgeHeight, 'baseThickness', '安装耳板厚度必须低于拱座连接平台。')
  }
  return result()
}

export function archedClevisSupportFeatures(model) {
  const n = (key) => Number.isFinite(dimensionNumber(model?.[key])) ? Number(model[key]) : '待补全'
  return [
    { id: 'arch', icon: '∩', label: `拱座 R${n('archOuterRadius')} / R${n('archInnerRadius')}`, meta: '底部拱孔沿 Y 贯穿' },
    { id: 'clevis', icon: '∪', label: `双耳厚 ${n('earThickness')} · 净间距 ${n('earGap')}`, meta: `上耳 R${n('earRadius')} · 圆心 Z ${n('earCenterHeight')}` },
    { id: 'ear-holes', icon: '◌', label: `上耳横孔 2 × Ø${n('earHoleDiameter')}`, meta: '沿 Y 分别贯穿两侧上耳' },
    { id: 'mounting-ears', icon: '◉', label: `安装耳 R${n('mountEarRadius')} · 2 × Ø${n('mountHoleDiameter')}`, meta: `中心距 ${n('mountHoleCenterDistance')} · 仅贯穿 ${n('baseThickness')} mm 耳板` },
  ]
}
