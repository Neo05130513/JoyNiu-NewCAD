import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describeModelEditing } from './modelEditing.js'
import { cadPlanSignature } from './cadAgentState.js'
import { cacheFeatureWorkspace, cachedFeatureWorkspace, featureWorkspaceKey, featureDefaults, draftFromFeatureRecord, workspacePayload } from './directFeatureModel.js'
import { MECHANICAL_CASES } from './mechanicalCases.js'

const RUN_A = `cad_${'a'.repeat(32)}`, RUN_B = `cad_${'b'.repeat(32)}`
function plan() {
  return { version: 'cad-plan-v1', name: '带孔长方体', units: 'mm',
    parameters: { length: { value: 40, label: '长度', source: { type: 'user', text: '40 mm' } }, width: { value: 20 }, height: { value: 10 }, halfWidth: { value: null, expression: 'width / 2', label: '孔中心 Y' } },
    features: [ { id: 'body', op: 'box', size: ['length', 'width', 'height'], origin: [0, 0, 0] },
      { id: 'hole', op: 'cylinder', radius: 2, height: 'height + 2', origin: ['length / 2', 'halfWidth', -1], direction: [0, 0, 1] },
      { id: 'cutHole', op: 'cut', inputs: ['body', 'hole'] } ], result: 'cutHole', notes: ['仅以给定尺寸继续编辑。'] }
}
function context() {
  const cadPlan = plan()
  return { accountKey: 'user-a', fileId: 'file-a', model: { kind: 'feature_model', cadPlan, agentRun: { runId: RUN_A, revision: 2, status: 'ready', dirty: false, downloadToken: 'must-not-copy' } },
    generation: { kind: 'feature_model', runId: RUN_A, revision: 2, planSignature: cadPlanSignature(cadPlan), stale: false, validation: { valid: true }, artifacts: [{ format: 'glb', url: '/private-url' }] } }
}
const clone = value => structuredClone(value)

test('generated models enter with exact feature order, expressions, source evidence and source revision', () => {
  const input = context(), before = clone(input), result = describeModelEditing(input)
  assert.equal(result.editable, true); assert.equal(result.previewCurrent, true)
  assert.deepEqual(result.initialDraft.plan, input.model.cadPlan)
  assert.deepEqual(result.initialPlan, input.model.cadPlan)
  assert.deepEqual(result.sourceRun, { runId: RUN_A, revision: 2 })
  assert.deepEqual(result.initialDraft.sourceRun, result.sourceRun)
  assert.equal(result.source.fileId, 'file-a'); assert.equal(result.source.name, '带孔长方体')
  assert.equal(result.initialDraft.plan.parameters.halfWidth.expression, 'width / 2')
  assert.equal(result.requiresReview, true)
  assert.equal(JSON.stringify(result).includes('must-not-copy'), false)
  assert.equal(JSON.stringify(result).includes('/private-url'), false)
  result.initialDraft.plan.features[0].size[0] = 99
  result.initialDraft.sourceRun.revision = 3
  assert.deepEqual(input, before); assert.equal(result.sourceRun.revision, 2)
})

test('session identity is stable for JSON object order and token or artifact refresh', () => {
  const input = context(), original = describeModelEditing(input)
  const reordered = clone(input)
  reordered.model.cadPlan = Object.fromEntries(Object.entries(reordered.model.cadPlan).reverse())
  reordered.model.cadPlan.parameters = Object.fromEntries(Object.entries(reordered.model.cadPlan.parameters).reverse())
  reordered.model.agentRun.access_token = 'new-token'
  reordered.generation.artifacts[0].url = '/fresh-signed-url'
  assert.equal(describeModelEditing(reordered).sessionKey, original.sessionKey)
  assert.equal(original.sessionKey, original.initialPlanKey)
  assert.equal(JSON.parse(original.sessionKey)[5], cadPlanSignature(input.model.cadPlan))
})

test('accounts, files, run identities, revisions and actual plan edits never share sessions', () => {
  const input = context(), original = describeModelEditing(input).sessionKey
  for (const change of [ c => { c.accountKey = 'user-b' }, c => { c.fileId = 'file-b' }, c => { c.model.agentRun.runId = RUN_B },
    c => { c.model.agentRun.revision = 3 }, c => { c.model.cadPlan.parameters.width.value = 25 },
    c => { c.model.cadPlan.features = [c.model.cadPlan.features[1], c.model.cadPlan.features[0], c.model.cadPlan.features[2]] } ]) {
    const next = clone(input); change(next)
    const result = describeModelEditing(next)
    assert.equal(result.editable, true); assert.notEqual(result.sessionKey, original)
  }
})

