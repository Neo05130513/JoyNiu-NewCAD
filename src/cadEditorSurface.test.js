import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { emptyCadDraft } from './cadEditorTransactions.js'
import { replaceSketchShape } from './featureSketchModel.js'

const fixture=JSON.parse(await readFile(new URL('./fixtures/cadViewportBoxBore.json',import.meta.url),'utf8'))
const clone=value=>structuredClone(value)
const draft=()=>({name:'实体',plan:{...clone(fixture.provenance.plan),parameters:{width:{value:20}}},suppressed:[],sourceRun:null,changeNote:'初始设计'})

function harness(Component,initialProps){
  const slots=[],effects=[],events=new Map();let cursor=0,dirty=true,tree,props=initialProps
  const previous=globalThis.window
  globalThis.window={addEventListener(name,fn){if(!events.has(name))events.set(name,new Set());events.get(name).add(fn)},removeEventListener(name,fn){events.get(name)?.delete(fn)}}
  globalThis.__surfaceHooks={
    useState(initial){const i=cursor++;if(!slots[i])slots[i]={value:typeof initial==='function'?initial():initial};return[slots[i].value,value=>{const next=typeof value==='function'?value(slots[i].value):value;if(!Object.is(next,slots[i].value)){slots[i].value=next;dirty=true}}]},
    useRef(initial){const i=cursor++;if(!slots[i])slots[i]={current:initial};return slots[i]},
    useMemo(fn,deps){const i=cursor++,old=slots[i];if(!old||deps.some((v,j)=>!Object.is(v,old.deps[j])))slots[i]={deps,value:fn()};return slots[i].value},
    useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||!deps||deps.some((v,j)=>!Object.is(v,old.deps?.[j]))){slots[i]={deps,cleanup:old?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn()})}},
  }
  const render=()=>{let n=0;do{dirty=false;cursor=0;tree=Component(props);while(effects.length)effects.shift()();assert.ok(++n<30,'component settles')}while(dirty);return tree}
  const flush=async()=>{for(let i=0;i<5;i++){await new Promise(resolve=>setTimeout(resolve,1));render()}}
  const nodes=(value=tree)=>{
    if(!React.isValidElement(value))return []
    // Tool/Panel/Icon are pure JSX components. Heavy viewer/form children are
    // explicit props boundaries; all transaction handlers are the real ones.
    const child=typeof value.type==='function'&&!value.type.testBoundary?value.type(value.props):value.props.children
    return[value,...React.Children.toArray(child).flatMap(nodes)]
  }
  const text=value=>React.isValidElement(value)?React.Children.toArray(value.props.children).map(text).join(''):String(value??'')
  const find=fn=>{const item=nodes().find(fn);assert.ok(item,'matching UI control exists');return item}
  const button=name=>find(node=>node.type==='button'&&text(node)===name)
  const boundary=name=>find(node=>node.type?.testBoundary===name)
  const event=async(node,event,...args)=>{const result=await node.props[event](...args);render();await flush();return result}
  const update=async next=>{props={...props,...next};render();await flush()}
  const unmount=()=>{slots.forEach(slot=>slot?.cleanup?.());if(previous===undefined)delete globalThis.window;else globalThis.window=previous}
  render();return{render,flush,nodes,find,button,boundary,event,update,events,result:()=>tree,text:()=>nodes().filter(node=>typeof node.type==='string').map(text).join(' '),unmount}
}

let directory,Surface,PreviewHook
async function load(){
  if(Surface)return
  directory=await mkdtemp(resolve('node_modules/.cad-surface-tests-'))
  const bundle=await rolldown({input:{Surface:resolve('src/CadEditorSurface.jsx'),Preview:resolve('src/useCadPreview.js')},external:['react/jsx-runtime'],transform:{jsx:{runtime:'automatic'}},plugins:[{
    name:'cad-surface-harness',resolveId(source,importer){if(source==='react')return '\0surface-hooks';if(source==='./useCadPreview.js'&&importer?.endsWith('/CadEditorSurface.jsx'))return '\0preview-stub'},
    load(id){
      if(id==='\0surface-hooks')return{code:'export const useState=(...a)=>globalThis.__surfaceHooks.useState(...a);export const useRef=(...a)=>globalThis.__surfaceHooks.useRef(...a);export const useEffect=(...a)=>globalThis.__surfaceHooks.useEffect(...a);export const useMemo=(...a)=>globalThis.__surfaceHooks.useMemo(...a);',moduleType:'js'}
      if(id==='\0preview-stub')return{code:'export default function useCadPreview(props){return globalThis.__surfacePreview(props)}',moduleType:'js'}
      if(id.endsWith('.css'))return{code:'',moduleType:'js'}
      if(id.endsWith('/directFeatureClient.js'))return{code:'export const directFeatureClient={preview:(...args)=>globalThis.__previewRequest(...args)}',moduleType:'js'}
      if(id.endsWith('/CadModelViewport.jsx'))return{code:'function Viewport(){return null};Viewport.testBoundary="viewport";export default Viewport',moduleType:'js'}
      for(const [file,name]of [['CadReferencePanel','reference'],['CadPmiPanel','pmi'],['CadPdmPanel','pdm'],['CadCommunityPanel','community'],['CadStandardPartPanel','standard'],['CadImportBodyPanel','import']])if(id.endsWith(`/${file}.jsx`))return{code:`function Panel(){return null};Panel.testBoundary="${name}";export default Panel`,moduleType:'js'}
      if(id.endsWith('/FeatureSketchEditor.jsx'))return{code:'function Sketch(){return null};Sketch.testBoundary="sketch";export default Sketch',moduleType:'js'}
      if(id.endsWith('/CadFeatureFields.jsx'))return{code:'export function FeatureEditor(){return null};FeatureEditor.testBoundary="feature";export function ParameterEditor(){return null};ParameterEditor.testBoundary="parameters"',moduleType:'js'}
    },
  }]})
  await bundle.write({dir:directory,format:'esm'});await bundle.close()
  Surface=(await import(pathToFileURL(resolve(directory,'Surface.js')).href)).default
  PreviewHook=(await import(pathToFileURL(resolve(directory,'Preview.js')).href)).default
}
test.after(async()=>{if(directory)await rm(directory,{recursive:true,force:true})})
test('community entry publishes only saved record and passes explicit resource opening through the parent guard',async()=>{
  await load();const opened=[],ui=harness(Surface,defaults({dirty:true,onOpenCommunity:value=>opened.push(value)}))
  try{
    await ui.event(ui.button('AI 资源社区'),'onClick')
    assert.equal(ui.boundary('community').props.record,null)
    await ui.event(ui.boundary('community'),'onClose')
    await ui.update({dirty:false})
    await ui.event(ui.button('AI 资源社区'),'onClick')
    assert.equal(ui.boundary('community').props.record.id,'feature_saved')
    const entry={resourceId:'resource_'+ 'a'.repeat(32),name:'公开模型'}
    await ui.event(ui.boundary('community'),'onOpen',entry)
    assert.deepEqual(opened,[entry]);assert.equal(ui.nodes().some(n=>n.type?.testBoundary==='community'),false)
  }finally{ui.unmount()}
})
function defaults(extra={}){
  globalThis.__surfacePreview=props=>({geometry:props.featureId==='block'?fixture.input:fixture.result,current:true,loading:false,error:'',retry(){}})
  return{draft:draft(),record:{id:'feature_saved',revision:1,status:'built'},token:'token',scope:'account:file',active:true,busy:'',error:'',unavailable:false,dirty:false,selected:'drilled',onSelected(){},onEdit(){assert.fail('transaction must not write the parent draft')},onComplete:async()=>null,onSave(){},onUndo(){},onRedo(){},onBack(){},...extra}
}
const featureRow=(ui,id)=>ui.find(node=>node.type==='button'&&node.props.title==='双击编辑特征'&&ui.nodes(node).some(child=>child.type==='span'&&React.Children.toArray(child.props.children).join('').includes(id)))
const clickTool=(ui,label)=>ui.event(ui.button(label),'onClick',{currentTarget:{closest:()=>({open:true})}})

