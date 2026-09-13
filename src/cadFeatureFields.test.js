import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import React from 'react'
import { rolldown } from 'rolldown'
import { featureDefaults } from './directFeatureModel.js'
import { evaluateSketchExpression } from './featureSketchExpressions.js'

let directory,Fields
async function load(){
  if(Fields)return
  directory=await mkdtemp(resolve('node_modules/.feature-fields-tests-'))
  const bundle=await rolldown({input:resolve('src/CadFeatureFields.jsx'),external:['react/jsx-runtime'],transform:{jsx:{runtime:'automatic'}},plugins:[{
    name:'field-form-state',resolveId(source){if(source==='react')return '\0field-hooks'},load(id){if(id.endsWith('.css'))return{code:'',moduleType:'js'};if(id.endsWith('/CadSweepPathEditor.jsx'))return{code:'function Path(){return null};Path.testBoundary=true;export default Path',moduleType:'js'};if(id==='\0field-hooks')return{code:'export const useState=(...args)=>globalThis.__featureFieldHooks.useState(...args)',moduleType:'js'}},
  }]})
  await bundle.write({dir:directory,format:'esm'});await bundle.close();Fields=await import(pathToFileURL(resolve(directory,'CadFeatureFields.js')).href)
}
test.after(async()=>{if(directory)await rm(directory,{recursive:true,force:true});delete globalThis.__featureFieldHooks})
function form(feature,extra={}){
  const changes=[],sketches=[],sketchCalls=[],slots=[];let cursor=0,tree,props={feature,preceding:[{id:'base',label:'初始体'},{id:'tool',label:'工具体'}],...extra}
  globalThis.__featureFieldHooks={useState(initial){const i=cursor++;if(!slots[i])slots[i]={value:initial};return[slots[i].value,next=>{slots[i].value=typeof next==='function'?next(slots[i].value):next}]}}
  props.onChange=next=>{changes.push(next);props={...props,feature:next}}
  props.onSketch=(...args)=>{sketches.push(props.feature);sketchCalls.push(args);extra.onSketch?.(...args)}
  const render=()=>{cursor=0;tree=Fields.FeatureEditor(props)}
  const nodes=(value=tree)=>React.isValidElement(value)?[value,...React.Children.toArray(typeof value.type==='function'?value.type(value.props):value.props.children).flatMap(nodes)]:[]
  const text=value=>React.isValidElement(value)?React.Children.toArray(value.props.children).map(text).join(''):String(value??'')
  const find=predicate=>{const node=nodes().find(predicate);assert.ok(node,'field control exists');return node}
  const input=label=>find(node=>['input','select'].includes(node.type)&&node.props['aria-label']===label)
  const button=label=>find(node=>node.type==='button'&&(text(node)===label||node.props.className===label))
  const change=(label,value)=>{input(label).props.onChange({target:{value,checked:value}});render()}
  const click=label=>{button(label).props.onClick();render()}
  render();return{nodes,input,button,change,click,changes,sketches,sketchCalls,feature:()=>props.feature,text:()=>nodes().filter(node=>typeof node.type==='string').map(text).join(' ')}
}
const profile=()=>({id:'raised',op:'profile_extrude',label:'所选面拉伸',plane:'custom',frame:{origin:[10,8,10],xDir:[1,0,0],normal:[0,0,1]},
  planeSource:{kind:'face',sourceFeatureId:'base',geometryVersion:'a'.repeat(64),signature:'b'.repeat(64),index:2,binding:{version:1,key:'c'.repeat(64)}},
  planeAttachment:{version:1,origin:[2,0,0],xDir:[1,0,0],normal:[0,0,1]},
  start:[0,0],segments:[{type:'arc',through:[2,2],to:[4,0],radius:'R'},{type:'line',to:[0,0]}],distance:'depth',sketchConstraints:[]})

