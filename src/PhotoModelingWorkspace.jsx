import {useEffect, useRef, useState} from 'react'
import {photoModelingPrompt, addPhotoFiles, setPhotoPixelCoordinate} from './photoModeling.js'
import {readableError} from './workspaceFeedback.js'
import './cad-learning.css'
import './photo-modeling-workspace.css'

export default function PhotoModelingWorkspace({onPrepare, busy = false}) {
  const [photos, setPhotos] = useState([]), [points, setPoints] = useState([]), [activePoint, setActivePoint] = useState(0)
  const [coordinateInputs, setCoordinateInputs] = useState([['', ''], ['', '']])
  const [length, setLength] = useState(''), [plane, setPlane] = useState('正视参考平面'), [dimensions, setDimensions] = useState(''), [description, setDescription] = useState('')
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [preparing, setPreparing] = useState(false)
  const image = useRef(null), resources = useRef([]), submitting = useRef(false)
  const disabled = busy || preparing, main = photos[0], size = main?.status === 'loaded' ? {width: main.width, height: main.height} : {width: 0, height: 0}
  useEffect(() => () => resources.current.forEach(url => URL.revokeObjectURL(url)), [])
  const clearCalibration = () => {setPoints([]); setCoordinateInputs([['', ''], ['', '']]); setActivePoint(0); setLength(''); setError(''); setNotice('')}
  const addFiles = chosen => {
    if (!chosen.length || disabled) return
    try {
      const next = addPhotoFiles(photos.map(item => item.file), chosen)
      const added = next.slice(photos.length).map(file => {const url = URL.createObjectURL(file); resources.current.push(url); return {file, url, status: 'loading'}})
      setPhotos(current => [...current, ...added]); setError(''); setNotice(added.length ? '' : '这些照片已在列表中。')
    } catch (reason) {setError(reason.message)}
  }
  const remove = index => {
    const deleted = photos[index]; URL.revokeObjectURL(deleted.url); resources.current = resources.current.filter(url => url !== deleted.url)
    setPhotos(current => current.filter((_, i) => i !== index))
    if (index === 0) clearCalibration()
  }
  const load = (url, element) => setPhotos(current => current.map(item => item.url === url ? {...item, status: 'loaded', width: element.naturalWidth, height: element.naturalHeight} : item))
  const failed = url => {setPhotos(current => current.map(item => item.url === url ? {...item, status: 'error'} : item)); setError('有照片无法读取，请移除损坏图片并重新选择。')}
  const ready = async () => {
    if (disabled || submitting.current) return
    submitting.current = true; setPreparing(true); setError(''); setNotice('')
    try {
      if (!photos.length || photos.some(item => item.status !== 'loaded')) throw new Error('请等待全部照片加载，或移除无法读取的照片')
      const prompt = photoModelingPrompt({points, referenceLength: Number(length), ...size, knownDimensions: dimensions, description, plane, filename: main.file.name})
      if (typeof onPrepare !== 'function') throw new Error('建模会话暂未连接，请稍后重试')
      const result = await onPrepare(photos.map(item => item.file), prompt)
      if (result === false) throw new Error('照片未能添加到会话，请检查当前任务状态后重试')
      setNotice('照片和标定说明已添加到建模会话，可在会话中检查并发送。')
    } catch (reason) {setError(readableError(reason))}
    finally {submitting.current = false; setPreparing(false)}
  }
  const marked = points.filter(point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite)).length
  return <section className="cad-learning"><header><span className="learning-kicker">PHOTO TO CAD</span><h1>照片逆向建模</h1><p>补充尺度与结构信息，把零件外形照片送入可继续修改的建模会话。</p></header>
    <div className="photo-layout"><div className="photo-main">
      <label className="photo-upload">添加照片（已选 {photos.length}/4）<input aria-label="添加零件照片" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" multiple disabled={disabled || photos.length >= 4} onChange={event => {const chosen = Array.from(event.target.files || []); event.target.value = ''; addFiles(chosen)}}/></label>
      {error && <p className="learning-error" role="alert">{error}</p>}
      {main ? <><p className="photo-main-name">主照片：{main.file.name}{main.status === 'loading' ? ' · 正在读取…' : main.status === 'error' ? ' · 无法读取' : ` · ${main.width}×${main.height}`}</p>
        <div className={`photo-stage ${activePoint === null ? 'calibrated' : ''}`} onClick={event => {
          if (disabled || main.status !== 'loaded' || activePoint === null || !image.current) return
          const rect = image.current.getBoundingClientRect(), x = (event.clientX - rect.left) / rect.width, y = (event.clientY - rect.top) / rect.height
          if (x >= 0 && x <= 1 && y >= 0 && y <= 1) {setPoints(current => {const next = [...current]; next[activePoint] = [x, y]; return next}); setCoordinateInputs(current => current.map((point, index) => index === activePoint ? [(x * size.width).toFixed(3), (y * size.height).toFixed(3)] : point)); setActivePoint(activePoint === 0 ? 1 : null); setError('')}
        }}>
          <img key={main.url} ref={image} src={main.url} alt="主照片，可点击标记参考尺寸两端" onLoad={event => load(main.url, event.currentTarget)} onError={() => failed(main.url)}/>
          <svg viewBox="0 0 100 100" preserveAspectRatio="none">{marked === 2 && <line x1={points[0][0] * 100} y1={points[0][1] * 100} x2={points[1][0] * 100} y2={points[1][1] * 100} stroke="#f24751" strokeWidth=".5"/>}{points.map((point, index) => point?.every(Number.isFinite) && <g key={index}><circle cx={point[0] * 100} cy={point[1] * 100} r="1.1" fill="#f24751"/><text x={point[0] * 100 + 1.5} y={point[1] * 100} fill="#e82c39" fontSize="3">{index + 1}</text></g>)}</svg>
        </div>
        <div className="photo-point-actions">{[0, 1].map(index => <button key={index} disabled={disabled || main.status !== 'loaded'} aria-pressed={activePoint === index} onClick={() => setActivePoint(index)}>重选参考点 {index + 1}</button>)}<button disabled={disabled || !points.length} onClick={clearCalibration}>清除标定</button></div>
      </> : <div className="photo-empty">先添加主视照片，再补充侧面、背面或局部照片。<br/>点选主照片中一个已知尺寸的两端。</div>}
      <div className="photo-thumbnails">{photos.map((item, index) => <figure key={item.url}><img src={item.url} alt={`${index ? '补充' : '主'}照片 ${index + 1}`} onLoad={event => load(item.url, event.currentTarget)} onError={() => failed(item.url)}/><figcaption>{item.file.name}{item.status === 'error' && ' · 无法读取'}</figcaption><button disabled={disabled} onClick={() => remove(index)}>移除照片 {index + 1}</button>{index > 0 && <button disabled={disabled || item.status !== 'loaded'} onClick={() => {setPhotos(current => [current[index], ...current.filter((_, i) => i !== index)]); clearCalibration()}}>设为主图并重新标定</button>}</figure>)}</div>
    </div><fieldset disabled={disabled} className="photo-form"><h2>1 · 标定参考尺寸</h2><p>{marked === 2 ? '两个参考点已标记。重选点位请使用图片下方按钮，避免误触覆盖。' : activePoint === null ? '请补全两个参考点的像素坐标；要在图片上选点，请先点击图片下方的重选按钮。' : `请在主照片点选参考点 ${activePoint === 1 ? 2 : 1}，也可填写像素坐标。`}</p>
      {size.width > 0 && <div className="photo-coordinates">{[0, 1].map(index => <div key={index}><b>参考点 {index + 1}</b>{['X', 'Y'].map((axis, coordinate) => <label key={axis}>{axis} 像素<input aria-label={`参考点 ${index + 1} ${axis} 像素`} inputMode="decimal" value={coordinateInputs[index][coordinate]} placeholder="尚未标记" onChange={event => {const text = event.target.value; setCoordinateInputs(current => current.map((point, i) => i === index ? point.map((value, axis) => axis === coordinate ? text : value) : point)); setActivePoint(null); try {setPoints(setPhotoPixelCoordinate(points, index, coordinate, text, size)); setError('')} catch (reason) {setPoints(setPhotoPixelCoordinate(points, index, coordinate, '', size)); setError(reason.message)}}}/></label>)}</div>)}</div>}
      <label>两点实际距离 mm<input type="number" min=".001" step="any" placeholder="填写已知尺寸" value={length} onChange={event => setLength(event.target.value)}/></label><label>参考位置 / 平面<input value={plane} onChange={event => setPlane(event.target.value)} placeholder="例如正面两个安装孔中心"/></label>
      <h2>2 · 补充已知信息</h2><label>尺寸、孔深、壁厚、背面结构<textarea rows="5" value={dimensions} onChange={event => setDimensions(event.target.value)} placeholder="例如总厚度 12 mm，中心孔 Ø20 贯穿；背面平整"/></label><label>零件描述与建模范围<textarea rows="4" value={description} onChange={event => setDescription(event.target.value)} placeholder="描述想要复刻的结构，说明是否需要螺纹、倒角等"/></label>
      <h2>3 · 进入建模会话</h2><p>照片的透视和遮挡可能留下未确定尺寸，后续可在会话中补充并核对生成结果。</p>{notice && <p role="status">{notice}</p>}<button className="learning-primary" disabled={disabled || !photos.length || photos.some(item => item.status !== 'loaded')} onClick={ready}>{preparing ? '正在添加到会话…' : '添加照片和标定说明到会话'}</button>
    </fieldset></div>
  </section>
}
