import test, {before, after} from 'node:test'
import assert from 'node:assert/strict'
import {mkdtemp, rm} from 'node:fs/promises'
import {existsSync} from 'node:fs'
import {resolve} from 'node:path'
import {pathToFileURL} from 'node:url'
import {spawnSync} from 'node:child_process'
import React from 'react'
import {rolldown} from 'rolldown'
import {
  baseReferenceFrames, referenceFrames, resolvedConstruction, independentSketchFeature,
  storeIndependentSketch, featureFromIndependentSketch, removeIndependentSketch,
  pmiMeasurement, pmiLabel, resolvedPmi, patchBodyState, maskCadBodies,
} from './cadEditorEntities.js'
import {cadAnnotatedSvg} from './cadAnnotationExport.js'
import {describeModelEditing} from './modelEditing.js'

const close = (actual, expected, tolerance=1e-9) => {
  assert.equal(actual.length, expected.length)
  actual.forEach((value,i)=>assert.ok(Math.abs(value-expected[i])<tolerance,`${actual} != ${expected}`))
}
const parameters={D:{value:10},A:{value:90},R:{value:4},P:{expression:'D / 2'},N:{value:2}}
const references=[
  {id:'raised',kind:'plane',mode:'offset',parent:'XY',offset:'D',angle:0,axis:'x'},
  {id:'tilted',kind:'plane',mode:'angle',parent:'raised',offset:2,angle:'A',axis:'y'},
  {id:'side',kind:'plane',mode:'three_points',points:[[1,2,3],[1,5,3],[1,2,7]]},
  {id:'axis1',kind:'axis',mode:'two_points',start:[0,0,'D'],end:[0,'D','D']},
  {id:'point1',kind:'point',mode:'coordinates',point:['D','D / 2',3]},
  {id:'helix1',kind:'helix',radius:'R',pitch:'P',turns:'N',frame:{origin:[1,2,3],xDir:[0,2,0],normal:[4,0,0]},lefthand:false},
]
const square={id:'sketch1',label:'安装截面',plane:'XY',origin:[0,0,0],start:[0,0],segments:[{type:'line',to:[10,0]},{type:'line',to:[10,5]},{type:'line',to:[0,5]},{type:'line',to:[0,0]}]}
const plan=(extra={})=>({version:'cad-plan-v1',units:'mm',parameters:{},features:[],sketches:[],...extra})

test('construction planes resolve chained offsets, local-axis rotations and three points without mutating expressions',()=>{
  const source=plan({parameters,references}),snapshot=structuredClone(source),frames=referenceFrames(source)
  close(frames.raised.origin,[0,0,10]);close(frames.tilted.origin,[0,0,12]);close(frames.tilted.normal,[1,0,0]);close(frames.tilted.xDir,[0,0,-1])
  close(frames.side.origin,[1,2,3]);close(frames.side.xDir,[0,1,0]);close(frames.side.normal,[1,0,0])
  const changed=referenceFrames({...source,parameters:{...parameters,D:{value:18},A:{value:0}}})
  close(changed.tilted.origin,[0,0,20]);close(changed.tilted.normal,[0,0,1]);assert.deepEqual(source,snapshot)
})

test('construction reference validation rejects missing parents, unresolved parameters and degenerate frames',()=>{
  assert.throws(()=>referenceFrames(plan({references:[{...references[0],parent:'missing'}]})),/不存在/)
  assert.throws(()=>referenceFrames(plan({references:[references[0]]})),/D.*未定义/)
  assert.throws(()=>referenceFrames(plan({references:[{id:'p',kind:'plane',mode:'three_points',points:[[0,0,0],[1,0,0],[2,0,0]]}]})),/共线/)
  assert.throws(()=>referenceFrames(plan({references:[{id:'p',kind:'plane',mode:'frame',frame:{origin:[0,0,0],xDir:[0,0,2],normal:[0,0,1]}}]})),/方向/)
})

