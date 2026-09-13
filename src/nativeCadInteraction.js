import {entityPoints, transformEntity} from './nativeDrawingModel.js'

export function cadCoordinate(value, origin=[0,0]) {
  const s=value.trim(), relative=s.startsWith('@'), text=relative?s.slice(1):s
  const number='([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+))'
  let match=text.match(new RegExp(`^${number}\\s*,\\s*${number}$`)), point
  if(match)point=match.slice(1).map(Number)
  else if((match=text.match(new RegExp(`^${number}\\s*<\\s*${number}$`))))point=[Number(match[1])*Math.cos(Number(match[2])*Math.PI/180),Number(match[1])*Math.sin(Number(match[2])*Math.PI/180)]
  else return null
  if(relative)point=point.map((n,i)=>n+origin[i])
  if(point.some(n=>!Number.isFinite(n)||Math.abs(n)>1e7))throw new Error('坐标超出范围')
  return point
}

export function cadBoxSelection(entities, a, b, crossing=b[0]<a[0]) {
  const x=Math.min(a[0],b[0]),right=Math.max(a[0],b[0]),y=Math.min(a[1],b[1]),top=Math.max(a[1],b[1])
  return entities.filter(e=>{const pts=entityPoints(e);if(!pts.length)return false
    const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys)
    return crossing?maxX>=x&&minX<=right&&maxY>=y&&minY<=top:minX>=x&&maxX<=right&&minY>=y&&maxY<=top
  }).map(e=>e.id)
}

export function cadTransformValues(kind, a, b) {
  const translation=[b[0]-a[0],b[1]-a[1]],angle=Math.atan2(translation[1],translation[0])*180/Math.PI
  if(kind==='rotate')return {origin:a,rotation:angle}
  if(kind==='scale'){const scale=Math.hypot(...translation)/10;if(scale<=0)throw new Error('比例必须大于零');return {origin:a,scale}}
  return {translation,origin:a}
}

export function cadMirror(entity,a,b) {
  const angle=Math.atan2(b[1]-a[1],b[0]-a[0])*180/Math.PI
  if(Math.hypot(b[0]-a[0],b[1]-a[1])<1e-8)throw new Error('镜像轴需要两个不同点')
  return transformEntity(transformEntity(transformEntity(entity,{origin:a,rotation:-angle}),{origin:a,mirror:'x'}),{origin:a,rotation:angle})
}

export function cadCornerOperations(entities, radius, chamfer=false) {
  if(entities.length!==2||entities.some(e=>e.type!=='LINE'))throw new Error('请选择两条相交直线')
  if(!Number.isFinite(radius)||radius<=0)throw new Error('圆角半径或倒角距离必须大于零')
  const [a,b]=entities,u=[a.end[0]-a.start[0],a.end[1]-a.start[1]],v=[b.end[0]-b.start[0],b.end[1]-b.start[1]],cross=(u,v)=>u[0]*v[1]-u[1]*v[0],d=cross(u,v)
  if(Math.abs(d)<1e-9)throw new Error('平行或重合的直线不能建立此圆角')
  const t=cross([b.start[0]-a.start[0],b.start[1]-a.start[1]],v)/d,o=[a.start[0]+t*u[0],a.start[1]+t*u[1]]
  const ends=entities.map(e=>['start','end'].sort((k,l)=>Math.hypot(e[l][0]-o[0],e[l][1]-o[1])-Math.hypot(e[k][0]-o[0],e[k][1]-o[1]))[0])
  const lengths=entities.map((e,i)=>Math.hypot(e[ends[i]][0]-o[0],e[ends[i]][1]-o[1]))
  if(lengths.some(n=>n<1e-8))throw new Error('直线长度不足')
  const dirs=entities.map((e,i)=>[(e[ends[i]][0]-o[0])/lengths[i],(e[ends[i]][1]-o[1])/lengths[i]])
  const theta=Math.acos(Math.max(-1,Math.min(1,dirs[0][0]*dirs[1][0]+dirs[0][1]*dirs[1][1]))),distance=chamfer?radius:radius/Math.tan(theta/2)
  if(!Number.isFinite(distance)||lengths.some(n=>distance>=n))throw new Error('半径或距离超过所选直线的可用长度')
  const points=dirs.map(v=>[o[0]+v[0]*distance,o[1]+v[1]*distance,0])
  const operations=entities.map((e,i)=>({op:'update',id:e.id,changes:{[ends[i]==='start'?'end':'start']:points[i]}}))
  if(chamfer)return [...operations,{op:'add',entity:{type:'LINE',start:points[0],end:points[1],layer:a.layer}}]
  const sum=[dirs[0][0]+dirs[1][0],dirs[0][1]+dirs[1][1]],length=Math.hypot(...sum),center=sum.map((n,i)=>o[i]+n/length*radius/Math.sin(theta/2))
  let angles=points.map(p=>Math.atan2(p[1]-center[1],p[0]-center[0])*180/Math.PI)
  if((angles[1]-angles[0]+360)%360>180)angles.reverse()
  return [...operations,{op:'add',entity:{type:'ARC',center:[...center,0],radius,startAngle:angles[0],endAngle:angles[1],layer:a.layer}}]
}

export function cadTrimOperation(line, boundaries, pick) {
  if(line?.type!=='LINE')throw new Error('请选择需要修剪的直线')
  const a=line.start,u=[line.end[0]-a[0],line.end[1]-a[1]],square=u[0]**2+u[1]**2,cross=(u,v)=>u[0]*v[1]-u[1]*v[0]
  if(square<1e-12)throw new Error('零长度直线不能修剪')
  const cuts=[0,1]
  for(const other of boundaries.filter(e=>e.type==='LINE'&&e.id!==line.id)){
    const v=[other.end[0]-other.start[0],other.end[1]-other.start[1]],d=cross(u,v),w=[other.start[0]-a[0],other.start[1]-a[1]]
    if(Math.abs(d)<1e-9)continue
    const t=cross(w,v)/d,s=cross(w,u)/d
    if(t>1e-8&&t<1-1e-8&&s>=-1e-8&&s<=1+1e-8)cuts.push(t)
  }
  cuts.sort((a,b)=>a-b);if(cuts.length===2)throw new Error('没有相交边界可用于修剪')
  const at=Math.max(0,Math.min(1,((pick[0]-a[0])*u[0]+(pick[1]-a[1])*u[1])/square))
  const index=Math.max(0,Math.min(cuts.length-2,cuts.findIndex((v,i)=>i<cuts.length-1&&at<=cuts[i+1])))
  const operations=[]
  if(cuts[index]>1e-8)operations.push({op:'trim',id:line.id,start:0,end:cuts[index]})
  else operations.push({op:'delete',ids:[line.id]})
  if(cuts[index+1]<1-1e-8)operations.push({op:'add',entity:{...line,id:undefined,start:[a[0]+u[0]*cuts[index+1],a[1]+u[1]*cuts[index+1],0]}})
  return operations
}
