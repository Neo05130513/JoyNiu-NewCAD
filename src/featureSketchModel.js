import { resolveSketchFeature } from './featureSketchExpressions.js'
import { ellipsePoint, ellipseSvgPath, sketchSplineBeziers, splineSvgPath } from './featureSketchCurves.js'
import { sketchContourList, validateSketchContourStructure } from './featureSketchContours.js'
const finite = value => typeof value === 'number' && Number.isFinite(value)
export const numericSketchPoint = point => Array.isArray(point) && point.length === 2 && point.every(finite)
export const sameSketchPoint = (a, b) => numericSketchPoint(a) && numericSketchPoint(b) && Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-8
export const sketchAxes = plane => ({ XY: ['X', 'Y'], XZ: ['X', 'Z'], YZ: ['Y', 'Z'] })[plane] || ['U', 'V']

// Use only when creating a known rectangle. Existing freeform contours must
// never acquire rectangular constraints merely because their points look close.
export function rectangleSketchConstraints(start) {
  return [...['horizontal', 'vertical', 'horizontal', 'vertical'].map((type, edge) => ({ id: `sketch_rect_${edge}`, type, edge })),
    { id: 'sketch_rect_origin', type: 'fixed', points: ['start'], position: [...start] }]
}

export function sketchPoints(feature, parameters) {
  if (parameters !== undefined) feature = resolveSketchFeature(feature, parameters).feature
  const points = [{ key: 'start', label: '起点', value: feature.start }]
  for (const [index, segment] of (feature.segments || []).entries()) {
    if (segment.type === 'arc') points.push({ key: `${index}:through`, label: `边 ${index + 1} 圆弧经过点`, value: segment.through })
    if (segment.type === 'spline') for(const [j,point]of segment.through.entries())points.push({key:`${index}:through:${j}`,label:`边 ${index+1} 样条插值点 ${j+1}`,value:point})
    if (segment.type === 'ellipse') points.push({key:`${index}:center`,label:`边 ${index+1} 椭圆中心`,value:segment.center})
    points.push({ key: `${index}:to`, label: `边 ${index + 1} 终点`, value: segment.to })
  }
  return points
}

export function sketchNumber(value, label = '坐标') {
  const text = String(value ?? '').trim()
  if (!text || !Number.isFinite(Number(text)) || Math.abs(Number(text)) > 1000000) throw new Error(`${label}请输入 -1000000 至 1000000 之间的数值；表达式须在此明确改为数值。`)
  return Number(text)
}

// Strings are expressions, even when they happen to look numeric. Pointer
// operations must never replace them; only the explicit coordinate form can.
export function updateSketchPoint(feature, key, point, { explicit = false } = {}) {
  if (!numericSketchPoint(point) || point.some(value => Math.abs(value) > 1000000)) throw new Error('点坐标超出可编辑范围。')
  const source = sketchPoints(feature).find(item => item.key === key)
  if (!source) throw new Error('所选点已不存在，请重新选择。')
  if (!explicit && !numericSketchPoint(source.value)) throw new Error('表达式坐标不能拖动，请用坐标输入明确修改。')
  const segments = (feature.segments || []).map(segment => ({ ...segment }))
  const last = segments.length - 1
  const numericClosure = sameSketchPoint(feature.start, segments[last]?.to)
  if (key === 'start') {
    if (numericClosure) segments[last].to = [...point]
    return { ...feature, start: [...point], segments }
  }
  const [index, field,subindex] = key.split(':')
  if(subindex!==undefined){segments[Number(index)][field]=segments[Number(index)][field].map((item,i)=>i===Number(subindex)?[...point]:item)}
  else segments[Number(index)][field] = [...point]
  // Coincident numeric start/end points are one closed-contour vertex.
  return { ...feature, ...(numericClosure && Number(index) === last && field === 'to' ? { start: [...point] } : {}), segments }
}