test('helix viewport samples preserve the exact reference frame, pitch and handedness when parameters change',()=>{
  const source=plan({parameters,references}),values=resolvedConstruction(source),helix=values.find(r=>r.id==='helix1')
  close(values.find(r=>r.id==='point1').point,[10,5,3]);close(values.find(r=>r.id==='axis1').end,[0,10,10])
  close(helix.points[0],[1,6,3]);close(helix.points[32],[3.5,-2,3]);close(helix.points.at(-1),[11,6,3])
  close(helix.points[16],[2.25,2,7]);assert.equal(helix.points.length,129)
  const left=resolvedConstruction({...source,references:[{...references.at(-1),lefthand:true}],parameters:{...parameters,R:{value:6},P:{value:3}}})[0]
  close(left.points[0],[1,8,3]);close(left.points[16],[1.75,2,-3]);close(left.points.at(-1),[7,8,3])
  assert.throws(()=>resolvedConstruction({...source,parameters:{...parameters,R:{value:0}}}),/尺寸无效/)
})

test('reference UI samples agree with the real backend resolver using the same parameterized payload',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  const source=plan({parameters,references})
  const script=`import json,sys\nfrom app.cad_editor_entities import resolved_references\nfrom app.cad_plan import resolve_parameters,evaluate_expression\np=json.load(sys.stdin)\nvalues=resolve_parameters(p)\nf,r=resolved_references(p,lambda v:evaluate_expression(v,values))\nprint(json.dumps({'frames':f,'references':r}))`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script],{cwd:resolve('apps/api'),input:JSON.stringify(source),encoding:'utf8',timeout:10000})
  assert.equal(result.status,0,result.stderr)
  const backend=JSON.parse(result.stdout),frontend=resolvedConstruction(source)
  for(const reference of frontend){const other=backend.references.find(r=>r.id===reference.id);for(const key of ['origin','xDir','normal'])if(reference.frame)close(reference.frame[key],other.frame[key]);if(reference.point)close(reference.point,other.point);if(reference.start)close(reference.start,other.start);if(reference.end)close(reference.end,other.end)}
})

test('independent sketch storage removes stale standard-plane origin and old attachment metadata before custom reuse',()=>{
  const frame={origin:[1,2,3],xDir:[0,1,0],normal:[1,0,0]},custom={...square,plane:'custom',frame};delete custom.origin
  const source=plan({sketches:[square],features:[{...square,id:'extrude1',op:'profile_extrude',sketchId:'sketch1',distance:'H',planeReference:'oldPlane',planeSource:{old:true},planeAttachment:{old:true},sourceLabel:'keep me'}]})
  const result=storeIndependentSketch(source,custom,'sketch1'),feature=result.plan.features[0]
  assert.deepEqual(feature.frame,frame);assert.equal(feature.plane,'custom');for(const key of ['origin','planeReference','planeSource','planeAttachment'])assert.equal(Object.hasOwn(feature,key),false,key)
  assert.equal(feature.distance,'H');assert.equal(feature.sourceLabel,'keep me');assert.equal(source.features[0].origin[0],0)
  const reverted=storeIndependentSketch(result.plan,square,'sketch1').plan.features[0]
  assert.equal(Object.hasOwn(reverted,'frame'),false);assert.equal(reverted.plane,'XY');assert.deepEqual(reverted.origin,[0,0,0])
})

test('independent sketch reuse carries exact holes, constraints and expressions into extrusion/revolution without stale defaults',()=>{
  const custom={...square,plane:'custom',frame:baseReferenceFrames.YZ,contours:[{id:'hole1',role:'hole',parent:'main',start:[4,2],segments:[{type:'ellipse',center:[3,2],radii:[1,1],rotation:0,startAngle:0,endAngle:360,to:[4,2]}]}],sketchConstraints:[{id:'horizontal1',type:'horizontal',edge:0}]};delete custom.origin
  const source=plan({sketches:[custom]})
  for(const op of ['profile_extrude','profile_revolve']){const feature=featureFromIndependentSketch(source,'sketch1',op);assert.deepEqual(feature.frame,custom.frame);assert.deepEqual(feature.contours,custom.contours);assert.deepEqual(feature.sketchConstraints,custom.sketchConstraints);assert.equal(feature.sketchId,'sketch1');assert.equal(Object.hasOwn(feature,'origin'),false);feature.segments[0].to[0]=999;assert.equal(custom.segments[0].to[0],10)}
  const wrapper=independentSketchFeature(custom);assert.equal(wrapper.distance,1);assert.equal(wrapper.op,'profile_extrude');assert.equal(wrapper.id,custom.id);assert.equal(Object.hasOwn(wrapper,'origin'),false)
  assert.throws(()=>featureFromIndependentSketch(source,'missing','profile_extrude'),/不存在/)
})

