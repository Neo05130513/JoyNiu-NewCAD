import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdtemp,readFile,rm} from 'node:fs/promises'
import {resolve} from 'node:path'
import {pathToFileURL} from 'node:url'
import {rolldown} from 'rolldown'
import React from 'react'
import {webcrypto} from 'node:crypto'

// Execute the actual component handlers and effects without taking control of
// the user's browser. Hook slots are local to this one component test harness.
function harness(Component,request,props={token:'test',active:true}) {
  const slots=[],effects=[],events=new Map(),storage=new Map([['native-drawing:last-open-id','drawing']])
  let cursor=0,dirty=true,tree
  globalThis.window={addEventListener:(name,fn)=>{if(!events.has(name))events.set(name,new Set());events.get(name).add(fn)},removeEventListener:(name,fn)=>events.get(name)?.delete(fn)}
  globalThis.sessionStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}
  globalThis.__nativeRequest=request
  globalThis.__nativeDownloads=[]
  globalThis.__nativeHooks={
    useState(initial){const i=cursor++;if(!slots[i])slots[i]={value:typeof initial==='function'?initial():initial};return [slots[i].value,value=>{const next=typeof value==='function'?value(slots[i].value):value;if(!Object.is(next,slots[i].value)){slots[i].value=next;dirty=true}}]},
    useRef(initial){const i=cursor++;if(!slots[i])slots[i]={current:initial};return slots[i]},
    useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||!deps||deps.some((value,j)=>!Object.is(value,old.deps?.[j]))){slots[i]={deps,cleanup:old?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn()})}},
  }
  const render=()=>{let rounds=0;do{dirty=false;cursor=0;tree=Component(props);while(effects.length)effects.shift()();assert.ok(++rounds<30,'component must settle')}while(dirty);return tree}
  const flush=async()=>{for(let i=0;i<4;i++){await new Promise(resolve=>setImmediate(resolve));render()}return tree}
  const nodes=(value=tree)=>React.isValidElement(value)?[value,...React.Children.toArray(value.props.children).flatMap(nodes)]:[]
  const text=value=>React.isValidElement(value)?React.Children.toArray(value.props.children).map(text).join(''):String(value??'')
  const find=predicate=>{const found=nodes().find(predicate);assert.ok(found,'expected matching control');return found}
  const button=name=>{const label=({'直线 L':'直线','圆 C':'圆','圆弧 A':'圆弧','多段线 PL':'多段线','文字 T':'文字','标注 D':'尺寸标注','气泡 B':'检验气泡'})[name]||name;return find(node=>node.type==='button'&&(text(node)===label||node.props['aria-label']===label))}
  const event=async(node,name,arg)=>{await node.props[name](arg);render();await flush()}
  const command=async(value)=>{await event(find(n=>n.props['aria-label']==='CAD 命令'),'onChange',{target:{value}});await event(button('执行'),'onClick')}
  const update=async next=>{props={...props,...next};render();await flush()}
  const unmount=()=>{for(const slot of slots)slot?.cleanup?.();delete globalThis.window;delete globalThis.sessionStorage}
  render()
  return {render,flush,nodes,text:()=>text(tree),find,button,event,command,update,events,unmount}
}
const drawing=()=>({id:'drawing',name:'测试图',revision:1,units:4,entities:[{id:'a',type:'LINE',start:[0,0,0],end:[20,0,0],layer:'0',color:256,linetype:'BYLAYER',lineweight:-1,editable:true},{id:'b',type:'CIRCLE',center:[30,0,0],radius:3,layer:'0',editable:true}],layers:[{name:'0',visible:true,color:7}],renderEntities:[],dimensions:[],warnings:[],layouts:['Model'],customProperties:{},canUndo:false,canRedo:false})

