const near = (a, b) => Array.isArray(a) && Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-4

export function nativeLayouts(layouts) {
  const names = [...new Set((Array.isArray(layouts) ? layouts : []).filter(name => typeof name === 'string' && name.length))]
  return names.length ? names : ['Model']
}

export function validNativeLayout(layouts, current) {
  const names = nativeLayouts(layouts)
  return names.includes(current) ? current : names.includes('Model') ? 'Model' : names[0]
}

export function visibleNativeEntities(document) {
  return (document?.entities || []).filter(entity => !(document.layers || []).some(layer => layer.name === entity.layer && (layer.visible === false || layer.frozen)))
}

export function nativeVisibleRenderEntities(document) {
  const originals=document?.entities||[],rendered=document?.renderEntities||[]
  const parents=new Map(originals.map(entity=>[entity.id,entity]))
  const hiddenLayers=new Set((document?.layers||[]).filter(layer=>layer.visible===false||layer.frozen).map(layer=>layer.name))
  const renderedParents=new Set(rendered.map(entity=>entity.parentId))
  return [...originals.filter(entity=>!renderedParents.has(entity.id)),...rendered].flatMap(entity=>{
    const parent=parents.get(entity.parentId)
    const layer=parent&&entity.layer==='0'?parent.layer:entity.layer
    if(hiddenLayers.has(layer)||hiddenLayers.has(parent?.layer))return []
    return [{...entity,layer,...(parent&&entity.color===0?{color:parent.color}:{} )}]
  })
}

export function nativeDialogKey(event,cancel) {
  event.stopPropagation()
  if(event.key==='Escape'){event.preventDefault();cancel();return}
  if(event.key!=='Tab')return
  const buttons=[...event.currentTarget.querySelectorAll('button:not(:disabled)')]
  const first=buttons[0],last=buttons.at(-1)
  if(event.shiftKey&&event.target===first){event.preventDefault();last?.focus()}
  else if(!event.shiftKey&&event.target===last){event.preventDefault();first?.focus()}
}

export function nativePolylineText(points) {
  return (points || []).map(point => point.join(',')).join('\n')
}

export function parseNativePolylineText(text) {
  const rows=text.trim().split('\n')
  if(rows.length<2||rows.length>10000)throw new Error('轮廓需要 2–10000 个顶点')
  return rows.map(row=>{
    const parts=row.trim().split(',').map(part=>part.trim())
    if(parts.length<2||parts.length>3||parts.some(part=>part===''||!Number.isFinite(Number(part))||Math.abs(Number(part))>1e7))throw new Error('每行请输入 X,Y 或 X,Y,凸度，不能留空')
    return [Number(parts[0]),Number(parts[1]),Number(parts[2]||0)]
  })
}

export function nativePropertyDirty(document,property,pointText) {
  if(!property)return false
  const saved=document?.entities?.find(entity=>entity.id===property.id)
  return !!saved&&(JSON.stringify(saved)!==JSON.stringify(property)||(property.type==='LWPOLYLINE'&&pointText!==nativePolylineText(saved.points)))
}

function pointReference(entities, point) {
  for (const entity of entities) {
    const supported = entity.type === 'LINE' ? ['start', 'end'] : ['CIRCLE', 'ARC'].includes(entity.type) ? ['center'] : []
    for (const key of supported) if (near(entity[key], point)) return {entityId: entity.id, point: key}
  }
}

export function nativeDimensionDraft({kind, layer, points, entities = [], preferredIds = []}) {
  if (!Array.isArray(points) || points.length !== 3 || points.some(point => !Array.isArray(point) || point.length < 2 || point.slice(0, 2).some(n => !Number.isFinite(n)))) throw new Error('标注需要三个有效坐标')
  const [a, b, c] = points
  const dx = b[0] - a[0], dy = b[1] - a[1], length = Math.hypot(dx, dy)
  if (length < 1e-9) throw new Error('标注的两个定义点不能重合，请重新选择')
  const dimension = {op: 'dimension', kind, layer, p1: a, p2: b, base: c, angle: 0}
  if (kind === 'aligned') dimension.distance = (dx * (c[1] - a[1]) - dy * (c[0] - a[0])) / length
  if (['radius', 'diameter'].includes(kind)) Object.assign(dimension, {center: a, radius: length, angle: Math.atan2(c[1] - a[1], c[0] - a[0]) * 180 / Math.PI})
  if (kind === 'angular') {
    if (Math.hypot(c[0] - a[0], c[1] - a[1]) < 1e-9) throw new Error('角度标注的射线不能为零长度，请重新选择')
    Object.assign(dimension, {center: a, p1: b, p2: c})
  }
  const ordered = [...entities].sort((first, second) => Number(preferredIds.includes(second.id)) - Number(preferredIds.includes(first.id)))
  const sourceRefs = {}
  if (['radius', 'diameter'].includes(kind)) {
    const circle = ordered.find(entity => ['CIRCLE', 'ARC'].includes(entity.type) && near(entity.center, a) && Math.abs(entity.radius - length) < 1e-4)
    if (circle) sourceRefs.circle = {entityId: circle.id}
  } else {
    const keys = kind === 'angular' ? [['center', a], ['p1', b], ['p2', c]] : [['p1', a], ['p2', b]]
    for (const [key, point] of keys) {
      const reference = pointReference(ordered, point)
      if (reference) sourceRefs[key] = reference
    }
  }
  if (Object.keys(sourceRefs).length) dimension.sourceRefs = sourceRefs
  return dimension
}
