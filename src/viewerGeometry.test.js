import test from 'node:test'
import assert from 'node:assert/strict'
import * as THREE from 'three'
import { clampSupportBaseGeometry, shaftPreviewSegments } from './viewerGeometry.js'
import { validateModelParameters } from './modelValidation.js'

const shaft = { kind: 'shaft', outerDiameter: 24, length: 70, holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40 }
const meshesForShaft = (model) => shaftPreviewSegments(model).map(({ geometry }) => new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ side: THREE.DoubleSide })))
const hits = (meshes, origin, direction) => new THREE.Raycaster(new THREE.Vector3(...origin), new THREE.Vector3(...direction)).intersectObjects(meshes)

test('shaft preview has an open through bore and a real keyway floor that moves with depth', () => {
  for (const keywayDepth of [3, 5]) {
    const model = { ...shaft, keywayDepth }
    assert.equal(validateModelParameters(model).valid, true)
    const meshes = meshesForShaft(model)
    assert.equal(hits(meshes, [-10, 0, 0], [1, 0, 0]).length, 0, 'the bore must pass through both shaft segments')
    const floor = hits(meshes, [20, 0, 30], [0, 0, -1])[0]
    assert.ok(floor)
    assert.ok(Math.abs(floor.point.z - (12 - keywayDepth)) < 1e-6, 'a deeper slot must expose a lower floor, not an unchanged overlay')
    const beyondSlot = hits(meshes, [60, 0, 30], [0, 0, -1])[0]
    assert.ok(Math.abs(beyondSlot.point.z - 12) < 1e-5)
    const box = new THREE.Box3().setFromObject(new THREE.Group().add(...meshes))
    assert.ok(Math.abs(box.max.x - 70) < 1e-6)
    assert.ok(Math.abs(box.min.z + 12) < 1e-4)
  }
})

test('keyway width and length change the removed region instead of decorative surfaces', () => {
  const narrow = meshesForShaft({ ...shaft, keywayWidth: 4, keywayLength: 20 })
  const wide = meshesForShaft({ ...shaft, keywayWidth: 8, keywayLength: 50 })
  assert.ok(hits(narrow, [10, 3, 30], [0, 0, -1])[0].point.z > 11)
  assert.ok(Math.abs(hits(wide, [10, 3, 30], [0, 0, -1])[0].point.z - 9) < 1e-6)
  assert.ok(hits(narrow, [35, 0, 30], [0, 0, -1])[0].point.z > 11)
  assert.ok(Math.abs(hits(wide, [35, 0, 30], [0, 0, -1])[0].point.z - 9) < 1e-6)
  assert.equal(shaftPreviewSegments({ ...shaft, keywayLength: 70 }).length, 1)
})

test('changing a valid clamp base depth 47 to 50 changes its actual footprint', () => {
  const parameters = {
    length: 125, width: 95, thickness: 15, mainDepth: 47, frontExtensionWidth: 80,
    cornerRadius: 8, reliefRadius: 5, tongueRadius: 8, mountHoleDiameter: 12,
    mountHoleDistance: 96, mountHoleCenterFromRear: 40,
  }
  const mesh = (mainDepth) => new THREE.Mesh(clampSupportBaseGeometry({ ...parameters, mainDepth }), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }))
  assert.equal(hits([mesh(47)], [50, -1, 30], [0, 0, -1]).length, 0)
  assert.ok(hits([mesh(50)], [50, -1, 30], [0, 0, -1]).length > 0)
  assert.equal(hits([mesh(50)], [48, 7.5, 30], [0, 0, -1]).length, 0, 'mounting holes remain open')
})