export function appendSketchSegment(feature, segment) {
  if ((feature.segments || []).length >= 128) throw new Error('单个轮廓最多支持 128 条边。')
  if (!['line', 'arc','spline'].includes(segment.type) || !numericSketchPoint(segment.to) || (segment.type === 'arc' && !numericSketchPoint(segment.through)) || (segment.type==='spline'&&(!Array.isArray(segment.through)||segment.through.length<1||segment.through.length>62||!segment.through.every(numericSketchPoint)))) throw new Error('请完整输入新边的数值坐标。')
  if ([segment.to, ...(segment.type === 'arc' ? [segment.through] : segment.type==='spline'?segment.through:[])].some(point => point.some(value => Math.abs(value) > 1000000))) throw new Error('新边坐标超出可编辑范围。')
  const previous = feature.segments?.at(-1)?.to || feature.start
  if (sameSketchPoint(previous, segment.to)&&segment.type!=='spline') throw new Error('新边终点不能与上一终点重合。')
  if (segment.type === 'arc' && numericSketchPoint(previous) && !sketchArc(previous, segment.through, segment.to)) throw new Error('圆弧三点不能重合或共线。')
  if(segment.type==='spline')sketchSplineBeziers([previous,...segment.through,segment.to])
  return { ...feature, segments: [...(feature.segments || []), structuredClone(segment)] }
}

export function removeSketchSegment(feature, index) {
  if (feature.segments.length <= 2) throw new Error('轮廓至少需要 2 条边；可以用矩形或圆重新绘制。')
  if (!Number.isInteger(index) || index < 0 || index >= feature.segments.length) throw new Error('所选边已不存在。')
  if (feature.sketchConstraints?.length) throw new Error('删除边会改变约束引用，请先移除相关草图约束。')
  return { ...feature, segments: feature.segments.filter((_, i) => i !== index) }
}

export function removeSketchPoint(feature, key) {
  if (feature.sketchConstraints?.length) throw new Error('删除点会改变约束引用，请先移除相关草图约束。')
  if (/^\d+:through:\d+$/.test(key)) {
    const [index,,control]=key.split(':').map((value,i)=>i===1?value:Number(value)),segment=feature.segments[index]
    if(segment?.type!=='spline'||!segment.through[control])throw new Error('所选样条插值点已不存在。')
    if(segment.through.length===1)throw new Error('样条至少保留一个中间插值点；如需直线，请明确删除整条样条。')
    return {...feature,segments:feature.segments.map((item,i)=>i===index?{...item,through:item.through.filter((_,j)=>j!==control)}:item)}
  }
  if(key.endsWith(':center'))throw new Error('椭圆中心用于定义曲线，不能单独删除；可删除整个轮廓。')
  if (key.endsWith(':through')) {
    const index = Number(key.split(':')[0])
    if (feature.segments[index]?.type !== 'arc') throw new Error('所选圆弧经过点已不存在。')
    return { ...feature, segments: feature.segments.map((segment, i) => i === index ? { type: 'line', to: [...segment.to] } : segment) }
  }
  const index = key === 'start' ? 0 : Number(key.split(':')[0]), next = removeSketchSegment(feature, index)
  return key === 'start' ? { ...next, start: [...feature.segments[0].to] } : next
}

export function replaceSketchShape(feature, type, values) {
  const x = sketchNumber(values.x, '位置 X'), y = sketchNumber(values.y, '位置 Y')
  const positive = (value, name) => { const number = sketchNumber(value, name); if (number <= 0) throw new Error(`${name}必须大于零。`); return number }
  let start, segments
  if (type === 'rectangle') {
    const width = positive(values.width, '宽度'), height = positive(values.height, '高度')
    start = [x, y]
    segments = [{ type: 'line', to: [x + width, y] }, { type: 'line', to: [x + width, y + height] }, { type: 'line', to: [x, y + height] }, { type: 'line', to: [...start] }]
  } else if (type === 'circle') {
    const radius = positive(values.radius, '半径')
    start = [x + radius, y]
    segments = [{ type: 'arc', through: [x, y + radius], to: [x - radius, y] }, { type: 'arc', through: [x, y - radius], to: [...start] }]
  } else if(type==='ellipse'){
    const rx=positive(values.rx,'长轴半径'),ry=positive(values.ry,'短轴半径'),rotation=sketchNumber(values.rotation??0,'椭圆方向')
    const segment={type:'ellipse',center:[x,y],radii:[rx,ry],rotation,startAngle:0,endAngle:360}
    start=ellipsePoint(segment,0);segments=[{...segment,to:[...start]}]
  } else throw new Error('请选择矩形、圆或椭圆。')
  if ([start, ...segments.flatMap(segment => [segment.to, ...(segment.through ? [segment.through] : [])])].some(point => point.some(value => Math.abs(value) > 1000000))) throw new Error('新轮廓坐标超出可编辑范围。')
  const next = { ...feature, start, segments }
  if (type === 'rectangle') next.sketchConstraints = rectangleSketchConstraints(start)
  else if(type==='circle')next.sketchConstraints=[{id:'sketch_circle_equal',type:'equal',edges:[0,1]},{id:'sketch_circle_concentric',type:'concentric',edges:[0,1]}]
  else if (next.sketchConstraints) next.sketchConstraints = []
  return next
}

