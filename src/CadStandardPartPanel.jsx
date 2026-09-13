import { useState } from 'react'
import { cadStandardParts, insertCadStandardPart } from './cadStandardPartModel.js'
import './cad-editor-panels.css'

export default function CadStandardPartPanel({ plan, onApply, onClose, disabled = false, initialPosition = [0, 0, 0] }) {
  const [catalogId, setCatalogId] = useState(cadStandardParts[0].catalogId), [position, setPosition] = useState(initialPosition.map(String)), [axis, setAxis] = useState('Z')
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const part = cadStandardParts.find(item => item.catalogId === catalogId)
  const insert = async event => {
    event.preventDefault()
    if (disabled || busy) return
    setError('')
    try {
      if (position.some(value => !String(value).trim())) throw new Error('请填写三个位置坐标。')
      const result = insertCadStandardPart(plan, catalogId, { position: position.map(Number), axis })
      setBusy(true)
      await onApply?.(result.plan, { label: result.label, catalogId, featureIds: result.featureIds, parameterNames: result.parameterNames })
    } catch (reason) { setError(reason.message || '标准件插入失败。') }
    finally { setBusy(false) }
  }
  return <section className="cad-editor-panel" aria-label="插入标准件"><header><h2>插入标准件</h2>{onClose && <button type="button" onClick={onClose} disabled={busy}>关闭</button>}</header>
    <form onSubmit={insert}>
      <label>名称与规格<select aria-label="标准件规格" value={catalogId} onChange={event => setCatalogId(event.target.value)} disabled={disabled || busy}>{cadStandardParts.map(item => <option key={item.catalogId} value={item.catalogId}>{item.name} · {item.spec}</option>)}</select></label>
      <fieldset disabled={disabled || busy}><legend>定位（mm）</legend><div className="cad-panel-coordinates">{position.map((value, index) => <label key={index}>{['X', 'Y', 'Z'][index]}<input aria-label={`标准件位置 ${['X', 'Y', 'Z'][index]}`} inputMode="decimal" value={value} onChange={event => setPosition(values => values.map((old, i) => i === index ? event.target.value : old))} /></label>)}</div><label>轴向<select aria-label="标准件轴向" value={axis} onChange={event => setAxis(event.target.value)}>{['X', 'Y', 'Z'].map(value => <option key={value}>{value}</option>)}</select></label></fieldset>
      <p className="cad-panel-help">{part.source}。插入独立实体，尺寸和位置保留为可编辑参数。</p>
      <details><summary>结构与尺寸来源</summary><p>{part.kind === 'bearing' ? '包含独立内外圈、圆弧滚道、滚珠与带球窝的铆接保持架。公布尺寸来自产品目录；滚道曲率、游隙和保持架厚度为可编辑设计参数。' : part.kind === 'socket_screw' ? '包含 60° 连续螺旋牙型、圆弧牙底、内六角孔及端部倒角。公称尺寸按所列 ISO 规格。' : part.kind === 'pin' ? '包含完整圆柱段与两端 15° 导入端。' : '包含公称厚度和贯通中心孔。'}</p><a href={part.sourceUrl} target="_blank" rel="noreferrer">查看尺寸来源</a><p>更改规格参数后按自定义尺寸重建；模型不代替材料与制造公差验收。</p></details>
      {error && <p role="alert">{error}</p>}<button className="primary-button" disabled={disabled || busy || !onApply} type="submit">{busy ? '正在插入…' : '插入当前模型'}</button>
    </form></section>
}
