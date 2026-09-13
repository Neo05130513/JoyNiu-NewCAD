import test from 'node:test'
import assert from 'node:assert/strict'
import { evaluateSketchExpression, resolveSketchFeature, sketchParameterLeaves } from './featureSketchExpressions.js'
import { addSketchConstraint, changeLinkedSketchParameter, driveSketchDimension, moveLinkedSketchPoint, sketchConstraintResiduals, sketchEdgeMeasurement, validateSketchConstraints } from './featureSketchConstraints.js'
import { removeSketchSegment, replaceSketchShape, sketchGeometry, sketchViewBox } from './featureSketchModel.js'
import { editableProfile } from './cadEditorTransactions.js'

const profile = () => ({ id: 'p', op: 'profile_extrude', plane: 'XY', start: [0, 0], segments: [{ type: 'line', to: [20, 0] }, { type: 'line', to: [20, 10] }, { type: 'line', to: [0, 10] }], distance: 'depth' })
const expressed = () => ({ ...profile(), start: ['0', '0'], segments: [{ type: 'line', to: ['width / 2', '0'] }, { type: 'line', to: ['width / 2', 'height'] }, { type: 'line', to: ['0', 'height'] }] })
const parameters = () => ({ width: { value: 40, unit: 'mm', source: '用户确认' }, height: { value: 10 }, depth: { value: 5 } })
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-7, `${actual} ≈ ${expected}`)

test('new anchored rectangles keep exact orthogonal corners and the undriven dimension when any side changes', () => {
  const rectangle = editableProfile({id:'box1',op:'box',size:[80,50,12]})
  for (const edge of [0,1,2,3]) {
    const result = driveSketchDimension(rectangle, edge, edge % 2 ? 55 : 85).feature
    const width = edge % 2 ? 80 : 85, height = edge % 2 ? 55 : 50
    assert.deepEqual([result.start,...result.segments.map(segment=>segment.to)], [[0,0],[width,0],[width,height],[0,height],[0,0]])
    assert.equal(result.distance,12);assert.equal(validateSketchConstraints(result),true)
  }
  const dimensioned=driveSketchDimension(rectangle,0,85).feature
  const before=structuredClone(dimensioned)
  assert.throws(()=>driveSketchDimension(dimensioned,2,90),/冲突/)
  assert.deepEqual(dimensioned,before)
})

test('rectangle formulas keep both driven corners linked and source parameter geometry is never rewritten', () => {
  const rectangle=replaceSketchShape(profile(),'rectangle',{x:3,y:4,width:80,height:50})
  const parameters={W:{value:85,source:'用户尺寸'}}
  const linked=driveSketchDimension(rectangle,0,'W',parameters)
  assert.equal(linked.feature.segments[0].to[0],linked.feature.segments[1].to[0])
  assert.equal(typeof linked.feature.segments[0].to[0],'string')
  const resized=changeLinkedSketchParameter(linked.feature,linked.parameters,'W',90)
  assert.deepEqual(resolveSketchFeature(resized.feature,resized.parameters).feature.segments.map(s=>s.to),[[93,4],[93,54],[3,54],[3,4]])
  const source=editableProfile({id:'box1',op:'box',size:['W','H','D'],origin:['O',4,5]})
  const sourceParameters={W:{value:80},H:{value:50},D:{value:12},O:{value:3}}
  const changed=driveSketchDimension(source,0,85,sourceParameters)
  assert.deepEqual(changed.feature.segments,source.segments)
  assert.deepEqual(changed.feature.origin,['O',4,5]);assert.equal(changed.feature.distance,'D')
  assert.deepEqual(changed.parameters,{...sourceParameters,W:{value:85}})
  const legacy={...profile(),segments:profile().segments.map(s=>({...s,to:[...s.to]}))},legacyBefore=structuredClone(legacy)
  const unforced=editableProfile(legacy)
  assert.deepEqual(unforced,legacyBefore);assert.equal(Object.hasOwn(unforced,'sketchConstraints'),false)
})