test('disabled ribbon tools close their menus and cannot enter an operation through mouse or keyboard disclosure',async()=>{
  await load();const calls=[],ui=harness(Surface,defaults({onSelected:id=>calls.push(id)}))
  try{
    const summary=()=>ui.find(node=>node.type==='summary'&&node.props['aria-label']==='拉伸更多工具')
    assert.equal(summary().props['aria-disabled'],false);assert.equal(summary().props.tabIndex,0)
    // Opening an operation disables the ribbon while retaining its real menu.
    await clickTool(ui,'圆角')
    assert.equal(ui.button('拉伸').props.disabled,true)
    assert.equal(summary().props['aria-disabled'],true);assert.equal(summary().props.tabIndex,-1)
    for(const menu of ui.nodes().filter(node=>node.type==='details'&&node.props.className==='ce-tool-menu'))assert.equal(menu.props.open,false)
    let prevented=0
    await ui.event(summary(),'onClick',{preventDefault(){prevented++}})
    assert.equal(ui.button('旋转').props.disabled,true)
    await ui.event(ui.button('旋转'),'onClick',{preventDefault(){prevented++}})
    assert.equal(prevented,2);assert.equal(calls.length,1);assert.equal(ui.boundary('feature').props.feature.op,'fillet')
    await ui.event(ui.button('取消'),'onClick')
    assert.equal(summary().props['aria-disabled'],false);assert.equal(ui.button('旋转').props.disabled,false)
    await ui.update({busy:'正在保存'})
    assert.equal(summary().props['aria-disabled'],true);assert.equal(ui.button('旋转').props.disabled,true)
  }finally{ui.unmount()}
})

test('direct extrusion and revolve start empty even when the controller automatically selected a feature; empty completion never submits',async()=>{
  await load()
  for(const source of [emptyCadDraft(),draft()])for(const [label,op]of [['拉伸','profile_extrude'],['旋转','profile_revolve']]){
    const calls=[],initial=defaults({draft:source,onComplete:async value=>{calls.push(value);return value}}),before=clone(source),ui=harness(Surface,initial)
    try{
      await clickTool(ui,label)
      assert.equal(ui.boundary('sketch').props.feature.op,op);assert.deepEqual(ui.boundary('sketch').props.feature.segments,[])
      assert.match(ui.text(),/为空.*绘制闭合轮廓/)
      await ui.event(ui.button('完成草图'),'onClick')
      assert.ok(ui.boundary('sketch'));assert.equal(calls.length,0)
      const created=replaceSketchShape(ui.boundary('sketch').props.feature,'rectangle',{x:2,y:0,width:3,height:4})
      await ui.event(ui.boundary('sketch'),'onChange',created)
      assert.doesNotMatch(ui.text(),/请先绘制闭合轮廓，再完成草图。/)
      await ui.event(ui.button('完成草图'),'onClick')
      assert.deepEqual(ui.boundary('feature').props.feature.segments,created.segments)
      // A late empty edit cannot bypass the final completion guard either.
      await ui.event(ui.boundary('feature'),'onChange',{...created,segments:[]})
      await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,0)
      await ui.event(ui.boundary('feature'),'onChange',created)
      await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,1)
      assert.deepEqual(calls[0].plan.features.find(f=>f.id===created.id).segments,created.segments)
      assert.deepEqual(source,before)
    }finally{ui.unmount()}
  }
})

test('explicit tree sketch selection reuses its real contour and operation settings while another document starts unselected',async()=>{
  await load();const initial=defaults(),before=clone(initial.draft),ui=harness(Surface,initial)
  try{
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.className==='ce-expand'),'onClick')
    const sketchNode=ui.find(node=>node.type==='button'&&node.props.title==='双击编辑草图')
    await ui.event(sketchNode,'onClick');assert.match(ui.text(),/点击拉伸或旋转修改其所属特征/)
    await clickTool(ui,'拉伸')
    const feature=ui.boundary('feature').props.feature
    assert.equal(feature.id,'block');assert.equal(feature.distance,10);assert.equal(feature.sketchConstraints.length,5)
    assert.match(ui.text(),/修改现有特征/);assert.equal(ui.nodes().some(node=>node.props['aria-label']==='特征作用'),false)
    const changed={...feature,distance:'width / 2'}
    await ui.event(ui.boundary('feature'),'onChange',changed)
    await ui.event(ui.boundary('feature'),'onSketch')
    assert.equal(ui.boundary('sketch').props.feature.distance,'width / 2')
    await ui.event(ui.button('完成草图'),'onClick');assert.equal(ui.boundary('feature').props.feature.distance,'width / 2')
    await ui.event(ui.button('取消'),'onClick');assert.deepEqual(initial.draft,before)
    await clickTool(ui,'旋转')
    assert.equal(ui.boundary('feature').props.feature.id,'block');assert.equal(ui.boundary('feature').props.feature.op,'profile_revolve')
    assert.deepEqual(ui.boundary('feature').props.feature.segments,feature.segments)
    await ui.event(ui.button('取消'),'onClick')
    await ui.update({scope:'another-file'})
    await clickTool(ui,'拉伸');assert.deepEqual(ui.boundary('sketch').props.feature.segments,[])
    assert.notEqual(ui.boundary('sketch').props.feature.id,'block')
  }finally{ui.unmount()}
})

test('direct face extrusion retains the actual selected frame and rejects a curved face instead of inventing a plane',async()=>{
  await load();const ui=harness(Surface,defaults())
  try{
    const face=fixture.result.faces[2],hit={kind:'face',id:face.id,selector:face.selector,worldPoint:[2,3,10]}
    await ui.event(ui.boundary('viewport'),'onSelect',[hit],hit)
    await clickTool(ui,'拉伸')
    const feature=ui.boundary('sketch').props.feature
    assert.equal(feature.plane,'custom');assert.deepEqual(feature.frame.origin,face.origin);assert.deepEqual(feature.planeSource,face.selector)
    assert.equal(Object.hasOwn(feature,'origin'),false);assert.deepEqual(feature.segments,[])
    await ui.event(ui.button('取消'),'onClick')
    const curved=fixture.result.faces.find(item=>!item.planar),curveHit={kind:'face',id:curved.id,selector:curved.selector}
    await ui.event(ui.boundary('viewport'),'onSelect',[curveHit],curveHit);await clickTool(ui,'旋转')
    assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='sketch'),false);assert.match(ui.text(),/请选择模型平面或基准面/)
  }finally{ui.unmount()}
})

