import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { beginCadEdit, beginCadProfileTool, beginCadTool, cadTransactionDraft, changeCadProfileOperation, editableProfile, emptyCadDraft, patchCadTransaction, requireCadFeatureInputs } from './cadEditorTransactions.js'
import { featureDefaults, initialFeatureDraft } from './directFeatureModel.js'
import { evaluateSketchExpression } from './featureSketchExpressions.js'

test('profile tools start with a real empty sketch and reuse a contour only when its identity was explicitly selected',()=>{
  for(const source of [emptyCadDraft(),initialFeatureDraft()])for(const op of ['profile_extrude','profile_revolve']){
    const before=structuredClone(source),entry=beginCadProfileTool(source,op),feature=entry.draft.plan.features.at(-1)
    assert.equal(entry.isNew,true);assert.equal(entry.stage,'sketch');assert.deepEqual(feature.start,[0,0]);assert.deepEqual(feature.segments,[])
    assert.throws(()=>requireCadFeatureInputs(feature),/为空.*绘制闭合轮廓/);assert.deepEqual(source,before)
  }
  const source=initialFeatureDraft(),profile=editableProfile({...source.plan.features[0],size:['W','H','D']})
  source.plan.features[0]=profile;source.plan.parameters={W:{value:80},H:{value:50},D:{value:12}}
  const selected=beginCadProfileTool(source,'profile_extrude',{sketchId:profile.id})
  assert.equal(selected.isNew,false);assert.equal(selected.stage,'feature');assert.deepEqual(selected.draft,source)
  const revolved=beginCadProfileTool(source,'profile_revolve',{sketchId:profile.id})
  assert.equal(revolved.id,profile.id);assert.deepEqual(revolved.draft.plan.features[0].segments,profile.segments)
  assert.deepEqual(revolved.draft.plan.parameters,source.plan.parameters);assert.equal(revolved.draft.plan.features.length,1)
  assert.throws(()=>beginCadProfileTool({...source,suppressed:[profile.id]},'profile_extrude',{sketchId:profile.id}),/已抑制/)
  const frame={origin:[8,9,12],xDir:[1,0,0],normal:[0,0,1]},planeSource={kind:'face',sourceFeatureId:'box1',index:5,geometryVersion:'a'.repeat(64),signature:'b'.repeat(64)}
  const faceEntry=beginCadProfileTool(source,'profile_extrude',{sketchId:profile.id,plane:'custom',frame,planeSource}).draft.plan.features.at(-1)
  assert.deepEqual(faceEntry.frame,frame);assert.deepEqual(faceEntry.planeSource,planeSource);assert.deepEqual(faceEntry.segments,[])
  assert.equal(Object.hasOwn(faceEntry,'origin'),false)
})

test('sweep and loft creation contain no example geometry; incomplete inputs cannot pass the completion guard',()=>{
  const source=emptyCadDraft(),sweep=beginCadTool(source,'sweep').draft.plan.features[0],loft=beginCadTool(source,'loft').draft.plan.features[0]
  assert.equal(sweep.radius,'');assert.deepEqual(sweep.path,[['','',''],['','','']])
  assert.deepEqual(loft.sections,[{origin:['','',''],radius:''},{origin:['','',''],radius:''}])
  assert.throws(()=>requireCadFeatureInputs(sweep),/圆截面半径/);assert.throws(()=>requireCadFeatureInputs(loft),/至少两个圆形或矩形/)
  const savedSweep={...sweep,radius:'R',path:[[0,0,0],[0,0,20]]},savedLoft={...loft,sections:[{origin:[0,0,0],radius:4},{origin:[0,0,10],width:6,height:8}]}
  assert.doesNotThrow(()=>requireCadFeatureInputs(savedSweep));assert.doesNotThrow(()=>requireCadFeatureInputs(savedLoft))
  for(const feature of [savedSweep,savedLoft]){
    const draft={...source,plan:{...source.plan,features:[feature],result:feature.id}}
    assert.deepEqual(beginCadEdit(draft,feature.id).draft,draft)
  }
})