let directory,Component,Library,NumericComponent
async function loadComponent(){
  if(Component)return Component
  directory=await mkdtemp(resolve('node_modules/.native-ui-tests-'))
  const bundle=await rolldown({input:{NativeDrawingWorkspace:resolve('src/NativeDrawingWorkspace.jsx'),NativeParametricLibrary:resolve('src/NativeParametricLibrary.jsx')},external:['react/jsx-runtime'],transform:{jsx:{runtime:'automatic'}},plugins:[{name:'native-ui-harness',resolveId(source){if(source==='react')return '\0native-hooks'},async load(id){if(id==='\0native-hooks')return {code:'export const useState=(...a)=>globalThis.__nativeHooks.useState(...a);export const useRef=(...a)=>globalThis.__nativeHooks.useRef(...a);export const useEffect=(...a)=>globalThis.__nativeHooks.useEffect(...a);',moduleType:'js'};if(id===resolve('src/NativeDrawingWorkspace.jsx'))return {code:(await readFile(id,'utf8'))+'\nexport { Numeric as TestNumeric };',moduleType:'jsx'};if(id.endsWith('.css'))return {code:'',moduleType:'js'};if(id.endsWith('/nativeDrawingClient.js'))return {code:'export const nativeDrawingRequest=(...a)=>globalThis.__nativeRequest(...a);export const saveNativeBlob=(...a)=>globalThis.__nativeDownloads.push(a);',moduleType:'js'}}}]})
  await bundle.write({dir:directory,format:'esm'});await bundle.close()
  const workspace=await import(pathToFileURL(resolve(directory,'NativeDrawingWorkspace.js')).href)
  Component=workspace.default;NumericComponent=workspace.TestNumeric
  Library=(await import(pathToFileURL(resolve(directory,'NativeParametricLibrary.js')).href)).default
  return Component
}
test.after(async()=>{if(directory)await rm(directory,{recursive:true,force:true})})

// Public HTTP origins retain getRandomValues but do not expose randomUUID.
// Exercise the bundled components with that exact capability boundary.
const uuidPattern=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
async function withHttpCrypto(run){
  const original=Object.getOwnPropertyDescriptor(globalThis,'crypto')
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:{getRandomValues:bytes=>webcrypto.getRandomValues(bytes)}})
  try{assert.equal(globalThis.crypto.randomUUID,undefined);return await run()}
  finally{if(original)Object.defineProperty(globalThis,'crypto',original);else delete globalThis.crypto}
}

test('both empty-drawing create buttons work on HTTP and lost-response retries keep one request identity',async()=>withHttpCrypto(async()=>{
  const identities=[]
  for(const name of ['新建','创建第一张图纸']){
    const calls=[],created=new Map();let lost=true
    const ui=harness(await loadComponent(),async(path,options={})=>{
      if(path==='/capabilities')return {canWrite:true}
      if(path===''&&options.method!=='POST')return {items:[]}
      assert.equal(path,'');assert.equal(options.method,'POST')
      const body=structuredClone(options.body);calls.push(body);assert.match(body.requestId,uuidPattern)
      if(!created.has(body.requestId))created.set(body.requestId,{...drawing(),name:body.name,entities:[]})
      if(lost){lost=false;const error=new Error('保存响应丢失');error.retryable=true;throw error}
      return structuredClone(created.get(body.requestId))
    })
    try{
      await ui.flush();await ui.event(ui.find(n=>n.props['aria-label']==='图纸名称'),'onChange',{target:{value:`HTTP ${name}`}})
      assert.equal(ui.button(name).props.disabled,false)
      await ui.event(ui.button(name),'onClick')
      assert.match(ui.text(),/草稿已保留/)
      assert.equal(ui.button('新建').props.disabled,true);assert.equal(ui.button('创建第一张图纸').props.disabled,true)
      await ui.event(ui.button('重试未确认请求'),'onClick')
      assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.equal(created.size,1)
      assert.equal(calls[0].name,`HTTP ${name}`);assert.match(ui.text(),/当前修订已保存/)
      identities.push(calls[0].requestId)
    }finally{ui.unmount()}
  }
  assert.notEqual(identities[0],identities[1],'Separate creates must not share an idempotency key')
}))

