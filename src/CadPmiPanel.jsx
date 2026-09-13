import {useEffect,useState} from 'react'
import {ScalarField,VectorField} from './CadScalarFields.jsx'
import {editorEntityId,pmiLabel,pmiNames,pmiMeasurement} from './cadEditorEntities.js'

export default function CadPmiPanel({plan,initial,picks=[],onResetPicks,onApply,onClose,disabled,onDelete,onPreview}){
  const [note,setNote]=useState(initial||{id:editorEntityId('pmi',plan.annotations),kind:'distance',text:'',points:[],references:[],position:[0,0,0]})
  const [error,setError]=useState('')
  useEffect(()=>{onPreview?.(note)},[note])
  const needed={distance:2,angle:3,radius:3,diameter:3}[note.kind]||1
  useEffect(()=>{if(!picks.length)return;const points=picks.slice(-needed).map(p=>p.worldPoint);setNote(old=>({...old,anchors:undefined,points,references:picks.slice(-needed).filter(p=>p.selector).map(p=>p.selector),position:points.at(-1).map((v,i)=>v+(i===2?5:0))}))},[picks,needed])
  const patch=(key,value)=>{setNote(old=>({...old,...(key==='points'?{anchors:undefined,references:[]}:{}),[key]:value}));setError('')}
  const apply=async()=>{try{if(['distance','angle','radius','diameter'].includes(note.kind)&&note.points.length!==needed)throw new Error(`请在模型上选择 ${needed} 个标注位置。`);if(['note','datum','tolerance','surface_finish'].includes(note.kind)&&!(note.text.trim()||note.datum?.trim()||note.value!==undefined))throw new Error('请填写标注内容。');pmiMeasurement(note,plan.parameters);if(await onApply(note)!==false)onClose()}catch(e){setError(e.message)}}
  return <section aria-label="PMI 标注" className="ce-entity-panel"><header><b>{initial?'编辑标注':'PMI 标注'}</b><button onClick={onClose} disabled={disabled}>×</button></header><fieldset disabled={disabled}>
    <div className="ce-pmi-kinds" role="group" aria-label="标注类型">{Object.entries(pmiNames).map(([kind,label])=><button key={kind} aria-pressed={note.kind===kind} onClick={()=>{setNote({...note,kind,points:[],references:[],anchors:undefined});onResetPicks();setError('')}}>{label}</button>)}</div>
    <p className="ce-panel-note">在模型上点击标注位置，已选 {note.points.length} / {needed}。可在下方精确调整坐标。</p><button onClick={()=>{patch('points',[]);onResetPicks()}}>重新选择</button>{note.references?.length>0&&<button onClick={()=>{patch('points',note.points);onResetPicks()}}>解除几何关联</button>}{note.points.length>0&&!note.references?.length&&<p className="ce-panel-note">独立坐标标注；修改模型后请核对位置。</p>}
    {note.points.map((point,i)=><VectorField key={i} label={`标注点 ${i+1}`} value={point} onChange={v=>patch('points',note.points.map((p,j)=>i===j?v:p))}/>)}
    <VectorField label="文字位置" value={note.position} onChange={v=>patch('position',v)}/>
    <label className="df-field"><span>标注文字</span><input aria-label="PMI 标注文字" maxLength={2000} value={note.text} onChange={e=>patch('text',e.target.value)}/></label>
    {['datum','tolerance'].includes(note.kind)&&<label className="df-field"><span>基准</span><input aria-label="基准代号" value={note.datum||''} maxLength={100} onChange={e=>patch('datum',e.target.value)}/></label>}
    {note.kind==='tolerance'&&<label className="df-field"><span>形位符号</span><select aria-label="形位公差符号" value={note.symbol||'⌖'} onChange={e=>patch('symbol',e.target.value)}>{['⌖','⏥','○','⌭','∥','⟂','∠','⌒','⌓','↗','⌰'].map(s=><option key={s}>{s}</option>)}</select></label>}
    {['tolerance','surface_finish'].includes(note.kind)&&<ScalarField label={note.kind==='surface_finish'?'粗糙度 Ra (μm)':'公差值 (mm)'} value={note.value??''} onChange={v=>patch('value',v)}/>}
    {['distance','radius','diameter'].includes(note.kind)&&<details><summary>尺寸公差</summary><ScalarField label="上偏差" value={note.upper??0} onChange={v=>patch('upper',v)}/><ScalarField label="下偏差" value={note.lower??0} onChange={v=>patch('lower',v)}/></details>}
    <output className="ce-pmi-preview">{['distance','angle','radius','diameter'].includes(note.kind)&&note.points.length<needed?`还需选择 ${needed-note.points.length} 个标注位置`:pmiLabel(note,plan.parameters)}</output>{error&&<p className="df-error" role="alert">{error}</p>}
    <footer>{onDelete&&<button onClick={onDelete}>删除</button>}<button className="ce-done" onClick={apply}>完成标注</button><button onClick={onClose}>取消</button></footer>
  </fieldset></section>
}
