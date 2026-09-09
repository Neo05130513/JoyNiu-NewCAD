import test from 'node:test'
import assert from 'node:assert/strict'
import * as THREE from 'three'
import {
  archedClevisSupportDefaults as defaults, archedClevisSupportDimensions,
  archedClevisSupportRequiredParameterKeys, validateArchedClevisSupport,
} from './archedClevisSupport.js'
import { archedClevisSupportGeometries } from './viewerGeometry.js'

const meshes = (patch = {}) => archedClevisSupportGeometries({ ...defaults, ...patch })
  .map(({ geometry }) => new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ side: THREE.DoubleSide })))
const hits = (objects, origin, direction) => new THREE.Raycaster(new THREE.Vector3(...origin), new THREE.Vector3(...direction)).intersectObjects(objects)
const near = (actual, expected, tolerance = 0.015) => assert.ok(Math.abs(actual - expected) < tolerance, `${actual} should equal ${expected}`)

test('drawing dimensions derive a 110 × 50 × 55 support and the tangent bridge height', () => {
  assert.equal(validateArchedClevisSupport(defaults).valid, true)
  const d = archedClevisSupportDimensions(defaults)
  assert.deepEqual([d.baseLength, d.baseWidth, d.totalHeight], [110, 50, 55])
  near(d.bridgeHeight, Math.sqrt(559), 1e-10)
  assert.deepEqual(d.earRangesY, [[-25, -15], [15, 25]])
  const box = new THREE.Box3().setFromObject(new THREE.Group().add(...meshes()))
  near(box.min.x, -55); near(box.max.x, 55)
  near(box.min.y, -25); near(box.max.y, 25)
  near(box.min.z, 0); near(box.max.z, 55)
})

test('missing dimensions and impossible geometry produce field-specific validation errors', () => {
  for (const field of archedClevisSupportRequiredParameterKeys) {
    for (const value of [undefined, '', null, true, NaN, Infinity, 0, -1]) {
      const result = validateArchedClevisSupport({ ...defaults, [field]: value })
      assert.equal(result.valid, false, `${field}=${value}`)
      assert.ok(result.errors.some((error) => error.field === field && error.message))
    }
  }
  for (const patch of [
    { archInnerRadius: 28 }, { earRadius: 28 }, { earRadius: 24 }, { earHoleDiameter: 30 },
    { earGap: 31 }, { mountEarRadius: 26 }, { mountHoleDiameter: 30 },
    { mountHoleCenterDistance: 65 }, { earCenterHeight: 28 }, { baseThickness: 25 }, { units: 'cm' },
  ]) {
    assert.equal(validateArchedClevisSupport({ ...defaults, ...patch }).valid, false, JSON.stringify(patch))
    assert.throws(() => archedClevisSupportGeometries({ ...defaults, ...patch }), RangeError)
  }
  const numericStrings = Object.fromEntries(Object.entries(defaults).map(([key, value]) => [key, typeof value === 'number' ? String(value) : value]))
  assert.equal(validateArchedClevisSupport(numericStrings).valid, true)
})

test('the underside arch and the two aligned ear holes are open, with no material in the upper centre gap', () => {
  const model = meshes()
  assert.equal(hits(model, [0, -60, 8], [0, 1, 0]).length, 0, 'R16 passage must stay open across the entire width')
  assert.ok(hits(model, [0, -60, 18], [0, 1, 0]).length > 0, 'arch material exists above the R16 opening')
  assert.equal(hits(model, [0, -60, 40], [0, 1, 0]).length, 0, 'both Ø13 ear holes must be open on the same Y axis')
  assert.ok(hits(model, [10, -60, 40], [0, 1, 0]).length > 0, 'the ear still has material around its hole')
  assert.equal(hits(model, [-60, 0, 40], [1, 0, 0]).length, 0, 'the 30 mm gap contains no upper rectangular slab')
  near(hits(model, [0, 0, 80], [0, 0, -1])[0].point.z, Math.sqrt(559))
  near(hits(model, [0, -20, 80], [0, 0, -1])[0].point.z, 55)
  near(hits(model, [25, 20, 80], [0, 0, -1])[0].point.z, Math.sqrt(28 ** 2 - 25 ** 2))
})

