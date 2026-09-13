import { createClientId } from './clientId.js'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { engineeringClient as client, topologyKey, topologySelector, engineeringNumber, engineeringDirection, engineeringDrawingSettings, engineeringDraftEquals, removeEngineeringView, engineeringCanvasVisible } from './engineeringWorkspaceClient.js'
import { cadAgent, cadArtifactUrl } from './cadAgentClient.js'
import { API_BASE } from './api.js'
import { generationMatchesModel } from './viewerState.js'
import './engineering-workspace.css'

const kinds = { vertex: '点', edge: '边', face: '面' }
const viewNames = { front:'主视图',top:'俯视图',right:'右视图',rear:'后视图',bottom:'仰视图',left:'左视图',isometric:'轴测图',custom:'自定义投影',section:'剖面' }
const axisNormal = axis => 'XYZ'.split('').map(value=>Number(value===axis))
const viewLabel = item => item.view==='section'?`${item.axis==='custom'?`法向 (${item.normal.map(num).join(', ')})`:`${item.axis||'Z'} 方向`} ${item.sectionMode==='cross_section'?'断面':'剖视'} · 位置 ${num(item.position??item.z)} mm`:viewNames[item.view]
const num = value => Number.isFinite(Number(value)) ? Number(Number(value).toFixed(4)).toString() : '—'
const label = item => `${kinds[item.kind]} ${item.index + 1}${item.type ? ` · ${item.type}` : ''} · (${item.center.map(num).join(', ')})`
const initialViews = () => ['front', 'top', 'right'].map(view => ({ view, x: '', y: '', scale: '' }))
const assemblyStatus = { queued:'等待开始',planning:'AI 正在规划零件与配合',building:'正在逐件生成实体',assembling:'正在求解装配并检查干涉',review_required:'已生成，请检查设计',needs_input:'需要补充设计信息',failed:'本次未完成',interrupted:'服务中断，草稿已保存',ready:'实体已完成' }
const activeAssembly = job => ['queued','planning','building','assembling'].includes(job?.status)

function AssemblyAIComposer({token,accountKey,fileId,onReady}) {
  const [message,setMessage]=useState(''),[job,setJob]=useState(null),[history,setHistory]=useState([]),[error,setError]=useState(''),[submitting,setSubmitting]=useState(false)
  const readyRef=useRef(onReady), requestRef=useRef(null), mounted=useRef(true), submissionRef=useRef(null)
  readyRef.current=onReady
  useEffect(()=>{const control=new AbortController();mounted.current=true;submissionRef.current=null;setJob(null);setHistory([]);setError('');setMessage('');setSubmitting(false);requestRef.current?.abort()
    client.assemblyJobs(fileId,{token,signal:control.signal}).then(value=>{if(!control.signal.aborted){setHistory(old=>[...old,...value.items.filter(item=>!old.some(existing=>existing.id===item.id))]);const running=value.items.find(activeAssembly);if(running)setJob(old=>old||running)}}).catch(e=>{if(e.name!=='AbortError')setError(e.message)})
    return()=>{mounted.current=false;control.abort();requestRef.current?.abort()}
  },[accountKey||token,fileId])
  useEffect(()=>{
    if(!activeAssembly(job))return
    const control=new AbortController();let timer
    async function poll(){try{const value=await client.assemblyJob(job.id,{token,signal:control.signal});if(control.signal.aborted)return;setJob(value)
      if(activeAssembly(value))timer=setTimeout(poll,2000)
      else{setHistory(old=>[value,...old.filter(x=>x.id!==value.id)]);if(value.designId)await readyRef.current(value.designId,{automatic:true})}
    }catch(e){if(e.name!=='AbortError'&&!control.signal.aborted){setError(`读取任务进度失败：${e.message}。可点击下方任务重新查看。`);timer=setTimeout(poll,5000)}}}
    timer=setTimeout(poll,700);return()=>{control.abort();clearTimeout(timer)}
  },[job?.id,activeAssembly(job),token])
  async function submit(){
    if(requestRef.current || activeAssembly(job) || !message.trim())return
    const control=new AbortController();requestRef.current=control;setSubmitting(true);setError('')
    const payload={fileId,message,...(job?.status==='needs_input'?{parentJobId:job.id}:{})},identity=JSON.stringify(payload)
    try{
      if(submissionRef.current?.identity!==identity)submissionRef.current={identity,requestId:createClientId()}
      const value=await client.startAiAssembly({...payload,requestId:submissionRef.current.requestId},{token,signal:control.signal});if(control.signal.aborted)return;setJob(value);setHistory(old=>[value,...old.filter(x=>x.id!==value.id)]);setMessage('')}
    catch(e){if(e.name!=='AbortError'&&!control.signal.aborted)setError(e.message)}
    finally{if(requestRef.current===control)requestRef.current=null;if(!control.signal.aborted&&mounted.current)setSubmitting(false)}
  }
  async function openJob(id){if(submitting||activeAssembly(job))return;requestRef.current?.abort();const control=new AbortController();requestRef.current=control;setError('');try{const value=await client.assemblyJob(id,{token,signal:control.signal});if(control.signal.aborted)return;setJob(value);if(value.designId)await readyRef.current(value.designId)}catch(e){if(e.name!=='AbortError'&&!control.signal.aborted)setError(e.message)}finally{if(requestRef.current===control)requestRef.current=null}}
  return <section className="eng-ai-assembly" aria-label="AI 多零件装配"><details className="eng-disclosure" open={submitting || activeAssembly(job) || job?.status==='needs_input'}><summary>用 AI 生成零件并装配</summary><p>写清每种零件的形状、尺寸、数量、位置和配合关系。必要信息不足会保存草稿并列出问题；AI 调用按当前账户计费规则记录实际用量。</p>
    <label>{job?.status==='needs_input'?'补充下方问题，接着规划':'完整装配需求'}<textarea disabled={submitting||activeAssembly(job)} value={message} onChange={e=>setMessage(e.target.value)} maxLength={16000} rows={5} placeholder="例如：两个边长 20 mm 的立方体，局部原点均为最小角点。实例 A 固定在 (0,0,0)，实例 B 位于 (30,0,0)，两者角度均为 (0,0,0)，按指定位置装配，不增加其他特征或配合。"/></label>
    <div className="eng-actions"><button className="eng-primary" disabled={submitting||activeAssembly(job)||!message.trim()} onClick={submit}>{submitting?'正在提交…':job?.status==='needs_input'?'补充信息并继续 AI 规划':'AI 规划并生成装配'}</button>{job&&!activeAssembly(job)&&<button disabled={submitting} onClick={()=>{submissionRef.current=null;setJob(null);setMessage('');setError('')}}>开始新需求</button>}</div></details>
    {error&&<p className="eng-error" role="alert">{error}</p>}
    {job&&<div className="eng-ai-job"><p role="status"><b>{assemblyStatus[job.status]||job.status}</b>{job.elapsedSeconds!=null&&` · ${num(job.elapsedSeconds)} 秒`}</p>
      {job.questions?.length>0&&<ul>{job.questions.map((q,i)=><li key={i}>{q}</li>)}</ul>}{job.errors?.length>0&&job.status!=='needs_input'&&<p className="eng-error">{job.errors.join('；')}</p>}
      {job.parts?.length>0&&<ul>{job.parts.map(part=><li key={part.id}>{part.name}：{assemblyStatus[part.status]||part.status}{part.metrics&&` · ${part.metrics.solidCount} 个实体`}</li>)}</ul>}
      {job.designId&&<><p>内核生成已完成。原始描述符合性尚未人工检查，请核对尺寸、配合和干涉。</p><button onClick={()=>readyRef.current(job.designId)}>打开装配，继续编辑 / 出图</button></>}
      <details><summary>需求和零件计划记录</summary><p>{job.requirements?.join('\n')}</p><pre>{JSON.stringify(job.plan,null,2)||'等待 AI 返回计划。'}</pre></details>
    </div>}
    {history.length>0&&<details><summary>近期装配任务（{history.length}）</summary>{history.map(item=><button className="eng-history-job" key={item.id} disabled={submitting||activeAssembly(job)} onClick={()=>openJob(item.id)}>{item.plan?.name||item.requirements?.[0]?.slice(0,28)||'装配任务'} · {assemblyStatus[item.status]||item.status}</button>)}</details>}
  </section>
}

