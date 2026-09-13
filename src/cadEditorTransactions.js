import { featureFromIndependentSketch, storeIndependentSketch, sketchEntityFields, referenceFrames } from './cadEditorEntities.js'
import { evaluateSketchExpression } from './featureSketchExpressions.js'
import { cloneFeatureValue, featureDefaults, featureNames, newFeature } from './directFeatureModel.js'
import { rectangleSketchConstraints, validateSketchContours } from './featureSketchModel.js'

export function emptyCadDraft(name = '新零件') {
  return {name,plan:{version:'cad-plan-v1',units:'mm',name,parameters:{},features:[],result:''},suppressed:[],changeNote:'新建零件',sourceRun:null}
}

export function editableProfile(feature, parameters = {}) {
  if (['profile_extrude', 'profile_revolve','profile_sweep'].includes(feature.op)) return cloneFeatureValue(feature)
  const origin = cloneFeatureValue(feature.origin || [0, 0, 0])
  if (feature.op === 'box') {
    const [width, height, depth] = feature.size
    return { id: feature.id, label: feature.label || '拉伸', op: 'profile_extrude', plane: 'XY', origin,
      start: [0, 0], segments: [{ type:'line', to:[width,0] },{ type:'line', to:[width,height] },{ type:'line', to:[0,height] },{ type:'line', to:[0,0] }], distance:depth, sketchConstraints:rectangleSketchConstraints([0,0]) }
  }
  if (feature.op === 'cylinder') {
    const direction = feature.direction || [0,0,1]
    const resolved = direction.map(value => evaluateSketchExpression(value, parameters)), magnitude = Math.hypot(...resolved)
    if (!resolved.every(Number.isFinite) || magnitude < 1e-9) throw new Error('圆柱轴线方向必须为非零有限向量。')
    const numeric = direction.every(value => typeof value === 'number')
    const unit = resolved.map(value=>value/magnitude)
    const normal = numeric ? unit : direction.map(value=>typeof value==='number'?value/magnitude:`(${value}) / ${magnitude}`)
    // Preserve formula-driven direction. Scaling by this fixed nonzero factor
    // does not freeze its direction; the kernel normalizes the frame each build.
    const negativeValue = value => typeof value==='number' ? -value : `-(${value})`
    const xDir = Math.abs(unit[0]) < .9 ? [0,normal[2],negativeValue(normal[1])] : [negativeValue(normal[2]),0,normal[0]]
    if([...normal,...xDir].some(value=>typeof value==='string'&&value.length>256))throw new Error('轴线公式过长，请先定义命名参数再编辑圆柱草图。')
    const negative = typeof feature.radius === 'number' ? -feature.radius : `-(${feature.radius})`
    return {id:feature.id,label:feature.label || '圆柱拉伸',op:'profile_extrude',plane:'custom',frame:{origin,xDir,normal},start:[feature.radius,0],segments:[{type:'arc',through:[0,feature.radius],to:[negative,0]},{type:'arc',through:[0,negative],to:[feature.radius,0]}],distance:feature.height}
  }
  throw new Error('这个特征没有可编辑的轮廓草图。')
}
export function beginCadEdit(draft, id, stage = 'feature') {
  const next = cloneFeatureValue(draft), index=next.plan.features.findIndex(item=>item.id===id)
  if(index<0)throw new Error('特征已不存在。')
  if(stage==='sketch')next.plan.features[index]=editableProfile(next.plan.features[index], next.plan.parameters)
  return {id,stage,draft:next,base:cloneFeatureValue(draft),isNew:false,combine:'new',selection:[]}
}
export function beginCadTool(draft, op, {plane='XY',frame,planeSource,stage}={}) {
  if(!featureDefaults[op])throw new Error('请选择建模工具。')
  if(('input' in featureDefaults[op] || 'inputs' in featureDefaults[op]) && !draft.plan.result)throw new Error('请先创建一个实体，再使用此工具。')
  const next=cloneFeatureValue(draft), feature=newFeature(op,next.plan.features), baseId=next.plan.result
  if('input' in feature)feature.input=baseId
  if(['fillet','chamfer'].includes(op))feature.edges=[]
  if(op==='shell')feature.faces=[]
  if(op==='sweep'){feature.radius='';feature.path=[['','',''],['','','']]}
  if(op==='loft')feature.sections=[{origin:['','',''],radius:''},{origin:['','',''],radius:''}]
  if(['profile_extrude','profile_revolve','profile_sweep'].includes(op)) {
    feature.plane=plane
    feature.start=[0,0];feature.segments=[]
    if(frame) {feature.frame=cloneFeatureValue(frame);delete feature.origin}
    if(planeSource) feature.planeSource=cloneFeatureValue(planeSource)
  }
  next.plan.features.push(feature);next.plan.result=feature.id
  return {id:feature.id,stage:stage || (['profile_extrude','profile_revolve','profile_sweep'].includes(op)?'sketch':'feature'),draft:next,base:cloneFeatureValue(draft),baseId,isNew:true,combine:'new',selection:[]}
}

