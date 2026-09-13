import {referenceFrames} from './cadEditorEntities.js'
import {ScalarField,VectorField} from './CadScalarFields.jsx'
export {ScalarField,VectorField} from './CadScalarFields.jsx'
import {CadSweepFields,CadLoftFields,CadSurfaceGridFields} from './CadProfileTools.jsx'
import { useState } from 'react'
import { cloneFeatureValue, featureNames, featureScalar, fieldNames } from './directFeatureModel.js'
import { editableProfile } from './cadEditorTransactions.js'

const scalarText = value => typeof value === 'string' || typeof value === 'number' ? value : ''
const own = (value, key) => Object.hasOwn(value, key)
const plus = (value, amount) => typeof value === 'number' ? value + amount : `(${value}) + ${amount}`
function PathEditor({ feature, onChange }) {
  const path=Array.isArray(feature.path)?feature.path:[]
  return <section className="df-contour"><h4>路径点</h4>{path.map((point,index) => <div className="df-segment" key={index}><VectorField label={`路径点 ${index+1}`} value={point} onChange={value=>onChange({...feature,path:path.map((item,i)=>i===index?value:item)})}/><button type="button" disabled={path.length<=2} onClick={()=>onChange({...feature,path:path.filter((_item,i)=>i!==index)})}>删除点</button></div>)}<button type="button" disabled={path.length>=32} onClick={()=>{const last=path.at(-1)||[0,0,0];onChange({...feature,path:[...path,[last[0],last[1],plus(last[2],10)]]})}}>＋ 路径点</button></section>
}
function LoftEditor({ feature, onChange }) {
  const sections=Array.isArray(feature.sections)?feature.sections:[]
  const update=(index,section)=>onChange({...feature,sections:sections.map((item,i)=>i===index?section:item)})
  return <section className="df-contour"><h4>截面</h4>{sections.map((section,index)=><div className="df-segment" key={index}><div className="df-row"><b>截面 {index+1}</b><select aria-label={`截面 ${index+1} 类型`} value={own(section,'radius')?'circle':'rectangle'} onChange={event=>{const next={...section};delete next.radius;delete next.width;delete next.height;update(index,{...next,...(event.target.value==='circle'?{radius:5}:{width:10,height:10})})}}><option value="circle">圆形</option><option value="rectangle">矩形</option></select><button type="button" disabled={sections.length<=2} onClick={()=>onChange({...feature,sections:sections.filter((_item,i)=>i!==index)})}>删除截面</button></div><VectorField label={`截面 ${index+1} 中心`} value={section.origin} onChange={origin=>update(index,{...section,origin})}/>{own(section,'radius')?<ScalarField label={`截面 ${index+1} 半径`} value={section.radius} onChange={radius=>update(index,{...section,radius})}/>:<><ScalarField label={`截面 ${index+1} 宽度`} value={section.width} onChange={width=>update(index,{...section,width})}/><ScalarField label={`截面 ${index+1} 高度`} value={section.height} onChange={height=>update(index,{...section,height})}/></>}</div>)}<button type="button" disabled={sections.length>=12} onClick={()=>{const last=sections.at(-1)||{origin:[0,0,0],radius:5};onChange({...feature,sections:[...sections,{...cloneFeatureValue(last),origin:[last.origin[0],last.origin[1],plus(last.origin[2],10)]}]})}}>＋ 截面</button></section>
}
const mainFields = {
  box:['size'], cylinder:['radius','height'], profile_extrude:['distance'], profile_revolve:['angle'],
  translate:['vector'], rotate:['angle'], fillet:['radius'], chamfer:['length','length2'], shell:['thickness'],
  linear_pattern:['count','vector','fuse'], circular_pattern:['count','angle','fuse'], mirror:['keepOriginal'],
  gear:['module','teeth','pressureAngle','width','boreDiameter'], spring:['meanDiameter','wireDiameter','pitch','turns','lefthand'], sweep:['radius'], loft:['ruled'], profile_loft:['ruled'],surface_offset:['distance','keepOriginal'],move_face:['distance'],surface_boundary:['keepOriginal'],surface_join:['tolerance','makeSolid'],surface_thicken:['thickness','keepOriginal'],body_edit:['vector','angle'],
}
const advancedFields = { box:['origin'], cylinder:['origin','direction'], profile_revolve:['axisStart','axisEnd'], rotate:['axisStart','axisEnd'],body_edit:['axisStart','axisEnd'], circular_pattern:['axisStart','axisEnd'], gear:['profileShift','backlash','origin'], spring:['origin'] }
const boolNames = {makeSolid:'封闭后生成实体',fuse:'融合相交实例',lefthand:'左旋',ruled:'直纹放样',keepOriginal:'保留原体'}
const labels = {tolerance:'连接公差 (mm)',distance:'距离 (mm)',angle:'角度 (°)',radius:'半径 (mm)',height:'高度 (mm)',length:'距离 (mm)',length2:'第二侧 (mm)',count:'数量',vector:'方向 / 步距',thickness:'壁厚 (mm)',size:'尺寸 (mm)'}
const basePlanes = {XY:{xDir:[1,0,0],normal:[0,0,1]},XZ:{xDir:[1,0,0],normal:[0,-1,0]},YZ:{xDir:[0,1,0],normal:[1,0,0]}}
const implicitValues = {origin:[0,0,0],direction:[0,0,1],boreDiameter:0,profileShift:0,backlash:0,keepOriginal:false,fuse:false,lefthand:false,ruled:false}