test('all registered operation forms expose only scalar/vector controls and preserve every unrelated source field',async()=>{
  await load()
  for(const[op,defaults]of Object.entries(featureDefaults)){
    const source={id:'part',op,...structuredClone(defaults),sourceMetadata:{drawing:'source',uncertainty:['keep']}}
    if('input'in source)source.input='base';if('inputs'in source)source.inputs=['base','tool']
    const before=structuredClone(source),ui=form(source)
    assert.equal(ui.changes.length,0)
    for(const node of ui.nodes())if(node.type==='input')assert.ok(node.props.value===undefined||['string','number'].includes(typeof node.props.value),`${op} must not put objects in an input`)
    assert.ok(!ui.text().includes('[object Object]'),op)
    ui.change('特征名称','改名')
    assert.deepEqual(ui.feature(),{...before,label:'改名'},`${op} rename must not add default geometry or drop metadata`)
    assert.deepEqual(source,before)
  }
})

test('editing a custom profile keeps its attachment, signed plane and optional arc radius without injecting default origin',async()=>{
  await load();const source=profile(),ui=form(source)
  ui.change('距离 (mm)','depth + 2')
  assert.equal(Object.hasOwn(ui.feature(),'origin'),false)
  assert.deepEqual(ui.feature(),{...source,distance:'depth + 2'})
  assert.ok(!ui.nodes().some(node=>node.type==='input'&&/binding|planeAttachment/.test(node.props['aria-label']||'')))
  ui.click('ce-sketch-reference');assert.deepEqual(ui.sketches,[ui.feature()])
  assert.equal(ui.feature().segments[0].radius,'R')
})

test('custom mirror parameters and switching planes never mix frame with origin or discard unrelated metadata',async()=>{
  await load();const source={id:'mirror1',op:'mirror',input:'base',plane:'custom',frame:{origin:['offset',0,0],xDir:[0,1,0],normal:[1,0,0]},keepOriginal:true,sourceMetadata:{note:'preserve'}}
  const ui=form(source)
  ui.change('平面原点 X','offset + 3');assert.equal(Object.hasOwn(ui.feature(),'origin'),false)
  assert.deepEqual(ui.feature().frame.origin,['offset + 3',0,0])
  ui.change('镜像平面','XZ');assert.equal(Object.hasOwn(ui.feature(),'frame'),false);assert.deepEqual(ui.feature().origin,['offset + 3',0,0])
  ui.change('镜像平面','custom');assert.equal(Object.hasOwn(ui.feature(),'origin'),false)
  assert.deepEqual(ui.feature().frame,{origin:['offset + 3',0,0],xDir:[1,0,0],normal:[0,-1,0]})
  const keep=ui.nodes().find(node=>node.type==='input'&&node.props.type==='checkbox');keep.props.onChange({target:{checked:false}})
  assert.equal(ui.feature().keepOriginal,false);assert.deepEqual(ui.feature().sourceMetadata,source.sourceMetadata)
})

test('box and tilted cylinder sketch entry converts the edited feature before entering and preserves driving expressions',async()=>{
  await load()
  const parameters={W:{value:20},H:{value:16},D:{value:10},R:{value:3},axis:{value:1}}
  const box=form({id:'b',op:'box',size:['W','H','D'],origin:[2,3,4]},{parameters})
  box.click('ce-sketch-reference');assert.equal(box.sketches.length,1);assert.equal(box.sketches[0].op,'profile_extrude')
  assert.deepEqual(box.feature().segments[1].to,['W','H']);assert.equal(box.feature().distance,'D');assert.equal(Object.hasOwn(box.feature(),'size'),false)
  const cylinder=form({id:'c',op:'cylinder',radius:'R',height:'D',direction:['axis',0,1],origin:[2,3,4]},{parameters})
  cylinder.click('ce-sketch-reference');assert.equal(cylinder.sketches.length,1)
  assert.equal(cylinder.feature().plane,'custom');assert.equal(Object.hasOwn(cylinder.feature(),'origin'),false)
  assert.deepEqual(cylinder.feature().frame.origin,[2,3,4]);assert.equal(cylinder.feature().start[0],'R');assert.equal(cylinder.feature().distance,'D')
  assert.ok(Math.abs(evaluateSketchExpression(cylinder.feature().frame.normal[0],parameters)-1/Math.sqrt(2))<1e-10)
  assert.equal(evaluateSketchExpression(cylinder.feature().segments[0].to[0],parameters),-3)
  for(const key of ['radius','height','direction'])assert.equal(Object.hasOwn(cylinder.feature(),key),false)
  const unknown=form({id:'c',op:'cylinder',radius:'R',height:'D',direction:['missing',0,1]},{parameters})
  unknown.click('ce-sketch-reference');assert.equal(unknown.sketches.length,0);assert.equal(unknown.changes.length,0);assert.match(unknown.text(),/missing/)
})