export function beginCadProfileTool(draft, op, {sketchId,plane='XY',frame,planeSource}={}) {
  if(!['profile_extrude','profile_revolve','profile_sweep'].includes(op))throw new Error('请选择拉伸或旋转。')
  if(sketchId && draft.plan.sketches?.some(s=>s.id===sketchId)){
    const next=beginCadTool(draft,op,{plane,stage:'feature'}),feature=featureFromIndependentSketch(draft.plan,sketchId,op);next.id=feature.id;next.draft.plan.features[next.draft.plan.features.length-1]=feature;next.draft.plan.result=feature.id;return next
  }
  if(sketchId&&!frame){
    if(draft.suppressed?.includes(sketchId))throw new Error('所选草图已抑制，请先恢复该特征。')
    const edit=changeCadProfileOperation(beginCadEdit(draft,sketchId,'sketch'),op)
    const feature=edit.draft.plan.features.find(item=>item.id===edit.id)
    return {...edit,stage:feature.segments?.length>=2?'feature':'sketch'}
  }
  return beginCadTool(draft,op,{plane,frame,planeSource,stage:'sketch'})
}

export function cadSketchFeature(transaction) {
  const feature=transaction?.draft.plan.features.find(f=>f.id===transaction.id)
  if(feature?.op!=='profile_loft')return feature
  const section=feature.sections?.[transaction.sectionIndex]
  if(!section)return null
  return {id:feature.id,label:`${feature.label||'放样'} · 截面 ${transaction.sectionIndex+1}`,op:'profile_extrude',distance:1,plane:'custom',...cloneFeatureValue(section)}
}
export function patchCadSketch(transaction, feature, parameters) {
  const owner=transaction?.draft.plan.features.find(f=>f.id===transaction.id)
  if(owner?.op!=='profile_loft')return patchCadTransaction(transaction,feature,parameters)
  const next=cloneFeatureValue(transaction),section=next.draft.plan.features.find(f=>f.id===next.id).sections[next.sectionIndex]
  for(const key of ['start','segments','contours','sketchConstraints','frame']) {
    delete section[key]
    if(Object.hasOwn(feature,key))section[key]=cloneFeatureValue(feature[key])
  }
  if(section.sketchId) {
    const source=next.draft.plan.sketches.find(s=>s.id===section.sketchId)
    const changed={...source,...Object.fromEntries(['start','segments','contours','sketchConstraints'].filter(k=>Object.hasOwn(section,k)).map(k=>[k,section[k]]))}
    for(const key of ['contours','sketchConstraints'])if(!Object.hasOwn(section,key))delete changed[key]
    next.draft.plan=storeIndependentSketch(next.draft.plan,changed,section.sketchId).plan
  }
  if(parameters)next.draft.plan.parameters=cloneFeatureValue(parameters)
  return next
}