test('the existing feature cache restores edits for the same model source without cross-file leakage', () => {
  const input = context(), entry = describeModelEditing(input)
  const cacheKey = featureWorkspaceKey(null, entry.initialPlanKey, input.accountKey)
  const snapshot = { draft: clone(entry.initialDraft), record: { id: 'feature_saved', fileId: input.fileId, revision: 3 }, dirty: true, history: [clone(entry.initialDraft)], future: [] }
  snapshot.draft.plan.parameters.width.value = 27
  cacheFeatureWorkspace(cacheKey, snapshot)
  const returned = describeModelEditing(clone(input))
  const recovered = cachedFeatureWorkspace(featureWorkspaceKey(null, returned.initialPlanKey, input.accountKey))
  assert.equal(recovered.draft.plan.parameters.width.value, 27)
  assert.equal(recovered.record.id, 'feature_saved'); assert.equal(recovered.history.length, 1)
  const other = describeModelEditing({ ...input, fileId: 'another-file' })
  assert.equal(cachedFeatureWorkspace(featureWorkspaceKey(null, other.initialPlanKey, input.accountKey)), null)
})

test('failed or stale generation does not block editing a retained plan and never substitutes older geometry parameters', () => {
  const input = context(); input.model.agentRun.status = 'failed'; input.model.agentRun.dirty = true
  input.generation = { ...input.generation, runId: RUN_B, stale: true, parameters: { width: 900 } }
  const result = describeModelEditing(input)
  assert.equal(result.editable, true); assert.equal(result.previewCurrent, false)
  assert.equal(result.initialDraft.plan.parameters.width.value, 20)
  assert.deepEqual(result.sourceRun, { runId: RUN_A, revision: 2 })
  assert.match(result.reason, /重新构建/)
})

test('unresolved source dimensions stay unknown and formulas stay expressions', () => {
  const input = context(); input.model.cadPlan.parameters.length.value = null
  input.generation = null; input.model.agentRun.status = 'needs_input'
  const result = describeModelEditing(input)
  assert.equal(result.editable, true); assert.deepEqual(result.unresolvedParameters, ['length'])
  assert.equal(result.initialDraft.plan.parameters.length.value, null)
  assert.equal(result.initialDraft.plan.parameters.halfWidth.value, null)
  assert.equal(result.initialDraft.plan.parameters.halfWidth.expression, 'width / 2')
  assert.match(result.reason, /补充未知尺寸/)
})

test('parameter-free plans get only the schema empty map while retaining their source signature', () => {
  const input = context()
  input.model.cadPlan = { version: 'cad-plan-v1', features: [{ id: 'body', op: 'box', size: [40, 20, 10] }], result: 'body' }
  input.generation.planSignature = cadPlanSignature(input.model.cadPlan)
  const result = describeModelEditing(input)
  assert.equal(result.editable, true); assert.equal(result.previewCurrent, true)
  assert.deepEqual(result.initialDraft.plan.parameters, {})
  assert.deepEqual(result.initialPlan, { ...input.model.cadPlan, parameters: {} })
  assert.equal(result.source.planSignature, input.generation.planSignature)
  assert.equal(Object.hasOwn(input.model.cadPlan, 'parameters'), false)
})
test('saved independent-body compounds reopen with all inputs and source history while duplicate inputs are rejected',()=>{
  const input=context()
  input.model.cadPlan={version:'cad-plan-v1',parameters:{},features:[{id:'body1',op:'box',size:[10,10,10]},{id:'body2',op:'box',origin:[10,0,0],size:[10,10,10]},{id:'group',op:'compound',label:'两个独立实体',inputs:['body1','body2']}],result:'group'}
  const entry=describeModelEditing(input)
  assert.equal(entry.editable,true);assert.deepEqual(entry.initialDraft.plan,input.model.cadPlan);assert.deepEqual(entry.sourceRun,{runId:input.model.agentRun.runId,revision:input.model.agentRun.revision})
  input.model.cadPlan.features.at(-1).inputs=['body1','body1']
  assert.equal(describeModelEditing(input).editable,false)
})

