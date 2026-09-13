export const featureNames = { box: '长方体', cylinder: '圆柱', profile_extrude: '轮廓拉伸', profile_revolve: '轮廓旋转', union: '布尔合并', cut: '布尔切除', intersect: '布尔交集', compound: '多实体组合', translate: '平移', rotate: '旋转定位', mirror: '镜像', fillet: '圆角', chamfer: '倒角', shell: '抽壳', linear_pattern: '线性阵列', circular_pattern: '环形阵列', gear: '渐开线齿轮', spring: '螺旋弹簧', sweep: '圆截面扫掠', loft: '截面放样' }
Object.assign(featureNames,{profile_sweep:'扫掠',profile_loft:'放样',surface_offset:'偏移曲面',move_face:'移动面',surface_boundary:'边界混合',surface_style:'样式曲面',surface_join:'曲面连接',surface_thicken:'曲面加厚',body_edit:'实体编辑',mesh_body:'网格实体',standard_part:'标准件',import_step:'导入实体'})
export const fieldNames = { size: '长 / 宽 / 高', origin: '原点', radius: '半径', height: '高度', direction: '轴线方向', vector: '每步位移', distance: '拉伸距离', angle: '角度', axisStart: '轴起点', axisEnd: '轴终点', start: '轮廓起点', module: '模数', teeth: '齿数', pressureAngle: '压力角', width: '齿宽', boreDiameter: '轴孔直径', profileShift: '变位系数', backlash: '圆周齿隙', meanDiameter: '弹簧中径', wireDiameter: '线径', pitch: '螺距', turns: '圈数', count: '实例数量', length: '倒角距离', length2: '第二倒角距离', thickness: '壁厚（负值向内）' }
export const featureDefaults = {
  mirror: { input: '', plane: 'YZ', origin: [0, 0, 0], keepOriginal: true },
  box: { size: [20, 20, 10], origin: [0, 0, 0] }, cylinder: { radius: 5, height: 20, origin: [0, 0, 0], direction: [0, 0, 1] },
  profile_extrude: { plane: 'XY', origin: [0, 0, 0], start: [0, 0], segments: [{ type: 'line', to: [20, 0] }, { type: 'line', to: [20, 10] }, { type: 'line', to: [0, 10] }], distance: 5 },
  profile_revolve: { plane: 'XZ', origin: [0, 0, 0], start: [5, 0], segments: [{ type: 'line', to: [10, 0] }, { type: 'line', to: [10, 20] }, { type: 'line', to: [5, 20] }], axisStart: [0, 0], axisEnd: [0, 1], angle: 360 },
  translate: { input: '', vector: [10, 0, 0] }, rotate: { input: '', axisStart: [0, 0, 0], axisEnd: [0, 0, 1], angle: 90 },
  union: { inputs: [] }, cut: { inputs: [] }, intersect: { inputs: [] }, compound: {inputs:[]}, fillet: { input: '', radius: 1, edges: 'all' },
  chamfer: { input: '', length: 1, edges: 'all' }, shell: { input: '', thickness: -1, faces: ['maxZ'] },
  linear_pattern: { input: '', count: 3, vector: [25, 0, 0], fuse: false },
  circular_pattern: { input: '', count: 4, axisStart: [0, 0, 0], axisEnd: [0, 0, 1], angle: 360, fuse: false },
  gear: { module: 2, teeth: 24, pressureAngle: 20, width: 8, boreDiameter: 8, profileShift: 0, backlash: 0, origin: [0, 0, 0] },
  spring: { meanDiameter: 20, wireDiameter: 2, pitch: 5, turns: 3, lefthand: false, origin: [0, 0, 0] },
  sweep: { radius: 2, path: [[0, 0, 0], [0, 0, 20], [15, 0, 20]] },
  loft: { sections: [{ origin: [0, 0, 0], radius: 8 }, { origin: [0, 0, 20], radius: 4 }], ruled: false },
}
Object.assign(featureDefaults,{
  profile_sweep:{plane:'XY',origin:[0,0,0],start:[0,0],segments:[],path:{start:[0,0,0],segments:[]},isFrenet:false,transition:'transformed'},
  profile_loft:{sections:[],ruled:false},
  surface_offset:{input:'',faces:[],distance:1,keepOriginal:true},move_face:{input:'',faces:[],distance:1},
  surface_boundary:{input:'',edges:[],keepOriginal:true,continuity:'position'},surface_style:{points:[],tolerance:0.01},
  surface_join:{inputs:[],tolerance:0.00001,makeSolid:false},surface_thicken:{input:'',faces:[],thickness:1,keepOriginal:false},
  body_edit:{input:'',bodies:[],action:'translate',vector:[0,0,0]},mesh_body:{vertices:[],triangles:[]},standard_part:{catalogId:'washer-m8',dimensions:{innerDiameter:8.4,outerDiameter:16,length:1.6}},
})

