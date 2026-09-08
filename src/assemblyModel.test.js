import test from 'node:test'
import assert from 'node:assert/strict'
import { Euler, Vector3 } from 'three'
import {
  applyPartTransform, assemblyFingerprint, boundsOverlap, checkAssembly,
  createPartInstance, duplicatePartInstance, initialPartPosition, instanceBounds,
  mainModelEnvelope, rotateVector, standardParts, validatePartDefinition, validateTransform,
} from './assemblyModel.js'

const shaft = { kind: 'shaft', name: '自定义动力轴', outerDiameter: 24, length: 70 }
const bearing = (catalogId = 'bearing-6204', position = { x: 35, y: 0, z: 0 }, rotation = { x: 0, y: 0, z: 0 }) => createPartInstance(standardParts.find((part) => part.catalogId === catalogId), { position, rotation })

test('catalog inserts have independent IDs, dimension snapshots, and persisted provenance', () => {
  const first = bearing(); const second = bearing()
  assert.notEqual(first.id, second.id)
  assert.equal(first.catalogId, 'bearing-6204')
  assert.equal(first.source, '内置公称尺寸 · 6204')
  assert.equal(first.units, 'mm')
  assert.deepEqual(first.dimensions, { innerDiameter: 20, outerDiameter: 47, length: 14 })
  first.dimensions.length = 99
  assert.equal(second.dimensions.length, 14)
  assert.equal(standardParts[0].dimensions.length, 14)
  for (const part of standardParts) assert.deepEqual(validatePartDefinition(part), [])
})

test('custom shapes reject impossible holes and non-finite, missing or blank dimensions', () => {
  const custom = { name: '隔套', shape: 'ring', dimensions: { outerDiameter: 20, innerDiameter: 10, length: 30 } }
  assert.deepEqual(validatePartDefinition(custom), [])
  for (const innerDiameter of [0, 20, 25, -1, Infinity, '', null]) assert.ok(validatePartDefinition({ ...custom, dimensions: { ...custom.dimensions, innerDiameter } }).length)
  for (const length of [0, -1, NaN, Infinity, '', ' ', null, undefined, true]) assert.throws(() => createPartInstance({ ...custom, dimensions: { ...custom.dimensions, length } }))
  assert.throws(() => createPartInstance({ ...custom, name: '  ' }))
  const solid = createPartInstance({ ...custom, shape: 'cylinder', dimensions: { outerDiameter: '20', innerDiameter: 0, length: '30' } })
  assert.equal(solid.dimensions.outerDiameter, 20)
  assert.equal(solid.source, '用户自定义尺寸')
})

test('editing a transform accepts zero and negative coordinates without changing the original', () => {
  const original = bearing()
  const moved = applyPartTransform(original, { x: '0', y: '-15.5', z: '20' }, { x: 0, y: -90, z: 180 })
  assert.equal(moved.id, original.id)
  assert.deepEqual(moved.position, { x: 0, y: -15.5, z: 20 })
  assert.deepEqual(original.position, { x: 35, y: 0, z: 0 })
  for (const x of ['', null, undefined, NaN, Infinity, 1000001]) assert.ok(validateTransform({ x, y: 0, z: 0 }, { x: 0, y: 0, z: 0 }).length)
  assert.throws(() => applyPartTransform(original, { x: 0, y: '', z: 0 }, original.rotation))
})

test('rotated world bounds account for dimensions, translation, and Euler rotation', () => {
  const item = createPartInstance({ name: '圆柱', shape: 'cylinder', dimensions: { outerDiameter: 10, length: 40 } }, { position: { x: 5, y: 15, z: -2 }, rotation: { x: 0, y: 0, z: 90 } })
  const bounds = instanceBounds(item)
  assert.ok(Math.abs(bounds.min.x - 0) < 1e-9)
  assert.ok(Math.abs(bounds.max.x - 10) < 1e-9)
  assert.ok(Math.abs(bounds.min.y - -5) < 1e-9)
  assert.ok(Math.abs(bounds.max.y - 35) < 1e-9)
  assert.deepEqual([bounds.min.z, bounds.max.z], [-7, 3])
  const vector = rotateVector({ x: 1, y: 0, z: 0 }, { x: 0, y: 90, z: 0 })
  assert.ok(Math.abs(vector.z + 1) < 1e-9)
  const point = { x: 13, y: -9, z: 4 }
  const rotation = { x: 32, y: -74, z: 127 }
  const expected = new Vector3(point.x, point.y, point.z).applyEuler(new Euler(...Object.values(rotation).map((degrees) => degrees * Math.PI / 180), 'XYZ'))
  const actual = rotateVector(point, rotation)
  for (const axis of ['x', 'y', 'z']) assert.ok(Math.abs(actual[axis] - expected[axis]) < 1e-9)
})

test('bolt bounds include the head outside the shaft length', () => {
  const bolt = createPartInstance(standardParts.find((item) => item.catalogId === 'socket-m8-30'))
  const bounds = instanceBounds(bolt)
  assert.deepEqual(bounds, { min: { x: -23, y: -6.5, z: -6.5 }, max: { x: 15, y: 6.5, z: 6.5 } })
})

