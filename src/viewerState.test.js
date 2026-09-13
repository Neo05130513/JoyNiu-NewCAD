import test from 'node:test'
import assert from 'node:assert/strict'
import * as THREE from 'three'
import { generationMatchesModel, updateModelView, fitModelView, configureViewerZoom, glbFileRecoveryStatus, VIEWER_MIN_ZOOM, VIEWER_MAX_ZOOM } from './viewerState.js'

const shaft = { kind: 'shaft', outerDiameter: 24, length: 70, holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40 }

test('signed candidate and confirmed GLBs renew expired access without rebuilding geometry', () => {
  for (const status of [403, 404]) {
    for (const error of [{ status }, { response: { status } }, { target: { status } }, new Error(`fetch for model.glb responded with ${status}: Forbidden`)]) {
      assert.equal(glbFileRecoveryStatus(error, { featureModel: true }), status)
    }
  }
  assert.equal(glbFileRecoveryStatus(new Error('responded with a status of 403'), { featureModel: true }), 403)
  assert.equal(glbFileRecoveryStatus({ status: 500, message: 'artifact 404: missing' }, { featureModel: true }), null)
  for (const error of [{ status: 401 }, { status: 500 }, new Error('Failed to fetch'), new Error('model-404-example.glb invalid')]) {
    assert.equal(glbFileRecoveryStatus(error, { featureModel: true }), null)
  }
  assert.equal(glbFileRecoveryStatus({ status: 403 }, { productionReady: true }), null)
  assert.equal(glbFileRecoveryStatus({ status: 404 }, { productionReady: true }), 404)
  assert.equal(glbFileRecoveryStatus({ status: 404 }), null)
})

test('wheel zoom bounds match toolbar limits and keep the full model inside the far clipping plane', () => {
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 5000)
  const runtime = { camera, controls: {}, baseDistance: 180, modelRadius: 40 }
  configureViewerZoom(runtime)
  assert.equal(runtime.baseDistance / runtime.controls.minDistance, VIEWER_MAX_ZOOM)
  assert.equal(runtime.baseDistance / runtime.controls.maxDistance, VIEWER_MIN_ZOOM)
  assert.ok(camera.far > runtime.controls.maxDistance + runtime.modelRadius)
  runtime.baseDistance = 3600
  runtime.modelRadius = 800
  configureViewerZoom(runtime)
  assert.ok(camera.far > runtime.controls.maxDistance + runtime.modelRadius, 'large models stay visible at the minimum toolbar zoom')
})

test('an artifact with old dimensions is rejected even when stale was not set', () => {
  const generated = { parameters: { ...shaft }, partType: 'shaft' }
  assert.equal(generationMatchesModel(shaft, generated), true)
  assert.equal(generationMatchesModel({ ...shaft, length: 90 }, generated), false)
  assert.equal(generationMatchesModel(shaft, { ...generated, stale: true }), false)
  assert.equal(generationMatchesModel(shaft, { ...generated, partType: 'bracket' }), false)
  assert.equal(generationMatchesModel(shaft, { parameters: { length: 70 } }), false)
  assert.equal(generationMatchesModel({ ...shaft, material: 'AL6061' }, generated), true, 'material does not change geometric identity')
})

test('canonical recipe ids and persisted snake case numeric dimensions remain compatible', () => {
  const generated = { recipeId: 'shaft_v1', parameters: Object.fromEntries(Object.entries(shaft).map(([key, value]) => [key.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`), String(value)])) }
  assert.equal(generationMatchesModel(shaft, generated), true)
})

test('same-part dimension edits preserve orbit, distance and pan until explicitly fitted', () => {
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 5000)
  const runtime = { camera, target: new THREE.Vector3(), controls: { target: new THREE.Vector3() }, baseDistance: 180 }
  assert.equal(updateModelView(runtime, new THREE.Vector3(35, 0, 0), 40, 'shaft'), false)
  camera.position.set(100, -160, 95)
  runtime.controls.target.set(38, 5, 2)
  camera.lookAt(runtime.controls.target)
  const distance = camera.position.distanceTo(runtime.controls.target)
  const orientation = camera.quaternion.clone()
  const oldBase = runtime.baseDistance
  assert.equal(updateModelView(runtime, new THREE.Vector3(70, 0, 0), 80, 'shaft'), true)
  assert.ok(Math.abs(camera.position.distanceTo(runtime.controls.target) - distance) < 1e-8)
  assert.deepEqual(camera.quaternion.toArray(), orientation.toArray())
  assert.deepEqual(runtime.controls.target.toArray(), [73, 5, 2])
  assert.equal(runtime.baseDistance, oldBase)
  fitModelView(runtime)
  assert.ok(runtime.baseDistance > oldBase * 1.9)
  const fittedWide = runtime.baseDistance
  camera.aspect = 0.25
  fitModelView(runtime)
  assert.ok(runtime.baseDistance > fittedWide, 'fit after resizing must use the current narrower frustum')
  assert.equal(updateModelView(runtime, new THREE.Vector3(), 30, 'bracket'), false)
  assert.deepEqual(runtime.controls.target.toArray(), [0, 0, 0])
})
