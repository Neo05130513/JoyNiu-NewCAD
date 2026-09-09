import { validateCadPlan } from './cadAgentState.js'
import { archedClevisSupportDefinition, validateArchedClevisSupport } from './archedClevisSupport.js'

const aliases = {
  arched_clevis_support_v1: 'arched_clevis_support',
  shaft_v1: 'shaft', bracket_support_v1: 'bracket',
  split_clamp_support_v1: 'split_clamp_support', split_clamp_pedestal: 'split_clamp_support',
  clamp_pedestal: 'split_clamp_support', circular_clamp: 'split_clamp_support', circular_clamp_v1: 'split_clamp_support',
  stepped_tapered_nozzle_with_insert_v1: 'stepped_tapered_nozzle', stepped_tapered_nozzle_v1: 'stepped_tapered_nozzle',
  tapered_nozzle_with_insert: 'stepped_tapered_nozzle',
}
const parameters = {
  arched_clevis_support: archedClevisSupportDefinition.required,
  shaft: ['outerDiameter', 'length', 'holeDiameter', 'keywayWidth', 'keywayDepth', 'keywayLength'],
  bracket: ['baseLength', 'baseWidth', 'baseThickness', 'upperLength', 'upperWidth', 'upperHeight', 'totalHeight', 'notchOpening', 'notchRadius', 'slotLength', 'slotWidth', 'pocketDepth', 'bossDiameter', 'bossCenterDistance'],
  split_clamp_support: ['baseLength', 'baseWidth', 'baseThickness', 'baseMainDepth', 'frontTongueWidth', 'rearBridgeWidth', 'totalHeight', 'pedestalOuterRadius', 'pedestalCenterFromRear', 'pedestalHeight', 'rearClampRise', 'boreDiameter', 'boreFloorZ', 'splitWidth', 'mountHoleCount', 'mountHoleDiameter', 'mountHoleCenterDistance', 'mountHoleCenterFromRear', 'crossHoleDiameter', 'crossHoleCenterZ', 'ribHeight', 'ribThickness', 'outerCornerRadius', 'neckConcaveRadius', 'neckConvexRadius'],
  stepped_tapered_nozzle: ['mainLength', 'headLength', 'neckLength', 'headLeftDiameter', 'headRightDiameter', 'neckDiameter', 'tipDiameter', 'counterboreDiameter', 'counterboreDepth', 'axialBoreDiameter', 'outletDiameter', 'outletTaperHalfAngle', 'insertOuterDiameter', 'insertLength', 'insertThreadDesignation', 'insertAxialOffset'],
}

export function canonicalModelKind(value) {
  const identities = typeof value === 'object' && value !== null
    ? [value.kind, value.partType, value.part_type, value.recipeId, value.recipe_id]
    : [value]
  const normalized = identities.map((item) => {
    const raw = String(item || '').trim().toLowerCase()
    return aliases[raw] || raw
  })
  return normalized.find((kind) => Object.hasOwn(parameters, kind)) || normalized.find(Boolean) || ''
}

export function validationParameterKeys(model) { return [...(parameters[canonicalModelKind(model)] || [])] }