export function sketchArc(start, through, end) {
  if (![start, through, end].every(numericSketchPoint)) return null
  // Solve in coordinates relative to start to avoid cancellation far from origin.
  const bx = through[0] - start[0], by = through[1] - start[1], cx = end[0] - start[0], cy = end[1] - start[1]
  const determinant = 2 * (bx * cy - by * cx)
  if (Math.abs(determinant) <= 1e-10 * Math.max(bx * bx + by * by, cx * cx + cy * cy, 1)) return null
  const ux = ((bx * bx + by * by) * cy - (cx * cx + cy * cy) * by) / determinant
  const uy = ((cx * cx + cy * cy) * bx - (bx * bx + by * by) * cx) / determinant
  const center = [start[0] + ux, start[1] + uy], radius = Math.hypot(ux, uy)
  const tau = Math.PI * 2, angle = point => Math.atan2(point[1] - center[1], point[0] - center[0]), positive = value => (value % tau + tau) % tau
  const from = angle(start), mid = positive(angle(through) - from), to = positive(angle(end) - from)
  const sweep = mid < to ? 1 : 0, span = sweep ? to : tau - to
  return { center, radius, from, span, sweep, largeArc: Number(span > Math.PI + 1e-10) }
}

export function sketchGeometry(feature, parameters) {
  const resolution = parameters !== undefined ? resolveSketchFeature(feature, parameters) : null
  if (resolution) feature = resolution.feature
  const edges = [], warnings = []
  let previous = feature.start
  for (const [index, segment] of (feature.segments || []).entries()) {
    if (!numericSketchPoint(previous) || !numericSketchPoint(segment.to) || (segment.type === 'arc' && !numericSketchPoint(segment.through))) warnings.push(`边 ${index + 1} 含表达式，暂不绘制该边。`)
    else if(segment.type==='ellipse'){
      if(!numericSketchPoint(segment.center)||!numericSketchPoint(segment.radii)||segment.radii.some(value=>value<=0)||![segment.rotation,segment.startAngle,segment.endAngle].every(finite))warnings.push(`边 ${index+1} 椭圆参数无效。`)
      else if(Math.abs(segment.endAngle-segment.startAngle)<=1e-7||Math.abs(segment.endAngle-segment.startAngle)>360+1e-7||Math.hypot(...ellipsePoint(segment,segment.startAngle).map((v,i)=>v-previous[i]))>1e-5||Math.hypot(...ellipsePoint(segment,segment.endAngle).map((v,i)=>v-segment.to[i]))>1e-5)warnings.push(`边 ${index+1} 椭圆起终点与参数不一致。`)
      else edges.push({index,start:previous,end:segment.to,ellipse:segment,d:ellipseSvgPath(segment)})
    }else if(segment.type==='spline'){
      try{const points=[previous,...segment.through,segment.to];edges.push({index,start:previous,end:segment.to,spline:sketchSplineBeziers(points),d:splineSvgPath(points)})}catch(error){warnings.push(`边 ${index+1}：${error.message}`)}
    }else if (segment.type === 'arc') {
      const arc = sketchArc(previous, segment.through, segment.to)
      if (arc) edges.push({ index, arc, start: previous, end: segment.to, d: `M ${previous.join(' ')} A ${arc.radius} ${arc.radius} 0 ${arc.largeArc} ${arc.sweep} ${segment.to.join(' ')}` })
      else { warnings.push(`边 ${index + 1} 的圆弧三点共线或重合。`); edges.push({ index, invalid: true, d: `M ${previous.join(' ')} L ${segment.through.join(' ')} L ${segment.to.join(' ')}` }) }
    } else edges.push({ index, start: previous, end: segment.to, d: `M ${previous.join(' ')} L ${segment.to.join(' ')}` })
    previous = segment.to
  }
  const closed = sameSketchPoint(feature.start, previous)
  const closingPath = !closed && numericSketchPoint(feature.start) && numericSketchPoint(previous) ? `M ${previous.join(' ')} L ${feature.start.join(' ')}` : ''
  return { edges, warnings: resolution?.errors.length ? resolution.errors : warnings, closed, closingPath, closureKnown: numericSketchPoint(feature.start) && numericSketchPoint(previous) }
}

