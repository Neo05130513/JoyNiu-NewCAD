import { sketchContour, sketchContourList } from './featureSketchContours.js'
import { sketchArc } from './featureSketchModel.js'
import { ellipsePoint, ellipseTangent, sketchSplineBeziers } from './featureSketchCurves.js'

export function pointKey(reference,contourId='main') {
  if(reference&&typeof reference==='object')return pointKey(reference.point,reference.contourId)
  if(typeof reference!=='string')throw new Error('约束点引用无效。')
  if(reference.includes('/'))return reference
  return contourId==='main'?reference:`${contourId}/${reference}`
}
export function pointAddress(reference,contourId='main') {
  const key=pointKey(reference,contourId),split=key.indexOf('/')
  return split<0?{contourId:'main',point:key}:{contourId:key.slice(0,split),point:key.slice(split+1)}
}
export function sketchPointAt(feature,reference,contourId='main') {
  const address=pointAddress(reference,contourId),contour=sketchContour(feature,address.contourId)
  if(address.point==='start')return contour.start
  const [edge,field,index]=address.point.split(':'),segment=contour.segments[Number(edge)]
  return index===undefined?segment?.[field]:segment?.[field]?.[Number(index)]
}
export function edgeAddress(reference,contourId='main') {
  if(Number.isInteger(reference))return {contourId,edge:reference}
  if(!reference||typeof reference!=='object'||!Number.isInteger(reference.edge)||typeof reference.contourId!=='string')throw new Error('约束边引用无效。')
  return {contourId:reference.contourId,edge:reference.edge}
}
export function sketchEdgeKeys(reference,contourId='main') {
  const ref=edgeAddress(reference,contourId)
  return [pointKey(ref.edge===0?'start':`${ref.edge-1}:to`,ref.contourId),pointKey(`${ref.edge}:to`,ref.contourId)]
}
export function sketchCurveKeys(feature,reference,contourId='main') {
  const ref=edgeAddress(reference,contourId),segment=sketchContour(feature,ref.contourId).segments[ref.edge]
  if(!segment)throw new Error('约束引用的边已不存在。')
  return [...sketchEdgeKeys(ref),...(segment.type==='arc'?[pointKey(`${ref.edge}:through`,ref.contourId)]:segment.type==='spline'?segment.through.map((_,i)=>pointKey(`${ref.edge}:through:${i}`,ref.contourId)):segment.type==='ellipse'?[pointKey(`${ref.edge}:center`,ref.contourId),pointKey(`${ref.edge}:radii`,ref.contourId)]:[])]
}
export function sketchConstraintKeys(feature,constraint) {
  const contourId=constraint.contourId||'main'
  if(constraint.edges)return [...new Set(constraint.edges.flatMap(ref=>sketchCurveKeys(feature,ref,contourId)))]
  if(constraint.edge!==undefined)return ['horizontal','vertical','length'].includes(constraint.type)?sketchEdgeKeys(constraint.edge,contourId):sketchCurveKeys(feature,constraint.edge,contourId)
  return (constraint.points||[]).map(ref=>pointKey(ref,contourId))
}
export function sketchCurve(feature,reference,contourId='main') {
  const ref=edgeAddress(reference,contourId),segment=sketchContour(feature,ref.contourId).segments[ref.edge],keys=sketchEdgeKeys(ref),[start,end]=keys.map(key=>sketchPointAt(feature,key))
  if(!segment||!start||!end)throw new Error('约束引用的边已不存在。')
  const line=end.map((v,i)=>v-start[i]),base={...ref,segment,start,end,length:Math.hypot(...line),tangents:[line,line]}
  if(segment.type==='arc'){
    const circle=sketchArc(start,segment.through,end)
    if(!circle)throw new Error('圆弧三点不能共线或重合。')
    return {...base,circle,tangents:[start,end].map(p=>[-(p[1]-circle.center[1]),p[0]-circle.center[0]])}
  }
  if(segment.type==='ellipse')return {...base,ellipse:segment,...(Math.abs(segment.radii[0]-segment.radii[1])<1e-7?{circle:{center:segment.center,radius:segment.radii[0]}}:{}),tangents:[ellipseTangent(segment,segment.startAngle),ellipseTangent(segment,segment.endAngle)]}
  if(segment.type==='spline'){
    const curves=sketchSplineBeziers([start,...segment.through,end])
    return {...base,spline:curves,tangents:[curves[0].c1.map((v,i)=>v-start[i]),end.map((v,i)=>v-curves.at(-1).c2[i])]}
  }
  return base
}
export function sketchClosurePairs(feature) {
  return sketchContourList(feature).filter(contour=>contour.segments.length&&JSON.stringify(contour.start)===JSON.stringify(contour.segments.at(-1).to)).map(contour=>[pointKey('start',contour.id),pointKey(`${contour.segments.length-1}:to`,contour.id)])
}
export function implicitSketchResiduals(feature) {
  const residuals=[]
  for(const contour of sketchContourList(feature))for(const [index,segment]of contour.segments.entries())if(segment.type==='ellipse'){
    if(segment.radii.some(value=>!Number.isFinite(value)||value<=0))throw new Error('椭圆半径必须大于零。')
    const actual=[index?contour.segments[index-1].to:contour.start,segment.to],expected=[ellipsePoint(segment,segment.startAngle),ellipsePoint(segment,segment.endAngle)]
    for(let j=0;j<2;j++)for(let axis=0;axis<2;axis++)residuals.push(actual[j][axis]-expected[j][axis])
  }
  return residuals
}
