import test,{before,after} from 'node:test'
import assert from 'node:assert/strict'
import {mkdtemp,rm} from 'node:fs/promises'
import {existsSync} from 'node:fs'
import {spawnSync} from 'node:child_process'
import {resolve} from 'node:path'
import {pathToFileURL} from 'node:url'
import React from 'react'
import {rolldown} from 'rolldown'
import {sweepPathFromSketch,sampleSweepSegment,moveSweepPathPoint} from './cadSweepPathModel.js'
import {replaceSketchShape} from './featureSketchModel.js'
let folder,Fields,PathEditor
before(async()=>{
  folder=await mkdtemp(resolve('node_modules/.profile-tools-'))
  for(const [name,input]of [['fields','src/CadProfileTools.jsx'],['path','src/CadSweepPathEditor.jsx']]){
    const build=await rolldown({input:resolve(input),external:['react/jsx-runtime','react-dom'],transform:{jsx:{runtime:'automatic'}},plugins:[{name:'profile-hooks',resolveId(s){if(s==='react')return'\0profile-hooks'},load(id){if(id==='\0profile-hooks')return{code:['useState','useRef','useEffect'].map(name=>`export const ${name}=(...args)=>globalThis.__profileHooks.${name}(...args)`).join('\n'),moduleType:'js'};if(id.endsWith('.css'))return{code:'',moduleType:'js'}}}]})
    try{await build.write({file:resolve(folder,`${name}.mjs`),format:'esm'})}finally{await build.close()}
  }
  Fields=await import(pathToFileURL(resolve(folder,'fields.mjs')).href);PathEditor=(await import(pathToFileURL(resolve(folder,'path.mjs')).href)).default
})
after(async()=>{if(folder)await rm(folder,{recursive:true,force:true});delete globalThis.__profileHooks})
function harness(Component,initial,props={}){
  const slots=[],changes=[],calls=[];let cursor=0,effects=[],dirty=true,tree,value=structuredClone(initial)
  const bounds={left:0,top:0,width:400,height:300},canvas={getBoundingClientRect:()=>bounds,setPointerCapture(){},releasePointerCapture(){}}
  globalThis.__profileHooks={useState(initial){const i=cursor++;slots[i]??={value:typeof initial==='function'?initial():initial};return[slots[i].value,next=>{const v=typeof next==='function'?next(slots[i].value):next;if(v!==slots[i].value){slots[i].value=v;dirty=true}}]},useRef(initial){return slots[cursor++]??={current:initial}},useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||deps.some((v,j)=>!Object.is(v,old.deps[j]))){slots[i]={deps,cleanup:old?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn()})}}}
  const nodes=(v=tree)=>React.isValidElement(v)?[v,...React.Children.toArray(v.props.children).flatMap(nodes)]:[]
  const text=v=>React.isValidElement(v)?React.Children.toArray(v.props.children).map(text).join(''):String(v??'')
  const render=()=>{let guard=0;do{dirty=false;cursor=0;tree=Component({...props,feature:value,path:value,onChange:next=>{value=next;changes.push(structuredClone(next));dirty=true},onSketch:(...args)=>calls.push(args)});const svg=nodes().find(n=>n.type==='svg');if(svg)svg.props.ref.current=canvas;while(effects.length)effects.shift()();assert.ok(++guard<30)}while(dirty)}
  const find=pred=>{const n=nodes().find(pred);assert.ok(n,'expected profile control');return n},label=value=>find(n=>n.props['aria-label']===value),button=value=>find(n=>n.type==='button'&&text(n)===value),event=(n,key,arg)=>{n.props[key](arg);render()},click=name=>event(button(name),'onClick'),input=(name,v)=>event(label(name),'onChange',{target:{value:v}}),svg=()=>find(n=>n.type==='svg')
  const pointer=(x,y)=>{const [a,b,w,h]=svg().props.viewBox.split(' ').map(Number),s=Math.min(bounds.width/w,bounds.height/h);return{clientX:(bounds.width-w*s)/2+(x-a)*s,clientY:(bounds.height-h*s)/2+(-y-b)*s,pointerId:1,button:0,stopPropagation(){},preventDefault(){}}}
  const update=next=>{props={...props,...next};render()};render()
  return{find,label,button,click,input,event,svg,pointer,update,nodes,value:()=>value,text:()=>text(tree),changes,calls,cleanup(){slots.forEach(s=>s?.cleanup?.())}}
}
const empty=()=>({start:['','',''],segments:[]})
const realCases=[]