export function requireCadFeatureInputs(feature,parameters={}) {
  if(['profile_extrude','profile_revolve','profile_sweep'].includes(feature?.op))validateSketchContours(feature,parameters)
  const scalar=value=>typeof value==='number'?Number.isFinite(value):typeof value==='string'&&Boolean(value.trim())
  const vector=value=>Array.isArray(value)&&value.length===3&&value.every(scalar)
  if(feature?.op==='sweep'&&(!scalar(feature.radius)||!Array.isArray(feature.path)||feature.path.length<2||!feature.path.every(vector)))throw new Error('请填写圆截面半径及至少两个完整的路径点坐标。')
  if(feature?.op==='loft'&&(!Array.isArray(feature.sections)||feature.sections.length<2||!feature.sections.every(section=>vector(section.origin)&&(Object.hasOwn(section,'radius')?scalar(section.radius):scalar(section.width)&&scalar(section.height)))))throw new Error('请填写至少两个圆形或矩形截面的位置和尺寸；截面平行于 XY，Z 高度须递增。')
  if(feature?.op==='profile_sweep') {
    const path=feature.path
    if(path?.curveReference)return
    if(!path||!vector(path.start)||!Array.isArray(path.segments)||!path.segments.length)throw new Error('请绘制扫掠路径，或选择独立路径草图。')
    for(const segment of path.segments){if(!vector(segment.to))throw new Error('路径终点坐标不完整。');if(segment.type==='arc'&&!vector(segment.through))throw new Error('请补全圆弧路径的经过点。');if(segment.type==='spline'&&(!Array.isArray(segment.through)||!segment.through.every(vector)))throw new Error('请补全样条路径插值点。')}
  }
  if(feature?.op==='profile_loft') {
    if(!Array.isArray(feature.sections)||feature.sections.length<2)throw new Error('请选择或绘制至少两个放样截面。')
    feature.sections.forEach((section,index)=>{try{validateSketchContours({...section,op:'profile_extrude'},parameters)}catch(reason){throw new Error(`截面 ${index+1}：${reason.message}`)};if(!section.frame||!['origin','xDir','normal'].every(k=>vector(section.frame[k])))throw new Error(`截面 ${index+1} 的位置或方向不完整。`)})
  }
  if(feature?.op==='surface_style'&&(!Array.isArray(feature.points)||feature.points.length<2||!feature.points.every(row=>Array.isArray(row)&&row.length>=2&&row.every(vector))))throw new Error('请创建曲面控制点网格，再调整控制点。')
  if(feature?.op==='surface_join'&&(!Array.isArray(feature.inputs)||!feature.inputs.length||feature.inputs.some(id=>!id)))throw new Error('请选择要连接的曲面。')
  if(feature?.op==='body_edit'&&!feature.bodies?.length)throw new Error('请先选择要操作的实体。')
  if(feature?.op==='compound' &&(!Array.isArray(feature.inputs)||feature.inputs.length<2||feature.inputs.length>32||feature.inputs.some(id=>typeof id!=='string'||!id.trim())||new Set(feature.inputs).size!==feature.inputs.length))throw new Error('多实体组合须保留至少两个不同的前置实体。')
}
export function patchCadTransaction(transaction, feature, parameters) {
  const next=cloneFeatureValue(transaction)
  next.draft.plan.features=next.draft.plan.features.map(item=>item.id===next.id?cloneFeatureValue(feature):item)
  if(feature.sketchId)next.draft.plan=storeIndependentSketch(next.draft.plan,feature,feature.sketchId).plan
  if(parameters)next.draft.plan.parameters=cloneFeatureValue(parameters)
  return next
}

