import {useEffect,useRef,useState} from 'react'
import {screenToSketch} from './featureSketchModel.js'
import {evaluateSketchExpression} from './featureSketchExpressions.js'
import {appendSweepPathSegment,detachSweepPath,moveSweepPathPoint,projectSweepPoint,resolveSweepPoint,sampleSweepSegment,sweepPathPoints,sweepPointFromPlane,sweepProjectionAxes} from './cadSweepPathModel.js'
import './cad-profile-tools.css'

export default function CadSweepPathEditor({path,onChange,parameters={},disabled=false}){
  const [plane,setPlane]=useState('XZ'),[offset,setOffset]=useState('0'),[tool,setTool]=useState('select'),[pending,setPending]=useState([]),[error,setError]=useState(''),[preview,setPreview]=useState(null),[view,setView]=useState([-20,-60,100,100])
  const svg=useRef(null),drag=useRef(null),history=useRef({past:[],present:structuredClone(path),future:[]}),[historyTick,setHistoryTick]=useState(0),shown=preview||path,axes=sweepProjectionAxes(plane)
  useEffect(()=>{if(JSON.stringify(path)!==JSON.stringify(history.current.present)){history.current={past:[...history.current.past,history.current.present].slice(-50),present:structuredClone(path),future:[]};setHistoryTick(v=>v+1)}},[path])
  useEffect(()=>{drag.current=null;setPreview(null);setPending([])},[path,plane,disabled])
  const numericPoint=p=>{try{return resolveSweepPoint(p,parameters)}catch{return null}}
  const points=sweepPathPoints(shown).map(p=>({...p,numeric:numericPoint(p.value)})).filter(p=>p.numeric)
  const curves=[],warnings=[];let previous=shown.start
  for(const [index,segment]of (shown.segments||[]).entries()){
    try{const samples=sampleSweepSegment(previous,segment,parameters).map(p=>projectSweepPoint(p,plane));curves.push({index,samples,d:samples.map((p,i)=>`${i?'L':'M'} ${p[0]} ${-p[1]}`).join(' ')})}catch(reason){warnings.push(reason.message)}
    previous=segment.to
  }
  const reset=()=>{setPending([]);setPreview(null);drag.current=null;setError('')}
  const selectTool=value=>{reset();setTool(value)}
  const publish=next=>{if(disabled)return;if(JSON.stringify(next)===JSON.stringify(path))return;const previous=history.current;history.current={past:[...previous.past,previous.present].slice(-50),present:structuredClone(next),future:[]};try{onChange(next);setHistoryTick(v=>v+1);setError('')}catch(reason){history.current=previous;throw reason}}
  const travel=redo=>{if(disabled)return;const old=history.current;if(redo?!old.future.length:!old.past.length)return;const next=redo?{past:[...old.past,old.present].slice(-50),present:old.future[0],future:old.future.slice(1)}:{past:old.past.slice(0,-1),present:old.past.at(-1),future:[old.present,...old.future]};reset();history.current=next;try{onChange(structuredClone(next.present));setHistoryTick(v=>v+1)}catch(reason){history.current=old;setError(reason.message)}}
  const run=fn=>{if(disabled)return;try{fn();setError('')}catch(reason){setError(reason.message||'无法编辑路径。')}}
  const toPlane=event=>screenToSketch(event.clientX,event.clientY,svg.current?.getBoundingClientRect(),view)
  const world=event=>{const p=toPlane(event);return p?sweepPointFromPlane(p,plane,evaluateSketchExpression(offset,parameters)):null}
  const currentEnd=()=>numericPoint(path.segments?.at(-1)?.to||path.start)
  const draw=event=>run(()=>{
    if(event.button!==undefined&&event.button!==0)return
    if(tool==='select')return
    const point=world(event);if(!point)return
    if(tool==='pan'){drag.current={pan:true,pointerId:event.pointerId,client:[event.clientX,event.clientY],view:[...view]};svg.current?.setPointerCapture?.(event.pointerId);return}
    if(!currentEnd()&&!pending.length){setPending([point]);return}
    const start=currentEnd()||pending[0],values=pending.length?pending:[start]
    if(tool==='arc'&&values.length<2){setPending([...values,point]);return}
    if(tool==='spline'){if(values.length>=64)throw new Error('样条最多64个插值点。');setPending([...values,point]);return}
    const source=currentEnd()?path:{...detachSweepPath(path),start,segments:[]}
    publish(appendSweepPathSegment(source,tool==='arc'?{type:'arc',through:values[1],to:point}:{type:'line',to:point},parameters));setPending([])
  })
  const finishSpline=()=>run(()=>{
    if(pending.length<3)throw new Error('请至少选择三个插值点。')
    const source=currentEnd()?path:{...detachSweepPath(path),start:pending[0],segments:[]}
    publish(appendSweepPathSegment(source,{type:'spline',through:pending.slice(1,-1),to:pending.at(-1)},parameters));setPending([]);setTool('select')
  })
  const startPoint=(event,point)=>{
    event.stopPropagation();if(disabled)return;if(tool!=='select'){draw(event);return}
    run(()=>{if(point.value.some(v=>typeof v!=='number'))throw new Error('表达式控制点请在坐标输入中明确修改。');if(point.key.endsWith(':center')||path.segments?.[Number(point.key.split(':')[0])]?.type==='ellipse')throw new Error('椭圆路径请编辑关联草图或下方椭圆参数；不能单独拖散曲线端点。');drag.current={key:point.key,original:point.numeric,path,pointerId:event.pointerId};svg.current?.setPointerCapture?.(event.pointerId)})
  }
  const move=event=>{
    const state=drag.current;if(disabled||!state||state.pointerId!==event.pointerId)return
    run(()=>{if(state.pan){const box=svg.current?.getBoundingClientRect(),scale=Math.min(box.width/state.view[2],box.height/state.view[3]);setView([state.view[0]-(event.clientX-state.client[0])/scale,state.view[1]-(event.clientY-state.client[1])/scale,...state.view.slice(2)]);return}const p=toPlane(event);if(!p)return;const target=[...state.original];target[axes[0]]=p[0];target[axes[1]]=p[1];const next=moveSweepPathPoint(state.path,state.key,target);state.next=next;setPreview(next)})
  }
  const finish=event=>{const state=drag.current;drag.current=null;setPreview(null);try{svg.current?.releasePointerCapture?.(event.pointerId)}catch{}if(!disabled&&state?.next&&state.path===path)run(()=>publish(state.next))}
  const fit=()=>{const p=[...curves.flatMap(c=>c.samples),...points.map(p=>projectSweepPoint(p.numeric,plane))];if(!p.length){setView([-20,-60,100,100]);return}const x=p.map(p=>p[0]),y=p.map(p=>p[1]),size=Math.max(Math.max(...x)-Math.min(...x),Math.max(...y)-Math.min(...y),10),margin=size*.2;setView([Math.min(...x)-margin,-Math.max(...y)-margin,Math.max(Math.max(...x)-Math.min(...x),size*.4)+margin*2,Math.max(Math.max(...y)-Math.min(...y),size*.4)+margin*2])}
  return <div className="cad-path-editor" onKeyDown={event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'&&!['INPUT','TEXTAREA','SELECT'].includes(event.target?.tagName)){event.preventDefault();event.stopPropagation();travel(Boolean(event.shiftKey))}if(event.key==='Escape'){event.stopPropagation();reset();setTool('select')}}}>
    <div className="cad-path-toolbar" role="group" aria-label="图形路径工具">
      <button type="button" disabled={disabled||!history.current.past.length} onClick={()=>travel(false)}>撤销路径</button><button type="button" disabled={disabled||!history.current.future.length} onClick={()=>travel(true)}>重做路径</button>
      {[['select','选择 / 拖点'],['line','路径直线'],['arc','路径圆弧'],['spline','路径样条'],['pan','平移路径画布']].map(([value,label])=><button key={value} type="button" disabled={disabled} aria-pressed={tool===value} onClick={()=>selectTool(value)}>{label}</button>)}
      {tool==='spline'&&<button type="button" disabled={disabled||pending.length<3} onClick={finishSpline}>完成路径样条</button>}
      <label>投影平面<select aria-label="路径绘图平面" value={plane} disabled={disabled} onChange={e=>setPlane(e.target.value)}>{['XZ','YZ','XY'].map(p=><option key={p}>{p}</option>)}</select></label>
      <label>{'XYZ'[axes[2]]} 坐标<input aria-label="路径绘图固定坐标" value={offset} disabled={disabled} onChange={e=>setOffset(e.target.value)}/></label>
      <button type="button" disabled={disabled||!path.segments?.length} onClick={()=>run(()=>{reset();publish({start:['','',''],segments:[]})})}>清空路径</button><button type="button" onClick={fit}>适合路径</button><button type="button" aria-label="放大路径画布" onClick={()=>setView(([x,y,w,h])=>[x+w*.1,y+h*.1,w*.8,h*.8])}>＋</button><button type="button" aria-label="缩小路径画布" onClick={()=>setView(([x,y,w,h])=>[x-w*.125,y-h*.125,w*1.25,h*1.25])}>−</button>
    </div>
    <svg ref={svg} className="cad-path-canvas" role="group" tabIndex={disabled?-1:0} aria-label="图形扫掠路径" viewBox={view.join(' ')} onPointerDown={draw} onPointerMove={move} onPointerUp={finish} onPointerCancel={reset} onLostPointerCapture={()=>{drag.current=null;setPreview(null)}}>
      <line className="cad-path-axis" x1={view[0]} x2={view[0]+view[2]} y1={0} y2={0}/><line className="cad-path-axis" x1={0} x2={0} y1={view[1]} y2={view[1]+view[3]}/>
      {curves.map(c=><path key={c.index} className="cad-path-curve" d={c.d}/>)}
      {points.map(p=>{const [u,v]=projectSweepPoint(p.numeric,plane);return <circle key={p.key} className="cad-path-point" cx={u} cy={-v} r={view[2]/100} role="button" aria-label={`路径控制点 ${p.key}`} onPointerDown={e=>startPoint(e,p)}><title>{p.numeric.join(', ')} mm</title></circle>})}
      {pending.map((p,i)=>{const [u,v]=projectSweepPoint(p,plane);return <circle key={i} className="cad-path-pending" cx={u} cy={-v} r={view[2]/110}/>})}
    </svg>
    <p className="ce-panel-note">当前为 {'XYZ'[axes[0]]} / {'XYZ'[axes[1]]} 投影；新增点的 {'XYZ'[axes[2]]} 坐标取上方输入。拖动保留每个原有点的第三坐标。路径不自动闭合。{tool==='arc'?'依次点击经过点与终点。':tool==='spline'?`已取 ${pending.length} 个插值点，点击完成路径样条提交。`:!currentEnd()?'先点击路径起点，再点击终点。':''}{path.sketchId?'直接修改路径将解除草图关联。':''}</p>
    {(error||warnings.length>0)&&<p role="alert" className="df-error">{error||[...new Set(warnings)].join(' ')}</p>}
  </div>
}