test('sweep section finishes before the user defines a real path, and neither empty stage can commit',async()=>{
  await load();const calls=[],previews=[],ui=harness(Surface,defaults({draft:emptyCadDraft(),record:null,onComplete:async value=>{calls.push(value);return value}}))
  globalThis.__surfacePreview=props=>{previews.push(props);return{geometry:null,current:false,loading:false,error:'',retry(){}}}
  try{
    await clickTool(ui,'扫掠');assert.equal(ui.boundary('sketch').props.feature.op,'profile_sweep')
    await ui.event(ui.button('完成草图'),'onClick');assert.equal(calls.length,0);assert.ok(ui.boundary('sketch'))
    const profile=replaceSketchShape(ui.boundary('sketch').props.feature,'circle',{x:0,y:0,radius:2})
    await ui.event(ui.boundary('sketch'),'onChange',profile);await ui.event(ui.button('完成草图'),'onClick')
    assert.equal(ui.boundary('feature').props.feature.op,'profile_sweep');assert.deepEqual(ui.boundary('feature').props.feature.segments,profile.segments)
    await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,0);assert.match(ui.text(),/扫掠路径/)
    assert.equal(previews.filter(p=>p.delay===600).at(-1).enabled,false)
    const ready={...ui.boundary('feature').props.feature,path:{start:[0,0,0],segments:[{type:'line',to:[0,0,20]}]}}
    await ui.event(ui.boundary('feature'),'onChange',ready);assert.equal(previews.filter(p=>p.delay===600).at(-1).enabled,true)
    await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.features[0],ready)
  }finally{ui.unmount()}
})

test('new body retains both inputs in a compound and the solid tree shows real read-only entries',async()=>{
  await load();const calls=[],initial=defaults({onComplete:async value=>{calls.push(value);return value}}),ui=harness(Surface,initial)
  try{
    globalThis.__surfacePreview=()=>({geometry:{...fixture.result,inspection:{solidCount:2}},current:true,loading:false,error:'',retry(){}})
    await clickTool(ui,'长方体')
    await ui.event(ui.button('新建实体'),'onClick')
    const body=ui.boundary('feature').props.feature
    await ui.event(ui.button('完成'),'onClick')
    assert.equal(calls[0].plan.features.at(-1).op,'compound')
    assert.deepEqual(calls[0].plan.features.at(-1).inputs,[initial.draft.plan.result,body.id])
    const entries=ui.nodes().filter(node=>node.props.role==='listitem'&&node.props['aria-label']?.startsWith('实体 '))
    assert.equal(entries.length,2);assert.ok(entries.every(node=>node.type==='div'&&!node.props.onClick))
  }finally{ui.unmount()}
})

test('double-click enters an isolated feature transaction; cancel never writes its parameter changes to the parent',async()=>{
  await load();const initial=defaults(),before=clone(initial.draft),completions=[];initial.onComplete=async value=>{completions.push(value);return value}
  const ui=harness(Surface,initial)
  try{
    const row=ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征')
    await ui.event(row,'onDoubleClick')
    const editor=ui.boundary('feature');assert.equal(editor.props.feature.id,'block')
    await ui.event(editor,'onChange',{...editor.props.feature,size:[35,16,10]})
    assert.deepEqual(initial.draft,before);assert.equal(ui.boundary('feature').props.feature.size[0],35)
    await ui.event(ui.button('取消'),'onClick');assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='feature'),false)
    assert.deepEqual(initial.draft,before);assert.deepEqual(completions,[])
  }finally{ui.unmount()}
})

test('complete is atomic, ignores double clicks, and retains its transaction after a failed commit',async()=>{
  await load();let finish;const calls=[],initial=defaults({onComplete:next=>{calls.push(clone(next));return new Promise(resolve=>{finish=resolve})}}),ui=harness(Surface,initial)
  try{
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
    const editor=ui.boundary('feature');await ui.event(editor,'onChange',{...editor.props.feature,size:[30,16,10]})
    const complete=ui.button('完成').props.onClick,pending=complete();const duplicate=complete();ui.render();await duplicate
    await ui.flush();assert.equal(calls.length,1);assert.equal(ui.button('正在完成…').props.disabled,true)
    finish(null);await pending;await ui.flush();assert.equal(ui.boundary('feature').props.feature.size[0],30)
    assert.equal(initial.draft.plan.features[0].size[0],20)
    const retry=ui.button('完成').props.onClick();await ui.flush();finish({id:'built'});await retry;await ui.flush()
    assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='feature'),false)
  }finally{ui.unmount()}
})

test('completion drains sent previews; cancel, document change, inactive page and logout revoke queued submission',async()=>{
  await load()
  for(const action of ['complete','cancel','scope','inactive','logout']){
    let queue,release
    const calls=[],ui=harness(Surface,defaults({onComplete:async next=>{calls.push(next);return{id:'built'}}}))
    globalThis.__surfacePreview=props=>{queue=props.queue;return{geometry:fixture.result,current:true,loading:!props.paused,error:'',retry(){}}}
    ui.render();await ui.flush()
    const sent=queue.preview(()=>new Promise(resolve=>{release=resolve}));await ui.flush()
    let pending
    try{
      await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
      const click=ui.button('完成（等待预览）').props.onClick
      pending=click();ui.render();await ui.flush();await click()
      assert.equal(calls.length,0,action)
      assert.equal(ui.button('等待预览完成…').props.disabled,true)
      assert.equal(ui.button('取消').props.disabled,false)
      assert.match(ui.text(),/正在预览/)
      if(action==='cancel')await ui.event(ui.button('取消'),'onClick')
      if(action==='scope')await ui.update({scope:'account:another-file',draft:draft()})
      if(action==='inactive')await ui.update({active:false})
      if(action==='logout')await ui.update({token:null})
      release(fixture.result);await sent;await pending;await ui.flush()
      assert.equal(calls.length,action==='complete'?1:0,action)
      if(action==='inactive')assert.ok(ui.boundary('feature'),'hidden transaction draft remains editable on return')
    }finally{release?.();await sent;await pending;ui.unmount()}
  }
})

test('preview errors leave completion available and failed final validation preserves the draft',async()=>{
  await load();const calls=[],ui=harness(Surface,defaults({onComplete:async next=>{calls.push(next);throw new Error('圆角半径必须小于实体厚度')}}))
  globalThis.__surfacePreview=()=>({geometry:fixture.result,current:true,loading:false,error:'预览失败',retry(){}})
  try{
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
    assert.equal(ui.button('完成').props.disabled,false)
    await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,1)
    assert.ok(ui.boundary('feature'));assert.match(ui.text(),/圆角半径必须/)
    assert.equal(ui.button('取消').props.disabled,false);assert.equal(ui.button('完成').props.disabled,false)
  }finally{ui.unmount()}
})

test('editing saved edge selections restores highlights; removing one reference preserves every other selection',async()=>{
  await load();const initial=defaults()
  initial.draft.plan.features.push({id:'rounded',op:'fillet',label:'已保存圆角',input:'block',radius:1,edges:fixture.input.edges.slice(0,2).map(edge=>clone(edge.selector))});initial.draft.plan.result='rounded'
  const before=clone(initial.draft),ui=harness(Surface,initial)
  try{
    await ui.event(featureRow(ui,'已保存圆角'),'onDoubleClick')
    assert.equal(ui.boundary('viewport').props.selection.length,2)
    // Clearing canvas selection must not make removing one list row erase all refs.
    await ui.event(ui.find(node=>node.type==='button'&&node.props.className==='ce-tree-row ce-origin'),'onClick')
    await ui.event(ui.find(node=>node.props['aria-label']==='移除选择 1'),'onClick')
    assert.deepEqual(ui.boundary('feature').props.feature.edges,[fixture.input.edges[1].selector])
    const wrong={kind:'edge',id:'edge:0',selector:fixture.result.edges[0].selector}
    await ui.event(ui.boundary('viewport'),'onSelect',[wrong],wrong)
    assert.deepEqual(ui.boundary('feature').props.feature.edges,[],'stale or other-input references must not enter the transaction')
    assert.deepEqual(initial.draft,before)
  }finally{ui.unmount()}
})