test('deleting an independent sketch is blocked for profile, sweep path and loft-section dependencies without changing the source',()=>{
  for(const dependency of [{sketchId:'sketch1'},{path:{sketchId:'sketch1'}},{sections:[{id:'section1',sketchId:'sketch1'}]}]){
    const source=plan({sketches:[square],features:[{id:'use',...dependency}]}),snapshot=structuredClone(source)
    assert.throws(()=>removeIndependentSketch(source,'sketch1'),/仍被特征使用/);assert.deepEqual(source,snapshot)
  }
  const source=plan({sketches:[square,{...square,id:'keep'}]});assert.deepEqual(removeIndependentSketch(source,'sketch1').sketches.map(s=>s.id),['keep']);assert.equal(source.sketches.length,2)
})

test('PMI measures actual XYZ coordinates and recomputes after parameter edits',()=>{
  const distance={kind:'distance',points:[[0,0,0],['X',4,12]]}
  assert.equal(pmiMeasurement(distance,{X:{value:3}}),13);assert.equal(pmiMeasurement(distance,{X:{value:0}}),Math.sqrt(160))
  assert.ok(Math.abs(pmiMeasurement({kind:'angle',points:[[1,0,1],[0,0,0],[0,1,1]]})-60)<1e-10)
  const circle={kind:'radius',points:[[1,5,3],[1,2,6],[1,-1,3]]};close([pmiMeasurement(circle),pmiMeasurement({...circle,kind:'diameter'})],[3,6])
  assert.throws(()=>pmiMeasurement({kind:'radius',points:[[0,0,0],[1,0,0],[2,0,0]]}),/共线/)
})

test('PMI display retains engineering formats and evaluates tolerance expressions with correct signs',()=>{
  const distance={kind:'distance',points:[[0,0,0],[3,4,0]],upper:'T',lower:'-T'}
  assert.equal(pmiLabel(distance,{T:{value:0.2}}),'5 +0.2/-0.2')
  assert.equal(pmiLabel({...distance,upper:-0.1,lower:-0.2}),'5 -0.1/-0.2')
  assert.equal(pmiLabel({kind:'tolerance',value:'T / 2',symbol:'⟂',datum:'A'},{T:{value:0.2}}),'⟂ | 0.1 | A')
  assert.equal(pmiLabel({kind:'surface_finish',value:1.6}),'⌯ Ra 1.6')
  assert.equal(pmiLabel({kind:'datum',datum:'B'}),'[B]')
  assert.equal(pmiLabel({kind:'note',text:'不得缩放尺寸'}),'不得缩放尺寸')
})

test('resolved PMI hides suppressed records and never emits unresolved or non-finite world positions',()=>{
  const good={id:'n1',kind:'distance',points:[[0,0,0],['X',4,0]],position:['X',5,6]},source=plan({parameters:{X:{value:3}},annotations:[good,{...good,id:'hidden',hidden:true},{...good,id:'bad',position:['missing',0,0]}]})
  assert.deepEqual(resolvedPmi(source).map(n=>[n.id,n.displayText,n.position]),[['n1','5',[3,5,6]]]);assert.equal(source.annotations[0].points[1][0],'X')
  assert.equal(resolvedPmi({...source,parameters:{X:{value:0}}})[0].displayText,'4')
})

const png='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC'
test('annotation SVG escapes customer text and preserves screenshot pixel dimensions and projected leader coordinates',()=>{
  const title='零件 <A> & "B"',displayText='R5 <script>alert("x")</script>\nA & B \'quoted\'',svg=cadAnnotatedSvg({image:png,width:640,height:360,title,annotations:[{position:[123,45],points:[[1,2],[3,4]],displayText},{hidden:true,position:[1,1],displayText:'hidden-note'},{position:[NaN,1],displayText:'bad-note'}]})
  assert.match(svg,/width="640" height="404" viewBox="0 0 640 404"/);assert.match(svg,/<image[^>]+width="640" height="360"/);assert.match(svg,/points="1,2 3,4 123,45"/)
  assert.match(svg,/&lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/);assert.match(svg,/A &amp; B &apos;quoted&apos;/);assert.match(svg,/<tspan x="123" dy="19">/)
  assert.doesNotMatch(svg,/<script>|hidden-note|bad-note|NaN|undefined/)
  for(const invalid of [{image:'https://example.test/preview.png'},{image:'data:image/svg+xml,<svg/>'},{width:0},{height:Infinity}])assert.throws(()=>cadAnnotatedSvg({image:png,width:640,height:360,...invalid}),/画布尚未就绪/)
})

