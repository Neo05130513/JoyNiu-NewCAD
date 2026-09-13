import { createClientId } from './clientId.js'
import { useEffect, useRef, useState } from 'react'
import { nativeDrawingRequest as request, saveNativeBlob } from './nativeDrawingClient.js'
import { CONSTRAINT_TYPES, PROFESSIONAL_SYMBOLS, drawingBounds, evaluateFormula, professionalSymbol, snapPoint, solveConstraints, transformEntity } from './nativeDrawingModel.js'
import { nativeDimensionDraft, nativeLayouts, validNativeLayout, visibleNativeEntities, nativePolylineText, parseNativePolylineText, nativePropertyDirty, nativeVisibleRenderEntities, nativeDialogKey } from './nativeDrawingDraft.js'
import NativeParametricLibrary from './NativeParametricLibrary.jsx'
import './native-drawing.css'
const TOOLS={select:'选择',line:'直线 L',rectangle:'矩形 REC',circle:'圆 C',arc:'圆弧 A',polyline:'多段线 PL',text:'文字 T',dimension:'标注 D',bubble:'气泡 B'}
const TOOL_GROUPS={select:{label:'选择',tools:['select'],initial:'select'},draw:{label:'绘制',tools:['line','rectangle','circle','arc','polyline'],initial:'line'},annotate:{label:'标注',tools:['dimension','text','bubble'],initial:'dimension'},transform:{label:'变换',tools:[],initial:'select'}}
const TRANSFORMS={move:'移动',copy:'复制',rotate:'旋转',scale:'缩放',mirror:'镜像副本',array:'矩形阵列',polar:'环形阵列',offset:'偏移',trim:'裁剪直线'}
const COLORS=['#b6c0cc','#ef5c63','#e8c14c','#52c58c','#48bccf','#6194ff','#ba83ef','#d5dce5','#8694a8']
const colorOf=e=>e.trueColor!==undefined?`#${Number(e.trueColor).toString(16).padStart(6,'0')}`:COLORS[(e.color>0&&e.color<9?e.color:7)]
const displayEntity=(e,layers)=>{const l=layers.find(l=>l.name===e.layer);return {...e,color:e.color===256?(l?.color||7):e.color,linetype:(e.linetype||'BYLAYER').toUpperCase()==='BYLAYER'?(l?.linetype||'CONTINUOUS'):e.linetype,lineweight:e.lineweight===-1?(l?.lineweight??-1):e.lineweight}}
const fmt=n=>Number(Number(n||0).toFixed(4))
const entityChanges=e=>{const fields={LINE:['start','end'],CIRCLE:['center','radius'],ARC:['center','radius','startAngle','endAngle'],LWPOLYLINE:['points','closed'],TEXT:['position','height','rotation','text'],MTEXT:['position','height','rotation','text']}[e.type]||[];return Object.fromEntries(Object.entries(e).filter(([k])=>['layer','color','linetype','lineweight',...fields].includes(k)))}
function arcPath(e){const a=e.startAngle*Math.PI/180,b=e.endAngle*Math.PI/180,r=e.radius,[x,y]=e.center,s=[x+r*Math.cos(a),y+r*Math.sin(a)],t=[x+r*Math.cos(b),y+r*Math.sin(b)];return `M ${s[0]} ${s[1]} A ${r} ${r} 0 ${((e.endAngle-e.startAngle+360)%360)>180?1:0} 1 ${t[0]} ${t[1]}`}
function polylinePath(e){const p=e.points||[];if(!p.length)return '';let d=`M${p[0][0]} ${p[0][1]}`;for(let i=0;i<(e.closed?p.length:p.length-1);i++){const a=p[i],b=p[(i+1)%p.length],bulge=a[2]||0,chord=Math.hypot(b[0]-a[0],b[1]-a[1]);if(Math.abs(bulge)>1e-9&&chord>0){const r=chord*(1+bulge*bulge)/(4*Math.abs(bulge));d+=` A${r} ${r} 0 ${Math.abs(bulge)>1?1:0} ${bulge>0?1:0} ${b[0]} ${b[1]}`}else d+=` L${b[0]} ${b[1]}`}return d+(e.closed?' Z':'')}
function Entity({entity:e,selected,onSelect,scale=1}) {
  const props={stroke:selected?'#63d5ff':colorOf(e),strokeWidth:selected?2.4:Math.max(1.1,(e.lineweight>0?e.lineweight/100:0)*3.78),strokeDasharray:e.linetype==='DASHED'?'8 4':e.linetype==='CENTER'?'12 3 2 3':undefined,vectorEffect:'non-scaling-stroke',fill:e.filled?colorOf(e):'none',onPointerDown:onSelect,'data-entity':e.id,style:{cursor:'pointer'},className:'native-entity'}
  if(e.type==='LINE')return <line {...props} x1={e.start[0]} y1={e.start[1]} x2={e.end[0]} y2={e.end[1]}/>
  if(e.type==='CIRCLE')return <circle {...props} cx={e.center[0]} cy={e.center[1]} r={e.radius}/>
  if(e.type==='ARC')return <path {...props} d={arcPath(e)}/>
  if(e.type==='LWPOLYLINE')return <path {...props} d={polylinePath(e)}/>
  if(e.type==='TEXT'||e.type==='MTEXT')return <text {...props} stroke="none" fill={props.stroke} transform={`translate(${e.position[0]},${e.position[1]}) rotate(${e.rotation||0}) scale(1,-1)`} fontSize={e.height||2.5} textAnchor={e.anchor||'start'} dominantBaseline={e.verticalAnchor==='middle'?'central':e.verticalAnchor==='top'?'hanging':'auto'}>{e.stackedTolerance?<><tspan>{e.stackedTolerance.base}</tspan><tspan dx={(e.height||2.5)*.15} dy={-(e.height||2.5)*.35} fontSize={(e.height||2.5)*.5}>{e.stackedTolerance.upper}</tspan><tspan dx={-(e.height||2.5)*.5*e.stackedTolerance.upper.length*.6} dy={(e.height||2.5)*.7} fontSize={(e.height||2.5)*.5}>{e.stackedTolerance.lower}</tspan></>:e.text}</text>
  if(e.type==='DIMENSION'){const p=e.points?.defpoint2||e.points?.defpoint||[0,0],q=e.points?.defpoint3||p,b=e.points?.defpoint||p;return <g {...props}><path d={`M${p[0]} ${p[1]} L${p[0]} ${b[1]} L${q[0]} ${b[1]} L${q[0]} ${q[1]}`}/><text stroke="none" fill={props.stroke} transform={`translate(${(p[0]+q[0])/2},${b[1]+3*scale}) scale(1,-1)`} fontSize={Math.max(e.style?.dimtxt||2.5,3*scale)} textAnchor="middle">{e.text&&e.text!=='<>'?e.text:fmt(e.value)}{e.toleranceEnabled?` +${e.toleranceUpper}/−${e.toleranceLower}`:''}</text></g>}
  if(e.position)return <g {...props}><circle cx={e.position[0]} cy={e.position[1]} r={4*scale}/><text fill={props.stroke} transform={`translate(${e.position[0]},${e.position[1]}) scale(1,-1)`} fontSize={4*scale}>{e.block||e.type}</text></g>
  return null
}
function Numeric({label,value,onChange,min,step='any'}){
  const [draft,setDraft]=useState(String(fmt(value)))
  // Undo/redo can change the source value while macOS leaves this input focused.
  // An unchanged numeric value does not rerun this effect, so "0." stays editable.
  useEffect(()=>{setDraft(String(fmt(value)))},[value])
  return <label>{label}<input type="number" step={step} min={min} value={draft} onBlur={()=>{if(draft.trim()===''||!Number.isFinite(Number(draft)))setDraft(String(fmt(value)))}} onChange={e=>{setDraft(e.target.value);if(e.target.value.trim()!==''&&Number.isFinite(Number(e.target.value)))onChange(Number(e.target.value))}}/></label>
}
export default function NativeDrawingWorkspace({token,accountKey=token,showToast=()=>{},active=true}){
  const [toolGroup,setToolGroup]=useState('select'),[transformKind,setTransformKind]=useState('move')
  const [documents,setDocuments]=useState([]),[doc,setDoc]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[tool,setTool]=useState('select'),[selection,setSelection]=useState([]),[view,setView]=useState({x:-100,y:-75,width:200,height:150}),[points,setPoints]=useState([]),[cursor,setCursor]=useState(null),[grid,setGrid]=useState(1),[layer,setLayer]=useState('0'),[command,setCommand]=useState(''),[tab,setTab]=useState('属性'),[name,setName]=useState('新图纸'),[text,setText]=useState('文字'),[textHeight,setTextHeight]=useState(5),[bubbleNumber,setBubbleNumber]=useState(1),[dimensionKind,setDimensionKind]=useState('linear'),[fields,setFields]=useState({dx:10,dy:0,angle:90,scale:1,count:4,offset:5,trimStart:0,trimEnd:.5}),[property,setProperty]=useState(null),[constraintType,setConstraintType]=useState('length'),[constraintValue,setConstraintValue]=useState('100'),[parameterName,setParameterName]=useState('width'),[parameterValue,setParameterValue]=useState('100'),[symbol,setSymbol]=useState('wall'),[symbolWidth,setSymbolWidth]=useState(3000),[symbolHeight,setSymbolHeight]=useState(240),[paper,setPaper]=useState('A4'),[printScale,setPrintScale]=useState('fit'),[printColor,setPrintColor]=useState('original'),[printLayout,setPrintLayout]=useState('Model'),[preview,setPreview]=useState(''),[endA,setEndA]=useState(0),[endB,setEndB]=useState(0),[pointText,setPointText]=useState(''),[recovery,setRecovery]=useState(null),[transition,setTransition]=useState(null),[canWrite,setCanWrite]=useState(null),[libraryDirty,setLibraryDirty]=useState(false),[libraryReset,setLibraryReset]=useState(0),[landscape,setLandscape]=useState(false),[connectionRetry,setConnectionRetry]=useState(0)
  const tokenRef=useRef(token),authSeen=useRef({accountKey,token});tokenRef.current=token
  const accountRequest=(path,options={})=>request(path,{...options,token:()=>typeof tokenRef.current==='function'?tokenRef.current():tokenRef.current})
  const recoveryRef=useRef(null),unsavedRef=useRef(false),epoch=useRef(0),busyRef=useRef(false),svg=useRef(null),pan=useRef(null),root=useRef(null),docRef=useRef(doc),previewRef=useRef('');docRef.current=doc;recoveryRef.current=recovery
  const drawingUnit=({0:'图纸单位',1:'in',2:'ft',4:'mm',5:'cm',6:'m'})[doc?.units]||'图纸单位'
  const properties=doc?.customProperties||{},constraints=properties.constraints||[],parameters=properties.parameters||{},selected=(doc?.entities||[]).filter(e=>selection.includes(e.id)),scale=view.width/900
  const propertyDirty=nativePropertyDirty(doc,property,pointText),writeBlocked=busy||canWrite!==true||!!recovery
  unsavedRef.current=busy||propertyDirty||points.length>0||libraryDirty||!!recovery
  const clearPreview=()=>{if(previewRef.current)URL.revokeObjectURL(previewRef.current);previewRef.current='';setPreview('')}
  useEffect(()=>{
    let cancelled=false;epoch.current++;busyRef.current=true;setBusy(true);setDoc(null);setDocuments([]);setSelection([]);setPoints([]);setCursor(null);setPrintLayout('Model');setLayer('0');setError('');setRecovery(null);setTransition(null);setCanWrite(null);clearPreview()
    if(token)Promise.all([accountRequest('',{token}),accountRequest('/capabilities',{token})]).then(async([listing,capabilities])=>{
      if(cancelled)return
      const items=Array.isArray(listing)?listing:listing.items||[];setDocuments(items);setCanWrite(capabilities.canWrite===true)
      let remembered;try{remembered=sessionStorage.getItem('native-drawing:last-open-id')}catch{/* Storage may be unavailable. */}
      if(items.some(item=>item.id===remembered)){
        const result=await accountRequest(`/${remembered}`,{token})
        if(!cancelled){setDoc(result);setName(result.name);fit(result)}
      }
    }).catch(e=>{if(!cancelled)setError(e.message)}).finally(()=>{if(!cancelled){busyRef.current=false;setBusy(false)}})
    else {busyRef.current=false;setBusy(false)}
    return()=>{cancelled=true;epoch.current++}
  },[accountKey,connectionRetry])
  useEffect(()=>{
    const previous=authSeen.current;authSeen.current={accountKey,token}
    if(previous.accountKey!==accountKey||previous.token===token||!token)return
    const scope=epoch.current
    accountRequest('/capabilities').then(result=>{if(scope===epoch.current)setCanWrite(result.canWrite===true)}).catch(error=>{if(scope===epoch.current)setError(error.message)})
  },[token,accountKey])
  useEffect(()=>{const entity=selected.length===1?structuredClone(selected[0]):null;setProperty(entity);setPointText(entity?.type==='LWPOLYLINE'?nativePolylineText(entity.points):'')},[doc,selection.join(',')])
  useEffect(()=>{if(!active)pan.current=null},[active])
  useEffect(()=>{const warn=e=>{if(unsavedRef.current){e.preventDefault();e.returnValue=''}};window.addEventListener('beforeunload',warn);return()=>{window.removeEventListener('beforeunload',warn);if(previewRef.current)URL.revokeObjectURL(previewRef.current)}},[])
  useEffect(clearPreview,[paper,printScale,printColor,printLayout,landscape])
  const act=async(fn,options={})=>{
    if(busyRef.current)return
    busyRef.current=true;setBusy(true);setError('');const scope=epoch.current
    try{
      const result=await fn();if(scope!==epoch.current)return
      if(result?.entities){
        if(result.id!==docRef.current?.id)setSelection([])
        else setSelection(current=>current.filter(id=>result.entities.some(entity=>entity.id===id)))
        setPoints([]);setCursor(null);setPrintLayout(current=>validNativeLayout(result.layouts,current));setLayer(current=>result.layers.some(item=>item.name===current)?current:result.layers.some(item=>item.name==='0')?'0':result.layers[0]?.name||'0')
        docRef.current=result;setDoc(result);setName(result.name);setDocuments(old=>[result,...old.filter(d=>d.id!==result.id)]);clearPreview()
        try{sessionStorage.setItem('native-drawing:last-open-id',result.id)}catch{/* The server save is already durable. */}
      }
      if(options.mutation){recoveryRef.current=null;setRecovery(null)}
      options.onSuccess?.(result);return result
    }catch(e){
      if(scope===epoch.current){
        setError(e.message)
        if(options.mutation&&e.retryable){const pending={fn,options};recoveryRef.current=pending;setRecovery(pending)}
        else options.onFailure?.(e)
        if(e.status===409)showToast('图纸版本有变化，本次修改未覆盖云端版本，请重新读取后核对','error')
      }
    }finally{if(scope===epoch.current){busyRef.current=false;setBusy(false)}}
  }
  const mutation=(fn,options={})=>{if(propertyDirty&&!options.allowDirty){setTransition({label:'执行下一项修改',run:()=>act(fn,{...options,mutation:true}),saveAllowed:false});return Promise.resolve()}if(canWrite!==true){setError('当前账号只有读取权限');return Promise.resolve()}if(recoveryRef.current){setError('请先重试未确认的保存，或重新读取云端图纸');return Promise.resolve()}return act(fn,{...options,mutation:true})}
  const operate=(ops,options={})=>{if(!doc||!ops.length)return Promise.resolve();const requestId=createClientId(),identity=doc.id,revision=docRef.current.revision;return mutation(()=>accountRequest(`/${identity}/operations`,{token,method:'POST',body:{requestId,expectedRevision:revision,operations:ops}}),options)}
  const add=(entities,options)=>operate(entities.map(entity=>({op:'add',entity:{layer,...entity}})),options)
  const metadata=changes=>({op:'metadata',customProperties:{...properties,...changes}})
  const fit=d=>{const drawing=d||doc;setView(drawingBounds([...(drawing?.entities||[]),...(drawing?.renderEntities||[])]))}
  const resetDraft=()=>{setPoints([]);setCursor(null);const saved=selected.length===1?structuredClone(selected[0]):null;setProperty(saved);setPointText(saved?.type==='LWPOLYLINE'?nativePolylineText(saved.points):'');setLibraryReset(value=>value+1);setLibraryDirty(false)}
  const guard=(label,run,{drawing=true,library=false}={})=>{
    if(busyRef.current)return
    if(recoveryRef.current){setError('请先处理未确认的保存，避免覆盖草稿');return}
    if(propertyDirty||(drawing&&points.length)||(library&&libraryDirty))setTransition({label,run,saveAllowed:['切换选择','切换面板','打开图纸','新建图纸','导入图纸','导出图纸','预览图纸'].includes(label)})
    else { try { return run() } catch (error) { setError(error.message || '操作未完成，请重试') } }
  }
  const selectEntities=ids=>guard('切换选择',()=>setSelection(ids),{drawing:false})
  const open=id=>guard('打开图纸',()=>act(()=>accountRequest(`/${id}`,{token}),{onSuccess:fit}),{library:true})
  const reload=()=>{if(!doc)return;const run=()=>{recoveryRef.current=null;setRecovery(null);act(()=>accountRequest(`/${doc.id}`,{token}),{onSuccess:fit})};if(propertyDirty||points.length||libraryDirty||recovery)setTransition({label:'重新读取云端图纸',run});else run()}
  const create=()=>guard('新建图纸',()=>{const requestId=createClientId(),newName=name.trim()||'新图纸';return mutation(()=>accountRequest('',{token,method:'POST',body:{name:newName,requestId}}),{onSuccess:fit})},{library:true})
  const importFile=file=>{if(!file)return;if(!/\.(dwg|dxf)$/i.test(file.name)||!file.size||file.size>20*1024*1024){setError('请选择非空的 DWG / DXF，大小不超过 20 MiB');return}return guard('导入图纸',()=>{const form=new FormData();form.append('file',file);form.append('requestId',createClientId());return mutation(()=>accountRequest('/import',{token,method:'POST',body:form}),{onSuccess:fit})},{library:true})}
  const remove=()=>selection.length&&guard('删除所选图元',()=>operate([{op:'delete',ids:selection},metadata({constraints:constraints.filter(c=>!c.entities.some(id=>selection.includes(id)))})]).then(result=>{if(result)setSelection([])}))
  const undo=redo=>doc&&guard(redo?'重做':'撤销',()=>{const requestId=createClientId(),identity=doc.id,revision=docRef.current.revision;return mutation(()=>accountRequest(`/${identity}/${redo?'redo':'undo'}`,{token,method:'POST',body:{requestId,expectedRevision:revision}}))},{library:true})
  const chooseTool=t=>{if(busyRef.current||recoveryRef.current)return;return guard('切换工具',()=>{setTool(t);setToolGroup(Object.keys(TOOL_GROUPS).find(key=>TOOL_GROUPS[key].tools.includes(t))||'select');setPoints([]);setCursor(null)},{drawing:false})}
  const chooseToolGroup=group=>guard('切换工具组',()=>{setToolGroup(group);setTool(TOOL_GROUPS[group].tools.includes(tool)?tool:TOOL_GROUPS[group].initial);setPoints([]);setCursor(null);setTab(group==='transform'?'变换':'属性')},{library:tab==='图库'})
  const saveProperties=()=>{
    try{
      const candidate=property?.type==='LWPOLYLINE'?{...property,points:parseNativePolylineText(pointText)}:property
      if(!candidate)return Promise.resolve()
      const replacements=doc.entities.map(e=>e.id===candidate.id?candidate:e),solved=constraints.length?solveConstraints(replacements,constraints,parameters):replacements
      const ops=solved.filter((e,i)=>JSON.stringify(e)!==JSON.stringify(doc.entities[i])).map(e=>({op:'update',id:e.id,changes:e.type==='DIMENSION'?{text:e.text,toleranceUpper:e.toleranceUpper,toleranceLower:e.toleranceLower,toleranceEnabled:e.toleranceEnabled,style:e.style,layer:e.layer,color:e.color,linetype:e.linetype,lineweight:e.lineweight}:entityChanges(e)}))
      return ops.length?operate(ops,{allowDirty:true}):Promise.resolve(doc)
    }catch(e){setError(e.message);return Promise.resolve()}
  }
  const world=e=>{const point=svg.current.createSVGPoint();point.x=e.clientX;point.y=e.clientY;const matrix=svg.current.getScreenCTM(),p=point.matrixTransform(matrix.inverse());return snapPoint([p.x,-p.y],visibleNativeEntities(doc),8/matrix.a,grid)}
  const finishPolyline=()=>{if(points.length>=2)return add([{type:'LWPOLYLINE',points:points.map(p=>[...p,0]),closed:false}])}
  const draw=p=>{
    if(!doc||busyRef.current||recoveryRef.current||!active)return
    if(tool!=='select'&&canWrite!==true)return
    if(tool==='select'){selectEntities([]);return}
    if(tool==='pan')return
    if(propertyDirty){setError('请先保存属性或放弃属性修改，再开始绘制');return}
    if(tool==='text'){add([{type:'TEXT',position:[...p,0],text,height:textHeight,rotation:0}]);return}
    if(tool==='bubble'){operate([{op:'bubble',position:p,radius:textHeight,number:bubbleNumber}],{onSuccess:()=>setBubbleNumber(n=>n+1)});return}
    const next=[...points,p];setPoints(next)
    if(tool==='polyline')return
    if(tool==='arc'&&next.length===3){const[c,a,b]=next;add([{type:'ARC',center:[...c,0],radius:Math.hypot(a[0]-c[0],a[1]-c[1]),startAngle:Math.atan2(a[1]-c[1],a[0]-c[0])*180/Math.PI,endAngle:Math.atan2(b[1]-c[1],b[0]-c[0])*180/Math.PI}],{onFailure:()=>setPoints(points)});return}
    if(tool==='dimension'){
      if(next.length===3){try{operate([nativeDimensionDraft({kind:dimensionKind,layer,points:next,entities:visibleNativeEntities(doc),preferredIds:selection})],{onFailure:()=>setPoints(points)})}catch(e){setError(e.message);setPoints(points)}}return
    }
    if(next.length===2){const[a,b]=next;if(tool==='line')add([{type:'LINE',start:[...a,0],end:[...b,0]}],{onFailure:()=>setPoints(points)});if(tool==='circle')add([{type:'CIRCLE',center:[...a,0],radius:Math.hypot(b[0]-a[0],b[1]-a[1])}],{onFailure:()=>setPoints(points)});if(tool==='rectangle')add([{type:'LWPOLYLINE',points:[[a[0],a[1],0],[b[0],a[1],0],[b[0],b[1],0],[a[0],b[1],0]],closed:true}],{onFailure:()=>setPoints(points)})}
  }
  const transform=(kind)=>{
    if(!selection.length)return
    if(['move','rotate','scale','trim'].includes(kind)&&constraints.some(c=>c.entities.some(id=>selection.includes(id)))){setError('所选图元受约束控制，请在约束面板修改尺寸或解除相关约束后变换');return}
    if(kind==='offset')return operate([{op:'offset',ids:selection,distance:fields.offset}])
    if(kind==='trim')return operate(selection.map(id=>({op:'trim',id,start:fields.trimStart,end:fields.trimEnd})))
    const values={translation:kind==='move'||kind==='copy'?[fields.dx,fields.dy]:[0,0],rotation:kind==='rotate'?fields.angle:0,scale:kind==='scale'?fields.scale:1,origin:[0,0],mirror:kind==='mirror'?'y':undefined}
    if(['array','polar'].includes(kind)){if(!Number.isInteger(fields.count)||fields.count<2||fields.count>100){setError('阵列数量须为 2–100');return}const added=[];for(let i=1;i<fields.count;i++)for(const e of selected){if(e.type==='DIMENSION'||!e.editable){setError('阵列请选择基础可编辑图元');return}added.push(transformEntity(e,kind==='array'?{translation:[fields.dx*i,fields.dy*i]}:{rotation:360/fields.count*i}))}return add(added)}
    if(kind==='copy'||kind==='mirror'){if(selected.some(e=>e.type==='DIMENSION'||!e.editable)){setError('复制/镜像请选择基础可编辑图元');return}return add(selected.map(e=>transformEntity(e,values)))}
    return operate([{op:'transform',ids:selection,...values}])
  }
  const solve=(nextConstraints,nextParameters=parameters)=>{
    try{const updated=solveConstraints(doc.entities,nextConstraints,nextParameters),ops=updated.filter((e,i)=>JSON.stringify(e)!==JSON.stringify(doc.entities[i])).map(e=>({op:'update',id:e.id,changes:entityChanges(e)}));operate([...ops,metadata({constraints:nextConstraints,parameters:nextParameters})])}catch(e){setError(e.message)}
  }
  const addConstraint=()=>{if(!selected.length){setError('请先选择约束图元');return}const required=['parallel','perpendicular','coincident','equal','concentric','distance'].includes(constraintType)?2:1;if(selected.length!==required){setError(`该约束需要 ${required} 个图元`);return}solve([...constraints,{id:createClientId(),type:constraintType,entities:selection,value:constraintValue,ends:[endA,endB],...(constraintType==='fixed'?{geometry:structuredClone(selected[0])}:{})}])}
  const runCommand=()=>{if(!active||busyRef.current||recoveryRef.current)return;const coordinate=command.trim().match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))$/);if(coordinate){const p=coordinate.slice(1).map(Number);if(p.some(n=>!Number.isFinite(n)||Math.abs(n)>1e7)){setError('坐标超出范围');return}draw(p);setCommand('');return}const aliases={L:'line',LINE:'line',REC:'rectangle',C:'circle',A:'arc',PL:'polyline',T:'text',D:'dimension',B:'bubble'};const c=command.trim().toUpperCase();if(aliases[c])chooseTool(aliases[c]);else if(c==='E'||c==='ERASE')remove();else if(c==='U')undo(false);else if(c==='REDO')undo(true);else if(c==='Z'||c==='FIT')fit();else if(c==='M')transform('move');else if(c==='CO')transform('copy');else if(c==='RO')transform('rotate');else if(c==='O')transform('offset');else setError('命令：L / REC / C / A / PL / T / D / B / E / U / REDO / FIT / M / CO / RO / O');setCommand('')}
  const exportQuery=format=>new URLSearchParams({format,paper,scale:printScale,color:printColor,layout:printLayout,landscape:String(landscape)})
  const exportDoc=format=>guard('导出图纸',()=>act(()=>accountRequest(`/${doc.id}/export?${exportQuery(format)}`,{token,blob:true}),{onSuccess:blob=>saveNativeBlob(blob,format==='original'?doc.source.filename:`${doc.name}.${format}`)}),{library:true})
  const fullPreview=()=>guard('预览图纸',()=>act(()=>accountRequest(`/${doc.id}/export?${exportQuery('svg')}`,{token,blob:true}),{onSuccess:blob=>{clearPreview();previewRef.current=URL.createObjectURL(blob);setPreview(previewRef.current)}}))
  const autoBubbles=()=>{if(!doc.dimensions.length){setError('没有原生尺寸，请先使用标注工具');return}const used=new Set((doc.bubbles||[]).map(b=>b.number));let next=1;const ops=doc.dimensions.filter(d=>!d.bubbleNumber).map((d,i)=>{while(used.has(next))next++;const number=next++;used.add(number);return {op:'bubble',dimensionId:d.id,position:(d.points?.defpoint||[i*15,0]).slice(0,2).map((n,j)=>n+(j?textHeight*3:0)),radius:textHeight,number}});if(!ops.length){showToast('全部尺寸已有气泡编号');return}operate(ops)}
  const onKey=e=>{if(!active)return;if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)||e.nativeEvent.isComposing)return;if(e.key==='Escape'){chooseTool('select');selectEntities([])}if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();remove()}if(e.key==='Enter'&&tool==='polyline')finishPolyline();if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='z'){e.preventDefault();e.stopPropagation();undo(e.shiftKey)}if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='a'){e.preventDefault();selectEntities(visibleNativeEntities(doc).filter(e=>e.editable&&!doc.layers.find(l=>l.name===e.layer)?.locked).map(e=>e.id))}}
  useEffect(()=>{
    if(!active)return
    const save=e=>{if(!(e.metaKey||e.ctrlKey)||e.key.toLowerCase()!=='s')return;e.preventDefault();e.stopPropagation();if(busyRef.current||recoveryRef.current){showToast('保存仍在处理或等待确认，请查看本页提示');return}if(propertyDirty){if(canWrite!==true){showToast('当前账号只有读取权限','error');return}saveProperties().then(result=>{if(result)showToast('属性已保存')})}else showToast(points.length?'请完成当前绘制后保存':libraryDirty?'请在参数图库中保存参数或重建实例':doc?'当前图纸修订已保存':'请先新建或打开图纸')}
    window.addEventListener('keydown',save)
    return()=>window.removeEventListener('keydown',save)
  },[active,property,pointText,doc,points.length,libraryDirty,canWrite])
  if(!token)return <section className="native-workspace"><h1>原生二维 CAD</h1><p>登录后可创建或导入 DWG / DXF，图纸和历史保存在你的账号中。</p></section>
  return <section className="native-workspace" ref={root} tabIndex={0} onKeyDown={onKey} aria-label="原生二维 CAD 工作台">
    <header className="native-heading"><div><h1>原生二维 CAD</h1><p>{doc?`${doc.name} · 修订 ${doc.revision} · ${doc.entities.length} 个实体 · ${busy?'请求处理中':recovery?'保存结果待确认':propertyDirty?'属性未保存':points.length?'绘制尚未完成':'当前修订已保存'}`:'从空白图纸开始，或打开已有 DWG / DXF'}</p></div><div className="native-actions"><input aria-label="图纸名称" value={name} onChange={e=>setName(e.target.value)}/><button disabled={writeBlocked} onClick={create}>新建</button>{doc&&<button disabled={writeBlocked} onClick={()=>guard('重命名',()=>operate([{op:'metadata',name}]))}>重命名</button>}<label className="native-file-button">导入 DWG / DXF<input type="file" accept=".dwg,.dxf" disabled={writeBlocked} onChange={e=>{importFile(e.target.files[0]);e.target.value=''}}/></label><select aria-label="打开云图纸" value={doc?.id||''} disabled={busy} onChange={e=>open(e.target.value)}><option value="" disabled>打开云图纸</option>{documents.map(d=><option key={d.id} value={d.id}>{d.name}</option>)}</select></div></header>
    {canWrite===false&&<p className="native-note">当前账号为只读，可查看图纸并导出。</p>}
    {error&&<div className="native-error" role="alert"><span>{error}{recovery&&' 草稿已保留。重试同一请求不会重复添加图元或图纸。'}</span><div>{recovery&&<button disabled={busy} onClick={()=>act(recovery.fn,recovery.options)}>重试未确认请求</button>}{doc?<button disabled={busy} onClick={reload}>重新读取</button>:!recovery&&<button disabled={busy} onClick={()=>setConnectionRetry(value=>value+1)}>重新连接</button>}<button disabled={busy} onClick={()=>setError('')}>收起提示</button></div></div>}
    {recovery&&!error&&<p className="native-note"><button disabled={busy} onClick={()=>act(recovery.fn,recovery.options)}>重试未确认请求</button> 草稿仍保留，继续编辑前请确认保存结果。</p>}
    {transition&&<div className="native-draft-dialog" role="dialog" aria-modal="true" aria-label="未保存修改" onKeyDown={event=>nativeDialogKey(event,()=>setTransition(null))}><div><h2>有尚未保存的修改</h2><p>继续{transition.label}会放弃当前本地输入。云端已保存的图纸不受影响。</p><button autoFocus onClick={()=>setTransition(null)}>继续编辑</button>{transition.saveAllowed&&propertyDirty&&!points.length&&!libraryDirty&&!recovery&&<button disabled={busy} onClick={async()=>{const result=await saveProperties();if(result){const next=transition.run;setTransition(null);next()}}}>保存属性后继续</button>}<button disabled={busy} onClick={()=>{const next=transition.run;resetDraft();setTransition(null);next()}}>放弃本地输入并继续</button></div></div>}
    {doc&&<><div className="native-toolbar">
      <div className="native-tool-ribbon"><nav className="native-tool-groups" aria-label="二维工具组">{Object.entries(TOOL_GROUPS).map(([id,group])=><button key={id} aria-label={`${group.label}工具组`} aria-pressed={toolGroup===id} className={toolGroup===id?'active':''} disabled={busy||!!recovery} onClick={()=>chooseToolGroup(id)}>{group.label}</button>)}</nav><div className="native-history-actions" aria-label="历史与视图"><button disabled={writeBlocked||!doc.canUndo} onClick={()=>undo(false)}>撤销</button><button disabled={writeBlocked||!doc.canRedo} onClick={()=>undo(true)}>重做</button><button onClick={()=>fit()}>适合窗口</button></div></div>
      <div className="native-tool-row">
        {Object.entries(TOOL_GROUPS).map(([group,definition])=><div key={group} className="native-tool-options" aria-label={`${definition.label}工具`} hidden={toolGroup!==group}>{definition.tools.map(id=><button key={id} className={tool===id?'active':''} aria-pressed={tool===id} disabled={busy||!!recovery||(id!=='select'&&canWrite!==true)} onClick={()=>chooseTool(id)}>{TOOLS[id]}</button>)}{group==='select'&&<><button disabled={busy||!!recovery} className={tool==='pan'?'active':''} aria-pressed={tool==='pan'} onClick={()=>chooseTool(tool==='pan'?'select':'pan')}>平移</button><button disabled={!selection.length||writeBlocked} onClick={remove}>删除</button></>}{group==='transform'&&<><span className="native-tool-tip">先在画布选择图元</span><button disabled={!selection.length||writeBlocked} onClick={remove}>删除所选</button></>}</div>)}
        <details className="native-drawing-defaults"><summary>绘图设置</summary><div><label>捕捉网格<input aria-label={`捕捉网格 ${drawingUnit}`} type="number" min="0" value={grid} onChange={e=>setGrid(Math.max(0,Number(e.target.value)))}/></label><label>当前图层<select aria-label="绘图图层" value={layer} disabled={writeBlocked} onChange={e=>setLayer(e.target.value)}>{doc.layers.map(l=><option key={l.name}>{l.name}</option>)}</select></label></div></details>
      </div>
      <fieldset className="native-tool-settings" disabled={writeBlocked} hidden={!['text','bubble','dimension'].includes(tool)} aria-label="当前工具设置">
        <div hidden={tool!=='text'}><label>文字内容<input value={text} onChange={e=>setText(e.target.value)}/></label><Numeric label="文字高度" value={textHeight} min={.1} onChange={setTextHeight}/></div>
        <div hidden={tool!=='bubble'}><Numeric label="下一气泡编号" value={bubbleNumber} min={1} step="1" onChange={setBubbleNumber}/><Numeric label="气泡半径" value={textHeight} min={.1} onChange={setTextHeight}/></div>
        <div hidden={tool!=='dimension'}><label>标注类型<select value={dimensionKind} onChange={e=>setDimensionKind(e.target.value)}>{Object.entries({linear:'水平尺寸',aligned:'对齐尺寸',radius:'半径',diameter:'直径',angular:'角度'}).map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label><span className="native-tool-tip">点选两个定义点，再放置尺寸</span></div>
      </fieldset>
    </div>
    <div className="native-body"><div className="native-canvas-column"><div className="native-canvas" onContextMenu={e=>{e.preventDefault();if(tool==='polyline')finishPolyline();else chooseTool('select')}}>
      <svg ref={svg} viewBox={`${view.x} ${-view.y-view.height} ${view.width} ${view.height}`} preserveAspectRatio="xMidYMid meet" aria-label="二维绘图画布" onWheel={e=>{const factor=e.deltaY>0?1.12:.89;setView(v=>({x:v.x+v.width*(1-factor)/2,y:v.y+v.height*(1-factor)/2,width:Math.min(1e7,Math.max(.1,v.width*factor)),height:Math.min(1e7,Math.max(.1,v.height*factor))}))}} onPointerDown={e=>{root.current?.focus();if(e.button===1||e.altKey||tool==='pan'){pan.current={x:e.clientX,y:e.clientY,view};e.currentTarget.setPointerCapture(e.pointerId);return}if(e.button===0)draw(world(e))}} onPointerMove={e=>{if(pan.current){const m=svg.current.getScreenCTM(),p=pan.current;setView({...p.view,x:p.view.x-(e.clientX-p.x)/m.a,y:p.view.y+(e.clientY-p.y)/m.d});return}setCursor(world(e))}} onPointerUp={()=>{pan.current=null}} onPointerCancel={()=>{pan.current=null}} onLostPointerCapture={()=>{pan.current=null}} onDoubleClick={()=>tool==='polyline'&&finishPolyline()}>
        <defs><pattern id="native-grid" width={Math.max(grid,view.width/40)} height={Math.max(grid,view.width/40)} patternUnits="userSpaceOnUse"><path d={`M ${Math.max(grid,view.width/40)} 0 L 0 0 0 ${Math.max(grid,view.width/40)}`} fill="none" stroke="#26303d" strokeWidth={scale*.5}/></pattern></defs><rect x={view.x} y={-view.y-view.height} width={view.width} height={view.height} fill="url(#native-grid)"/>
        <g transform="scale(1,-1)">{nativeVisibleRenderEntities(doc).map(e=><Entity key={e.id} entity={displayEntity(e,doc.layers)} scale={scale} selected={selection.includes(e.parentId||e.id)} onSelect={event=>{if(tool!=='select')return;event.stopPropagation();root.current?.focus();const id=e.parentId||e.id;selectEntities(event.shiftKey?(selection.includes(id)?selection.filter(v=>v!==id):[...selection,id]):[id])}}/>)}{points.length>0&&cursor&&<path d={[...points,cursor].map((p,i)=>`${i?'L':'M'} ${p[0]} ${p[1]}`).join(' ')} stroke="#ffce68" fill="none" strokeDasharray={`${scale*4} ${scale*3}`} strokeWidth={scale}/>} {cursor&&tool!=='select'&&<circle cx={cursor[0]} cy={cursor[1]} r={scale*3} fill="#63d5ff"/>}</g>
      </svg><div className="native-canvas-hint">{busy?'请求处理中…':`${tool==='pan'?'平移':TOOLS[tool]} · ${selection.length} 项已选 · ${cursor?`${cursor.map(fmt).join(', ')} ${drawingUnit}`:drawingUnit}`}<br/>滚轮缩放 · 平移按钮 / Alt 拖动 · Shift 多选 · Esc 取消</div></div>
      <div className="native-command"><input aria-label="CAD 命令" placeholder="输入命令或坐标：L → 0,0 → 100,0；REC、C、D、M…" value={command} onChange={e=>setCommand(e.target.value)} onKeyDown={e=>{if(e.key==='Enter')runCommand()}}/><button disabled={busy||!!recovery} onClick={runCommand}>执行</button>{tool==='polyline'&&<button disabled={writeBlocked||points.length<2} onClick={finishPolyline}>完成多段线</button>}{points.length>0&&<button disabled={busy||!!recovery} onClick={()=>{setPoints([]);setCursor(null)}}>取消绘制</button>}</div>
      <p className="native-instruction">{tool==='arc'?'依次点选圆心、起点、终点':tool==='dimension'?'依次点选两个定义点及标注位置；半径/直径先点圆心，角度先点顶点':tool==='polyline'?'连续点击顶点，Enter 或右键结束':tool==='circle'?'点选圆心和圆周上的点':tool==='select'?'点击图元后修改属性；全部原始内容可在整图预览中核对':'点击画布绘制，尺寸单位沿用当前图纸'}</p>
      {!!doc.warnings.length&&<p className="native-note">{doc.warnings.join(' ')} {doc.blocks?.length?`${doc.blocks.length} 个块定义保留。`:''}</p>}
      {preview&&<div className="native-preview"><button onClick={clearPreview}>关闭整图预览</button><img alt="原生图纸全实体渲染" src={preview}/></div>}
    </div><aside className="native-inspector"><nav>{['属性','变换','约束','图库','图层','出图'].map(t=><button className={tab===t?'active':''} key={t} onClick={()=>guard('切换面板',()=>setTab(t),{drawing:false,library:tab==='图库'})}>{t}</button>)}</nav><fieldset className="native-editor-fields" disabled={tab==='出图'?busy:writeBlocked}>
    {tab==='属性'&&<div className="native-form"><h2>实体属性</h2>
      <label>选择实体<select aria-label="选择图纸实体" value={selection.length===1?selection[0]:''} onChange={e=>selectEntities(e.target.value?[e.target.value]:[])}><option value="">从画布或列表选择</option>{doc.entities.map(e=><option key={e.id} value={e.id}>{e.type} · {e.id} · {e.layer}</option>)}</select></label>
      {property?<><h3>{property.type} · {property.id}</h3>{!property.editable?<p>此实体保留原定义。可在整图预览核对，并导出 DXF。</p>:<>
      {['start','end','center','position'].filter(k=>property[k]).map(k=><div key={k} className="native-pair"><Numeric label={`${{start:'起点',end:'终点',center:'圆心',position:'位置'}[k]} X`} value={property[k][0]} onChange={n=>setProperty(p=>({...p,[k]:[n,p[k][1],p[k][2]||0]}))}/><Numeric label="Y" value={property[k][1]} onChange={n=>setProperty(p=>({...p,[k]:[p[k][0],n,p[k][2]||0]}))}/></div>)}
      {['radius','height','rotation','startAngle','endAngle'].filter(k=>property[k]!==undefined).map(k=><Numeric key={k} label={{radius:'半径',height:'字高',rotation:'转角',startAngle:'起始角',endAngle:'结束角'}[k]} value={property[k]} onChange={n=>setProperty(p=>({...p,[k]:n}))}/>)}
      {property.points&&Array.isArray(property.points)&&<label>轮廓顶点（每行 X,Y,凸度）<textarea rows="6" value={pointText} onChange={e=>setPointText(e.target.value)}/></label>}
      {property.text!==undefined&&<label>文字 / 尺寸覆写<input value={property.text} onChange={e=>setProperty(p=>({...p,text:e.target.value}))}/></label>}
      {property.bubble&&<><Numeric label="检验气泡编号" value={property.bubble.number} min={1} step="1" onChange={n=>setProperty(p=>({...p,bubble:{...p.bubble,number:n}}))}/><button disabled={busy} onClick={()=>{const circle=property.id===property.bubble.circleId?property:doc.entities.find(e=>e.id===property.bubble.circleId);operate([{op:'bubble',dimensionId:property.bubble.dimensionId,number:property.bubble.number,position:circle.center,radius:circle.radius}],{allowDirty:true})}}>更新气泡编号与检验表</button><p>此气泡关联尺寸 {property.bubble.dimensionId}。选择气泡圆可移动整组，选择任一部件删除可移除整组；编号会同步到 Excel。</p></>}{property.type==='DIMENSION'&&<><Numeric label="上偏差" value={property.toleranceUpper} onChange={n=>setProperty(p=>({...p,toleranceUpper:n}))}/><Numeric label="下偏差幅值" value={property.toleranceLower} min={0} onChange={n=>setProperty(p=>({...p,toleranceLower:n}))}/>{['dimtxt','dimasz','dimdec','dimtdec'].map(k=><Numeric key={k} label={{dimtxt:'尺寸字高',dimasz:'箭头大小',dimdec:'小数位',dimtdec:'公差小数位'}[k]} value={property.style?.[k]} onChange={n=>setProperty(p=>({...p,style:{...p.style,[k]:n}}))}/>)}</>}
      <details className="native-settings"><summary>图层与线条样式</summary><div className="native-settings-fields"><label>实体图层<select value={property.layer} onChange={e=>setProperty(p=>({...p,layer:e.target.value}))}>{doc.layers.map(l=><option key={l.name}>{l.name}</option>)}</select></label><label>颜色<select value={property.color} onChange={e=>setProperty(p=>({...p,color:Number(e.target.value)}))}><option value="256">随层</option>{[1,2,3,4,5,6,7,8].map(n=><option key={n} value={n}>索引 {n}</option>)}</select></label>
      <label>线型<select value={(property.linetype||'BYLAYER').toUpperCase()} onChange={e=>setProperty(p=>({...p,linetype:e.target.value}))}>{(doc.linetypes||['BYLAYER','CONTINUOUS']).map(n=><option key={n} value={n.toUpperCase()}>{n}</option>)}</select></label><label>线宽 mm<select value={property.lineweight??-1} onChange={e=>setProperty(p=>({...p,lineweight:Number(e.target.value)}))}><option value={-1}>随层</option>{[0,13,18,25,35,50,70,100,140,200].map(n=><option key={n} value={n}>{n/100}</option>)}</select></label></div></details>
      {property.type==='DIMENSION'&&<label><input type="checkbox" checked={property.toleranceEnabled} onChange={e=>setProperty(p=>({...p,toleranceEnabled:e.target.checked}))}/>显示公差</label>}
      {propertyDirty&&<p role="status">属性已修改，点击保存后才写入云端。</p>}<button className="primary" disabled={writeBlocked||!propertyDirty} onClick={saveProperties}>保存属性并求解约束</button></>}</>:<p>点击图元查看属性，Shift 点击可多选。</p>}
    </div>}
    {tab==='变换'&&<div className="native-form"><h2>编辑与变换</h2><label>变换操作<select aria-label="变换操作" value={transformKind} onChange={e=>setTransformKind(e.target.value)}>{Object.entries(TRANSFORMS).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label><p>已选 {selection.length} 个图元。{['rotate','mirror','polar','scale'].includes(transformKind)&&'以坐标原点为中心。'}</p>
      {['move','copy','array'].includes(transformKind)&&<div className="native-pair"><Numeric label="位移 X" value={fields.dx} onChange={n=>setFields(f=>({...f,dx:n}))}/><Numeric label="位移 Y" value={fields.dy} onChange={n=>setFields(f=>({...f,dy:n}))}/></div>}
      {['rotate','polar'].includes(transformKind)&&<Numeric label={transformKind==='polar'?'相邻项夹角°':'角度°'} value={fields.angle} onChange={n=>setFields(f=>({...f,angle:n}))}/>}
      {transformKind==='scale'&&<Numeric label="比例" value={fields.scale} min={.001} onChange={n=>setFields(f=>({...f,scale:n}))}/>}
      {['array','polar'].includes(transformKind)&&<Numeric label="阵列总数" value={fields.count} min={2} step="1" onChange={n=>setFields(f=>({...f,count:n}))}/>}
      {transformKind==='offset'&&<Numeric label="偏移距离" value={fields.offset} onChange={n=>setFields(f=>({...f,offset:n}))}/>}
      {transformKind==='trim'&&<div className="native-pair"><Numeric label="裁剪起点 0–1" value={fields.trimStart} onChange={n=>setFields(f=>({...f,trimStart:n}))}/><Numeric label="裁剪终点 0–1" value={fields.trimEnd} onChange={n=>setFields(f=>({...f,trimEnd:n}))}/></div>}
      <button className="primary" disabled={!selection.length||writeBlocked} onClick={()=>{try{transform(transformKind)}catch(e){setError(e.message)}}}>{TRANSFORMS[transformKind]}</button>{!selection.length&&<p>切换「选择」工具，在画布点选图元；Shift 点击可多选。</p>}
    </div>}
    {tab==='约束'&&<div className="native-form"><h2>参数与公式</h2><div className="native-pair"><label>参数名<input value={parameterName} onChange={e=>setParameterName(e.target.value)}/></label><label>值 / 公式<input value={parameterValue} onChange={e=>setParameterValue(e.target.value)}/></label></div><button disabled={busy} onClick={()=>{try{if(!/^[A-Za-z_]\w{0,39}$/.test(parameterName))throw new Error('参数名须用字母/下划线开头');const next={...parameters,[parameterName]:parameterValue};Object.values(next).forEach(v=>evaluateFormula(v,next));solve(constraints,next)}catch(e){setError(e.message)}}}>保存参数并重算</button>
      {Object.entries(parameters).map(([k,v])=><div className="native-record" key={k}><button onClick={()=>{setParameterName(k);setParameterValue(v)}}>{k} = {v}</button><small>{(()=>{try{return fmt(evaluateFormula(v,parameters))}catch{return '公式无效'}})()}</small><button title={`删除参数 ${k}`} onClick={()=>{const next={...parameters};delete next[k];solve(constraints,next)}}>×</button></div>)}
      <h3>几何约束</h3><label>约束类型<select value={constraintType} onChange={e=>setConstraintType(e.target.value)}>{Object.entries(CONSTRAINT_TYPES).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label><label>尺寸 / 公式<input value={constraintValue} onChange={e=>setConstraintValue(e.target.value)} placeholder="width / 2"/></label><div className="native-pair"><label>首个图元端点<select value={endA} onChange={e=>setEndA(Number(e.target.value))}><option value={0}>起点/圆心</option><option value={1}>终点</option></select></label><label>第二图元端点<select value={endB} onChange={e=>setEndB(Number(e.target.value))}><option value={0}>起点/圆心</option><option value={1}>终点</option></select></label></div><button className="primary" disabled={busy||!selection.length} onClick={addConstraint}>添加到所选图元</button><p>长度、角度、半径支持参数公式；固定约束保留加入时的几何。冲突时不保存。</p>
      {constraints.map(c=><div className="native-record" key={c.id}><button onClick={()=>selectEntities(c.entities)}>{CONSTRAINT_TYPES[c.type]} · {c.entities.join(',')} {['length','radius','angle','distance'].includes(c.type)?`= ${c.value}`:''}</button><button title="删除约束" onClick={()=>solve(constraints.filter(x=>x.id!==c.id))}>×</button></div>)}
    </div>}
    {tab==='图库'&&<div className="native-form"><h2>专业绘图构件</h2><label>专业 / 构件<select value={symbol} onChange={e=>{const s=PROFESSIONAL_SYMBOLS.find(x=>x.id===e.target.value);setSymbol(s.id);setSymbolWidth(s.width);setSymbolHeight(s.height)}}>{['建筑','给排水','电气','暖通','结构'].map(d=><optgroup key={d} label={d}>{PROFESSIONAL_SYMBOLS.filter(s=>s.discipline===d).map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</optgroup>)}</select></label><div className="native-pair"><Numeric label={`宽度 ${drawingUnit}`} value={symbolWidth} min={1} onChange={setSymbolWidth}/><Numeric label={`高度 ${drawingUnit}`} value={symbolHeight} min={1} onChange={setSymbolHeight}/></div><button className="primary" disabled={busy} onClick={()=>{try{add(professionalSymbol(symbol,symbolWidth,symbolHeight,[fields.dx,fields.dy])).then(d=>d&&fit(d))}catch(e){setError(e.message)}}}>按尺寸插入构件</button><p>插入点沿用变换面板 X/Y；构件由可编辑线、圆、弧组成。符号可用于绘图，不附带专业负荷或规范计算。</p>
      <NativeParametricLibrary key={doc.id} document={doc} selected={selected} constraints={constraints} parameters={parameters} translation={[fields.dx,fields.dy]} busy={writeBlocked} onDirtyChange={setLibraryDirty} resetToken={libraryReset} operate={operate} fit={fit} onError={setError}/>

    </div>}
    {tab==='图层'&&<div className="native-form"><h2>图层管理</h2><label>当前 / 新图层名称<input value={layer} onChange={e=>setLayer(e.target.value)}/></label><button disabled={busy} onClick={()=>{if(doc.layers.some(item=>item.name===layer.trim())){setError('图层已存在，请使用下方的图层属性控制');return}operate([{op:'layer',name:layer.trim(),color:7,visible:true}])}}>创建图层</button>{doc.layers.map(l=><div className="native-layer" key={l.name}><label><input type="checkbox" checked={l.visible} disabled={busy} onChange={e=>operate([{op:'layer',name:l.name,visible:e.target.checked}])}/>{l.name}</label><select aria-label={`${l.name} 颜色`} value={l.color} disabled={busy} onChange={e=>operate([{op:'layer',name:l.name,color:Number(e.target.value)}])}>{[1,2,3,4,5,6,7,8,9].map(n=><option key={n}>{n}</option>)}</select><button disabled={busy} onClick={()=>operate([{op:'layer',name:l.name,locked:!l.locked}])}>{l.locked?'解锁':'锁定'}</button></div>)}</div>}
    {tab==='出图'&&<div className="native-form"><h2>打印与检验图</h2><details className="native-settings"><summary>打印设置 · {paper} · {landscape?'横向':'纵向'}</summary><div className="native-settings-fields"><label>打印布局<select value={printLayout} onChange={e=>setPrintLayout(e.target.value)}>{nativeLayouts(doc.layouts).map(l=><option key={l}>{l}</option>)}</select></label><label>纸张方向<select value={landscape?'landscape':'portrait'} onChange={e=>setLandscape(e.target.value==='landscape')}><option value="portrait">纵向</option><option value="landscape">横向</option></select></label><label>纸张<select value={paper} onChange={e=>setPaper(e.target.value)}>{['A4','A3','A2','A1','A0'].map(p=><option key={p}>{p}</option>)}</select></label><label>比例<select value={printScale} onChange={e=>setPrintScale(e.target.value)}>{['fit','1:1','1:2','1:5','1:10','1:20','1:50','1:100'].map(s=><option key={s} value={s}>{s==='fit'?'适合纸张':s}</option>)}</select></label><label>颜色<select value={printColor} onChange={e=>setPrintColor(e.target.value)}><option value="original">原始彩色</option><option value="monochrome">黑白</option></select></label></div></details><button disabled={busy} onClick={fullPreview}>整图预览（含块和原始标注）</button><button disabled={writeBlocked||!doc.dimensions.length} onClick={autoBubbles}>按全部尺寸自动编号气泡</button><p>{doc.dimensions.length} 个原生尺寸；炸开的线和文字不自动冒充尺寸。气泡与检验表编号关联，重复生成只补充未编号尺寸。</p><div className="native-button-grid">{['dxf','dwg','pdf','xlsx','json'].map(f=><button key={f} disabled={busy} onClick={()=>exportDoc(f)}>导出 {f.toUpperCase()}</button>)}</div>{doc.source?.filename&&<button disabled={busy} onClick={()=>exportDoc('original')}>下载上传原文件</button>}<p>纸张、方向、比例和布局用于 PDF / 预览；DXF / DWG 保留整份图纸，Excel 导出模型空间尺寸。</p>{doc.dimensions.map((d,i)=><button className="native-dimension" key={d.id} onClick={()=>{selectEntities([d.id]);setTab('属性')}}><b>{d.bubbleNumber||'待编号'}. {fmt(d.value)}</b><span>{d.kind} · {d.toleranceEnabled?`+${d.toleranceUpper}/−${d.toleranceLower}`:'无公差'}</span></button>)}</div>}
    </fieldset></aside></div></>}
    {!doc&&<div className="native-empty"><h2>直接编辑原生图元</h2><p>直线、圆弧、轮廓、文字、尺寸、公差、图层和历史版本。</p><button className="primary" disabled={writeBlocked} onClick={create}>创建第一张图纸</button></div>}
  </section>
}
