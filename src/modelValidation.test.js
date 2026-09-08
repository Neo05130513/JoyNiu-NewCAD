import test from 'node:test'
import assert from 'node:assert/strict'
import { canonicalModelKind, validateModelParameters, validationParameterKeys } from './modelValidation.js'

const shaft = { kind: 'shaft', outerDiameter: 24, length: 70, holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40 }
const bracket = { kind: 'bracket', baseLength: 100, baseWidth: 50, baseThickness: 10, upperLength: 70, upperWidth: 50, upperHeight: 30, totalHeight: 40, notchOpening: 40, notchRadius: 15, slotLength: 30, slotWidth: 10, pocketDepth: 10, bossDiameter: 20, bossCenterDistance: 70 }
const clamp = { kind: 'split_clamp_support', baseLength: 125, baseWidth: 95, baseThickness: 15, baseMainDepth: 80, frontTongueWidth: 80, rearBridgeWidth: 86, totalHeight: 75, pedestalOuterRadius: 33, pedestalCenterFromRear: 35, pedestalHeight: 40, rearClampRise: 20, boreDiameter: 36, boreFloorZ: 40, splitWidth: 12, mountHoleCount: 2, mountHoleDiameter: 12, mountHoleCenterDistance: 96, mountHoleCenterFromRear: 40, crossHoleDiameter: 12, crossHoleCenterZ: 55, ribHeight: 20, ribThickness: 10, outerCornerRadius: 8, neckConcaveRadius: 5, neckConvexRadius: 8 }
const nozzle = { kind: 'stepped_tapered_nozzle', mainLength: 98, headLength: 50, neckLength: 20, headLeftDiameter: 54.2544935, headRightDiameter: 56, neckDiameter: 30, tipDiameter: 25, counterboreDiameter: 40, counterboreDepth: 40, axialBoreDiameter: 13, outletDiameter: 17, outletTaperHalfAngle: 15, insertOuterDiameter: 39.4, insertLength: 40, insertThreadDesignation: 'M12', insertAxialOffset: 0 }
const fieldErrors = (model) => validateModelParameters(model).errors.map((error) => error.field)

test('all supported baseline recipes pass and preserve valid zero insert offset', () => {
  for (const model of [shaft, bracket, clamp, nozzle]) assert.equal(validateModelParameters(model).valid, true, model.kind)
  assert.equal(validateModelParameters({ ...nozzle, insertThreadDesignation: 'M12×1.5' }).valid, true)
})

test('empty, nonfinite and boolean dimensions cannot be reported valid', () => {
  for (const value of ['', ' ', null, undefined, NaN, Infinity, true, -1, 0, [70], {}, '0x46']) assert.ok(fieldErrors({ ...shaft, length: value }).includes('length'))
  assert.equal(validateModelParameters({ ...shaft, length: '85' }).valid, true)
  assert.equal(validateModelParameters({ ...shaft, length: '8.5e1' }).valid, true)
})

test('shaft validation rejects holes, slot lengths and cuts that destroy the wall', () => {
  assert.ok(fieldErrors({ ...shaft, holeDiameter: 24 }).includes('holeDiameter'))
  assert.ok(fieldErrors({ ...shaft, keywayLength: 71 }).includes('keywayLength'))
  assert.ok(fieldErrors({ ...shaft, keywayDepth: 7 }).includes('keywayDepth'))
  assert.ok(fieldErrors({ ...shaft, keywayWidth: 20 }).includes('keywayWidth'))
  assert.equal(validateModelParameters({ ...shaft, outerDiameter: 32, length: 85 }).valid, true)
})

test('bracket checks actual upper-body bounds, height stack and side holes', () => {
  assert.ok(fieldErrors({ ...bracket, upperWidth: 20 }).includes('slotLength'))
  assert.ok(fieldErrors({ ...bracket, totalHeight: 41 }).includes('totalHeight'))
  assert.ok(fieldErrors({ ...bracket, notchRadius: 30, notchOpening: 60 }).includes('notchRadius'))
  assert.ok(fieldErrors({ ...bracket, bossCenterDistance: 90 }).includes('bossCenterDistance'))
  assert.ok(fieldErrors({ ...bracket, holeThrough: false }).includes('holeThrough'))
})