export function sketchViewBox(feature, parameters) {
  if (parameters !== undefined) feature = resolveSketchFeature(feature, parameters).feature
  const points = sketchPoints(feature).map(item => item.value).filter(numericSketchPoint)
  for (const { arc } of sketchGeometry(feature).edges) if (arc) {
    // Include the actual arc's cardinal extrema, including major arcs.
    for (let i = 0; i < 4; i++) {
      const angle = i * Math.PI / 2, delta = ((arc.sweep ? angle - arc.from : arc.from - angle) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI)
      if (delta <= arc.span + 1e-8) points.push([arc.center[0] + arc.radius * Math.cos(angle), arc.center[1] + arc.radius * Math.sin(angle)])
    }
  }
  for(const edge of sketchGeometry(feature).edges){
    if(edge.ellipse)for(let i=0;i<72;i++)points.push(ellipsePoint(edge.ellipse,edge.ellipse.startAngle+(edge.ellipse.endAngle-edge.ellipse.startAngle)*i/71))
    if(edge.spline)for(const curve of edge.spline)points.push(curve.c1,curve.c2)
  }
  if (!points.length) return [-25, -25, 50, 50]
  const xs = points.map(point => point[0]), ys = points.map(point => point[1])
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys)
  const size = Math.max(maxX - minX, maxY - minY, 10), padding = size * .18
  return [minX - padding, -maxY - padding, Math.max(maxX - minX, size * .3) + padding * 2, Math.max(maxY - minY, size * .3) + padding * 2]
}

export function validateSketchContours(feature,parameters={},{allowEmpty=false}={}) {
  validateSketchContourStructure(feature)
  const resolution=resolveSketchFeature(feature,parameters)
  if(resolution.errors.length)throw new Error(resolution.errors[0])
  for(const contour of sketchContourList(resolution.feature)){
    const label=contour.id==='main'?'主外轮廓':`轮廓 ${contour.id}`
    if(!contour.segments.length){if(allowEmpty)continue;throw new Error(`${label}为空，请绘制闭合轮廓或删除未完成的轮廓。`)}
    const geometry=sketchGeometry(contour)
    if(geometry.warnings.length)throw new Error(`${label}：${geometry.warnings[0]}`)
    const single=contour.segments[0]
    if(contour.segments.length===1&&!(single.type==='ellipse'&&Math.abs(Math.abs(single.endAngle-single.startAngle)-360)<1e-8)&&!(single.type==='spline'&&geometry.closed))throw new Error(`${label}尚未形成闭合区域。`)
  }
  return true
}

export function screenToSketch(clientX, clientY, bounds, viewBox) {
  if (!bounds || ![clientX, clientY, bounds.left, bounds.top, bounds.width, bounds.height, ...viewBox].every(finite) || bounds.width <= 0 || bounds.height <= 0 || viewBox[2] <= 0 || viewBox[3] <= 0) return null
  const scale = Math.min(bounds.width / viewBox[2], bounds.height / viewBox[3])
  const offsetX = (bounds.width - viewBox[2] * scale) / 2, offsetY = (bounds.height - viewBox[3] * scale) / 2
  return [viewBox[0] + (clientX - bounds.left - offsetX) / scale, -(viewBox[1] + (clientY - bounds.top - offsetY) / scale)].map(value => Math.round(value * 1000) / 1000 || 0)
}
