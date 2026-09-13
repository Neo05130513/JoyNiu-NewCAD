import { numericSketchPoint, sameSketchPoint, sketchArc, sketchPoints, updateSketchPoint } from './featureSketchModel.js'
import { evaluateSketchExpression, resolveSketchFeature, setSketchParameter, sketchParameterLeaves } from './featureSketchExpressions.js'
import { sketchContour, sketchContourList } from './featureSketchContours.js'
import { pointKey, pointAddress, edgeAddress, sketchPointAt, sketchEdgeKeys, sketchCurveKeys, sketchConstraintKeys, sketchCurve, sketchClosurePairs, implicitSketchResiduals } from './featureSketchConstraintGeometry.js'

const tolerance = 1e-5
const literal = value => typeof value === 'number' || (typeof value === 'string' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim()))
const pointAt = sketchPointAt
const edgeKeys = sketchEdgeKeys
const clone = feature => structuredClone(feature)
const constraintKeys = (c,feature) => sketchConstraintKeys(feature,c)
const constraintName = type => ({ horizontal:'水平',vertical:'垂直',coincident:'重合',fixed:'固定',length:'长度',parallel:'平行',perpendicular:'垂直关系',equal:'相等',tangent:'相切',angle:'夹角',concentric:'同心',radius:'半径' })[type] || type
export { constraintName }

function resolve(feature, parameters) {
  const result = resolveSketchFeature(feature, parameters)
  if (result.errors.length) throw new Error(result.errors[0])
  return result.feature
}
export function sketchConstraintResiduals(feature, parameters = {}) {
  const resolved=resolve(feature,parameters),constraints=feature.sketchConstraints||[],ids=new Set(),kinds=new Set()
  if(constraints.length>128)throw new Error('单个草图最多支持128个约束。')
  const residuals=constraints.flatMap(c=>{
    if(!c.id||ids.has(c.id)||!['horizontal','vertical','coincident','fixed','length','parallel','perpendicular','equal','tangent','angle','concentric','radius'].includes(c.type))throw new Error('草图约束无效或编号重复。')
    ids.add(c.id)
    const keys=constraintKeys(c,feature),scope=c.contourId||'main'
    if(!keys.length||keys.some(key=>!pointAt(resolved,key)))throw new Error('约束引用的点或线已不存在。')
    const signature=JSON.stringify([c.type,c.edges?c.edges.map(ref=>edgeAddress(ref,scope)).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b))):c.edge!==undefined?edgeAddress(c.edge,scope):[...keys].sort()])
    if(kinds.has(signature))throw new Error('重复约束会造成过约束，请编辑已有约束。')
    kinds.add(signature)
    if(c.edges){
      if(c.edges.length!==2||JSON.stringify(edgeAddress(c.edges[0],scope))===JSON.stringify(edgeAddress(c.edges[1],scope)))throw new Error('请选择两条不同的边。')
      const [a,b]=c.edges.map(ref=>sketchCurve(resolved,ref,scope)),unit=v=>{const n=Math.hypot(...v);if(n<1e-10)throw new Error('约束边方向不能为零。');return v.map(x=>x/n)},cross=(u,v)=>u[0]*v[1]-u[1]*v[0],dot=(u,v)=>u[0]*v[0]+u[1]*v[1]
      if(['parallel','perpendicular','angle'].includes(c.type)){
        if(a.segment.type!=='line'||b.segment.type!=='line')throw new Error('平行、垂直关系和夹角仅支持两条直线。')
        const u=unit(a.tangents[0]),v=unit(b.tangents[0])
        if(c.type==='parallel')return [cross(u,v)]
        if(c.type==='perpendicular')return [dot(u,v)]
        const target=evaluateSketchExpression(c.value,parameters)
        if(target<0||target>180)throw new Error('夹角须为0至180度。')
        return [Math.atan2(Math.abs(cross(u,v)),dot(u,v))-target*Math.PI/180]
      }
      if(c.type==='equal'){
        if(a.segment.type==='line'&&b.segment.type==='line')return[a.length-b.length]
        if(a.circle&&b.circle)return[a.circle.radius-b.circle.radius]
        throw new Error('相等支持两条直线长度或两个圆弧/圆的半径。')
      }
      if(c.type==='concentric'){
        if(!a.circle||!b.circle)throw new Error('同心需要两个圆或圆弧。')
        return a.circle.center.map((value,i)=>value-b.circle.center[i])
      }
      if(c.type==='tangent'){
        const pairs=[0,1].flatMap(i=>[0,1].map(j=>({i,j,a:i?a.end:a.start,b:j?b.end:b.start}))).sort((x,y)=>Math.hypot(...x.a.map((v,i)=>v-x.b[i]))-Math.hypot(...y.a.map((v,i)=>v-y.b[i])))
        const pair=pairs[0]
        return [...pair.a.map((value,i)=>value-pair.b[i]),cross(unit(a.tangents[pair.i]),unit(b.tangents[pair.j]))]
      }
    }
    const [a,b]=keys.map(key=>pointAt(resolved,key))
    if(c.type==='coincident'){if(keys.length!==2||keys[0]===keys[1])throw new Error('请选择两个不同的点。');return a.map((v,i)=>v-b[i])}
    if(c.type==='fixed'){if(keys.length!==1||!Array.isArray(c.position)||c.position.length!==2)throw new Error('固定约束缺少原始位置。');return a.map((v,i)=>v-evaluateSketchExpression(c.position[i],parameters))}
    const curve=sketchCurve(resolved,c.edge,scope)
    if(c.type==='radius'){
      if(!curve.circle)throw new Error('半径约束需要圆或圆弧。')
      const target=evaluateSketchExpression(c.value,parameters);if(target<=0)throw new Error('半径必须大于零。');return[curve.circle.radius-target]
    }
    if(curve.segment.type!=='line')throw new Error('此约束仅支持直线。')
    if(c.type==='horizontal')return[b[1]-a[1]]
    if(c.type==='vertical')return[b[0]-a[0]]
    const target=evaluateSketchExpression(c.value,parameters);if(target<=0)throw new Error('长度必须大于零。');return[curve.length-target]
  })
  return [...residuals,...implicitSketchResiduals(resolved)]
}

