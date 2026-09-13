import test from 'node:test'
import assert from 'node:assert/strict'
import {existsSync} from 'node:fs'
import {resolve} from 'node:path'
import {spawnSync} from 'node:child_process'
import {addSketchContour,replaceSketchContour,deleteSketchContour,setSketchContourRole,constraintContours} from './featureSketchContours.js'
import {replaceSketchShape,removeSketchPoint,validateSketchContours,sketchGeometry} from './featureSketchModel.js'
import {addSketchConstraint,changeSketchConstraintValue,changeLinkedSketchParameter,sketchConstraintResiduals,validateSketchConstraints} from './featureSketchConstraints.js'
import {sketchCurve} from './featureSketchConstraintGeometry.js'
import {sketchContractIssue} from './featureSketchContract.js'
import {resolveSketchFeature} from './featureSketchExpressions.js'
import {sketchSplineBeziers,ellipsePoint} from './featureSketchCurves.js'
import {describeModelEditing} from './modelEditing.js'

const profile=()=>({id:'part',op:'profile_extrude',plane:'XY',start:[0,0],segments:[{type:'line',to:[20,2]},{type:'line',to:[18,12]},{type:'line',to:[1,10]},{type:'line',to:[0,0]}],distance:5})
const shape=(type,values)=>replaceSketchShape(profile(),type,values)
const circle=(x,y,radius)=>shape('circle',{x,y,radius})
const add=(feature,local,role='outer',parent='main')=>{const created=addSketchContour(feature,{role,parent});return{feature:replaceSketchContour(created.feature,created.id,local,{replaceConstraints:true}),id:created.id}}
const near=(a,b,tol=1e-7)=>assert.ok(Math.abs(a-b)<tol,`${a} ≈ ${b}`)
const check=f=>{assert.equal(sketchContractIssue(f),'');assert.equal(validateSketchContours(f),true);assert.equal(validateSketchConstraints(f),true);assert.ok(sketchConstraintResiduals(f).every(v=>Math.abs(v)<1e-5))}
const actualCases=[]

test('parallel, perpendicular, equal and angle relationships change real points and remain satisfied when angle is edited',()=>{
  for(const type of ['parallel','perpendicular','equal','angle']){
    const original=profile(),before=structuredClone(original),edges=type==='parallel'||type==='equal'?[0,2]:[0,1]
    let result=addSketchConstraint(original,type,{edges,...(type==='angle'?{value:60}:{})}).feature
    assert.deepEqual(original,before);assert.notDeepEqual(result.segments,original.segments);check(result)
    const a=sketchCurve(result,edges[0]),b=sketchCurve(result,edges[1]);if(type==='equal')near(a.length,b.length)
    if(type==='angle'){const id=result.sketchConstraints.at(-1).id;result=changeSketchConstraintValue(result,id,45).feature;check(result);const [u,v]=[sketchCurve(result,0),sketchCurve(result,1)].map(c=>c.tangents[0]);near(Math.acos((u[0]*v[0]+u[1]*v[1])/(Math.hypot(...u)*Math.hypot(...v)))*180/Math.PI,45)}
    actualCases.push({name:type,feature:result})
  }
})

test('circle relationships cross contours, keep radii during concentric solve and drive persisted radii',()=>{
  const built=add(circle(0,0,12),circle(2,1,3),'hole'),ref={contourId:built.id,edge:0}
  const result=addSketchConstraint(built.feature,'concentric',{edges:[0,ref]}).feature
  check(result);const a=sketchCurve(result,0).circle,b=sketchCurve(result,ref).circle
  a.center.forEach((v,i)=>near(v,b.center[i]));near(a.radius,12);near(b.radius,3)
  const dimensioned=addSketchConstraint(result,'radius',{edge:ref,value:4}).feature
  check(dimensioned);near(sketchCurve(dimensioned,ref).circle.radius,4);sketchCurve(dimensioned,ref).circle.center.forEach((v,i)=>near(v,b.center[i]))
  assert.throws(()=>deleteSketchContour(dimensioned,built.id),/关联约束/)
  actualCases.push({name:'concentric-hole',feature:dimensioned,volume:Math.PI*(144-16)*5})
  const pair=add(circle(0,0,10),circle(30,0,5)),equal=addSketchConstraint(pair.feature,'equal',{edges:[0,{contourId:pair.id,edge:0}]}).feature
  check(equal);near(sketchCurve(equal,0).circle.radius,sketchCurve(equal,{contourId:pair.id,edge:0}).circle.radius)
  actualCases.push({name:'equal-two-circles',feature:equal,solids:2})
})