test('all four main model families use their own envelopes and unsupported kinds are explicit', () => {
  const cases = [
    [shaft, [0, 70, -12, 12]],
    [{ kind: 'bracket', baseLength: 100, baseWidth: 50, totalHeight: 40 }, [-50, 50, 0, 40]],
    [{ kind: 'circular_clamp_v1', baseLength: 125, baseWidth: 95, totalHeight: 75 }, [-62.5, 62.5, 0, 75]],
    [{ kind: 'stepped_tapered_nozzle_with_insert_v1', mainLength: 98, headLeftDiameter: 54.2, headRightDiameter: 56, neckDiameter: 30, tipDiameter: 25 }, [0, 98, -28, 28]],
  ]
  for (const [model, expected] of cases) {
    const box = mainModelEnvelope(model)
    assert.deepEqual([box.min.x, box.max.x, box.min.z, box.max.z], expected)
  }
  assert.equal(mainModelEnvelope({ ...shaft, kind: 'unknown' }), null)
  assert.equal(mainModelEnvelope({ ...shaft, length: Infinity }), null)
})

test('initial placement clears both the current model and already inserted parts', () => {
  const part = standardParts[0]
  const first = createPartInstance(part, { position: initialPartPosition(shaft, [], part) })
  const second = createPartInstance(part, { position: initialPartPosition(shaft, [first], part) })
  assert.equal(boundsOverlap(instanceBounds(first), mainModelEnvelope(shaft)), false)
  assert.equal(boundsOverlap(instanceBounds(second), instanceBounds(first)), false)
  const copy = duplicatePartInstance(first)
  assert.notEqual(copy.id, first.id)
  assert.equal(copy.catalogId, first.catalogId)
  assert.equal(boundsOverlap(instanceBounds(copy), instanceBounds(first)), false)
})

test('coaxial shaft/ring checks identify too-small bores and actual nominal radial gaps', () => {
  const small = checkAssembly(shaft, [bearing()])
  assert.equal(small.fits[0].status, 'too-small')
  assert.equal(small.fits[0].radialClearance, -2)
  assert.ok(small.issues.some((issue) => issue.code === 'shaft-fit' && issue.severity === 'error'))
  const loose = checkAssembly(shaft, [bearing('bearing-6205')])
  assert.equal(loose.fits[0].status, 'clearance')
  assert.equal(loose.fits[0].radialClearance, 0.5)
  assert.ok(loose.issues.some((issue) => issue.code === 'envelope-overlap'))
  assert.equal(loose.issues.some((issue) => issue.severity === 'error'), false)
  assert.match(loose.scope, /未进行实体布尔干涉/)
})

test('off-axis, rotated, or axially separated rings never report a solved coaxial fit', () => {
  for (const [position, rotation, status] of [
    [{ x: 35, y: 5, z: 0 }, { x: 0, y: 0, z: 0 }, 'unaligned'],
    [{ x: 35, y: 0, z: 0 }, { x: 0, y: 0, z: 90 }, 'unaligned'],
    [{ x: 100, y: 0, z: 0 }, { x: 0, y: 0, z: 0 }, 'separated'],
  ]) {
    const report = checkAssembly(shaft, [bearing('bearing-6204', position, rotation)])
    assert.equal(report.fits[0].status, status)
    assert.equal(report.issues.some((issue) => issue.code === 'shaft-fit'), false)
  }
})

test('pairwise instance overlap is measured and reports become stale when any saved transform changes', () => {
  const first = bearing('bearing-6204', { x: 150, y: 0, z: 0 })
  const second = bearing('bearing-6205', { x: 150, y: 0, z: 0 })
  const items = [first, second]
  const report = checkAssembly(shaft, items)
  assert.ok(report.issues.some((issue) => issue.code === 'instance-overlap'))
  assert.equal(report.fingerprint, assemblyFingerprint(shaft, items))
  const moved = applyPartTransform(second, { x: 150, y: 80, z: 0 }, second.rotation)
  assert.notEqual(report.fingerprint, assemblyFingerprint(shaft, [first, moved]))
  assert.notEqual(report.fingerprint, assemblyFingerprint({ ...shaft, outerDiameter: 25 }, items))
  assert.equal(checkAssembly(shaft, [first, moved]).issues.some((issue) => issue.code === 'instance-overlap'), false)
})

test('corrupt instance dimensions and duplicate IDs are rejected without a false clean report', () => {
  const good = bearing()
  const report = checkAssembly(shaft, [good, { ...good, dimensions: { ...good.dimensions, innerDiameter: 500 } }])
  assert.ok(report.issues.some((issue) => issue.code === 'invalid-instance' && issue.severity === 'error'))
  const invalidModel = checkAssembly({ ...shaft, kind: 'other' }, [])
  assert.equal(invalidModel.issues[0].code, 'model-envelope')
})