test('rotation vectors have correct dimensions and malformed coordinates never become object strings',async()=>{
  await load()
  const revolve=form({id:'r',op:'profile_revolve',...structuredClone(featureDefaults.profile_revolve)})
  assert.equal(revolve.nodes().filter(node=>node.type==='input'&&node.props['aria-label']?.startsWith('轴起点')).length,2)
  revolve.change('轴起点 U','offset');assert.deepEqual(revolve.feature().axisStart,['offset',0])
  const rotate=form({id:'r',op:'rotate',...structuredClone(featureDefaults.rotate)})
  assert.equal(rotate.nodes().filter(node=>node.type==='input'&&node.props['aria-label']?.startsWith('轴起点')).length,3)
  const broken=form({id:'b',op:'box',size:[20,{bad:true}],origin:null})
  assert.match(broken.text(),/请填写 3 个坐标/);assert.ok(!broken.text().includes('[object Object]'));assert.equal(broken.changes.length,0)
  const unresolved=form({id:'b',op:'box',size:[20,null,'H']})
  unresolved.change('尺寸 (mm) 长','25');assert.deepEqual(unresolved.feature().size,[25,null,'H'])
})

test('optional chamfer side stays removed and zero loft radius remains a circle while section/path additions preserve formulas',async()=>{
  await load();const chamfer=form({id:'f',op:'chamfer',input:'base',length:'L',length2:0,edges:'all'})
  assert.equal(chamfer.input('第二侧 (mm)').props.value,0)
  chamfer.click('恢复等距倒角');chamfer.change('距离 (mm)','L + 1');assert.equal(Object.hasOwn(chamfer.feature(),'length2'),false)
  chamfer.click('设置第二侧倒角距离');assert.equal(chamfer.feature().length2,'L + 1')
  const loft=form({id:'l',op:'loft',sections:[{origin:[0,0,0],radius:0},{origin:[0,0,'Z'],radius:'R',source:{note:'keep'}}]})
  assert.equal(loft.input('截面 1 类型').props.value,'circle');assert.equal(loft.input('截面 1 半径').props.value,0)
  loft.click('＋ 截面');assert.deepEqual(loft.feature().sections.at(-1),{origin:[0,0,'(Z) + 10'],radius:'R',source:{note:'keep'}})
  loft.change('截面 2 类型','rectangle');assert.equal(Object.hasOwn(loft.feature().sections[1],'radius'),false);assert.deepEqual(loft.feature().sections[1].source,{note:'keep'})
  const sweep=form({id:'s',op:'sweep',radius:2,path:[[0,0,0],['X',0,'Z']]})
  sweep.click('＋ 路径点');assert.deepEqual(sweep.feature().path.at(-1),['X',0,'(Z) + 10'])
})

test('omitted optional fields display the kernel defaults rather than new-feature example dimensions',async()=>{
  await load()
  const source={id:'g',op:'gear',module:2,teeth:24,pressureAngle:20,width:10},gear=form(source)
  assert.equal(gear.input('轴孔直径').props.value,0,'an omitted bore must not be displayed as the example 8 mm hole')
  gear.change('特征名称','原无孔齿轮');assert.equal(Object.hasOwn(gear.feature(),'boreDiameter'),false)
  const mirror=form({id:'m',op:'mirror',input:'base',plane:'YZ'})
  assert.equal(mirror.nodes().find(node=>node.type==='input'&&node.props.type==='checkbox').props.checked,false,'the kernel mirrors without keeping the original unless requested')
  mirror.change('特征名称','镜像');assert.equal(Object.hasOwn(mirror.feature(),'keepOriginal'),false)
})

