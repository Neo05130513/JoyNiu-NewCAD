import {cadAnnotatedSvg} from './cadAnnotationExport.js'
import CadReferencePanel from './CadReferencePanel.jsx'
import CadPmiPanel from './CadPmiPanel.jsx'
import CadPdmPanel from './CadPdmPanel.jsx'
import CadCommunityPanel from './CadCommunityPanel.jsx'
import CadImportBodyPanel from './CadImportBodyPanel.jsx'
import CadStandardPartPanel from './CadStandardPartPanel.jsx'
import { bodyStateFor, editorEntityId, independentSketchFeature, maskCadBodies, patchBodyState, referenceFrames, resolvedPmi, resolvedConstruction, removeIndependentSketch } from './cadEditorEntities.js'
import { bodyStatesAfterOperation, removeCadReference } from './cadEditorDeletion.js'
import './cad-editor-entities.css'
import { useEffect, useMemo, useRef, useState } from 'react'
import CadModelViewport from './CadModelViewport.jsx'
import CadEditorIcon from './CadEditorIcons.jsx'
import {validateSketchContours} from './featureSketchModel.js'
import FeatureSketchEditor from './FeatureSketchEditor.jsx'
import { FeatureEditor, ParameterEditor } from './CadFeatureFields.jsx'
import { beginCadEdit, beginCadProfileTool, beginCadTool, cadFeatureTree, cadTransactionDraft, changeCadProfileOperation, patchCadTransaction, requireCadFeatureInputs, cadSketchFeature, patchCadSketch } from './cadEditorTransactions.js'
import { cloneFeatureValue, deleteFeature, featureNames, moveFeature } from './directFeatureModel.js'
import { cadPlaneForFace } from './cadModelViewport.js'
import useCadPreview from './useCadPreview.js'
import { cadPreviewQueue } from './cadPreviewQueue.js'
import './cad-editor-surface.css'

const faceOperations=['shell','surface_offset','move_face','surface_thicken']
const edgeOperations=['fillet','chamfer','surface_boundary']
const planes = [['XY','上视基准面'],['XZ','前视基准面'],['YZ','右视基准面']]
const operationIcons = { profile_extrude:'extrude',profile_revolve:'extrude',fillet:'fillet',chamfer:'fillet',shell:'cube',linear_pattern:'pattern',circular_pattern:'pattern',mirror:'mirror',union:'boolean',cut:'boolean',intersect:'boolean',gear:'gear',spring:'spring',sweep:'surface',loft:'surface' }
function Tool({icon,label,onClick,disabled,children}) {
  return <div className="ce-tool"><button disabled={disabled} onClick={onClick} title={label}><CadEditorIcon name={icon}/><span>{label}</span></button>{children&&<details className="ce-tool-menu" open={disabled?false:undefined}><summary aria-label={`${label}更多工具`} aria-disabled={Boolean(disabled)} tabIndex={disabled?-1:0} onClick={event=>{if(disabled)event.preventDefault()}}>⌄</summary><div>{children}</div></details>}</div>
}
function ToolItem({children,onClick,disabled}) { return <button disabled={disabled} onClick={event=>{if(disabled){event.preventDefault();return}event.currentTarget.closest('details').open=false;onClick()}}>{children}</button> }
function Panel({title,children,onClose,className=''}) { return <section className={`ce-floating ${className}`} aria-label={title}><header><b>{title}</b><button aria-label={`关闭${title}`} onClick={onClose}>×</button></header>{children}</section> }
const number = value => Number.isFinite(value) ? Number(value.toFixed(3)) : '—'
const selectionFromRefs = feature => {
  const kind = faceOperations.includes(feature?.op) ? 'face' : 'edge', refs = faceOperations.includes(feature?.op) ? feature.faces : feature?.edges
  return Array.isArray(refs) ? refs.filter(ref=>ref?.kind===kind).map(selector=>({kind,id:`${kind}:${selector.index}`,index:selector.index,selector:cloneFeatureValue(selector)})) : []
}
const matchingSelections = (items, geometry) => {
  if(!geometry)return []
  return items.filter(item=>{
    const actual=(item.kind==='face'?geometry.faces:item.kind==='edge'?geometry.edges:[])?.find(value=>value.id===item.id)
    return actual && item.selector?.geometryVersion===geometry.geometryVersion && item.selector?.sourceFeatureId===geometry.sourceFeatureId && item.selector?.signature===actual.selector.signature
  })
}

