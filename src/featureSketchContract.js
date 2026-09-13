// Structural recovery check only: unknown parameter values remain editable.
// Geometry, expressions, ownership and dimensional constraints are checked by
// the solver/kernel when the user rebuilds.
export function sketchContractIssue(feature) {
  const plain=value=>value!==null&&typeof value==='object'&&!Array.isArray(value)
  const scalar=value=>typeof value==='number'?Number.isFinite(value)&&Math.abs(value)<=1e6:typeof value==='string'&&value.trim()&&value.length<=256
  const point=value=>Array.isArray(value)&&value.length===2&&value.every(scalar)
  const identifier=/^[A-Za-z][A-Za-z0-9_]{0,63}$/
  const exact=(value,required,optional=[])=>plain(value)&&required.every(key=>Object.hasOwn(value,key))&&Object.keys(value).every(key=>required.includes(key)||optional.includes(key))
  if(!plain(feature)||!point(feature.start)||!Array.isArray(feature.segments))return '草图主轮廓定义不完整。'
  if(feature.contours!==undefined&&(!Array.isArray(feature.contours)||feature.contours.length>32))return '附加轮廓最多支持32个。'
  const contours=[{id:'main',role:'outer',start:feature.start,segments:feature.segments},...(feature.contours||[])],ids=new Set()
  let edgeCount=0
  for(const contour of contours){
    if(!exact(contour,['id','role','start','segments'],['parent'])||!identifier.test(contour.id)||ids.has(contour.id)||!['outer','hole'].includes(contour.role)||!point(contour.start)||!Array.isArray(contour.segments)||contour.segments.length<1||contour.segments.length>128)return '轮廓编号、类型或几何定义不完整。'
    ids.add(contour.id);edgeCount+=contour.segments.length
    if(contour.role==='hole'&&(typeof contour.parent!=='string'||contour.parent===contour.id||!contours.some(item=>item.id===contour.parent&&item.role==='outer')))return '孔洞必须明确属于一个存在的外轮廓。'
    if(contour.role==='outer'&&contour.parent!==undefined)return '外轮廓不能指定孔洞归属。'
    for(const segment of contour.segments){
      if(!plain(segment)||!point(segment.to))return '草图边终点定义不完整。'
      if(segment.type==='line'){if(!exact(segment,['type','to']))return '直线字段无效。'}
      else if(segment.type==='arc'){if(!exact(segment,['type','through','to'],['radius'])||!point(segment.through)||(segment.radius!==undefined&&!scalar(segment.radius)))return '圆弧定义不完整。'}
      else if(segment.type==='spline'){if(!exact(segment,['type','through','to'])||!Array.isArray(segment.through)||segment.through.length<1||segment.through.length>62||!segment.through.every(point))return '样条插值点定义不完整。'}
      else if(segment.type==='ellipse'){if(!exact(segment,['type','center','radii','rotation','startAngle','endAngle','to'])||!point(segment.center)||!point(segment.radii)||!['rotation','startAngle','endAngle'].every(key=>scalar(segment[key]))||segment.radii.some(value=>typeof value==='number'&&value<=0))return '椭圆定义不完整。'}
      else return '草图包含未知曲线类型。'
    }
    if(contour.segments.length===1&&contour.segments[0].type!=='ellipse'&&contour.segments[0].type!=='spline')return '草图尚未形成可重建区域。'
  }
  if(edgeCount>512)return '草图总边数超过512。'
  const constraints=feature.sketchConstraints||[]
  if(!Array.isArray(constraints)||constraints.length>128)return '草图约束列表无效。'
  const fields={horizontal:['edge'],vertical:['edge'],length:['edge','value'],fixed:['points','position'],coincident:['points'],radius:['edge','value'],parallel:['edges'],perpendicular:['edges'],equal:['edges'],tangent:['edges'],concentric:['edges'],angle:['edges','value']},constraintIds=new Set(),identities=new Set()
  const edgeRef=(value,fallback)=>{
    const ref=Number.isInteger(value)?{contourId:fallback,edge:value}:value
    if(!exact(ref,['contourId','edge'])||!ids.has(ref.contourId)||!Number.isInteger(ref.edge)||ref.edge<0||!contours.find(item=>item.id===ref.contourId).segments[ref.edge])throw new Error('草图约束引用了不存在的边。')
    return {contourId:ref.contourId,edge:ref.edge}
  }
  const pointRef=(value,fallback)=>{
    const ref=typeof value==='string'?{contourId:fallback,point:value}:value
    if(!exact(ref,['contourId','point'])||!ids.has(ref.contourId)||typeof ref.point!=='string')throw new Error('草图约束点引用无效。')
    const contour=contours.find(item=>item.id===ref.contourId),points=new Set(['start'])
    contour.segments.forEach((segment,i)=>{points.add(`${i}:to`);if(segment.type==='arc')points.add(`${i}:through`);if(segment.type==='spline')segment.through.forEach((_,j)=>points.add(`${i}:through:${j}`));if(segment.type==='ellipse')points.add(`${i}:center`)})
    if(!points.has(ref.point))throw new Error('草图约束引用了不存在的控制点。')
    return ref
  }
  try{for(const constraint of constraints){
    if(!plain(constraint)||!Object.hasOwn(fields,constraint.type)||!exact(constraint,['id','type',...fields[constraint.type]],['contourId']))return '草图约束类型或字段无效。'
    if(typeof constraint.id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(constraint.id)||constraintIds.has(constraint.id))return '草图约束 ID 无效或重复。'
    constraintIds.add(constraint.id)
    const fallback=constraint.contourId||'main';if(!ids.has(fallback))return '草图约束所属轮廓不存在。'
    let refs,identity
    if(constraint.edges){if(!Array.isArray(constraint.edges)||constraint.edges.length!==2)return '此约束需要两条边。';refs=constraint.edges.map(value=>edgeRef(value,fallback));if(JSON.stringify(refs[0])===JSON.stringify(refs[1]))return '此约束需要两条不同的边。';identity=[...refs].map(value=>JSON.stringify(value)).sort()}
    else if(constraint.edge!==undefined){refs=[edgeRef(constraint.edge,fallback)];identity=refs}
    else{if(!Array.isArray(constraint.points)||constraint.points.length!==(constraint.type==='fixed'?1:2))return '草图约束点数量无效。';refs=constraint.points.map(value=>pointRef(value,fallback));identity=refs.map(value=>JSON.stringify(value)).sort();if(new Set(identity).size!==identity.length)return '草图约束点不能重复。'}
    const signature=JSON.stringify([constraint.type,identity]);if(identities.has(signature))return '草图约束重复。';identities.add(signature)
    if(['horizontal','vertical','length','parallel','perpendicular','angle'].includes(constraint.type)&&refs.some(ref=>contours.find(item=>item.id===ref.contourId).segments[ref.edge].type!=='line'))return '此约束需要直线边。'
    if(constraint.value!==undefined&&!scalar(constraint.value))return '约束驱动值无效。'
    if(constraint.position!==undefined&&!point(constraint.position))return '固定点坐标无效。'
  }}catch(error){return error.message}
  return ''
}
