import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as ProjectStore from './projectStore.js'
import * as session from './cadDocumentSession.js'
import { createManualFeatureReference, applyManualFeatureReference } from './manualFeatureReference.js'

const accountKey='document-account'
const storage=()=>{const data=new Map();return {getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value)}}
const fresh=()=>ProjectStore.loadProjectStore(storage())
const add=(store,name,snapshot=session.emptyCadDocumentSnapshot(),type='零件')=>ProjectStore.createProjectFile(store,store.activeProjectId,{name,snapshot,type})
const source=store=>({accountKey,projectId:store.activeProjectId,fileId:store.activeFileId})
const plan={version:'cad-plan-v1',units:'mm',name:'真实特征',parameters:{},features:[{id:'body',op:'box',size:[40,20,10]}],result:'body'}

test('an uncommitted empty CAD document survives persistence and first save with the same file session',()=>{
  const disk=storage();let store=add(fresh(),'空 CAD'),binding=source(store)
  const first=session.cadDocumentTarget(store,binding)
  assert.equal(first.status,'empty');assert.deepEqual(first.initialDraft.plan.features,[]);assert.equal(first.initialDraft.name,'空 CAD')
  ProjectStore.persistProjectStore(store,disk);store=ProjectStore.loadProjectStore(disk)
  assert.equal(ProjectStore.getActiveFile(store).snapshot.cadEditorDocument,true)
  assert.equal(ProjectStore.getActiveFile(store).snapshot.activeMode,'特征编辑')
  assert.deepEqual(session.cadDocumentTarget(store,binding),first)
  const ref=createManualFeatureReference({id:'feature_empty_saved',fileId:binding.fileId,revision:1,status:'draft',name:'首次草稿'},binding)
  store=applyManualFeatureReference(store,ref,{accountKey});ProjectStore.persistProjectStore(store,disk)
  const reopened=session.cadDocumentTarget(ProjectStore.loadProjectStore(disk),binding)
  assert.equal(reopened.initialPlanKey,first.initialPlanKey);assert.equal(reopened.savedFeatureId,ref.featureId)
  assert.equal(reopened.reference.status,'draft');assert.equal(reopened.fileId,binding.fileId)
})

test('project tabs include only real editable files and reject corrupt or foreign references without falling back',()=>{
  let store=fresh();const names=['空 CAD','AI 特征','保存的手工']
  store=add(store,names[0]);store=add(store,names[1],{model:{kind:'feature_model',cadPlan:plan}})
  store=add(store,names[2]);const savedSource=source(store)
  const ref=createManualFeatureReference({id:'feature_stored',fileId:savedSource.fileId,revision:2,status:'built'},savedSource)
  store=applyManualFeatureReference(store,ref,{accountKey})
  store=add(store,'普通空文件',{});store=add(store,'只有预览',{model:{kind:'shaft',length:20}})
  store=add(store,'坏计划',{model:{cadPlan:{...plan,features:[{id:'unsupported',op:'guess'}]}}})
  store=add(store,'文档',session.emptyCadDocumentSnapshot(),'文档')
  store=add(store,'失配的引用',{...session.emptyCadDocumentSnapshot(),model:{cadPlan:plan},manualFeatureReference:ref})
  const mismatch=source(store)
  assert.equal(session.cadDocumentTarget(store,mismatch),null)
  assert.equal(session.cadDocumentTarget(store,{...savedSource,accountKey:'other-account'}),null)
  const unavailable=add(store,'不可读取');unavailable.projects[0].files.at(-1).contentUnavailable=true;store=unavailable
  const before=JSON.stringify(store)
  assert.deepEqual(session.cadProjectDocuments(store,{accountKey,projectId:store.activeProjectId}).map(item=>item.name),names)
  assert.equal(JSON.stringify(store),before)
  assert.deepEqual(session.cadProjectDocuments(store,{accountKey,projectId:'missing'}),[])
  assert.equal(session.matchesCadDocumentSource({...savedSource,accountKey:'forged'},{accountKey,projectId:savedSource.projectId}),false)
})

const app=readFileSync(new URL('./App.jsx',import.meta.url),'utf8')
const section=(from,to)=>{const start=app.indexOf(from),end=app.indexOf(to,start);assert.ok(start>=0&&end>start);return app.slice(start,end)}
function appHarness(){
  const state={store:add(fresh(),'原图'),target:null,mode:'',flushes:0,notices:[],allowed:true}
  let context
  const restore=(next,mode)=>{state.store=next;state.mode=mode;context.workspaceStore=next;context.storeRef.current=next}
  context=vm.createContext({...session,ProjectStore,accountKey,workspaceStore:state.store,storeRef:{current:state.store},editingTarget:null,
    canSwitch:()=>state.allowed,flushWorkspace:()=>{state.flushes++;return state.store},restoreWorkspace:restore,setActiveCase:()=>{},
    setEditingTarget:target=>{state.target=target;context.editingTarget=target},showToast:(...args)=>state.notices.push(args)})
  vm.runInContext(`${section('  const createCadDocument = () => {','  const duplicateLocalFile = ')}\nObject.assign(globalThis,{create:createCadDocument,open:openCadDocument})`,context)
  return {context,state}
}
test('actual App new/open handlers persist the empty marker and recheck tab ownership and file existence',()=>{
  const {context,state}=appHarness(),oldFile=state.store.activeFileId,projectId=state.store.activeProjectId
  assert.equal(context.create(),true)
  assert.notEqual(state.target.fileId,oldFile);assert.equal(state.target.fileId,state.store.activeFileId)
  assert.equal(ProjectStore.getActiveFile(state.store).snapshot.cadEditorDocument,true)
  assert.equal(state.mode,'特征编辑');assert.deepEqual(state.target.initialDraft.plan.features,[])
  const first=state.target.fileId;assert.equal(context.create(),true);const second=state.target.fileId
  assert.notEqual(first,second)
  const flushes=state.flushes
  for(const tab of [{accountKey:'other',projectId,fileId:first},{accountKey,projectId:'another',fileId:first},{accountKey,projectId,fileId:'deleted'}])assert.equal(context.open(tab),false)
  assert.equal(state.flushes,flushes);assert.equal(state.target.fileId,second)
  assert.equal(context.open({accountKey,projectId,fileId:first}),true)
  assert.equal(state.target.fileId,first);assert.equal(state.store.activeFileId,first)
  state.allowed=false;assert.equal(context.create(),false);assert.equal(context.open({accountKey,projectId,fileId:second}),false)
  const snapshot=ProjectStore.getActiveFile(state.store).snapshot
  const init=vm.createContext({...session,initialSnapshot:snapshot,initialStoreRef:{current:state.store},account:{session:{user:{id:accountKey}}},activeFile:ProjectStore.getActiveFile(state.store),
    useState:fn=>[typeof fn==='function'?fn():fn,()=>{}]})
  vm.runInContext(`${section('  const [editingTarget, setEditingTarget] = useState(() => {','  const [showOriginalModel, setShowOriginalModel]')}\nglobalThis.restored=editingTarget`,init)
  assert.equal(init.restored.fileId,first);assert.equal(init.restored.initialPlanKey,state.target.initialPlanKey)
})