test('full circular ellipse radius is solved without perturbing its circle classification or center',()=>{
  const original=shape('ellipse',{x:2,y:3,rx:6,ry:6,rotation:20})
  const dimensioned=addSketchConstraint(original,'radius',{edge:0,value:8}).feature
  check(dimensioned);near(dimensioned.segments[0].radii[0],8);near(dimensioned.segments[0].radii[1],8);assert.deepEqual(dimensioned.segments[0].center,[2,3])
  const updated=changeSketchConstraintValue(dimensioned,dimensioned.sketchConstraints[0].id,9).feature
  check(updated);near(updated.segments[0].radii[0],9);near(updated.segments[0].radii[1],9)
  actualCases.push({name:'circular-ellipse-radius',feature:updated,volume:Math.PI*81*5})
})

test('tangent constrains actual shared-endpoint derivatives and rejects disconnected curves without guessing joins',()=>{
  const capsule={...profile(),start:[0,0],segments:[{type:'line',to:[10,0]},{type:'arc',through:[16,5],to:[10,10]},{type:'line',to:[0,10]},{type:'line',to:[0,0]}],sketchConstraints:[{id:'start_fixed',type:'fixed',points:['start'],position:[0,0]},{id:'join_fixed',type:'fixed',points:['0:to'],position:[10,0]},{id:'end_fixed',type:'fixed',points:['1:to'],position:[10,10]}]}
  const result=addSketchConstraint(capsule,'tangent',{edges:[0,1]}).feature
  check(result);near(result.segments[1].through[0],15);near(result.segments[1].through[1],5,1e-5)
  actualCases.push({name:'tangent-capsule',feature:result,volume:(100+Math.PI*25/2)*5})
  const separate=add(profile(),circle(60,60,4)),before=structuredClone(separate.feature)
  assert.throws(()=>addSketchConstraint(separate.feature,'tangent',{edges:[0,{contourId:separate.id,edge:0}]}),/共同端点/);assert.deepEqual(separate.feature,before)
})

test('angle parameter expressions stay bound when a parameter change resolves the connected geometry',()=>{
  const initial=addSketchConstraint(profile(),'angle',{edges:[0,1],value:'angleDeg'},{angleDeg:{value:60,source:'user'}})
  const changed=changeLinkedSketchParameter(initial.feature,initial.parameters,'angleDeg',45)
  assert.equal(changed.feature.sketchConstraints[0].value,'angleDeg');assert.equal(changed.parameters.angleDeg.value,45);assert.equal(changed.parameters.angleDeg.source,'user')
  validateSketchConstraints(changed.feature,changed.parameters)
  assert.notDeepEqual(changed.feature.segments,initial.feature.segments)
})

test('contour deletion and role changes preserve internal constraints and reject real external dependencies',()=>{
  const built=add(profile(),circle(40,0,3)),id=built.id
  const local={id:'local_radius',type:'radius',edge:{contourId:id,edge:0},value:3}
  assert.deepEqual(constraintContours(local),[id]);assert.deepEqual(constraintContours({id:'f',type:'fixed',points:[{contourId:id,point:'start'}],position:[43,0]}),[id])
  const withDimension={...built.feature,sketchConstraints:[...built.feature.sketchConstraints,local]}
  const deleted=deleteSketchContour(withDimension,id);assert.equal(deleted.contours.length,0);assert.equal(deleted.sketchConstraints.length,0)
  const asHole=setSketchContourRole(withDimension,id,'hole');assert.equal(asHole.contours[0].parent,'main');assert.equal(setSketchContourRole(asHole,id,'outer').contours[0].parent,undefined)
  const parent=add(profile(),circle(50,0,10)),child=add(parent.feature,circle(50,0,3),'hole',parent.id)
  assert.throws(()=>deleteSketchContour(child.feature,parent.id),/包含孔洞/);assert.throws(()=>setSketchContourRole(child.feature,parent.id,'hole'),/包含孔洞/)
  const crossed={...built.feature,sketchConstraints:[{id:'cross',type:'equal',edges:[0,{contourId:id,edge:0}]}]}
  assert.throws(()=>replaceSketchContour(crossed,id,circle(40,0,5),{replaceConstraints:true}),/关联约束/)
})