test('all supported feature operations and real mechanical case plans remain editable without geometry substitution', () => {
  for (const [op, defaults] of Object.entries(featureDefaults)) {
    const feature = { id: 'part', op, ...clone(defaults) }
    if ('input' in feature) feature.input = 'base'
    if ('inputs' in feature) feature.inputs = ['base', 'tool']
    if(op==='profile_sweep')Object.assign(feature,{start:[0,0],segments:[{type:'line',to:[2,0]},{type:'line',to:[2,2]},{type:'line',to:[0,2]}],path:{start:[0,0,0],segments:[{type:'line',to:[0,0,20]}]}})
    if(['surface_offset','move_face','surface_thicken'].includes(op))feature.faces=['maxZ']
    if(op==='surface_boundary')feature.edges='all'
    const cadPlan = { version: 'cad-plan-v1', parameters: {}, features: [{ id: 'base', op: 'box', size: [20, 20, 20] }, { id: 'tool', op: 'cylinder', radius: 5, height: 20 }, feature], result: 'part' }
    const result = describeModelEditing({ ...context(), model: { cadPlan } })
    assert.equal(result.editable, true, `${op}: ${result.reason}`)
    assert.deepEqual(result.initialDraft.plan, cadPlan)
  }
  for (const item of MECHANICAL_CASES) {
    const result = describeModelEditing({ ...context(), model: { cadPlan: item.plan } })
    assert.equal(result.editable, true, `${item.id}: ${result.reason}`)
    assert.deepEqual(result.initialDraft.plan, item.plan)
  }
})

test('generation may supply source identity only when its complete plan signature matches', () => {
  const input = context(); delete input.model.agentRun
  assert.deepEqual(describeModelEditing(input).sourceRun, { runId: RUN_A, revision: 2 })
  input.generation.planSignature = 'older-plan'
  const result = describeModelEditing(input)
  assert.equal(result.editable, true); assert.equal(result.sourceRun, null); assert.equal(result.previewCurrent, false)
})

test('incomplete task references are refused rather than silently dropping their provenance', () => {
  for (const agentRun of [{ runId: RUN_A }, { runId: RUN_A, revision: 0 }, { runId: '../../other', revision: 1 }, { revision: 2 }]) {
    const input = context(); input.model.agentRun = agentRun
    assert.equal(describeModelEditing(input).code, 'invalid_source')
  }
})

test('mesh previews, old recipes and empty files cannot manufacture editable feature history', () => {
  for (const model of [null, {}, { kind: 'mesh', geometry: { boundingBox: [40, 20, 10] } }, { kind: 'shaft', recipeId: 'shaft_v1', outerDiameter: 30, length: 100 }, { kind: 'bracket', recipeId: 'bracket_support_v1' }]) {
    const result = describeModelEditing({ ...context(), model })
    assert.equal(result.editable, false); assert.equal(result.initialDraft, null)
    assert.match(result.reason, /没有 CAD 特征计划/)
  }
})

test('malformed, unsupported or lossy plans are blocked without mutating source data', () => {
  for (const change of [ p => { p.features[0].op = 'mesh_import' }, p => { p.features[0].size = [40, 20] },
    p => { p.features[1].radius = {} }, p => { p.features[0].size[0] = NaN }, p => { p.parameters.width.value = Infinity },
    p => { delete p.features[0].size }, p => { p.features[2].inputs = ['missing', 'body'] }, p => { p.features = [] },
    p => { p.units = 'inch' }, p => { p.features[0].code = 'do-not-execute' }, p => { p.features[0].label = undefined },
    p => { p.features[0].label = {} }, p => { p.parameters.width.label = {} }, p => { p.notes = {} },
    p => { p.notes = [() => 'not-json'] }, p => { p.self = p } ]) {
    const input = context(); change(input.model.cadPlan)
    assert.equal(describeModelEditing(input).editable, false)
  }
  assert.equal(describeModelEditing({ ...context(), accountKey: null }).code, 'account_required')
  assert.equal(describeModelEditing({ ...context(), fileId: '' }).code, 'file_required')
})