export function validateSketchConstraints(feature, parameters = {}) {
  if (sketchConstraintResiduals(feature, parameters).some(value => Math.abs(value) > tolerance)) throw new Error('修改与已有约束冲突；请先修改尺寸或移除相关约束。')
  const resolved = resolve(feature, parameters)
  for(const contour of sketchContourList(resolved)){
  let previous = contour.start
  for (const segment of contour.segments) {
    if (!numericSketchPoint(segment.to) || (sameSketchPoint(previous, segment.to)&&!['ellipse','spline'].includes(segment.type))) throw new Error('修改会产生零长度边，请调整尺寸或约束。')
    if (segment.type === 'arc') {
      const arc = sketchArc(previous, segment.through, segment.to)
      if (!arc) throw new Error('修改会使圆弧三点共线或重合。')
      if (segment.radius !== undefined && Math.abs(arc.radius - evaluateSketchExpression(segment.radius, parameters)) > tolerance) throw new Error('修改与圆弧的半径校核冲突，请修改关联半径参数或尺寸。')
    }
    if(segment.type==='spline')sketchCurve(resolved,{contourId:contour.id,edge:contour.segments.indexOf(segment)})
    previous = segment.to
  }}
  return true
}

function linearSolve(matrix, vector) {
  const rows = matrix.map((row, i) => [...row, vector[i]]), n = vector.length
  for (let col = 0; col < n; col++) {
    let pivot = col
    for (let row = col + 1; row < n; row++) if (Math.abs(rows[row][col]) > Math.abs(rows[pivot][col])) pivot = row
    if (Math.abs(rows[pivot][col]) < 1e-16) return null
    ;[rows[col], rows[pivot]] = [rows[pivot], rows[col]]
    const divider = rows[col][col]
    for (let j = col; j <= n; j++) rows[col][j] /= divider
    for (let row = 0; row < n; row++) if (row !== col) { const factor = rows[row][col]; for (let j = col; j <= n; j++) rows[row][j] -= factor * rows[col][j] }
  }
  return rows.map(row => row[n])
}

