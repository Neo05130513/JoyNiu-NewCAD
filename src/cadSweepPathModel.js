import {evaluateSketchExpression} from './featureSketchExpressions.js'
import {baseReferenceFrames,referenceFrames} from './cadEditorEntities.js'
import {cloneFeatureValue} from './directFeatureModel.js'

const norm=v=>Math.hypot(...v)
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
const sub=(a,b)=>a.map((v,i)=>v-b[i])
const unit=v=>{const n=norm(v);if(n<1e-9)throw new Error('方向不能为零。');return v.map(x=>x/n)}
export const sweepProjectionAxes=plane=>({XY:[0,1,2],XZ:[0,2,1],YZ:[1,2,0]})[plane]||[0,2,1]
export const projectSweepPoint=(point,plane)=>sweepProjectionAxes(plane).slice(0,2).map(axis=>point[axis])
export function sweepPointFromPlane(point,plane,offset=0){const axes=sweepProjectionAxes(plane),result=[0,0,0];result[axes[0]]=point[0];result[axes[1]]=point[1];result[axes[2]]=offset;return result}
export function resolveSweepPoint(point,parameters={}){if(!Array.isArray(point)||point.length!==3)throw new Error('路径点需要三个坐标。');return point.map(v=>evaluateSketchExpression(v,parameters))}
export function sweepPathPoints(path){return [{key:'start',value:path.start},...(path.segments||[]).flatMap((s,i)=>[...(s.type==='arc'?[{key:`${i}:through`,value:s.through}]:s.type==='spline'?s.through.map((value,j)=>({key:`${i}:through:${j}`,value})):s.type==='ellipse'?[{key:`${i}:center`,value:s.center}]:[]),{key:`${i}:to`,value:s.to}])]}
export function detachSweepPath(path){const next=cloneFeatureValue(path);delete next.sketchId;return next}
export function moveSweepPathPoint(path,key,point){
  if(!Array.isArray(point)||point.length!==3||point.some(v=>!Number.isFinite(v)||Math.abs(v)>1e6))throw new Error('路径点坐标无效。')
  const original=sweepPathPoints(path).find(p=>p.key===key)
  if(!original)throw new Error('路径点已不存在。')
  if(original.value.some(v=>typeof v!=='number'))throw new Error('此路径点由表达式驱动，请通过坐标或关联参数明确编辑。')
  const [edge,field]=key.split(':'),index=Number(edge)
  if(key==='start'&&path.segments?.[0]?.type==='ellipse'||field==='center'||field==='to'&&(path.segments?.[index]?.type==='ellipse'||path.segments?.[index+1]?.type==='ellipse'))throw new Error('椭圆端点由椭圆参数定义，请编辑关联草图或完整参数。')
  const next=detachSweepPath(path)
  if(key==='start')next.start=[...point]
  else{const [i,field,k]=key.split(':');if(k===undefined)next.segments[Number(i)][field]=[...point];else next.segments[Number(i)][field][Number(k)]=[...point]}
  return next
}
export function appendSweepPathSegment(path,segment,parameters={}){
  if((path.segments||[]).length>=128)throw new Error('扫掠路径最多128段。')
  if(!['line','arc','spline'].includes(segment.type))throw new Error('请选择直线、圆弧或样条。')
  const start=resolveSweepPoint(path.segments?.at(-1)?.to||path.start,parameters),to=resolveSweepPoint(segment.to,parameters)
  if(path.segments?.length&&norm(sub(start,resolveSweepPoint(path.start,parameters)))<1e-7)throw new Error('闭合路径不能继续追加；请清空路径后重新绘制，或选择另一张草图。')
  if(segment.type!=='spline'&&norm(sub(start,to))<1e-7)throw new Error('路径段终点不能与起点重合。')
  if(segment.type==='arc'){const through=resolveSweepPoint(segment.through,parameters);if(norm(cross(sub(through,start),sub(to,start)))<1e-7)throw new Error('圆弧三点不能共线或重合。')}
  if(segment.type==='spline'){
    if(!Array.isArray(segment.through)||segment.through.length<1||segment.through.length>62)throw new Error('样条需要1至62个中间插值点。')
    const points=[start,...segment.through.map(p=>resolveSweepPoint(p,parameters)),to];if(points.some((p,i)=>i&&norm(sub(p,points[i-1]))<1e-7))throw new Error('相邻样条插值点不能重合。')
  }
  return {...detachSweepPath(path),segments:[...(path.segments||[]),cloneFeatureValue(segment)]}
}
export function sweepSketchFrame(sketch,parameters={},references=[]){
  const frame=sketch.planeReference?referenceFrames({parameters,references})[sketch.planeReference]:sketch.frame||{...baseReferenceFrames[sketch.plane||'XY'],origin:sketch.origin||[0,0,0]}
  if(!frame)throw new Error('草图所引用的基准面不存在。')
  const xDir=unit(resolveSweepPoint(frame.xDir,parameters)),normal=unit(resolveSweepPoint(frame.normal,parameters))
  if(Math.abs(xDir.reduce((s,v,i)=>s+v*normal[i],0))>1e-6)throw new Error('草图坐标轴须与平面法线垂直。')
  return {origin:resolveSweepPoint(frame.origin,parameters),xDir,normal}
}
export function sweepPathFromSketch(sketch,parameters={},references=[]){
  if(!sketch?.segments?.length)throw new Error('请先在所选草图绘制路径。')
  const frame=sweepSketchFrame(sketch,parameters,references),y=cross(frame.normal,frame.xDir),world=p=>{if(!Array.isArray(p)||p.length!==2)throw new Error('草图路径坐标无效。');return frame.origin.map((v,i)=>v+frame.xDir[i]*evaluateSketchExpression(p[0],parameters)+y[i]*evaluateSketchExpression(p[1],parameters))}
  return {sketchId:sketch.id,start:world(sketch.start),segments:sketch.segments.map(segment=>({...cloneFeatureValue(segment),to:world(segment.to),...(segment.type==='arc'?{through:world(segment.through)}:segment.type==='spline'?{through:segment.through.map(world)}:segment.type==='ellipse'?{center:world(segment.center),xDir:frame.xDir,normal:frame.normal}:{} )}))}
}
export function sampleSweepSegment(start,segment,parameters={}){
  const a=resolveSweepPoint(start,parameters),b=resolveSweepPoint(segment.to,parameters)
  if(segment.type==='line')return[a,b]
  if(segment.type==='arc'){
    const through=resolveSweepPoint(segment.through,parameters),u=sub(through,a),v=sub(b,a),n=cross(u,v),nn=norm(n)**2
    if(nn<1e-14)throw new Error('圆弧三点共线。')
    const vn=cross(v,n),nu=cross(n,u),offset=vn.map((value,i)=>(value*norm(u)**2+nu[i]*norm(v)**2)/(2*nn)),center=a.map((x,i)=>x+offset[i]),x=unit(sub(a,center)),y=unit(cross(n,x)),radius=norm(offset),angle=p=>{const d=sub(p,center);return Math.atan2(d.reduce((s,v,i)=>s+v*y[i],0),d.reduce((s,v,i)=>s+v*x[i],0))},positive=t=>(t+2*Math.PI)%(2*Math.PI)
    const mid=positive(angle(through)),end=positive(angle(b)),span=mid<=end?end:end-2*Math.PI
    return Array.from({length:49},(_,i)=>center.map((v,j)=>v+radius*(x[j]*Math.cos(span*i/48)+y[j]*Math.sin(span*i/48))))
  }
  if(segment.type==='ellipse'){
    const center=resolveSweepPoint(segment.center,parameters),x=unit(resolveSweepPoint(segment.xDir,parameters)),n=unit(resolveSweepPoint(segment.normal,parameters)),y=cross(n,x),[rx,ry]=segment.radii.map(v=>evaluateSketchExpression(v,parameters)),rotation=evaluateSketchExpression(segment.rotation,parameters)*Math.PI/180,begin=evaluateSketchExpression(segment.startAngle,parameters),end=evaluateSketchExpression(segment.endAngle,parameters)
    if(rx<=0||ry<=0)throw new Error('椭圆半轴必须大于零。')
    return Array.from({length:73},(_,i)=>{const t=(begin+(end-begin)*i/72)*Math.PI/180,u=rx*Math.cos(t),v=ry*Math.sin(t);return center.map((c,j)=>c+x[j]*(u*Math.cos(rotation)-v*Math.sin(rotation))+y[j]*(u*Math.sin(rotation)+v*Math.cos(rotation)))})
  }
  if(segment.type==='spline'){
    const points=[a,...segment.through.map(p=>resolveSweepPoint(p,parameters)),b],h=points.slice(1).map((p,i)=>norm(sub(p,points[i])))
    if(h.some(v=>v<1e-7))throw new Error('相邻插值点不能重合。')
    const secants=h.map((d,i)=>sub(points[i+1],points[i]).map(v=>v/d)),derivatives=points.map((_,i)=>i===0?secants[0]:i===points.length-1?secants.at(-1):[0,1,2].map(axis=>(h[i]*secants[i-1][axis]+h[i-1]*secants[i][axis])/(h[i-1]+h[i])))
    if(norm(sub(a,b))<1e-8)derivatives[0]=derivatives[points.length-1]=[0,1,2].map(axis=>(h[0]*secants.at(-1)[axis]+h.at(-1)*secants[0][axis])/(h[0]+h.at(-1)))
    return h.flatMap((step,i)=>Array.from({length:25},(_,j)=>{const t=j/24;return [0,1,2].map(axis=>(2*t**3-3*t**2+1)*points[i][axis]+(t**3-2*t**2+t)*step*derivatives[i][axis]+(-2*t**3+3*t**2)*points[i+1][axis]+(t**3-t**2)*step*derivatives[i+1][axis])}))
  }
  throw new Error('不支持的路径类型。')
}