test('new-body transactions preserve the previous result as a compound while additions stay fused and later edits keep the result',()=>{
  const source=initialFeatureDraft(),entry=beginCadTool(source,'box')
  const saved=cadTransactionDraft(entry),compound=saved.plan.features.at(-1)
  assert.equal(compound.op,'compound');assert.deepEqual(compound.inputs,['box1',entry.id]);assert.equal(saved.plan.result,compound.id)
  assert.equal(saved.plan.features.filter(feature=>feature.op==='box').length,2)
  const edited=beginCadEdit(saved,entry.id),body=edited.draft.plan.features.find(feature=>feature.id===entry.id)
  const modified=cadTransactionDraft(patchCadTransaction(edited,{...body,size:[30,20,10]}))
  assert.equal(modified.plan.result,compound.id);assert.deepEqual(modified.plan.features.at(-1),compound)
  assert.equal(modified.plan.features.filter(feature=>feature.op==='compound').length,1)
  const added=cadTransactionDraft({...entry,combine:'union'})
  assert.equal(added.plan.features.at(-1).op,'union')
  const first=cadTransactionDraft(beginCadTool(emptyCadDraft(),'box'))
  assert.equal(first.plan.features.length,1);assert.equal(first.plan.result,first.plan.features[0].id)
  assert.throws(()=>requireCadFeatureInputs({...compound,inputs:['box1']}),/至少两个/)
  assert.throws(()=>requireCadFeatureInputs({...compound,inputs:['box1','box1']}),/不同/)
})

test('every default operation wraps only an explicitly creatable new body, never a modified result',()=>{
  const source=initialFeatureDraft(),before=structuredClone(source),creators=new Set(['profile_extrude','profile_revolve','box','cylinder','sweep','loft','gear','profile_sweep','profile_loft','surface_style','standard_part','mesh_body'])
  for(const op of Object.keys(featureDefaults)){
    const transaction=beginCadTool(source,op),result=cadTransactionDraft(transaction)
    if(creators.has(op)){
      assert.equal(result.plan.features.length,3,op)
      assert.equal(result.plan.features.at(-1).op,'compound',op)
      assert.deepEqual(result.plan.features.at(-1).inputs,['box1',transaction.id],op)
    }else{
      assert.equal(result.plan.features.length,2,op)
      assert.equal(result.plan.result,transaction.id,op)
      assert.equal(result.plan.features.at(-1).op,op)
      assert.deepEqual(result.plan,transaction.draft.plan,op)
    }
    assert.deepEqual(source,before,op)
  }
})

