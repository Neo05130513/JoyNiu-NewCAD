import { useEffect, useRef, useState } from 'react'
import { cadImportClient } from './cadImportClient.js'
import { insertImportedBody } from './cadImportBodyModel.js'
import { createClientId } from './clientId.js'
import './cad-editor-panels.css'

export default function CadImportBodyPanel({ plan, token, accountKey, active = true, disabled = false, initialPosition = [0, 0, 0], onApply, onClose }) {
  const [file, setFile] = useState(null), [name, setName] = useState(''), [units, setUnits] = useState('mm'), [position, setPosition] = useState(initialPosition.map(String)), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const task = useRef(null), retry = useRef(null), credentials = useRef(token), context = useRef('')
  const key = `${accountKey || token || ''}:${active}:${JSON.stringify(plan)}`
  context.current = key; credentials.current = token
  useEffect(() => { task.current?.abort(); task.current = null; setBusy(false); return () => { task.current?.abort(); task.current = null } }, [key])
  useEffect(() => { setFile(null); setName(''); setError(''); retry.current = null }, [accountKey || token || ''])
  const stl = file?.name?.toLowerCase().endsWith('.stl')
  const blocked = disabled || busy || !token || !active
  const chooseFile = event => {
    const value = event.target.files?.[0] || null
    retry.current = null; setError(''); setFile(value); setName(value?.name?.replace(/\.(step|stp|stl)$/i, '') || '')
  }
  const close = () => { task.current?.abort(); task.current = null; setBusy(false); onClose?.() }
  const submit = async event => {
    event.preventDefault()
    if (blocked || task.current || !onApply) return
    if (!file || !/\.(step|stp|stl)$/i.test(file.name)) { setError('请选择 STEP、STP 或 STL 文件。'); return }
    if (!file.size || file.size > 20 * 1024 ** 2) { setError('导入文件须为 1 字节至 20 MB。'); return }
    if (!name.trim() || position.some(value => !String(value).trim() || !Number.isFinite(Number(value)))) { setError('请填写实体名称和三个有效定位坐标。'); return }
    const fingerprint = JSON.stringify([name.trim(), stl ? units : 'mm'])
    if (retry.current?.fingerprint !== fingerprint) retry.current = { fingerprint, requestId: createClientId() }
    const stamp = context.current, controller = new AbortController(); task.current = controller; setBusy(true); setError('')
    try {
      const asset = await cadImportClient.upload(() => credentials.current, file, { name: name.trim(), units: stl ? units : 'mm', requestId: retry.current.requestId }, controller.signal)
      if (controller.signal.aborted || context.current !== stamp) return
      const result = insertImportedBody(plan, asset, { position: position.map(Number) })
      await onApply(result.plan, { assetId: asset.id, featureIds: result.featureIds, label: result.label, originalFormat: asset.originalFormat })
    } catch (reason) { if (!controller.signal.aborted && context.current === stamp) setError(reason.message || '实体导入失败。') }
    finally { if (task.current === controller) { task.current = null; if (context.current === stamp) setBusy(false) } }
  }
  return <section className="cad-editor-panel" aria-label="导入实体"><header><h2>导入实体</h2>{onClose && <button type="button" onClick={close}>{busy ? '取消导入' : '关闭'}</button>}</header><form onSubmit={submit}>
    <label>源文件<input type="file" aria-label="实体源文件" accept=".step,.stp,.stl" onChange={chooseFile} disabled={blocked} /></label>
    <label>实体名称<input aria-label="导入实体名称" value={name} maxLength={180} onChange={event => setName(event.target.value)} disabled={blocked} /></label>
    {stl ? <label>STL 原始单位<select aria-label="STL 原始单位" value={units} onChange={event => setUnits(event.target.value)} disabled={blocked}><option value="mm">毫米 mm</option><option value="cm">厘米 cm</option><option value="m">米 m</option><option value="inch">英寸 inch</option></select></label> : <p className="cad-panel-help">STEP 使用文件内置单位并转换为毫米。</p>}
    <fieldset disabled={blocked}><legend>相对原坐标的位移（mm）</legend><div className="cad-panel-coordinates">{position.map((value, index) => <label key={index}>{['X', 'Y', 'Z'][index]}<input aria-label={`导入位移 ${['X', 'Y', 'Z'][index]}`} inputMode="decimal" value={value} onChange={event => setPosition(values => values.map((old, i) => i === index ? event.target.value : old))} /></label>)}</div></fieldset>
    <p className="cad-panel-help">{stl ? 'STL 的闭合三角网格将转换为可编辑实体。曲面仍为原三角面，不推测原始造型历史。' : '保留真实实体，导入后可直接选面、圆角、移动面或切除挖孔。'}</p>
    {error && <p role="alert">{error}</p>}<button type="submit" className="primary-button" disabled={blocked || !onApply}>{busy ? '正在转换与核验…' : '导入并开始编辑'}</button>
  </form></section>
}
