import { cloneFeatureValue, newFeature } from './directFeatureModel.js'
import { evaluateSketchExpression } from './featureSketchExpressions.js'

export const baseReferenceFrames = {XY:{origin:[0,0,0],xDir:[1,0,0],normal:[0,0,1]},XZ:{origin:[0,0,0],xDir:[1,0,0],normal:[0,-1,0]},YZ:{origin:[0,0,0],xDir:[0,1,0],normal:[1,0,0]}}
export const sketchEntityFields = ['plane','origin','frame','start','segments','contours','sketchConstraints','planeReference','planeSource','planeAttachment']
export const pmiNames = {distance:'距离',angle:'角度',radius:'半径',diameter:'直径',note:'注释',datum:'基准',tolerance:'形位公差',surface_finish:'表面粗糙度'}
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
const unit=v=>{const l=Math.hypot(...v);if(l<1e-9)throw new Error('方向不能为零，构造点不能共线。');return v.map(n=>n/l)}
export function editorEntityId(prefix,items=[]){let i=1;while(items.some(item=>item.id===`${prefix}${i}`))i++;return `${prefix}${i}`}
export function referenceFrames(plan){
  const parameters=(plan.parameters||{})
  const number=value=>evaluateSketchExpression(value,parameters)
  const vector=p=>p.map(number),frames=cloneFeatureValue(baseReferenceFrames)
  for(const ref of plan.references||[]){
    if(ref.kind!=='plane')continue
    let frame
    if(ref.mode==='frame')frame=Object.fromEntries(Object.entries(ref.frame).map(([k,v])=>[k,vector(v)]))
    else if(ref.mode==='three_points'){
      const [a,b,c]=ref.points.map(vector),x=unit(b.map((v,i)=>v-a[i]));frame={origin:a,xDir:x,normal:unit(cross(x,c.map((v,i)=>v-a[i])))}
    }else{
      if(!frames[ref.parent])throw new Error('引用的基准面不存在。')
      frame=cloneFeatureValue(frames[ref.parent]);frame.origin=frame.origin.map((v,i)=>v+number(ref.offset??0)*frame.normal[i])
      const axis=ref.axis==='y'?unit(cross(frame.normal,frame.xDir)):frame.xDir,a=number(ref.angle??0)*Math.PI/180
      const rotate=v=>{const c=cross(axis,v),dot=axis.reduce((s,x,i)=>s+x*v[i],0);return v.map((x,i)=>x*Math.cos(a)+c[i]*Math.sin(a)+axis[i]*dot*(1-Math.cos(a)))}
      frame.normal=rotate(frame.normal);frame.xDir=rotate(frame.xDir)
    }
    frame.normal=unit(frame.normal);frame.xDir=unit(cross(cross(frame.normal,frame.xDir),frame.normal));frames[ref.id]=frame
  }
  return frames
}
export function independentSketchFeature(sketch,op='profile_extrude'){
  return {id:sketch.id,label:sketch.label||'草图',op,...Object.fromEntries(sketchEntityFields.filter(k=>Object.hasOwn(sketch,k)).map(k=>[k,cloneFeatureValue(sketch[k])])),...(op==='profile_extrude'?{distance:1}:{})}
}
export function storeIndependentSketch(plan,feature,id){
  const next=cloneFeatureValue(plan),key=id||editorEntityId('sketch',plan.sketches)
  const sketch={id:key,label:feature.label||`草图 ${key.replace(/^sketch/,'')}`,...Object.fromEntries(sketchEntityFields.filter(k=>Object.hasOwn(feature,k)).map(k=>[k,cloneFeatureValue(feature[k])]))}
  next.sketches=(next.sketches||[]).some(s=>s.id===key)?next.sketches.map(s=>s.id===key?sketch:s):[...(next.sketches||[]),sketch]
  next.features=next.features.map(f=>{if(f.sketchId!==key)return f;const result={...f};for(const k of sketchEntityFields)delete result[k];return {...result,...Object.fromEntries(sketchEntityFields.filter(k=>Object.hasOwn(sketch,k)).map(k=>[k,cloneFeatureValue(sketch[k])]))}})
  return {plan:next,sketch}
}
export function featureFromIndependentSketch(plan,id,op){
  const sketch=plan.sketches?.find(item=>item.id===id)
  if(!sketch)throw new Error('独立草图不存在。')
  const feature=newFeature(op,plan.features)
  for(const key of sketchEntityFields)delete feature[key]
  Object.assign(feature,Object.fromEntries(sketchEntityFields.filter(k=>Object.hasOwn(sketch,k)).map(k=>[k,cloneFeatureValue(sketch[k])])),{sketchId:id})
  return feature
}
export function removeIndependentSketch(plan,id){
  if(plan.features.some(feature=>feature.sketchId===id||feature.path?.sketchId===id||feature.sections?.some(section=>section.sketchId===id)))throw new Error('该草图仍被特征使用，请先解除引用或删除依赖特征。')
  return {...cloneFeatureValue(plan),sketches:(plan.sketches||[]).filter(s=>s.id!==id)}
}
export function pmiMeasurement(note,parameters={}){
  const values=parameters,number=v=>evaluateSketchExpression(v,values),points=(note.points||[]).map(p=>p.map(number))
  if(note.kind==='distance')return Math.hypot(...points[0].map((v,i)=>v-points[1][i]))
  if(note.kind==='angle'){const [a,o,b]=points,va=unit(a.map((v,i)=>v-o[i])),vb=unit(b.map((v,i)=>v-o[i]));return Math.acos(Math.max(-1,Math.min(1,va.reduce((s,v,i)=>s+v*vb[i],0))))*180/Math.PI}
  if(['radius','diameter'].includes(note.kind)){const [a,b,c]=points,area=Math.hypot(...cross(b.map((v,i)=>v-a[i]),c.map((v,i)=>v-a[i])));if(area<1e-10)throw new Error('标注点共线，不能计算半径。');return Math.hypot(...a.map((v,i)=>v-b[i]))*Math.hypot(...b.map((v,i)=>v-c[i]))*Math.hypot(...c.map((v,i)=>v-a[i]))/(2*area)*(note.kind==='diameter'?2:1)}
  return note.value!==undefined?number(note.value):null
}
export function pmiLabel(note,parameters={}){
  try{const value=pmiMeasurement(note,parameters),formatted=value===null?'':Number(value.toFixed(3)).toString(),signed=v=>{const n=evaluateSketchExpression(v??0,parameters);return `${n>=0?'+':''}${Number(n.toFixed(3))}`},suffix=note.upper!==undefined||note.lower!==undefined?` ${signed(note.upper)}/${signed(note.lower)}`:''
    return note.kind==='note'?note.text:note.kind==='datum'?`[${note.datum||note.text}]`:note.kind==='tolerance'?`${note.symbol||'⌖'} | ${formatted||note.text} | ${note.datum||''}`:note.kind==='surface_finish'?`⌯ Ra ${formatted||note.text}`:`${note.kind==='radius'?'R':note.kind==='diameter'?'⌀':''}${formatted}${note.kind==='angle'?'°':''}${suffix}${note.text?` ${note.text}`:''}`
  }catch{return '标注需要重新选择几何'}
}
export function resolvedPmi(plan){
  const values=(plan.parameters||{})
  return (plan.annotations||[]).filter(note=>!note.hidden).map(note=>{try{pmiMeasurement(note,plan.parameters);return {...note,position:note.position.map(v=>evaluateSketchExpression(v,values)),points:note.points.map(p=>p.map(v=>evaluateSketchExpression(v,values))),displayText:pmiLabel(note,plan.parameters)}}catch{return null}}).filter(Boolean)
}
export function bodyStateFor(plan,body){return (plan.bodyStates||[]).find(item=>item.signature===body.signature)}
export function patchBodyState(plan,body,patch){
  const previous=bodyStateFor(plan,body),record={id:previous?.id||editorEntityId('bodyState',plan.bodyStates),label:previous?.label||`实体 ${body.index+1}`,signature:body.signature,...previous,...patch}
  return {...cloneFeatureValue(plan),bodyStates:[...(plan.bodyStates||[]).filter(item=>item.signature!==body.signature),record]}
}
export function maskCadBodies(geometry,states=[]){
  if(!geometry?.bodies?.length||!states.length)return geometry
  const hiddenFaces=new Set(),hiddenEdges=new Set(),colors=new Map()
  for(const body of geometry.bodies){const state=states.find(s=>s.signature===body.signature);if(state?.hidden){body.faceIds.forEach(id=>hiddenFaces.add(id));body.edgeIds.forEach(id=>hiddenEdges.add(id))}if(state?.color)body.faceIds.forEach(id=>colors.set(id,state.color))}
  const faceIds=[],indices=[]
  geometry.mesh.faceIds.forEach((id,i)=>{if(!hiddenFaces.has(id)){faceIds.push(id);indices.push(...geometry.mesh.indices.slice(i*3,i*3+3))}})
  return {...geometry,allBodiesHidden:!indices.length,mesh:indices.length?{...geometry.mesh,faceIds,indices}:geometry.mesh,faces:geometry.faces.map(face=>({...face,...(colors.has(face.id)?{color:colors.get(face.id)}:{})})),edges:geometry.edges.map(edge=>({...edge,hidden:hiddenEdges.has(edge.id)}))}
}