test('new sketch starts empty on the actual selected plane and cancel keeps the original model untouched',async()=>{
  await load();const initial=defaults(),before=clone(initial.draft),ui=harness(Surface,initial)
  try{
    await ui.event(ui.button('新建草图'),'onClick')
    const face=fixture.result.faces[2],hit={kind:'face',id:face.id,selector:face.selector,worldPoint:[2,3,10]}
    await ui.event(ui.boundary('viewport'),'onSelect',[hit],hit)
    const sketch=ui.boundary('sketch')
    assert.equal(sketch.props.feature.plane,'custom');assert.deepEqual(sketch.props.feature.segments,[])
    assert.deepEqual(sketch.props.feature.planeSource,face.selector)
    assert.deepEqual(sketch.props.feature.frame.origin,face.origin)
    assert.equal(Object.hasOwn(sketch.props.feature,'origin'),false)
    await ui.event(ui.button('取消'),'onClick');assert.deepEqual(initial.draft,before)
  }finally{ui.unmount()}
})

test('formula transactions block parent undo and are removed with deferred navigation when document scope changes',async()=>{
  await load();let undo=0,back=0
  const initial=defaults({onUndo:()=>{undo++},onBack:()=>{back++}}),ui=harness(Surface,initial)
  try{
    await ui.event(ui.find(node=>node.type==='button'&&React.Children.toArray(node.props.children).some(value=>typeof value==='string'&&value.includes('方程式'))),'onClick')
    await ui.event(ui.boundary('parameters'),'onChange',{width:{value:99}})
    for(const handler of ui.events.get('keydown')||[])handler({key:'z',ctrlKey:true,target:{tagName:'BUTTON'},preventDefault(){}})
    assert.equal(undo,0)
    await ui.event(ui.button('〈 返回云管理界面'),'onClick');assert.equal(back,0)
    assert.ok(ui.find(node=>node.props['aria-label']==='结束当前操作'))
    await ui.update({scope:'other-account:other-file',draft:draft()})
    assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='parameters'),false)
    assert.equal(ui.nodes().some(node=>node.props['aria-label']==='结束当前操作'),false)
    assert.equal(back,0)
  }finally{ui.unmount()}
})

test('a completed old-scope transaction cannot close the new document transaction',async()=>{
  await load();let finish
  const ui=harness(Surface,defaults({onComplete:()=>new Promise(resolve=>{finish=resolve})}))
  try{
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
    const pending=ui.button('完成').props.onClick();ui.render();await ui.flush()
    await ui.update({scope:'another-document',draft:draft()})
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
    finish({id:'old-result'});await pending;await ui.flush()
    assert.equal(ui.boundary('feature').props.feature.id,'block')
    assert.equal(ui.button('完成').props.disabled,false)
  }finally{ui.unmount()}
})

test('unfinished formulas cannot be hidden by auxiliary panels, saved as the old model, or closed during completion',async()=>{
  await load();let finish, saves=0
  const ui=harness(Surface,defaults({dirty:true,onSave:()=>{saves++},onComplete:()=>new Promise(resolve=>{finish=resolve})}))
  try{
    await ui.event(ui.find(node=>node.type==='button'&&node.props.className==='ce-tree-section'&&React.Children.toArray(node.props.children).join('').includes('方程式')),'onClick')
    await ui.event(ui.boundary('parameters'),'onChange',{width:{value:36}})
    for(const target of [()=>ui.button('⚙ 设置'),()=>ui.button('▱ 全部'),()=>ui.find(node=>node.props.className==='ce-active-document')]){
      await ui.event(target(),'onClick');assert.deepEqual(ui.boundary('parameters').props.parameters,{width:{value:36}})
    }
    assert.equal(ui.button('保存').props.disabled,true);assert.equal(saves,0)
    const pending=ui.button('完成').props.onClick();ui.render();await ui.flush()
    await ui.event(ui.find(node=>node.props['aria-label']==='关闭方程式'),'onClick')
    assert.deepEqual(ui.boundary('parameters').props.parameters,{width:{value:36}})
    finish(null);await pending;await ui.flush()
    await ui.event(ui.button('取消'),'onClick');assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='parameters'),false)
  }finally{ui.unmount()}
})

test('preview hook hides old geometry and drains obsolete sent requests before the next document preview',async()=>{
  await load();const calls=[]
  globalThis.__previewRequest=(token,payload,signal)=>new Promise(resolve=>calls.push({token,payload,signal,resolve}))
  const initial={draft:draft(),token:'before',scope:'scope-a',delay:0},ui=harness(PreviewHook,initial)
  try{
    await ui.flush();assert.equal(calls.length,1);calls[0].resolve(fixture.result);await ui.flush()
    assert.equal(ui.result().current,true)
    await ui.update({featureId:'block'});assert.equal(calls.length,2)
    assert.equal(ui.result().geometry,null);assert.equal(ui.result().current,false)
    await ui.update({scope:'scope-b',token:'after'});assert.equal(calls[1].signal,undefined);assert.equal(calls.length,2)
    calls[1].resolve(fixture.input);await ui.flush();assert.equal(ui.result().geometry,null)
    assert.equal(calls.length,3);assert.equal(calls[2].token(),'after')
    calls[2].resolve(fixture.input);await ui.flush();assert.equal(ui.result().geometry,fixture.input)
    await ui.update({enabled:false});assert.equal(ui.result().geometry,null);assert.equal(ui.result().current,false);assert.equal(ui.result().loading,false)
  }finally{ui.unmount()}
})

test('base and result hooks share admission; pausing cancels queued work and retains already settled geometry',async()=>{
  await load();const calls=[]
  globalThis.__previewRequest=(token,payload,signal)=>new Promise(resolve=>calls.push({token,payload,signal,resolve}))
  const Pair=props=>({base:PreviewHook({...props,featureId:'block'}),result:PreviewHook(props)})
  const ui=harness(Pair,{draft:draft(),token:'account-token',scope:'shared-document',delay:0})
  try{
    await ui.flush();assert.equal(calls.length,1);assert.equal(ui.result().base.loading,true);assert.equal(ui.result().result.loading,true)
    calls[0].resolve(fixture.input);await ui.flush();assert.equal(calls.length,2)
    assert.equal(ui.result().base.geometry,fixture.input)
    await ui.update({paused:true});assert.equal(ui.result().base.geometry,fixture.input)
    assert.equal(ui.result().base.loading,false);assert.equal(ui.result().result.loading,false)
    calls[1].resolve(fixture.result);await ui.flush();assert.equal(ui.result().result.geometry,null,'obsolete result is not restored while submission is waiting')
    await ui.update({paused:false});assert.equal(calls.length,3);calls[2].resolve(fixture.result);await ui.flush()
    assert.equal(ui.result().result.geometry,fixture.result)
    await ui.update({draft:{...draft(),plan:{...draft().plan,name:'revised'}}});assert.equal(calls.length,4)
    await ui.update({scope:'new-account:new-file',token:'new-token',enabled:false});assert.equal(ui.result().base.loading,false);assert.equal(ui.result().result.loading,false)
    calls[3].resolve(fixture.input);await ui.flush();assert.equal(calls.length,4,'queued obsolete result never sends')
    assert.equal(ui.result().base.geometry,null);assert.equal(ui.result().result.geometry,null)
  }finally{for(const call of calls)call.resolve(fixture.result);ui.unmount()}
})