test('the exported SVG is real parseable XML and renders to the specified screenshot size plus its caption',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  const text='孔 <5> & "A"',svg=cadAnnotatedSvg({image:png,width:640,height:360,title:'QA <drawing>',annotations:[{position:[123,45],points:[[5,8],[12,20]],displayText:text}]})
  const script=`import json,sys,xml.etree.ElementTree as E\nimport fitz\ns=sys.stdin.read();root=E.fromstring(s);ns={'s':'http://www.w3.org/2000/svg'}\nassert root.find('s:title',ns).text=='QA <drawing>'\nassert root.find('.//s:tspan',ns).text=='孔 <5> & "A"'\nwith fitz.open(stream=s.encode(),filetype='svg') as source:\n pdf=source.convert_to_pdf()\nwith fitz.open(stream=pdf,filetype='pdf') as doc:\n p=doc[0].get_pixmap()\n print(json.dumps({'width':p.width,'height':p.height}))`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script],{input:svg,encoding:'utf8',timeout:15000})
  assert.equal(result.status,0,result.stderr);assert.deepEqual(JSON.parse(result.stdout.trim().split('\n').at(-1)),{width:640,height:404})
})

test('body display states are signature-scoped, preserve geometry sources, and hide only the real matching triangles',()=>{
  const body={id:'body:0',index:0,signature:'a'.repeat(64),faceIds:['face:0'],edgeIds:['edge:0']},other={id:'body:1',index:1,signature:'b'.repeat(64),faceIds:['face:1'],edgeIds:['edge:1']},source=plan(),updated=patchBodyState(source,body,{hidden:true,color:'#123456'})
  assert.equal(source.bodyStates,undefined);assert.equal(updated.bodyStates.length,1);assert.equal(patchBodyState(updated,body,{label:'支架'}).bodyStates.length,1)
  const geometry={bodies:[body,other],mesh:{positions:[0,0,0,1,0,0,0,1,0],indices:[0,1,2,0,2,1],faceIds:['face:0','face:1']},faces:[{id:'face:0'},{id:'face:1'}],edges:[{id:'edge:0'},{id:'edge:1'}]},masked=maskCadBodies(geometry,updated.bodyStates)
  assert.deepEqual(masked.mesh.indices,[0,2,1]);assert.deepEqual(masked.mesh.faceIds,['face:1']);assert.equal(masked.edges[0].hidden,true);assert.equal(masked.edges[1].hidden,false);assert.equal(masked.faces[0].color,'#123456');assert.equal(geometry.mesh.indices.length,6)
})