export function resolvedConstruction(plan) {
  const frames=referenceFrames(plan),number=v=>evaluateSketchExpression(v,plan.parameters||{}),vector=p=>p.map(number)
  return (plan.references||[]).map(ref=>{
    if(ref.kind==='plane')return {...ref,frame:frames[ref.id]}
    if(ref.kind==='axis')return {...ref,start:vector(ref.start),end:vector(ref.end)}
    if(ref.kind==='point')return {...ref,point:vector(ref.point)}
    const radius=number(ref.radius),pitch=number(ref.pitch),turns=number(ref.turns),f=Object.fromEntries(Object.entries(ref.frame).map(([k,v])=>[k,vector(v)]))
    if(radius<=0||pitch<=0||turns<=0||turns>100)throw new Error('螺旋线尺寸无效。')
    const n=unit(f.normal),x=unit(cross(cross(n,f.xDir),n)),y=unit(cross(n,x)),count=Math.min(4096,Math.max(32,Math.ceil(turns*64)))
    const points=Array.from({length:count+1},(_,i)=>{const t=i/count*turns,a=t*Math.PI*2*(ref.lefthand?-1:1);return f.origin.map((v,j)=>v+radius*(Math.cos(a)*x[j]+Math.sin(a)*y[j])+pitch*t*n[j])})
    return {...ref,radius,pitch,turns,frame:{...f,xDir:x,normal:n},points}
  })
}
