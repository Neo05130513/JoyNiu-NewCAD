import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { appendSketchSegment, numericSketchPoint, removeSketchPoint, removeSketchSegment, replaceSketchShape, sameSketchPoint, screenToSketch, sketchAxes, sketchGeometry, sketchNumber, sketchPoints, sketchViewBox, updateSketchPoint } from './featureSketchModel.js'
import { addSketchContour, deleteSketchContour, replaceSketchContour, sketchContour, sketchContourFeature, sketchContourList, sketchEdgeRef, sketchPointRef, setSketchContourRole, constraintContours } from './featureSketchContours.js'
import { edgeAddress, pointAddress, sketchCurveKeys } from './featureSketchConstraintGeometry.js'
import { ellipsePoint } from './featureSketchCurves.js'
import { evaluateSketchExpression, sketchParameterLeaves } from './featureSketchExpressions.js'
import { addSketchConstraint, changeLinkedSketchParameter, changeSketchConstraintValue, constraintName, driveSketchDimension, driveSketchEllipseRadius, moveLinkedSketchEdge, moveLinkedSketchPoint, setSketchPointExpressions, sketchEdgeMeasurement, validateSketchConstraints } from './featureSketchConstraints.js'
import { createSketchHistory, recordSketchHistory, sketchHistoryValue, syncSketchHistory, travelSketchHistory } from './featureSketchHistory.js'
import './feature-sketch-editor.css'

const display = value => value === undefined ? '' : String(value)
const pointText = point => (point || []).map(display).join(', ')
const canvasViewBox = (feature, parameters) => {
  const bounds=sketchContourList(feature).filter(item=>item.segments?.length).map(item=>sketchViewBox(item,parameters))
  if(!bounds.length)return[-50,-50,100,100]
  const x=Math.min(...bounds.map(b=>b[0])),y=Math.min(...bounds.map(b=>b[1]))
  return[x,y,Math.max(...bounds.map(b=>b[0]+b[2]))-x,Math.max(...bounds.map(b=>b[1]+b[3]))-y]
}