test('preview retry bypasses the local cache and same-account token renewal keeps the current geometry',async()=>{
  await load();const calls=[]
  globalThis.__previewRequest=(token,payload,signal)=>new Promise((resolve,reject)=>calls.push({token,payload,signal,resolve,reject}))
  const ui=harness(PreviewHook,{draft:draft(),token:'old-token',scope:'one-document',delay:0})
  try{
    await ui.flush();calls[0].resolve(fixture.result);await ui.flush()
    await ui.update({token:'new-token'});assert.equal(calls.length,1);assert.equal(ui.result().geometry,fixture.result)
    ui.result().retry();ui.render();await ui.flush();assert.equal(calls.length,2);assert.equal(calls[1].token(),'new-token')
    calls[1].reject(new Error('本轮预览失败'));await ui.flush()
    assert.equal(ui.result().geometry,null);assert.equal(ui.result().current,false);assert.match(ui.result().error,/预览失败/)
    await ui.update({active:false});assert.equal(ui.result().current,false);assert.equal(ui.result().loading,false)
  }finally{ui.unmount()}
})

test('real project tabs defer navigation until the active feature operation is cancelled explicitly',async()=>{
  await load();const opened=[],documents=[{accountKey:'account',projectId:'p',fileId:'a',name:'轴',status:'built'},{accountKey:'account',projectId:'p',fileId:'b',name:'新零件',status:'empty'}]
  const ui=harness(Surface,defaults({sourceProjectId:'p',sourceFileId:'a',documents,onOpenDocument:value=>opened.push(value)}))
  try{
    const tabs=()=>ui.nodes().filter(node=>node.type==='button'&&node.props['aria-label']?.startsWith('打开图档 '))
    assert.equal(tabs().length,2);assert.equal(tabs()[0].props['aria-current'],'page')
    assert.equal(ui.find(node=>node.props['aria-label']==='项目图档').props.style.overflowX,'auto')
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
    assert.deepEqual(ui.boundary('feature').props.parameters,{width:{value:20}})
    await ui.event(tabs()[1],'onClick');assert.deepEqual(opened,[])
    await ui.event(ui.button('继续编辑'),'onClick');assert.ok(ui.boundary('feature'))
    await ui.event(tabs()[1],'onClick');await ui.event(ui.button('取消操作并离开'),'onClick')
    assert.deepEqual(opened,[documents[1]]);assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='feature'),false)
    await ui.update({busy:'正在完成并保存…'})
    assert.equal(tabs()[1].props.disabled,true)
  }finally{ui.unmount()}
})

test('completed sketch can become a revolve or extrusion without replacing its contour, parameters, or parent draft',async()=>{
  await load();const completed=[],initial=defaults({onComplete:async value=>{completed.push(value);return{id:'saved'}}}),before=clone(initial.draft)
  const ui=harness(Surface,initial)
  try{
    await clickTool(ui,'拉伸')
    const sketch=ui.boundary('sketch'),feature={...sketch.props.feature,start:[2,0],segments:[{type:'line',to:[5,0]},{type:'line',to:[5,4]},{type:'line',to:[2,4]}],distance:'width / 2'}
    const parameters={...sketch.props.parameters,depth:{value:9}}
    await ui.event(sketch,'onChange',feature,parameters)
    await ui.event(ui.button('完成草图'),'onClick')
    const operation=()=>ui.find(node=>node.props['aria-label']==='草图特征类型')
    await ui.event(operation(),'onChange',{target:{value:'profile_revolve'}})
    assert.equal(ui.boundary('feature').props.feature.id,feature.id)
    assert.deepEqual(ui.boundary('feature').props.feature.segments,feature.segments)
    assert.deepEqual(ui.boundary('feature').props.parameters,parameters)
    assert.equal(Object.hasOwn(ui.boundary('feature').props.feature,'distance'),false)
    await ui.event(operation(),'onChange',{target:{value:'profile_sweep'}})
    assert.equal(operation().props.value,'profile_sweep');assert.match(ui.text(),/扫掠路径/)
    assert.deepEqual(ui.boundary('feature').props.feature.segments,feature.segments)
    await ui.event(operation(),'onChange',{target:{value:'profile_extrude'}})
    assert.equal(ui.boundary('feature').props.feature.distance,'width / 2')
    await ui.event(operation(),'onChange',{target:{value:'profile_revolve'}})
    assert.deepEqual(initial.draft,before);assert.deepEqual(completed,[])
    await ui.event(ui.button('完成'),'onClick')
    assert.equal(completed.length,1)
    const saved=completed[0].plan.features.find(item=>item.id===feature.id)
    assert.equal(saved.op,'profile_revolve');assert.deepEqual(saved.segments,feature.segments)
    assert.equal(completed[0].plan.features.filter(item=>item.id===feature.id).length,1)
  }finally{ui.unmount()}
})

test('entering a sketch keeps the same viewport subtree mounted but hidden and inactive until it completes',async()=>{
  await load();const ui=harness(Surface,defaults())
  const container=()=>ui.find(node=>node.type==='div'&&node.props.children?.type?.testBoundary==='viewport')
  try{
    const original=ui.boundary('viewport'),originalContainer=container()
    await clickTool(ui,'拉伸')
    assert.equal(container().type,originalContainer.type);assert.equal(container().key,originalContainer.key)
    assert.equal(ui.boundary('viewport').type,original.type);assert.equal(ui.boundary('viewport').key,original.key)
    assert.equal(container().props.hidden,true);assert.equal(ui.boundary('viewport').props.active,false)
    assert.equal(ui.boundary('viewport').props.documentScope,original.props.documentScope)
    assert.equal(ui.boundary('sketch').props.active,true)
    await ui.update({active:false});assert.equal(ui.boundary('sketch').props.active,false)
    await ui.update({active:true})
    await ui.event(ui.boundary('sketch'),'onChange',replaceSketchShape(ui.boundary('sketch').props.feature,'rectangle',{x:0,y:0,width:10,height:5}))
    await ui.event(ui.button('完成草图'),'onClick')
    assert.equal(container().props.hidden,false);assert.equal(ui.boundary('viewport').props.active,true)
    assert.equal(ui.boundary('viewport').props.documentScope,original.props.documentScope)
    await ui.update({scope:'another-document'})
    assert.equal(ui.boundary('viewport').props.documentScope,'another-document')
  }finally{ui.unmount()}
})

test('reloading after a revision conflict first confirms cancellation and respects shortcuts consumed by the sketch',async()=>{
  await load();let reloaded=0,saves=0
  const ui=harness(Surface,defaults({error:'版本冲突',onReload:()=>{reloaded++},onSave:()=>{saves++}}))
  try{
    for(const handler of ui.events.get('keydown')||[])handler({key:'s',ctrlKey:true,defaultPrevented:true,preventDefault(){assert.fail('already consumed')},stopPropagation(){}})
    assert.equal(saves,0)
    await ui.event(ui.nodes().find(node=>node.type==='button'&&node.props.title==='双击编辑特征'),'onDoubleClick')
    await ui.event(ui.button('重新读取已保存版本'),'onClick');assert.equal(reloaded,0)
    await ui.event(ui.button('继续编辑'),'onClick');assert.equal(reloaded,0);assert.ok(ui.boundary('feature'))
    await ui.event(ui.button('重新读取已保存版本'),'onClick');await ui.event(ui.button('取消操作并离开'),'onClick')
    assert.equal(reloaded,1);assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='feature'),false)
    await ui.update({busy:'正在完成并保存…'});assert.equal(ui.button('重新读取已保存版本').props.disabled,true)
  }finally{ui.unmount()}
})