test('frontend new-body, fused-body, shell, mirror and rounding transactions produce the intended real STEP solids',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  const source=initialFeatureDraft(),cases=[]
  for(const offset of [0,20,30]){
    const entry=beginCadTool(source,'box'),feature=entry.draft.plan.features.at(-1)
    const result=cadTransactionDraft(patchCadTransaction(entry,{...feature,origin:[offset,0,0]}))
    cases.push({name:`independent_${offset}`,plan:result.plan,solids:2,volume:8000})
    if(offset===20)cases.push({name:'fused',plan:cadTransactionDraft({...patchCadTransaction(entry,{...feature,origin:[offset,0,0]}),combine:'union'}).plan,solids:1,volume:8000})
  }
  const shell=beginCadTool(source,'shell'),shelled=cadTransactionDraft(patchCadTransaction(shell,{...shell.draft.plan.features.at(-1),thickness:-1,faces:['maxZ']}))
  cases.push({name:'shell',plan:shelled.plan,solids:1,volume:1084})
  cases.push({name:'shell_mirror',plan:cadTransactionDraft(beginCadTool(shelled,'mirror')).plan,solids:1,volume:2168})
  cases.push({name:'linear_pattern',plan:cadTransactionDraft(beginCadTool(source,'linear_pattern')).plan,solids:3,volume:12000})
  for(const op of ['fillet','chamfer']){
    const edit=beginCadTool(source,op)
    cases.push({name:op,plan:cadTransactionDraft(patchCadTransaction(edit,{...edit.draft.plan.features.at(-1),edges:'all'})).plan,solids:1,maxVolume:4000})
  }
  const script=`import sys,json,tempfile,math
from pathlib import Path
from app.cad_plan import validate_plan
from app.cad_executor import build_plan_shape
from app.geometry import get_cadquery
cq=get_cadquery()
evidence=[]
with tempfile.TemporaryDirectory(prefix='joyniu-body-transactions-') as folder:
 for case in json.load(sys.stdin):
  shape,_,_=build_plan_shape(validate_plan(case['plan']))
  path=Path(folder)/(case['name']+'.step')
  cq.exporters.export(shape.val(),str(path),exportType='STEP')
  restored=cq.importers.importStep(str(path)).val()
  assert restored.isValid(),case['name']
  count=len(restored.Solids());volume=restored.Volume()
  assert count==case['solids'],(case['name'],count,case['solids'])
  if 'volume' in case: assert math.isclose(volume,case['volume'],abs_tol=1e-5),(case['name'],volume,case['volume'])
  else: assert 0<volume<case['maxVolume'],(case['name'],volume)
  evidence.append({'name':case['name'],'solidCount':count,'volumeMm3':volume})
print(json.dumps(evidence))`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script],{cwd:resolve('apps/api'),input:JSON.stringify(cases),encoding:'utf8',timeout:30000})
  assert.equal(result.status,0,result.stderr);assert.equal(JSON.parse(result.stdout).length,cases.length)
})

test('feature and sketch transactions keep the original document intact until completion',()=>{
  const draft=initialFeatureDraft(), original=structuredClone(draft)
  const edit=beginCadEdit(draft,'box1','sketch')
  const feature=edit.draft.plan.features[0]
  assert.equal(feature.op,'profile_extrude')
  const changed=patchCadTransaction(edit,{...feature,distance:30},{length:{value:40}})
  assert.equal(cadTransactionDraft(changed).plan.features[0].distance,30)
  assert.deepEqual(draft,original)
  assert.equal(edit.draft.plan.features[0].distance,10)
})
test('a face sketch retains only the selected frame origin and uses the current result for its boolean',()=>{
  const draft=initialFeatureDraft(),frame={origin:[10,10,10],xDir:[1,0,0],normal:[0,0,1]},planeSource={kind:'face',sourceFeatureId:'box1',index:5,geometryVersion:'a'.repeat(64),signature:'b'.repeat(64)}
  const edit=beginCadTool(draft,'profile_extrude',{plane:'custom',frame,planeSource})
  edit.combine='cut'
  const next=cadTransactionDraft(edit),profile=next.plan.features[1]
  assert.deepEqual(profile.frame,frame);assert.deepEqual(profile.planeSource,planeSource)
  assert.equal(Object.hasOwn(profile,'origin'),false)
  assert.deepEqual(next.plan.features.at(-1).inputs,['box1',profile.id]);assert.equal(next.plan.result,next.plan.features.at(-1).id)
  assert.equal(edit.draft.plan.features.length,2);assert.equal(draft.plan.features.length,1)
})
test('cylinder sketch follows expression-driven axes when their parameters change',()=>{
  const feature={id:'c1',op:'cylinder',direction:['dx','dy','dz'],origin:['x',2,3],radius:'radius',height:15}
  const parameters={dx:{value:0},dy:{value:1},dz:{value:1},x:{value:5},radius:{value:4}}
  const profile=editableProfile(feature,parameters)
  assert.equal(Object.hasOwn(profile,'origin'),false)
  assert.deepEqual(profile.frame.origin,['x',2,3])
  for(const dy of [1,2]){
    const values={...parameters,dy:{value:dy}}
    const normal=profile.frame.normal.map(value=>evaluateSketchExpression(value,values)),tangent=profile.frame.xDir.map(value=>evaluateSketchExpression(value,values))
    assert.ok(Math.abs(normal.reduce((sum,value,i)=>sum+value*tangent[i],0))<1e-10)
    assert.ok(Math.abs(normal[1]/normal[2]-dy)<1e-10)
  }
  assert.equal(profile.start[0],'radius')
})
test('cylinder axes at kernel threshold normalize while undefined and zero axes fail explicitly',()=>{
  const cylinder={id:'c',op:'cylinder',radius:3,height:5,direction:[0,0,1e-9]}
  assert.deepEqual(editableProfile(cylinder).frame.normal,[0,0,1])
  assert.throws(()=>editableProfile({...cylinder,direction:[0,0,0]}),/非零/)
  assert.throws(()=>editableProfile({...cylinder,direction:['missing',0,1]}),/尚未定义/)
})

