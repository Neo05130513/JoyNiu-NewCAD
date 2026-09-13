// This interpreter deliberately matches cad_plan's arithmetic subset. No eval,
// dynamic Function, property access, or implicit JavaScript coercion is used.
const functions = { sqrt: Math.sqrt, abs: Math.abs, min: Math.min, max: Math.max, sin: Math.sin, cos: Math.cos, radians: n => n * Math.PI / 180 }
const bounded = n => { if (typeof n !== 'number' || !Number.isFinite(n) || Math.abs(n) > 1e6) throw new Error('表达式结果须为 ±1000000 以内的有限数值。'); return n }
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key)

export function evaluateSketchExpression(value, parameters = {}, stack = []) {
  if (typeof value !== 'string') return bounded(value)
  if (!value.trim() || value.length > 256) throw new Error('表达式长度须为 1 至 256 个字符。')
  const tokens = value.match(/(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z][A-Za-z0-9_]*|\*\*|[()+\-*/,]/g) || []
  if (tokens.join('') !== value.replace(/\s/g, '') || tokens.length > 80) throw new Error('表达式仅支持参数、四则运算、** 及安全数学函数。')
  let cursor = 0, depth = 0, nodes = 1
  const addNodes = count => { nodes += count; if (nodes > 80) throw new Error('表达式过于复杂。') }
  const expression = (min = 0) => {
    if (++depth > 32) throw new Error('表达式嵌套过深。')
    let token = tokens[cursor++], result
    if (token === '+' || token === '-') { addNodes(2); result = bounded((token === '-' ? -1 : 1) * expression(3)) }
    else if (token === '(') { result = expression(); if (tokens[cursor++] !== ')') throw new Error('表达式括号不匹配。') }
    else if (/^(?:\d|\.)/.test(token || '')) { addNodes(1); result = bounded(Number(token)) }
    else if (/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(token || '')) {
      if (tokens[cursor] === '(') {
        addNodes(3)
        if (!own(functions, token)) throw new Error(`不支持函数 ${token}。`)
        cursor++; const args = [expression()]
        while (tokens[cursor] === ',') { cursor++; args.push(expression()); if (args.length > 8) throw new Error('函数最多支持 8 个参数。') }
        if (tokens[cursor++] !== ')' || (['min', 'max'].includes(token) ? args.length < 2 : args.length !== 1)) throw new Error('函数参数不正确。')
        result = bounded(functions[token](...args))
      } else {
        addNodes(2)
        if (!own(parameters, token)) throw new Error(`参数 ${token} 尚未定义，请先填写参数。`)
        if (stack.includes(token) || stack.length >= 100) throw new Error(`参数 ${token} 存在循环引用。`)
        const p = parameters[token], scalar = p && typeof p === 'object' ? (p.expression || p.value) : p
        result = evaluateSketchExpression(scalar, parameters, [...stack, token])
      }
    } else throw new Error('表达式缺少数值或参数。')
    for (;;) {
      const op = tokens[cursor], precedence = ({ '+': 1, '-': 1, '*': 2, '/': 2, '**': 4 })[op]
      if (!precedence || precedence < min) break
      addNodes(2)
      cursor++; const right = expression(op === '**' ? precedence : precedence + 1)
      if (op === '**' && Math.abs(right) > 8) throw new Error('指数须在 -8 至 8 之间。')
      result = bounded(op === '+' ? result + right : op === '-' ? result - right : op === '*' ? result * right : op === '/' ? result / right : result ** right)
    }
    depth--; return result
  }
  const result = expression()
  if (cursor !== tokens.length) throw new Error('表达式包含多余内容。')
  return result
}

export function sketchParameterLeaves(value, parameters, found = new Set(), stack = []) {
  if (typeof value !== 'string') return found
  // Validation comes first, so identifiers in invalid input cannot become writes.
  evaluateSketchExpression(value, parameters)
  for (const name of value.match(/(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z][A-Za-z0-9_]*/g) || []) {
    if (!own(parameters, name)) continue
    if (stack.includes(name)) throw new Error('参数存在循环引用。')
    const p = parameters[name]
    if (p && typeof p === 'object' && p.expression) sketchParameterLeaves(p.expression, parameters, found, [...stack, name])
    else if (typeof p === 'string') sketchParameterLeaves(p, parameters, found, [...stack, name])
    else found.add(name)
  }
  return found
}

export function setSketchParameter(parameters, name, value) {
  bounded(value)
  if (!own(parameters, name)) throw new Error(`参数 ${name} 不存在。`)
  const p = parameters[name]
  if (p && typeof p === 'object' && p.expression) throw new Error(`参数 ${name} 由表达式驱动，请修改其关联参数。`)
  return { ...parameters, [name]: p && typeof p === 'object' ? { ...p, value } : value }
}

export function resolveSketchFeature(feature, parameters = {}) {
  const errors = [], point = (value, label) => (value || []).map(scalar => { try { return evaluateSketchExpression(scalar, parameters) } catch (error) { errors.push(`${label}：${error.message}`); return null } })
  const scalar=(value,label)=>{try{return evaluateSketchExpression(value,parameters)}catch(error){errors.push(`${label}：${error.message}`);return null}}
  const contour=(item,label)=>({...item,start:point(item.start,`${label}起点`),segments:(item.segments||[]).map((segment,index)=>({...segment,to:point(segment.to,`${label}边 ${index+1}`),...(segment.type==='arc'?{through:point(segment.through,`${label}圆弧 ${index+1}`)}:{}),...(segment.type==='spline'?{through:(segment.through||[]).map(p=>point(p,`${label}样条 ${index+1}`))}:{}),...(segment.type==='ellipse'?{center:point(segment.center,`${label}椭圆中心`),radii:point(segment.radii,`${label}椭圆半径`),rotation:scalar(segment.rotation,`${label}椭圆方向`),startAngle:scalar(segment.startAngle,`${label}椭圆起角`),endAngle:scalar(segment.endAngle,`${label}椭圆终角`)}:{})}))})
  return {feature:{...contour(feature,''),...(feature.contours?{contours:feature.contours.map((item,index)=>contour(item,`轮廓 ${index+2} `))}:{})},errors:[...new Set(errors)]}
}