test('an empty document saves an independent ellipse sketch without a phantom solid, then reuses that exact sketch in a new feature',async()=>{
  await load();const saved=[],built=[],source=emptyCadDraft(),ui=harness(Surface,defaults({draft:source,record:null,onSaveDraft:async value=>{saved.push(value);return{id:'saved-sketch'}},onComplete:async value=>{built.push(value);return{id:'built'}}}))
  try{
    await clickTool(ui,'新建草图');await ui.event(ui.find(n=>n.type==='button'&&n.props.className==='ce-plane-choice'),'onClick')
    const created=replaceSketchShape(ui.boundary('sketch').props.feature,'ellipse',{x:0,y:0,rx:10,ry:5,rotation:0})
    await ui.event(ui.boundary('sketch'),'onChange',created,{size:{value:10,source:'user'}});await ui.event(ui.button('完成草图'),'onClick')
    assert.equal(saved.length,1);assert.equal(built.length,0);assert.deepEqual(saved[0].plan.features,[]);assert.equal(saved[0].plan.result,'');assert.equal(saved[0].plan.sketches.length,1)
    assert.deepEqual(saved[0].plan.sketches[0].segments,created.segments);assert.equal(saved[0].plan.parameters.size.value,10);assert.deepEqual(source.plan.features,[])
    assert.equal(ui.nodes().some(n=>n.type?.testBoundary==='sketch'),false)
    await ui.update({draft:saved[0],record:{id:'saved-sketch',revision:1,status:'draft'}})
    await ui.event(ui.button(saved[0].plan.sketches[0].label),'onClick');await clickTool(ui,'拉伸')
    const feature=ui.boundary('feature').props.feature;assert.equal(feature.sketchId,saved[0].plan.sketches[0].id);assert.deepEqual(feature.segments,created.segments);assert.equal(feature.op,'profile_extrude')
    await ui.event(ui.button('完成'),'onClick');assert.equal(built.length,1);assert.equal(built[0].plan.features.length,1);assert.equal(built[0].plan.sketches.length,1)
  }finally{ui.unmount()}
})

test('loft section editor uses the latest supplied feature and writes only its chosen section before one atomic finish',async()=>{
  await load();const calls=[],source=emptyCadDraft(),ui=harness(Surface,defaults({draft:source,record:null,onComplete:async value=>{calls.push(value);return value}}))
  const frame=z=>({origin:[0,0,z],xDir:[1,0,0],normal:[0,0,1]})
  try{
    await clickTool(ui,'放样');assert.deepEqual(ui.boundary('feature').props.feature.sections,[])
    for(let i=0;i<2;i++){
      const owner=ui.boundary('feature').props.feature,next={...owner,sections:[...owner.sections,{id:`section${i+1}`,frame:frame(i*20),start:[0,0],segments:[]}]}
      await ui.event(ui.boundary('feature'),'onSketch',i,next)
      assert.equal(ui.boundary('sketch').props.feature.op,'profile_extrude');assert.deepEqual(ui.boundary('sketch').props.feature.frame,frame(i*20))
      await ui.event(ui.button('完成草图'),'onClick');assert.ok(ui.boundary('sketch'));assert.equal(calls.length,0)
      const profile=replaceSketchShape(ui.boundary('sketch').props.feature,'circle',{x:0,y:0,radius:5-i})
      await ui.event(ui.boundary('sketch'),'onChange',profile);await ui.event(ui.button('完成草图'),'onClick')
      const updated=ui.boundary('feature').props.feature;assert.equal(updated.op,'profile_loft');assert.equal(updated.sections.length,i+1)
      assert.deepEqual(updated.sections[i].segments,profile.segments);assert.deepEqual(updated.sections[i].frame,frame(i*20));assert.equal(Object.hasOwn(updated.sections[i],'distance'),false);assert.equal(Object.hasOwn(updated.sections[i],'op'),false)
      if(!i){await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,0);assert.match(ui.text(),/至少两个/)}
    }
    const prepared=clone(ui.boundary('feature').props.feature)
    await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.features[0],prepared);assert.deepEqual(source.plan.features,[])
  }finally{ui.unmount()}
})

test('surface control-grid editing stays inside the transaction and blank grids cannot finish',async()=>{
  await load();const source=emptyCadDraft(),calls=[],ui=harness(Surface,defaults({draft:source,record:null,onComplete:async next=>{calls.push(next);return next}}))
  try{
    await clickTool(ui,'样式曲面');assert.equal(ui.boundary('feature').props.feature.op,'surface_style')
    await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,0);assert.match(ui.text(),/控制点网格/)
    const feature={...ui.boundary('feature').props.feature,points:[[[0,0,0],[20,0,0]],[[0,20,0],[20,20,0]]]}
    await ui.event(ui.boundary('feature'),'onChange',feature);await ui.event(ui.boundary('feature'),'onEditGrid')
    assert.deepEqual(ui.boundary('viewport').props.controlPoints,feature.points)
    await ui.event(ui.boundary('viewport'),'onControlPointChange',1,1,[20,20,5]);assert.deepEqual(ui.boundary('feature').props.feature.points[1][1],[20,20,5]);assert.equal(calls.length,0);assert.deepEqual(source.plan.features,[])
    await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.features[0].points[1][1],[20,20,5])
  }finally{ui.unmount()}
})

test('PMI and PDM panel transitions keep source geometry intact and honor dirty, busy and scope gates',async()=>{
  await load();const calls=[],opened=[],source=draft(),ui=harness(Surface,defaults({draft:source,onComplete:async value=>{calls.push(value);return value},onOpenPdm:value=>opened.push(value)}))
  try{
    await clickTool(ui,'PMI');assert.deepEqual(ui.boundary('pmi').props.plan,source.plan);assert.equal(ui.button('保存到 PDM').props.disabled,true)
    const note={id:'note1',kind:'note',text:'QA尺寸待验收',position:[2,3,10],points:[]}
    await ui.event(ui.boundary('pmi'),'onApply',note);assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.features,source.plan.features);assert.deepEqual(calls[0].plan.annotations,[note]);assert.equal(source.plan.annotations,undefined)
    await clickTool(ui,'保存到 PDM');assert.equal(ui.boundary('pdm').props.token,'token');assert.equal(ui.boundary('pdm').props.accountKey,'account:file')
    await ui.event(ui.boundary('pdm'),'onOpen',{id:'pdm-own'});assert.deepEqual(opened,[{id:'pdm-own'}])
    await ui.update({dirty:true});assert.equal(ui.button('保存到 PDM').props.disabled,true);assert.equal(ui.button('导出 STP').props.disabled,true)
    await ui.update({dirty:false,busy:'保存中'});assert.equal(ui.button('PMI').props.disabled,true)
    await ui.update({busy:''});await clickTool(ui,'PMI');await ui.update({scope:'another-file'});assert.equal(ui.nodes().some(n=>n.type?.testBoundary==='pmi'),false)
  }finally{ui.unmount()}
})