function TopologySelect({ topology = [], value, onChange, title, optional = false }) {
  return <label>{title}<select value={value} onChange={event => onChange(event.target.value)}><option value="">{optional ? '不选择' : '请选择拓扑实体'}</option>{topology.map(item => <option key={topologyKey(item)} value={topologyKey(item)}>{label(item)}</option>)}</select></label>
}

function DirectionInput({value,onChange,label}) {
  return <div className="eng-direction"><span>{label}</span><div className="eng-vector">{['X','Y','Z'].map((axis,index)=><label key={axis}>{axis}<input type="number" step="any" aria-label={`${label} ${axis}`} value={value[index]} onChange={event=>onChange(value.map((x,i)=>i===index?event.target.value:x))}/></label>)}</div></div>
}

export function EngineeringModelCanvas({ token, design, markers = [], onSelectTopology, pickKind = 'vertex', sectionPlane, active = true }) {
  const host = useRef(null)
  const [error, setError] = useState('')
  const [loading,setLoading]=useState(false)
  const [nonce, setNonce] = useState(0)
  const [exploded, setExploded] = useState(false)
  const [clipped, setClipped] = useState(false)
  const selectRef=useRef(onSelectTopology)
  selectRef.current=onSelectTopology
  const markerRef=useRef(markers), updateMarkers=useRef(null), savedView=useRef(null)
  const activeRef=useRef(active),updateRendering=useRef(null)
  activeRef.current=active
  const planeRef=useRef(sectionPlane),updateClipping=useRef(null)
  planeRef.current=sectionPlane
  const planeKey=JSON.stringify(sectionPlane)
  markerRef.current=markers
  const markerKey = JSON.stringify(markers)
  useEffect(()=>{setExploded(false);setClipped(false)},[design?.id])
  useEffect(() => {
    if (!design || !host.current) return
    const controller = new AbortController()
    let renderer, controls, frame, observer, scene, camera, disposed = false, pointerDown, pointerUp, visibilityChange
    const disposeScene = root => root?.traverse(node => { node.geometry?.dispose(); (Array.isArray(node.material) ? node.material : [node.material]).forEach(material => material?.dispose()) })
    async function start() {
      setError('');setLoading(true)
      const blob = await client.artifact(design.id, 'glb', { token, signal: controller.signal })
      const gltf = await new GLTFLoader().parseAsync(await blob.arrayBuffer(), '')
      if (disposed) { disposeScene(gltf.scene); return }
      if (exploded && design.kind === 'assembly' && design.instances?.length) {
        const gap = Math.max(new THREE.Box3().setFromObject(gltf.scene).getSize(new THREE.Vector3()).length()*.3, 5)
        const group = new THREE.Group()
        try {
          for (const [index,item] of design.instances.entries()) {
            const partBlob=await client.artifact(item.designId,'glb',{token,signal:controller.signal})
            const part=await new GLTFLoader().parseAsync(await partBlob.arrayBuffer(),'')
            part.scene.position.fromArray(item.position)
            part.scene.rotation.set(...item.rotation.map(value=>value*Math.PI/180),'ZYX')
            part.scene.position.x += (index-(design.instances.length-1)/2)*gap
            group.add(part.scene)
          }
        } catch(error) { disposeScene(group);disposeScene(gltf.scene);throw error }
        disposeScene(gltf.scene);gltf.scene=group
        if(disposed){disposeScene(group);return}
      }
      scene = new THREE.Scene(); scene.background = new THREE.Color('#eff4fa')
      scene.add(gltf.scene)
      gltf.scene.traverse(node => { if (node.isMesh) { (Array.isArray(node.material)?node.material:[node.material]).forEach(material=>material?.dispose());node.material = new THREE.MeshStandardMaterial({ color: '#8caac4', metalness: .12, roughness: .54 }); node.add(new THREE.LineSegments(new THREE.EdgesGeometry(node.geometry, 30), new THREE.LineBasicMaterial({ color: '#47617a', transparent: true, opacity: .3 }))) } })
      const box = new THREE.Box3().setFromObject(gltf.scene), center = box.getCenter(new THREE.Vector3()), extent = Math.max(box.getSize(new THREE.Vector3()).length(), 1)
      const pickable = !exploded && onSelectTopology ? (design.topology||[]).filter(item=>item.kind===pickKind) : []
      let pickMesh
      if(pickable.length){
        pickMesh=new THREE.InstancedMesh(new THREE.SphereGeometry(extent/160,10,8),new THREE.MeshBasicMaterial({color:'#217ae0',transparent:true,opacity:.68,depthTest:false}),pickable.length)
        const matrix=new THREE.Matrix4()
        pickable.forEach((item,index)=>{matrix.makeTranslation(...item.center);pickMesh.setMatrixAt(index,matrix)})
        pickMesh.instanceMatrix.needsUpdate=true;pickMesh.renderOrder=9;scene.add(pickMesh)
      }
      const highlights=new THREE.Group();scene.add(highlights)
      updateMarkers.current=()=>{disposeScene(highlights);highlights.clear();for(const [index,point] of markerRef.current.entries()){const sphere=new THREE.Mesh(new THREE.SphereGeometry(extent/100,16,12),new THREE.MeshBasicMaterial({color:index?'#ee8541':'#d3336b',depthTest:false}));sphere.position.fromArray(point);sphere.renderOrder=10;highlights.add(sphere)}}
      updateMarkers.current()
      camera = new THREE.PerspectiveCamera(38, 1, extent / 10000, extent * 100)
      camera.up.set(0, 0, 1); camera.position.copy(center).add(new THREE.Vector3(1.3,-1.5,1.1).normalize().multiplyScalar(extent * 1.8))
      renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2)); host.current.appendChild(renderer.domElement)
      updateClipping.current=()=>{const setting=planeRef.current;const n=new THREE.Vector3(...(setting?.normal||[0,0,1]));if(setting?.invalid||n.lengthSq()<1e-16){renderer.clippingPlanes=[];return}n.normalize();renderer.clippingPlanes=clipped?[new THREE.Plane(n.negate(),setting?.position??center.z)]:[]}
      updateClipping.current()
      renderer.domElement.setAttribute('aria-label', '真实 STEP 实体预览，可拖动旋转、滚轮缩放')
      if(pickMesh){
        let start=null
        const raycaster=new THREE.Raycaster()
        pointerDown=event=>{if(event.button===0)start=[event.clientX,event.clientY]}
        pointerUp=event=>{if(!start||Math.hypot(event.clientX-start[0],event.clientY-start[1])>5){start=null;return}start=null;const rect=renderer.domElement.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1),camera);const hit=raycaster.intersectObject(pickMesh)[0];if(hit?.instanceId!==undefined)selectRef.current?.(pickable[hit.instanceId])}
        renderer.domElement.addEventListener('pointerdown',pointerDown)
        renderer.domElement.addEventListener('pointerup',pointerUp)
      }
      controls = new OrbitControls(camera, renderer.domElement); controls.target.copy(center); controls.enableDamping = true
      if(savedView.current?.id===design.id&&savedView.current.nonce===nonce&&savedView.current.exploded===exploded){camera.position.fromArray(savedView.current.position);controls.target.fromArray(savedView.current.target)}
      scene.add(new THREE.AmbientLight(0xffffff, 2)); const light = new THREE.DirectionalLight(0xffffff, 3); light.position.set(extent,-extent,extent*2); scene.add(light)
      const grid = new THREE.GridHelper(extent*2,20,0xb4c4d4,0xd9e3ed); grid.rotation.x=Math.PI/2; grid.position.set(center.x,center.y,box.min.z); scene.add(grid)
      const canRender = () => engineeringCanvasVisible({ active:activeRef.current, hidden:document.hidden, width:host.current?.clientWidth, height:host.current?.clientHeight })
      const pause = () => { cancelAnimationFrame(frame); frame=undefined }
      const render = () => { frame=undefined;if(disposed||!canRender())return;controls.update();renderer.render(scene,camera);frame=requestAnimationFrame(render) }
      const resize = () => { if(disposed||!canRender()){pause();return}const w=host.current.clientWidth,h=host.current.clientHeight;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();if(frame===undefined)render() }
      updateRendering.current=resize;visibilityChange=resize;document.addEventListener('visibilitychange',visibilityChange)
      resize();observer=new ResizeObserver(resize);observer.observe(host.current)
    }
    start().catch(error => { if (!disposed && error.name !== 'AbortError') setError(error.message) }).finally(()=>{if(!disposed)setLoading(false)})
    return () => { disposed=true;if(camera&&controls)savedView.current={id:design.id,nonce,exploded,position:camera.position.toArray(),target:controls.target.toArray()};updateMarkers.current=null;updateClipping.current=null;updateRendering.current=null;document.removeEventListener('visibilitychange',visibilityChange);controller.abort();cancelAnimationFrame(frame);observer?.disconnect();controls?.dispose();renderer?.domElement.removeEventListener('pointerdown',pointerDown);renderer?.domElement.removeEventListener('pointerup',pointerUp);disposeScene(scene);renderer?.dispose();renderer?.domElement.remove() }
  }, [design?.id, token, nonce, exploded, clipped, pickKind, Boolean(onSelectTopology)])
  useEffect(()=>{updateMarkers.current?.()},[markerKey])
  useEffect(()=>{updateClipping.current?.()},[planeKey])
  useEffect(()=>{updateRendering.current?.()},[active])
  return <div className="eng-canvas-wrap"><div ref={host} className="eng-canvas" />{loading&&<p className="eng-canvas-loading" role="status">正在加载实体预览…</p>}{error && <div className="eng-canvas-error" role="alert">{error}<button onClick={() => setNonce(n=>n+1)}>重新加载</button></div>}<div className="eng-fit eng-actions">{design?.kind==='assembly'&&<button aria-pressed={exploded} onClick={()=>setExploded(v=>!v)}>{exploded?'恢复装配位置':'爆炸显示'}</button>}<button disabled={sectionPlane?.invalid&&!clipped} aria-pressed={clipped} onClick={()=>setClipped(v=>!v)}>{clipped?'关闭剖切':'剖切显示'}</button><button onClick={() => setNonce(n=>n+1)}>适合窗口</button></div><span className="eng-display-note">{sectionPlane?.invalid?'请填写有效的剖切法向和位置。':exploded?'爆炸位移只改变显示，恢复装配后可点选测量。':onSelectTopology?`点击蓝色${kinds[pickKind]}${pickKind==='vertex'?'':'中心'}标记选中真实拓扑；拖动旋转。`:clipped?'剖切显示不改变已保存实体。':''}</span></div>
}