test('HTTP drawing, undo, redo, import and parameter constraints all submit usable unique ids',async()=>withHttpCrypto(async()=>{
  let saved=drawing(),drawn
  const requests=[]
  const ui=harness(await loadComponent(),async(path,options={})=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true}
    if(options.method!=='POST'){assert.equal(path,'/drawing');return structuredClone(saved)}
    const requestId=path==='/import'?options.body.get('requestId'):options.body.requestId
    assert.match(requestId,uuidPattern);requests.push({path,requestId,body:options.body})
    if(path==='/import'){
      assert.equal(options.body.get('file').name,'source.dxf');assert.equal(await options.body.get('file').text(),'actual DXF source bytes')
      saved={...drawing(),id:'imported',name:'source'}
    }else if(path==='/drawing/undo'){
      assert.equal(options.body.expectedRevision,2);saved={...drawing(),revision:3,canRedo:true}
    }else if(path==='/drawing/redo'){
      assert.equal(options.body.expectedRevision,3);saved={...drawn,revision:4,canRedo:false,canUndo:true}
    }else{
      assert.equal(path,`/${saved.id}/operations`);assert.equal(options.body.expectedRevision,saved.revision)
      for(const operation of options.body.operations){
        if(operation.op==='add')saved={...saved,entities:[...saved.entities,{id:'new-line',editable:true,...operation.entity}]}
        else if(operation.op==='update')saved={...saved,entities:saved.entities.map(entity=>entity.id===operation.id?{...entity,...operation.changes}:entity)}
        else if(operation.op==='metadata')saved={...saved,customProperties:operation.customProperties}
        else assert.fail(`Unexpected operation ${operation.op}`)
      }
      saved={...saved,revision:saved.revision+1,canUndo:true}
      if(saved.id==='drawing')drawn=structuredClone(saved)
    }
    return structuredClone(saved)
  })
  const control=(label,type)=>ui.nodes(ui.find(n=>n.type==='label'&&React.Children.toArray(n.props.children).includes(label))).find(n=>n.type===type)
  try{
    await ui.flush();await ui.event(ui.button('直线'),'onClick')
    await ui.event(ui.button('直线 L'),'onClick');await ui.command('0,10');await ui.command('30,10')
    assert.deepEqual(saved.entities.at(-1).end,[30,10,0]);assert.equal(saved.entities.length,3)
    await ui.event(ui.button('撤销'),'onClick');assert.equal(saved.entities.length,2)
    await ui.event(ui.button('重做'),'onClick');assert.equal(saved.entities.length,3)
    const file=new File(['actual DXF source bytes'],'source.dxf',{type:'application/dxf'})
    const target={files:[file],value:'source.dxf'}
    await ui.event(ui.find(n=>n.type==='input'&&n.props.type==='file'),'onChange',{target})
    assert.equal(target.value,'');assert.match(ui.text(),/source · 修订 1/)
    await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'a'}})
    await ui.event(ui.button('约束'),'onClick')
    await ui.event(control('参数名','input'),'onChange',{target:{value:'width'}})
    await ui.event(control('值 / 公式','input'),'onChange',{target:{value:'20'}})
    await ui.event(ui.button('保存参数并重算'),'onClick')
    await ui.event(control('约束类型','select'),'onChange',{target:{value:'length'}})
    await ui.event(control('尺寸 / 公式','input'),'onChange',{target:{value:'width'}})
    await ui.event(ui.button('添加到所选图元'),'onClick')
    assert.equal(saved.customProperties.parameters.width,'20')
    assert.equal(saved.customProperties.constraints.length,1)
    const constraint=saved.customProperties.constraints[0]
    assert.match(constraint.id,uuidPattern);assert.equal(constraint.value,'width');assert.deepEqual(constraint.entities,['a'])
    assert.equal(requests.length,6)
    assert.equal(new Set([...requests.map(item=>item.requestId),constraint.id]).size,7)
  }finally{ui.unmount()}
}))