test('standard and imported body insertions are staged for review and cancel does not persist their prepared plans',async()=>{
  await load()
  for(const [label,boundary,op]of [['标准件','standard','standard_part'],['导入实体','import','import_step']]){
    const source=emptyCadDraft(),calls=[],ui=harness(Surface,defaults({draft:source,record:null,onComplete:async next=>{calls.push(next);return next}}))
    try{
      await clickTool(ui,label);const panel=ui.boundary(boundary);assert.deepEqual(panel.props.plan,source.plan)
      if(boundary==='import'){assert.equal(panel.props.token,'token');assert.equal(panel.props.accountKey,'account:file');assert.equal(panel.props.active,true)}
      const feature=op==='standard_part'?{id:'part1',op,catalogId:'washer-m8',dimensions:{innerDiameter:8.4,outerDiameter:16,length:1.6}}:{id:'part1',op,assetId:`asset_${'a'.repeat(32)}`,sha256:'b'.repeat(64)}
      const prepared={...clone(source.plan),features:[feature],result:feature.id}
      await ui.event(panel,'onApply',prepared,{label:'自有试件',featureIds:['part1']})
      assert.deepEqual(ui.boundary('feature').props.feature,feature);assert.equal(calls.length,0);assert.deepEqual(source.plan.features,[])
      await ui.event(ui.button('取消'),'onClick');assert.equal(ui.nodes().some(n=>n.type?.testBoundary==='feature'),false);assert.equal(calls.length,0)
      await clickTool(ui,label);await ui.event(ui.boundary(boundary),'onApply',prepared,{label:'自有试件',featureIds:['part1']});await ui.event(ui.button('完成'),'onClick')
      assert.equal(calls.length,1);assert.deepEqual(calls[0].plan,prepared)
    }finally{ui.unmount()}
  }
})

const namedBodies = () => [0,1,2].map(index=>({id:`body:${index}`,index,signature:String(index+1).repeat(64),faceIds:[],edgeIds:[]}))
function bodyDraft(){const value=draft();value.plan.bodyStates=namedBodies().map((body,i)=>({id:`state${i}`,signature:body.signature,label:['命名甲','命名乙','命名丙'][i],hidden:i===0,color:'#123456',binding:{version:1,keys:[String(i+4).repeat(64)],otherKeys:[]}}));return value}
function bodyPreview(current=true){globalThis.__surfacePreview=()=>({geometry:{...clone(fixture.result),bodies:namedBodies(),inspection:{solidCount:3}},current,loading:!current,error:'',retry(){}})}

test('body deletion and keep remove only the explicitly discarded state records; cancel and failed saves keep the original metadata',async()=>{
  await load()
  for(const [label,remaining] of [['删除所选实体',['state1','state2']],['仅保留所选实体',['state0']]]){
    const source=bodyDraft(),before=clone(source),calls=[],ui=harness(Surface,defaults({draft:source,onComplete:async value=>{calls.push(value);return null}}));bodyPreview();ui.render()
    try{
      await ui.event(ui.button('命名甲'),'onClick');await clickTool(ui,label)
      const previewed=ui.boundary('feature').props.feature;assert.equal(previewed.op,'body_edit')
      assert.equal(previewed.bodies[0].signature,source.plan.bodyStates[0].signature)
      await ui.event(ui.button('取消'),'onClick');assert.deepEqual(source,before);assert.equal(calls.length,0)
      await ui.event(ui.button('命名甲'),'onClick');await clickTool(ui,label);await ui.event(ui.button('完成'),'onClick')
      assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.bodyStates.map(state=>state.id),remaining)
      for(const state of calls[0].plan.bodyStates)assert.deepEqual(state,before.plan.bodyStates.find(original=>original.id===state.id))
      assert.deepEqual(source,before);assert.equal(ui.boundary('feature').props.feature.action,label.startsWith('删除')?'delete':'keep')
      await ui.event(ui.button('取消'),'onClick');assert.deepEqual(source,before)
    }finally{ui.unmount()}
  }
})

test('moving rotating copying or adding a body retains all unrelated state bindings and names',async()=>{
  await load()
  for(const label of ['移动','旋转','复制','长方体']){
    const source=bodyDraft(),calls=[],ui=harness(Surface,defaults({draft:source,onComplete:async value=>{calls.push(value);return value}}));bodyPreview();ui.render()
    try{
      if(label==='长方体'){await clickTool(ui,label);await ui.event(ui.button('新建实体'),'onClick')}
      else{await ui.event(ui.button('命名甲'),'onClick');const action=ui.find(n=>n.type==='button'&&n.props.className==='ce-context-action'&&React.Children.toArray(n.props.children).join('')===label);await ui.event(action,'onClick')}
      await ui.event(ui.button('完成'),'onClick');assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.bodyStates,source.plan.bodyStates)
    }finally{ui.unmount()}
  }
})

test('body deletion rejects stale input previews before staging or submitting a destructive operation',async()=>{
  await load();const source=bodyDraft(),calls=[],ui=harness(Surface,defaults({draft:source,onComplete:async value=>calls.push(value)}));bodyPreview(false);ui.render()
  try{await ui.event(ui.button('命名甲'),'onClick');await clickTool(ui,'删除所选实体');assert.match(ui.text(),/实体预览已变化/);assert.equal(ui.nodes().some(n=>n.type?.testBoundary==='feature'),false);assert.equal(calls.length,0)}finally{ui.unmount()}
})

const referencesFixture=()=>[
  {id:'plane1',kind:'plane',mode:'offset',label:'偏移平面',parent:'XY',offset:5,angle:0,axis:'x'},
  {id:'axis1',kind:'axis',mode:'two_points',label:'旋转轴',start:[0,0,0],end:[0,0,10]},
  {id:'helix1',kind:'helix',label:'扫掠曲线',mode:'coordinates',radius:5,pitch:2,turns:3,frame:{origin:[0,0,0],xDir:[1,0,0],normal:[0,0,1]}},
  {id:'point1',kind:'point',mode:'coordinates',label:'定位点',point:[2,3,4]},
]
const independentFixture=()=>({id:'sketch1',label:'独立截面',plane:'XY',start:[0,0],segments:[{type:'line',to:[10,0]},{type:'line',to:[10,5]},{type:'line',to:[0,5]},{type:'line',to:[0,0]}]})
const ariaControl=(ui,name)=>ui.find(node=>node.type==='button'&&node.props['aria-label']===name)

test('reference tree exposes real edit and delete handlers, commits only the chosen record and respects busy transaction gates',async()=>{
  await load();const source=draft();source.plan.references=referencesFixture();const before=clone(source),calls=[],ui=harness(Surface,defaults({draft:source,onComplete:async value=>{calls.push(value);return value}}))
  try{
    await ui.event(ariaControl(ui,'编辑参考几何 point1'),'onClick');assert.equal(ui.boundary('reference').props.initial.id,'point1')
    assert.equal(ariaControl(ui,'删除参考几何 axis1').props.disabled,true)
    await ui.event(ariaControl(ui,'删除参考几何 axis1'),'onClick');assert.equal(calls.length,0)
    await ui.event(ui.boundary('reference'),'onClose')
    await ui.event(ariaControl(ui,'删除参考几何 point1'),'onClick');await ui.flush();assert.equal(calls.length,1)
    assert.deepEqual(calls[0].plan.references,source.plan.references.filter(ref=>ref.id!=='point1'));assert.deepEqual(calls[0].plan.features,source.plan.features);assert.deepEqual(source,before)
    await ui.update({busy:'保存中'});assert.equal(ariaControl(ui,'删除参考几何 plane1').props.disabled,true)
    await ui.event(ariaControl(ui,'删除参考几何 plane1'),'onClick');assert.equal(calls.length,1)
  }finally{ui.unmount()}
})

