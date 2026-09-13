// Geometry and a bounded expression/constraint engine, shared by the native drawing UI.
const finite = value => typeof value === 'number' && Number.isFinite(value)
const clone = value => structuredClone(value)
export function evaluateFormula(expression, parameters = {}, stack = []) {
  if (finite(expression)) return expression
  const source = String(expression).trim()
  if (!source || source.length > 256) throw new Error('公式为空或过长')
  const tokens = source.match(/(?:\d*\.\d+|\d+\.?\d*)(?:[eE][+-]?\d+)?|[A-Za-z_][\w]*|[()+\-*/^,]/g) || []
  if (tokens.join('') !== source.replace(/\s/g, '')) throw new Error('公式含不支持的字符')
  let cursor = 0
  const functions = { sqrt: Math.sqrt, sin: Math.sin, cos: Math.cos, tan: Math.tan, abs: Math.abs, min: Math.min, max: Math.max }
  function atom() {
    const token = tokens[cursor++]
    if (token === '(') { const value = sum(); if (tokens[cursor++] !== ')') throw new Error('缺少右括号'); return value }
    if (/^(?:\d|\.)/.test(token || '')) return Number(token)
    if (token === 'pi') return Math.PI
    if (functions[token] && tokens[cursor] === '(') {
      cursor++; const args = [sum()]
      while (tokens[cursor] === ',') { cursor++; args.push(sum()) }
      if (tokens[cursor++] !== ')' || args.length > 8) throw new Error('函数参数无效')
      return functions[token](...args)
    }
    if (token && Object.hasOwn(parameters, token)) {
      if (stack.includes(token) || stack.length > 24) throw new Error('参数公式存在循环引用')
      return evaluateFormula(parameters[token], parameters, [...stack, token])
    }
    throw new Error(`未知参数 ${token || ''}`)
  }
  function power() { let n = atom(); if (tokens[cursor] === '^') { cursor++; n = n ** unary() } return n }
  function unary() { if (tokens[cursor] === '+') { cursor++; return unary() } if (tokens[cursor] === '-') { cursor++; return -unary() } return power() }
  function product() { let n = unary(); while (['*', '/'].includes(tokens[cursor])) { const op = tokens[cursor++], b = unary(); n = op === '*' ? n * b : n / b } return n }
  function sum() { let n = product(); while (['+', '-'].includes(tokens[cursor])) { const op = tokens[cursor++], b = product(); n = op === '+' ? n + b : n - b } return n }
  const result = sum()
  if (cursor !== tokens.length || !finite(result) || Math.abs(result) > 1e9) throw new Error('公式结果无效或超限')
  return result
}
export function entityPoints(entity) {
  if (entity.type === 'LINE') return [entity.start, entity.end]
  if (entity.type === 'ELLIPSE') { const [x,y]=entity.center,[a,b]=entity.majorAxis,r=entity.ratio,dx=Math.hypot(a,b*r),dy=Math.hypot(b,a*r);return [[x-dx,y-dy],[x+dx,y+dy],entity.center] }
  if (['CIRCLE', 'ARC'].includes(entity.type)) { const [x,y] = entity.center, r = entity.radius; return [[x-r,y-r],[x+r,y+r],entity.center] }
  if (entity.type === 'LWPOLYLINE') return entity.points || []
  return entity.position ? [entity.position] : []
}
export function drawingBounds(entities) {
  let x=Infinity,y=Infinity,right=-Infinity,top=-Infinity
  for(const entity of entities)for(const point of entityPoints(entity))if(finite(point?.[0])&&finite(point?.[1])){x=Math.min(x,point[0]);y=Math.min(y,point[1]);right=Math.max(right,point[0]);top=Math.max(top,point[1])}
  if (!finite(x)) return { x: -100, y: -75, width: 200, height: 150 }
  const width = Math.max(10,right-x), height = Math.max(10,top-y)
  return { x:x-width*.08, y:y-height*.08, width:width*1.16, height:height*1.16 }
}
export function snapPoint(point, entities, tolerance, grid = 0) {
  const points = entities.flatMap(e => {
    if (e.type === 'LINE') return [e.start,e.end,[(e.start[0]+e.end[0])/2,(e.start[1]+e.end[1])/2]]
    if (['CIRCLE','ARC'].includes(e.type)) return [e.center,...[0,90,180,270].map(a=>[e.center[0]+e.radius*Math.cos(a*Math.PI/180),e.center[1]+e.radius*Math.sin(a*Math.PI/180)])]
    return e.type === 'LWPOLYLINE' ? e.points : e.position ? [e.position] : []
  })
  let best = point, distance = tolerance
  for (const p of points) { const d = Math.hypot(p[0]-point[0],p[1]-point[1]); if (d < distance) { best = p.slice(0,2); distance = d } }
  return best !== point ? best : grid > 0 ? point.map(n=>Math.round(n/grid)*grid) : point
}
export function transformEntity(entity, { translation=[0,0], rotation=0, scale=1, origin=[0,0], mirror } = {}) {
  const result = clone(entity), angle=rotation*Math.PI/180
  if (!finite(scale) || scale <= 0 || scale > 1000) throw new Error('缩放必须在 0 到 1000 之间')
  const point = p => { let x=(p[0]-origin[0])*scale,y=(p[1]-origin[1])*scale; if(mirror==='x') y=-y; if(mirror==='y') x=-x; return [origin[0]+x*Math.cos(angle)-y*Math.sin(angle)+translation[0],origin[1]+x*Math.sin(angle)+y*Math.cos(angle)+translation[1],...(p.length>2?[p[2]]:[])] }
  for (const key of ['start','end','center','position']) if (result[key]) result[key]=point(result[key])
  if (result.points) result.points=result.points.map(p=>{const out=point(p);if(mirror&&out.length>2)out[2]=-out[2];return out})
  if (result.radius) result.radius*=scale
  if (result.majorAxis) {let [x,y]=result.majorAxis;x*=scale;y*=scale;if(mirror==='x')y=-y;if(mirror==='y')x=-x;result.majorAxis=[x*Math.cos(angle)-y*Math.sin(angle),x*Math.sin(angle)+y*Math.cos(angle),result.majorAxis[2]||0];if(mirror){const a=result.startParam??0,b=result.endParam??Math.PI*2;result.startParam=-b;result.endParam=-a}}
  if (result.height) result.height*=scale
  if (result.type==='ARC') { let a=result.startAngle,b=result.endAngle; if(mirror) [a,b]=mirror==='x'?[-b,-a]:[180-b,180-a]; result.startAngle=a+rotation; result.endAngle=b+rotation }
  if (['TEXT','MTEXT'].includes(result.type)) result.rotation=(result.rotation||0)+rotation
  return result
}
export const CONSTRAINT_TYPES = { horizontal:'水平',vertical:'垂直',length:'线长',radius:'半径',angle:'角度',parallel:'平行',perpendicular:'垂直关系',coincident:'端点重合',equal:'等长/等半径',concentric:'同心',distance:'端点距离',fixed:'固定' }
function solveLinear(a,b) {
  const n=b.length, m=a.map((r,i)=>[...r,b[i]])
  for(let k=0;k<n;k++){let p=k;for(let i=k+1;i<n;i++)if(Math.abs(m[i][k])>Math.abs(m[p][k]))p=i;[m[k],m[p]]=[m[p],m[k]];if(Math.abs(m[k][k])<1e-15)continue;const d=m[k][k];for(let j=k;j<=n;j++)m[k][j]/=d;for(let i=0;i<n;i++)if(i!==k){const f=m[i][k];for(let j=k;j<=n;j++)m[i][j]-=f*m[k][j]}}
  return m.map(r=>r[n])
}
export function solveConstraints(entities, constraints, parameters = {}) {
  if (constraints.length > 80) throw new Error('单组约束不能超过 80 项')
  const ids=new Set(constraints.flatMap(c=>c.entities||[])), working=entities.filter(e=>ids.has(e.id)).map(clone), original=new Map(entities.map(e=>[e.id,e]))
  if(working.length>40)throw new Error('单次约束求解最多 40 个图元')
  const byId=new Map(working.map(e=>[e.id,e])), variables=[]
  for(const e of working){if(e.type==='LINE')for(const k of ['start','end'])for(let i=0;i<2;i++)variables.push([e,k,i]);else if(['CIRCLE','ARC'].includes(e.type)){for(let i=0;i<2;i++)variables.push([e,'center',i]);variables.push([e,'radius',null])}else throw new Error('约束支持直线、圆和圆弧')}
  const read=([e,k,i])=>i===null?e[k]:e[k][i],write=([e,k,i],v)=>{if(i===null)e[k]=v;else e[k][i]=v}
  const length=e=>e.type==='LINE'?Math.hypot(e.end[0]-e.start[0],e.end[1]-e.start[1]):e.radius
  const endpoint=(e,which=0)=>e.type==='LINE'?(which?e.end:e.start):e.center
  const residual=()=>constraints.flatMap(c=>{
    const [a,b]=(c.entities||[]).map(id=>byId.get(id));if(!a || (['parallel','perpendicular','coincident','equal','concentric','distance'].includes(c.type)&&!b))throw new Error('约束引用的图元不存在')
    const numeric=['length','radius','angle','distance'].includes(c.type)?evaluateFormula(c.value,parameters):0
    if(['horizontal','vertical','length','angle','parallel','perpendicular'].includes(c.type)&&a.type!=='LINE')throw new Error('该约束需要直线')
    if(['angle','parallel','perpendicular'].includes(c.type)&&(length(a)<1e-9||(b?.type==='LINE'&&length(b)<1e-9)))throw new Error('零长度直线不能确定方向关系')
    const v=e=>[e.end[0]-e.start[0],e.end[1]-e.start[1]], [dx,dy]=a.type==='LINE'?v(a):[0,0]
    switch(c.type){
      case 'horizontal':return [dy];case 'vertical':return [dx];case 'length':if(numeric<=0)throw new Error('长度须大于零');return [length(a)-numeric]
      case 'radius':if(!['CIRCLE','ARC'].includes(a.type)||numeric<=0)throw new Error('半径需要圆且大于零');return [a.radius-numeric]
      case 'angle':return [Math.atan2(Math.sin(Math.atan2(dy,dx)-numeric*Math.PI/180),Math.cos(Math.atan2(dy,dx)-numeric*Math.PI/180))]
      case 'parallel':case 'perpendicular':{if(b.type!=='LINE')throw new Error('需要两条直线');const [bx,by]=v(b), norm=Math.max(1e-6,length(a)*length(b));return [(c.type==='parallel'?dx*by-dy*bx:dx*bx+dy*by)/norm]}
      case 'equal':if((a.type==='LINE')!==(b.type==='LINE'))throw new Error('等长需要两条直线，等半径需要两个圆或圆弧');return [length(a)-length(b)]
      case 'coincident':case 'distance':{const p=endpoint(a,c.ends?.[0]),q=endpoint(b,c.ends?.[1]);return c.type==='distance'?[Math.hypot(p[0]-q[0],p[1]-q[1])-numeric]:[p[0]-q[0],p[1]-q[1]]}
      case 'concentric':if(!a.center||!b.center)throw new Error('同心需要两个圆/圆弧');return a.center.slice(0,2).map((n,i)=>n-b.center[i])
      case 'fixed':{const base=c.geometry||original.get(a.id);return variables.filter(v=>v[0]===a).map(v=>read(v)-(v[2]===null?base[v[1]]:base[v[1]][v[2]]))}
      default:throw new Error('未知约束')
    }
  })
  for(let it=0;it<70;it++){
    const r=residual();if(r.every(v=>Math.abs(v)<1e-6))break
    const jac=variables.map(v=>{const saved=read(v),h=Math.max(1e-6,Math.abs(saved)*1e-7);write(v,saved+h);const rr=residual();write(v,saved);return rr.map((n,i)=>(n-r[i])/h)})
    const a=variables.map((_,i)=>variables.map((__,j)=>jac[i].reduce((s,n,k)=>s+n*jac[j][k],0)+(i===j?1e-7:0))),b=jac.map(row=>-row.reduce((s,n,k)=>s+n*r[k],0))
    const step=solveLinear(a,b),before=r.reduce((s,n)=>s+n*n,0),values=variables.map(read)
    let accepted=false
    for(let factor=1;factor>1/128;factor/=2){variables.forEach((v,i)=>write(v,values[i]+step[i]*factor));const after=residual().reduce((s,n)=>s+n*n,0);if(after<before){accepted=true;break}}
    if(!accepted){variables.forEach((v,i)=>write(v,values[i]));break}
  }
  if(residual().some(v=>!finite(v)||Math.abs(v)>1e-4)||variables.some(v=>!finite(read(v))||Math.abs(read(v))>1e7)||working.some(e=>e.radius!==undefined&&e.radius<=0))throw new Error('约束冲突或不能收敛，请检查重复尺寸与固定条件')
  return entities.map(e=>byId.get(e.id)||e)
}
const line=(x1,y1,x2,y2)=>({type:'LINE',start:[x1,y1,0],end:[x2,y2,0]})
const circle=(x,y,r)=>({type:'CIRCLE',center:[x,y,0],radius:r})
const rect=(x,y,w,h)=>({type:'LWPOLYLINE',points:[[x,y,0],[x+w,y,0],[x+w,y+h,0],[x,y+h,0]],closed:true})
export const PROFESSIONAL_SYMBOLS = [
  {id:'wall',discipline:'建筑',name:'双线墙',width:3000,height:240}, {id:'door',discipline:'建筑',name:'平开门',width:900,height:900}, {id:'window',discipline:'建筑',name:'窗',width:1500,height:240}, {id:'stair',discipline:'建筑',name:'直梯平面',width:1200,height:3000},
  {id:'pipe',discipline:'给排水',name:'双线管',width:2000,height:100}, {id:'valve',discipline:'给排水',name:'阀门',width:300,height:180}, {id:'pump',discipline:'给排水',name:'水泵',width:400,height:400}, {id:'drain',discipline:'给排水',name:'地漏',width:200,height:200},
  {id:'light',discipline:'电气',name:'照明灯',width:300,height:300}, {id:'switch',discipline:'电气',name:'开关',width:200,height:150}, {id:'outlet',discipline:'电气',name:'插座',width:200,height:200}, {id:'ground',discipline:'电气',name:'接地',width:250,height:250},
  {id:'duct',discipline:'暖通',name:'风管',width:2000,height:400}, {id:'diffuser',discipline:'暖通',name:'散流器',width:600,height:600}, {id:'fan',discipline:'暖通',name:'风机',width:600,height:600}, {id:'damper',discipline:'暖通',name:'风阀',width:600,height:400},
  {id:'column',discipline:'结构',name:'柱截面',width:600,height:600}, {id:'beam',discipline:'结构',name:'梁平面',width:4000,height:400}, {id:'footing',discipline:'结构',name:'独立基础',width:2000,height:1800}, {id:'rebar',discipline:'结构',name:'箍筋与纵筋',width:500,height:700},
]
export function professionalSymbol(id,width,height,origin=[0,0]) {
  if(![width,height].every(n=>finite(n)&&n>0&&n<=1e6))throw new Error('构件尺寸须大于零且不超过 1000000 mm')
  const w=width,h=height,r=Math.min(w,h)/2; let e=[]
  switch(id){
    case 'wall':case 'pipe':case 'duct':case 'beam':e=[line(0,0,w,0),line(0,h,w,h)];break
    case 'door':e=[line(0,0,w,0),line(0,0,0,h),{type:'ARC',center:[0,0,0],radius:w,startAngle:0,endAngle:90}];break
    case 'window':e=[rect(0,0,w,h),line(0,h/3,w,h/3),line(0,2*h/3,w,2*h/3)];break
    case 'stair':e=[rect(0,0,w,h),...Array.from({length:11},(_,i)=>line(0,(i+1)*h/12,w,(i+1)*h/12)),line(w/2,h*.1,w/2,h*.9),line(w/2,h*.9,w*.4,h*.8),line(w/2,h*.9,w*.6,h*.8)];break
    case 'valve':e=[{type:'LWPOLYLINE',points:[[0,0],[w,h],[w,0],[0,h]],closed:true}];break
    case 'pump':e=[circle(w/2,h/2,r),line(w/2,h/2,w,h/2),line(w,h/2,w*.65,h*.7),line(w,h/2,w*.65,h*.3)];break
    case 'drain':e=[rect(0,0,w,h),...Array.from({length:4},(_,i)=>line(0,(i+1)*h/5,w,(i+1)*h/5))];break
    case 'light':e=[circle(w/2,h/2,r),line(0,0,w,h),line(0,h,w,0)];break
    case 'switch':e=[circle(0,0,w*.12),line(w*.12,0,w,h)];break
    case 'outlet':e=[circle(w/2,h/2,r),line(w*.35,h*.2,w*.35,h*.55),line(w*.65,h*.2,w*.65,h*.55)];break
    case 'ground':e=[line(w/2,h,w/2,h*.45),line(0,h*.45,w,h*.45),line(w*.2,h*.22,w*.8,h*.22),line(w*.4,0,w*.6,0)];break
    case 'diffuser':e=[rect(0,0,w,h),rect(w*.2,h*.2,w*.6,h*.6),rect(w*.4,h*.4,w*.2,h*.2),line(0,0,w,h),line(0,h,w,0)];break
    case 'fan':e=[circle(w/2,h/2,r),circle(w/2,h/2,r*.2),...Array.from({length:4},(_,i)=>{const a=i*Math.PI/2;return line(w/2+r*.2*Math.cos(a),h/2+r*.2*Math.sin(a),w/2+r*.8*Math.cos(a+.4),h/2+r*.8*Math.sin(a+.4))})];break
    case 'damper':e=[rect(0,0,w,h),line(0,0,w,h),line(w/2,h/2,w/2,h*1.3)];break
    case 'column':e=[rect(0,0,w,h),line(0,0,w,h),line(0,h,w,0)];break
    case 'footing':e=[rect(0,0,w,h),rect(w*.35,h*.35,w*.3,h*.3),line(-w*.1,h/2,w*1.1,h/2),line(w/2,-h*.1,w/2,h*1.1)];break
    case 'rebar':e=[rect(0,0,w,h),rect(w*.1,h*.1,w*.8,h*.8),...[[.12,.12],[.88,.12],[.12,.88],[.88,.88]].map(([x,y])=>circle(w*x,h*y,Math.min(w,h)*.025))];break
    default:throw new Error('未知专业构件')
  }
  return e.map(entity=>transformEntity(entity,{translation:origin}))
}