test('editing entry works with no Web Crypto and uses no browser storage', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  try {
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: undefined })
    assert.equal(describeModelEditing(context()).editable, true)
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor)
    else delete globalThis.crypto
  }
})

const topology = JSON.parse(readFileSync(new URL('./fixtures/cadViewportBoxBore.json', import.meta.url), 'utf8'))
function directEditPlan() {
  const cadPlan = clone(topology.provenance.plan), face = clone(topology.result.faces[2])
  cadPlan.parameters = { extraDepth: { value: 5 }, mirroredAt: { value: null, expression: 'extraDepth * 6' } }
  cadPlan.features.push({ id: 'raised', op: 'profile_extrude', plane: 'custom', frame: { origin: face.origin, normal: face.normal, xDir: face.xAxis }, planeSource: face.selector,
    start: [0, 0], segments: [{ type: 'line', to: [4, 0] }, { type: 'line', to: [4, 3] }, { type: 'line', to: [0, 3] }, { type: 'line', to: [0, 0] }], distance: 'extraDepth',
    sketchConstraints: [{ id: 'fixed-start', type: 'fixed', points: ['start'], position: [0, 0] }, { id: 'horizontal-0', type: 'horizontal', edge: 0 }, { id: 'vertical-1', type: 'vertical', edge: 1 }, { id: 'length-0', type: 'length', edge: 0, value: 'extraDepth - 1' }, { id: 'closed', type: 'coincident', points: ['start', '3:to'] }] })
  cadPlan.features.push({ id: 'rounded', op: 'fillet', input: 'block', radius: 0.4, edges: [clone(topology.input.edges[0].selector)] })
  cadPlan.features.push({ id: 'chamfered', op: 'chamfer', input: 'drilled', length: 0.3, length2: 0.5, edges: [clone(topology.result.edges[1].selector)] })
  cadPlan.features.push({ id: 'shelled', op: 'shell', input: 'drilled', thickness: -1, faces: [face.selector] })
  cadPlan.features.push({ id: 'mirrored', op: 'mirror', input: 'raised', plane: 'custom', frame: { origin: ['mirroredAt', 0, 0], xDir: [0, 1, 0], normal: [1, 0, 0] }, keepOriginal: true })
  cadPlan.result = 'mirrored'
  return cadPlan
}

test('reopening a newly saved direct-edit plan preserves its custom plane, topology references, sketch constraints and mirror expressions', () => {
  const input = context(); input.model.cadPlan = directEditPlan()
  const entry = describeModelEditing(input)
  assert.equal(entry.editable, true, entry.reason); assert.equal(entry.previewCurrent, false)
  const saved = { id: 'feature_manual', fileId: input.fileId, revision: 4, status: 'built', ...clone(entry.initialDraft) }
  const reopened = JSON.parse(JSON.stringify(saved))
  const savedDraft = draftFromFeatureRecord(reopened)
  const descriptor = describeModelEditing({ ...input, model: { ...input.model, cadPlan: reopened.plan } })
  assert.equal(descriptor.editable, true, descriptor.reason)
  assert.equal(descriptor.initialPlanKey, entry.initialPlanKey)
  assert.deepEqual(descriptor.initialDraft.plan, saved.plan)
  assert.deepEqual(descriptor.initialDraft.sourceRun, { runId: RUN_A, revision: 2 }, 'The manual revision must not replace the originating AI revision')
  const payload = workspacePayload(savedDraft, reopened, input.fileId)
  assert.equal(payload.expectedRevision, 4); assert.equal(payload.fileId, input.fileId)
  assert.deepEqual(payload.plan.features[3].planeSource, topology.result.faces[2].selector)
  assert.equal(payload.plan.features[3].sketchConstraints.length, 5)
  assert.equal(payload.plan.features.at(-1).frame.origin[0], 'mirroredAt')
  assert.equal(payload.plan.parameters.mirroredAt.expression, 'extraDepth * 6')
  const cacheKey = featureWorkspaceKey(null, descriptor.initialPlanKey, input.accountKey)
  cacheFeatureWorkspace(cacheKey, { draft: savedDraft, record: reopened, dirty: false, history: [], future: [] })
  assert.equal(cachedFeatureWorkspace(cacheKey).record.id, 'feature_manual')
  assert.equal(cachedFeatureWorkspace(cacheKey).record.revision, 4)
})

