import {useState,useEffect} from 'react'
import {ScalarField,VectorField} from './CadScalarFields.jsx'
import {baseReferenceFrames,editorEntityId,referenceFrames} from './cadEditorEntities.js'

export default function CadReferencePanel({plan,initial,kind='plane',onApply,onClose,disabled,selectedFrame,picks=[],onPreview}){
  const id=initial?.id||editorEntityId(kind,plan.references),label={plane:'平面',axis:'轴',point:'点',helix:'螺旋线'}[kind]
  const [value,setValue]=useState(initial||{id,kind,mode:kind==='plane'?'offset':kind==='axis'?'two_points':'coordinates',label:`${label} ${id.replace(/^[a-z]+/,'')}`,...(kind==='helix'?{radius:10,pitch:5,turns:3,lefthand:false,frame:baseReferenceFrames.XY}:kind==='plane'?{parent:'XY',offset:0,angle:0,axis:'x'}:kind==='axis'?{start:[0,0,0],end:[0,0,10]}:{point:[0,0,0]})})
  const [error,setError]=useState('')
  useEffect(()=>{onPreview?.(value)},[value])
  useEffect(()=>{if(!picks.length)return;setValue(old=>{const points=picks.map(p=>p.worldPoint);if(kind==='point')return {...old,point:points.at(-1)};if(kind==='axis')return {...old,start:points.at(-2)||points.at(-1),end:points.at(-1)};if(kind==='plane'&&old.mode==='three_points')return {...old,points:[...old.points,...points].slice(-3)};return old})},[picks])
  const patch=(key,next)=>{setValue(old=>({...old,[key]:next}));setError('')}
  const mode=next=>{const base={id:value.id,label:value.label,kind};setValue({...base,mode:next,...(next==='frame'?{frame:selectedFrame||baseReferenceFrames.XY}:next==='three_points'?{points:[[0,0,0],[10,0,0],[0,10,0]]}:{parent:'XY',offset:0,angle:0,axis:'x'})})}
  const apply=async()=>{try{referenceFrames({...plan,references:(plan.references||[]).some(r=>r.id===id)?plan.references.map(r=>r.id===id?value:r):[...(plan.references||[]),value]});if(await onApply(value)!==false)onClose()}catch(e){setError(e.message)}}
  return <section aria-label={`构造${label}`} className="ce-entity-panel"><header><b>{initial?'编辑':'新建'}{label}</b><button onClick={onClose} disabled={disabled}>×</button></header><fieldset disabled={disabled}>
    <label className="df-field"><span>名称</span><input aria-label="参考几何名称" value={value.label} maxLength={200} onChange={e=>patch('label',e.target.value)}/></label>
    {kind==='plane'&&<><label className="df-field"><span>构造方法</span><select aria-label="平面构造方法" value={value.mode} onChange={e=>mode(e.target.value)}><option value="offset">偏移平面</option><option value="angle">成角度平面</option><option value="three_points">通过三点</option><option value="frame">原点和方向</option></select></label>
    {['offset','angle'].includes(value.mode)&&<><label className="df-field"><span>参考平面</span><select aria-label="参考平面" value={value.parent} onChange={e=>patch('parent',e.target.value)}>{[...Object.keys(baseReferenceFrames).map(id=>({id,label:id})),...(plan.references||[]).filter(r=>r.kind==='plane'&&r.id!==id)].map(r=><option key={r.id} value={r.id}>{r.label||r.id}</option>)}</select></label><ScalarField label="平面偏移 (mm)" value={value.offset} onChange={v=>patch('offset',v)}/>{value.mode==='angle'&&<><ScalarField label="平面角度 (°)" value={value.angle} onChange={v=>patch('angle',v)}/><label className="df-field"><span>旋转方向</span><select value={value.axis} onChange={e=>patch('axis',e.target.value)}><option value="x">平面 X 轴</option><option value="y">平面 Y 轴</option></select></label></>}</>}
    {value.mode==='three_points'&&value.points.map((point,i)=><VectorField key={i} label={`构造点 ${i+1}`} value={point} onChange={v=>patch('points',value.points.map((p,j)=>i===j?v:p))}/>)}
    {value.mode==='frame'&&Object.entries({origin:'平面原点',xDir:'平面 X 方向',normal:'平面法线'}).map(([key,text])=><VectorField key={key} label={text} value={value.frame[key]} onChange={v=>patch('frame',{...value.frame,[key]:v})}/>)}</>}
    {kind==='axis'&&<><VectorField label="轴起点" value={value.start} onChange={v=>patch('start',v)}/><VectorField label="轴终点" value={value.end} onChange={v=>patch('end',v)}/></>}
    {kind==='helix'&&<><ScalarField label="螺旋半径 (mm)" value={value.radius} onChange={v=>patch('radius',v)}/><ScalarField label="螺距 (mm)" value={value.pitch} onChange={v=>patch('pitch',v)}/><ScalarField label="圈数" value={value.turns} onChange={v=>patch('turns',v)}/><label className="df-check"><input type="checkbox" checked={value.lefthand} onChange={e=>patch('lefthand',e.target.checked)}/>左旋</label>{Object.entries({origin:'螺旋原点',xDir:'起始半径方向',normal:'螺旋轴方向'}).map(([key,text])=><VectorField key={key} label={text} value={value.frame[key]} onChange={v=>patch('frame',{...value.frame,[key]:v})}/>)}</>}
    {['axis','point'].includes(kind)&&<p className="ce-panel-note">可在模型上点击选点，或输入精确坐标。</p>}{kind==='point'&&<VectorField label="参考点" value={value.point} onChange={v=>patch('point',v)}/>}
    {error&&<p role="alert" className="df-error">{error}</p>}
    <footer><button className="ce-done" onClick={apply}>完成</button><button onClick={onClose}>取消</button></footer>
  </fieldset></section>
}