// Only the selected points and their constraint-connected component can move.
// Expressions remain in the profile; independent parameter values are variables.
function solve(feature, parameters, keys, targetResidual, { parameterNames, allowParameterChanges=true } = {}) {
  const working = clone(feature); let nextParameters = { ...parameters }
  const active = new Set(keys.map(key=>pointKey(key))), constraints = feature.sketchConstraints || []
  const closures=sketchClosurePairs(feature)
  const relationships=[...constraints.map(c=>constraintKeys(c,feature)),...closures,...sketchContourList(feature).flatMap(contour=>contour.segments.flatMap((segment,edge)=>segment.type==='ellipse'?[sketchCurveKeys(feature,{contourId:contour.id,edge})]:[]))]
  for(let pass=0;pass<=relationships.length;pass++)for(const related of relationships)if(related.some(key=>active.has(key)))related.forEach(key=>active.add(key))
  const circleRadii=new Set()
  for(const c of constraints)if(['radius','equal','concentric'].includes(c.type))for(const ref of c.edges||(c.edge!==undefined?[c.edge]:[])){const address=edgeAddress(ref,c.contourId||'main'),curve=sketchCurve(resolve(feature,parameters),address);if(curve.ellipse&&curve.circle)circleRadii.add(pointKey(`${address.edge}:radii`,address.contourId))}
  const variables = [], names = new Set()
  for (const key of active) {
    if (closures.some(([,last])=>key===last)) continue
    const point = pointAt(feature, key)
    if (!point) throw new Error('所选点已不存在。')
    for (let axis = 0; axis < 2; axis++) {
      if(axis===1&&circleRadii.has(key)&&point.every(literal))continue
      if (literal(point[axis]) && !parameterNames) variables.push({ key, axis, string: typeof point[axis] === 'string' })
      else if(allowParameterChanges)for (const name of sketchParameterLeaves(point[axis], parameters)) if (!parameterNames || parameterNames.includes(name)) names.add(name)
    }
  }
  names.forEach(name => variables.push({ name }))
  if (variables.length > 80) throw new Error('关联约束过多，请分段编辑或直接修改参数。')
  const read = variable => variable.name ? evaluateSketchExpression(variable.name, nextParameters) : Number(pointAt(working, variable.key)[variable.axis])
  const write = (variable, value) => {
    if (!Number.isFinite(value) || Math.abs(value) > 1e6) throw new Error('求解坐标超出范围。')
    if (variable.name) nextParameters = setSketchParameter(nextParameters, variable.name, value)
    else { pointAt(working, variable.key)[variable.axis] = variable.string ? String(value) : value; if(variable.axis===0&&circleRadii.has(variable.key)&&pointAt(feature,variable.key).every(literal))pointAt(working,variable.key)[1]=typeof pointAt(feature,variable.key)[1]==='string'?String(value):value; for(const [first,last]of closures)if(variable.key===first)pointAt(working,last)[variable.axis]=pointAt(working,first)[variable.axis] }
  }
  const residual = () => [...sketchConstraintResiduals(working, nextParameters), ...targetResidual(resolve(working, nextParameters), nextParameters)]
  for (let iteration = 0; iteration < 60; iteration++) {
    const r = residual()
    if (r.every(value => Math.abs(value) < 1e-10)) break
    if (!variables.length) throw new Error('当前几何由表达式固定，请修改其关联参数。')
    const values = variables.map(read), jac = variables.map((variable, i) => {
      const h = Math.max(1e-5, Math.abs(values[i]) * 1e-6)
      write(variable, values[i] + h)
      let rr
      try { rr = residual() } finally { write(variable, values[i]) }
      return rr.map((value, row) => (value - r[row]) / h)
    })
    const matrix = variables.map((_, i) => variables.map((__, j) => jac[i].reduce((sum, value, row) => sum + value * jac[j][row], 0) + (i === j ? 1e-8 : 0)))
    const delta = linearSolve(matrix, jac.map(column => -column.reduce((sum, value, row) => sum + value * r[row], 0)))
    if (!delta) break
    const before = r.reduce((sum, value) => sum + value * value, 0); let accepted = false
    for (let factor = 1; factor > 1 / 256; factor /= 2) {
      try { variables.forEach((variable, i) => write(variable, values[i] + factor * delta[i])); const after = residual().reduce((sum, value) => sum + value * value, 0); if (after < before) { accepted = true; break } } catch { /* Try a smaller bounded step. */ }
    }
    if (!accepted) { variables.forEach((variable, i) => write(variable, values[i])); break }
  }
  variables.forEach(variable => write(variable, Number(read(variable).toPrecision(13))))
  if (residual().some(value => !Number.isFinite(value) || Math.abs(value) > tolerance)) throw new Error('约束冲突或过约束，无法同时满足所选尺寸与固定条件。')
  validateSketchConstraints(working, nextParameters)
  return { feature: working, parameters: nextParameters }
}