test('path graphics use real user points, append lines and arcs without auto-closure, and retain third-axis coordinates during drag',()=>{
  const ui=harness(PathEditor,empty())
  try{
    ui.click('路径直线');ui.event(ui.svg(),'onPointerDown',ui.pointer(0,0));assert.equal(ui.changes.length,0)
    ui.event(ui.svg(),'onPointerDown',ui.pointer(0,20));assert.deepEqual(ui.value(),{start:[0,0,0],segments:[{type:'line',to:[0,0,20]}]})
    ui.click('路径圆弧');ui.event(ui.svg(),'onPointerDown',ui.pointer(5,25));assert.equal(ui.changes.length,1);ui.event(ui.svg(),'onPointerDown',ui.pointer(10,20))
    assert.deepEqual(ui.value().segments[1],{type:'arc',through:[5,0,25],to:[10,0,20]})
    ui.click('路径直线');ui.event(ui.svg(),'onPointerDown',ui.pointer(10,0));realCases.push({name:'graphical-line-arc-line',path:structuredClone(ui.value()),length:40+5*Math.PI})
    ui.click('选择 / 拖点');const count=ui.changes.length;ui.event(ui.label('路径控制点 2:to'),'onPointerDown',ui.pointer(10,0));ui.event(ui.svg(),'onPointerMove',ui.pointer(12,1));assert.equal(ui.changes.length,count);ui.event(ui.svg(),'onPointerUp',ui.pointer(12,1));assert.deepEqual(ui.value().segments[2].to,[12,0,1]);assert.equal(ui.changes.length,count+1)
  }finally{ui.cleanup()}
})

test('path spline commits only after explicit finish, rejects duplicate points and can be canceled without a mutation',()=>{
  const ui=harness(PathEditor,empty())
  try{
    ui.click('路径样条');for(const point of [[0,0],[0,10],[5,20]])ui.event(ui.svg(),'onPointerDown',ui.pointer(...point));assert.equal(ui.changes.length,0);ui.click('完成路径样条')
    assert.deepEqual(ui.value(),{start:[0,0,0],segments:[{type:'spline',through:[[0,0,10]],to:[5,0,20]}]});realCases.push({name:'graphical-spline',path:structuredClone(ui.value())})
    const saved=structuredClone(ui.value());ui.click('路径圆弧');ui.event(ui.svg(),'onPointerDown',ui.pointer(8,22));ui.event(ui.find(n=>n.props.className==='cad-path-editor'),'onKeyDown',{key:'Escape',stopPropagation(){}});assert.deepEqual(ui.value(),saved)
    ui.click('路径直线');ui.event(ui.svg(),'onPointerDown',ui.pointer(5,20));assert.match(ui.text(),/不能与起点重合/);assert.deepEqual(ui.value(),saved)
  }finally{ui.cleanup()}
})

test('independent ellipse paths are real 3D curves and manual editing explicitly detaches the sketch reference',()=>{
  const ellipse=replaceSketchShape({id:'route',plane:'XY',start:[0,0],segments:[]},'ellipse',{x:0,y:0,rx:20,ry:10,rotation:0}),path=sweepPathFromSketch(ellipse)
  assert.equal(path.sketchId,'route');assert.deepEqual(path.start,[20,0,0]);assert.deepEqual(path.segments[0].center,[0,0,0]);assert.deepEqual(path.segments[0].xDir,[1,0,0]);assert.deepEqual(path.segments[0].normal,[0,0,1]);assert.equal(path.segments[0].type,'ellipse')
  const samples=sampleSweepSegment(path.start,path.segments[0]);assert.ok(Math.abs(samples[18][0])<1e-8);assert.ok(Math.abs(samples[18][1]-10)<1e-8)
  const ui=harness(Fields.CadSweepFields,{id:'s',op:'profile_sweep',path:empty()},{sketches:[ellipse]})
  try{ui.input('扫掠路径草图','route');assert.deepEqual(ui.value().path,path);ui.input('扫掠路径草图','');assert.equal(ui.value().path.sketchId,undefined);assert.equal(ui.value().path.segments[0].type,'ellipse')}finally{ui.cleanup()}
  realCases.push({name:'independent-ellipse-path',path,frame:{origin:[20,0,0],xDir:[1,0,0],normal:[0,1,0]}})
})