test('the fourth rectangle edge supports dimensions and constraints while preserving closed endpoint references', () => {
  const rectangle = replaceSketchShape(profile(), 'rectangle', { x: 2, y: 3, width: 12, height: 7 })
  close(sketchEdgeMeasurement(rectangle, 3).value, 7)
  const resized = driveSketchDimension(rectangle, 3, 11).feature
  close(sketchEdgeMeasurement(resized, 3).value, 11)
  assert.deepEqual(resized.start, resized.segments[3].to)
  resized.start.forEach((value, axis) => close(value, rectangle.start[axis]))
  assert.deepEqual(resized.sketchConstraints.map(c => [c.type, c.edge ?? c.points]), [['horizontal', 0], ['vertical', 1], ['horizontal', 2], ['vertical', 3], ['fixed', ['start']], ['length', 3]])
  assert.equal(validateSketchConstraints(resized), true)
  assert.throws(() => removeSketchSegment(resized, 3), /先移除相关草图约束/)
  const replaced = replaceSketchShape(resized, 'rectangle', { x: 0, y: 0, width: 10, height: 5 })
  assert.equal(replaced.sketchConstraints.length, 5); assert.equal(replaced.segments.length, 4)
  assert.ok(!replaced.sketchConstraints.some(c => c.type === 'length'))
  close(sketchEdgeMeasurement(rectangle, 3).value, 7)
})

test('CAD expression interpreter follows Python power, radians, dependency and bounded arithmetic rules', () => {
  const values = { width: { value: 40 }, half: { expression: 'width/2' } }
  for (const [expression, expected] of [['-2**2', -4], ['2**-2', .25], ['2**2**3', 256], ['sin(radians(30))*half', 10], ['max(2, min(5, 7))', 5]]) close(evaluateSketchExpression(expression, values), expected)
  assert.deepEqual([...sketchParameterLeaves('half + 1e-3', { ...values, e: { value: 20 } })], ['width'])
  for (const bad of ['globalThis.fetch(1)', 'width.constructor', 'width[0]', '1/0', 'sqrt(-1)', '2**9', '2^2', '999999*999999', 'unknown+1', 'Math.sin(1)']) assert.throws(() => evaluateSketchExpression(bad, values))
  assert.throws(() => evaluateSketchExpression('a', { a: { expression: 'b' }, b: { expression: 'a' } }), /循环/)
})

test('complete expression profile renders all edges and true bounds without rewriting any source scalar', () => {
  const feature = expressed(), before = JSON.stringify(feature), geometry = sketchGeometry(feature, parameters())
  assert.equal(geometry.edges.length, 3); assert.deepEqual(geometry.warnings, [])
  assert.equal(geometry.edges[0].d, 'M 0 0 L 20 0')
  const bounds = sketchViewBox(feature, parameters()); assert.ok(bounds[0] <= 0 && bounds[0] + bounds[2] > 20)
  assert.equal(JSON.stringify(feature), before)
  const unresolved = resolveSketchFeature(feature, {})
  assert.ok(unresolved.errors.some(error => error.includes('width')))
  assert.equal(unresolved.feature.segments[0].to[0], null)
})

test('drag and length dimension drive the independent parameter while preserving all original expressions and provenance', () => {
  const feature = expressed(), p = parameters(), moved = moveLinkedSketchPoint(feature, '0:to', [25, 0], p)
  close(moved.parameters.width.value, 50); assert.deepEqual(moved.feature, feature)
  assert.deepEqual(moved.parameters.width, { ...p.width, value: moved.parameters.width.value })
  assert.equal(p.width.value, 40)
  const dimensioned = driveSketchDimension(feature, 0, 35, p)
  close(dimensioned.parameters.width.value, 70)
  assert.deepEqual(dimensioned.feature.segments, feature.segments)
  assert.equal(dimensioned.feature.sketchConstraints.length, 1)
  assert.equal(validateSketchConstraints(dimensioned.feature, dimensioned.parameters), true)
  const changed = changeLinkedSketchParameter(dimensioned.feature, dimensioned.parameters, 'width', 80)
  close(sketchEdgeMeasurement(changed.feature, 0, changed.parameters).value, 40)
  const changedAgain = driveSketchDimension(dimensioned.feature, 0, 25, dimensioned.parameters)
  assert.equal(changedAgain.feature.sketchConstraints.length, 1)
  close(changedAgain.parameters.width.value, 50)
})