export function moveLinkedSketchPoint(feature, key, point, parameters = {}) {
  key=pointKey(key)
  if (!numericSketchPoint(point)) throw new Error('坐标须为有限数值。')
  const original = pointAt(feature, key), resolved = pointAt(resolve(feature, parameters), key)
  if (!original) throw new Error('所选点已不存在。')
  if (!feature.sketchConstraints?.length && numericSketchPoint(original)&&!key.includes('/')&&feature.segments.every(segment=>['line','arc'].includes(segment.type))) {
    const next = updateSketchPoint(feature, key, point)
    validateSketchConstraints(next, parameters)
    return { feature: next, parameters }
  }
  for (let axis = 0; axis < 2; axis++) if (Math.abs(resolved[axis] - point[axis]) > tolerance && sketchParameterLeaves(original[axis], parameters).size > 1) throw new Error('此坐标关联多个参数，请在所选参数中指定要修改的尺寸。')
  return solve(feature, parameters, [key], resolved => pointAt(resolved, key).map((value, axis) => value - point[axis]))
}

export function moveLinkedSketchEdge(feature, index, delta, parameters = {}) {
  if (!numericSketchPoint(delta)) throw new Error('移动距离须为有限数值。')
  const keys = sketchCurveKeys(feature,index), original = resolve(feature, parameters)
  const coordinateKeys=keys.filter(key=>!key.endsWith(':radii'))
  for (const key of keys) for (let axis = 0; axis < 2; axis++) if (Math.abs(delta[axis]) > tolerance && sketchParameterLeaves(pointAt(feature, key)[axis], parameters).size > 1) throw new Error('所选边关联多个参数，请指定修改参数或分别移动控制点。')
  return solve(feature, parameters, keys, resolved => coordinateKeys.flatMap(key => pointAt(resolved, key).map((value, axis) => value - pointAt(original, key)[axis] - delta[axis])))
}

export function setSketchPointExpressions(feature, key, coordinates, parameters = {}) {
  key=pointKey(key)
  const next = clone(feature), raw = pointAt(next, key)
  if (!raw || !Array.isArray(coordinates) || coordinates.length !== 2) throw new Error('请选择存在的控制点。')
  coordinates.forEach((value, axis) => { evaluateSketchExpression(value, parameters); raw[axis] = value })
  for(const [first,last]of sketchClosurePairs(feature)){
    if(key===first)pointAt(next,last).splice(0,2,...raw)
    if(key===last)pointAt(next,first).splice(0,2,...raw)
  }
  validateSketchConstraints(next, parameters)
  return { feature: next, parameters }
}

export function addSketchConstraint(feature, type, selection, parameters = {}) {
  const id = `sketch_c${Array.from({ length: 129 }, (_, i) => i + 1).find(n => !(feature.sketchConstraints || []).some(c => c.id === `sketch_c${n}`))}`
  const c = { id, type, ...selection }
  if (type === 'fixed') c.position = [...pointAt(resolve(feature, parameters), selection.points[0],selection.contourId||'main')]
  const curves=c.edges?.map(ref=>sketchCurve(resolve(feature,parameters),ref,c.contourId||'main')),originalCircle=type==='radius'?sketchCurve(resolve(feature,parameters),c.edge,c.contourId||'main').circle:null
  if(type==='tangent'&&Math.min(...[curves[0].start,curves[0].end].flatMap(a=>[curves[1].start,curves[1].end].map(b=>Math.hypot(a[0]-b[0],a[1]-b[1]))))>tolerance)throw new Error('相切需要两条曲线已有共同端点；请先添加重合约束。')
  const next = { ...feature, sketchConstraints: [...(feature.sketchConstraints || []), c] }
  sketchConstraintResiduals(next, parameters) // Schema / duplicate checks before solving.
  return solve(next, parameters, constraintKeys(c,next), resolved => type==='concentric'?c.edges.map((ref,i)=>sketchCurve(resolved,ref,c.contourId||'main').circle.radius-curves[i].circle.radius):type==='radius'?sketchCurve(resolved,c.edge,c.contourId||'main').circle.center.map((v,i)=>v-originalCircle.center[i]):[])
}