test('mounting ears have rounded R15 ends and separate vertical Ø13 holes through only the base thickness', () => {
  const model = meshes()
  for (const direction of [-1, 1]) {
    assert.equal(hits(model, [40 * direction, 0, 80], [0, 0, -1]).length, 0)
    near(hits(model, [50 * direction, 0, 80], [0, 0, -1])[0].point.z, 9)
    assert.equal(hits(model, [53 * direction, 10, 80], [0, 0, -1]).length, 0, 'outside rounded end must be empty')
    near(hits(model, [49 * direction, 10, 80], [0, 0, -1])[0].point.z, 9)
  }
})

test('valid thick feet do not fill the inner arch where their connector reaches inside its radius', () => {
  const thick = { ...defaults, baseThickness: 23.5 }
  assert.equal(validateArchedClevisSupport(thick).valid, true)
  assert.ok(archedClevisSupportDimensions(thick).baseJoinX < thick.archInnerRadius)
  const model = meshes({ baseThickness: 23.5 })
  for (const x of [-15.5, 15.5]) {
    assert.equal(hits(model, [x, -60, 1], [0, 1, 0]).length, 0, 'feet must be cut by the same R16 underside cylinder')
    near(hits(model, [x, 0, 80], [0, 0, -1])[0].point.z, 23.5, 0.001)
    const fromBelow = hits(model, [x, 0, -10], [0, 0, 1])[0]
    near(fromBelow.point.z, Math.sqrt(16 ** 2 - x ** 2), 0.015)
  }
  assert.equal(hits(model, [40, 0, 80], [0, 0, -1]).length, 0, 'the taller foot keeps its mounting hole open')
})

test('ear and installation hole diameters independently change their real removed regions', () => {
  const original = meshes()
  const widerEarHole = meshes({ earHoleDiameter: 17 })
  const widerMountHole = meshes({ mountHoleDiameter: 17 })
  assert.ok(hits(original, [7, -60, 40], [0, 1, 0]).length > 0)
  assert.equal(hits(widerEarHole, [7, -60, 40], [0, 1, 0]).length, 0)
  assert.ok(hits(widerMountHole, [7, -60, 40], [0, 1, 0]).length > 0)
  assert.ok(hits(original, [47, 0, 80], [0, 0, -1]).length > 0)
  assert.equal(hits(widerMountHole, [47, 0, 80], [0, 0, -1]).length, 0)
  assert.ok(hits(widerEarHole, [47, 0, 80], [0, 0, -1]).length > 0)
})

test('changing the gap, both arch radii and base thickness moves material instead of a label or overlay', () => {
  const original = meshes()
  const widerGap = meshes({ earGap: 34, earThickness: 8 })
  assert.ok(hits(original, [-60, 16, 45], [1, 0, 0]).length > 0)
  assert.equal(hits(widerGap, [-60, 16, 45], [1, 0, 0]).length, 0)
  assert.ok(hits(original, [0, -60, 17], [0, 1, 0]).length > 0)
  assert.equal(hits(meshes({ archInnerRadius: 18 }), [0, -60, 17], [0, 1, 0]).length, 0)
  assert.equal(hits(original, [25, -60, 13], [0, 1, 0]).length, 0)
  assert.ok(hits(meshes({ archOuterRadius: 30 }), [25, -60, 13], [0, 1, 0]).length > 0)
  near(hits(meshes({ baseThickness: 11 }), [50, 0, 80], [0, 0, -1])[0].point.z, 11)
})

test('a valid custom support retains its supplied dimensions without silently clamping to the reference drawing', () => {
  const custom = { archOuterRadius: 30, archInnerRadius: 17, earRadius: 14, earCenterHeight: 44, baseWidth: 60,
    earThickness: 12, earGap: 36, mountEarRadius: 16, mountHoleDiameter: 14, mountHoleCenterDistance: 90, baseThickness: 10 }
  assert.equal(validateArchedClevisSupport({ ...defaults, ...custom }).valid, true)
  const box = new THREE.Box3().setFromObject(new THREE.Group().add(...meshes(custom)))
  near(box.max.x - box.min.x, 122)
  near(box.max.y - box.min.y, 60)
  near(box.max.z, 58)
})