export const cloneFeatureValue = value => JSON.parse(JSON.stringify(value))
export function featureScalar(value) {
  const text = String(value).trim()
  return text !== '' && !text.endsWith('.') && Number.isFinite(Number(text)) ? Number(text) : text
}
export function newFeature(op, features = []) {
  if (!Object.hasOwn(featureDefaults, op)) throw new Error('请选择支持的建模操作。')
  let suffix = 1
  while (features.some(feature => feature.id === `${op}${suffix}`)) suffix += 1
  const feature = { id: `${op}${suffix}`, op, label: `${featureNames[op]} ${suffix}`, ...cloneFeatureValue(featureDefaults[op]) }
  if ('input' in feature) feature.input = features.at(-1)?.id || ''
  if ('inputs' in feature) feature.inputs = features.slice(-2).map(item => item.id)
  return feature
}
export function initialFeatureDraft(initialPlan) {
  const plan = initialPlan ? cloneFeatureValue(initialPlan) : { version: 'cad-plan-v1', units: 'mm', name: '手工特征设计', parameters: {}, features: [newFeature('box')], result: 'box1' }
  return { name: plan.name || '手工特征设计', plan, suppressed: [], changeNote: '创建手工特征设计', sourceRun: null }
}
export function featureDependencyIssues(plan, suppressed = []) {
  const seen = new Set(), disabled = new Set(suppressed), issues = []
  for (const feature of plan.features) {
    if (seen.has(feature.id)) issues.push(`特征 ID ${feature.id} 重复。`)
    const refs = [...new Set([...(feature.input ? [feature.input] : feature.inputs || []), ...(feature.planeSource?.sourceFeatureId ? [feature.planeSource.sourceFeatureId] : [])])]
    for (const ref of refs) {
      if (!seen.has(ref)) issues.push(`${feature.label || feature.id} 依赖前置特征 ${ref}；请恢复顺序或修改引用。`)
      if (!disabled.has(feature.id) && disabled.has(ref)) issues.push(`${feature.label || feature.id} 依赖已抑制的 ${ref}；请同时抑制依赖项或修改引用。`)
    }
    seen.add(feature.id)
  }
  if (!(plan.features.length===0 && plan.result==='' && (plan.sketches?.length||plan.references?.length)) && (!seen.has(plan.result) || disabled.has(plan.result))) issues.push('请选择存在且未抑制的输出特征。')
  return [...new Set(issues)]
}
export function deleteFeature(draft, id) {
  const dependent = [...draft.plan.features,...(draft.plan.sketches||[]),...(draft.plan.annotations||[])].filter(feature => feature.input === id || feature.inputs?.includes(id) || feature.planeSource?.sourceFeatureId === id || feature.references?.some(ref=>ref.sourceFeatureId===id))
  if (dependent.length) throw new Error(`无法删除；这些特征仍引用它：${dependent.map(item => item.label || item.id).join('、')}。请先修改引用或删除依赖项。`)
  const next = cloneFeatureValue(draft)
  next.plan.features = next.plan.features.filter(feature => feature.id !== id)
  if (!next.plan.features.length) throw new Error('至少保留一个建模特征。')
  next.suppressed = next.suppressed.filter(item => item !== id)
  if (next.plan.result === id) next.plan.result = next.plan.features.filter(item => !next.suppressed.includes(item.id)).at(-1)?.id || ''
  return next
}
export function moveFeature(draft, id, delta) {
  const next = cloneFeatureValue(draft), list = next.plan.features, index = list.findIndex(item => item.id === id), target = index + delta
  if (index < 0 || target < 0 || target >= list.length) return next
  ;[list[index], list[target]] = [list[target], list[index]]
  const problems = featureDependencyIssues(next.plan, next.suppressed)
  if (problems.length) throw new Error(problems[0])
  return next
}
export function workspacePayload(draft, record, fileId) {
  const problems = featureDependencyIssues(draft.plan, draft.suppressed)
  if (problems.length) throw new Error(problems[0])
  if (!draft.changeNote.trim()) throw new Error('请填写这次修改的用途或说明。')
  const value = cloneFeatureValue(draft)
  for (const [key, parameter] of Object.entries(value.plan.parameters)) {
    if (Object.hasOwn(parameter, 'expression') || parameter.value === null) continue
    if (typeof parameter.value === 'string' && parameter.value.trim() && Number.isFinite(Number(parameter.value))) parameter.value = Number(parameter.value)
    if (typeof parameter.value !== 'number' || !Number.isFinite(parameter.value)) throw new Error(`参数 ${key} 需要直接数值；公式请切换为“关联表达式”。`)
  }
  return { ...value, fileId: record?.fileId || fileId, ...(record ? { expectedRevision: record.revision } : {}) }
}