let panelFolder,ReferencePanel,PmiPanel
before(async()=>{
  panelFolder=await mkdtemp(resolve('node_modules/.entity-panel-tests-'))
  const bundle=await rolldown({input:{reference:resolve('src/CadReferencePanel.jsx'),pmi:resolve('src/CadPmiPanel.jsx')},external:['react/jsx-runtime'],transform:{jsx:{runtime:'automatic'}},plugins:[{name:'entity-panel-hooks',resolveId(source){if(source==='react')return'\0entity-hooks'},load(id){if(id==='\0entity-hooks')return{code:['useState','useEffect'].map(name=>`export const ${name}=(...args)=>globalThis.__entityHooks.${name}(...args)`).join('\n'),moduleType:'js'};if(id.endsWith('.css'))return{code:'',moduleType:'js'}}}]})
  try{await bundle.write({dir:panelFolder,format:'esm'})}finally{await bundle.close()}
  ReferencePanel=(await import(pathToFileURL(resolve(panelFolder,'reference.js')).href)).default
  PmiPanel=(await import(pathToFileURL(resolve(panelFolder,'pmi.js')).href)).default
})
after(async()=>{if(panelFolder)await rm(panelFolder,{recursive:true,force:true});delete globalThis.__entityHooks})
function panelHarness(Component,initialProps){
  const slots=[],effects=[],applied=[];let cursor=0,dirty=true,tree,closed=0,props={...initialProps},apply=async()=>true
  globalThis.__entityHooks={
    useState(initial){const i=cursor++;slots[i]??={value:typeof initial==='function'?initial():initial};return[slots[i].value,next=>{const value=typeof next==='function'?next(slots[i].value):next;if(value!==slots[i].value){slots[i].value=value;dirty=true}}]},
    useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||deps.some((v,j)=>!Object.is(v,old.deps[j]))){slots[i]={deps,cleanup:old?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn()})}},
  }
  const nodes=(value=tree)=>React.isValidElement(value)?[value,...React.Children.toArray(value.props.children).flatMap(nodes)]:[]
  const text=value=>React.isValidElement(value)?React.Children.toArray(value.props.children).map(text).join(''):String(value??'')
  const render=()=>{let guard=0;do{cursor=0;dirty=false;tree=Component({...props,onApply:async value=>{applied.push(structuredClone(value));return await apply(value)},onClose:()=>closed++,onResetPicks:()=>{props={...props,picks:[]};dirty=true}});while(effects.length)effects.shift()();assert.ok(++guard<30,'panel render loop')}while(dirty)}
  const find=predicate=>{const found=nodes().find(predicate);assert.ok(found,'panel control exists');return found}
  const event=async(node,key,value)=>{await node.props[key](value);render()}
  const input=async(label,value)=>event(find(n=>n.props['aria-label']===label),'onChange',{target:{value}})
  const field=async(label,value)=>event(find(n=>n.props.label===label),'onChange',value)
  const click=async label=>event(find(n=>n.type==='button'&&text(n)===label),'onClick')
  render();return{nodes,find,event,input,field,click,applied,closed:()=>closed,text:()=>text(tree),setApply(fn){apply=fn},update(next){props={...props,...next};render()},cleanup(){slots.forEach(s=>s?.cleanup?.())}}
}

test('reference-panel mode changes discard incompatible fields and preserve user parameter expressions in the saved record',async()=>{
  const frame={origin:[2,3,4],xDir:[0,1,0],normal:[1,0,0]},ui=panelHarness(ReferencePanel,{plan:plan({parameters}),kind:'plane',selectedFrame:frame,picks:[]})
  try{
    await ui.input('平面构造方法','three_points');await ui.field('构造点 3',[20,0,0]);await ui.click('完成');assert.equal(ui.applied.length,0);assert.match(ui.text(),/共线/)
    await ui.input('平面构造方法','frame');await ui.click('完成');assert.deepEqual(ui.applied[0].frame,frame);assert.equal(ui.applied[0].points,undefined);assert.equal(ui.applied[0].parent,undefined)
    await ui.input('平面构造方法','angle');await ui.field('平面偏移 (mm)','D / 2');await ui.field('平面角度 (°)','A');ui.setApply(async()=>false);await ui.click('完成')
    const saved=ui.applied.at(-1);assert.equal(saved.offset,'D / 2');assert.equal(saved.angle,'A');assert.equal(saved.frame,undefined);assert.equal(saved.points,undefined);assert.equal(ui.closed(),1)
    close(referenceFrames(plan({parameters,references:[saved]}))[saved.id].origin,[0,0,5])
  }finally{ui.cleanup()}
})

test('helix-panel fields persist radius, pitch, turns, handedness and a user-defined 3D frame together',async()=>{
  const ui=panelHarness(ReferencePanel,{plan:plan({parameters}),kind:'helix',picks:[]})
  try{
    await ui.field('螺旋半径 (mm)','R');await ui.field('螺距 (mm)','P');await ui.field('圈数','N');await ui.field('螺旋原点',[1,2,3]);await ui.field('起始半径方向',[0,1,0]);await ui.field('螺旋轴方向',[1,0,0]);await ui.event(ui.find(n=>n.type==='input'&&n.props.type==='checkbox'),'onChange',{target:{checked:true}});await ui.click('完成')
    assert.equal(ui.applied.length,1);const record=ui.applied[0];assert.equal(record.radius,'R');assert.equal(record.pitch,'P');assert.equal(record.turns,'N');assert.equal(record.lefthand,true)
    const resolved=resolvedConstruction(plan({parameters,references:[record]}))[0];close(resolved.points[16],[2.25,2,-1]);assert.equal(ui.closed(),1)
  }finally{ui.cleanup()}
})