export function validateModelParameters(model) {
  const kind = canonicalModelKind(model)
  if (kind === 'feature_model') return validateCadPlan(model)
  if (kind === 'arched_clevis_support') return validateArchedClevisSupport(model)
  const errors = []
  const add = (field, message) => { if (!errors.some((item) => item.field === field && item.message === message)) errors.push({ field, message }) }
  const check = (condition, field, message) => { if (!condition) add(field, message) }
  if (!Object.hasOwn(parameters, kind)) return { valid: false, errors: [{ field: 'kind', message: '尚不支持此零件类型，请选择已支持的参数化配方。' }], unsupported: true, kind }
  const isFiniteDimension = (value) => (typeof value === 'number' || (typeof value === 'string' && /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim()))) && Number.isFinite(Number(value))
  const n = (key) => Number(model?.[key])
  for (const key of parameters[kind]) {
    if (key === 'insertThreadDesignation') {
      const designation = String(model?.[key] || '').trim()
      check(designation.length <= 32 && /^M\d+(?:\.\d+)?(?:[X×]\d+(?:\.\d+)?)?$/i.test(designation), key, '请输入不超过 32 字符的公制螺纹标注，例如 M12 或 M12×1.5。')
      continue
    }
    const value = model?.[key]
    check(isFiniteDimension(value) && (key === 'insertAxialOffset' ? n(key) >= 0 : n(key) > 0), key,
      key === 'insertAxialOffset' ? '轴向偏置必须是大于或等于 0 的有限数值。' : '请填写大于 0 的有限尺寸。')
  }
  if (model?.units !== undefined && model.units !== 'mm') add('units', '当前配方使用毫米，请先将尺寸换算为 mm。')
  if (kind === 'bracket') for (const field of ['holeDepth', 'saddleDepth', 'bossHeight']) {
    if (model[field] !== undefined && model[field] !== null) check(isFiniteDimension(model[field]), field, '辅助尺寸必须是有限数值，也可以留空以使用配方默认值。')
  }
  // Do not calculate relationships using blank, NaN, or infinite inputs.
  if (errors.length) return { valid: false, errors, unsupported: false, kind }
  if (kind === 'shaft') {
    check(n('holeDiameter') < n('outerDiameter'), 'holeDiameter', '通孔直径必须小于外径，并保留轴壁。')
    check(n('keywayLength') <= n('length'), 'keywayLength', '键槽长度不能超过轴的总长度。')
    check(n('keywayWidth') < n('outerDiameter'), 'keywayWidth', '键槽宽度必须小于轴的外径。')
    const radius = n('outerDiameter') / 2
    const floor = radius - n('keywayDepth')
    check(floor > n('holeDiameter') / 2, 'keywayDepth', '键槽底面必须高于通孔，槽深应小于（外径 − 孔径）/ 2。')
    const chord = 2 * Math.sqrt(Math.max(0, radius * radius - floor * floor))
    check(n('keywayWidth') <= chord + 1e-7, 'keywayWidth', '此槽深处的轴截面不足以容纳键槽宽度，请减小槽宽或调整槽深。')
  } else if (kind === 'bracket') {
    const epsilon = 1e-6
    check(n('upperLength') <= n('baseLength') + epsilon, 'upperLength', '上部长度不能超过底板长度。')
    check(n('upperWidth') <= n('baseWidth') + epsilon, 'upperWidth', '上部宽度不能超过底板宽度。')
    check(Math.abs(n('totalHeight') - n('baseThickness') - n('upperHeight')) <= 0.01, 'totalHeight', '总高必须等于底板厚度 + 上部高度。')
    check(n('notchOpening') < n('upperLength') - epsilon, 'notchOpening', '鞍槽开口必须小于上部长度，以保留侧壁。')
    check(n('notchRadius') <= n('notchOpening') / 2 + epsilon, 'notchRadius', '鞍槽直径不能大于开口宽度。')
    check(n('totalHeight') - n('notchRadius') > n('baseThickness') + epsilon && n('notchRadius') <= n('upperHeight') + epsilon, 'notchRadius', '鞍槽底必须高于底板上表面。')
    check(n('slotWidth') <= n('notchOpening') / 2 + epsilon, 'slotWidth', '单侧浅槽宽度不能超过鞍槽开口的一半。')
    check(n('slotLength') <= n('upperWidth') + epsilon, 'slotLength', '浅槽长度不能超过上部宽度。')
    check(n('pocketDepth') <= n('upperHeight') + epsilon, 'pocketDepth', '浅槽深度不能超过上部高度。')
    check(n('bossCenterDistance') + n('bossDiameter') <= n('baseLength') + epsilon, 'bossCenterDistance', '孔中心距 + 孔径不能超过底板长度。')
    check(n('bossDiameter') <= n('baseWidth') + epsilon, 'bossDiameter', '贯穿孔直径不能超过底板宽度。')
    check(n('bossCenterDistance') >= n('bossDiameter') - epsilon, 'bossCenterDistance', '两个贯穿孔不能相互重叠。')
    if (model.holeThrough !== undefined) check(model.holeThrough === true, 'holeThrough', '当前支架配方要求两个孔贯穿全高。')
    // The API resolves through-hole depth to totalHeight, ignoring legacy holeDepth/bossHeight.
    const saddleDepth = model.saddleDepth == null ? n('baseWidth') : n('saddleDepth')
    check(saddleDepth > 0 && Math.abs(Math.min(saddleDepth, n('upperWidth')) - Math.min(n('baseWidth'), n('upperWidth'))) <= 0.01, 'saddleDepth', '鞍槽深度必须贯穿上部宽度。')
  } else if (kind === 'split_clamp_support') {
    const radius = n('pedestalOuterRadius'), lowerTop = n('baseThickness') + n('pedestalHeight')
    const tongueDepth = n('baseWidth') - n('baseMainDepth'), shoulder = (n('baseLength') - n('frontTongueWidth')) / 2
    check(n('mountHoleCount') === 2, 'mountHoleCount', '当前夹紧座配方支持 2 个对称安装孔。')
    check(tongueDepth > 0, 'baseMainDepth', '主段深度必须小于底板总深度，以保留前舌。')
    check(shoulder > 0, 'frontTongueWidth', '前舌宽度必须小于底板总长，以保留圆角肩部。')
    check(2 * radius <= n('rearBridgeWidth') && n('rearBridgeWidth') <= n('baseLength'), 'rearBridgeWidth', '后桥宽度必须介于夹座外径与底板总长之间。')
    check(n('outerCornerRadius') <= Math.min(n('baseMainDepth'), n('baseLength')) / 2, 'outerCornerRadius', '底板圆角不能超过主板最小边长的一半。')
    check(n('neckConcaveRadius') <= Math.min(tongueDepth, shoulder), 'neckConcaveRadius', '肩部内凹圆角超出前舌深度或肩宽。')
    check(n('neckConvexRadius') <= Math.min(tongueDepth, n('frontTongueWidth') / 2), 'neckConvexRadius', '前舌外圆角超出前舌边界。')
    check(n('neckConcaveRadius') + n('neckConvexRadius') <= tongueDepth, 'neckConcaveRadius', '内凹和外凸圆角半径之和不能超过前舌深度。')
    check(2 * radius <= n('baseLength'), 'pedestalOuterRadius', '夹座外径不能超过底板长度。')
    check(n('pedestalCenterFromRear') >= radius && n('pedestalCenterFromRear') + radius <= n('baseWidth'), 'pedestalCenterFromRear', '夹座轴线位置必须让圆弧轮廓落在底板范围内。')
    check(n('boreDiameter') < 2 * radius, 'boreDiameter', '中央孔直径必须小于夹座外径。')
    check(n('boreFloorZ') > n('baseThickness') && n('boreFloorZ') < lowerTop, 'boreFloorZ', '盲孔底面必须位于底板顶面与低夹座顶面之间。')
    check(n('splitWidth') < n('boreDiameter'), 'splitWidth', '开缝宽度必须小于中央孔直径。')
    check(n('mountHoleCenterDistance') + n('mountHoleDiameter') <= n('baseLength'), 'mountHoleCenterDistance', '安装孔中心距 + 孔径不能超过底板长度。')
    check(n('mountHoleCenterFromRear') >= n('mountHoleDiameter') / 2 && n('baseMainDepth') - n('mountHoleCenterFromRear') >= n('mountHoleDiameter') / 2, 'mountHoleCenterFromRear', '安装孔不能超出主底板前后边界。')
    check(Math.hypot(n('mountHoleCenterDistance') / 2, n('pedestalCenterFromRear') - n('mountHoleCenterFromRear')) > n('mountHoleDiameter') / 2 + radius, 'mountHoleCenterDistance', '安装孔与夹座外轮廓相交，请调整孔距。')
    const crossR = n('crossHoleDiameter') / 2
    check(n('crossHoleCenterZ') >= n('baseThickness') + crossR && n('crossHoleCenterZ') <= n('totalHeight') - crossR, 'crossHoleCenterZ', '横孔必须落在夹座上下边界内。')
    check(Math.abs(n('crossHoleCenterZ') - lowerTop) <= crossR, 'crossHoleCenterZ', '横孔必须跨过低夹座顶面，才能连通配方中的夹紧区。')
    check(n('ribHeight') <= n('pedestalHeight'), 'ribHeight', '加强筋高度不能超过低夹座高度。')
    check(Math.abs(n('totalHeight') - lowerTop - n('rearClampRise')) <= 0.01, 'totalHeight', '总高必须等于底板厚度 + 低夹座高度 + 后夹耳加高。')
  } else {
    const tipLength = n('mainLength') - n('headLength') - n('neckLength')
    check(tipLength > 0, 'mainLength', '主件总长必须大于前段长 + 颈段长，以保留末段。')
    check(n('tipDiameter') <= n('neckDiameter'), 'tipDiameter', '末段直径不能超过颈段直径。')
    check(n('neckDiameter') < Math.min(n('headLeftDiameter'), n('headRightDiameter')), 'neckDiameter', '颈段直径必须小于前段两端直径。')
    check(n('counterboreDiameter') < Math.min(n('headLeftDiameter'), n('headRightDiameter')), 'counterboreDiameter', '沉孔必须小于前段外径，以保留孔壁。')
    check(n('counterboreDiameter') > n('insertOuterDiameter'), 'insertOuterDiameter', '镶件外径必须小于沉孔直径。')
    check(n('neckDiameter') > n('axialBoreDiameter'), 'axialBoreDiameter', '轴向孔必须小于颈段外径。')
    check(n('tipDiameter') > n('outletDiameter') && n('outletDiameter') > n('axialBoreDiameter'), 'outletDiameter', '出口直径必须大于轴向孔径且小于末段外径。')
    check(n('counterboreDepth') <= n('headLength'), 'counterboreDepth', '沉孔深度不能超过前段长度。')
    check(n('insertAxialOffset') + n('insertLength') <= n('counterboreDepth'), 'insertLength', '镶件偏置 + 长度不能超过沉孔深度。')
    const threadDiameter = Number(String(model.insertThreadDesignation).trim().match(/^M(\d+(?:\.\d+)?)/i)?.[1])
    check(threadDiameter > 0 && threadDiameter < n('insertOuterDiameter'), 'insertThreadDesignation', '螺纹公称直径必须小于镶件外径。')
    check(n('outletTaperHalfAngle') < 90, 'outletTaperHalfAngle', '出口锥半角必须小于 90°。')
    const taperLength = (n('outletDiameter') - n('axialBoreDiameter')) / (2 * Math.tan(n('outletTaperHalfAngle') * (Math.PI / 180)))
    check(Number.isFinite(taperLength) && taperLength > 0 && taperLength <= tipLength, 'outletTaperHalfAngle', '此出口锥角形成的锥深不能超过末段长度。')
  }
  return { valid: errors.length === 0, errors, unsupported: false, kind }
}