test('custom-plane field edits and primitive sketch conversion preserve real OCCT solids through STEP exchange',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},async()=>{
  await load();const outputs=[]
  for(const feature of [{id:'b',op:'box',size:[20,16,10],origin:[2,3,4]},{id:'c',op:'cylinder',radius:3,height:10,direction:[1,0,1],origin:[2,3,4]}]){
    const ui=form(feature);ui.click('ce-sketch-reference');outputs.push([ui.feature()])
  }
  const ui=form({id:'m',op:'mirror',input:'base',plane:'custom',frame:{origin:[30,0,0],xDir:[0,1,0],normal:[1,0,0]},keepOriginal:true})
  ui.change('平面原点 X','32');outputs.push([{id:'base',op:'box',size:[20,16,10]},ui.feature()])
  ui.change('镜像平面','YZ');outputs.push([{id:'base',op:'box',size:[20,16,10]},structuredClone(ui.feature())])
  ui.change('镜像平面','custom');outputs.push([{id:'base',op:'box',size:[20,16,10]},ui.feature()])
  const plans=outputs.map(features=>({version:'cad-plan-v1',units:'mm',parameters:{},features,result:features.at(-1).id}))
  const script=`import sys,json,tempfile,math
from pathlib import Path
from app.cad_plan import validate_plan
from app.cad_executor import build_plan_shape
from app.geometry import get_cadquery
cq=get_cadquery()
volumes=[]
with tempfile.TemporaryDirectory(prefix='joyniu-field-geometry-') as folder:
 for i,plan in enumerate(json.load(sys.stdin)):
  shape,_,_=build_plan_shape(validate_plan(plan))
  output=Path(folder)/f'{i}.step'
  cq.exporters.export(shape.val(),str(output),exportType='STEP')
  restored=cq.importers.importStep(str(output)).val()
  assert restored.isValid() and restored.Solids()
  expected=math.pi*3**2*10 if i==1 else 3200 if i==0 else 6400
  assert math.isclose(restored.Volume(),expected,rel_tol=1e-5),(i,restored.Volume(),expected)
  volumes.append(restored.Volume())
print(json.dumps(volumes))`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script],{cwd:resolve('apps/api'),input:JSON.stringify(plans),encoding:'utf8',timeout:30000})
  assert.equal(result.status,0,result.stderr);assert.equal(JSON.parse(result.stdout).length,5)
})

test('sweep fields forward the transaction lock to their graphic canvas and loft callbacks retain the new section payload',async()=>{
  await load()
  const sweep=form({id:'s',op:'profile_sweep',plane:'XY',start:[0,0],segments:[{type:'line',to:[2,0]},{type:'line',to:[2,2]},{type:'line',to:[0,2]}],path:{start:[0,0,0],segments:[]}},{disabled:true})
  const path=sweep.nodes().find(n=>n.type?.testBoundary===true);assert.ok(path);assert.equal(path.props.disabled,true)
  const loft=form({id:'l',op:'profile_loft',sections:[]});loft.click('＋ 绘制新截面')
  assert.equal(loft.sketchCalls[0][0],0);assert.deepEqual(loft.sketchCalls[0][1],loft.feature());assert.deepEqual(loft.feature().sections[0].segments,[])
})

test('imported asset fields preserve required identifiers on rename without offering fictitious primitive parameters',async()=>{
  await load();const feature={id:'imported',op:'import_step',assetId:`asset_${'a'.repeat(32)}`,sha256:'b'.repeat(64)},ui=form(feature)
  ui.change('特征名称','导入轴套')
  assert.deepEqual(ui.feature(),{...feature,label:'导入轴套'})
  assert.equal(ui.nodes().some(n=>n.type==='input'&&/尺寸|半径|距离/.test(n.props['aria-label']||'')),false)
})