function FeatureSketchEditorBody({ feature: documentFeature, onChange, parameters = {}, onParametersChange, disabled = false, active = true, variant = 'canvas', toolbarHost = null }) {
  const [contourId,setContourId]=useState('main'),projection=useRef(null)
  const effectiveContour=sketchContourList(documentFeature).some(item=>item.id===contourId)?contourId:'main'
  if(!projection.current||projection.current.source!==documentFeature||projection.current.id!==effectiveContour)projection.current={source:documentFeature,id:effectiveContour,feature:sketchContourFeature(documentFeature,effectiveContour)}
  const feature=projection.current.feature
  const edgeRef=index=>sketchEdgeRef(effectiveContour,index),pointRef=key=>sketchPointRef(effectiveContour,key)
  const [pointSelection,setPointSelection]=useState([])
  const [edgeSelection,setEdgeSelection]=useState([]),[constraintType,setConstraintType]=useState('parallel'),[constraintValue,setConstraintValue]=useState(''),[constraintDrafts,setConstraintDrafts]=useState({}),[drawMode,setDrawMode]=useState('append'),[splinePoints,setSplinePoints]=useState([]),[ellipseMajor,setEllipseMajor]=useState(null)
  const [history, setHistory] = useState(() => createSketchHistory(documentFeature, parameters)), historyRef = useRef(history)
  historyRef.current = history
  const [selected, setSelected] = useState(null), [tool, setTool] = useState('select'), [arcThrough, setArcThrough] = useState(null)
  const [viewport, setViewport] = useState({ width: 800, height: 500 })
  const [viewBox, setViewBox] = useState(() => canvasViewBox(documentFeature, parameters)), [preview, setPreview] = useState(null), [error, setError] = useState('')
  const [selectedEdge, setSelectedEdge] = useState(null), [secondPoint, setSecondPoint] = useState(''), [dimension, setDimension] = useState(null), [parameterDraft, setParameterDraft] = useState({})
  const [drawOrigin, setDrawOrigin] = useState(null)
  const [coordinates, setCoordinates] = useState(() => (feature.start || ['', '']).map(display))
  const [shape, setShape] = useState('rectangle'), [shapeValues, setShapeValues] = useState({ x: '0', y: '0', width: '', height: '', radius: '',rx:'',ry:'',rotation:'0' })
  const [edgeValues, setEdgeValues] = useState({ type: 'line', x: '', y: '', throughX: '', throughY: '' })
  const [originValues, setOriginValues] = useState(() => (feature.origin || [0, 0, 0]).map(display))
  const svgRef = useRef(null), dragRef = useRef(null)
  const shown = preview?.feature?sketchContourFeature(preview.feature,effectiveContour):feature, shownParameters = preview?.parameters || parameters, points = sketchPoints(shown), resolvedPoints = sketchPoints(shown, shownParameters), current = points.find(point => point.key === selected) || points[0]
  const geometry = sketchGeometry(shown, shownParameters), axes = sketchAxes(feature.plane), coordinateKey = JSON.stringify(current?.value), originKey = JSON.stringify(feature.origin)
  const parameterKey = JSON.stringify(parameters), parameterRef = useRef(parameterKey)
  useEffect(() => {
    const next = syncSketchHistory(historyRef.current, documentFeature, parameters)
    if (next !== historyRef.current) { historyRef.current = next; setHistory(next) }
  }, [documentFeature, parameterKey])
  useEffect(() => { setCoordinates((current?.value || ['', '']).map(display)) }, [selected, coordinateKey])
  useEffect(() => { setOriginValues((feature.origin || [0, 0, 0]).map(display)) }, [originKey])
  useEffect(() => { if (selected && !points.some(point => point.key === selected)) setSelected(null) }, [selected, feature.segments.length])
  useEffect(() => { cancelPoint(); setDimension(null); if (selectedEdge !== null && !feature.segments[selectedEdge]) setSelectedEdge(null); if (!points.some(point => point.key === secondPoint)) setSecondPoint('') }, [feature])
  useEffect(() => { setParameterDraft({}); if (parameterRef.current !== parameterKey) { parameterRef.current = parameterKey; cancelPoint(); setViewBox(canvasViewBox(documentFeature, parameters)); setDimension(null) } }, [parameterKey])
  useEffect(() => { if (disabled||!active) { dragRef.current = null; setPreview(null); setArcThrough(null); setDrawOrigin(null);setSplinePoints([]);setEllipseMajor(null);setTool('select') } }, [disabled,active])
  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const measure = () => { const bounds = svg.getBoundingClientRect(); const width = svg.clientWidth || bounds.width, height = svg.clientHeight || bounds.height; if (width > 0 && height > 0) setViewport(old => old.width === width && old.height === height ? old : { width, height }) }
    measure()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    observer?.observe(svg); globalThis.addEventListener?.('resize', measure)
    return () => { observer?.disconnect(); globalThis.removeEventListener?.('resize', measure) }
  }, [toolbarHost, variant])
  useEffect(() => {
    const svg = svgRef.current
    if (!svg?.addEventListener) return
    const wheel = event => { event.preventDefault(); const anchor = pointFromEvent(event); zoom(Math.exp(Math.max(-1, Math.min(1, event.deltaY * .002))), anchor) }
    svg.addEventListener('wheel', wheel, { passive: false })
    return () => svg.removeEventListener('wheel', wheel)
  }, [viewBox])
  useEffect(() => {
    if (!active) return
    // Capture only non-text undo keys while this sketch is active. The ribbon
    // is portalled outside the canvas, and the parent also owns undo shortcuts.
    const key = event => historyKeyboard(event)
    globalThis.window?.addEventListener('keydown', key, true)
    return () => globalThis.window?.removeEventListener('keydown', key, true)
  }, [active, disabled, feature, parameterKey])

  const publishHistory = next => {
    if (next === historyRef.current) return
    const previous = historyRef.current, value = sketchHistoryValue(next)
    historyRef.current = next; setHistory(next)
    try {
      onChange(value.feature, value.parameters)
      if (onParametersChange && JSON.stringify(value.parameters) !== parameterKey) onParametersChange(value.parameters)
    } catch (reason) { historyRef.current = previous; setHistory(previous); throw reason }
  }
  const travelHistory = redo => {
    if (disabled || !active) return
    cancelPoint(); setArcThrough(null); setDrawOrigin(null); setDimension(null); setParameterDraft({}); setEdgeSelection([]);setPointSelection([]);setSplinePoints([]);setEllipseMajor(null);setError('')
    const next = travelSketchHistory(historyRef.current, redo)
    if (next === historyRef.current) return
    try { publishHistory(next); setViewBox(canvasViewBox(next.present.feature, next.present.parameters)) }
    catch (reason) { setError(reason.message || '无法恢复草图修改。') }
  }
  function historyKeyboard(event) {
    if (!active || disabled || event.defaultPrevented || event.altKey || !(event.metaKey || event.ctrlKey) || event.key?.toLowerCase() !== 'z'
      || ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target?.tagName) || event.target?.isContentEditable) return false
    event.preventDefault(); event.stopPropagation?.(); travelHistory(Boolean(event.shiftKey)); return true
  }

  const perform = operation => {
    if (disabled||!active) return
    try {
      const result = operation(); setError('')
      if (result) {
        const next = result.feature || replaceSketchContour(documentFeature,effectiveContour,result,{replaceConstraints:result.sketchConstraints!==feature.sketchConstraints}), nextParameters = result.parameters || parameters
        if(!result.feature){if(result.plane!==feature.plane)next.plane=result.plane;if(result.origin!==feature.origin)next.origin=result.origin}
        if (next.sketchConstraints?.length) validateSketchConstraints(next, nextParameters)
        publishHistory(recordSketchHistory(historyRef.current, next, nextParameters))
      }
    }
    catch (reason) { setError(reason.message || '草图修改失败，请检查坐标。') }
  }
  const pointFromEvent = event => {
    const svg = svgRef.current, bounds = svg?.getBoundingClientRect()
    if (!bounds) return null
    // SVG borders belong to its CSS box, outside the drawable viewport.
    const viewport = { left: bounds.left + (svg.clientLeft || 0), top: bounds.top + (svg.clientTop || 0), width: typeof svg.clientWidth === 'number' ? svg.clientWidth : bounds.width, height: typeof svg.clientHeight === 'number' ? svg.clientHeight : bounds.height }
    return screenToSketch(event.clientX, event.clientY, viewport, viewBox)
  }
  const selectTool = value => { cancelPoint(); setTool(value); setArcThrough(null); setDrawOrigin(null); setDimension(null); setSplinePoints([]);setEllipseMajor(null);setError('') }
  const clearSelection = () => { cancelPoint(); setSelected(null); setSelectedEdge(null); setSecondPoint(''); setDimension(null);setEdgeSelection([]);setPointSelection([]) }
  const selectContour=id=>{clearSelection();setContourId(id);setTool('select');setArcThrough(null);setDrawOrigin(null);setSplinePoints([]);setEllipseMajor(null);setError('')}
  const newContour=role=>perform(()=>{const current=sketchContour(documentFeature,effectiveContour),created=addSketchContour(documentFeature,{role,parent:current.role==='hole'?current.parent:effectiveContour});selectContour(created.id);return{feature:created.feature,parameters}})
  const publishShape=(type,values,mode=drawMode)=>{
    const shape=replaceSketchShape(feature,type,values)
    let next=documentFeature,id=effectiveContour
    if(mode==='append'&&feature.segments.length){const created=addSketchContour(next,{role:'outer'});next=created.feature;id=created.id}
    next=replaceSketchContour(next,id,shape,{replaceConstraints:true})
    selectContour(id);setSelected('start');setViewBox(canvasViewBox(next,parameters))
    return{feature:next,parameters}
  }
  const finishSpline=()=>perform(()=>{
    const values=splinePoints
    if(values.length<3)throw new Error('样条至少需要三个插值点。')
    const local=feature.segments.length?feature:{...feature,start:values[0]}
    const next=appendSketchSegment(local,{type:'spline',through:values.slice(1,-1),to:values.at(-1)})
    setSplinePoints([]);setTool('select');setSelected(`${next.segments.length-1}:to`)
    return next
  })
  const capture = event => { try { svgRef.current?.setPointerCapture?.(event.pointerId) } catch { /* Detached or synthetic pointer. */ } }
  const release = event => { try { svgRef.current?.releasePointerCapture?.(event.pointerId) } catch { /* Pointer may already have ended. */ } }
  const zoom = (factor, anchor = null) => {
    cancelPoint()
    setViewBox(old => { const bounded = Math.max(.001 / old[2], Math.min(1e7 / old[2], factor)), x = anchor?.[0] ?? old[0] + old[2] / 2, y = anchor ? -anchor[1] : old[1] + old[3] / 2; return [x + (old[0] - x) * bounded, y + (old[1] - y) * bounded, old[2] * bounded, old[3] * bounded] })
  }
  function startPoint(event, point) {
    event.stopPropagation()
    if (disabled || !active || (event.button !== undefined && event.button !== 0)) return
    if (tool !== 'select') { draw(event); return }
    if ((event.shiftKey||event.ctrlKey||event.metaKey) && selected) { setSecondPoint(point.key);setPointSelection(old=>[...(old.length?old:[pointRef(selected)]),pointRef(point.key)].slice(-2)); return }
    setPointSelection([pointRef(point.key)]);setSecondPoint('');setEdgeSelection([])
    setSelected(point.key); setSelectedEdge(null); setError('')
    if (!numericSketchPoint(point.value)) return
    dragRef.current = { key: point.key, feature, parameters: parameterKey, pointerId: event.pointerId, moved: false, point: point.value }
    capture(event)
  }
  function movePoint(event) {
    const drag = dragRef.current
    if (disabled || !active || !drag || drag.pointerId !== event.pointerId) return
    if (drag.pan) {
      const bounds = svgRef.current?.getBoundingClientRect(), scale = bounds && Math.min(bounds.width / drag.viewBox[2], bounds.height / drag.viewBox[3])
      if (scale > 0) setViewBox([drag.viewBox[0] - (event.clientX - drag.clientX) / scale, drag.viewBox[1] - (event.clientY - drag.clientY) / scale, ...drag.viewBox.slice(2)])
      return
    }
    if (feature !== drag.feature || parameterKey !== drag.parameters) { dragRef.current = null; setPreview(null); return }
    const point = pointFromEvent(event)
    if (!point) return
    if (sameSketchPoint(drag.point, point)) { drag.moved = false; setPreview(null); setError(''); return }
    try { const result = drag.edge !== undefined ? moveLinkedSketchEdge(documentFeature, edgeRef(drag.edge), point.map((value, axis) => value - drag.point[axis]), parameters) : moveLinkedSketchPoint(documentFeature, pointRef(drag.key), point, parameters); setPreview(result); drag.moved = true; drag.current = point; setError('') }
    catch (reason) { drag.moved = false; setPreview(null); setError(reason.message) }
  }
  function finishPoint(event) {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    dragRef.current = null; setPreview(null); release(event)
    if (!disabled && active && drag.moved && feature === drag.feature && parameterKey === drag.parameters) perform(() => drag.edge !== undefined ? moveLinkedSketchEdge(documentFeature, edgeRef(drag.edge), drag.current.map((value, axis) => value - drag.point[axis]), parameters) : moveLinkedSketchPoint(documentFeature, pointRef(drag.key), drag.current, parameters))
  }
  function cancelPoint() { dragRef.current = null; setPreview(null) }
  function draw(event) {
    if (disabled || !active || (event.button !== undefined && event.button !== 0)) return
    if (tool === 'select') { clearSelection(); return }
    if (tool === 'pan') { dragRef.current = { pan: true, pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY, viewBox: [...viewBox] }; capture(event); return }
    const point = pointFromEvent(event)
    if (!point) return
    if(tool==='spline'){
      setSplinePoints(old=>old.length?[...old,point]:feature.segments.length?[feature.segments.at(-1).to,point]:[point]);return
    }
    if(tool==='ellipse'){
      if(!arcThrough){setArcThrough(point);return}
      if(!ellipseMajor){if(sameSketchPoint(arcThrough,point)){setError('长轴半径必须大于零。');return}setEllipseMajor(point);return}
      perform(()=>{const dx=ellipseMajor[0]-arcThrough[0],dy=ellipseMajor[1]-arcThrough[1],rx=Math.hypot(dx,dy),ry=Math.abs((point[0]-arcThrough[0])*(-dy/rx)+(point[1]-arcThrough[1])*(dx/rx));return publishShape('ellipse',{x:arcThrough[0],y:arcThrough[1],rx,ry,rotation:Math.atan2(dy,dx)*180/Math.PI})});return
    }
    if (tool === 'rectangle' || tool === 'circle') {
      if (!arcThrough) { setArcThrough(point); return }
      perform(() => { const values = tool === 'rectangle' ? { x: Math.min(arcThrough[0], point[0]), y: Math.min(arcThrough[1], point[1]), width: Math.abs(point[0] - arcThrough[0]), height: Math.abs(point[1] - arcThrough[1]) } : { x: arcThrough[0], y: arcThrough[1], radius: Math.hypot(point[0] - arcThrough[0], point[1] - arcThrough[1]) };return publishShape(tool,values) })
      return
    }
    if (!feature.segments.length && !drawOrigin) { setDrawOrigin(point); setError(''); return }
    if (tool === 'arc' && !arcThrough) { setArcThrough(point); setError(''); return }
    perform(() => {
      const next = appendSketchSegment(feature.segments.length ? feature : { ...feature, start: drawOrigin }, tool === 'arc' ? { type: 'arc', through: arcThrough, to: point } : { type: 'line', to: point })
      setSelected(`${next.segments.length - 1}:to`); setArcThrough(null); setDrawOrigin(null)
      return next
    })
  }
  function keyPoint(event, point) {
    if (disabled||!active) return
    if (['Enter', ' '].includes(event.key)) { event.preventDefault();if((event.shiftKey||event.ctrlKey||event.metaKey)&&selected){setSecondPoint(point.key);setPointSelection(old=>[...(old.length?old:[pointRef(selected)]),pointRef(point.key)].slice(-2))}else{setPointSelection([pointRef(point.key)]);setSecondPoint('');setEdgeSelection([]);setSelected(point.key);setSelectedEdge(null)}return }
    const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[event.key]
    if (!direction) return
    event.preventDefault(); setSelected(point.key); setSelectedEdge(null)
    perform(() => {
      if (!numericSketchPoint(point.value)) throw new Error('表达式坐标不能拖动，请用坐标输入明确修改。')
      return moveLinkedSketchPoint(documentFeature, pointRef(point.key), point.value.map((value, i) => value + direction[i] * (event.shiftKey ? 10 : 1)), parameters)
    })
  }
  const step = 10 ** Math.floor(Math.log10(Math.max(viewBox[2], viewBox[3]) / 8))
  const gridX = [], gridY = []
  for (let x = Math.ceil(viewBox[0] / step) * step; x < viewBox[0] + viewBox[2]; x += step) gridX.push(x)
  for (let y = Math.ceil(viewBox[1] / step) * step; y < viewBox[1] + viewBox[3]; y += step) gridY.push(y)
  const pixelUnit = 1 / Math.min(viewport.width / viewBox[2], viewport.height / viewBox[3])
  const lastKey = `${feature.segments.length - 1}:to`, markerSize = 3 * pixelUnit
  const selectedSegment = selected && selected !== 'start' ? feature.segments[Number(selected.split(':')[0])] : null
  const chooseEdge = (index,additive=false) => {setPointSelection([]);setSecondPoint('');const ref=edgeRef(index);setEdgeSelection(old=>additive?(old.some(item=>JSON.stringify(item)===JSON.stringify(ref))?old.filter(item=>JSON.stringify(item)!==JSON.stringify(ref)):[...old,ref].slice(-2)):[ref]);setSelectedEdge(index); setSelected(`${index}:to`); setDimension(null);setError('') }
  const startEdge = (event, edge) => {
    event.stopPropagation(); if (disabled || !active || (event.button !== undefined && event.button !== 0)) return
    if (tool !== 'select') { draw(event); return }
    chooseEdge(edge,event.shiftKey||event.ctrlKey||event.metaKey); const point = pointFromEvent(event)
    if (point) { dragRef.current = { edge, feature, parameters: parameterKey, pointerId: event.pointerId, moved: false, point }; capture(event) }
  }
  const editDimension = index => { if (disabled||!active) return; try { const measurement = sketchEdgeMeasurement(documentFeature, edgeRef(index), parameters); chooseEdge(index); setDimension({ index, value: String(Number(measurement.value.toPrecision(10))), type: measurement.type }) } catch (reason) { setError(reason.message) } }
  const selectOtherEdge=(event,id,index)=>{event.stopPropagation();if(disabled||!active)return;if(tool!=='select'){draw(event);return}const ref=sketchEdgeRef(id,index),add=event.shiftKey||event.ctrlKey||event.metaKey,previous=edgeSelection;selectContour(id);setEdgeSelection(add?[...previous,ref].slice(-2):[ref]);setSelectedEdge(index);setSelected(`${index}:to`)}
  const selectOtherPoint=(event,id,key)=>{event.stopPropagation();if(disabled||!active)return;if(tool!=='select'){draw(event);return}const ref=sketchPointRef(id,key),add=event.shiftKey||event.ctrlKey||event.metaKey,previous=pointSelection.length?pointSelection:selected?[pointRef(selected)]:[];selectContour(id);setPointSelection(add?[...previous,ref].slice(-2):[ref]);setSelected(key)}
  const selectedKeys = selectedEdge === null ? [selected] : [selectedEdge === 0 ? 'start' : `${selectedEdge - 1}:to`, `${selectedEdge}:to`, ...(feature.segments[selectedEdge]?.type === 'arc' ? [`${selectedEdge}:through`] : [])]
  const relatedParameters = new Set()
  try { for (const point of points.filter(p => selectedKeys.includes(p.key))) for (const scalar of point.value) sketchParameterLeaves(scalar, parameters, relatedParameters) } catch { /* Unresolved parameters are explained next to the canvas. */ }
  const selectedConstraints = (documentFeature.sketchConstraints || []).filter(c => {const scope=c.contourId||'main';return [...(c.edges||[]),...(c.edge!==undefined?[c.edge]:[])].some(ref=>{const value=edgeAddress(ref,scope);return edgeSelection.some(selected=>JSON.stringify(edgeAddress(selected))===JSON.stringify(value))||(value.contourId===effectiveContour&&value.edge===selectedEdge)})||c.points?.some(ref=>{const value=pointAddress(ref,scope);return value.contourId===effectiveContour&&selectedKeys.includes(value.point)})})
  const applyConstraint = type => perform(() => addSketchConstraint(documentFeature, type, type==='coincident'&&pointSelection.length===2?{points:pointSelection}:{...(effectiveContour==='main'?{}:{contourId:effectiveContour}),...(['horizontal', 'vertical'].includes(type) ? { edge: selectedEdge } : { points: type === 'fixed' ? [selected] : [selected, secondPoint] })}, parameters))
  const applyRelationship=()=>perform(()=>addSketchConstraint(documentFeature,constraintType,{...(constraintType==='radius'?{edge:edgeSelection[0]}:{edges:edgeSelection}),...(['angle','radius'].includes(constraintType)?{value:constraintValue.trim()&&Number.isFinite(Number(constraintValue))?Number(constraintValue):constraintValue}:{})},parameters))
  const removeLocal=operation=>{if((documentFeature.sketchConstraints||[]).some(c=>constraintContours(c).includes(effectiveContour)))throw new Error('删除会改变约束引用，请先移除当前轮廓的相关草图约束。');const next=operation({...feature,sketchConstraints:[]});return{feature:replaceSketchContour(documentFeature,effectiveContour,next),parameters}}
  const deleteSelection = () => { if (selected === null && selectedEdge === null) return; perform(() => { const result=removeLocal(local=>selectedEdge!==null?removeSketchSegment(local,selectedEdge):removeSketchPoint(local,selected));clearSelection();return result }) }
  const onKeyboard = event => {
    if (historyKeyboard(event)) return
    if (event.key === 'Escape') { event.stopPropagation?.(); selectTool('select'); clearSelection(); return }
    if (disabled || ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target?.tagName) || event.target?.isContentEditable) return
    if (['Delete', 'Backspace'].includes(event.key) && selected !== null) { event.preventDefault(); event.stopPropagation?.(); deleteSelection() }
  }
  const toolbar = <div className={`fsk-toolbar${toolbarHost ? ' fsk-toolbar--portal' : ''}`} role="group" aria-label="草图工具">
      <button type="button" aria-label="撤销草图" title="撤销草图 (Ctrl / ⌘ Z)" disabled={disabled || !active || !history.past.length} onClick={() => travelHistory(false)}>撤销</button>
      <button type="button" aria-label="重做草图" title="重做草图 (Ctrl / ⌘ Shift Z)" disabled={disabled || !active || !history.future.length} onClick={() => travelHistory(true)}>重做</button>
      <span className="fsk-ribbon-divider" />
      {[['select', '选择 / 拖动'], ['line', '画直线'], ['rectangle', '画矩形'], ['circle', '画圆'], ['ellipse','画椭圆'],['spline','画样条'],['arc', '画三点圆弧'], ['pan', '平移']].map(([value, title]) => <button type="button" key={value} aria-pressed={tool === value} disabled={disabled || (['line', 'arc','spline'].includes(value) && feature.segments.length >= 128)} onClick={() => selectTool(value)}>{title}</button>)}
      {tool==='spline'&&<button disabled={disabled||splinePoints.length<3} onClick={finishSpline}>完成样条</button>}
      <label>图形方式<select aria-label="新图形加入方式" value={drawMode} disabled={disabled} onChange={event=>setDrawMode(event.target.value)}><option value="append">新增轮廓</option><option value="replace">替换当前轮廓</option></select></label>
      <span className="fsk-ribbon-divider" />
      <button type="button" disabled={disabled || selectedEdge === null || feature.segments[selectedEdge]?.type !== 'line'} onClick={() => applyConstraint('horizontal')}>水平</button>
      <button type="button" disabled={disabled || selectedEdge === null || feature.segments[selectedEdge]?.type !== 'line'} onClick={() => applyConstraint('vertical')}>垂直</button>
      <button type="button" disabled={disabled || !selected || selectedEdge !== null} onClick={() => applyConstraint('fixed')}>固定点</button>
      <button type="button" disabled={disabled || !(pointSelection.length===2||secondPoint&&selected!==secondPoint)} onClick={() => applyConstraint('coincident')}>重合</button>
      <button type="button" disabled={disabled || selectedEdge === null} onClick={() => editDimension(selectedEdge)}>尺寸</button>
      <label>关系<select aria-label="几何约束类型" value={constraintType} disabled={disabled} onChange={event=>setConstraintType(event.target.value)}>{['parallel','perpendicular','equal','tangent','angle','concentric','radius'].map(type=><option key={type} value={type}>{constraintName(type)}</option>)}</select></label>
      {['angle','radius'].includes(constraintType)&&<input aria-label="约束驱动值" placeholder={constraintType==='angle'?'角度°':'半径mm'} value={constraintValue} disabled={disabled} onChange={event=>setConstraintValue(event.target.value)}/>}
      <button disabled={disabled||edgeSelection.length!==(constraintType==='radius'?1:2)} onClick={applyRelationship}>应用关系 ({edgeSelection.length})</button>
      <button type="button" disabled={disabled || !selected || (feature.segments.length <= 2 && !selected?.includes(':through'))} onClick={deleteSelection}>删除所选</button>
      <button type="button" onClick={() => { cancelPoint(); setViewBox(canvasViewBox(documentFeature, parameters)) }}>适合草图</button>
      <button type="button" aria-label="放大草图" title="放大草图" onClick={() => zoom(.8)}>＋</button><button type="button" aria-label="缩小草图" title="缩小草图" onClick={() => zoom(1.25)}>−</button>
      <label>平面<select aria-label="草图平面" value={feature.plane} disabled={disabled || feature.plane === 'custom'} onChange={event => perform(() => ({ ...feature, plane: event.target.value }))}>{(feature.plane === 'custom' ? ['custom'] : ['XY', 'XZ', 'YZ']).map(plane => <option key={plane} value={plane}>{plane === 'custom' ? '所选模型面 · U/V' : plane}</option>)}</select></label>
    </div>
  return <section className={`feature-sketch feature-sketch--${variant}`} aria-label="图形草图编辑器" onKeyDown={onKeyboard}>
    {toolbarHost ? createPortal(toolbar, toolbarHost) : toolbar}
    <div className="fsk-contour-bar" role="group" aria-label="轮廓管理"><label>当前轮廓<select aria-label="当前草图轮廓" value={effectiveContour} disabled={disabled} onChange={event=>selectContour(event.target.value)}>{sketchContourList(documentFeature).map((item,i)=><option key={item.id} value={item.id}>{i+1} · {item.role==='hole'?'孔洞':'外轮廓'}{item.role==='hole'?` → ${item.parent}`:''}{item.segments.length?'':'（未绘制）'}</option>)}</select></label><button disabled={disabled} onClick={()=>newContour('outer')}>新增外轮廓</button><button disabled={disabled} onClick={()=>newContour('hole')}>新增孔洞</button><button disabled={disabled||effectiveContour==='main'} onClick={()=>perform(()=>{const next=deleteSketchContour(documentFeature,effectiveContour);selectContour('main');return{feature:next,parameters}})}>删除轮廓</button>{effectiveContour!=='main'&&<><label>用途<select aria-label="当前轮廓用途" value={sketchContour(documentFeature,effectiveContour).role} disabled={disabled} onChange={event=>perform(()=>({feature:setSketchContourRole(documentFeature,effectiveContour,event.target.value),parameters}))}><option value="outer">外轮廓</option><option value="hole">孔洞</option></select></label>{sketchContour(documentFeature,effectiveContour).role==='hole'&&<label>所属外轮廓<select aria-label="孔洞所属外轮廓" value={sketchContour(documentFeature,effectiveContour).parent} disabled={disabled} onChange={event=>perform(()=>({feature:setSketchContourRole(documentFeature,effectiveContour,'hole',event.target.value),parameters}))}>{sketchContourList(documentFeature).filter(item=>item.role==='outer').map(item=><option key={item.id} value={item.id}>{item.id==='main'?'主外轮廓':item.id}</option>)}</select></label>}</>}<span>Shift / Ctrl 点选两条边可设置关系；孔洞须位于所属外轮廓内部。</span></div>
    <div className="fsk-workspace"><div className="fsk-view">
    <svg ref={svgRef} className={`fsk-canvas ${tool === 'pan' ? 'fsk-canvas--pan' : tool !== 'select' ? 'fsk-canvas--draw' : ''}`} viewBox={viewBox.join(' ')} preserveAspectRatio="xMidYMid meet" role="group" aria-label={`${feature.plane} 平面草图，局部坐标，单位毫米`} onPointerDown={draw} onPointerMove={movePoint} onPointerUp={finishPoint} onPointerCancel={cancelPoint} onLostPointerCapture={cancelPoint}>
      <title>草图局部 {axes.join(' / ')} 坐标；原点 {pointText(feature.frame?.origin || feature.origin || [0, 0, 0])} mm</title>
      {gridX.map(x => <line key={`x${x}`} className={Math.abs(x) < 1e-8 ? 'fsk-axis' : 'fsk-grid'} x1={x} y1={viewBox[1]} x2={x} y2={viewBox[1] + viewBox[3]} />)}
      {gridY.map(y => <line key={`y${y}`} className={Math.abs(y) < 1e-8 ? 'fsk-axis' : 'fsk-grid'} x1={viewBox[0]} y1={y} x2={viewBox[0] + viewBox[2]} y2={y} />)}
      <g transform="scale(1 -1)">
        {sketchContourList(preview?.feature||documentFeature).filter(item=>item.id!==effectiveContour).flatMap(contour=>sketchGeometry(contour,shownParameters).edges.map(edge=><path key={`${contour.id}:${edge.index}`} className={`fsk-edge fsk-edge--other${contour.role==='hole'?' fsk-edge--hole':''}${edgeSelection.some(ref=>{const value=edgeAddress(ref);return value.contourId===contour.id&&value.edge===edge.index})?' fsk-edge--selected':''}`} d={edge.d} role="button" aria-label={`选择轮廓 ${contour.id} 边 ${edge.index+1}`} tabIndex={disabled?-1:0} onPointerDown={event=>selectOtherEdge(event,contour.id,edge.index)} onKeyDown={event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();selectOtherEdge(event,contour.id,edge.index)}}}/>))}
        {sketchContourList(documentFeature).filter(item=>item.id!==effectiveContour).flatMap(contour=>sketchPoints(contour,shownParameters).filter(point=>numericSketchPoint(point.value)&&!(point.key===`${contour.segments.length-1}:to`&&sameSketchPoint(point.value,sketchPoints(contour,shownParameters)[0].value))).map(point=><circle key={`other-point-${contour.id}:${point.key}`} className={`fsk-point fsk-point--other${pointSelection.some(ref=>{const value=pointAddress(ref);return value.contourId===contour.id&&value.point===point.key})?' fsk-point--selected':''}`} cx={point.value[0]} cy={point.value[1]} r={markerSize} role="button" tabIndex={disabled?-1:0} aria-label={`选择轮廓 ${contour.id} ${point.label}`} onPointerDown={event=>selectOtherPoint(event,contour.id,point.key)} onKeyDown={event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();selectOtherPoint(event,contour.id,point.key)}}}/>))}
        {geometry.closingPath && <path className="fsk-closing" d={geometry.closingPath}><title>重建时自动补出的闭合直线</title></path>}
        {geometry.edges.map(edge => <path key={edge.index} role="button" aria-label={`选择草图边 ${edge.index + 1}`} tabIndex={disabled ? -1 : 0} onPointerDown={event => startEdge(event, edge.index)} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); if (!disabled) chooseEdge(edge.index,event.shiftKey||event.ctrlKey||event.metaKey) } }} className={`fsk-edge${edge.invalid ? ' fsk-edge--invalid' : ''}${selectedEdge === edge.index ? ' fsk-edge--selected' : ''}`} d={edge.d} />)}
        {arcThrough && <circle className="fsk-pending" cx={arcThrough[0]} cy={arcThrough[1]} r={markerSize} />}
        {drawOrigin && <circle className="fsk-pending" cx={drawOrigin[0]} cy={drawOrigin[1]} r={markerSize} />}
        {splinePoints.map((point,index)=><circle key={`spline-pending-${index}`} className="fsk-pending" cx={point[0]} cy={point[1]} r={markerSize}/>)}
        {ellipseMajor&&<line className="fsk-closing" x1={arcThrough[0]} y1={arcThrough[1]} x2={ellipseMajor[0]} y2={ellipseMajor[1]}/>}
        {resolvedPoints.filter(point => feature.segments.length && numericSketchPoint(point.value) && !(point.key === lastKey && geometry.closed)).map(point => <circle key={point.key} className={`fsk-point${point.key === selected || point.key === secondPoint || (point.key === 'start' && selected === lastKey && geometry.closed) ? ' fsk-point--selected' : ''}${point.key.includes(':through') ? ' fsk-point--through' : ''}`} cx={point.value[0]} cy={point.value[1]} r={markerSize} tabIndex={disabled ? -1 : 0} role="button" aria-label={`${point.label} (${pointText(point.value)})`} aria-disabled={disabled} onPointerDown={event => startPoint(event, point)} onKeyDown={event => keyPoint(event, point)}><title>{point.label}：{pointText(point.value)} mm</title></circle>)}
      </g>
      {geometry.edges.filter((edge, index, all) => !edge.invalid&&!edge.ellipse&&!edge.spline && edge.start && (!edge.arc || !all.slice(0, index).some(other => other.arc && sameSketchPoint(other.arc.center, edge.arc.center) && Math.abs(other.arc.radius - edge.arc.radius) < 1e-8))).map(edge => {
        const dx = edge.end[0] - edge.start[0], dy = edge.end[1] - edge.start[1], length = Math.hypot(dx, dy), offset = 22 * pixelUnit
        const anchor = edge.arc ? edge.arc.center : [(edge.start[0] + edge.end[0]) / 2, (edge.start[1] + edge.end[1]) / 2]
        const pos = edge.arc ? [edge.arc.center[0] + edge.arc.radius * .72, edge.arc.center[1] + edge.arc.radius * .72] : [anchor[0] - dy / (length || 1) * offset, anchor[1] + dx / (length || 1) * offset]
        const ends = edge.arc ? [edge.arc.center, pos] : [edge.start, edge.end].map(p => [p[0] - dy / (length || 1) * offset, p[1] + dx / (length || 1) * offset])
        const label = `${edge.arc ? 'R ' : ''}${Number((edge.arc?.radius ?? length).toFixed(3))}`
        const hitWidth = Math.max(32, label.length * 7.8 + 12) * pixelUnit
        const arrow = (tip, other) => { const angle = Math.atan2(other[1] - tip[1], other[0] - tip[0]), size = 5 * pixelUnit; return `M ${tip[0] + Math.cos(angle - .35) * size} ${-tip[1] - Math.sin(angle - .35) * size} L ${tip[0]} ${-tip[1]} L ${tip[0] + Math.cos(angle + .35) * size} ${-tip[1] - Math.sin(angle + .35) * size}` }
        return <g key={`dimension-${edge.index}`} className="fsk-dimension" role="button" aria-label={`编辑边 ${edge.index + 1} ${edge.arc ? '半径' : '长度'}`} tabIndex={disabled ? -1 : 0} onPointerDown={event => event.stopPropagation()} onClick={() => { if (!disabled) chooseEdge(edge.index,event.shiftKey||event.ctrlKey||event.metaKey) }} onDoubleClick={() => editDimension(edge.index)} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); editDimension(edge.index) } }}>
          {!edge.arc && [edge.start, edge.end].map((p, i) => <line key={i} x1={p[0]} y1={-p[1]} x2={ends[i][0]} y2={-ends[i][1]} />)}
          <line x1={ends[0][0]} y1={-ends[0][1]} x2={ends[1][0]} y2={-ends[1][1]} />
          <path className="fsk-dimension-arrow" d={`${arrow(ends[0], ends[1])} ${arrow(ends[1], ends[0])}`} />
          <rect className="fsk-dimension-hit" x={pos[0] - hitWidth / 2} y={-pos[1] - 18 * pixelUnit} width={hitWidth} height={28 * pixelUnit} fill="transparent" />
          <text x={pos[0]} y={-pos[1]} textAnchor="middle" fontSize={12 * pixelUnit} style={{ strokeWidth: 3 * pixelUnit }}>{label}</text>
        </g>
      })}
      {geometry.edges.filter(edge=>edge.ellipse).flatMap(edge=>[0,1].map(axis=>{
        const ellipse=edge.ellipse,end=ellipsePoint(ellipse,axis?90:0),center=ellipse.center,label=`${axis?'b':'a'} ${Number(ellipse.radii[axis].toFixed(3))}`
        return <g key={`ellipse-dimension-${edge.index}-${axis}`} className="fsk-dimension" role="button" aria-label={`编辑边 ${edge.index+1} ${axis?'短轴':'长轴'}半径`} tabIndex={disabled?-1:0} onPointerDown={event=>event.stopPropagation()} onDoubleClick={()=>{if(disabled||!active)return;chooseEdge(edge.index);setDimension({index:edge.index,type:'ellipseRadius',axis,value:String(ellipse.radii[axis])})}} onKeyDown={event=>{if(!disabled&&['Enter',' '].includes(event.key)){event.preventDefault();chooseEdge(edge.index);setDimension({index:edge.index,type:'ellipseRadius',axis,value:String(ellipse.radii[axis])})}}}><line x1={center[0]} y1={-center[1]} x2={end[0]} y2={-end[1]}/><text x={end[0]+8*pixelUnit} y={-end[1]-8*pixelUnit} fontSize={12*pixelUnit}>{label}</text></g>
      }))}
    </svg>
    <div className="fsk-caption"><span>局部 {axes[0]} → · {axes[1]} ↑ · 网格 {Number(step.toPrecision(5))} mm</span><span className="fsk-tool-hint">{tool === 'select' ? '点选或拖动 · 双击尺寸 · Shift 多选点' : tool === 'pan' ? '拖动画布平移 · 滚轮缩放' : tool === 'line' ? !feature.segments.length && !drawOrigin ? '点击起点，再点击直线终点' : '点击追加直线终点' : tool==='ellipse'?(ellipseMajor?'点击短轴方向上的点确定短轴半径':arcThrough?'点击长轴端点确定方向和长度':'点击椭圆中心'):tool==='spline'?`点击插值点（已选${splinePoints.length}点），然后完成样条；Esc取消`:['rectangle', 'circle'].includes(tool) ? `${tool === 'rectangle' ? '点击两个对角点' : '点击圆心和圆周点'}，${drawMode==='append'?'新增轮廓':'替换当前轮廓'}；Esc 取消` : arcThrough ? '已选圆弧经过点，再点击圆弧终点。' : '点击圆弧经过点，再点击终点'}{arcThrough && <button type="button" disabled={disabled} onClick={() => setArcThrough(null)}>取消</button>}</span><span role="status">{!feature.segments.length ? '空草图 · 选择工具后点击画布开始绘制' : geometry.closed ? '首尾已闭合' : geometry.closureKnown ? '虚线为自动闭合边' : '存在未定义的参数'}</span></div>
    {error && <p role="alert" className="fsk-error">{error}</p>}
    {geometry.warnings.length > 0 && <p className="fsk-warning">{geometry.warnings.join(' ')} <button type="button" onClick={() => { setSelected(resolvedPoints.find(point => !numericSketchPoint(point.value))?.key || 'start'); setSelectedEdge(null) }}>定位未解析点</button></p>}
    </div>{selected !== null && <aside className="fsk-properties" aria-label="所选草图属性">
    <div className="fsk-properties-heading"><strong>{selectedEdge === null ? current.label : `边 ${selectedEdge + 1} · ${({arc:'圆弧',ellipse:'椭圆',spline:'样条',line:'直线'})[feature.segments[selectedEdge]?.type]||'曲线'}`}</strong><button type="button" aria-label="关闭草图属性" onClick={clearSelection}>×</button></div>
    {dimension && <form className="fsk-dimension-form" onSubmit={event => { event.preventDefault(); perform(() => { const value = dimension.value.trim(), scalar = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value) ? Number(value) : value; const result = dimension.type==='ellipseRadius'?driveSketchEllipseRadius(documentFeature,edgeRef(dimension.index),dimension.axis||0,scalar,parameters):driveSketchDimension(documentFeature, edgeRef(dimension.index), scalar, parameters); setDimension(null); return result }) }}>
      <label>{dimension.type==='ellipseRadius'?(dimension.axis?'短轴半径':'长轴半径'):dimension.type === 'radius' ? '半径' : '长度'} (mm)<input autoFocus aria-label="草图驱动尺寸" value={dimension.value} disabled={disabled} onChange={event => setDimension(old => ({ ...old, value: event.target.value }))} /></label><button type="submit" disabled={disabled}>应用尺寸</button>
    </form>}
    {[...relatedParameters].map(name => <div className="fsk-parameter" key={name}><label>{name}<input aria-label={`草图关联参数 ${name}`} value={parameterDraft[name] ?? String(evaluateSketchExpression(name, parameters))} disabled={disabled} onChange={event => setParameterDraft(old => ({ ...old, [name]: event.target.value }))} /></label><button type="button" disabled={disabled} aria-label={`应用草图参数 ${name}`} onClick={() => perform(() => changeLinkedSketchParameter(documentFeature, parameters, name, parameterDraft[name] ?? evaluateSketchExpression(name, parameters)))}>应用</button></div>)}
    {relatedParameters.size > 0 && <p className="fsk-hint">几何随关联参数变化，原表达式保持不变。</p>}
    <div className="fsk-constraints">{selectedConstraints.length ? selectedConstraints.map(c => <div key={c.id}><span>{constraintName(c.type)}{c.value!==undefined&&<><input aria-label={`约束 ${c.id} 驱动值`} value={constraintDrafts[c.id]??String(c.value)} disabled={disabled} onChange={event=>setConstraintDrafts(old=>({...old,[c.id]:event.target.value}))}/><button disabled={disabled} aria-label={`更新约束 ${c.id}`} onClick={()=>perform(()=>{const raw=constraintDrafts[c.id]??String(c.value),value=raw.trim()&&Number.isFinite(Number(raw))?Number(raw):raw;return changeSketchConstraintValue(documentFeature,c.id,value,parameters)})}>更新</button></>}</span><button type="button" disabled={disabled} aria-label={`移除草图约束 ${c.id}`} onClick={() => perform(() => ({ feature:{...documentFeature,sketchConstraints:documentFeature.sketchConstraints.filter(item=>item.id!==c.id)},parameters }))}>移除</button></div>) : <p className="fsk-hint">{selectedEdge === null ? '选择另一点时按住 Shift 可添加重合。' : '按 Shift / Ctrl 选择另一条边，设置跨轮廓几何关系。'}</p>}</div>
    <details className="fsk-details fsk-coordinate-details"><summary>精确坐标与表达式</summary><div className="fsk-point-form">
      <label>所选点<select aria-label="选择草图控制点" value={current.key} disabled={disabled} onChange={event => { setSelected(event.target.value); setError('') }}>{points.map(point => <option key={point.key} value={point.key}>{point.label}{!numericSketchPoint(point.value) ? ' · 表达式' : ''}</option>)}</select></label>
      {axes.map((axis, index) => <label key={axis}>{axis} (mm)<input aria-label={`所选点 ${axis} 坐标`} inputMode="decimal" disabled={disabled} value={coordinates[index] ?? ''} onChange={event => setCoordinates(old => old.map((value, i) => i === index ? event.target.value : value))} /></label>)}
      <button type="button" disabled={disabled} onClick={() => perform(() => {
        if (coordinates.some(value => !Number.isFinite(Number(value))) && numericSketchPoint(resolvedPoints.find(p => p.key === current.key)?.value)) return setSketchPointExpressions(documentFeature, pointRef(current.key), coordinates.map(value => Number.isFinite(Number(value)) && value.trim() ? Number(value) : value.trim()), parameters)
        const target = coordinates.map((value, index) => sketchNumber(value, `${axes[index]} 坐标`))
        if (!numericSketchPoint(resolvedPoints.find(p => p.key === current.key)?.value)) return updateSketchPoint(feature, current.key, target, { explicit: true })
        return moveLinkedSketchPoint(documentFeature, pointRef(current.key), target, parameters)
      })}>应用坐标</button>
    </div>
    {!numericSketchPoint(current.value) && <p className="fsk-hint">表达式：{pointText(current.value)}。输入数值会驱动关联参数；输入新的表达式会明确更换当前点关联。缺失参数时，明确输入两个数值会替换当前点。</p>}
    {selectedSegment?.radius !== undefined && <p className="fsk-hint">此圆弧保留半径校核 {String(selectedSegment.radius)} mm；修改控制点后须重建检查。</p>}
    </details>
    <details className="fsk-details"><summary>替换为矩形 / 圆 / 椭圆</summary><p className="fsk-hint">以下操作替换当前轮廓，保留特征名称、平面、原点与拉伸 / 旋转设置。</p><div className="fsk-form">
      <label>形状<select aria-label="新建草图形状" disabled={disabled} value={shape} onChange={event => setShape(event.target.value)}><option value="rectangle">矩形</option><option value="circle">圆</option><option value="ellipse">椭圆</option></select></label>
      {[['x', `${shape === 'circle' ? '圆心' : shape==='ellipse'?'中心':'左下角'} ${axes[0]}`], ['y', `${shape === 'circle' ? '圆心' : shape==='ellipse'?'中心':'左下角'} ${axes[1]}`], ...(shape === 'rectangle' ? [['width', '宽度'], ['height', '高度']] : shape==='ellipse'?[['rx','长轴半径'],['ry','短轴半径'],['rotation','方向角度']]:[['radius', '半径']])].map(([key, title]) => <label key={key}>{title} ({key==='rotation'?'°':'mm'})<input aria-label={`新轮廓 ${title}`} inputMode="decimal" value={shapeValues[key]} disabled={disabled} onChange={event => setShapeValues(old => ({ ...old, [key]: event.target.value }))} /></label>)}
      <button type="button" disabled={disabled} onClick={() => perform(() => { return publishShape(shape,shapeValues,'replace') })}>用{shape === 'rectangle' ? '矩形' : shape==='ellipse'?'椭圆':'圆'}替换轮廓</button>
    </div></details>
    <details className="fsk-details"><summary>精确添加边与删除边（{feature.segments.length} / 128）</summary><div className="fsk-form">
      <label>新边类型<select aria-label="精确新边类型" disabled={disabled} value={edgeValues.type} onChange={event => setEdgeValues(old => ({ ...old, type: event.target.value }))}><option value="line">直线</option><option value="arc">三点圆弧</option></select></label>
      {[['x', `终点 ${axes[0]}`], ['y', `终点 ${axes[1]}`], ...(edgeValues.type === 'arc' ? [['throughX', `经过点 ${axes[0]}`], ['throughY', `经过点 ${axes[1]}`]] : [])].map(([key, title]) => <label key={key}>{title}<input aria-label={`新边 ${title}`} inputMode="decimal" value={edgeValues[key]} disabled={disabled} onChange={event => setEdgeValues(old => ({ ...old, [key]: event.target.value }))} /></label>)}
      <button type="button" disabled={disabled || feature.segments.length >= 128} onClick={() => perform(() => { const next = appendSketchSegment(feature, { type: edgeValues.type, to: [sketchNumber(edgeValues.x), sketchNumber(edgeValues.y)], ...(edgeValues.type === 'arc' ? { through: [sketchNumber(edgeValues.throughX), sketchNumber(edgeValues.throughY)] } : {}) }); setSelected(`${next.segments.length - 1}:to`); setArcThrough(null); return next })}>添加这条边</button>
    </div><div className="fsk-edge-list">{feature.segments.map((segment, index) => <div key={index}><button type="button" disabled={disabled} onClick={() => setSelected(`${index}:to`)}>边 {index + 1} · {({arc:'圆弧',line:'直线',ellipse:'椭圆',spline:'样条'})[segment.type]} · ({pointText(segment.to)})</button><button type="button" aria-label={`删除草图边 ${index + 1}`} disabled={disabled || feature.segments.length <= 2} onClick={() => perform(() => { const result=removeLocal(local=>removeSketchSegment(local,index));setSelected('start');setArcThrough(null);return result })}>删除</button></div>)}</div><p className="fsk-hint">删除后会从前一终点连接下一条边；轮廓至少保留 2 条边，末端始终自动闭合。重建会检查轮廓是否有效。</p></details>
    {feature.plane !== 'custom' && <details className="fsk-details"><summary>草图原点：({pointText(feature.origin || [0, 0, 0])}) mm</summary><div className="fsk-form">{['X', 'Y', 'Z'].map((axis, index) => <label key={axis}>全局 {axis}<input aria-label={`草图原点 ${axis}`} inputMode="decimal" value={originValues[index]} disabled={disabled} onChange={event => setOriginValues(old => old.map((value, i) => i === index ? event.target.value : value))} /></label>)}<button type="button" disabled={disabled} onClick={() => perform(() => ({ ...feature, origin: originValues.map((value, i) => sketchNumber(value, `原点 ${'XYZ'[i]}`)) }))}>应用原点</button></div></details>}
    </aside>}</div>
  </section>
}

export default function FeatureSketchEditor(props) {
  if (!props.feature || !['profile_extrude', 'profile_revolve','profile_sweep'].includes(props.feature.op)) return null
  return <FeatureSketchEditorBody key={`${props.feature.id}:${props.feature.op}`} {...props} />
}