test('HTTP parameter library saves real geometry, internal constraints and formulas without randomUUID',async()=>withHttpCrypto(async()=>{
  await loadComponent();const operations=[],errors=[]
  const selected=[drawing().entities[0]],constraints=[{id:'length-source',type:'length',entities:['a'],value:'width'}]
  let doc={id:'drawing',revision:1,customProperties:{}}
  const props={document:doc,selected,constraints,parameters:{width:20},translation:[0,0],busy:false,
    operate:async ops=>{operations.push(structuredClone(ops));doc={...doc,revision:doc.revision+1,customProperties:ops[0].customProperties};return doc},
    fit(){},onError:error=>errors.push(error),onDirtyChange(){}}
  const ui=harness(Library,async()=>{},props)
  try{
    await ui.flush();await ui.event(ui.button('保存所选图元、内部约束和参数'),'onClick')
    assert.deepEqual(errors,[]);assert.equal(operations.length,1)
    const saved=doc.customProperties.library[0]
    assert.match(saved.id,uuidPattern);assert.equal(saved.version,2);assert.equal(saved.entities.length,1)
    assert.deepEqual(saved.entities[0].end,[20,0,0]);assert.equal(saved.constraints[0].value,'width')
    assert.equal(saved.constraints[0].entities[0],saved.entities[0].id);assert.deepEqual(saved.parameters,{width:20})
    await ui.update({document:doc});await ui.event(ui.find(n=>n.props['aria-label']==='构件参数 width'),'onChange',{target:{value:'30'}})
    await ui.event(ui.button('保存为默认参数'),'onClick')
    assert.deepEqual(errors,[]);assert.equal(operations.length,2)
    assert.equal(doc.customProperties.library[0].id,saved.id);assert.equal(doc.customProperties.library[0].parameters.width,'30')
  }finally{ui.unmount()}
}))

test('drawing draft survives a lost response and retries exactly the same operation id',async()=>{
  const calls=[];let saved=drawing(),lost=true
  const ui=harness(await loadComponent(),async(path,options)=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return structuredClone(saved)
    assert.equal(path,'/drawing/operations');calls.push(structuredClone(options.body))
    if(lost){lost=false;saved={...saved,revision:2,entities:[...saved.entities,{id:'new',...options.body.operations[0].entity}]};const error=new Error('connection lost');error.retryable=true;throw error}
    return structuredClone(saved)
  })
  try{
    await ui.flush();await ui.event(ui.button('直线'),'onClick');await ui.event(ui.button('直线 L'),'onClick');await ui.command('0,10');await ui.command('30,10')
    assert.match(ui.text(),/保存结果待确认/);assert.ok(ui.button('取消绘制').props.disabled)
    await ui.event(ui.button('重试未确认请求'),'onClick')
    assert.equal(calls.length,2);assert.deepEqual(calls[0],calls[1]);assert.match(calls[0].requestId,/^[a-z0-9-]+$/)
    assert.match(ui.text(),/修订 2/);assert.match(ui.text(),/当前修订已保存/);assert.equal(saved.entities.length,3)
  }finally{ui.unmount()}
})

test('unsaved property edits require a decision before changing selection',async()=>{
  let saved=drawing();const writes=[]
  const ui=harness(await loadComponent(),async(path,options)=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return structuredClone(saved)
    writes.push(options.body);saved={...saved,revision:saved.revision+1,entities:saved.entities.map(entity=>entity.id==='a'?{...entity,...options.body.operations[0].changes}:entity)};return structuredClone(saved)
  })
  try{
    await ui.flush();await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'a'}})
    await ui.event(ui.find(n=>n.props.label==='终点 X'),'onChange',45)
    assert.match(ui.text(),/属性未保存/)
    await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'b'}})
    assert.equal(writes.length,0);assert.ok(ui.find(n=>n.props.role==='dialog'))
    await ui.event(ui.button('继续编辑'),'onClick');assert.equal(ui.find(n=>n.props.label==='终点 X').props.value,45)
    await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'b'}})
    await ui.event(ui.button('保存属性后继续'),'onClick')
    assert.equal(writes.length,1);assert.equal(saved.entities[0].end[0],45)
    assert.equal(ui.find(n=>n.props['aria-label']==='选择图纸实体').props.value,'b')
  }finally{ui.unmount()}
})

