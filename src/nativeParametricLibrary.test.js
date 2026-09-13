import test from 'node:test'
import assert from 'node:assert/strict'
import {createNativeTemplate, prepareNativeTemplate} from './nativeParametricLibrary.js'
import {solveConstraints} from './nativeDrawingModel.js'

const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-4, `${actual} != ${expected}`)
const line = (id, start, end) => ({id, type: 'LINE', editable: true, start: [...start, 0], end: [...end, 0]})
const rectangle = () => {
  const entities = [line('a', [0, 0], [10, 0]), line('b', [10, 0], [10, 20]), line('c', [10, 20], [0, 20]), line('d', [0, 20], [0, 0])]
  const constraints = entities.map((entity, i) => ({type: i % 2 ? 'vertical' : 'horizontal', entities: [entity.id]}))
  constraints.push({type: 'length', entities: ['a'], value: 'width'}, {type: 'length', entities: ['b'], value: 'height'})
  for (let i = 0; i < 4; i++) constraints.push({type: 'coincident', entities: [entities[i].id, entities[(i + 1) % 4].id], ends: [1, 0]})
  return createNativeTemplate({id: 'rectangle', name: '参数矩形', entities, constraints, parameters: {width: 10, height: 20, unrelated: 123}})
}
test('parameter templates solve independent rectangle width and height and keep connected corners', () => {
  const template = rectangle(), copy = structuredClone(template)
  const prepared = prepareNativeTemplate(template, {width: 40, height: 8}, [30, 60])
  close(Math.hypot(prepared.entities[0].end[0] - prepared.entities[0].start[0], prepared.entities[0].end[1] - prepared.entities[0].start[1]), 40)
  close(Math.hypot(prepared.entities[1].end[0] - prepared.entities[1].start[0], prepared.entities[1].end[1] - prepared.entities[1].start[1]), 8)
  for (let i = 0; i < 4; i++) {
    const end = prepared.entities[i].end, next = prepared.entities[(i + 1) % 4].start
    close(end[0], next[0]); close(end[1], next[1])
  }
  assert.deepEqual(template, copy)
  assert.deepEqual(Object.keys(template.parameters), ['width', 'height'])
  assert.deepEqual(solveConstraints(prepared.entities, prepared.constraints, prepared.parameters), prepared.entities)
})
test('formula dependencies and fixed reference geometry survive translation and independent line-circle edits', () => {
  const datum = {id: 'datum', type: 'CIRCLE', center: [0, 0, 0], radius: 1}
  const entities = [datum, line('line', [0, 0], [10, 0]), {id: 'circle', type: 'CIRCLE', center: [0, 0, 0], radius: 5}]
  const constraints = [{type: 'fixed', entities: ['datum'], geometry: datum}, {type: 'coincident', entities: ['line', 'datum'], ends: [0, 0]}, {type: 'horizontal', entities: ['line']}, {type: 'length', entities: ['line'], value: 'width'}, {type: 'concentric', entities: ['datum', 'circle']}, {type: 'radius', entities: ['circle'], value: 'radius'}]
  const template = createNativeTemplate({id: 'line-circle', name: '线圆', entities, constraints, parameters: {width: 10, diameter: 10, radius: 'diameter / 2', unrelated: 1}})
  const prepared = prepareNativeTemplate(template, {width: 45, diameter: 6}, [100, 200], 'instance_1')
  close(prepared.entities[1].end[0], 145)
  close(prepared.entities[2].radius, 3)
  assert.deepEqual(prepared.constraints[0].geometry.center, [100, 200, 0])
  assert.equal(prepared.instanceId, 'instance_1')
  assert.deepEqual(solveConstraints(prepared.entities, prepared.constraints, prepared.parameters), prepared.entities)
})
test('parameter library rejects external constraints, static geometry, invalid formulas and conflicting values', () => {
  const entity = line('a', [0, 0], [10, 0])
  assert.throws(() => createNativeTemplate({id: 't', name: 't', entities: [entity], constraints: [{type: 'coincident', entities: ['a', 'outside']}], parameters: {width: 10}}), /外部/)
  assert.throws(() => createNativeTemplate({id: 't', name: 't', entities: [entity], constraints: [], parameters: {width: 10}}), /引用参数/)
  assert.throws(() => prepareNativeTemplate(rectangle(), {width: 'globalThis.process.exit()'}))
  assert.throws(() => prepareNativeTemplate(rectangle(), {width: -10}), /大于零/)
  assert.throws(() => prepareNativeTemplate(rectangle(), {unknown: 20}), /不存在/)
  assert.throws(() => createNativeTemplate({id: 't', name: 't', entities: [entity], constraints: [{type: 'length', entities: ['a'], value: 'pi'}], parameters: {pi: 10}}), /内置名称冲突/)
})