export function changeSketchConstraintValue(feature,id,value,parameters={}) {
  const constraint=(feature.sketchConstraints||[]).find(item=>item.id===id)
  if(!constraint||!['length','radius','angle'].includes(constraint.type))throw new Error('请选择可驱动的尺寸约束。')
  const next={...feature,sketchConstraints:feature.sketchConstraints.map(item=>item.id===id?{...item,value}:item)}
  const originalCircle=constraint.type==='radius'?sketchCurve(resolve(feature,parameters),constraint.edge,constraint.contourId||'main').circle:null
  return solve(next,parameters,constraintKeys(constraint,next),resolved=>originalCircle?sketchCurve(resolved,constraint.edge,constraint.contourId||'main').circle.center.map((v,i)=>v-originalCircle.center[i]):[])
}

export function driveSketchEllipseRadius(feature,index,axis,scalar,parameters={}) {
  if(![0,1].includes(axis))throw new Error('请选择椭圆的长轴或短轴半径。')
  const curve=sketchCurve(resolve(feature,parameters),index)
  if(!curve.ellipse)throw new Error('所选边不是椭圆。')
  const value=evaluateSketchExpression(scalar,parameters)
  if(value<=0)throw new Error('椭圆半径必须大于零。')
  const ref=edgeAddress(index),key=pointKey(`${ref.edge}:radii`,ref.contourId),center=pointKey(`${ref.edge}:center`,ref.contourId)
  let source=feature
  // An explicitly typed formula becomes the radius reference. Keep existing
  // expression-owned coordinates; generate endpoint formulas only for literal
  // points produced by drawing tools, so later parameter changes stay bound.
  if(typeof scalar==='string'&&!literal(scalar)){
    source=clone(feature)
    const segment=sketchContour(source,ref.contourId).segments[ref.edge]
    segment.radii[axis]=scalar
    const endKeys=sketchEdgeKeys(ref)
    for(const [i,angle]of [segment.startAngle,segment.endAngle].entries()){
      const coordinates=pointAt(source,endKeys[i]),c=segment.center,r=segment.radii,t=`radians(${angle})`,rotation=`radians(${segment.rotation})`
      const expressions=[`(${c[0]})+(${r[0]})*cos(${t})*cos(${rotation})-(${r[1]})*sin(${t})*sin(${rotation})`,`(${c[1]})+(${r[0]})*cos(${t})*sin(${rotation})+(${r[1]})*sin(${t})*cos(${rotation})`]
      coordinates.forEach((old,j)=>{if(literal(old)){evaluateSketchExpression(expressions[j],parameters);coordinates[j]=expressions[j]}})
    }
  }
  return solve(source,parameters,sketchCurveKeys(source,index),(resolved)=>[pointAt(resolved,key)[axis]-value,pointAt(resolved,key)[1-axis]-curve.ellipse.radii[1-axis],...pointAt(resolved,center).map((v,i)=>v-curve.ellipse.center[i])])
}

function driveGeneralDimension(feature,index,scalar,parameters) {
  const measurement=sketchEdgeMeasurement(feature,index,parameters),ref=edgeAddress(index)
  if(measurement.type==='ellipseRadius')return driveSketchEllipseRadius(feature,index,0,scalar,parameters)
  if(measurement.type==='radius'){
    const local=sketchContour(feature,ref.contourId),resolved=sketchContour(resolve(feature,parameters),ref.contourId),circle=measurement.arc
    const points=sketchPoints(local),isCircle=local.segments.length===2&&local.segments.every(segment=>segment.type==='arc')&&sketchPoints(resolved).every(({value})=>Math.abs(Math.hypot(value[0]-circle.center[0],value[1]-circle.center[1])-circle.radius)<tolerance)
    if(isCircle&&points.every(({value})=>value.every(literal))){
      const next=clone(feature),target=evaluateSketchExpression(scalar,parameters)
      if(target<=0)throw new Error('半径必须大于零。')
      for(const point of points){const raw=pointAt(next,pointKey(point.key,ref.contourId));for(let axis=0;axis<2;axis++){const ratio=(Number(point.value[axis])-circle.center[axis])/circle.radius;raw[axis]=typeof scalar==='string'?`(${circle.center[axis]})+(${Number(ratio.toPrecision(14))})*(${scalar})`:circle.center[axis]+ratio*target}}
      const existing=(next.sketchConstraints||[]).find(c=>c.type==='radius'&&JSON.stringify(edgeAddress(c.edge,c.contourId||'main'))===JSON.stringify(ref))
      const dimension={id:existing?.id||`sketch_radius_${ref.contourId}_${ref.edge}`,type:'radius',edge:index,value:scalar}
      next.sketchConstraints=[...(next.sketchConstraints||[]).filter(item=>item!==existing),dimension]
      try{validateSketchConstraints(next,parameters);return{feature:next,parameters}}catch{
        return solve(next,parameters,sketchCurveKeys(next,index),value=>sketchCurve(value,index).circle.center.map((coordinate,axis)=>coordinate-circle.center[axis]))
      }
    }
  }
  const type=measurement.type==='radius'?'radius':'length',existing=(feature.sketchConstraints||[]).find(c=>c.type===type&&c.edge!==undefined&&JSON.stringify(edgeAddress(c.edge,c.contourId||'main'))===JSON.stringify(ref))
  if(existing)return changeSketchConstraintValue(feature,existing.id,scalar,parameters)
  return addSketchConstraint(feature,type,type==='radius'?{edge:index,value:scalar}:{...(ref.contourId==='main'?{}:{contourId:ref.contourId}),edge:ref.edge,value:scalar},parameters)
}