export function FeatureEditor({ feature, preceding = [], onChange, parameters = {}, disabled = false, onSketch,sketches=[],references=[],onPickPath,onEditGrid,editingGrid=false }) {
  const [error,setError]=useState('')
  // Defaults are display hints only. Do not add optional origins, replace
  // unknown dimensions, or serialize metadata as scalar form inputs.
  const value=key=>own(feature,key)?feature[key]:key==='angle'&&feature.op==='profile_revolve'?360:implicitValues[key]
  const change=next=>{if(!disabled){setError('');onChange(next)}}
  const patch=(key,next)=>{const updated={...feature,[key]:next};if(['axisStart','axisEnd'].includes(key))delete updated.axisReference;change(updated)}
  const field=key=>{
    if(feature.op==='body_edit' && !Object.hasOwn(feature,key))return null
    if(key==='length2'&&!own(feature,key))return null
    const shown=value(key),label=labels[key]||fieldNames[key]||key
    if(own(boolNames,key))return <label key={key} className="df-check"><input type="checkbox" checked={Boolean(shown)} onChange={event=>patch(key,event.target.checked)}/>{boolNames[key]}</label>
    if(['size','origin','direction','vector','axisStart','axisEnd'].includes(key))return <VectorField key={key} label={label} value={shown} dimensions={feature.op==='profile_revolve'&&key.startsWith('axis')?2:3} axes={key==='size'?['长','宽','高']:undefined} onChange={next=>patch(key,next)}/>
    return <ScalarField key={key} label={label} value={shown} onChange={next=>patch(key,next)}/>
  }
  const mirrorPlane=plane=>{
    const next={...feature,plane};delete next.planeReference
    if(references.some(ref=>ref.id===plane&&ref.kind==='plane')){next.planeReference=plane;next.plane='custom';next.frame=referenceFrames({references,parameters})[plane];delete next.origin;change(next);return}
    if(plane==='custom'){
      next.frame=feature.frame?cloneFeatureValue(feature.frame):{origin:cloneFeatureValue(value('origin')),...cloneFeatureValue(basePlanes[feature.plane]||basePlanes.YZ)}
      delete next.origin
    }else{next.origin=cloneFeatureValue(feature.frame?.origin||value('origin')||[0,0,0]);delete next.frame}
    change(next)
  }
  const startSketch=()=>{
    if(disabled||!onSketch)return
    try{
      if(['box','cylinder'].includes(feature.op)){
        const profile=editableProfile(feature,parameters),next={...feature,...profile}
        for(const key of ['size','radius','height','direction'])delete next[key]
        if(profile.plane==='custom')delete next.origin
        change(next)
      }
      onSketch()
    }catch(reason){setError(reason.message)}
  }
  const supportsSketch=['box','cylinder','profile_extrude','profile_revolve','profile_sweep'].includes(feature.op)
  const advanced=advancedFields[feature.op]||[]
  return <fieldset className="df-feature-editor" disabled={disabled} style={{border:0,padding:0,margin:0,minWidth:0}}>
    <h3>{featureNames[feature.op]} <small>{feature.id}</small></h3>
    {'input' in feature&&<label className="df-field"><span>实体</span><select aria-label="引用实体" value={feature.input} onChange={event=>patch('input',event.target.value)}><option value="">选择前置特征</option>{feature.input&&!preceding.some(item=>item.id===feature.input)&&<option value={feature.input} disabled>{feature.input}（不可用）</option>}{preceding.map(item=><option value={item.id} key={item.id}>{item.label||item.id}</option>)}</select></label>}
    {Array.isArray(feature.inputs)&&<div className="df-field"><span>{feature.op==='cut'?'目标 / 工具体':'参与实体'}</span>{feature.inputs.map((input,index)=><div className="df-row" key={index}><select aria-label={`参与实体 ${index+1}`} value={input} onChange={event=>patch('inputs',feature.inputs.map((item,i)=>i===index?event.target.value:item))}><option value="">选择实体</option>{input&&!preceding.some(item=>item.id===input)&&<option value={input} disabled>{input}（不可用）</option>}{preceding.map(item=><option value={item.id} key={item.id}>{item.label||item.id}</option>)}</select><button type="button" disabled={feature.inputs.length<=2} onClick={()=>patch('inputs',feature.inputs.filter((_item,i)=>i!==index))}>移除</button></div>)}<button type="button" disabled={feature.inputs.length>=32} onClick={()=>patch('inputs',[...feature.inputs,preceding.find(item=>!feature.inputs.includes(item.id))?.id||''])}>＋ 实体</button></div>}
    {feature.op==='mirror'&&<label className="df-field"><span>镜像平面</span><select aria-label="镜像平面" value={feature.planeReference||value('plane')} onChange={event=>mirrorPlane(event.target.value)}>{[['XY','上视平面 XY'],['XZ','前视平面 XZ'],['YZ','右视平面 YZ'],['custom','自定义平面'],...references.filter(r=>r.kind==='plane').map(r=>[r.id,r.label||r.id])].map(([code,label])=><option key={code} value={code}>{label}</option>)}</select></label>}
    {(['rotate','circular_pattern','profile_revolve'].includes(feature.op)||feature.op==='body_edit'&&feature.action==='rotate')&&references.some(r=>r.kind==='axis')&&<label className="df-field"><span>旋转参考轴</span><select aria-label="旋转参考轴" value={feature.axisReference||''} onChange={event=>{const next={...feature};if(event.target.value)next.axisReference=event.target.value;else delete next.axisReference;change(next)}}><option value="">自定义轴起点和终点</option>{references.filter(r=>r.kind==='axis').map(r=><option value={r.id} key={r.id}>{r.label||r.id}</option>)}</select></label>}
    {(mainFields[feature.op]||[]).map(field)}
    {feature.op==='profile_sweep'&&<CadSweepFields disabled={disabled} feature={feature} onChange={change} sketches={sketches} parameters={parameters} references={references} onPickPath={onPickPath}/>}
    {feature.op==='profile_loft'&&<CadLoftFields feature={feature} onChange={change} sketches={sketches} parameters={parameters} references={references} onSketch={onSketch}/>}
    {feature.op==='surface_style'&&<CadSurfaceGridFields feature={feature} onChange={change} onEditGrid={onEditGrid} editingGrid={editingGrid} parameters={parameters}/>}
    {feature.op==='surface_boundary'&&<label className="df-field"><span>边界连续性</span><select aria-label="边界连续性" value={feature.continuity||'position'} onChange={e=>patch('continuity',e.target.value)}><option value="position">位置连续 G0</option><option value="tangent">相切连续 G1</option><option value="curvature">曲率连续 G2</option></select></label>}
    {feature.op==='standard_part'&&Object.entries(feature.dimensions||{}).map(([key,value])=><ScalarField key={key} label={parameters[value]?.label?.split(' · ').at(-1)||fieldNames[key]||key} value={value} onChange={v=>patch('dimensions',{...feature.dimensions,[key]:v})}/>)}

    {supportsSketch&&onSketch&&<button type="button" className="ce-sketch-reference" onClick={startSketch}>▱ {feature.label||feature.id} · 草图 <span>编辑</span></button>}
    {feature.op==='chamfer'&&<button type="button" onClick={()=>{if(own(feature,'length2')){const next={...feature};delete next.length2;change(next)}else patch('length2',value('length'))}}>{own(feature,'length2')?'恢复等距倒角':'设置第二侧倒角距离'}</button>}
    {feature.op==='sweep'&&<PathEditor feature={feature} onChange={change}/>}{feature.op==='loft'&&<LoftEditor feature={feature} onChange={change}/>}
    <details className="df-feature-details"><summary>更多设置</summary>
      <label className="df-field"><span>名称</span><input aria-label="特征名称" maxLength={200} value={feature.label||''} onChange={event=>patch('label',event.target.value)}/></label>
      {advanced.map(field)}
      {['profile_extrude','profile_revolve','profile_sweep'].includes(feature.op)&&<><p className="df-note">草图平面：{feature.plane==='custom'?'所选平面':feature.plane}</p>{feature.plane!=='custom'&&<VectorField label="草图原点" value={own(feature,'origin')?feature.origin:[0,0,0]} onChange={origin=>patch('origin',origin)}/>}</>}
      {feature.op==='mirror'&&(feature.plane==='custom'?['origin','xDir','normal'].map(key=><VectorField key={key} label={{origin:'平面原点',xDir:'平面 X 方向',normal:'平面法线'}[key]} value={feature.frame?.[key]} onChange={next=>patch('frame',{...feature.frame,[key]:next})}/>):field('origin'))}
      {['fillet','chamfer'].includes(feature.op)&&typeof feature.edges==='string'&&<label className="df-field"><span>选边规则</span><select aria-label="选边规则" value={feature.edges} onChange={event=>patch('edges',event.target.value)}>{[['all','全部边'],['parallelX','平行 X'],['parallelY','平行 Y'],['parallelZ','平行 Z']].map(([code,label])=><option key={code} value={code}>{label}</option>)}</select></label>}
      {feature.op==='shell'&&Array.isArray(feature.faces)&&feature.faces.every(face=>typeof face==='string')&&<p className="df-note">开口规则：{feature.faces.join('、')}；也可在模型上重新选面。</p>}
      {feature.op==='gear'&&<p className="df-note">外啮合直齿轮；根切组合会被拒绝。</p>}
      {feature.op==='spring'&&<p className="df-note">等螺距开放端；螺距须大于线径的 1.05 倍。</p>}
      {feature.op==='shell'&&<p className="df-note">负壁厚向内抽壳，正壁厚向外偏置。</p>}
      {feature.op==='sweep'&&<p className="df-note">圆截面沿路径移动，转角采用斜接。</p>}
      {feature.op==='loft'&&<p className="df-note">截面平行于 XY，按 Z 高度递增。</p>}
    </details>
    {error&&<p className="df-error" role="alert">{error}</p>}
  </fieldset>
}
export function ParameterEditor({ parameters, onChange }) {
  const [keyDraft, setKeyDraft] = useState('')
  const [error, setError] = useState('')
  const update = (key, value) => onChange({ ...parameters, [key]: value })
  return <section className="df-parameters"><h3>尺寸与表达式</h3><p>特征数值可以引用参数名，例如 <code>width / 2</code>。</p>{Object.entries(parameters).map(([key, parameter]) => <div className="df-parameter-row" key={key}><div className="df-row"><b>{key}</b><button title="使用中的参数删除后会提示修复" onClick={() => { const next = { ...parameters }; delete next[key]; onChange(next) }}>删除参数</button></div><input aria-label={`${key} 显示名称`} placeholder="显示名称" value={parameter.label || ''} onChange={event => update(key, { ...parameter, label: event.target.value })} /><select aria-label={`${key} 取值方式`} value={Object.hasOwn(parameter, 'expression') ? 'expression' : 'value'} onChange={event => { const next = { ...parameter }; if (event.target.value === 'expression') { next.expression = parameter.value === null ? '' : String(parameter.value); next.value = null; } else { const numeric = Number(parameter.expression); delete next.expression; next.value = parameter.expression?.trim() && Number.isFinite(numeric) ? numeric : null; } update(key, next) }}><option value="value">直接数值</option><option value="expression">关联表达式</option></select><input aria-label={`${key} 数值或表达式`} value={Object.hasOwn(parameter, 'expression') ? parameter.expression : parameter.value ?? ''} placeholder={Object.hasOwn(parameter, 'expression') ? '例如 width / 2' : '待补充'} onChange={event => { const next = { ...parameter, source: { type: 'user', text: '手工特征工作台修改' } }; if (Object.hasOwn(parameter, 'expression')) { next.expression = event.target.value; next.value = null; } else next.value = event.target.value.trim() === '' ? null : featureScalar(event.target.value); update(key, next) }} /></div>)}<div className="df-row"><input aria-label="新参数名" value={keyDraft} placeholder="英文参数名" onChange={event => setKeyDraft(event.target.value)} /><button onClick={() => { if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(keyDraft) || Object.hasOwn(parameters, keyDraft)) { setError('参数名需以字母开头，且不能重复。'); return } update(keyDraft, { value: 10, label: keyDraft, source: { type: 'user', text: '手工新增尺寸' } }); setKeyDraft(''); setError('') }}>＋ 参数</button></div>{error && <p role="alert" className="df-error">{error}</p>}</section>
}
