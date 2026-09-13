import test from 'node:test'
import assert from 'node:assert/strict'
import { evaluateFormula, PROFESSIONAL_SYMBOLS, professionalSymbol, solveConstraints, transformEntity } from './nativeDrawingModel.js'

const line = (id, start, end) => ({ id, type: 'LINE', start, end })
const close = (a, b, tolerance = 1e-4) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`)

test('formula parameters resolve recursively while cycles and executable expressions fail', () => {
  close(evaluateFormula('pitch * count + 2 * margin', { pitch: '12.5', count: '4', margin: 'pitch / 5' }), 55)
  close(evaluateFormula('sqrt(9) + sin(pi / 2)', {}), 4)
  assert.throws(() => evaluateFormula('a', { a: 'b', b: 'a' }), /循环/)
  assert.throws(() => evaluateFormula('globalThis.process.exit()'), /不支持|未知/)
  assert.throws(() => evaluateFormula('1 / 0'), /无效/)
  close(evaluateFormula('-2^2'), -4)
  close(evaluateFormula('2^-2'), 0.25)
  close(evaluateFormula('2^3^2'), 512)
  close(evaluateFormula('(-2)^2'), 4)
})

test('horizontal and formula-driven length are solved together without modifying input', () => {
  const entities = [line('a', [0, 0, 0], [10, 2, 0])], original = structuredClone(entities)
  const constraints = [{ type: 'horizontal', entities: ['a'] }, { type: 'length', entities: ['a'], value: 'width * 2' }]
  const solved = solveConstraints(entities, constraints, { width: '30' })[0]
  close(solved.start[1], solved.end[1])
  close(Math.hypot(solved.end[0] - solved.start[0], solved.end[1] - solved.start[1]), 60)
  assert.deepEqual(entities, original)
})

test('fixed geometry prevents an incompatible length from being silently accepted', () => {
  const entity = line('a', [0, 0], [10, 0])
  assert.throws(() => solveConstraints([entity], [{ type: 'fixed', entities: ['a'], geometry: structuredClone(entity) }, { type: 'length', entities: ['a'], value: 30 }]), /冲突|收敛/)
})

test('mixed length/radius and degenerate directional relations are rejected', () => {
  const a = line('a', [0, 0], [10, 0]), b = { id: 'b', type: 'CIRCLE', center: [0, 20], radius: 10 }
  assert.throws(() => solveConstraints([a, b], [{ type: 'equal', entities: ['a', 'b'] }]), /两条直线|等半径/)
  const zero = line('z', [0, 0], [0, 0])
  for (const type of ['parallel', 'perpendicular']) assert.throws(() => solveConstraints([a, zero], [{ type, entities: ['a', 'z'] }]), /零长度/)
  assert.throws(() => solveConstraints([zero], [{ type: 'angle', entities: ['z'], value: 0 }]), /零长度/)
})

test('mirroring bulged polylines reverses arc handedness and keeps source intact', () => {
  const entity = { type: 'LWPOLYLINE', points: [[0, 0, 1], [10, 0, 0]], closed: false }
  const mirrored = transformEntity(entity, { mirror: 'y', translation: [20, 0] })
  assert.deepEqual(mirrored.points, [[20, 0, -1], [10, 0, -0]])
  assert.deepEqual(entity.points, [[0, 0, 1], [10, 0, 0]])
})

test('each professional symbol produces editable geometry at the requested origin', () => {
  assert.equal(PROFESSIONAL_SYMBOLS.length, 20)
  for (const symbol of PROFESSIONAL_SYMBOLS) {
    const entities = professionalSymbol(symbol.id, symbol.width, symbol.height, [37, 61])
    assert.ok(entities.length > 0, symbol.id)
    assert.ok(entities.every(entity => ['LINE', 'CIRCLE', 'ARC', 'LWPOLYLINE', 'TEXT'].includes(entity.type)), symbol.id)
    assert.ok(!JSON.stringify(entities).includes('null'), symbol.id)
  }
})

test('fit bounds accepts a large imported drawing without JS argument stack overflow',async()=>{
  const {drawingBounds}=await import('./nativeDrawingModel.js')
  const entities=Array.from({length:60000},(_,i)=>({type:'CIRCLE',center:[i,0],radius:2}))
  const bounds=drawingBounds(entities)
  assert.ok(bounds.x< -2&&bounds.x+bounds.width>60001)
  assert.ok(Number.isFinite(bounds.width))
})
