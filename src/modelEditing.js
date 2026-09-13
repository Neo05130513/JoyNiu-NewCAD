import { sketchContractIssue } from './featureSketchContract.js'
import { cadPlanSignature } from './cadAgentState.js'
import { featureDefaults, featureDependencyIssues, initialFeatureDraft } from './directFeatureModel.js'

const VERSION = 'model-editing-session-v1'
const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_]{0,63}$/
const RUN = /^cad_[a-f0-9]{32}$/
const HASH = /^[a-f0-9]{64}$/
const required = {
  box: ['size'], cylinder: ['radius', 'height'], profile_extrude: ['plane', 'start', 'segments', 'distance'],
  profile_revolve: ['plane', 'start', 'segments', 'axisStart', 'axisEnd'], union: ['inputs'], cut: ['inputs'], intersect: ['inputs'], compound: ['inputs'],
  translate: ['input', 'vector'], rotate: ['input', 'axisStart', 'axisEnd', 'angle'], mirror: ['input', 'plane'], fillet: ['input', 'radius', 'edges'],
  chamfer: ['input', 'length', 'edges'], shell: ['input', 'thickness', 'faces'], linear_pattern: ['input', 'count', 'vector'],
  circular_pattern: ['input', 'count', 'axisStart', 'axisEnd', 'angle'], gear: ['module', 'teeth', 'pressureAngle', 'width'],
  spring: ['meanDiameter', 'wireDiameter', 'pitch', 'turns'], sweep: ['path', 'radius'], loft: ['sections'], profile_sweep:['plane','start','segments','path'],profile_loft:['sections'],surface_offset:['input','faces','distance'],move_face:['input','faces','distance'],surface_boundary:['input','edges'],surface_style:['points'],surface_join:['inputs'],surface_thicken:['input','faces','thickness'],body_edit:['input','bodies','action'],mesh_body:['vertices','triangles'],standard_part:['catalogId','dimensions'],import_step:['assetId','sha256'],
}
const planFields = new Set(['version', 'name', 'units', 'parameters', 'features', 'result', 'notes', 'questions','references','sketches','annotations','bodyStates'])
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value))
const identity = value => typeof value === 'string' && value.trim() && value.length <= 160
const dimension = value => typeof value === 'number' ? Number.isFinite(value) : typeof value === 'string' && value.trim() && value.length <= 256
const vector = (value, length) => Array.isArray(value) && value.length === length && value.every(dimension)
const blocked = (code, reason) => ({ editable: false, code, reason, sessionKey: null, initialPlanKey: null, initialDraft: null, sourceRun: null, source: null, previewCurrent: false })

function topologySelector(value, kind, seen, input) {
  const fields = ['kind', 'sourceFeatureId', 'geometryVersion', 'index', 'signature']
  return plain(value) && Object.keys(value).every(key => fields.includes(key) || key === 'binding') && fields.every(key => Object.hasOwn(value, key))
    && value.kind === kind && IDENTIFIER.test(value.sourceFeatureId) && seen.has(value.sourceFeatureId) && (!input || value.sourceFeatureId === input)
    && Number.isSafeInteger(value.index) && value.index >= 0 && value.index < (kind === 'edge' ? 6000 : 2000)
    && typeof value.geometryVersion === 'string' && HASH.test(value.geometryVersion) && typeof value.signature === 'string' && HASH.test(value.signature)
    && (value.binding === undefined || plain(value.binding) && Object.keys(value.binding).length === 2 && value.binding.version === 1 && typeof value.binding.key === 'string' && HASH.test(value.binding.key))
}

function planeAttachment(value) {
  return plain(value) && Object.keys(value).length === 4 && value.version === 1
    && ['origin','xDir','normal'].every(key => Array.isArray(value[key]) && value[key].length === 3 && value[key].every(item => typeof item === 'number' && Number.isFinite(item) && Math.abs(item) <= 1e6))
}

function selectorList(values, kind, seen, input) {
  return Array.isArray(values) && values.length >= 1 && values.length <= 64
    && values.every(value => topologySelector(value, kind, seen, input)) && new Set(values.map(value => value.index)).size === values.length
}

function sketchConstraintIssue(feature) { return sketchContractIssue(feature) }