test('changing profile operation retains the exact sketch, parameter map, source bindings and operation settings',()=>{
  const draft=initialFeatureDraft(),face={kind:'face',sourceFeatureId:'box1',index:5,geometryVersion:'a'.repeat(64),signature:'b'.repeat(64),binding:{version:1,key:'c'.repeat(64)}}
  let edit=beginCadTool(draft,'profile_extrude',{plane:'custom',frame:{origin:[5,6,7],normal:[0,0,1],xDir:[1,0,0]},planeSource:face})
  const feature=edit.draft.plan.features.at(-1)
  feature.start=['R',0];feature.segments=[{type:'line',to:['R + W',0]},{type:'line',to:['R + W','H']},{type:'line',to:['R','H']}];feature.distance='depth'
  feature.sketchConstraints=[{id:'h',type:'horizontal',edge:0}];feature.planeAttachment={version:1,origin:[0,0,0],normal:[0,0,1],xDir:[1,0,0]}
  edit.draft.plan.parameters={R:{value:2},W:{value:3},H:{value:4},depth:{value:9}}
  edit.combine='cut'
  const original=structuredClone(edit),revolved=changeCadProfileOperation(edit,'profile_revolve'),turned=revolved.draft.plan.features.at(-1)
  assert.deepEqual(edit,original);assert.equal(turned.id,feature.id);assert.equal(revolved.combine,'cut');assert.equal(Object.hasOwn(turned,'distance'),false)
  for(const key of ['start','segments','frame','planeSource','planeAttachment','sketchConstraints'])assert.deepEqual(turned[key],feature[key])
  assert.deepEqual(turned.axisStart,[0,0]);assert.deepEqual(turned.axisEnd,[0,1]);assert.equal(turned.angle,360)
  assert.deepEqual(revolved.draft.plan.parameters,original.draft.plan.parameters)
  const customAngle=patchCadTransaction(revolved,{...turned,angle:'depth * 10',axisStart:[1,0],axisEnd:[1,1]})
  const extruded=changeCadProfileOperation(customAngle,'profile_extrude'),restored=extruded.draft.plan.features.at(-1)
  assert.equal(restored.distance,'depth');for(const key of ['angle','axisStart','axisEnd'])assert.equal(Object.hasOwn(restored,key),false)
  assert.deepEqual(changeCadProfileOperation(extruded,'profile_revolve').draft.plan.features.at(-1),customAngle.draft.plan.features.at(-1))
  const plan=cadTransactionDraft(extruded).plan
  assert.equal(Object.hasOwn(plan,'profileOperationSettings'),false)
  assert.equal(plan.features.filter(item=>item.id===feature.id).length,1)
  assert.deepEqual(plan.features.at(-1).inputs,['box1',feature.id])
})

test('sweep and loft conversion preserve the user section and reject completion until path or second section is supplied',()=>{
  const source=initialFeatureDraft(),transaction=beginCadEdit({...source,plan:{...source.plan,features:[editableProfile(source.plan.features[0])]}},'box1'),before=structuredClone(transaction)
  const swept=changeCadProfileOperation(transaction,'sweep'),sf=swept.draft.plan.features[0]
  assert.equal(sf.op,'profile_sweep');assert.deepEqual(sf.segments,transaction.draft.plan.features[0].segments);assert.deepEqual(sf.path,{start:[0,0,0],segments:[]})
  assert.throws(()=>requireCadFeatureInputs(sf),/扫掠路径/)
  const lofted=changeCadProfileOperation(transaction,'loft'),lf=lofted.draft.plan.features[0]
  assert.equal(lf.op,'profile_loft');assert.equal(lf.sections.length,1);assert.deepEqual(lf.sections[0].segments,transaction.draft.plan.features[0].segments)
  assert.throws(()=>requireCadFeatureInputs(lf),/至少两个/);assert.deepEqual(transaction,before)
})

