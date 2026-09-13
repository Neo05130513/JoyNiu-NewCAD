const radians=value=>value*Math.PI/180
export function ellipsePoint(segment,angle) {
  const t=radians(angle),rotation=radians(segment.rotation||0),x=segment.radii[0]*Math.cos(t),y=segment.radii[1]*Math.sin(t)
  return [segment.center[0]+x*Math.cos(rotation)-y*Math.sin(rotation),segment.center[1]+x*Math.sin(rotation)+y*Math.cos(rotation)]
}
export function ellipseTangent(segment,angle) {
  const t=radians(angle),rotation=radians(segment.rotation||0),x=-segment.radii[0]*Math.sin(t),y=segment.radii[1]*Math.cos(t)
  return [x*Math.cos(rotation)-y*Math.sin(rotation),x*Math.sin(rotation)+y*Math.cos(rotation)]
}
export function ellipseSvgPath(segment) {
  const start=ellipsePoint(segment,segment.startAngle),span=segment.endAngle-segment.startAngle,sweep=span>0?1:0
  const arc=angle=>`A ${segment.radii.join(' ')} ${segment.rotation||0} ${Math.abs(angle-segment.startAngle)>180?1:0} ${sweep} ${ellipsePoint(segment,angle).join(' ')}`
  if(Math.abs(span)>=360-1e-8){const middle=ellipsePoint(segment,segment.startAngle+span/2);return `M ${start.join(' ')} A ${segment.radii.join(' ')} ${segment.rotation||0} 0 ${sweep} ${middle.join(' ')} A ${segment.radii.join(' ')} ${segment.rotation||0} 0 ${sweep} ${start.join(' ')}`}
  return `M ${start.join(' ')} ${arc(segment.endAngle)}`
}

// Chord-length cubic interpolation through the clicked points. The editable
// interpolation points are retained; the kernel builds the actual CAD spline.
export function sketchSplineBeziers(points) {
  if(points.length<3||points.some(point=>!Array.isArray(point)||point.length!==2||point.some(value=>!Number.isFinite(value))))throw new Error('样条需要至少三个有限插值点。')
  const n=points.length,h=points.slice(1).map((point,i)=>Math.hypot(point[0]-points[i][0],point[1]-points[i][1]))
  if(h.some(value=>value<1e-8))throw new Error('样条相邻插值点不能重合。')
  const secants=h.map((step,i)=>points[i].map((value,axis)=>(points[i+1][axis]-value)/step))
  const derivatives=points.map((_,i)=>i===0?[...secants[0]]:i===n-1?[...secants.at(-1)]:[0,1].map(axis=>(h[i]*secants[i-1][axis]+h[i-1]*secants[i][axis])/(h[i-1]+h[i])))
  if(Math.hypot(points[0][0]-points.at(-1)[0],points[0][1]-points.at(-1)[1])<1e-8){
    const closure=[0,1].map(axis=>(h[0]*secants.at(-1)[axis]+h.at(-1)*secants[0][axis])/(h[0]+h.at(-1)))
    derivatives[0]=closure;derivatives[n-1]=[...closure]
  }
  return h.map((step,i)=>({start:points[i],c1:points[i].map((v,axis)=>v+derivatives[i][axis]*step/3),c2:points[i+1].map((v,axis)=>v-derivatives[i+1][axis]*step/3),end:points[i+1]}))
}
export function splineSvgPath(points) {return sketchSplineBeziers(points).map((curve,i)=>`${i?'':`M ${curve.start.join(' ')} `}C ${curve.c1.join(' ')} ${curve.c2.join(' ')} ${curve.end.join(' ')}`).join(' ')}