// Memory only: tab navigation retains unsaved CAD work without persisting
// account tokens or design contents into browser storage.
const workspaceDrafts = new Map()
export function featureWorkspaceKey(token, caseKey, accountKey) { return JSON.stringify([accountKey || token || '', caseKey || 'manual']) }
export function cacheFeatureWorkspace(key, value) {
  workspaceDrafts.delete(key)
  workspaceDrafts.set(key, cloneFeatureValue(value))
  while (workspaceDrafts.size > 12) workspaceDrafts.delete(workspaceDrafts.keys().next().value)
}
export function cachedFeatureWorkspace(key) {
  const value = workspaceDrafts.get(key)
  return value ? cloneFeatureValue(value) : null
}
export function draftFromFeatureRecord(record) {
  return {name: record.name, plan: cloneFeatureValue(record.plan), suppressed: [...(record.suppressed || [])], changeNote: record.changeNote, sourceRun: record.sourceRun || null}
}

export function featureErrorFeedback(error) {
  const original = String(error?.message || error || '')
  const rules = [
    [/fillet failed:.*$/gi, '圆角无法在所选边上生成；请减小半径，或调整相邻边的选择'],
    [/chamfer failed:.*$/gi, '倒角无法在所选边上生成；请减小倒角距离，或调整所选边'],
    [/shell failed:.*$/gi, '抽壳无法生成；请减小壁厚，或重新选择开口面'],
    [/Fillet edges must be all, parallelX, parallelY or parallelZ/gi, '请选择模型上需要倒圆角的边'],
    [/box size must be greater than [\d.e+-]+ mm/gi, '长方体的长、宽、高必须大于 0'],
    [/\b(arc radius|fillet radius|sweep radius|loft radius|radius|height|gear module|gear width|chamfer second length|chamfer length|loft width|loft height|spring mean diameter|spring wire diameter|spring pitch) must be greater than [\d.e+-]+ mm/gi, (_match, name) => `${{'arc radius': '圆弧半径', 'fillet radius': '圆角半径', 'sweep radius': '扫掠半径', 'loft radius': '放样截面半径', radius: '半径', height: '高度', 'gear module': '齿轮模数', 'gear width': '齿宽', 'chamfer second length': '第二侧倒角距离', 'chamfer length': '倒角距离', 'loft width': '放样截面宽度', 'loft height': '放样截面高度', 'spring mean diameter': '弹簧中径', 'spring wire diameter': '弹簧线径', 'spring pitch': '弹簧螺距'}[name.toLowerCase()]}必须大于 0`],
    [/inner diameter must be (?:less|smaller) than (?:the )?outer diameter/gi, '内径必须小于外径'],
    [/inner radius must be (?:less|smaller) than (?:the )?outer radius/gi, '内半径必须小于外半径'],
    [/inner\s*>=\s*outer/gi, '内尺寸必须小于对应外尺寸'],
    [/Feature produced an empty or invalid solid/gi, '此特征未生成有效实体；请检查尺寸、孔径和参与实体的位置关系'],
    [/Input must name an earlier feature/gi, '引用实体必须是排在当前特征之前的特征，请调整顺序或修改引用'],
    [/Boolean inputs must contain 2 to 32 earlier feature IDs/gi, '布尔运算须选择 2 至 32 个前置特征，请检查参与实体及顺序'],
    [/Expression references unknown parameters:\s*/gi, '表达式引用了不存在的参数，请补充或修正：'],
    [/expression references unknown parameters/gi, '表达式引用了不存在的参数，请补充或修正'],
    [/Derived parameters contain a cycle or reference unknown names:\s*/gi, '参数存在循环依赖或引用了不存在的名称，请检查：'],
    [/Invalid dimension expression/gi, '尺寸表达式无效，请检查参数名和运算符'],
    [/Shell thickness must be nonzero; negative hollows inward/gi, '抽壳壁厚不能为 0；负值表示向内抽壳'],
    [/Profile extrusion distance must be nonzero/gi, '轮廓拉伸距离不能为 0'],
    [/Cylinder direction must be nonzero/gi, '圆柱轴线方向不能为零向量'],
    [/result must name a feature/gi, '请选择一个存在的特征作为输出实体'],
  ]
  let message = original
  for (const [pattern, replacement] of rules) message = message.replace(pattern, replacement)
  return {message, detail: message === original ? '' : original}
}
