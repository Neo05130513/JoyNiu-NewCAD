import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import React from 'react'
import * as THREE from 'three'
import { rolldown } from 'rolldown'
import { captureCadCamera, fitCadCamera, restoreCadCamera } from './cadModelViewport.js'

const fixture=JSON.parse(await readFile(new URL('./fixtures/cadViewportBoxBore.json',import.meta.url),'utf8'))
const controls=[]
class Element extends EventTarget {
  clientWidth=1000;clientHeight=650;style={}
  appendChild(){} setAttribute(){} remove(){} querySelector(){return null}
  getBoundingClientRect(){return{left:0,top:0,width:this.clientWidth,height:this.clientHeight}}
}
// Exercise the actual component, Three camera and OrbitControls lifecycle.
// Only WebGL/DOM drawing is replaced; no camera/fit math is mocked.
class Renderer {
  domElement=new Element()
  setPixelRatio(){} setSize(){} render(){} dispose(){}
}
let directory,Viewport
async function load(){
  if(Viewport)return
  globalThis.__cadCameraRenderer=Renderer;globalThis.__cadCameraControls=controls
  directory=await mkdtemp(resolve('node_modules/.cad-camera-tests-'))
  const bundle=await rolldown({input:resolve('src/CadModelViewport.jsx'),transform:{jsx:{runtime:'automatic'}},plugins:[{
    name:'camera-lifecycle-boundaries',resolveId(source,importer){
      if(source==='react')return '\0camera-hooks'
      if(source==='three'&&importer?.endsWith('/CadModelViewport.jsx'))return '\0camera-renderer'
      if(source.endsWith('/OrbitControls.js')&&importer?.endsWith('/CadModelViewport.jsx'))return '\0camera-controls'
      if(source.startsWith('react/')||source==='three'||source.startsWith('three/'))return{id:source,external:true}
    },load(id){
      if(id==='\0camera-hooks')return{code:'export const useState=(...a)=>globalThis.__cadCameraHooks.useState(...a);export const useRef=(...a)=>globalThis.__cadCameraHooks.useRef(...a);export const useEffect=(...a)=>globalThis.__cadCameraHooks.useEffect(...a)',moduleType:'js'}
      if(id==='\0camera-renderer')return{code:'export * from "three";export const WebGLRenderer=globalThis.__cadCameraRenderer',moduleType:'js'}
      if(id==='\0camera-controls')return{code:'import {OrbitControls as Real} from "three/examples/jsm/controls/OrbitControls.js";export class OrbitControls extends Real{constructor(camera){super(camera,null);globalThis.__cadCameraControls.push(this)}dispose(){}}',moduleType:'js'}
      if(id.endsWith('.css'))return{code:'',moduleType:'js'}
    },
  }]})
  await bundle.write({dir:directory,format:'esm'});await bundle.close()
  Viewport=(await import(pathToFileURL(resolve(directory,'CadModelViewport.js')).href)).default
}
test.after(async()=>{if(directory)await rm(directory,{recursive:true,force:true});delete globalThis.__cadCameraRenderer;delete globalThis.__cadCameraControls;delete globalThis.__cadCameraHooks})

function harness(initial){
  const names=['window','document','ResizeObserver','requestAnimationFrame','cancelAnimationFrame'],old=new Map(names.map(name=>[name,globalThis[name]]))
  globalThis.window={devicePixelRatio:1};globalThis.document=Object.assign(new Element(),{hidden:false})
  globalThis.ResizeObserver=class{observe(){}disconnect(){}};globalThis.requestAnimationFrame=()=>1;globalThis.cancelAnimationFrame=()=>{}
  const slots=[],effects=[],element=new Element();let cursor=0,dirty=true,props=initial,tree
  globalThis.__cadCameraHooks={
    useRef(value){const i=cursor++;if(!slots[i])slots[i]={current:value};return slots[i]},
    useState(value){const i=cursor++;if(!slots[i])slots[i]={value:typeof value==='function'?value():value};return[slots[i].value,next=>{next=typeof next==='function'?next(slots[i].value):next;if(!Object.is(next,slots[i].value)){slots[i].value=next;dirty=true}}]},
    useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||deps.some((value,j)=>!Object.is(value,old.deps[j]))){slots[i]={deps,cleanup:old?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn()})}},
  }
  function nodes(value=tree){return React.isValidElement(value)?[value,...React.Children.toArray(value.props.children).flatMap(nodes)]:[]}
  function render(){let n=0;do{dirty=false;cursor=0;tree=Viewport(props);for(const node of nodes())if(node.props.ref)node.props.ref.current=element;while(effects.length)effects.shift()();assert.ok(++n<30)}while(dirty)}
  async function flush(){for(let i=0;i<3;i++){await new Promise(resolve=>setTimeout(resolve,1));render()}}
  render()
  return{flush,element,async update(next){props={...props,...next};render();await flush()},
    button(label){const node=nodes().find(node=>node.type==='button'&&(node.props['aria-label']===label||node.props.children===label));assert.ok(node,label);return node},
    unmount(){slots.forEach(value=>value?.cleanup?.());for(const name of names)if(old.get(name)===undefined)delete globalThis[name];else globalThis[name]=old.get(name)},
  }
}
function framing(control,scope){return captureCadCamera(control.object,control.target,scope)}
function sameFraming(actual,expected){
  for(const key of ['position','quaternion','up','target'])actual[key].forEach((value,index)=>assert.ok(Math.abs(value-expected[key][index])<1e-10,`${key}[${index}]`))
  for(const key of ['zoom','left','right','top','bottom','near','far'])assert.equal(actual[key],expected[key],key)
}
function moveCamera(control){
  control.target.set(5,3,4);control.object.position.set(65,-70,55);control.object.up.set(0,0,1);control.object.zoom=2.75
  control.object.lookAt(control.target);control.object.updateProjectionMatrix();control.update()
}