test('an old-account preview response cannot create a URL or download after logout',async()=>{
  let finish;const original=URL.createObjectURL;let urls=0;URL.createObjectURL=()=>{urls++;return 'blob:test'}
  const ui=harness(await loadComponent(),async path=>{if(path==='')return {items:[drawing()]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return drawing();return new Promise(resolve=>{finish=resolve})})
  try{
    await ui.flush();await ui.event(ui.button('出图'),'onClick')
    const pending=ui.button('整图预览（含块和原始标注）').props.onClick();ui.render();await ui.update({token:null})
    finish(new Blob(['svg']));await pending;await ui.flush()
    assert.equal(urls,0);assert.equal(globalThis.__nativeDownloads.length,0);assert.match(ui.text(),/登录后可创建/)
  }finally{ui.unmount();URL.createObjectURL=original}
})

test('viewer writes are disabled and hidden workspaces do not handle the save shortcut',async()=>{
  const calls=[];const ui=harness(await loadComponent(),async(path,options)=>{calls.push({path,options});if(path==='')return {items:[drawing()]};if(path==='/capabilities')return {canWrite:false};return drawing()})
  try{
    await ui.flush();await ui.event(ui.button('直线'),'onClick');assert.ok(ui.button('新建').props.disabled);assert.ok(ui.button('直线 L').props.disabled);assert.match(ui.text(),/只读/)
    await ui.update({active:false});let prevented=false
    for(const fn of ui.events.get('keydown')||[])fn({metaKey:true,key:'s',preventDefault:()=>{prevented=true},stopPropagation(){}})
    assert.equal(prevented,false);assert.equal(calls.filter(call=>call.options?.method==='POST').length,0)
  }finally{ui.unmount()}
})

test('same-account token refresh retains property drafts and saves with the new credential',async()=>{
  let saved=drawing();const writes=[]
  const ui=harness(await loadComponent(),async(path,options)=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return structuredClone(saved)
    writes.push({credential:options.token(),body:options.body});saved={...saved,revision:2,entities:saved.entities.map(entity=>entity.id==='a'?{...entity,...options.body.operations[0].changes}:entity)};return structuredClone(saved)
  },{token:'before-refresh',accountKey:'alice',active:true})
  try{
    await ui.flush();await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'a'}})
    await ui.event(ui.find(n=>n.props.label==='终点 X'),'onChange',-45)
    await ui.update({token:'after-refresh'})
    assert.match(ui.text(),/属性未保存/);assert.equal(ui.find(n=>n.props.label==='终点 X').props.value,-45)
    let prevented=false
    for(const fn of ui.events.get('keydown')||[])fn({metaKey:true,key:'s',preventDefault:()=>{prevented=true},stopPropagation(){}})
    await ui.flush()
    assert.equal(prevented,true);assert.equal(writes.length,1);assert.equal(writes[0].credential,'after-refresh');assert.equal(saved.entities[0].end[0],-45)
  }finally{ui.unmount()}
})