export function sketchEdgeMeasurement(feature, index, parameters = {}) {
  const curve=sketchCurve(resolve(feature,parameters),index)
  if(curve.circle)return{type:'radius',value:curve.circle.radius,arc:curve.circle,start:curve.start,end:curve.end}
  if(curve.ellipse)return{type:'ellipseRadius',value:curve.ellipse.radii[0],ellipse:curve.ellipse,start:curve.start,end:curve.end}
  if(curve.spline)throw new Error('样条由插值点驱动，请编辑图上的插值点。')
  return {type:'length',value:curve.length,start:curve.start,end:curve.end}
}

function driveAnchoredRectangle(feature, index, scalar, parameters) {
  const constraints = feature.sketchConstraints || [], anchor = constraints.find(c => c.id === 'sketch_rect_origin' && c.type === 'fixed' && c.points?.length === 1 && c.points[0] === 'start')
  if (!anchor || feature.segments.length !== 4 || feature.segments.some(segment => segment.type !== 'line') || !['horizontal','vertical','horizontal','vertical'].every((type, edge) => constraints.some(c => c.id === `sketch_rect_${edge}` && c.type === type && c.edge === edge))) return null
  const axis = index % 2, corners = axis === 0 ? [0,1] : [1,2]
  // Formula-owned coordinates continue through the parameter solver unchanged.
  if (!literal(feature.start[axis]) || corners.some(edge => !literal(feature.segments[edge].to[axis]))) return null
  const resolved = resolve(feature, parameters)
  if (!sameSketchPoint(resolved.start, resolved.segments[3].to)) return null
  const extent = resolved.segments[axis === 0 ? 0 : 1].to[axis] - resolved.start[axis]
  if (Math.abs(extent) < tolerance) return null
  const next = clone(feature), target = evaluateSketchExpression(scalar, parameters), sign = Math.sign(extent)
  for (const edge of corners) {
    const coordinate = typeof scalar === 'string' ? `(${feature.start[axis]})+(${sign})*(${scalar})` : resolved.start[axis] + sign * target
    if (typeof coordinate === 'string' && coordinate.length > 256) throw new Error('尺寸公式过长，请先定义命名参数。')
    next.segments[edge].to[axis] = typeof scalar !== 'string' && typeof feature.segments[edge].to[axis] === 'string' ? String(coordinate) : coordinate
  }
  validateSketchConstraints(next, parameters)
  return {feature:next,parameters}
}