test('same document preserves actual camera, target and zoom across input/result meshes, empty loading, and fallback GLB',async()=>{
  await load();const ui=harness({geometry:fixture.input,documentScope:'account:file-a'})
  try{
    await ui.flush();moveCamera(controls.at(-1));const before=framing(controls.at(-1),'account:file-a')
    for(const change of [{geometry:fixture.result,selectionGeometry:fixture.input},{geometry:fixture.input,selectionGeometry:undefined},{geometry:null},{geometry:fixture.result}]){
      const oldCount=controls.length
      await ui.update(change)
      assert.equal(controls.length,oldCount+Number(Boolean(change.geometry)))
      if(change.geometry)sameFraming(framing(controls.at(-1),'account:file-a'),before)
    }
    const bytes=await readFile(new URL('./fixtures/cadViewportBoxBore.glb',import.meta.url))
    const beforeFallback=controls.length
    await ui.update({geometry:null,fallback:{arrayBuffer:bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)}})
    assert.equal(controls.length,beforeFallback+1)
    sameFraming(framing(controls.at(-1),'account:file-a'),before)
    await ui.update({geometry:fixture.result,fallback:undefined,active:false});sameFraming(framing(controls.at(-1),'account:file-a'),before)
    await ui.update({active:true});sameFraming(framing(controls.at(-1),'account:file-a'),before)
    ui.button('适合窗口').props.onClick()
    assert.equal(controls.at(-1).object.zoom,1,'explicit fit still resets framing')
    assert.deepEqual(controls.at(-1).target.toArray(),[10,8,5])
  }finally{ui.unmount()}
})

test('switching document scope resets camera even with identical geometry or an empty intermediate document',async()=>{
  await load();const ui=harness({geometry:fixture.result,documentScope:'account:file-a'})
  try{
    await ui.flush();moveCamera(controls.at(-1))
    await ui.update({documentScope:'account:file-b'})
    assert.equal(controls.at(-1).object.zoom,1);assert.deepEqual(controls.at(-1).target.toArray(),[10,8,5])
    moveCamera(controls.at(-1));await ui.update({geometry:null,documentScope:'other-account:file-a'})
    await ui.update({geometry:fixture.input})
    assert.equal(controls.at(-1).object.zoom,1);assert.deepEqual(controls.at(-1).target.toArray(),[10,8,5])
  }finally{ui.unmount()}
})

test('callers without documentScope retain framing for their mount and new mounts start independently',async()=>{
  await load();let ui=harness({geometry:fixture.input})
  try{
    await ui.flush();moveCamera(controls.at(-1));const before=framing(controls.at(-1))
    await ui.update({geometry:fixture.result});sameFraming(framing(controls.at(-1)),before)
  }finally{ui.unmount()}
  ui=harness({geometry:fixture.result})
  try{await ui.flush();assert.equal(controls.at(-1).object.zoom,1);assert.deepEqual(controls.at(-1).target.toArray(),[10,8,5])}
  finally{ui.unmount()}
})

test('camera snapshots never restore another document and resize can retain vertical scale without re-fitting geometry',()=>{
  const camera=new THREE.OrthographicCamera(),bounds=new THREE.Box3(new THREE.Vector3(0,0,0),new THREE.Vector3(20,16,10))
  const target=fitCadCamera(camera,bounds,{view:'right',aspect:2});camera.zoom=4;camera.updateProjectionMatrix()
  const snapshot=captureCadCamera(camera,target,'a'),restored=new THREE.OrthographicCamera()
  assert.equal(restoreCadCamera(restored,snapshot,'b'),null)
  const nextTarget=restoreCadCamera(restored,snapshot,'a');sameFraming(captureCadCamera(restored,nextTarget,'a'),snapshot)
  restored.left=-(restored.top-restored.bottom)/4;restored.right=-restored.left;restored.updateProjectionMatrix()
  assert.equal(restored.zoom,4);assert.equal(restored.top,snapshot.top);assert.deepEqual(restored.position.toArray(),snapshot.position)
})