const template=()=>({id:'t',name:'参数线',version:2,entities:[{id:'e0',type:'LINE',start:[0,0,0],end:[20,0,0],editable:true}],constraints:[{id:'c0',type:'length',entities:['e0'],value:'w'}],parameters:{w:20}})
test('parameter library keeps unsaved formulas when closing or switching editors',async()=>{
  await loadComponent();let dirty=false;const errors=[],operations=[]
  const props={document:{id:'d',revision:1,customProperties:{library:[template()]}},selected:[],constraints:[],parameters:{},translation:[0,0],busy:false,operate:async ops=>{operations.push(ops);return {}},fit(){},onError:error=>errors.push(error),onDirtyChange:value=>{dirty=value}}
  const ui=harness(Library,async()=>{},props)
  try{
    await ui.flush();await ui.event(ui.button('参数与插入'),'onClick')
    await ui.event(ui.find(n=>n.props['aria-label']==='构件参数 w'),'onChange',{target:{value:'width /'}})
    assert.equal(dirty,true);await ui.event(ui.button('关闭参数'),'onClick');assert.match(ui.text(),/构件参数尚未保存/)
    await ui.event(ui.button('继续编辑'),'onClick');assert.equal(ui.find(n=>n.props['aria-label']==='构件参数 w').props.value,'width /')
    await ui.event(ui.button('求解并插入新实例'),'onClick');assert.ok(errors.length);assert.equal(operations.length,0)
    await ui.event(ui.button('关闭参数'),'onClick');await ui.event(ui.button('放弃参数修改并继续'),'onClick');assert.equal(dirty,false)
  }finally{ui.unmount()}
})
test('undoing removal of a parameter template does not leave a stale open editor',async()=>{
  await loadComponent();const doc={id:'d',revision:1,customProperties:{library:[template()]}}
  const ui=harness(Library,async()=>{},{document:doc,selected:[],constraints:[],parameters:{},translation:[0,0],busy:false,operate:async()=>{},fit(){},onError(){},onDirtyChange(){}})
  try{
    await ui.flush();await ui.event(ui.button('参数与插入'),'onClick');assert.match(ui.text(),/插入参数构件/)
    await ui.update({document:{...doc,revision:2,customProperties:{library:[]}}})
    assert.doesNotMatch(ui.text(),/插入参数构件/)
  }finally{ui.unmount()}
})

test('Numeric follows undo and redo values without requiring an input blur',async()=>{
  await loadComponent();const changes=[]
  const ui=harness(NumericComponent,async()=>{},{label:'终点 X',value:55.25,onChange:value=>changes.push(value)})
  try{
    await ui.flush();const input=()=>ui.find(node=>node.type==='input')
    input().props.onFocus?.()
    await ui.update({value:40.5})
    assert.equal(input().props.value,'40.5','undo must update the displayed number while focus is retained')
    await ui.update({value:55.25})
    assert.equal(input().props.value,'55.25')
    assert.deepEqual(changes,[],'history refresh must not create another edit')
  }finally{ui.unmount()}
})

test('Numeric retains incomplete decimal or sign drafts when its source number is unchanged',async()=>{
  await loadComponent();const changes=[]
  const ui=harness(NumericComponent,async()=>{},{label:'位移 X',value:0,onChange:value=>changes.push(value)})
  try{
    await ui.flush();const input=()=>ui.find(node=>node.type==='input')
    await ui.event(input(),'onChange',{target:{value:'0.'}});await ui.update({value:0})
    assert.equal(input().props.value,'0.')
    await ui.event(input(),'onChange',{target:{value:'-'}});await ui.update({value:0})
    assert.equal(input().props.value,'-');assert.deepEqual(changes,[0])
    await ui.event(input(),'onChange',{target:{value:'-0.25'}});await ui.update({value:-.25})
    assert.equal(input().props.value,'-0.25');assert.deepEqual(changes,[0,-.25])
  }finally{ui.unmount()}
})


test('ribbon tabs retain text settings and actual drawing tools submit geometry',async()=>{
  let saved=drawing();const writes=[]
  const ui=harness(await loadComponent(),async(path,options)=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return structuredClone(saved)
    writes.push(options.body);saved={...saved,revision:2,entities:[...saved.entities,{id:'text-added',editable:true,...options.body.operations[0].entity}]};return structuredClone(saved)
  })
  try{
    await ui.flush();await ui.event(ui.button('文字'),'onClick')
    await ui.event(ui.find(n=>n.type==='input'&&n.props.value==='文字'),'onChange',{target:{value:'阀体 A'}})
    await ui.event(ui.find(n=>n.props.label==='文字高度'),'onChange',4)
    await ui.event(ui.button('直线'),'onClick')
    await ui.event(ui.button('注释'),'onClick');await ui.event(ui.button('文字'),'onClick')
    assert.equal(ui.find(n=>n.props.label==='文字高度').props.value,4)
    await ui.command('50,20');assert.equal(writes.length,1)
    assert.deepEqual(writes[0].operations[0].entity,{layer:'0',type:'TEXT',position:[50,20,0],text:'阀体 A',height:4,rotation:0})
  }finally{ui.unmount()}
})