test('PMI-panel picks use real world positions, manual coordinates detach geometric anchors and failed saves retain the editable measurement',async()=>{
  const selector={kind:'face',sourceFeatureId:'block',geometryVersion:'a'.repeat(64),index:0,signature:'b'.repeat(64)},ui=panelHarness(PmiPanel,{plan:plan({parameters:{X:{value:3}}}),picks:[]})
  try{
    await ui.click('完成标注');assert.equal(ui.applied.length,0);assert.match(ui.text(),/选择 2 个/)
    ui.update({picks:[{worldPoint:[0,0,0],selector},{worldPoint:[3,4,12],selector}]});assert.match(ui.text(),/13/)
    await ui.field('标注点 2',['X',4,0]);await ui.input('PMI 标注文字','检验尺寸');ui.setApply(async()=>{throw new Error('QA 保存冲突，请重试')});await ui.click('完成标注')
    assert.equal(ui.closed(),0);assert.match(ui.text(),/保存冲突/);assert.equal(ui.applied[0].points[1][0],'X');assert.deepEqual(ui.applied[0].references,[]);assert.equal(ui.applied[0].anchors,undefined);assert.equal(pmiLabel(ui.applied[0],{X:{value:3}}),'5 检验尺寸')
    ui.setApply(async()=>true);await ui.click('完成标注');assert.deepEqual(ui.applied[1],ui.applied[0]);assert.equal(ui.closed(),1)
    await ui.click('基准');await ui.input('基准代号','A');assert.match(ui.text(),/\[A\]/);await ui.click('完成标注');assert.equal(ui.applied.at(-1).kind,'datum');assert.deepEqual(ui.applied.at(-1).points,[])
  }finally{ui.cleanup()}
})

test('a saved independent sketch with a new custom frame reopens through the model bridge with its exact plan',()=>{
  const sketch={...square,plane:'custom',frame:{origin:[1,2,3],xDir:[0,1,0],normal:[1,0,0]}};delete sketch.origin
  const stored=storeIndependentSketch(plan(),sketch,'sketch1').plan,feature=featureFromIndependentSketch(stored,'sketch1','profile_extrude')
  feature.distance='H';stored.parameters={H:{value:12}};stored.features=[feature];stored.result=feature.id
  const saved=JSON.parse(JSON.stringify(stored)),entry=describeModelEditing({accountKey:'qa-account',fileId:'qa-file',model:{kind:'feature_model',cadPlan:saved}})
  assert.equal(entry.editable,true,entry.reason);assert.deepEqual(entry.initialDraft.plan,saved);assert.equal(Object.hasOwn(entry.initialDraft.plan.features[0],'origin'),false)
  assert.deepEqual(entry.initialDraft.plan.features[0].frame,sketch.frame)
})

test('backend rejects deleting construction references still used by a sketch, rotation axis or exact helix path',{skip:!existsSync(resolve('apps/api/.venv/bin/python'))},()=>{
  const script=`from copy import deepcopy\nfrom app.cad_plan import validate_plan,PlanValidationError\nbase={'version':'cad-plan-v1','units':'mm','features':[{'id':'box','op':'box','size':[10,8,6]}],'result':'box'}\ncases=[]\np=deepcopy(base);p['features'].append({'id':'rot','op':'rotate','input':'box','axisStart':[0,0,0],'axisEnd':[0,0,1],'angle':90,'axisReference':'deletedAxis'});p['result']='rot';cases.append(p)\np=deepcopy(base);p['sketches']=[{'id':'sk','plane':'XY','planeReference':'deletedPlane','start':[0,0],'segments':[{'type':'line','to':[1,0]},{'type':'line','to':[0,1]},{'type':'line','to':[0,0]}]}];cases.append(p)\np=deepcopy(base);p['features'].append({'id':'sweep','op':'profile_sweep','plane':'XY','start':[0,0],'segments':[{'type':'line','to':[1,0]},{'type':'line','to':[0,1]},{'type':'line','to':[0,0]}],'path':{'curveReference':'deletedHelix'}});p['result']='sweep';cases.append(p)\nfor p in cases:\n try: validate_plan(p)\n except PlanValidationError as e: assert '引用' in str(e) or '不存在' in str(e),str(e)\n else: raise AssertionError('dangling construction reference was accepted')\nprint(len(cases))`
  const result=spawnSync(resolve('apps/api/.venv/bin/python'),['-c',script],{cwd:resolve('apps/api'),encoding:'utf8',timeout:10000})
  assert.equal(result.status,0,result.stderr);assert.equal(result.stdout.trim(),'3')
})