export function driveSketchDimension(feature, index, scalar, parameters = {}) {
  if(typeof index==='object'||feature.segments[index]?.type==='ellipse'||feature.segments[index]?.type==='spline')return driveGeneralDimension(feature,index,scalar,parameters)
  const target = evaluateSketchExpression(scalar, parameters)
  if (target <= 0) throw new Error('尺寸必须大于零。')
  const measurement = sketchEdgeMeasurement(feature, index, parameters), keys = [...edgeKeys(index), ...(feature.segments[index].type === 'arc' ? [`${index}:through`] : [])]
  const leaves = new Set(keys.flatMap(key => pointAt(feature, key).flatMap(value => [...sketchParameterLeaves(value, parameters)])))
  const affecting = [...leaves].filter(name => {
    const original = evaluateSketchExpression(name, parameters), h = Math.max(1e-4, Math.abs(original) * 1e-5)
    return Math.abs(sketchEdgeMeasurement(feature, index, setSketchParameter(parameters, name, original + h)).value - measurement.value) / h > 1e-6
  })
  if (affecting.length > 1) throw new Error(`尺寸关联多个参数（${affecting.join('、')}），请指定修改其中一个参数。`)
  if (measurement.type === 'radius' && !affecting.length) {
    const next = clone(feature), circle = measurement.arc, resolved = resolve(feature, parameters)
    const wholeCircle = sketchPoints(resolved).every(({ value }) => Math.abs(Math.hypot(value[0] - circle.center[0], value[1] - circle.center[1]) - circle.radius) < tolerance)
    const changed = wholeCircle ? sketchPoints(feature).map(p => p.key) : keys
    for (const key of changed) {
      const raw = pointAt(next, key), point = pointAt(resolved, key)
      if (!raw.every(literal)) throw new Error('此圆弧由固定表达式定义，请编辑其表达式或关联参数。')
      point.forEach((value, axis) => { const ratio = (value - circle.center[axis]) / circle.radius, result = circle.center[axis] + ratio * target; raw[axis] = typeof scalar === 'string' ? `(${circle.center[axis]})+(${Number(ratio.toPrecision(14))})*(${scalar})` : typeof raw[axis] === 'string' ? String(result) : result })
    }
    next.segments.forEach((segment, edge) => { if ((wholeCircle || edge === index) && segment.radius !== undefined) segment.radius = scalar })
    validateSketchConstraints(next, parameters)
    return { feature: next, parameters }
  }
  // Editing a dimension updates its existing constraint, never adds a duplicate.
  let next = feature
  if (measurement.type === 'length') {
    const constraints = feature.sketchConstraints || [], existing = constraints.find(c => c.type === 'length' && c.edge === index)
    const dimension = { id: existing?.id || `sketch_dim_${index}`, type: 'length', edge: index, value: scalar }
    next = { ...feature, sketchConstraints: [...constraints.filter(c => c !== existing), dimension] }
    if (!affecting.length) {
      const rectangle = driveAnchoredRectangle(next, index, scalar, parameters)
      if (rectangle) return rectangle
    }
    if (!affecting.length && typeof scalar === 'string') {
      next = clone(next)
      const rawStart = pointAt(next, keys[0]), rawEnd = pointAt(next, keys[1]), closed = JSON.stringify(feature.start) === JSON.stringify(feature.segments.at(-1)?.to)
      for (let axis = 0; axis < 2; axis++) {
        const direction = (measurement.end[axis] - measurement.start[axis]) / measurement.value
        if (!literal(rawEnd[axis])) throw new Error('所选边由固定表达式定义，请编辑其关联参数。')
        rawEnd[axis] = Math.abs(direction) < 1e-12 ? rawStart[axis] : `(${rawStart[axis]})+(${Number(direction.toPrecision(14))})*(${scalar})`
      }
      if (closed && keys[1] === `${next.segments.length - 1}:to`) next.start = [...rawEnd]
      validateSketchConstraints(next, parameters)
      return { feature: next, parameters }
    }
  }
  const result = solve(next, parameters, keys, resolved => [sketchEdgeMeasurement(resolved, index).value - target], affecting.length ? { parameterNames: affecting } : {})
  // Parameter-owned geometry stays expression based, with the dimension following
  // the same parameter instead of freezing a contradictory numeric constraint.
  if (measurement.type === 'length' && affecting.length && typeof scalar === 'number') {
    const dimension = result.feature.sketchConstraints.find(c => c.type === 'length' && c.edge === index)
    const start = pointAt(feature, keys[0]), end = pointAt(feature, keys[1])
    dimension.value = `1000*sqrt((((${end[0]})-(${start[0]}))/1000)**2+(((${end[1]})-(${start[1]}))/1000)**2)`
    if (dimension.value.length > 256) dimension.value = scalar
  }
  return result
}

export function changeLinkedSketchParameter(feature, parameters, name, scalar) {
  const nextParameters = setSketchParameter(parameters, name, evaluateSketchExpression(scalar, parameters))
  try{validateSketchConstraints(feature,nextParameters);return{feature,parameters:nextParameters}}
  catch(error){
    if(!(feature.sketchConstraints||[]).some(item=>!['horizontal','vertical','fixed','coincident','length'].includes(item.type)))throw error
    return solve(feature,nextParameters,sketchContourList(feature).flatMap(contour=>sketchPoints(contour).map(point=>pointKey(point.key,contour.id))),()=>[],{allowParameterChanges:false})
  }
}