// Reject lossy JSON coercion: NaN, undefined, functions, cycles and class instances
// must never become plausible dimensions or disappear from an imported plan.
function jsonCopy(value, maximum = 256_000) {
  const parents = new Set()
  let nodes = 0
  function inspect(item, depth) {
    if (++nodes > 100_000 || depth > 64) throw new Error('模型计划过于复杂。')
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return
    if (typeof item === 'number' && Number.isFinite(item)) return
    if ((!Array.isArray(item) && !plain(item)) || parents.has(item)) throw new Error('模型计划包含无法安全保存的数据。')
    parents.add(item)
    for (const child of Object.values(item)) inspect(child, depth + 1)
    parents.delete(item)
  }
  inspect(value, 0)
  const text = JSON.stringify(value)
  if (new TextEncoder().encode(text).length > maximum) throw new Error('模型计划超出编辑器大小限制。')
  return JSON.parse(text)
}

function planIssue(plan) {
  if (!plain(plan) || plan.version !== 'cad-plan-v1' || (plan.units !== undefined && plan.units !== 'mm')) return '仅支持毫米单位的 CAD 特征计划。'
  if (Object.keys(plan).some(key => !planFields.has(key))) return '模型包含当前特征编辑器不支持的计划字段。'
  if (plan.name !== undefined && (typeof plan.name !== 'string' || plan.name.length > 200)) return '模型名称格式无效。'
  for (const key of ['notes', 'questions']) {
    if (plan[key] !== undefined && (!Array.isArray(plan[key]) || plan[key].length > 100 || plan[key].some(value => typeof value !== 'string' || value.length > 2000))) return '模型说明格式无效。'
  }
  for(const key of ['references','sketches','annotations','bodyStates']) {
    if(plan[key]!==undefined&&(!Array.isArray(plan[key])||plan[key].length>(['references','sketches'].includes(key)?128:256)||plan[key].some(item=>!plain(item)||!IDENTIFIER.test(item.id))||new Set(plan[key].map(item=>item.id)).size!==plan[key].length))return '参考几何、草图或标注记录格式无效。'
  }
  if (!Array.isArray(plan.features) || !plan.features.length || plan.features.length > 128) return '模型没有完整的可编辑特征列表。'
  if (plan.parameters !== undefined && (!plain(plan.parameters) || Object.keys(plan.parameters).length > 100)) return '模型参数格式不支持直接编辑。'
  for (const [key, parameter] of Object.entries(plan.parameters || {})) {
    if (!IDENTIFIER.test(key) || !plain(parameter)) return '模型参数需要保留明确的名称和数值或表达式。'
    if (parameter.value != null && (typeof parameter.value !== 'number' || !Number.isFinite(parameter.value))) return `参数 ${key} 的数值格式无效。`
    if (parameter.expression != null && (typeof parameter.expression !== 'string' || parameter.expression.length > 256)) return `参数 ${key} 的表达式格式无效。`
    for (const field of ['label', 'question', 'status']) {
      if (parameter[field] !== undefined && (typeof parameter[field] !== 'string' || parameter[field].length > 2000)) return `参数 ${key} 的说明格式无效。`
    }
  }
  const seen = new Set()
  for (const feature of plan.features) {
    if (!plain(feature) || !IDENTIFIER.test(feature.id || '') || !Object.hasOwn(required, feature.op)) return '模型含有未知特征，不能根据预览猜测其建模结构。'
    if(feature.op==='import_step'&&(!/^asset_[a-f0-9]{32}$/.test(feature.assetId||'')||!HASH.test(feature.sha256||'')))return '导入实体缺少可信资产标识。'
    if (feature.label !== undefined && (typeof feature.label !== 'string' || feature.label.length > 200)) return `特征 ${feature.id} 的名称格式无效。`
    if (required[feature.op].some(key => !Object.hasOwn(feature, key) || feature[key] == null)) return `特征 ${feature.id} 缺少原始建模定义。`
    const profile = ['profile_extrude', 'profile_revolve','profile_sweep'].includes(feature.op)
    const allowed = new Set(['id', 'op', 'label', ...Object.keys(featureDefaults[feature.op]||{}),...required[feature.op],...(['rotate','circular_pattern','body_edit','profile_revolve'].includes(feature.op)?['axisReference']:[]), ...(feature.op === 'chamfer' ? ['length2'] : []),...(feature.op==='body_edit'?['axisStart','axisEnd','angle']:[]), ...(profile ? ['frame', 'planeSource', 'planeAttachment', 'sketchConstraints','contours','sketchId','planeReference'] : feature.op === 'mirror' ? ['frame','planeReference'] : [])])
    if (Object.keys(feature).some(key => !allowed.has(key))) return `特征 ${feature.id} 含有无法编辑的字段。`
    for (const [key, value] of Object.entries(feature)) {
      if (['id', 'op', 'label', 'input', 'inputs', 'size', 'origin', 'direction', 'vector', 'axisStart', 'axisEnd', 'start', 'segments', 'path', 'sections', 'frame', 'planeSource', 'planeAttachment', 'sketchConstraints','contours','sketchId','planeReference','bodies','points','vertices','triangles','catalogId','dimensions','assetId','sha256','axisReference','action','transition','continuity'].includes(key)) continue
      if (['fuse', 'lefthand', 'ruled', 'keepOriginal','isFrenet','makeSolid'].includes(key)) { if (typeof value !== 'boolean') return `特征 ${feature.id} 的选项格式无效。` }
      else if (key === 'faces') {
        const legacy = Array.isArray(value) && value.length >= 1 && value.length <= 5 && new Set(value).size === value.length && value.every(face => ['minX', 'maxX', 'minY', 'maxY', 'minZ', 'maxZ'].includes(face))
        if (!legacy && !selectorList(value, 'face', seen, feature.input)) return `特征 ${feature.id} 的面选择引用不完整。`
      }
      else if (key === 'plane') { if (!['XY', 'XZ', 'YZ', 'custom'].includes(value)) return `特征 ${feature.id} 的轮廓平面无效。` }
      else if (key === 'edges') { if (!['all', 'parallelX', 'parallelY', 'parallelZ'].includes(value) && !selectorList(value, 'edge', seen, feature.input)) return `特征 ${feature.id} 的边选择引用不完整。` }
      else if (!dimension(value)) return `特征 ${feature.id} 的尺寸定义无效。`
    }
    if (feature.input !== undefined && typeof feature.input !== 'string') return `特征 ${feature.id} 的实体引用无效。`
    if (feature.inputs !== undefined && (!Array.isArray(feature.inputs) || feature.inputs.length < (feature.op==='surface_join'?1:2) || feature.inputs.some(id => typeof id !== 'string'))) return `特征 ${feature.id} 的参与实体无效。`
    if (feature.op === 'compound' && (feature.inputs.length > 32 || new Set(feature.inputs).size !== feature.inputs.length)) return `特征 ${feature.id} 的多实体组合引用无效。`
    for (const key of ['size', 'origin', 'direction', 'vector', 'axisStart', 'axisEnd', 'start']) {
      if (feature[key] !== undefined && !vector(feature[key], key === 'start' || (feature.op === 'profile_revolve' && key.startsWith('axis')) ? 2 : 3)) return `特征 ${feature.id} 的坐标定义不完整。`
    }
    if (profile || feature.op === 'mirror') {
      if (feature.plane === 'custom') {
        if (!plain(feature.frame) || Object.keys(feature.frame).length !== 3 || !['origin', 'xDir', 'normal'].every(key => vector(feature.frame[key], 3)) || Object.hasOwn(feature, 'origin')) return `特征 ${feature.id} 的自定义平面坐标系不完整。`
        if (feature.planeSource !== undefined && !topologySelector(feature.planeSource, 'face', seen)) return `特征 ${feature.id} 的草图来源面引用不完整。`
        if (feature.planeAttachment !== undefined && (!feature.planeSource?.binding || !planeAttachment(feature.planeAttachment))) return `特征 ${feature.id} 的草图面附着数据不完整。`
      } else if (feature.frame !== undefined || feature.planeSource !== undefined || feature.planeAttachment !== undefined) return `特征 ${feature.id} 的坐标系需要自定义平面。`
    }

    if (profile) { const issue = sketchConstraintIssue(feature); if (issue) return `特征 ${feature.id}：${issue}` }
    if (feature.op==='sweep' && feature.path !== undefined && (!Array.isArray(feature.path) || feature.path.length < 2 || feature.path.some(point => !vector(point, 3)))) return `特征 ${feature.id} 的扫掠路径不完整。`
    if (feature.op==='loft' && feature.sections !== undefined && (!Array.isArray(feature.sections) || feature.sections.length < 2 || feature.sections.some(section => !plain(section) || !vector(section.origin, 3) || !(dimension(section.radius) || (dimension(section.width) && dimension(section.height)))))) return `特征 ${feature.id} 的放样截面不完整。`
    seen.add(feature.id)
  }
  return featureDependencyIssues(plan)[0] || ''
}

