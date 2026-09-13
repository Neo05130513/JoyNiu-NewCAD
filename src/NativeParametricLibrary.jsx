import { createClientId } from './clientId.js'
import {useEffect, useState} from 'react'
import {evaluateFormula, transformEntity} from './nativeDrawingModel.js'
import {createNativeTemplate, prepareNativeTemplate} from './nativeParametricLibrary.js'
import {nativeDialogKey} from './nativeDrawingDraft.js'

export default function NativeParametricLibrary({document, selected, constraints, parameters, translation, busy, operate, fit, onError, onDirtyChange=()=>{}, resetToken=0}) {
  const [name, setName] = useState('我的参数构件')
  const [editor, setEditor] = useState(null)
  const [values, setValues] = useState({})
  const [position, setPosition] = useState([0, 0])
  const [initial, setInitial] = useState(null)
  const [pending, setPending] = useState(null)
  const dirty=!!editor&&JSON.stringify({values,position})!==initial
  useEffect(()=>{onDirtyChange(dirty)},[dirty,onDirtyChange])
  useEffect(()=>()=>onDirtyChange(false),[onDirtyChange])
  useEffect(()=>{setEditor(null);setPending(null)},[resetToken])
  const properties = document.customProperties || {}, library = properties.library || [], instances = properties.libraryInstances || []
  useEffect(()=>{
    if(editor&&(editor.instance?!instances.some(item=>item.id===editor.instance.id):!library.some(item=>item.id===editor.template.id))){setEditor(null);setPending(null)}
  },[document.id,document.revision])
  const updateLibrary = next => operate([{op: 'metadata', customProperties: {...properties, library: next}}])
  const attempt = async fn => {try {return await fn()} catch (error) {onError(error.message)}}
  const edit = (template, instance) => {
    if(dirty){setPending(()=>()=>{setEditorState(template,instance)});return}
    setEditorState(template,instance)
  }
  const setEditorState = (template, instance) => {
    setEditor({template, instance})
    const reverse = Object.fromEntries(Object.entries(instance?.parameterMap || {}).map(([key, value]) => [value, key]))
    const current = instance ? Object.fromEntries(Object.entries(instance.parameterMap).map(([key, mapped]) => {
      const value = parameters[mapped] ?? instance.parameters[key]
      return [key, typeof value === 'string' ? value.replace(/[A-Za-z_]\w*/g, token => reverse[token] || token) : value]
    })) : template.parameters
    const placement=[...(instance?.translation || translation)]
    setValues({...current})
    setPosition(placement)
    setInitial(JSON.stringify({values:current,position:placement}))
  }
  const saveSelected = () => attempt(async () => {
    const template = createNativeTemplate({id: createClientId(), name, entities: selected, constraints, parameters})
    const result = await updateLibrary([...library, template])
    if (result) edit(template)
  })
  const insert = replace => attempt(async () => {
    if(position.some(value=>String(value).trim()===''||!Number.isFinite(Number(value))))throw new Error('请填写有效的插入坐标')
    const operation = prepareNativeTemplate(editor.template, values, position.map(Number), replace ? editor.instance?.id : undefined)
    const result = await operate([operation])
    if (result) {fit(result); setEditor(null)}
  })
  const defaults = () => attempt(async () => {
    prepareNativeTemplate(editor.template, values)
    const updated = {...editor.template, parameters: {...values}}
    const result = await updateLibrary(library.map(item => item.id === updated.id ? updated : item))
    if (result) {setEditor({...editor, template: updated});setInitial(JSON.stringify({values,position}))}
  })
  return <>
    <h3>我的参数图库</h3>
    <label>构件名称<input value={name} onChange={event => setName(event.target.value)}/></label>
    <button disabled={busy || !selected.length} onClick={saveSelected}>保存所选图元、内部约束和参数</button>
    <p>先为线长、半径等尺寸关联参数。图库保存所需公式及内部约束，插入时重新求解各个尺寸；跨越选择范围的约束须先解除或一起选入。</p>
    {library.map(item => <div className="native-record" key={item.id}>
      <span>{item.name}<small> · {item.version === 2 ? `${Object.keys(item.parameters).length} 参数` : '旧版静态几何'}</small></span>
      <button disabled={busy} onClick={() => item.version === 2 ? edit(item) : attempt(async () => {const result = await operate(item.entities.map(entity => ({op: 'add', entity: transformEntity(entity, {translation})}))); if (result) fit(result)})}>{item.version === 2 ? '参数与插入' : '原尺寸插入'}</button>
      <button disabled={busy} title={`删除图库构件 ${item.name}`} onClick={() => {const remove=async()=>{const result=await updateLibrary(library.filter(record => record.id !== item.id));if(result&&editor?.template.id===item.id)setEditor(null)};if(dirty&&editor?.template.id===item.id)setPending(()=>remove);else remove()}}>×</button>
    </div>)}
    {editor && <>
      <h3>{editor.instance ? '修改实例参数' : '插入参数构件'} · {editor.template.name}</h3>
      {Object.entries(values).map(([key, value]) => <label key={key}>{key} · 值或公式<input aria-label={`构件参数 ${key}`} value={value} onChange={event => setValues(current => ({...current, [key]: event.target.value}))}/><small>{(() => {try {return `求值 ${Number(evaluateFormula(value, values).toFixed(5))}`} catch {return '请检查公式或参数引用'}})()}</small></label>)}
      <div className="native-pair">{['X', 'Y'].map((axis, index) => <label key={axis}>插入位移 {axis}<input type="number" value={position[index]} onChange={event => setPosition(current => current.map((value, i) => i === index ? event.target.value : value))}/></label>)}</div>
      <p>保留模板方向；参数、约束及图元在同次保存中绑定。实例若新增外部约束或关联标注，须先解除后再重建。</p>
      <div className="native-button-grid">
        <button className="primary" disabled={busy} onClick={() => insert(false)}>求解并插入新实例</button>
        {editor.instance ? <button disabled={busy} onClick={() => insert(true)}>重建当前实例</button> : <button disabled={busy} onClick={defaults}>保存为默认参数</button>}
        <button disabled={busy} onClick={() => dirty?setPending(()=>()=>setEditor(null)):setEditor(null)}>关闭参数</button>
      </div>
    </>}
    {!!instances.length && <h3>已插入的参数实例</h3>}
    {instances.map(instance => <div className="native-record" key={instance.id}><span>{instance.name} · {instance.entityIds.length} 图元</span><button disabled={busy} onClick={() => edit(instance.template, instance)}>编辑参数</button></div>)}
    {pending&&<div className="native-draft-dialog" role="dialog" aria-modal="true" aria-label="未保存的构件参数" onKeyDown={event=>nativeDialogKey(event,()=>setPending(null))}><div><h2>构件参数尚未保存</h2><p>保留输入继续编辑，或放弃本次参数修改。</p><button autoFocus onClick={()=>setPending(null)}>继续编辑</button><button onClick={()=>{const action=pending;setPending(null);action()}}>放弃参数修改并继续</button></div></div>}
  </>
}