test('custom revolve planes and legacy mirror planes remain editable without inventing origins or replacing expressions', () => {
  const input = context(); input.model.cadPlan = directEditPlan()
  const feature = input.model.cadPlan.features[3]
  feature.op = 'profile_revolve'; delete feature.distance
  feature.axisStart = [0, 0]; feature.axisEnd = [0, 1]; feature.angle = 270
  assert.equal(describeModelEditing(input).editable, true)
  for (const plane of ['XY', 'XZ', 'YZ']) {
    const mirror = input.model.cadPlan.features.at(-1)
    delete mirror.frame; mirror.plane = plane; mirror.origin = ['mirroredAt', 0, 0]
    const result = describeModelEditing(input)
    assert.equal(result.editable, true, result.reason)
    assert.deepEqual(result.initialDraft.plan.features.at(-1), mirror)
  }
})

test('incomplete custom frames, mismatched topology references and invalid sketch references are rejected before reopening', () => {
  for (const mutate of [
    p => { delete p.features[3].frame }, p => { p.features[3].origin = [0, 0, 0] }, p => { p.features[3].frame.xDir = [1, 0] }, p => { p.features[3].frame.extra = 1 },
    p => { p.features[3].plane = 'XY' }, p => { delete p.features[3].planeSource.signature }, p => { p.features[3].planeSource.kind = 'edge' }, p => { p.features[3].planeSource.sourceFeatureId = 'mirrored' },
    p => { p.features[4].edges[0].sourceFeatureId = 'drilled' }, p => { p.features[4].edges[0].index = 0.5 }, p => { p.features[4].edges[0].geometryVersion = 'old' },
    p => { p.features[4].edges.push(p.features[4].edges[0]) }, p => { p.features[5].edges.push('all') }, p => { p.features[6].faces[0].kind = 'edge' },
    p => { p.features[3].sketchConstraints[0].points = ['99:to'] }, p => { p.features[3].sketchConstraints[0].position = [0] }, p => { p.features[3].sketchConstraints[1].edge = 99 },
    p => { p.features[3].sketchConstraints[2].id = 'fixed-start' }, p => { p.features[3].sketchConstraints[3].value = {} }, p => { p.features[3].sketchConstraints[4].points = ['start', 'start'] },
    p => { p.features[3].sketchConstraints.push({ id: 'again', type: 'horizontal', edge: 0 }) }, p => { p.features.at(-1).keepOriginal = 'yes' },
  ]) {
    const input = context(); input.model.cadPlan = directEditPlan(); mutate(input.model.cadPlan)
    const result = describeModelEditing(input)
    assert.equal(result.editable, false); assert.equal(result.code, 'unsupported_plan')
  }
})

function persistentlyBoundPlan() {
  const plan=directEditPlan(),profile=plan.features[3]
  for(const feature of plan.features)for(const selector of [...(Array.isArray(feature.edges)?feature.edges:[]),...(Array.isArray(feature.faces)?feature.faces:[]),...(feature.planeSource?[feature.planeSource]:[])])selector.binding={version:1,key:'a'.repeat(64)}
  profile.planeAttachment={version:1,origin:[2,3,0],xDir:[1,0,0],normal:[0,0,1]}
  return plan
}

test('server-bound face and edge references and local plane attachment survive saving and reopening verbatim',()=>{
  const input=context();input.model.cadPlan=persistentlyBoundPlan()
  const first=describeModelEditing(input);assert.equal(first.editable,true,first.reason)
  const saved={id:'manual_bound',fileId:input.fileId,revision:7,...clone(first.initialDraft)}
  const restored=workspacePayload(draftFromFeatureRecord(JSON.parse(JSON.stringify(saved))),saved,input.fileId)
  const reopened=describeModelEditing({...input,model:{...input.model,cadPlan:restored.plan}})
  assert.equal(reopened.editable,true,reopened.reason);assert.deepEqual(reopened.initialDraft.plan,JSON.parse(JSON.stringify(input.model.cadPlan)))
  assert.deepEqual(reopened.initialDraft.plan.features[3].planeAttachment,{version:1,origin:[2,3,0],xDir:[1,0,0],normal:[0,0,1]})
  assert.equal(reopened.initialPlanKey,first.initialPlanKey)
})