/**
 * Build an editor entry from the current feature plan, never from rendered
 * bounds, artifact URLs, a recipe name or stale generation.parameters.
 * Eligibility means a source draft can be edited; the server still validates
 * formulas, ownership, geometry and the original drawing confirmation gate.
 */
export function describeModelEditing({ accountKey, fileId, model, generation } = {}) {
  if (!identity(accountKey)) return blocked('account_required', '请登录后编辑自己的模型。')
  if (!identity(fileId)) return blocked('file_required', '请先打开或保存当前模型所属的项目文件。')
  if (!model?.cadPlan) return blocked(model?.recipeId || model?.kind && model.kind !== 'feature_model' ? 'feature_history_unavailable' : 'plan_unavailable',
    '当前模型没有 CAD 特征计划，无法从预览恢复特征历史；请保留原参数编辑或继续建模。')
  let plan
  try { plan = jsonCopy(model.cadPlan) } catch (error) { return blocked('invalid_plan', error.message) }
  const issue = planIssue(plan)
  if (issue) return blocked('unsupported_plan', issue)
  const signature = cadPlanSignature(plan)
  const modelRun = model.agentRun
  const generatedRun = generation?.kind === 'feature_model' && generation.planSignature === signature ? generation : null
  const hasModelRun = modelRun && (Object.hasOwn(modelRun, 'runId') || Object.hasOwn(modelRun, 'revision'))
  const run = hasModelRun ? modelRun : generatedRun
  if (run && (!RUN.test(run.runId || '') || !Number.isSafeInteger(run.revision) || run.revision < 1)) return blocked('invalid_source', '来源任务或版本不完整，请重新读取原模型后再编辑。')
  const sourceRun = run ? { runId: run.runId, revision: run.revision } : null
  const sessionKey = JSON.stringify([VERSION, accountKey, fileId, sourceRun?.runId || null, sourceRun?.revision || null, signature])
  const previewCurrent = Boolean(sourceRun && generation?.kind === 'feature_model' && !generation.stale && !modelRun?.dirty
    && generation.runId === sourceRun.runId && generation.revision === sourceRun.revision && generation.planSignature === signature)
  // The schema permits an omitted empty parameter map; the editor needs that
  // container. Preserve the original source signature and every geometry field.
  const editorPlan = { ...plan, parameters: plan.parameters || {} }
  const initialDraft = initialFeatureDraft(editorPlan)
  initialDraft.sourceRun = sourceRun ? { ...sourceRun } : null
  initialDraft.changeNote = sourceRun ? `从来源模型第 ${sourceRun.revision} 版继续手工特征编辑，重建后需重新核对。` : '从当前文件的 CAD 特征计划继续手工编辑，重建后需核对。'
  const unresolvedParameters = Object.entries(plan.parameters || {}).filter(([, parameter]) => parameter.value == null && !parameter.expression?.trim()).map(([key]) => key)
  return {
    editable: true, code: 'feature_plan', reason: unresolvedParameters.length ? '已保留原始特征；请补充未知尺寸后重建。' : previewCurrent ? '继续编辑当前模型的特征和参数。' : '继续编辑当前特征计划；修改后需要重新构建实体。',
    sessionKey, initialPlanKey: sessionKey, initialPlan: jsonCopy(editorPlan), initialDraft, sourceRun,
    source: { accountKey, fileId, name: plan.name || (typeof model.name === 'string' && model.name) || '未命名模型', runId: sourceRun?.runId || null, revision: sourceRun?.revision || null, planSignature: signature, status: typeof modelRun?.status === 'string' ? modelRun.status : null },
    previewCurrent, unresolvedParameters, requiresReview: true,
  }
}