test('profile conversion renames only identifiable system labels and keeps user titles unchanged',()=>{
  for(const gap of [' ', '']){
    const transaction=beginCadTool(initialFeatureDraft(),'profile_extrude')
    const feature=transaction.draft.plan.features.at(-1)
    feature.label=`轮廓拉伸${gap}1`
    const revolved=changeCadProfileOperation(transaction,'profile_revolve')
    assert.equal(revolved.draft.plan.features.at(-1).label,`轮廓旋转${gap}1`)
    assert.equal(changeCadProfileOperation(revolved,'profile_extrude').draft.plan.features.at(-1).label,feature.label)
    assert.equal(feature.label,`轮廓拉伸${gap}1`)
  }
  for(const label of ['轴套内壁','轮廓拉伸 1 加强筋','轮廓拉伸用20号钢']){
    const transaction=beginCadTool(initialFeatureDraft(),'profile_extrude')
    transaction.draft.plan.features.at(-1).label=label
    assert.equal(changeCadProfileOperation(transaction,'profile_revolve').draft.plan.features.at(-1).label,label)
  }
})

test('the same expression-driven sketch makes valid extrusion and revolution solids after STEP readback',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  const feature={id:'profile1',op:'profile_extrude',plane:'custom',frame:{origin:[7,8,9],xDir:[1,0,0],normal:[0,-1,0]},start:['R',0],segments:[{type:'line',to:['R + W',0]},{type:'line',to:['R + W','H']},{type:'line',to:['R','H']},{type:'line',to:['R',0]}],distance:'depth'}
  const draft={...initialFeatureDraft(),plan:{version:'cad-plan-v1',units:'mm',parameters:{R:{value:2},W:{value:3},H:{value:4},depth:{value:9}},features:[feature],result:feature.id}}
  const original=beginCadEdit(draft,feature.id),revolved=changeCadProfileOperation(original,'profile_revolve'),restored=changeCadProfileOperation(revolved,'profile_extrude')
  const plans=[original,revolved,restored].map(transaction=>cadTransactionDraft(transaction).plan)
  assert.deepEqual(plans[0].features,plans[2].features)
  const script=`import sys,json,tempfile,math
from pathlib import Path
from app.cad_plan import validate_plan
from app.cad_executor import build_plan_shape
from app.geometry import get_cadquery
cq=get_cadquery()
volumes=[]
with tempfile.TemporaryDirectory(prefix='joyniu-profile-conversion-') as folder:
 for index,plan in enumerate(json.load(sys.stdin)):
  shape,_,_=build_plan_shape(validate_plan(plan))
  output=Path(folder)/f'{index}.step'
  cq.exporters.export(shape.val(),str(output),exportType='STEP')
  restored=cq.importers.importStep(str(output)).val()
  assert restored.isValid() and len(restored.Solids())==1
  expected=84*math.pi if index==1 else 108
  assert math.isclose(restored.Volume(),expected,rel_tol=1e-6),(index,restored.Volume(),expected)
  bounds=restored.BoundingBox()
  expected_bounds=[10,10,4] if index==1 else [3,9,4]
  assert all(math.isclose(actual,expected,abs_tol=1e-6) for actual,expected in zip([bounds.xlen,bounds.ylen,bounds.zlen],expected_bounds))
  volumes.append(restored.Volume())
print(json.dumps(volumes))`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script],{cwd:resolve('apps/api'),input:JSON.stringify(plans),encoding:'utf8',timeout:30000})
  assert.equal(result.status,0,result.stderr);assert.equal(JSON.parse(result.stdout).length,3)
})