test('bracket matches API auxiliary defaults and through-hole semantics', () => {
  for (const patch of [{ holeDepth: null, saddleDepth: null }, { holeThrough: true, holeDepth: 20 }, { holeDepth: 20 }, { saddleDepth: 49.995 }]) {
    assert.equal(validateModelParameters({ ...bracket, ...patch }).valid, true, JSON.stringify(patch))
  }
  assert.ok(fieldErrors({ ...bracket, saddleDepth: 49 }).includes('saddleDepth'))
  for (const field of ['holeDepth', 'saddleDepth', 'bossHeight']) assert.ok(fieldErrors({ ...bracket, [field]: Infinity }).includes(field))
})

test('clamp validation catches unsupported hole counts, colliding holes and impossible fillets', () => {
  assert.ok(fieldErrors({ ...clamp, mountHoleCount: 4 }).includes('mountHoleCount'))
  assert.ok(fieldErrors({ ...clamp, mountHoleCenterDistance: 60 }).includes('mountHoleCenterDistance'))
  assert.ok(fieldErrors({ ...clamp, neckConcaveRadius: 10 }).includes('neckConcaveRadius'))
  assert.ok(fieldErrors({ ...clamp, boreFloorZ: 10 }).includes('boreFloorZ'))
})

test('nozzle validation checks insert fit, bore wall and taper length', () => {
  assert.ok(fieldErrors({ ...nozzle, insertOuterDiameter: 40 }).includes('insertOuterDiameter'))
  assert.ok(fieldErrors({ ...nozzle, insertAxialOffset: 1 }).includes('insertLength'))
  assert.ok(fieldErrors({ ...nozzle, outletTaperHalfAngle: .5 }).includes('outletTaperHalfAngle'))
  assert.ok(fieldErrors({ ...nozzle, insertThreadDesignation: 'M0' }).includes('insertThreadDesignation'))
})

test('valid recipe variants are not blocked by constraints absent from the backend', () => {
  assert.equal(validateModelParameters({ ...clamp, baseLength: 200, ribThickness: 40, mountHoleCenterDistance: 160, rearBridgeWidth: 160 }).valid, true)
  assert.equal(validateModelParameters({ ...nozzle, insertOuterDiameter: 10, insertThreadDesignation: 'M6' }).valid, true)
  assert.equal(validateModelParameters({ ...nozzle, mainLength: 78, outletTaperHalfAngle: 14.036243467926479 }).valid, true)
  assert.ok(fieldErrors({ ...nozzle, mainLength: 77.9, outletTaperHalfAngle: 14.036243467926479 }).includes('outletTaperHalfAngle'))
})

test('units and thread strings respect the API wire constraints', () => {
  for (const units of ['MM', '', null, 'in']) assert.ok(fieldErrors({ ...bracket, units }).includes('units'))
  assert.equal(validateModelParameters({ ...bracket, units: 'mm' }).valid, true)
  assert.ok(fieldErrors({ ...nozzle, insertThreadDesignation: `M12X${'1'.repeat(30)}` }).includes('insertThreadDesignation'))
})

test('aliases are supported but unknown models never silently become shafts', () => {
  assert.equal(canonicalModelKind({ partType: 'unknown', recipeId: 'split_clamp_support_v1' }), 'split_clamp_support')
  assert.equal(canonicalModelKind('stepped_tapered_nozzle_with_insert_v1'), 'stepped_tapered_nozzle')
  const result = validateModelParameters({ ...shaft, kind: 'gear' })
  assert.equal(result.unsupported, true); assert.equal(result.valid, false)
  assert.deepEqual(validationParameterKeys('gear'), [])
  const keys = validationParameterKeys('shaft'); keys.length = 0
  assert.equal(validationParameterKeys('shaft').length, 6)
})