// Change the operation around the same profile; never seed a second example
// contour or replace its source face, parameter expressions or constraints.
export function changeCadProfileOperation(transaction, op) {
  if(op==='sweep')op='profile_sweep'
  if(op==='loft')op='profile_loft'
  if(!['profile_extrude','profile_revolve','profile_sweep','profile_loft'].includes(op))throw new Error('请选择草图建模工具。')
  const feature=transaction?.draft?.plan?.features.find(item=>item.id===transaction.id)
  if(!feature||!['profile_extrude','profile_revolve','profile_sweep'].includes(feature.op))throw new Error('当前特征没有可转换的轮廓草图。')
  if(feature.op===op)return transaction
  const keys={profile_extrude:['distance'],profile_revolve:['angle','axisStart','axisEnd'],profile_sweep:['path','transition','isFrenet']}
  const settings=cloneFeatureValue(transaction.profileOperationSettings||{})
  settings[feature.op]=Object.fromEntries(keys[feature.op].filter(k=>Object.hasOwn(feature,k)).map(k=>[k,cloneFeatureValue(feature[k])]))
  const next={...cloneFeatureValue(feature),op}
  for(const key of Object.values(keys).flat())delete next[key]
  if(op==='profile_loft') {
    const frame=feature.planeReference?referenceFrames(transaction.draft.plan)[feature.planeReference]:feature.frame||{...referenceFrames(transaction.draft.plan)[feature.plane||'XY'],origin:feature.origin||[0,0,0]}
    const section={id:'section1',frame:cloneFeatureValue(frame),...Object.fromEntries(['start','segments','contours','sketchConstraints','sketchId'].filter(k=>Object.hasOwn(feature,k)).map(k=>[k,cloneFeatureValue(feature[k])]))}
    for(const key of [...sketchEntityFields,'sketchId'])delete next[key]
    next.sections=[section];next.ruled=false
  } else Object.assign(next,cloneFeatureValue(Object.fromEntries(keys[op].filter(k=>Object.hasOwn(featureDefaults[op],k)).map(k=>[k,featureDefaults[op][k]]))),settings[op])
  if(feature.label===featureNames[feature.op]||new RegExp(`^${featureNames[feature.op]}\\s*\\d+$`).test(feature.label||''))next.label=feature.label.replace(featureNames[feature.op],featureNames[op])
  return {...patchCadTransaction(transaction,next),stage:'feature',profileOperationSettings:settings}
}
export function cadTransactionDraft(transaction) {
  if(transaction.kind==='independentSketch'){const base=cloneFeatureValue(transaction.base),feature=transaction.draft.plan.features.find(f=>f.id===transaction.id);base.plan=storeIndependentSketch({...base.plan,parameters:transaction.draft.plan.parameters},feature,transaction.sketchId).plan;base.changeNote='编辑独立草图';return base}
  if(transaction.customDraft)return cloneFeatureValue(transaction.customDraft)
  const next=cloneFeatureValue(transaction.draft), base=transaction.baseId
  const createsBody=['profile_extrude','profile_revolve','box','cylinder','sweep','loft','gear','profile_sweep','profile_loft','surface_style','standard_part','mesh_body'].includes(next.plan.features.find(item=>item.id===transaction.id)?.op)
  if(transaction.isNew && createsBody && ['union','cut','intersect','new'].includes(transaction.combine) && base) {
    const feature=newFeature(transaction.combine==='new'?'compound':transaction.combine,next.plan.features);feature.inputs=[base,transaction.id]
    next.plan.features.push(feature);next.plan.result=feature.id
  }
  next.changeNote=`${transaction.isNew?'新建':'编辑'}${next.plan.features.find(item=>item.id===transaction.id)?.label||featureNames[next.plan.features.find(item=>item.id===transaction.id)?.op]||'特征'}`
  if(next.plan.features.length>128)throw new Error('特征数量超过128项，请简化当前设计。')
  return next
}
export function cadFeatureTree(draft) {
  return draft.plan.features.map(feature=>({id:feature.id,label:feature.label||`${featureNames[feature.op]||feature.op} ${draft.plan.features.indexOf(feature)+1}`,op:feature.op,hasSketch:['profile_extrude','profile_revolve','profile_sweep','box','cylinder'].includes(feature.op),suppressed:draft.suppressed.includes(feature.id)}))
}