test('spline interpolation-point deletion is explicit and preserves segment identity and unrelated geometry',()=>{
  const feature={...profile(),segments:[{type:'spline',through:[[3,5],[6,4],[8,3]],to:[10,0]},{type:'line',to:[0,-5]},{type:'line',to:[0,0]}]}
  const reduced=removeSketchPoint(feature,'0:through:1');assert.deepEqual(reduced.segments[0].through,[[3,5],[8,3]]);assert.deepEqual(reduced.segments.slice(1),feature.segments.slice(1))
  const one=removeSketchPoint(reduced,'0:through:1');assert.throws(()=>removeSketchPoint(one,'0:through:0'),/至少保留/)
  assert.deepEqual(feature.segments[0].through,[[3,5],[6,4],[8,3]])
})

test('curve contract recovers saved profiles and unknown expressions while rejecting malformed references and empty drafts',()=>{
  const ellipse=shape('ellipse',{x:0,y:0,rx:10,ry:5,rotation:30}),multi=add(ellipse,circle(0,0,2),'hole').feature
  check(multi)
  const plan={version:'cad-plan-v1',units:'mm',parameters:{},features:[multi],result:multi.id}
  const entry=describeModelEditing({accountKey:'owner',fileId:'file',model:{kind:'feature_model',cadPlan:plan,agentRun:{runId:`cad_${'a'.repeat(32)}`,revision:3,status:'ready'}}})
  assert.equal(entry.editable,true,entry.reason);assert.deepEqual(entry.initialPlan,plan)
  const unresolved=structuredClone(multi);unresolved.start=['X','Y'];unresolved.segments[0].radii=['RX','RY'];assert.equal(sketchContractIssue(unresolved),'')
  assert.throws(()=>validateSketchContours(unresolved),/参数/)
  const empty=addSketchContour(multi,{role:'hole'}).feature;assert.throws(()=>validateSketchContours(empty),/为空/);assert.notEqual(sketchContractIssue(empty),'')
  for(const mutate of [f=>f.contours[0].parent='missing',f=>f.contours[0].id='main',f=>f.sketchConstraints=[{id:'bad',type:'parallel',edges:[0,{contourId:'missing',edge:0}]}],f=>f.segments[0].radii=[-1,3],f=>f.segments[0].unknown=1]){const bad=structuredClone(multi);mutate(bad);assert.notEqual(sketchContractIssue(bad),'')}
})

test('every solved constraint fixture validates in independent backend and exports true valid STEP', {skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  assert.equal(actualCases.length,8)
  const code=`import json,sys,math,tempfile\nfrom pathlib import Path\nfrom app.cad_plan import validate_plan\nfrom app.cad_executor import build_plan_shape\nfrom app.geometry import get_cadquery\ncq=get_cadquery()\nfor case in json.load(sys.stdin):\n f=case['feature'];s,_,_=build_plan_shape(validate_plan({'version':'cad-plan-v1','units':'mm','features':[f],'result':f['id']}))\n with tempfile.TemporaryDirectory() as d:\n  path=Path(d)/'constraints.step';cq.exporters.export(s.val(),str(path),exportType='STEP');r=cq.importers.importStep(str(path)).val()\n  assert r.isValid() and len(r.Solids())==case.get('solids',1),case['name']\n  if 'volume' in case: assert math.isclose(r.Volume(),case['volume'],rel_tol=1e-7),(case['name'],r.Volume(),case['volume'])\n print(case['name'],r.Volume())`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',code],{cwd:resolve('apps/api'),input:JSON.stringify(actualCases),encoding:'utf8',timeout:60000});assert.equal(result.status,0,result.stderr)
})

test('ellipse axis formulas persist and resizing their parameter keeps exact ellipse endpoints',async()=>{
  const {driveSketchEllipseRadius}=await import('./featureSketchConstraints.js')
  const source=shape('ellipse',{x:2,y:3,rx:10,ry:5,rotation:30}),parameters={major:{value:12,source:'user'}}
  const driven=driveSketchEllipseRadius(source,0,0,'major',parameters)
  assert.equal(driven.feature.segments[0].radii[0],'major');assert.equal(parameters.major.value,12)
  const changed=changeLinkedSketchParameter(driven.feature,driven.parameters,'major',14)
  assert.equal(changed.feature.segments[0].radii[0],'major');assert.deepEqual(changed.feature.start,driven.feature.start)
  const resolved=resolveSketchFeature(changed.feature,changed.parameters).feature
  near(resolved.segments[0].radii[0],14);assert.deepEqual(resolved.segments[0].center,[2,3]);near(resolved.start[0],2+14*Math.cos(Math.PI/6));near(resolved.start[1],10)
  assert.equal(validateSketchConstraints(changed.feature,changed.parameters),true)
})