test('persistent reference metadata rejects unsupported versions, malformed hashes, expression attachments and extra fields',()=>{
  for(const mutate of [
    p=>{p.features[3].planeSource.binding.version='1'},p=>{p.features[3].planeSource.binding.version=2},
    p=>{p.features[3].planeSource.binding.key='A'.repeat(64)},p=>{p.features[3].planeSource.binding.key='a'.repeat(63)},
    p=>{p.features[4].edges[0].binding.owner='another'},p=>{p.features[3].planeSource.extra='invalid'},
    p=>{p.features[3].planeAttachment.version=2},p=>{p.features[3].planeAttachment.origin[0]='extraDepth'},
    p=>{p.features[3].planeAttachment.origin[0]=1000001},p=>{p.features[3].planeAttachment.normal=[0,1]},
    p=>{p.features[3].planeAttachment.extra=1},p=>{delete p.features[3].planeSource.binding},
    p=>{p.features[3].plane='XY';delete p.features[3].frame;delete p.features[3].planeSource},
  ]){
    const input=context();input.model.cadPlan=persistentlyBoundPlan();mutate(input.model.cadPlan)
    assert.equal(describeModelEditing(input).editable,false)
  }
})

test('verified STEP asset features reopen without requiring a primitive default or losing the owner-bound asset hash',async()=>{
  const {insertImportedBody}=await import('./cadImportBodyModel.js')
  const asset={id:`asset_${'a'.repeat(32)}`,sha256:'b'.repeat(64),name:'自有阶梯轴',inspection:{valid:true,solidCount:1}}
  const input=context(),prepared=insertImportedBody(input.model.cadPlan,asset,{position:[10,0,20],instanceId:'review'}).plan
  input.model.cadPlan=prepared;input.model.agentRun.revision=4
  const result=describeModelEditing(input)
  assert.equal(Object.hasOwn(featureDefaults,'import_step'),false,'asset-backed ops must not get invented default geometry')
  assert.equal(result.editable,true,result.reason);assert.deepEqual(result.initialDraft.plan,prepared);assert.equal(result.sourceRun.revision,4)
  const imported=result.initialDraft.plan.features.find(f=>f.op==='import_step');assert.equal(imported.assetId,asset.id);assert.equal(imported.sha256,asset.sha256)
  for(const patch of [{assetId:'../../other'},{sha256:''},{assetId:`asset_${'A'.repeat(32)}`},{privatePath:'/data/other.step'}]){
    const bad=clone(input);Object.assign(bad.model.cadPlan.features.find(f=>f.op==='import_step'),patch);assert.equal(describeModelEditing(bad).editable,false)
  }
})

test('standard-part insertion and independent sketch sweep metadata reopen as their exact persisted plan',async()=>{
  const {insertCadStandardPart,cadStandardParts}=await import('./cadStandardPartModel.js')
  const input=context(),inserted=insertCadStandardPart(input.model.cadPlan,cadStandardParts[0].catalogId,{position:[10,20,30],axis:'X',instanceId:'restore'})
  input.model.cadPlan=inserted.plan
  const entry=describeModelEditing(input);assert.equal(entry.editable,true,entry.reason);assert.deepEqual(entry.initialPlan,inserted.plan)
  const sketch={id:'sectionSketch',label:'椭圆截面',plane:'XY',start:[2,0],segments:[{type:'ellipse',center:[0,0],radii:[2,1],rotation:0,startAngle:0,endAngle:360,to:[2,0]}]}
  const sweep={id:'swept',op:'profile_sweep',sketchId:sketch.id,plane:sketch.plane,start:sketch.start,segments:sketch.segments,path:{start:[0,0,0],segments:[{type:'spline',through:[[0,0,10]],to:[10,0,20]}]},transition:'transformed',isFrenet:false}
  input.model.cadPlan={version:'cad-plan-v1',units:'mm',parameters:{},sketches:[sketch],references:[],annotations:[{id:'note',kind:'note',text:'人工待验收',position:[2,0,0],points:[]}],features:[sweep],result:sweep.id}
  const restored=describeModelEditing(input);assert.equal(restored.editable,true,restored.reason);assert.deepEqual(restored.initialPlan,input.model.cadPlan)
})
