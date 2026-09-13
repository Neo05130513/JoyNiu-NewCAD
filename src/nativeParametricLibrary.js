import {evaluateFormula, solveConstraints, transformEntity} from './nativeDrawingModel.js'

const dimensions = new Set(['length', 'radius', 'angle', 'distance'])
const reserved = new Set(['pi', 'sqrt', 'sin', 'cos', 'tan', 'abs', 'min', 'max'])
const tokens = source => typeof source === 'string' ? source.match(/(?:\d*\.\d+|\d+\.?\d*)(?:[eE][+-]?\d+)?|[A-Za-z_]\w*/g) || [] : []
const clone = value => structuredClone(value)

export function createNativeTemplate({id, name, entities, constraints, parameters}) {
  if (!name.trim()) throw new Error('请填写构件名称')
  if (!entities.length || entities.length > 40 || entities.some(entity => !['LINE', 'CIRCLE', 'ARC', 'LWPOLYLINE', 'TEXT', 'MTEXT'].includes(entity.type) || entity.editable === false)) throw new Error('参数构件请选择 1–40 个可编辑基础图元')
  const ids = new Set(entities.map(entity => entity.id))
  const internal = constraints.filter(constraint => constraint.entities.some(identity => ids.has(identity)))
  if (internal.some(constraint => constraint.entities.some(identity => !ids.has(identity)))) throw new Error('所选图元与外部实体有关联约束，请同时选择相关实体或先解除该约束')
  const required = new Set()
  function include(expression) {
    for (const name of tokens(expression)) if (Object.hasOwn(parameters, name) && !required.has(name)) {
      if (reserved.has(name)) throw new Error(`参数名 ${name} 与公式内置名称冲突，请先重命名该参数`)
      required.add(name)
      include(parameters[name])
    }
  }
  for (const constraint of internal) if (dimensions.has(constraint.type)) {
    evaluateFormula(constraint.value, parameters)
    include(constraint.value)
  }
  if (!required.size) throw new Error('请先在约束面板添加引用参数的长度、半径、角度或距离约束，再保存参数构件')
  const used = Object.fromEntries([...required].map(name => [name, clone(parameters[name])]))
  for (const value of Object.values(used)) evaluateFormula(value, used)
  const mapping = Object.fromEntries(entities.map((entity, index) => [entity.id, `e${index}`]))
  const template = {id, name: name.trim(), version: 2, entities: entities.map(entity => ({...clone(entity), id: mapping[entity.id]})), constraints: internal.map((constraint, index) => ({...clone(constraint), id: `c${index}`, entities: constraint.entities.map(identity => mapping[identity])})), parameters: used}
  solveConstraints(template.entities, template.constraints, template.parameters)
  return template
}

export function prepareNativeTemplate(template, values = {}, translation = [0, 0], instanceId) {
  if (template.version !== 2) throw new Error('该构件是旧版静态几何，请为其补充参数约束并重新保存')
  if (!Array.isArray(translation) || translation.length !== 2 || translation.some(value => !Number.isFinite(value) || Math.abs(value) > 1e7)) throw new Error('插入位置无效')
  if (Object.keys(values).some(key => !Object.hasOwn(template.parameters, key))) throw new Error('构件参数不存在')
  const parameters = {...template.parameters, ...values}
  for (const expression of Object.values(parameters)) evaluateFormula(expression, parameters)
  const solved = solveConstraints(template.entities, template.constraints, parameters)
  const entities = solved.map(entity => transformEntity(entity, {translation}))
  const constraints = template.constraints.map(constraint => ({...clone(constraint), ...(constraint.type === 'fixed' ? {geometry: transformEntity(constraint.geometry || template.entities.find(entity => entity.id === constraint.entities[0]), {translation})} : {})}))
  // Translation preserves horizontal/vertical/angle constraints; rotation and
  // scaling are deliberately separate edit operations requiring a new solve.
  return {op: 'template', templateId: template.id, name: template.name, entities, constraints, parameters, translation: [...translation], ...(instanceId ? {instanceId} : {})}
}
