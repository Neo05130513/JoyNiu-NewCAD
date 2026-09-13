const clone=value=>structuredClone(value)
const identifier=/^[A-Za-z][A-Za-z0-9_]{0,63}$/

export function sketchContourList(feature) {
  return [{id:'main',role:'outer',start:feature.start,segments:feature.segments},...(feature.contours||[])]
}
export function sketchContour(feature,id='main') {
  const contour=sketchContourList(feature).find(item=>item.id===id)
  if(!contour)throw new Error('所选轮廓已不存在。')
  return contour
}
export function sketchContourFeature(feature,id='main') {
  if(id==='main')return feature
  const contour=sketchContour(feature,id)
  return {...feature,start:contour.start,segments:contour.segments}
}
export const sketchPointRef=(contourId,point)=>contourId==='main'?point:{contourId,point}
export const sketchEdgeRef=(contourId,edge)=>contourId==='main'?edge:{contourId,edge}
export function sketchReferenceContour(reference,fallback='main') {return reference&&typeof reference==='object'?reference.contourId:fallback}
export function constraintContours(constraint) {
  const fallback=constraint.contourId||'main'
  const references=[...(constraint.points||[]),...(constraint.edges||[]),...(constraint.edge!==undefined?[constraint.edge]:[])]
  return [...new Set(references.length?references.map(ref=>sketchReferenceContour(ref,fallback)):[fallback])]
}
export function replaceSketchContour(feature,id,replacement,{replaceConstraints=false}={}) {
  const next=clone(feature)
  if(id==='main'){next.start=clone(replacement.start);next.segments=clone(replacement.segments)}
  else{const index=(next.contours||[]).findIndex(item=>item.id===id);if(index<0)throw new Error('所选轮廓已不存在。');next.contours[index]={...next.contours[index],start:clone(replacement.start),segments:clone(replacement.segments)}}
  if(replaceConstraints){
    const cross=(next.sketchConstraints||[]).filter(item=>constraintContours(item).includes(id)&&constraintContours(item).some(value=>value!==id))
    if(cross.length)throw new Error('此轮廓与其他轮廓有关联约束，请先移除相关约束再替换。')
    next.sketchConstraints=[...(next.sketchConstraints||[]).filter(item=>!constraintContours(item).includes(id)),...(replacement.sketchConstraints||[]).map(item=>({...clone(item),id:id==='main'?item.id:`${id}_${item.id}`,...(id==='main'?{}:{contourId:id})}))]
  }
  return next
}
export function addSketchContour(feature,{role='outer',parent='main'}={}) {
  const contours=sketchContourList(feature)
  if(contours.length>=33)throw new Error('一个草图最多支持33个轮廓。')
  if(!['outer','hole'].includes(role))throw new Error('请选择外轮廓或孔洞。')
  if(role==='hole'&&sketchContour(feature,parent).role!=='outer')throw new Error('孔洞必须属于一个外轮廓。')
  let number=1;while(contours.some(item=>item.id===`contour${number}`))number++
  const id=`contour${number}`,next=clone(feature)
  next.contours=[...(next.contours||[]),{id,role,...(role==='hole'?{parent}:{}),start:[0,0],segments:[]}]
  return {feature:next,id}
}
export function deleteSketchContour(feature,id) {
  if(id==='main')throw new Error('主外轮廓不能删除；可明确替换其图形。')
  sketchContour(feature,id)
  if((feature.contours||[]).some(item=>item.parent===id))throw new Error('此外轮廓仍包含孔洞，请先删除或重新指定这些孔洞。')
  const cross=(feature.sketchConstraints||[]).filter(item=>constraintContours(item).includes(id)&&constraintContours(item).some(value=>value!==id))
  if(cross.length)throw new Error('此轮廓与其他轮廓有关联约束，请先移除相关约束。')
  return {...clone(feature),contours:(feature.contours||[]).filter(item=>item.id!==id),sketchConstraints:(feature.sketchConstraints||[]).filter(item=>!constraintContours(item).includes(id))}
}
export function setSketchContourRole(feature,id,role,parent='main') {
  if(id==='main')throw new Error('主轮廓始终是外轮廓。')
  if(!['outer','hole'].includes(role))throw new Error('请选择外轮廓或孔洞。')
  if(role==='hole'&&(parent===id||sketchContour(feature,parent).role!=='outer'))throw new Error('孔洞必须属于另一个外轮廓。')
  if(role==='hole'&&(feature.contours||[]).some(item=>item.parent===id))throw new Error('此轮廓仍包含孔洞，不能将它改为孔洞。')
  return {...clone(feature),contours:(feature.contours||[]).map(item=>{if(item.id!==id)return item;const next={...item,role};if(role==='hole')next.parent=parent;else delete next.parent;return next})}
}
export function validateSketchContourStructure(feature) {
  const contours=sketchContourList(feature),seen=new Set()
  if(contours.length>33)throw new Error('一个草图最多支持33个轮廓。')
  for(const item of contours){
    if(!identifier.test(item.id)||seen.has(item.id))throw new Error('轮廓编号无效或重复。')
    seen.add(item.id)
    if(!['outer','hole'].includes(item.role)||!Array.isArray(item.start)||item.start.length!==2||!Array.isArray(item.segments)||item.segments.length>128)throw new Error('轮廓结构无效。')
    if(item.role==='hole'&&(item.parent===item.id||!contours.some(parent=>parent.id===item.parent&&parent.role==='outer')))throw new Error('请为孔洞指定存在的外轮廓。')
    if(item.role==='outer'&&item.parent!==undefined)throw new Error('外轮廓不能指定孔洞归属。')
  }
  if(contours.reduce((sum,item)=>sum+item.segments.length,0)>512)throw new Error('单个草图总边数不能超过512。')
  return contours
}