function SaveBlob({ token, design, artifactKey, children, onError }) {
  const [busy,setBusy]=useState(false)
  const request=useRef(null)
  useEffect(()=>{setBusy(false);request.current=null;return()=>request.current?.abort()},[token,design.id,artifactKey])
  return <button disabled={busy} onClick={async()=>{if(request.current)return;const controller=new AbortController();request.current=controller;setBusy(true);try{const blob=await client.artifact(design.id,artifactKey,{token,signal:controller.signal});if(controller.signal.aborted)return;const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=design.artifacts[artifactKey].filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(error){if(!controller.signal.aborted)onError(error.message)}finally{if(request.current===controller)request.current=null;if(!controller.signal.aborted)setBusy(false)}}}>{busy?'下载中…':children}</button>
}

export function EngineeringDrawingPreview({ src }) {
  const [zoom,setZoom]=useState(100),[aspect,setAspect]=useState(Math.SQRT2),[size,setSize]=useState({width:0,height:0})
  const viewport=useRef(null),anchor=useRef(null)
  useEffect(()=>{const node=viewport.current;if(!node)return;const measure=()=>setSize({width:node.clientWidth,height:node.clientHeight});measure();const observer=new ResizeObserver(measure);observer.observe(node);return()=>observer.disconnect()},[])
  useEffect(()=>{setZoom(100);anchor.current=null;if(viewport.current){viewport.current.scrollLeft=0;viewport.current.scrollTop=0}},[src])
  useLayoutEffect(()=>{if(!viewport.current||!anchor.current)return;viewport.current.scrollLeft=anchor.current.x;viewport.current.scrollTop=anchor.current.y;anchor.current=null},[zoom])
  const changeZoom=next=>{next=Math.min(800,Math.max(50,next));const node=viewport.current;if(node)anchor.current={x:(node.scrollLeft+node.clientWidth/2)*next/zoom-node.clientWidth/2,y:(node.scrollTop+node.clientHeight/2)*next/zoom-node.clientHeight/2};setZoom(next)}
  const fit=()=>{anchor.current={x:0,y:0};setZoom(100);if(viewport.current){viewport.current.scrollLeft=0;viewport.current.scrollTop=0}}
  const fitWidth=size.width&&size.height?Math.min(size.width,size.height*aspect):null
  return <section className="eng-sheet-viewer" aria-label="工程图预览"><div className="eng-preview-controls"><span>预览缩放 <output>{zoom}%</output></span><button aria-label="缩小工程图预览" disabled={zoom<=50} onClick={()=>changeZoom(zoom-50)}>−</button><input aria-label="工程图预览缩放" type="range" min="50" max="800" step="50" value={zoom} onChange={event=>changeZoom(Number(event.target.value))}/><button aria-label="放大工程图预览" disabled={zoom>=800} onClick={()=>changeZoom(zoom+50)}>＋</button><button onClick={fit}>适合窗口</button></div><p>仅调整屏幕预览；不改变图纸比例或 DXF / PDF 文件。放大后可在预览区域滚动查看。</p><div ref={viewport} className="eng-sheet-viewport" tabIndex={0} role="region" aria-label="可滚动工程图预览"><img className="eng-sheet-preview" src={src} style={{width:fitWidth?fitWidth*zoom/100:`${zoom}%`,maxWidth:'none'}} onLoad={event=>{const image=event.currentTarget;if(image.naturalWidth&&image.naturalHeight)setAspect(image.naturalWidth/image.naturalHeight)}} alt="当前实体工程图，包含所选视图和尺寸标注"/></div></section>
}

export default function EngineeringWorkspace(props) {
  return <EngineeringWorkspaceContent key={props.accountKey || props.token || ''} {...props}/>
}

function EngineeringWorkspaceContent({ token, accountKey, model, generation, showToast, initialTab = 'viewer', active = true, onLogin }) {
  const scope = 'engineering-library'
  const [designs,setDesigns]=useState([]), [current,setCurrent]=useState(null), [tab,setTab]=useState(initialTab==='viewer'?'measure':initialTab)
  const [busy,setBusy]=useState(''), [error,setError]=useState(''), [feedback,setFeedback]=useState('')
  const [a,setA]=useState(''), [b,setB]=useState(''), [measurement,setMeasurement]=useState(null)
  const [instances,setInstances]=useState([]), [sources,setSources]=useState({}), [sourceId,setSourceId]=useState('')
  const [constraints,setConstraints]=useState([]), [mate,setMate]=useState({kind:'concentric',aInstance:'',bInstance:'',a:'',b:'',value:0})
  const [assemblyName,setAssemblyName]=useState('新装配设计')
  const [newAssemblyPending,setNewAssemblyPending]=useState(false),[libraryState,setLibraryState]=useState('loading')
  const [views,setViews]=useState(initialViews), [dimensions,setDimensions]=useState([]), [dimension,setDimension]=useState({viewIndex:0,orientation:'aligned',offset:10,a:'',b:''})
  const [page,setPage]=useState('A3'), [hidden,setHidden]=useState(true), [section,setSection]=useState({axis:'Z',normal:[1,1,0],position:0,sectionMode:'cutaway'}), [preview,setPreview]=useState('')
  const [projectionView,setProjectionView]=useState('front'),[projectionNormal,setProjectionNormal]=useState([1,-1,1])
  const [pickKind,setPickKind]=useState('vertex'), [pickTarget,setPickTarget]=useState('a'), [sourceModelOpen,setSourceModelOpen]=useState(false)
  const upload=useRef(null), operation=useRef(null), epoch=useRef(0), activeRef=useRef(active)
  activeRef.current=active
  const topology=current?.topology||[]
  const markers=useMemo(()=>(tab==='drawing'?[dimension.a,dimension.b]:[a,b]).map(key=>topology.find(item=>topologyKey(item)===key)?.center).filter(Boolean),[topology,a,b,dimension.a,dimension.b,tab])
  const selectTopology=item=>{if(operation.current)return;const key=topologyKey(item);if(tab==='drawing')setDimension(value=>({...value,[pickTarget]:key}));else if(pickTarget==='a')setA(key);else setB(key);setFeedback(`已选中 ${label(item)}，用于${pickTarget==='a'?' A / 起点':' B / 终点'}`)}
  const activate = value => { const saved=value.lastDrawing?.settings;setCurrent(value);setA('');setB('');setMeasurement(null);setDimensions(saved?.dimensions||[]);setDimension({viewIndex:0,orientation:'aligned',offset:10,a:'',b:''});setViews(saved?.views?.map(view=>({x:'',y:'',scale:'',...view}))||initialViews());setPage(saved?.page||'A3');setHidden(saved?.hidden??true);const cut=saved?.views?.find(view=>view.view==='section');setSection(cut?{axis:cut.axis||('normal'in cut?'custom':'Z'),normal:cut.normal||[1,1,0],position:cut.position??cut.z??0,sectionMode:cut.sectionMode||'cutaway'}:{axis:'Z',normal:[1,1,0],sectionMode:'cutaway',position:value.metrics ? (value.metrics.min[2]+value.metrics.max[2])/2 : 0}) }
  const refresh=async(signal)=>{setLibraryState('loading');try{const value=await client.list({token,signal});if(!signal?.aborted){setDesigns(value.items);setLibraryState('ready')}return value.items}catch(error){if(!signal?.aborted)setLibraryState('error');throw error}}
  const work = async(label, fn) => {
    if(operation.current)return
    const controller=new AbortController(), version=epoch.current;operation.current=controller;setBusy(label);setError('');setFeedback('')
    try { const result=await fn(controller.signal);if(version!==epoch.current||controller.signal.aborted)return;setFeedback(`${label}完成`);if(activeRef.current)showToast?.(`${label}完成`,'success');return result }
    catch(error){if(version===epoch.current&&error.name!=='AbortError')setError(error.message)}
    finally{if(operation.current===controller){operation.current=null;setBusy('')}}
  }
  useEffect(()=>{ epoch.current++;operation.current?.abort();operation.current=null;setBusy('');setDesigns([]);setCurrent(null);setSources({});setInstances([]);setConstraints([]);setPreview(''); if(!token)return;const controller=new AbortController();refresh(controller.signal).catch(error=>{if(!controller.signal.aborted)setError(error.message)});return()=>{epoch.current++;controller.abort();operation.current?.abort()} },[accountKey||token])
  useEffect(()=>()=>{if(preview)URL.revokeObjectURL(preview)},[preview])
  useEffect(()=>{setMeasurement(null)},[a,b,current?.id])
  useEffect(()=>{if(active)setTab(['assembly','drawing'].includes(initialTab)?initialTab:'measure')},[initialTab,active])
  async function importFile(file,signal){const value=await client.importStep(file,scope,{token,signal});if(signal.aborted)return;activate(value);await refresh(signal)}
  const currentStep=generation?.artifacts?.find(item=>item.format==='step')
  const canImportCurrent=Boolean(currentStep&&generation?.validation?.productionReady&&generationMatchesModel(model,generation)&&(!model?.agentRun||model.agentRun.status==='ready'))
  const importCurrent=()=>work('导入当前模型',async signal=>{
    if(!canImportCurrent)throw new Error('请先在建模工作台确认并生成当前版本的 STEP。')
    let blob
    if(model.agentRun){blob=(await cadAgent.downloadArtifact({token,signal,runId:model.agentRun.runId,revision:model.agentRun.revision,format:'step',artifactId:currentStep.id})).blob}
    else{const url=new URL(cadArtifactUrl(currentStep),API_BASE);if(url.origin!==new URL(API_BASE).origin)throw new Error('实体地址与当前服务不一致。');const response=await fetch(url,{signal,headers:{Authorization:`Bearer ${token}`}});if(!response.ok)throw new Error('读取当前STEP失败，请重新生成。');blob=await response.blob()}
    await importFile(new File([blob],`${model.name||'当前模型'}.step`,{type:'application/step'}),signal)
  })
  const addInstance=()=>work('添加实例',async signal=>{const source=await client.get(sourceId,{token,signal});if(signal.aborted)return;setSources(v=>({...v,[source.id]:source}));setInstances(v=>[...v,{id:`part${Date.now()}${v.length}`,designId:source.id,name:source.name,position:[v.length*30,0,0],rotation:[0,0,0],fixed:v.length===0}])})
  const changeVector=(index,kind,axis,value)=>setInstances(items=>items.map((item,i)=>i===index?{...item,[kind]:item[kind].map((v,j)=>j===axis?value:v)}:item))
  const mateTopology=key=>{const instance=instances.find(item=>item.id===mate[key]);return (sources[instance?.designId]?.topology||[]).filter(item=>mate.kind==='distance'||mate.kind==='concentric'&&['CIRCLE','CYLINDER'].includes(item.type)||mate.kind==='coincident'&&item.kind==='face'&&item.type==='PLANE')}
  const localEdit=fn=>{try{fn();setError('');setFeedback('')}catch(error){setError(error.message)}}
  const resetAssembly=()=>{setInstances([]);setConstraints([]);setSources({});setSourceId('');setAssemblyName('新装配设计');setMate({kind:'concentric',aInstance:'',bInstance:'',a:'',b:'',value:0});setNewAssemblyPending(false);setTab('assembly');setFeedback('已建立空白装配草稿。已保存的设计仍保留在工程库。');setError('')}
  const removeInstance=id=>{setInstances(items=>items.filter(item=>item.id!==id));setConstraints(items=>items.filter(item=>item.aInstance!==id&&item.bInstance!==id));setMate(value=>({...value,...(value.aInstance===id?{aInstance:'',a:''}:{}),...(value.bInstance===id?{bInstance:'',b:''}:{})}))}
  const addMate=()=>localEdit(()=>{if(!instances.some(item=>item.id===mate.aInstance)||!instances.some(item=>item.id===mate.bInstance)||mate.aInstance===mate.bInstance||!topologySelector(mate.a)||!topologySelector(mate.b))throw new Error('请选择两个不同实例及各自的拓扑实体。');const value={...mate,a:topologySelector(mate.a),b:topologySelector(mate.b),value:mate.kind==='distance'?engineeringNumber(mate.value,'中心距',{min:0}):0};if(constraints.some(item=>JSON.stringify(item)===JSON.stringify(value)))throw new Error('此配合已添加，无需重复添加。');setConstraints(items=>[...items,value])})
  const assemblyPayload=()=>({fileId:scope,name:assemblyName,instances:instances.map((item,index)=>({...item,position:item.position.map((v,i)=>engineeringNumber(v,`实例 ${index+1} 位置 ${'XYZ'[i]}`)),rotation:item.rotation.map((v,i)=>engineeringNumber(v,`实例 ${index+1} 旋转 ${'XYZ'[i]}`))})),constraints})
  const createAssembly=()=>work('生成装配并检查干涉',async signal=>{const value=await client.assemble(assemblyPayload(),{token,signal});if(signal.aborted)return;activate(value);setInstances(value.instances);await refresh(signal)})
  const addDimension=()=>localEdit(()=>{const radial=['diameter','radius'].includes(dimension.orientation),entity=topology.find(item=>topologyKey(item)===dimension.a);if(!entity||(!radial&&!topologySelector(dimension.b)))throw new Error('请先选择尺寸实体；直径和半径只需要圆柱面或圆边。');if(radial&&!['CYLINDER','CIRCLE'].includes(entity.type))throw new Error('直径和半径需要选择圆柱面或圆形边。');if(!views[dimension.viewIndex])throw new Error('请重新选择尺寸所属视图。');const offset=engineeringNumber(dimension.offset,'标注偏移',{min:-100,max:100});setDimensions(items=>[...items,{...dimension,a:topologySelector(dimension.a),b:radial?null:topologySelector(dimension.b),viewIndex:Number(dimension.viewIndex),offset}])})
  const removeView=index=>{const next=removeEngineeringView({views,dimensions,dimension},index);setViews(next.views);setDimensions(next.dimensions);setDimension(next.dimension);setFeedback(`已移除视图${next.removedDimensions?`及其 ${next.removedDimensions} 处尺寸`:''}，其他视图尺寸已保留。`)}
  const createDrawing=()=>work('生成工程图',async signal=>{
    const settings=engineeringDrawingSettings({page,hidden,views,dimensions})
    const value=await client.drawing(current.id,{title:current.name,...settings},{token,signal});if(signal.aborted)return;setCurrent(value)
  })
  const centerSection=next=>localEdit(()=>{const n=engineeringDirection(next.axis==='custom'?next.normal:axisNormal(next.axis)),length=Math.hypot(...n);const center=current.metrics.min.map((v,i)=>(v+current.metrics.max[i])/2);setSection({...next,position:center.reduce((sum,v,i)=>sum+v*n[i]/length,0)})})
  let drawingDirty=false,assemblyDirty=false,displayPlane
  try{drawingDirty=!!current?.lastDrawing&&!engineeringDraftEquals(engineeringDrawingSettings({page,hidden,views,dimensions}),current.lastDrawing.settings)}catch{drawingDirty=!!current?.lastDrawing}
  try{const draft=assemblyPayload();assemblyDirty=instances.length>0&&(current?.kind!=='assembly'||assemblyName!==current.name||!engineeringDraftEquals(draft.instances,current.instances)||!engineeringDraftEquals(constraints,current.constraints))}catch{assemblyDirty=true}
  try{displayPlane={normal:engineeringDirection(section.axis==='custom'?section.normal:axisNormal(section.axis)),position:engineeringNumber(section.position,'剖切位置')}}catch{displayPlane={invalid:true}}
  const previewKey=Object.keys(current?.lastDrawing?.artifacts||{}).find(key=>key.endsWith('.svg'))
  useEffect(()=>{setPreview('');if(!current?.id||!previewKey)return;const control=new AbortController();client.artifact(current.id,previewKey,{token,signal:control.signal}).then(blob=>{if(!control.signal.aborted)setPreview(URL.createObjectURL(blob))}).catch(error=>{if(!control.signal.aborted)setError(`工程图已保存，但预览加载失败：${error.message}。可下载已保存图纸。`)});return()=>control.abort()},[current?.id,previewKey,token])
  if(!token)return <section className="eng-workspace"><h1>工程工作台</h1><p>请先登录，以保存独立的 STEP 设计、装配与工程图。</p>{onLogin&&<button className="eng-primary" onClick={onLogin}>登录并继续工程设计</button>}</section>
  return <section className="eng-workspace" aria-label="工程工作台">
    <header className="eng-heading"><div><h1>工程设计</h1></div><div className="eng-actions"><input ref={upload} type="file" accept=".step,.stp" hidden onChange={event=>{const file=event.target.files?.[0];event.target.value='';if(file)work('导入 STEP',signal=>importFile(file,signal))}}/><button disabled={!!busy} onClick={()=>upload.current.click()}>＋ 上传 STEP / STP</button><button disabled={!!busy||!canImportCurrent} title={canImportCurrent?'导入当前已确认实体':'先在建模工作台生成并确认当前实体'} onClick={importCurrent}>导入当前模型</button></div></header>
    {busy&&<p role="status" className="eng-notice">{busy}…</p>}{error&&<p role="alert" className="eng-error">{error}</p>}{feedback&&!busy&&<p role="status" className="eng-notice">{feedback}</p>}
    <fieldset className="eng-edit-lock" disabled={!!busy}><div className="eng-layout"><aside className="eng-library"><div className="eng-row"><h2>我的工程设计</h2><button disabled={!!busy} onClick={()=>work('刷新设计',refresh)}>刷新</button></div>{designs.length?designs.map(item=><button className={`eng-design ${current?.id===item.id?'selected':''}`} key={item.id} disabled={!!busy} onClick={()=>work('打开设计',async signal=>{const value=await client.get(item.id,{token,signal});if(!signal.aborted)activate(value)})}><b>{item.name}</b><small>{item.kind==='assembly'?'装配体':'零件'} · {item.metrics?.solidCount} 个实体</small></button>):<p>{libraryState==='loading'?'正在加载工程设计…':libraryState==='error'?'设计列表加载失败，请点击刷新重试。':'上传第一个 STEP 后，可测量点、边、面并生成工程图。'}</p>}<button disabled={!!busy} onClick={()=>{setTab('assembly');if(instances.length)setNewAssemblyPending(true);else resetAssembly()}}>建立新装配</button></aside>
    <main className="eng-main"><nav className="eng-tabs" aria-label="工程工具">{[['measure','实体测量'],['assembly','真实装配'],['drawing','二维工程图']].map(([key,title])=><button key={key} aria-pressed={tab===key} onClick={()=>setTab(key)}>{title}</button>)}</nav>
      {current&&<><div className="eng-title"><h2>{current.name}</h2><div className="eng-actions">{['step','glb','bom'].filter(k=>current.artifacts?.[k]).map(k=><SaveBlob key={k} token={token} design={current} artifactKey={k} onError={setError}>{k.toUpperCase()} ↓</SaveBlob>)}</div></div>{tab==='drawing'&&<>{drawingDirty&&<p className="eng-notice" role="status">图纸参数已修改，尚未重新生成。下方预览与下载仍为上次保存的图纸。</p>}{preview&&<EngineeringDrawingPreview src={preview}/>}</>}<details className={`eng-model-reference${tab==='drawing'?' eng-disclosure':''}`} open={tab!=='drawing'||sourceModelOpen} onToggle={event=>{if(tab==='drawing')setSourceModelOpen(event.currentTarget.open)}}><summary hidden={tab!=='drawing'}>三维参考与标注点选</summary>{tab!=='assembly'&&<div className="eng-pick-tools"><label>点选对象<select aria-label="三维点选类型" value={pickKind} onChange={e=>setPickKind(e.target.value)}><option value="vertex">顶点</option><option value="edge">边中心</option><option value="face">面中心</option></select></label><div><button aria-pressed={pickTarget==='a'} onClick={()=>setPickTarget('a')}>选择 A / 起点</button><button aria-pressed={pickTarget==='b'} onClick={()=>setPickTarget('b')}>选择 B / 终点</button></div></div>}<EngineeringModelCanvas active={active&&(tab!=='drawing'||sourceModelOpen)} token={token} design={current} markers={tab==='assembly'?[]:markers} pickKind={pickKind} onSelectTopology={tab==='assembly'?undefined:selectTopology} sectionPlane={tab==='drawing'?displayPlane:undefined}/><div className="eng-metrics"><span>{current.metrics.solidCount} 个实体</span><span>包络 {current.metrics.size.map(num).join(' × ')} mm</span><span>体积 {num(current.metrics.volume)} mm³</span></div></details></>}
      {tab==='measure'&&(current?<div className="eng-panel"><h3>选择拓扑实体测量</h3><p>编号绑定当前 STEP 文件；彩色点标出所选实体的中心。最短距离由实体内核计算。</p><div className="eng-fields"><TopologySelect title="实体 A（粉色）" topology={topology} value={a} onChange={setA}/><TopologySelect title="实体 B（橙色，可选）" optional topology={topology} value={b} onChange={setB}/></div>{current.topologyTruncated&&<p>当前列表每类显示前 1500 个实体；大型模型请拆分后测量。</p>}<button disabled={!!busy||!a} onClick={()=>work('测量',async signal=>{const value=await client.measure(current.id,{a:topologySelector(a),...(b?{b:topologySelector(b)}:{})},{token,signal});if(!signal.aborted)setMeasurement(value)})}>计算实体测量</button>{measurement&&<dl className="eng-result">{[['最短距离',measurement.minimumDistance,'mm'],['中心距离',measurement.centerDistance,'mm'],['夹角',measurement.angleDegrees,'°'],['边长',measurement.a.length,'mm'],['面积',measurement.a.area,'mm²'],['直径',measurement.a.diameter,'mm']].filter(([,v])=>v!==undefined).map(([key,value,unit])=><div key={key}><dt>{key}</dt><dd>{num(value)} {unit}</dd></div>)}<div><dt>A 中心</dt><dd>{measurement.a.center.map(num).join(', ')} mm</dd></div></dl>}</div>:<div className="eng-panel"><h2>先选择或上传一个 STEP</h2><p>支持真实点坐标、边长、面面积、圆柱直径、最短距离和中心距。</p></div>)}
      <div className="eng-panel" hidden={tab!=='assembly'}>{newAssemblyPending&&<div className="eng-notice" role="alert"><p>当前装配草稿已有 {instances.length} 个实例。建立新装配将清空草稿；已保存的设计仍保留。</p><div className="eng-actions"><button onClick={resetAssembly}>清空草稿，建立新装配</button><button onClick={()=>setNewAssemblyPending(false)}>保留当前草稿</button></div></div>}<AssemblyAIComposer token={token} accountKey={accountKey} fileId={scope} onReady={(id,{automatic=false}={})=>work(automatic&&instances.length?'更新 AI 装配任务':'打开 AI 装配',async signal=>{if(automatic&&(instances.length||drawingDirty)){await refresh(signal);if(!signal.aborted)setError('AI 装配已生成。当前工程草稿已保留，可在 AI 任务中点击打开新装配。');return}const value=await client.get(id,{token,signal});const loaded={};for(const source of [...new Set(value.instances.map(item=>item.designId))])loaded[source]=await client.get(source,{token,signal});if(signal.aborted)return;activate(value);setInstances(value.instances);setConstraints(value.constraints||[]);setSources(loaded);setAssemblyName(value.name);await refresh(signal)})}/><h3>零件实例与位置</h3>{assemblyDirty&&<p className="eng-notice" role="status">装配草稿尚未生成。上方模型和下载文件仍为已保存设计；请生成装配以更新结果。</p>}{!instances.length&&<p>装配草稿为空。先上传 STEP，再从工程设计中添加实例；也可填写上方需求让 AI 生成。</p>}<div className="eng-fields"><label>装配名称<input value={assemblyName} onChange={event=>setAssemblyName(event.target.value)} maxLength={180}/></label><label>从工程设计插入<select value={sourceId} onChange={event=>setSourceId(event.target.value)}><option value="">选择已上传的真实实体</option>{designs.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label><button disabled={!!busy||!sourceId||instances.length>=30} onClick={addInstance}>添加实例</button></div>
        <div className="eng-table-scroll"><table><thead><tr><th>零件 / 固定</th><th>位置 X / Y / Z（mm）</th><th>旋转 X / Y / Z（°）</th><th>操作</th></tr></thead><tbody>{instances.map((item,index)=><tr key={item.id}><td><b>{index+1}. {item.name}</b><label><input type="checkbox" checked={item.fixed} onChange={e=>setInstances(v=>v.map((x,i)=>i===index?{...x,fixed:e.target.checked}:x))}/>固定</label></td>{['position','rotation'].map(kind=><td key={kind}><div className="eng-vector">{item[kind].map((value,axis)=><input key={axis} aria-label={`${item.name} ${kind} ${'XYZ'[axis]}`} type="number" value={value} onChange={e=>changeVector(index,kind,axis,e.target.value)}/>)}</div></td>)}<td><button onClick={()=>removeInstance(item.id)}>移除</button></td></tr>)}</tbody></table></div>
        <details className="eng-disclosure eng-mates"><summary>几何配合 <small>{constraints.length} 条</small></summary><p>同轴：圆柱面或圆边。重合：两个平面中心重合且法向相反。距离：所选实体中心之间的距离。求解后逐条验证残差。</p><div className="eng-fields"><label>配合类型<select value={mate.kind} onChange={e=>setMate({...mate,kind:e.target.value,a:'',b:''})}><option value="concentric">同轴</option><option value="coincident">平面重合</option><option value="distance">中心距离</option></select></label>{[['aInstance','a','A'],['bInstance','b','B']].map(([key,entity,title])=><div key={key}><label>实例 {title}<select value={mate[key]} onChange={e=>setMate({...mate,[key]:e.target.value,[entity]:''})}><option value="">请选择实例</option>{instances.map((item,i)=><option key={item.id} value={item.id}>{i+1}. {item.name}</option>)}</select></label><TopologySelect title={`拓扑 ${title}`} topology={mateTopology(key)} value={mate[entity]} onChange={value=>setMate({...mate,[entity]:value})}/></div>)}{mate.kind==='distance'&&<label>中心距 / mm<input type="number" min="0" value={mate.value} onChange={e=>setMate({...mate,value:e.target.value})}/></label>}<button disabled={!!busy||instances.length<2||constraints.length>=60} onClick={addMate}>添加配合</button></div>
        {constraints.map((item,i)=><div className="eng-list-row" key={i}><span>{i+1}. {{concentric:'同轴',coincident:'重合',distance:'中心距'}[item.kind]} · {instances.find(part=>part.id===item.aInstance)?.name||item.aInstance} → {instances.find(part=>part.id===item.bInstance)?.name||item.bInstance}{item.kind==='distance'?` · ${item.value} mm`:''}</span><button onClick={()=>setConstraints(v=>v.filter((_,j)=>i!==j))}>删除</button></div>)}</details><button className="eng-primary" disabled={!!busy||!instances.length} onClick={createAssembly}>生成装配 · 检查实体干涉</button>
        {current?.kind==='assembly'&&<><h3>当前已保存装配的检查结果</h3><button disabled={!!busy} onClick={()=>work('载入装配编辑',async signal=>{const loaded={};for(const id of [...new Set(current.instances.map(item=>item.designId))])loaded[id]=await client.get(id,{token,signal});if(signal.aborted)return;setSources(loaded);setInstances(current.instances);setConstraints(current.constraints||[]);setAssemblyName(current.name)})}>继续编辑此装配</button><p>{current.constraintResults.length} 条配合通过；{current.interference.filter(x=>x.interferes).length} 对零件存在实体重叠。</p>{current.interference.filter(x=>x.interferes).map(item=><p className="eng-error" key={`${item.a}-${item.b}`}>{item.a} / {item.b}：干涉体积 {num(item.volumeMm3)} mm³</p>)}<table><thead><tr><th>BOM 零件</th><th>数量</th></tr></thead><tbody>{current.bom.map(item=><tr key={item.designId}><td>{item.name}</td><td>{item.quantity}</td></tr>)}</tbody></table></>}
      </div>
      {tab==='drawing'&&(current?<div className="eng-panel"><h3>工程图设置</h3><div className="eng-fields"><label>图幅<select value={page} onChange={e=>setPage(e.target.value)}><option>A3</option><option>A4</option></select></label><label><input type="checkbox" checked={hidden} onChange={e=>setHidden(e.target.checked)}/>显示隐藏线</label><label>添加投影视图<select value={projectionView} onChange={e=>setProjectionView(e.target.value)}>{Object.entries(viewNames).filter(([key])=>key!=='section').map(([key,title])=><option key={key} value={key}>{title}</option>)}</select></label>{projectionView==='custom'&&<DirectionInput value={projectionNormal} onChange={setProjectionNormal} label="投影法向"/>}<button disabled={views.length>=8||!!busy} onClick={()=>localEdit(()=>{const normal=projectionView==='custom'?engineeringDirection(projectionNormal,'投影法向'):null;setViews(v=>[...v,{view:projectionView,...(normal?{normal}:{}),x:'',y:'',scale:''}])})}>添加投影</button></div><details className="eng-disclosure"><summary>剖视与断面</summary><div className="eng-fields eng-section-builder"><label>剖视内容<select value={section.sectionMode} onChange={e=>setSection({...section,sectionMode:e.target.value})}><option value="cutaway">完整剖视（保留切后实体）</option><option value="cross_section">仅断面轮廓</option></select></label><label>剖切方向<select value={section.axis} onChange={e=>centerSection({...section,axis:e.target.value})}><option>X</option><option>Y</option><option>Z</option><option value="custom">任意法向</option></select></label>{section.axis==='custom'&&<DirectionInput value={section.normal} onChange={normal=>setSection({...section,normal})} label="剖切法向"/>}<label>{section.axis==='custom'?'沿单位法向距原点 / mm':`${section.axis} 坐标 / mm`}<input type="number" step="any" value={section.position} onChange={e=>setSection({...section,position:e.target.value})}/></label><button onClick={()=>centerSection(section)}>平面穿过模型中心</button><button onClick={()=>localEdit(()=>setSection({...section,axis:'custom',normal:engineeringDirection(section.axis==='custom'?section.normal:axisNormal(section.axis)).map(v=>-v),position:-engineeringNumber(section.position,'剖切位置')}))}>反向剖切</button><button onClick={()=>localEdit(()=>{const position=engineeringNumber(section.position,'剖切位置'),normal=section.axis==='custom'?engineeringDirection(section.normal,'剖切法向'):null;setViews(v=>[...v,{view:'section',axis:section.axis,position,sectionMode:section.sectionMode,...(normal?{normal}:{}),x:'',y:'',scale:''}])})} disabled={views.length>=8||!!busy}>添加真实剖面</button></div><p>完整剖视对实体作半空间裁切后投影，并标出真实截面；仅断面模式显示截面轮廓。任意法向会自动单位化，位置可正可负。上方“剖切显示”同步使用此平面。曲线离散精度 0.05 mm。</p></details>
        <details className="eng-disclosure"><summary>视图布局与比例 <small>{views.length} 个视图</small></summary><p>布局坐标以图纸左下角为原点；留空自动布局。</p><div className="eng-table-scroll"><table><thead><tr><th>视图</th><th>中心 X</th><th>中心 Y</th><th>比例</th><th>操作</th></tr></thead><tbody>{views.map((item,index)=><tr key={index}><td>{viewLabel(item)}</td>{['x','y','scale'].map(key=><td key={key}><input aria-label={`视图${index+1} ${key}`} type="number" placeholder="自动" value={item[key]} onChange={e=>setViews(v=>v.map((x,i)=>i===index?{...x,[key]:e.target.value}:x))}/></td>)}<td><button disabled={views.length===1} onClick={()=>removeView(index)}>移除</button></td></tr>)}</tbody></table></div></details>
        <details className="eng-disclosure"><summary>实体尺寸标注 <small>{dimensions.length} 项</small></summary><div className="eng-fields"><label>视图<select value={dimension.viewIndex} onChange={e=>setDimension({...dimension,viewIndex:Number(e.target.value)})}>{views.map((v,i)=><option key={i} value={i}>{i+1}. {viewLabel(v)}</option>)}</select></label><TopologySelect title="起点实体" topology={topology} value={dimension.a} onChange={a=>setDimension({...dimension,a})}/><TopologySelect title="终点实体" topology={topology} value={dimension.b} onChange={b=>setDimension({...dimension,b})}/><label>方向<select value={dimension.orientation} onChange={e=>setDimension({...dimension,orientation:e.target.value})}><option value="aligned">对齐</option><option value="horizontal">水平</option><option value="vertical">垂直</option><option value="diameter">直径（只选起点圆）</option><option value="radius">半径（只选起点圆）</option></select></label><label>标注偏移 / 图纸 mm<input type="number" value={dimension.offset} onChange={e=>setDimension({...dimension,offset:e.target.value})}/></label><button onClick={addDimension} disabled={!!busy||dimensions.length>=100}>添加尺寸</button></div>{dimensions.map((item,i)=><div className="eng-list-row" key={i}><span>尺寸 {i+1} · 视图 {item.viewIndex+1} · {kinds[item.a.kind]} {item.a.index+1}{item.b?` → ${kinds[item.b.kind]} ${item.b.index+1}`:` · ${item.orientation==='diameter'?'直径':'半径'}`}</span><button onClick={()=>setDimensions(v=>v.filter((_,j)=>i!==j))}>删除</button></div>)}</details><button className="eng-primary" disabled={!!busy} onClick={createDrawing}>生成 DXF / PDF 工程图</button>{current.lastDrawing&&<div className="eng-actions">{Object.keys(current.lastDrawing.artifacts).map(key=><SaveBlob key={key} token={token} design={current} artifactKey={key} onError={setError}>{current.artifacts[key].label} ↓</SaveBlob>)}</div>}</div>:<div className="eng-panel"><p>先选择一个零件或装配设计。</p></div>)}
    </main></div></fieldset>
  </section>
}