test('path projected point drag preserves hidden coordinate and cannot overwrite expressions',()=>{
  const original={sketchId:'source',start:[1,7,3],segments:[{type:'line',to:[5,7,10]}]},moved=moveSweepPathPoint(original,'0:to',[8,7,12])
  assert.deepEqual(moved.segments[0].to,[8,7,12]);assert.equal(moved.sketchId,undefined);assert.equal(original.sketchId,'source')
  assert.throws(()=>moveSweepPathPoint({...original,start:['X',7,3]},'start',[3,7,3]),/表达式/)
})

test('loft new section callbacks carry the latest draft and preserve existing sections and source references',()=>{
  const source={id:'loft',op:'profile_loft',sections:[{id:'section1',frame:{origin:[0,0,0],xDir:[1,0,0],normal:[0,0,1]},start:[0,0],segments:[]}]},ui=harness(Fields.CadLoftFields,source)
  try{ui.click('＋ 绘制新截面');assert.equal(ui.calls[0][0],1);assert.deepEqual(ui.calls[0][1],ui.value());assert.deepEqual(ui.value().sections[0],source.sections[0]);assert.deepEqual(ui.value().sections[1].segments,[])
    ui.event(ui.label('上移截面 2'),'onClick');assert.equal(ui.value().sections[0].id,'section2');ui.event(ui.label('删除截面 2'),'onClick');assert.equal(ui.value().sections.length,1)
  }finally{ui.cleanup()}
})

test('surface grid rejects invalid dimensions visibly and creates only the grid the user requested',()=>{
  const ui=harness(Fields.CadSurfaceGridFields,{id:'surface',op:'surface_style',points:[]},{onEditGrid(){}})
  const vector=label=>ui.find(n=>n.props.label===label)
  try{ui.event(vector('网格行列'),'onChange',[1,3]);ui.click('创建控制网格');assert.equal(ui.changes.length,0);assert.match(ui.text(),/2至16/)
    ui.event(vector('网格行列'),'onChange',[2,3]);ui.event(vector('网格宽高'),'onChange',[-1,30]);ui.click('创建控制网格');assert.match(ui.text(),/必须大于零/)
    ui.event(vector('网格宽高'),'onChange',[20,30]);ui.click('创建控制网格');assert.deepEqual(ui.value().points,[[[0,0,0],[10,0,0],[20,0,0]],[[0,30,0],[10,30,0],[20,30,0]]]);assert.ok(ui.button('在模型视图拖动控制点'))
  }finally{ui.cleanup()}
})

test('graphically defined paths produce valid real swept STEP solids',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  assert.equal(realCases.length,3)
  const code=`import json,sys,tempfile,math\nfrom pathlib import Path\nfrom app.cad_plan import validate_plan\nfrom app.cad_executor import build_plan_shape\nfrom app.geometry import get_cadquery\ncq=get_cadquery()\nfor case in json.load(sys.stdin):\n path=case['path'];path.pop('sketchId',None)\n f={'id':'sweep','op':'profile_sweep','plane':'XY','start':[1,0],'segments':[{'type':'arc','through':[0,1],'to':[-1,0]},{'type':'arc','through':[0,-1],'to':[1,0]}],'path':path,'transition':'transformed'}\n if 'frame' in case: f['plane']='custom';f['frame']=case['frame']\n s,_,_=build_plan_shape(validate_plan({'version':'cad-plan-v1','units':'mm','features':[f],'result':'sweep'}))\n with tempfile.TemporaryDirectory() as d:\n  p=Path(d)/'path.step';cq.exporters.export(s.val(),str(p),exportType='STEP');r=cq.importers.importStep(str(p)).val()\n  assert r.isValid() and len(r.Solids())==1,(case['name'],r.isValid())\n  if 'length' in case: assert math.isclose(r.Volume(),math.pi*case['length'],rel_tol=1e-6),(r.Volume(),math.pi*case['length'])\n print(case['name'],r.Volume())`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',code],{cwd:resolve('apps/api'),input:JSON.stringify(realCases),encoding:'utf8',timeout:60000});assert.equal(result.status,0,result.stderr)
})

