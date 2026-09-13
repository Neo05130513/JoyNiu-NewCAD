import * as THREE from 'three'
import { canonicalModelKind, validationParameterKeys } from './modelValidation.js'
import { cadGenerationIsCurrent, isFeatureModel } from './cadAgentState.js'

export const VIEWER_MIN_ZOOM = 0.1
export const VIEWER_MAX_ZOOM = 10

// GLTFLoader puts HTTP status in its message; other loaders expose a Response.
// Signed CAD previews may expire before the model is confirmed for delivery.
export function glbFileRecoveryStatus(error, { featureModel = false, productionReady = false } = {}) {
  const explicit = Number(error?.target?.status || error?.response?.status || error?.status)
  const detail = error?.message || ''
  const status = explicit || Number(detail.match(/responded with (?:a status of )?(403|404)\b|\b(403|404)\s*(?::|Forbidden|Not Found)/i)?.slice(1).find(Boolean))
  return featureModel && [403, 404].includes(status) || productionReady && status === 404 ? status : null
}

export function configureViewerZoom(runtime) {
  if (!(runtime.baseDistance > 0)) return
  runtime.controls.minDistance = runtime.baseDistance / VIEWER_MAX_ZOOM
  runtime.controls.maxDistance = runtime.baseDistance / VIEWER_MIN_ZOOM
  runtime.camera.far = Math.max(runtime.controls.maxDistance + (runtime.modelRadius || 1) * 2, 1000)
  runtime.camera.updateProjectionMatrix()
}

// Both generation APIs return camelCase parameters (Pydantic by_alias=True).
// Accept snake_case persisted responses as well, but do not present an old
// artifact as current when its dimensions no longer describe the model.
export function generationMatchesModel(model, generation) {
  if (isFeatureModel(model)) return cadGenerationIsCurrent(model, generation)
  if (!generation || generation.stale || !generation.parameters) return false
  const generatedKind = canonicalModelKind(generation)
  if (generatedKind && generatedKind !== canonicalModelKind(model)) return false
  const generated = Object.fromEntries(Object.entries(generation.parameters).map(([key, value]) => [key.replace(/_([a-z])/g, (_, char) => char.toUpperCase()), value]))
  const keys = validationParameterKeys(model)
  return keys.length > 0 && keys.every((key) => {
    const actual = generated[key], expected = model[key]
    if (actual === undefined || actual === null || actual === '' || expected === undefined || expected === null || expected === '') return false
    if (key === 'insertThreadDesignation') return String(actual).toUpperCase().replace('×', 'X').replace(/\s/g, '') === String(expected).toUpperCase().replace('×', 'X').replace(/\s/g, '')
    const a = Number(actual), b = Number(expected)
    return Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= Math.max(1, Math.abs(a), Math.abs(b)) * 1e-8
  })
}

export function fitDistance(camera, radius, margin = 1.14) {
  const verticalHalfAngle = THREE.MathUtils.degToRad(camera.fov) / 2
  const horizontalHalfAngle = Math.atan(Math.tan(verticalHalfAngle) * Math.max(camera.aspect, 0.01))
  return radius * margin / Math.sin(Math.max(0.01, Math.min(verticalHalfAngle, horizontalHalfAngle)))
}

// Preserve scale, orbit and pan during a dimension edit. Translate the view
// with the new model center so extending an asymmetric part stays centered.
// A new recipe or first load gets a fit; an explicit reset uses fitModelView.
export function updateModelView(runtime, center, radius, identity) {
  const preserve = runtime.modelIdentity === identity && runtime.baseDistance > 0
  const translation = center.clone().sub(runtime.target)
  runtime.modelRadius = radius
  runtime.fitBaseDistance = Math.max(radius * 4.2, fitDistance(runtime.camera, radius))
  if (preserve) {
    runtime.camera.position.add(translation)
    runtime.controls.target.add(translation)
  } else {
    runtime.baseDistance = runtime.fitBaseDistance
    runtime.controls.target.copy(center)
  }
  runtime.target.copy(center)
  runtime.modelIdentity = identity
  return preserve
}

export function fitModelView(runtime) {
  if (runtime.modelRadius > 0) runtime.fitBaseDistance = Math.max(runtime.modelRadius * 4.2, fitDistance(runtime.camera, runtime.modelRadius))
  if (runtime.fitBaseDistance > 0) runtime.baseDistance = runtime.fitBaseDistance
}