test('deleting a reference used as a rotation axis, sweep curve, sketch plane or parent plane names its dependency and submits nothing',async()=>{
  await load()
  for(const [id,dependency] of [
    ['axis1',{feature:{id:'dependent',label:'依赖旋转',op:'rotate',input:'drilled',axisReference:'axis1',angle:30}}],
    ['helix1',{feature:{id:'dependent',label:'依赖扫掠',op:'profile_sweep',path:{curveReference:'helix1'}}}],
    ['plane1',{sketch:{...independentFixture(),label:'依赖草图',planeReference:'plane1'}}],
    ['plane1',{reference:{id:'plane2',label:'依赖平面',kind:'plane',mode:'offset',parent:'plane1',offset:3}}],
    ['plane1',{feature:{id:'dependent',label:'依赖截面',op:'profile_loft',sections:[{planeReference:'plane1'}]}}],
  ]){
    const source=draft();source.plan.references=referencesFixture();if(dependency.feature)source.plan.features.push(dependency.feature);if(dependency.sketch)source.plan.sketches=[dependency.sketch];if(dependency.reference)source.plan.references.push(dependency.reference)
    const calls=[],ui=harness(Surface,defaults({draft:source,onComplete:async value=>calls.push(value)}))
    try{await ui.event(ariaControl(ui,`删除参考几何 ${id}`),'onClick');assert.equal(calls.length,0);assert.match(ui.text(),/该参考几何仍被使用/);assert.match(ui.text(),/依赖/);assert.equal(source.plan.references.length,dependency.reference?5:4)}finally{ui.unmount()}
  }
})

test('independent sketch tree supports editing and deletion, clears the removed selection, and rejects profile path or section dependencies',async()=>{
  await load();const source=emptyCadDraft();source.plan.sketches=[independentFixture()];const calls=[],ui=harness(Surface,defaults({draft:source,record:null,onSaveDraft:async value=>{calls.push(value);return value}}))
  try{
    await ui.event(ariaControl(ui,'编辑独立草图 sketch1'),'onClick');assert.deepEqual(ui.boundary('sketch').props.feature.segments,source.plan.sketches[0].segments)
    await ui.event(ui.button('取消'),'onClick');assert.equal(calls.length,0)
    await ui.event(ui.button('独立截面'),'onClick');await ui.event(ariaControl(ui,'删除独立草图 sketch1'),'onClick');assert.equal(calls.length,1);assert.deepEqual(calls[0].plan.sketches,[]);assert.equal(source.plan.sketches.length,1)
    await ui.update({draft:calls[0]});await clickTool(ui,'拉伸');assert.deepEqual(ui.boundary('sketch').props.feature.segments,[]);assert.doesNotMatch(ui.text(),/草图不存在/)
  }finally{ui.unmount()}
  for(const feature of [{id:'extrude',op:'profile_extrude',sketchId:'sketch1'},{id:'sweep',op:'profile_sweep',path:{sketchId:'sketch1'}},{id:'loft',op:'profile_loft',sections:[{sketchId:'sketch1'}]}]){
    const value=draft();value.plan.sketches=[independentFixture()];value.plan.features.push(feature);const blocked=[],instance=harness(Surface,defaults({draft:value,onComplete:async next=>blocked.push(next)}))
    try{await instance.event(ariaControl(instance,'删除独立草图 sketch1'),'onClick');assert.equal(blocked.length,0);assert.match(instance.text(),/该草图仍被特征使用/);assert.equal(value.plan.sketches.length,1)}finally{instance.unmount()}
  }
})

test('saved construction geometry starts hidden; selection, eye toggles and live edit preview affect only local display',async()=>{
  await load();const source=draft();source.plan.references=referencesFixture();const before=clone(source),ui=harness(Surface,defaults({draft:source}))
  const shown=()=>ui.boundary('viewport').props.construction.map(reference=>reference.id)
  try{
    assert.deepEqual(shown(),[]);assert.match(ui.text(),/曲线 \(1\)/)
    await ui.event(ui.button('偏移平面'),'onClick');assert.deepEqual(shown(),['plane1'])
    await ui.event(ariaControl(ui,'隐藏参考几何 plane1'),'onClick');assert.deepEqual(shown(),[])
    await ui.event(ariaControl(ui,'显示参考几何 helix1'),'onClick');assert.deepEqual(shown(),['helix1']);assert.ok(ui.boundary('viewport').props.construction[0].points.length>100)
    await ui.event(ariaControl(ui,'编辑参考几何 axis1'),'onClick');assert.deepEqual(shown(),['axis1','helix1'])
    await ui.event(ui.boundary('reference'),'onPreview',{...source.plan.references[1],end:[0,0,25]});assert.deepEqual(ui.boundary('viewport').props.construction.find(ref=>ref.id==='axis1').end,[0,0,25])
    await ui.event(ui.boundary('reference'),'onClose');assert.deepEqual(shown(),['helix1']);assert.deepEqual(source,before)
    await ui.update({scope:'different-account-file'});assert.deepEqual(shown(),[])
    await clickTool(ui,'平面');await ui.event(ui.boundary('reference'),'onPreview',{id:'plane2',kind:'plane',mode:'offset',parent:'XY',offset:8})
    assert.deepEqual(shown(),['plane2']);await ui.event(ui.boundary('reference'),'onClose');assert.deepEqual(shown(),[])
    await clickTool(ui,'新建草图');await ui.event(ui.button('偏移平面'),'onClick')
    assert.equal(ui.boundary('sketch').props.feature.planeReference,'plane1');assert.deepEqual(ui.boundary('sketch').props.feature.frame.origin,[0,0,5]);assert.deepEqual(source,before)
  }finally{ui.unmount()}
})

test('surface grouping counts the real independent surface collection and selects its actual faces without inventing source ownership',async()=>{
  await load();const source=draft(),ui=harness(Surface,defaults({draft:source})),face=fixture.result.faces[0]
  const surface={id:'surface:0',index:0,signature:'a'.repeat(64),faceIds:[face.id],edgeIds:[],areaMm2:100,shapeType:'Face'}
  globalThis.__surfacePreview=()=>({geometry:{...clone(fixture.result),surfaceBodies:[surface],inspection:{solidCount:2}},current:true,loading:false,error:'',retry(){}});ui.render()
  try{
    assert.match(ui.text(),/实体 \(2\)/);assert.match(ui.text(),/曲面 \(1\)/)
    await ui.event(ui.button('曲面 1'),'onClick');assert.deepEqual(ui.boundary('viewport').props.selection.map(item=>item.id),[face.id])
    await ui.event(ui.button('曲面 1'),'onDoubleClick');assert.equal(ui.nodes().some(node=>node.type?.testBoundary==='feature'),false)
    globalThis.__surfacePreview=()=>({geometry:{...clone(fixture.result),sourceFeatureId:'sheet',surfaceBodies:[surface]},current:true,loading:false,error:'',retry(){}})
    await ui.update({draft:{...source,plan:{...source.plan,features:[{id:'sheet',op:'surface_style',points:[[[0,0,0],[10,0,0]],[[0,10,0],[10,10,0]]]}],result:'sheet'}}})
    await ui.event(ui.button('曲面 1'),'onDoubleClick');assert.equal(ui.boundary('feature').props.feature.id,'sheet')
  }finally{ui.unmount()}
})