test('path undo restores geometry and explicit sketch linkage, while disabled pointer intents cannot publish',()=>{
  const source={sketchId:'source',start:[0,4,0],segments:[{type:'line',to:[0,4,20]}]},ui=harness(PathEditor,source)
  try{
    ui.click('选择 / 拖点');ui.event(ui.label('路径控制点 0:to'),'onPointerDown',ui.pointer(0,20));ui.event(ui.svg(),'onPointerMove',ui.pointer(5,25));ui.event(ui.svg(),'onPointerUp',ui.pointer(5,25));assert.equal(ui.value().sketchId,undefined)
    assert.deepEqual(ui.value().segments[0].to,[5,4,25]);ui.click('撤销路径');assert.deepEqual(ui.value(),source);ui.click('重做路径');assert.deepEqual(ui.value().segments[0].to,[5,4,25])
    const saved=structuredClone(ui.value()),count=ui.changes.length;ui.event(ui.label('路径控制点 0:to'),'onPointerDown',ui.pointer(5,25));ui.event(ui.svg(),'onPointerMove',ui.pointer(9,29));ui.update({disabled:true});ui.event(ui.svg(),'onPointerUp',ui.pointer(9,29));assert.equal(ui.changes.length,count);assert.deepEqual(ui.value(),saved)
    ui.update({disabled:false});ui.click('清空路径');assert.deepEqual(ui.value(),empty());ui.click('撤销路径');assert.deepEqual(ui.value(),saved)
  }finally{ui.cleanup()}
})

test('ellipse path endpoint dragging is rejected instead of detaching valid ellipse geometry',()=>{
  const source=replaceSketchShape({id:'ellipse',plane:'XY',start:[0,0],segments:[]},'ellipse',{x:0,y:0,rx:20,ry:10,rotation:0}),path=sweepPathFromSketch(source)
  assert.throws(()=>moveSweepPathPoint(path,'start',[21,0,0]),/椭圆/)
  assert.throws(()=>moveSweepPathPoint(path,'0:to',[21,0,0]),/椭圆/)
})

test('sweep helix-reference selection replaces manual/sketch payloads atomically and exposes no editable coordinate snapshot',()=>{
  const route=replaceSketchShape({id:'route',plane:'XY',start:[0,0],segments:[]},'ellipse',{x:0,y:0,rx:20,ry:10,rotation:0}),helix={id:'helix1',label:'参数螺旋线',kind:'helix',radius:10,pitch:5,turns:3,frame:{origin:[0,0,0],xDir:[1,0,0],normal:[0,0,1]}},ui=harness(Fields.CadSweepFields,{id:'s',op:'profile_sweep',path:sweepPathFromSketch(route)},{sketches:[route],references:[helix],onPickPath(){}})
  const graphics=()=>ui.nodes().filter(n=>n.type?.name==='CadSweepPathEditor')
  try{
    ui.input('扫掠参考曲线','helix1');assert.deepEqual(ui.value().path,{curveReference:'helix1'});assert.equal(ui.label('扫掠路径草图').props.value,'');assert.equal(graphics().length,0)
    assert.equal(ui.nodes().some(n=>n.type==='button'&&n.props.children==='在模型上点选路径'),false);assert.match(ui.text(),/关联螺旋线/)
    ui.input('扫掠路径草图','route');assert.equal(ui.value().path.sketchId,'route');assert.equal(Object.hasOwn(ui.value().path,'curveReference'),false);assert.equal(ui.value().path.segments[0].type,'ellipse');assert.equal(graphics().length,1)
    ui.input('扫掠参考曲线','helix1');ui.input('扫掠路径草图','');assert.deepEqual(ui.value().path,{start:[0,0,0],segments:[]});assert.equal(graphics().length,1)
    ui.input('扫掠参考曲线','helix1');ui.input('扫掠参考曲线','');assert.deepEqual(ui.value().path,{start:[0,0,0],segments:[]})
  }finally{ui.cleanup()}
})

test('disabled sweep editors cannot replace a reference path with a stale selection event',()=>{
  const helix={id:'helix1',kind:'helix'},source={id:'s',op:'profile_sweep',path:{curveReference:'helix1'}},ui=harness(Fields.CadSweepFields,source,{references:[helix],disabled:true})
  try{ui.input('扫掠参考曲线','');ui.input('扫掠路径草图','');assert.deepEqual(ui.value(),source);assert.equal(ui.changes.length,0)}finally{ui.cleanup()}
})