export default function CadEditorSurface({draft,record,token,scope,active=true,busy,error,errorDetails,unavailable,dirty,selected,onSelected,onEdit,onComplete,onSaveDraft,onOpenPdm,onOpenCommunity,onSave,onUndo,onRedo,canUndo,canRedo,onBack,onNavigate,onNewDocument,onOpenDocument,documents,onLoad,onRetry,onReload,saved=[],versions=[],onRestore,onDownload,sourceLabel,sourceFileId,sourceProjectId,accountName,creditBalance,fallback}) {
  const [entityPanel,setEntityPanel]=useState(null),[pmiPicks,setPmiPicks]=useState([]),[bodySelection,setBodySelection]=useState([])
  const [referenceVisibility,setReferenceVisibility]=useState({}),[selectedReference,setSelectedReference]=useState(null)
  const captureView=useRef(null)
  const [sketchToolbarHost,setSketchToolbarHost] = useState(null)
  const [transaction,setTransaction] = useState(null), [panel,setPanel] = useState(''), [selection,setSelection] = useState([])
  const [view,setView] = useState('iso'), [plane,setPlane] = useState('XY'), [pendingSketch,setPendingSketch] = useState(false)
  const [selectedSketchId,setSelectedSketchId] = useState(null)
  const [localError,setLocalError] = useState(''), [appearance,setAppearance] = useState('shaded'), [treeOpen,setTreeOpen] = useState(true)
  const [expanded,setExpanded] = useState({}), [measurePoints,setMeasurePoints] = useState([]), [formulaDraft,setFormulaDraft] = useState(null)
  const [context,setContext] = useState(null), [confirmLeave,setConfirmLeave] = useState(null)
  const [finishing,setFinishing] = useState(false), [awaitingPreview,setAwaitingPreview] = useState(false), completion=useRef(null), scopeEpoch=useRef(0)
  const transactionRef=useRef(transaction); transactionRef.current=transaction
  const completionContext=useRef(null);completionContext.current={onComplete,onSaveDraft,active,authenticated:Boolean(token)}
  const currentFeature=transaction?.draft.plan.features.find(item=>item.id===transaction.id)
  const missingInputs=(()=>{try{requireCadFeatureInputs(currentFeature,transaction?.draft.plan.parameters);return ''}catch(reason){return reason.message}})()
  const editorSketch=cadSketchFeature(transaction)
  const sketch=transaction?.stage==='sketch'
  const locked=Boolean(busy||unavailable||finishing)
  useEffect(()=>{scopeEpoch.current+=1;completion.current?.abort.abort();completion.current=null;setFinishing(false);setAwaitingPreview(false);setTransaction(null);setPanel('');setEntityPanel(null);setPmiPicks([]);setBodySelection([]);setReferenceVisibility({});setSelectedReference(null);setSelection([]);setSelectedSketchId(null);setPendingSketch(false);setLocalError('');setExpanded({});setContext(null);setFormulaDraft(null);setConfirmLeave(null);setMeasurePoints([]);return()=>completion.current?.abort.abort()},[scope])
  useEffect(()=>{if(!active||!token){completion.current?.abort.abort();completion.current=null;setFinishing(false);setAwaitingPreview(false)}},[active,Boolean(token)])
  const candidate=useMemo(()=>{
    try {return transaction ? {draft:cadTransactionDraft(transaction),error:''} : {draft,error:''}}
    catch(reason){return {draft:null,error:reason.message}}
  },[transaction,draft])
  const baseDraft=transaction?.base||draft
  const selectedInput=currentFeature?.input || transaction?.baseId
  const base=useCadPreview({draft:baseDraft,featureId:selectedInput===baseDraft.plan.result?undefined:selectedInput,token,record,scope,active,enabled:!unavailable,paused:finishing,delay:0,queue:cadPreviewQueue})
  const waitingForSelection = Boolean(currentFeature && (edgeOperations.includes(currentFeature.op) && Array.isArray(currentFeature.edges) && !currentFeature.edges.length || faceOperations.includes(currentFeature.op) && !currentFeature.faces?.length))
  const result=useCadPreview({draft:candidate.draft,token,record,scope,active,enabled:Boolean(transaction&&!sketch&&!unavailable&&!candidate.error&&!waitingForSelection&&!missingInputs),paused:finishing,delay:600,queue:cadPreviewQueue})
  const displayed=transaction&&!sketch&&result.current ? result.geometry : base.current ? base.geometry : null
  const inspectedCount=displayed?.inspection?.solidCount??(!transaction&&!dirty?record?.inspection?.solidCount:undefined)
  const bodyCount=Number.isSafeInteger(inspectedCount)&&inspectedCount>=0?inspectedCount:null
  const previewError=candidate.error||(transaction&&!sketch?result.error:base.error)
  const previewLoading=transaction&&!sketch?result.loading:base.loading
  const tree=cadFeatureTree(transaction?.draft||draft)
  const available=!(transaction||locked||formulaDraft||entityPanel)
  const run=action=>{try{setLocalError('');setContext(null);action()}catch(reason){setLocalError(reason.message)}}
  const cancel=()=>{completion.current?.abort.abort();completion.current=null;setFinishing(false);setAwaitingPreview(false);setTransaction(null);setSelection([]);setLocalError('');setPendingSketch(false);setPanel('');setEntityPanel(null);setPmiPicks([]);setFormulaDraft(null)}
  const cancelLocked=Boolean(busy||unavailable||finishing&&!awaitingPreview)
  const completionLabel=finishing?(awaitingPreview?'等待预览完成…':'正在完成…'):(base.loading||result.loading?'完成（等待预览）':'完成')
  const showPanel=next=>{if(formulaDraft){setLocalError('请先完成或取消方程式编辑。');return}setPanel(next)}
  const navigate=action=>{if(locked||completion.current){setLocalError('当前操作仍在处理，请稍候。');return}if(transaction||formulaDraft||entityPanel){setConfirmLeave(()=>action);return}action?.()}
  const editFeature=(id,stage='feature')=>run(()=>{
    if(!available)return
    const next=beginCadEdit(draft,id,stage)
    next.selection=selectionFromRefs(next.draft.plan.features.find(item=>item.id===id))
    setTransaction(next);onSelected(id);setSelection(next.selection);setPanel('');setPendingSketch(false)
    setExpanded(old=>({...old,[id]:true}))
  })
  const startTool=(op,options={})=>run(()=>{
    if(!available)return
    const profileEntry=['profile_extrude','profile_revolve','profile_sweep'].includes(op)&&!options.empty,frame=profileEntry?(cadPlaneForFace(selectedPlanarFace)||(!planes.some(([key])=>key===plane)?referenceFrames(draft.plan)[plane]:null)):null
    if(profileEntry&&selectedPlanarFace&&!frame)throw new Error('请选择模型平面或基准面，再创建草图。')
    const next=profileEntry?beginCadProfileTool(draft,op,{sketchId:selectedSketchId,plane:frame?'custom':plane,frame,planeSource:frame?selectedPlanarFace?.selector:undefined}):beginCadTool(draft,op,options)
    if(next.isNew&&frame&&!selectedPlanarFace&&!planes.some(([key])=>key===plane))next.draft.plan.features.at(-1).planeReference=plane
    if(next.isNew&&op.startsWith('profile_'))next.combine=draft.plan.result?'union':'new'
    if(options.empty){const item=next.draft.plan.features.at(-1);item.start=[0,0];item.segments=[]}
    if(edgeOperations.includes(op)){
      const edges=matchingSelections(selection,base.current?base.geometry:null).filter(item=>item.kind==='edge'&&item.selector?.sourceFeatureId===next.baseId)
      next.draft.plan.features.at(-1).edges=edges.map(item=>item.selector);next.selection=edges
    }
    if(faceOperations.includes(op)){const faces=matchingSelections(selection,base.current?base.geometry:null).filter(item=>item.kind==='face'&&item.selector?.sourceFeatureId===next.baseId);next.draft.plan.features.at(-1).faces=faces.map(item=>item.selector);next.selection=faces}
    setTransaction(next);onSelected(next.id);setSelection(next.selection);setPanel('');setPendingSketch(false);setExpanded(old=>({...old,[next.id]:true}))
  })
  const selectedPlanarFace=base.current?base.geometry?.faces?.find(face=>matchingSelections(selection,base.geometry).some(value=>value.kind==='face'&&value.id===face.id)):null
  const startIndependentSketch=(frame,face,reference)=>run(()=>{
    if(!available && !pendingSketch)return
    const next=beginCadTool(draft,'profile_extrude',frame?{stage:'sketch',plane:'custom',frame,planeSource:face?.selector}:{stage:'sketch',plane})
    next.kind='independentSketch';next.sketchId=editorEntityId('sketch',draft.plan.sketches)
    const feature=next.draft.plan.features.at(-1);feature.label=`草图 ${next.sketchId.replace('sketch','')}`;if(reference)feature.planeReference=reference
    setTransaction(next);setPanel('');setEntityPanel(null);setPendingSketch(false)
  })
  const startSketch=(frame,face)=>startIndependentSketch(frame,face)
  const editIndependentSketch=id=>run(()=>{
    if(!available)return
    const source=draft.plan.sketches.find(item=>item.id===id),next=beginCadTool(draft,'profile_extrude',{stage:'sketch'})
    const feature={...independentSketchFeature(source),id:next.id};next.draft.plan.features[next.draft.plan.features.length-1]=feature
    next.kind='independentSketch';next.sketchId=id;setTransaction(next);setEntityPanel(null);setPanel('')
  })
  const chooseSketch=()=>{
    if(!available)return
    const frame=cadPlaneForFace(selectedPlanarFace)
    if(frame)startSketch(frame,selectedPlanarFace)
    else{setPendingSketch(true);setPanel('plane');setSelection([])}
  }
  const changeFeature=(feature,parameters)=>{
    if(locked)return
    setLocalError('')
    if(currentFeature?.input!==feature.input&&[...edgeOperations,...faceOperations].includes(feature.op)){
      feature={...feature,[faceOperations.includes(feature.op)?'faces':'edges']:[]};setSelection([])
    }
    setTransaction(old=>old?patchCadTransaction(old,feature,parameters):old)
  }
  const editSketchSection=(index,nextFeature)=>{if(locked)return;setTransaction(old=>{if(!old)return old;const next=nextFeature?patchCadTransaction(old,nextFeature):old;return {...next,stage:'sketch',sectionIndex:Number.isSafeInteger(index)?index:undefined}})}
  const changeProfileOperation=op=>run(()=>{
    if(locked)return
    setTransaction(changeCadProfileOperation(transaction,op))
  })
  const completeSketch=()=>run(()=>{if(locked)return;validateSketchContours(editorSketch,transaction?.draft.plan.parameters);if(transaction.kind==='independentSketch'){finish();return}setTransaction(old=>({...old,stage:'feature'}))})
  const commitDraft=async next=>{
    if(locked||!active||!token||completion.current)return false
    const operation={epoch:scopeEpoch.current,abort:new AbortController()};completion.current=operation;setFinishing(true);setAwaitingPreview(true);setLocalError('')
    try{
      const completed=await cadPreviewQueue.commit(()=>{
        if(operation.abort.signal.aborted||completion.current!==operation||scopeEpoch.current!==operation.epoch||!completionContext.current.active||!completionContext.current.authenticated)return null
        setAwaitingPreview(false)
        return next.plan.features.length?completionContext.current.onComplete(next):completionContext.current.onSaveDraft(next)
      },operation.abort.signal)
      if(completed&&completion.current===operation&&scopeEpoch.current===operation.epoch)cancel()
      return completed||false
    }catch(reason){if(completion.current===operation&&scopeEpoch.current===operation.epoch)setLocalError(reason.message);return false}
    finally{if(completion.current===operation){completion.current=null;setFinishing(false);setAwaitingPreview(false)}}
  }
  const finish=async()=>{
    if(!transaction&&!formulaDraft)return
    try{
      if(transaction)requireCadFeatureInputs(currentFeature,transaction.draft.plan.parameters)
      if(waitingForSelection){setLocalError('请在模型上选择要编辑的边或面。');return}
      const next=formulaDraft?{...cloneFeatureValue(draft),plan:{...cloneFeatureValue(draft.plan),parameters:formulaDraft},changeNote:'修改方程式和驱动尺寸'}:cadTransactionDraft(transaction)
      if(currentFeature?.op==='body_edit') {
        const source=base.current&&base.geometry?.sourceFeatureId===currentFeature.input?base.geometry:transaction.bodyStateSource?.sourceFeatureId===currentFeature.input?transaction.bodyStateSource:null
        next.plan=bodyStatesAfterOperation(next.plan,currentFeature,source?.bodies,transaction.base.plan.bodyStates)
      }
      return await commitDraft(next)
    }catch(reason){setLocalError(reason.message)}
  }
  const saveEntity=async(key,value,label)=>{
    const next=cloneFeatureValue(draft);next.plan[key]=(next.plan[key]||[]).some(item=>item.id===value.id)?next.plan[key].map(item=>item.id===value.id?value:item):[...(next.plan[key]||[]),value];next.changeNote=label
    return commitDraft(next)
  }
  const selectBody=body=>{if(!available)return;setBodySelection([body]);setSelection(base.geometry.faces.filter(face=>body.faceIds.includes(face.id)).map(face=>({kind:'face',id:face.id,index:face.selector.index,selector:face.selector})));setPanel('body')}
  const bodyTool=action=>run(()=>{if(!bodySelection.length||!available)return;if(!base.current||base.geometry?.sourceFeatureId!==draft.plan.result)throw new Error('实体预览已变化，请等待当前模型更新后重新选择。');const next=beginCadTool(draft,'body_edit');const feature=next.draft.plan.features.at(-1);feature.action=action;feature.bodies=bodySelection.map(({index,signature})=>({index,signature}));if(action==='rotate'){delete feature.vector;Object.assign(feature,{axisStart:[0,0,0],axisEnd:[0,0,1],angle:0})}else if(['keep','delete'].includes(action))delete feature.vector;next.bodyStateSource={sourceFeatureId:base.geometry.sourceFeatureId,bodies:cloneFeatureValue(base.geometry.bodies)};next.draft.plan=bodyStatesAfterOperation(next.draft.plan,feature,next.bodyStateSource.bodies);setPanel('');setTransaction(next)})
  const styledDisplay=useMemo(()=>maskCadBodies(displayed,draft.plan.bodyStates),[displayed,draft.plan.bodyStates])
  const styledSource=useMemo(()=>base.current?maskCadBodies(base.geometry,draft.plan.bodyStates):null,[base.current,base.geometry,draft.plan.bodyStates])
  const pmi=useMemo(()=>resolvedPmi(entityPanel?.kind==='pmi'&&entityPanel.preview?{...draft.plan,annotations:[...(draft.plan.annotations||[]).filter(n=>n.id!==entityPanel.preview.id),entityPanel.preview]}:draft.plan),[draft.plan.annotations,draft.plan.parameters,entityPanel?.preview])
  const allConstruction=useMemo(()=>{try{const plan=entityPanel?.preview&&['plane','axis','point','helix'].includes(entityPanel.kind)?{...draft.plan,references:(draft.plan.references||[]).some(r=>r.id===entityPanel.preview.id)?draft.plan.references.map(r=>r.id===entityPanel.preview.id?entityPanel.preview:r):[...(draft.plan.references||[]),entityPanel.preview]}:draft.plan;return resolvedConstruction(plan)}catch{return []}},[draft.plan.references,draft.plan.parameters,entityPanel?.preview])
  const referenceIsVisible=id=>Boolean(referenceVisibility[id]||selectedReference===id||entityPanel&&['plane','axis','point','helix'].includes(entityPanel.kind)&&(entityPanel.preview?.id||entityPanel.initial?.id)===id)
  const construction=allConstruction.filter(ref=>referenceIsVisible(ref.id))
  const toggleReference=id=>{if(!available)return;const visible=referenceIsVisible(id);setReferenceVisibility(previous=>({...previous,[id]:!visible}));if(visible)setSelectedReference(current=>current===id?null:current)}
  const exportPmi=()=>run(()=>{if(!captureView.current)throw new Error('画布尚未就绪。');const svg=cadAnnotatedSvg({...captureView.current(),title:draft.name||'零件 PMI'}),url=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=`${draft.name||'零件'}-PMI.svg`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)})
  const deleteEntity=(key,id)=>run(()=>{if(locked||transaction||formulaDraft||entityPanel&&!(key==='annotations'&&entityPanel.kind==='pmi'&&entityPanel.initial?.id===id))return;const next=cloneFeatureValue(draft);if(key==='sketches')next.plan=removeIndependentSketch(next.plan,id);else if(key==='references')next.plan=removeCadReference(next.plan,id);else next.plan[key]=(next.plan[key]||[]).filter(item=>item.id!==id);next.changeNote=key==='sketches'?'删除独立草图':key==='references'?'删除参考几何':'删除标注';void commitDraft(next).then(completed=>{if(!completed)return;if(key==='sketches')setSelectedSketchId(current=>current===id?null:current);if(key==='references'){setPlane(current=>current===id?'XY':current);setSelectedReference(current=>current===id?null:current);setReferenceVisibility(previous=>{const next={...previous};delete next[id];return next})}})})
  useEffect(()=>{
    if(!active)return
    const key=event=>{
      if(event.defaultPrevented)return
      if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='s'){event.preventDefault();event.stopPropagation();if(transactionRef.current||formulaDraft||entityPanel)setLocalError('请先完成当前操作，再保存模型。');else if(!locked)onSave(true)}
      if(event.key==='Escape'&&!cancelLocked){event.preventDefault();cancel();setContext(null)}
      if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='z'&&!locked&&!formulaDraft&&!transactionRef.current&&!['INPUT','TEXTAREA','SELECT'].includes(event.target?.tagName)){
        event.preventDefault();event.shiftKey?onRedo():onUndo()
      }
    }
    const unload=event=>{if(transactionRef.current||formulaDraft||entityPanel){event.preventDefault();event.returnValue=''}}
    window.addEventListener('keydown',key);window.addEventListener('beforeunload',unload);return()=>{window.removeEventListener('keydown',key);window.removeEventListener('beforeunload',unload)}
  },[active,locked,cancelLocked,onUndo,onRedo,onSave,formulaDraft,entityPanel])
  const onPick=(items,hit)=>{
    if(locked||!base.current||sketch)return
    setSelectedReference(null);setSelectedSketchId(null)
    items=matchingSelections(items,base.geometry)
    setSelection(items)
    const validHit=hit&&matchingSelections([hit],base.geometry).length?hit:null
    if(transaction?.pickingPath&&validHit?.worldPoint){const path=cloneFeatureValue(currentFeature.path),point=validHit.worldPoint;if(!transaction.pathPicked){path.start=point;path.segments=[]}else path.segments.push({type:'line',to:point});delete path.sketchId;setTransaction(old=>({...patchCadTransaction(old,{...currentFeature,path}),pathPicked:true}));return}
    if(['pmi','plane','axis','point'].includes(entityPanel?.kind)&&validHit?.worldPoint)setPmiPicks(points=>[...points.slice(-3),validHit])
    if(panel==='measure'&&validHit?.worldPoint)setMeasurePoints(points=>[...points.slice(-1),validHit.worldPoint])
    if(pendingSketch&&items.length===1&&validHit?.kind==='face'){
      const face=base.geometry.faces.find(value=>value.id===validHit.id),frame=cadPlaneForFace(face)
      if(frame){startSketch(frame,face);return}
      setLocalError('请选择平面，曲面不能直接创建平面草图。')
    }
    if(transaction&&[...edgeOperations,...faceOperations].includes(currentFeature.op)){
      const kind=faceOperations.includes(currentFeature.op)?'face':'edge',key=kind==='face'?'faces':'edges'
      setTransaction(old=>({...patchCadTransaction(old,{...currentFeature,[key]:items.filter(item=>item.kind===kind).map(item=>item.selector)}),selection:items}))
    }
  }
  const removeReference=index=>{
    if(locked||!transaction)return
    const key=faceOperations.includes(currentFeature.op)?'faces':'edges',ref=currentFeature[key][index]
    setTransaction(old=>patchCadTransaction(old,{...currentFeature,[key]:currentFeature[key].filter((_value,i)=>i!==index)}))
    setSelection(items=>items.filter(item=>JSON.stringify(item.selector)!==JSON.stringify(ref)))
  }
  const selectedRefs=faceOperations.includes(currentFeature?.op)?currentFeature.faces:currentFeature?.edges
  const pickKind=sketch||!base.current?'none':currentFeature&&edgeOperations.includes(currentFeature.op)?'edge':faceOperations.includes(currentFeature?.op)||pendingSketch?'face':'all'
  const editorName=(sourceLabel||draft.name||'未命名零件').replace(/ · 编辑模型$/,'')
  const actions = [
    ['extrude','拉伸','profile_extrude',[['profile_revolve','旋转'],['profile_sweep','扫掠'],['profile_loft','放样'],['box','长方体'],['cylinder','圆柱体']]],
    ['fillet','圆角','fillet',[['chamfer','倒角'],['shell','抽壳']]],
    ['pattern','线性阵列','linear_pattern',[['circular_pattern','圆形阵列'],['mirror','镜像']]],
    ['boolean','布尔运算','union',[['cut','移除'],['intersect','相交']]],
    ['gear','齿轮','gear',[['spring','弹簧']]],
  ]
  const referenceRow=ref=><div className="ce-entity-list-row" key={ref.id}><button className="ce-tree-row" onClick={()=>{if(!available&&!pendingSketch)return;setSelectedReference(ref.id);if(ref.kind==='plane')setPlane(ref.id);setSelectedSketchId(null);if(pendingSketch&&ref.kind==='plane')startIndependentSketch(referenceFrames(draft.plan)[ref.id],null,ref.id)}} onDoubleClick={()=>available&&setEntityPanel({kind:ref.kind,initial:ref})} title="双击编辑参考几何"><CadEditorIcon name={ref.kind==='helix'?'spring':'plane'} size={15}/>{ref.label||ref.id}</button><button aria-label={`${referenceIsVisible(ref.id)?'隐藏':'显示'}参考几何 ${ref.id}`} aria-pressed={referenceIsVisible(ref.id)} disabled={!available} onClick={()=>toggleReference(ref.id)}>{referenceIsVisible(ref.id)?'◉':'○'}</button><button aria-label={`编辑参考几何 ${ref.id}`} disabled={!available} onClick={()=>available&&setEntityPanel({kind:ref.kind,initial:ref})}>✎</button><button aria-label={`删除参考几何 ${ref.id}`} disabled={!available} onClick={()=>deleteEntity('references',ref.id)}>×</button></div>
  const surfaces=base.current&&Array.isArray(base.geometry?.surfaceBodies)?base.geometry.surfaceBodies:null
  const selectSurface=surface=>{if(!available||!base.current)return;onPick(base.geometry.faces.filter(face=>surface.faceIds.includes(face.id)).map(face=>({kind:'face',id:face.id,selector:face.selector})),null)}
  const editSurface=()=>{const source=draft.plan.features.find(feature=>feature.id===base.geometry?.sourceFeatureId);if(available&&source?.op.startsWith('surface_'))editFeature(source.id)}
  return <section className={`cad-editor ${sketch?'ce-is-sketch':''} ${treeOpen?'':'ce-tree-collapsed'}`} aria-label="三维零件编辑器">
    <header className="ce-header"><button className="ce-brand" aria-label="返回云管理界面" onClick={()=>navigate(onBack)}><CadEditorIcon name="cube" size={23}/><strong>JoyNiu<span> CAD</span></strong></button><span className="ce-document-name">{editorName}</span><button className="ce-back" onClick={()=>navigate(onBack)}>〈 返回云管理界面</button><div className="ce-header-spacer"/><span className="ce-credit">✦ {creditBalance??'—'}</span><button className="ce-account" title={accountName||'当前账号'} onClick={()=>navigate(()=>onNavigate?.('账号'))}>{(accountName||'J').slice(0,1)}</button><button className="ce-checkin" disabled={locked} onClick={()=>navigate(async()=>{if(dirty){const saved=await onSave(true);if(!saved)return}onBack?.()})}>退出编辑</button><button onClick={()=>showPanel(panel==='settings'?'':'settings')}>⚙ 设置</button></header>
    <div className="ce-ribbon" role="toolbar" aria-label={sketch?'草图操作':'模型编辑工具'}>
      {!sketch&&<div className="ce-history"><button disabled={!available||!canUndo} onClick={onUndo}><CadEditorIcon name="undo" size={17}/>撤销</button><button disabled={!available||!canRedo} onClick={onRedo}><CadEditorIcon name="redo" size={17}/>重做</button></div>}
      {sketch?<><Tool icon="sketch" label="完成草图" disabled={locked} onClick={completeSketch}/><button className="ce-cancel-sketch" disabled={locked} onClick={cancel}>取消</button><div className="ce-sketch-toolbar-host" ref={setSketchToolbarHost}/></>:<>
        <Tool icon="sketch" label="新建草图" disabled={!available} onClick={chooseSketch}/>
        {actions.map(([icon,label,op,items])=><Tool key={op} icon={icon} label={label} disabled={!available} onClick={()=>startTool(op)}>{items.map(([value,text])=><ToolItem key={value} disabled={!available} onClick={()=>startTool(value)}>{text}</ToolItem>)}</Tool>)}
        <Tool icon="plane" label="平面" disabled={!available} onClick={()=>{setEntityPanel({kind:'plane'});setPmiPicks([]);setPendingSketch(false)}}>{['axis','point'].map(kind=><ToolItem key={kind} onClick={()=>{setEntityPanel({kind});setPmiPicks([])}}>{kind==='axis'?'轴':'点'}</ToolItem>)}</Tool><Tool icon="surface" label="偏移曲面" disabled={!available} onClick={()=>startTool('surface_offset')}>{[['move_face','移动面'],['surface_boundary','边界混合'],['surface_style','样式曲面'],['surface_join','曲面连接'],['surface_thicken','曲面加厚']].map(([op,label])=><ToolItem key={op} disabled={!available} onClick={()=>startTool(op)}>{label}</ToolItem>)}</Tool>
        <Tool icon="spring" label="螺旋线" disabled={!available} onClick={()=>{setEntityPanel({kind:'helix'});setPmiPicks([])}}/><Tool icon="cube" label="导入实体" disabled={!available} onClick={()=>setEntityPanel({kind:'import'})}/><Tool icon="cube" label="标准件" disabled={!available} onClick={()=>setEntityPanel({kind:'standard'})}/>
        <Tool icon="measure" label="测量" disabled={locked||Boolean(transaction)} onClick={()=>{showPanel(panel==='measure'?'':'measure');setMeasurePoints([])}}/>
        <Tool icon="measure" label="PMI" disabled={!available} onClick={()=>{setEntityPanel({kind:'pmi'});setPmiPicks([])}}/><Tool icon="appearance" label="外观" disabled={locked} onClick={()=>showPanel(panel==='appearance'?'':'appearance')}/>
        <div className="ce-ribbon-separator"/>
        <div className="ce-file-tools"><button disabled={locked||Boolean(transaction||formulaDraft||entityPanel)||!record||dirty||record.status!=='built'} onClick={()=>onDownload('step')}><CadEditorIcon name="export" size={16}/>导出 STP</button><button disabled={!available||!pmi.length||!record||dirty} onClick={exportPmi}>导出 PMI 图</button><button disabled={locked||Boolean(transaction||formulaDraft||entityPanel)||!token||(!dirty&&Boolean(record))} onClick={()=>onSave(true)}><CadEditorIcon name="save" size={16}/>保存</button><button disabled={!available||!record||dirty||record.status!=='built'} onClick={()=>setEntityPanel({kind:'pdm'})}><CadEditorIcon name="save" size={16}/>保存到 PDM</button></div>
        <Tool icon="cube" label="AI 资源社区" disabled={!available} onClick={()=>setEntityPanel({kind:'community'})}/><Tool icon="ai" label="创模 AI" disabled={!available} onClick={()=>navigate(()=>onNavigate?.('3D 建模'))}/>
      </>}
    </div>
    <div className="ce-workarea">
      <aside className="ce-tree" aria-label="特征树"><header><b>特征 ({tree.length})</b><button aria-label="收起特征树" onClick={()=>setTreeOpen(false)}>‹</button></header><div className="ce-tree-scroll">
        <details open><summary>默认几何元</summary><button className="ce-tree-row ce-origin" onClick={()=>{setSelectedReference(null);setSelectedSketchId(null);setView('iso');setSelection([])}}>⌖ <span>原点</span></button>{planes.map(([value,label])=><button key={value} className={`ce-tree-row ce-plane ${plane===value&&panel==='plane'?'selected':''}`} onClick={()=>{setSelectedReference(null);setSelectedSketchId(null);setSelection([]);setPlane(value);if(pendingSketch)startIndependentSketch(referenceFrames(draft.plan)[value],null,value);else setView({XY:'top',XZ:'front',YZ:'right'}[value])}}><CadEditorIcon name="plane" size={15}/><span>{label}</span></button>)}</details>
        <details open><summary>参考几何元</summary>{(draft.plan.references||[]).filter(ref=>ref.kind!=='helix').map(referenceRow)}{(draft.plan.sketches||[]).map(item=><div className="ce-entity-list-row" key={item.id}><button className={`ce-tree-row ${selectedSketchId===item.id?'selected':''}`} onClick={()=>{if(available){setSelectedSketchId(item.id);setSelection([])}}} onDoubleClick={()=>editIndependentSketch(item.id)} title="双击编辑独立草图"><CadEditorIcon name="sketch" size={15}/>{item.label||item.id}</button><button aria-label={`编辑独立草图 ${item.id}`} disabled={!available} onClick={()=>editIndependentSketch(item.id)}>✎</button><button aria-label={`删除独立草图 ${item.id}`} disabled={!available} onClick={()=>deleteEntity('sketches',item.id)}>×</button></div>)}{tree.map(item=><div key={item.id} className={`ce-feature-node ${item.suppressed?'suppressed':''}`}><div className="ce-feature-tree-line"><button className="ce-expand" aria-label={`展开 ${item.label}`} onClick={()=>setExpanded(old=>({...old,[item.id]:!old[item.id]}))}>{item.hasSketch?(expanded[item.id]?'⌄':'›'):''}</button><button className={`ce-tree-row ${selected===item.id?'selected':''}`} onClick={()=>{setSelectedReference(null);setSelectedSketchId(null);onSelected(item.id)}} onDoubleClick={()=>editFeature(item.id)} onContextMenu={event=>{event.preventDefault();setSelectedSketchId(null);onSelected(item.id);setContext(item.id)}} title="双击编辑特征"><CadEditorIcon name={operationIcons[item.op]||'cube'} size={15}/><span>{item.label}</span>{transaction?.id===item.id&&<i>✎</i>}</button></div>{expanded[item.id]&&item.hasSketch&&<button className={`ce-tree-row ce-sketch-node ${selectedSketchId===item.id?'selected':''}`} aria-pressed={selectedSketchId===item.id} onClick={()=>{onSelected(item.id);if(available){setSelectedSketchId(item.id);setSelection([])}}} onDoubleClick={()=>editFeature(item.id,'sketch')} title="双击编辑草图"><CadEditorIcon name="sketch" size={15}/><span>{item.label} · 草图</span></button>}</div>)}</details>
      </div><div className="ce-tree-bottom"><details open><summary>实体 ({bodyCount??'—'})</summary>{!base.geometry?.bodies?.length&&Number.isInteger(bodyCount)&&bodyCount>0&&Array.from({length:bodyCount},(_,i)=><div key={i} role="listitem" aria-label={`实体 ${i+1}`} className="ce-tree-row"><CadEditorIcon name="cube" size={15}/>实体 {i+1}</div>)}{(base.geometry?.bodies||[]).map(body=>{const state=bodyStateFor(draft.plan,body);return <div className="ce-body-actions" key={body.id}><button className="ce-tree-row" onClick={()=>selectBody(body)} onContextMenu={event=>{event.preventDefault();selectBody(body)}}><CadEditorIcon name="cube" size={15}/>{state?.label||`实体 ${body.index+1}`}</button><button aria-label={`${state?.hidden?'显示':'隐藏'}实体 ${body.index+1}`} disabled={!available} onClick={()=>{const next={...cloneFeatureValue(draft),plan:patchBodyState(draft.plan,body,{hidden:!state?.hidden}),changeNote:'修改实体可见性'};onEdit(next)}}>{state?.hidden?'○':'◉'}</button></div>})}</details><details open><summary>曲面 ({surfaces?.length??'—'})</summary>{surfaces?.map(surface=><button key={surface.id} className="ce-tree-row" disabled={!available} onClick={()=>selectSurface(surface)} onDoubleClick={editSurface} title="选择曲面；在特征树中编辑造型"><CadEditorIcon name="surface" size={15}/>{`曲面 ${surface.index+1}`}</button>)}</details><details open><summary>曲线 ({(draft.plan.references||[]).filter(ref=>ref.kind==='helix').length})</summary>{(draft.plan.references||[]).filter(ref=>ref.kind==='helix').map(referenceRow)}</details><details open><summary>PMI ({(draft.plan.annotations||[]).length})</summary>{(draft.plan.annotations||[]).map(note=><div className="ce-entity-list-row" key={note.id}><button className="ce-tree-row" disabled={!available} onClick={()=>{setEntityPanel({kind:'pmi',initial:note});setPmiPicks([])}}>{note.label||note.text||note.id}</button><button aria-label={`删除标注 ${note.id}`} disabled={!available} onClick={()=>deleteEntity('annotations',note.id)}>×</button></div>)}</details><button className="ce-tree-section" disabled={!available} onClick={()=>{setFormulaDraft(cloneFeatureValue(draft.plan.parameters));setPanel('formulas')}}>⌄ 方程式 ({Object.keys(draft.plan.parameters).length})</button><button className="ce-tree-section" onClick={()=>showPanel(panel==='versions'?'':'versions')}>⌄ 历史版本 ({versions.length})</button></div></aside>
      <main className="ce-canvas-area">
        {!treeOpen&&<button className="ce-show-tree" aria-label="展开特征树" onClick={()=>setTreeOpen(true)}>☰</button>}
        <div hidden={sketch} aria-hidden={sketch||undefined} style={{height:'100%'}}><CadModelViewport documentScope={scope} active={active&&!sketch} geometry={styledDisplay} selectionGeometry={styledSource} annotations={pmi} onCaptureReady={value=>{captureView.current=value}} construction={construction} controlPoints={transaction?.editingGrid&&!locked?currentFeature.points:undefined} onControlPointChange={(row,col,point)=>{if(locked||currentFeature?.op!=='surface_style')return;const points=cloneFeatureValue(currentFeature.points);points[row][col]=point;changeFeature({...currentFeature,points})}} onAnnotationEdit={id=>{if(available){setEntityPanel({kind:'pmi',initial:draft.plan.annotations.find(n=>n.id===id)});setPmiPicks([])}}} selection={selection} onSelect={onPick} pickKind={pickKind} view={view} onView={setView} fallback={fallback} token={token} appearance={appearance} onPlaneSelect={available&&base.current?(frame,face)=>startSketch(frame,face):undefined}/></div>
        {sketch&&<FeatureSketchEditor key={`${scope}:${transaction.id}`} active={active} variant="canvas" toolbarHost={sketchToolbarHost} feature={editorSketch} parameters={transaction.draft.plan.parameters} onChange={(feature,parameters)=>{if(!locked)setTransaction(old=>patchCadSketch(old,feature,parameters))}} disabled={locked}/>}
        {missingInputs&&!localError&&!error&&<div className="ce-notice" role="status">{missingInputs}</div>}
        {selectedSketchId&&!transaction&&<div className="ce-notice" role="status">{draft.plan.sketches?.some(s=>s.id===selectedSketchId)?'已选择独立草图；点击拉伸、旋转或扫掠创建使用此草图的新特征。':`已选择“${tree.find(item=>item.id===selectedSketchId)?.label}”的草图；点击拉伸或旋转修改其所属特征。`}</div>}
        {(localError||error||previewError)&&<div className="ce-notice ce-error" role="alert"><span>{localError||error||previewError}</span>{errorDetails&&<details><summary>详情</summary><pre>{errorDetails}</pre></details>}{unavailable&&<button onClick={onRetry}>重新读取</button>}{error&&record&&onReload&&<button disabled={locked} onClick={()=>navigate(onReload)}>重新读取已保存版本</button>}{!transaction&&previewError&&<button onClick={base.retry}>重试</button>}</div>}
        {(busy||previewLoading)&&<div className="ce-progress" role="status">{busy||'正在更新模型…'}</div>}
        {!token&&<div className="ce-notice">登录后可读取实体、保存和导出。<button onClick={()=>onNavigate?.('账号')}>登录</button></div>}
        {transaction&&!sketch&&<Panel title={currentFeature.label||featureNames[currentFeature.op]} onClose={cancelLocked?()=>{}:cancel}>
          <fieldset disabled={locked}><div className="ce-operation-kind"><span>{transaction.isNew||transaction.inserted?'新建特征':'修改现有特征'}</span></div>{!transaction.isNew&&currentFeature.op.startsWith('profile_')&&<p className="ce-panel-note">正在修改“{currentFeature.label||currentFeature.id}”的草图；完成后更新此特征。</p>}
            {['profile_extrude','profile_revolve','profile_sweep'].includes(currentFeature.op)&&<><label className="df-field"><span>草图用途</span><select aria-label="草图特征类型" value={currentFeature.op} onChange={event=>changeProfileOperation(event.target.value)}><option value="profile_extrude">拉伸</option><option value="profile_revolve">旋转</option><option value="profile_sweep">扫掠</option><option value="profile_loft">放样</option></select></label>{currentFeature.op==='profile_revolve'&&<p className="df-note">旋转轴使用草图局部坐标，可在“更多设置”调整。</p>}</>}
            {transaction.isNew&&['profile_extrude','profile_revolve','box','cylinder','sweep','loft','gear','profile_sweep','profile_loft','surface_style'].includes(currentFeature.op)&&<div className="ce-combine" role="group" aria-label="特征作用">{[['union','添加'],['cut','移除'],['intersect','相交'],['new','新建实体']].map(([value,label])=><button key={value} aria-pressed={transaction.combine===value} onClick={()=>setTransaction(old=>({...old,combine:value}))}>{label}</button>)}</div>}
            {[...edgeOperations,...faceOperations].includes(currentFeature.op)&&<div className="ce-selection-box"><b>{faceOperations.includes(currentFeature.op)?'选择面':'选择边'}</b>{Array.isArray(selectedRefs)?selectedRefs.map((ref,index)=><div key={index}><span>{faceOperations.includes(currentFeature.op)?'面':'边'} {typeof ref==='string'?ref:ref.index+1}</span><button aria-label={`移除选择 ${index+1}`} onClick={()=>removeReference(index)}>×</button></div>):<span>已有规则：{selectedRefs}</span>}<p>在模型上点击{faceOperations.includes(currentFeature.op)?'面':'边'}，按住 Ctrl / ⌘ 多选</p></div>}
            <FeatureEditor disabled={locked} feature={currentFeature} parameters={transaction.draft.plan.parameters} preceding={transaction.draft.plan.features.slice(0,transaction.draft.plan.features.findIndex(item=>item.id===transaction.id))} onChange={changeFeature} sketches={transaction.draft.plan.sketches||[]} references={transaction.draft.plan.references||[]} onSketch={editSketchSection} onPickPath={()=>setTransaction(old=>({...old,pickingPath:!old.pickingPath}))} editingGrid={Boolean(transaction.editingGrid)} onEditGrid={()=>setTransaction(old=>({...old,editingGrid:!old.editingGrid}))}/>
          </fieldset>{(base.loading||result.loading||awaitingPreview)&&<p className="ce-panel-note" role="status">正在预览；完成操作会等待已发送的预览结束。等待期间可取消。</p>}{transaction.pickingPath&&<p className="ce-panel-note">在模型上依次点击路径点；首点作为路径起点，再次点击“从模型取点”结束。</p>}{transaction.editingGrid&&<p className="ce-panel-note">拖动曲面上的控制点，沿当前视图平面调整；坐标可在面板精确输入。</p>}<footer><button className="ce-done" disabled={locked||!token} onClick={finish}>{completionLabel}</button><button disabled={cancelLocked} onClick={cancel}>取消</button></footer>
        </Panel>}
        {panel==='plane'&&!transaction&&<Panel title={pendingSketch?'选择草图平面':'基准平面'} onClose={()=>{setPanel('');setPendingSketch(false)}}><p className="ce-panel-note">选择基准面，或点击模型上的平面。</p>{planes.map(([value,label])=><button className="ce-plane-choice" key={value} onClick={()=>{setSelectedReference(null);setSelectedSketchId(null);setSelection([]);setPlane(value);if(pendingSketch)startIndependentSketch(referenceFrames(draft.plan)[value],null,value);else{setView({XY:'top',XZ:'front',YZ:'right'}[value]);setPanel('')}}}><CadEditorIcon name="plane" size={18}/>{label}<span>{value}</span></button>)}</Panel>}
        {entityPanel&&['plane','axis','point','helix'].includes(entityPanel.kind)&&<CadReferencePanel key={entityPanel.initial?.id||entityPanel.kind} plan={draft.plan} kind={entityPanel.kind} initial={entityPanel.initial} onPreview={value=>setEntityPanel(old=>old?{...old,preview:value}:old)} picks={pmiPicks} selectedFrame={cadPlaneForFace(selectedPlanarFace)} disabled={locked} onClose={cancel} onApply={value=>saveEntity('references',value,'编辑参考几何')}/>}
        {entityPanel?.kind==='pmi'&&<CadPmiPanel key={entityPanel.initial?.id||'new'} plan={draft.plan} initial={entityPanel.initial} onPreview={value=>setEntityPanel(old=>old?{...old,preview:value}:old)} picks={pmiPicks} onResetPicks={()=>setPmiPicks([])} disabled={locked} onClose={cancel} onApply={value=>saveEntity('annotations',value,'编辑 PMI 标注')} onDelete={entityPanel.initial?()=>deleteEntity('annotations',entityPanel.initial.id):undefined}/>}
        {entityPanel?.kind==='community'&&<CadCommunityPanel record={dirty?null:record} token={token} accountKey={scope} active={active} onOpen={entry=>{setEntityPanel(null);return onOpenCommunity?.(entry)}} onClose={cancel}/>}
        {entityPanel?.kind==='pdm'&&<CadPdmPanel record={record} token={token} accountKey={scope} active={active} onOpen={entry=>{setEntityPanel(null);return onOpenPdm?.(entry)}} onClose={cancel}/>}
        {entityPanel?.kind==='import'&&<CadImportBodyPanel plan={draft.plan} token={token} accountKey={scope} active={active} disabled={locked} onClose={cancel} initialPosition={selection.at(-1)?.worldPoint||[0,0,0]} onApply={(plan,meta)=>{const next={...cloneFeatureValue(draft),plan,changeNote:`导入 ${meta.label}`};setEntityPanel(null);setTransaction({id:meta.featureIds[0],base:cloneFeatureValue(draft),baseId:draft.plan.result,draft:next,isNew:false,inserted:true,stage:'feature',selection:[]})}}/>}
        {entityPanel?.kind==='standard'&&<CadStandardPartPanel plan={draft.plan} disabled={locked} onClose={cancel} initialPosition={selection.at(-1)?.worldPoint||[0,0,0]} onApply={(plan,meta)=>{const next={...cloneFeatureValue(draft),plan,changeNote:`插入 ${meta.label}`};setEntityPanel(null);setTransaction({id:meta.featureIds[0],base:cloneFeatureValue(draft),baseId:draft.plan.result,draft:next,isNew:false,inserted:true,stage:'feature',selection:[]})}}/>}
        {panel==='body'&&bodySelection.length>0&&<Panel title="实体操作" onClose={()=>setPanel('')}><p>{bodyStateFor(draft.plan,bodySelection[0])?.label||`实体 ${bodySelection[0].index+1}`}</p><label className="df-field"><span>实体颜色</span><input type="color" aria-label="实体颜色" value={bodyStateFor(draft.plan,bodySelection[0])?.color||'#a8afd0'} onChange={event=>onEdit({...cloneFeatureValue(draft),plan:patchBodyState(draft.plan,bodySelection[0],{color:event.target.value}),changeNote:'修改实体颜色'})}/></label>{[['translate','移动'],['rotate','旋转'],['copy','复制'],['keep','仅保留所选实体'],['delete','删除所选实体']].map(([action,label])=><button className="ce-context-action" key={action} disabled={!available} onClick={()=>bodyTool(action)}>{label}</button>)}<label className="df-field"><span>实体名称</span><input aria-label="实体名称" value={bodyStateFor(draft.plan,bodySelection[0])?.label||`实体 ${bodySelection[0].index+1}`} onChange={event=>onEdit({...cloneFeatureValue(draft),plan:patchBodyState(draft.plan,bodySelection[0],{label:event.target.value}),changeNote:'重命名实体'})}/></label></Panel>}
        {panel==='formulas'&&formulaDraft&&<Panel title="方程式" className="ce-wide-panel" onClose={cancelLocked?()=>{}:cancel}><fieldset disabled={locked}><ParameterEditor parameters={formulaDraft} onChange={setFormulaDraft}/></fieldset><footer><button className="ce-done" disabled={locked||!token} onClick={finish}>{completionLabel}</button><button disabled={cancelLocked} onClick={cancel}>取消</button></footer></Panel>}
        {panel==='appearance'&&<Panel title="外观" onClose={()=>setPanel('')}><label className="ce-setting">显示方式<select value={appearance} onChange={event=>setAppearance(event.target.value)}><option value="shaded">着色并显示边线</option><option value="wireframe">线框</option></select></label></Panel>}
        {panel==='settings'&&<Panel title="设置" onClose={()=>setPanel('')}><label className="ce-setting">单位<span>毫米 (mm)</span></label><label className="ce-setting">特征树<input type="checkbox" checked={treeOpen} onChange={event=>setTreeOpen(event.target.checked)}/></label><p className="ce-panel-note">双击特征或草图进入编辑。滚轮缩放，拖动旋转，Shift 拖动平移。Esc 取消当前操作。</p></Panel>}
        {panel==='measure'&&<Panel title="测量" onClose={()=>setPanel('')}><p className="ce-panel-note">点击模型上的两个位置测量直线距离。</p>{measurePoints.length===2?<div className="ce-measure-value">{number(Math.hypot(...measurePoints[0].map((value,i)=>value-measurePoints[1][i])))} <small>mm</small></div>:<p className="ce-panel-note">已选 {measurePoints.length} / 2 个位置</p>}{measurePoints.map((point,index)=><p className="ce-measure-point" key={index}>{index?'B':'A'} · {point.map(number).join('，')}</p>)}<button onClick={()=>setMeasurePoints([])}>重新选择</button>{record?.inspection&&<div className="ce-measure-detail">体积 {number(record.inspection.volumeMm3)} mm³<br/>实体 {record.inspection.solidCount} 个</div>}</Panel>}
        {panel==='versions'&&<Panel title="历史版本" className="ce-wide-panel" onClose={()=>setPanel('')}>{versions.length?versions.map(version=><div className="ce-version" key={version.revision}><span>r{version.revision} · {version.status==='built'?'已完成':'草稿'}<small>{version.changeNote}</small></span>{version.status==='built'&&<button disabled={locked} onClick={()=>onDownload('step',{...record,revision:version.revision})}>STP ↓</button>}<button disabled={!available||version.revision===record?.revision} onClick={()=>onRestore(version.revision)}>恢复</button></div>):<p className="ce-panel-note">完成首次保存后显示历史版本。</p>}</Panel>}
        {panel==='documents'&&<Panel title="图档列表" className="ce-wide-panel" onClose={()=>setPanel('')}>{Array.isArray(documents) ? documents.length ? documents.map(item=><button className="ce-document-choice" key={`${item.projectId}:${item.fileId}`} disabled={locked} onClick={()=>navigate(()=>{onOpenDocument?.(item);setPanel('')})}><CadEditorIcon name="cube" size={16}/>{item.name}<span>{item.status==='empty'?'新图档':item.status==='draft'?'草稿':'可编辑'}</span></button>) : <p className="ce-panel-note">当前项目没有可编辑图档。</p> : saved.filter(item=>!sourceFileId||item.fileId===sourceFileId).map(item=><button className="ce-document-choice" key={item.id} onClick={()=>navigate(()=>{onLoad(item.id);setPanel('')})}><CadEditorIcon name="cube" size={16}/>{item.name}<span>r{item.revision}</span></button>)}</Panel>}
        {context&&<Panel title="特征操作" onClose={()=>setContext(null)}><button className="ce-context-action" disabled={!available} onClick={()=>editFeature(context)}>编辑特征</button>{tree.find(item=>item.id===context)?.hasSketch&&<button className="ce-context-action" disabled={!available} onClick={()=>editFeature(context,'sketch')}>编辑草图</button>}<button className="ce-context-action" disabled={!available} onClick={()=>run(()=>onEdit(moveFeature(draft,context,-1)))}>向上移动</button><button className="ce-context-action" disabled={!available} onClick={()=>run(()=>onEdit(moveFeature(draft,context,1)))}>向下移动</button><button className="ce-context-action" disabled={!available} onClick={()=>run(()=>onEdit(deleteFeature(draft,context)))}>删除特征</button></Panel>}
        {confirmLeave&&<div className="ce-dialog-backdrop"><section role="alertdialog" aria-label="结束当前操作"><b>当前操作尚未完成</b><p>可以继续编辑，或取消本次操作后离开。</p><button onClick={()=>setConfirmLeave(null)}>继续编辑</button><button onClick={()=>{const action=confirmLeave;setConfirmLeave(null);cancel();action()}}>取消操作并离开</button></section></div>}
      </main>
    </div>
    <footer className="ce-document-tabs"><button onClick={()=>showPanel(panel==='documents'?'':'documents')}>▱ 全部</button>{onNewDocument&&<button disabled={locked} onClick={()=>navigate(onNewDocument)}>＋ 新建</button>}<div role="navigation" aria-label="项目图档" style={{display:'flex',minWidth:0,overflowX:'auto',flex:1}}>{documents?.length ? documents.map(item=>{
      const current=item.fileId===sourceFileId&&item.projectId===sourceProjectId
      return <button key={`${item.projectId}:${item.fileId}`} style={{flexShrink:0,whiteSpace:'nowrap'}} className={current?'ce-active-document':''} aria-current={current?'page':undefined} aria-label={`打开图档 ${item.name}`} disabled={locked&&!current} onClick={()=>current?showPanel(''):navigate(()=>onOpenDocument?.(item))}><CadEditorIcon name="cube" size={14}/>{item.name}{current&&dirty?' •':''}</button>
    }) : <button className="ce-active-document" onClick={()=>showPanel('')}><CadEditorIcon name="cube" size={14}/>{editorName}{dirty?' •':''}</button>}</div><span className="ce-save-state">{busy|| (transaction||formulaDraft||entityPanel?'操作中':!draft.plan.features.length?'空图档':dirty||!record?'未保存':`已保存 r${record.revision}`)}</span></footer>
  </section>
}