test('horizontal and vertical constraints are solved and preserved during connected-point movement', () => {
  const source = profile(); source.segments[0].to = [20, 2]
  const horizontal = addSketchConstraint(source, 'horizontal', { edge: 0 })
  close(horizontal.feature.start[1], horizontal.feature.segments[0].to[1])
  const vertical = addSketchConstraint(horizontal.feature, 'vertical', { edge: 1 })
  const moved = moveLinkedSketchPoint(vertical.feature, '0:to', [30, 5])
  close(moved.feature.start[1], 5); close(moved.feature.segments[1].to[0], 30)
  assert.ok(sketchConstraintResiduals(moved.feature).every(n => Math.abs(n) < 1e-7))
  assert.deepEqual(source.start, [0, 0]); assert.deepEqual(source.segments[0].to, [20, 2])
})

test('fixed, duplicate and conflicting constraints reject atomically; deletion never leaves dangling references', () => {
  const horizontal = addSketchConstraint(profile(), 'horizontal', { edge: 0 }).feature
  assert.throws(() => addSketchConstraint(horizontal, 'horizontal', { edge: 0 }), /重复.*过约束/)
  assert.throws(() => addSketchConstraint(horizontal, 'vertical', { edge: 0 }), /零长度|冲突|过约束/)
  const fixed = addSketchConstraint(horizontal, 'fixed', { points: ['0:to'] }).feature, before = JSON.stringify(fixed)
  assert.throws(() => moveLinkedSketchPoint(fixed, '0:to', [21, 3]), /冲突|过约束/)
  assert.equal(JSON.stringify(fixed), before)
  assert.throws(() => removeSketchSegment(fixed, 0), /先移除/)
  const replaced = replaceSketchShape(fixed, 'circle', { x: 0, y: 0, radius: 3 })
  assert.deepEqual(replaced.sketchConstraints.map(item=>item.type), ['equal','concentric'])
  assert.equal(replaced.sketchConstraints.some(item=>horizontal.sketchConstraints.some(old=>old.id===item.id)),false)
})

test('coincident constraints connect two chosen vertices and reject a zero-length adjacent edge', () => {
  const feature = profile()
  feature.segments.push({ type: 'line', to: [-1, 1] })
  const coincident = addSketchConstraint(feature, 'coincident', { points: ['start', '3:to'] }).feature
  close(coincident.start[0], coincident.segments[3].to[0]); close(coincident.start[1], coincident.segments[3].to[1])
  assert.throws(() => addSketchConstraint(feature, 'coincident', { points: ['start', '0:to'] }), /零长度/)
})

test('ambiguous multi-parameter dimensions cannot silently choose a parameter; explicit parameter edits remain available', () => {
  const feature = expressed(); feature.segments[0].to[0] = 'width + height'
  assert.throws(() => moveLinkedSketchPoint(feature, '0:to', [55, 0], parameters()), /多个参数/)
  assert.throws(() => driveSketchDimension(feature, 0, 55, parameters()), /多个参数/)
  const changed = changeLinkedSketchParameter(feature, parameters(), 'width', '45')
  close(sketchEdgeMeasurement(feature, 0, changed.parameters).value, 55)
})

test('dimension expressions link numeric geometry to parameters and circle radius changes preserve both circular arcs', () => {
  const feature = profile(), p = parameters(), linked = driveSketchDimension(feature, 0, 'width', p)
  assert.equal(typeof linked.feature.segments[0].to[0], 'string')
  close(sketchEdgeMeasurement(linked.feature, 0, { ...p, width: { value: 55 } }).value, 55)
  assert.equal(validateSketchConstraints(linked.feature, { ...p, width: { value: 55 } }), true)
  const circle = replaceSketchShape(feature, 'circle', { x: 10, y: 20, radius: 4 })
  const resized = driveSketchDimension(circle, 0, 8, p)
  for (const index of [0, 1]) { const measurement = sketchEdgeMeasurement(resized.feature, index); close(measurement.value, 8); assert.deepEqual(measurement.arc.center, [10, 20]) }
  assert.deepEqual(resized.feature.start, resized.feature.segments.at(-1).to)
  const radiusParameter = driveSketchDimension(circle, 0, 'depth', p)
  for (const index of [0, 1]) close(sketchEdgeMeasurement(radiusParameter.feature, index, { ...p, depth: { value: 7 } }).value, 7)
})