test('changing ribbon tools preserves unfinished geometry and property drafts until explicitly discarded',async()=>{
  let saved=drawing();const writes=[]
  const ui=harness(await loadComponent(),async(path,options)=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return structuredClone(saved)
    writes.push(options.body);saved={...saved,revision:2,entities:[...saved.entities,{id:'added',editable:true,...options.body.operations[0].entity}]};return structuredClone(saved)
  })
  try{
    await ui.flush();await ui.event(ui.button('直线'),'onClick');await ui.command('0,12')
    await ui.event(ui.button('文字'),'onClick');assert.ok(ui.find(n=>n.props.role==='dialog'));assert.equal(writes.length,0)
    await ui.event(ui.button('继续编辑'),'onClick');await ui.command('20,12')
    assert.deepEqual(writes[0].operations[0].entity.start,[0,12,0]);assert.deepEqual(writes[0].operations[0].entity.end,[20,12,0])
    await ui.event(ui.button('选择'),'onClick');await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'a'}})
    await ui.event(ui.find(n=>n.props.label==='终点 X'),'onChange',45)
    await ui.event(ui.button('移动'),'onClick');await ui.event(ui.button('继续编辑'),'onClick')
    assert.equal(ui.find(n=>n.props.label==='终点 X').props.value,45);assert.equal(writes.length,1)
  }finally{ui.unmount()}
})

test('compact transform controls keep every operation reachable and submit only the chosen transform',async()=>{
  let saved=drawing();const writes=[]
  const ui=harness(await loadComponent(),async(path,options)=>{
    if(path==='')return {items:[saved]};if(path==='/capabilities')return {canWrite:true};if(path==='/drawing')return structuredClone(saved)
    writes.push(options.body);saved={...saved,revision:saved.revision+1};return structuredClone(saved)
  })
  try{
    await ui.flush();await ui.event(ui.find(n=>n.props['aria-label']==='选择图纸实体'),'onChange',{target:{value:'a'}})
    await ui.event(ui.button('变换'),'onClick')
    const operation=()=>ui.find(n=>n.props['aria-label']==='变换操作')
    assert.equal(React.Children.toArray(operation().props.children).length,9)
    assert.ok(ui.find(n=>n.props.label==='位移 X'))
    assert.equal(ui.nodes().some(n=>n.props.label==='偏移距离'),false)
    await ui.event(operation(),'onChange',{target:{value:'rotate'}})
    assert.equal(ui.nodes().some(n=>n.props.label==='位移 X'),false)
    await ui.event(ui.find(n=>n.props.label==='角度°'),'onChange',30)
    await ui.event(ui.find(n=>n.type==='button'&&n.props.className==='primary'&&React.Children.toArray(n.props.children).includes('旋转')),'onClick')
    assert.deepEqual(writes[0].operations,[{op:'transform',ids:['a'],translation:[0,0],rotation:30,scale:1,origin:[0,0],mirror:undefined}])
    await ui.event(operation(),'onChange',{target:{value:'offset'}})
    await ui.event(ui.find(n=>n.props.label==='偏移距离'),'onChange',7)
    await ui.event(ui.find(n=>n.type==='button'&&n.props.className==='primary'&&React.Children.toArray(n.props.children).includes('偏移')),'onClick')
    assert.deepEqual(writes[1].operations,[{op:'offset',ids:['a'],distance:7}])
    await ui.event(operation(),'onChange',{target:{value:'rotate'}})
    assert.equal(ui.find(n=>n.props.label==='角度°').props.value,30)
  }finally{ui.unmount()}
})
