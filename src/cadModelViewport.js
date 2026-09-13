import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'

export const CAD_VIEW_NAMES = { iso: '等轴测', top: '上视图', front: '前视图', right: '右视图', custom: '自由视角' }
export const CAD_VIEW_COLORS = { body: '#b3bcea', edge: '#586078', hover: '#b6d7cd', selected: '#64bcb3', edgeHover: '#327e73', edgeSelected: '#087f75' }
const hex = /^[a-f0-9]{64}$/
const vector = value => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite)
const fail = message => { throw new Error(`实体预览数据不完整：${message}`) }

export function cadSelectionKey(value) {
  return value && ['face', 'edge'].includes(value.kind) ? `${value.kind}:${value.id || value.index}` : ''
}

export function cadPlaneForFace(face) {
  if (!face?.planar || !vector(face.origin) || !vector(face.normal) || !vector(face.xAxis)) return null
  const normal = new THREE.Vector3(...face.normal), xDir = new THREE.Vector3(...face.xAxis)
  if (normal.lengthSq() < 1e-16 || xDir.lengthSq() < 1e-16) return null
  normal.normalize(); xDir.normalize()
  if (Math.abs(normal.dot(xDir)) > 1e-5) return null
  return { origin: [...face.origin], normal: normal.toArray(), xDir: xDir.toArray() }
}

function selectorMatches(selector, kind, index, payload) {
  return selector?.kind === kind && selector.index === index && selector.geometryVersion === payload.geometryVersion
    && selector.sourceFeatureId === payload.sourceFeatureId && hex.test(selector.signature)
}

/** Validate a single BRep tessellation before it can provide editing references. */
export function validateCadViewportGeometry(payload) {
  if (payload?.valid !== true || !hex.test(payload.geometryVersion) || typeof payload.sourceFeatureId !== 'string') fail('缺少当前实体版本。')
  const { mesh, faces, edges } = payload
  if (!mesh || !Array.isArray(mesh.positions) || mesh.positions.length < 9 || mesh.positions.length % 3 || mesh.positions.length > 3_000_000 || !mesh.positions.every(Number.isFinite)) fail('面片坐标无效。')
  const vertices = mesh.positions.length / 3
  if (mesh.normals !== undefined && (!Array.isArray(mesh.normals) || mesh.normals.length !== mesh.positions.length || !mesh.normals.every(Number.isFinite))) fail('面片法向无效。')
  if (!Array.isArray(mesh.indices) || !mesh.indices.length || mesh.indices.length % 3 || mesh.indices.length > 900_000 || !mesh.indices.every(index => Number.isSafeInteger(index) && index >= 0 && index < vertices)) fail('面片索引无效。')
  if (!Array.isArray(mesh.faceIds) || mesh.faceIds.length !== mesh.indices.length / 3) fail('面片与 CAD 面无法对应。')
  if (!Array.isArray(faces) || !faces.length || faces.length > 20_000 || !Array.isArray(edges) || edges.length > 40_000) fail('拓扑数量无效。')
  const ids = new Set()
  for (const [index, face] of faces.entries()) {
    if (face?.id !== `face:${index}` || ids.has(face.id) || !selectorMatches(face.selector, 'face', index, payload)) fail('面引用与当前实体不一致。')
    if (face.planar && !cadPlaneForFace(face)) fail('平面坐标系无效。')
    ids.add(face.id)
  }
  if (mesh.faceIds.some(id => !ids.has(id))) fail('面片引用了不存在的面。')
  let edgeCoordinates = 0
  for (const [index, edge] of edges.entries()) {
    if (edge?.id !== `edge:${index}` || !selectorMatches(edge.selector, 'edge', index, payload)) fail('边引用与当前实体不一致。')
    if (!Array.isArray(edge.points) || edge.points.length < 6 || edge.points.length % 3 || !edge.points.every(Number.isFinite)) fail('边曲线无效。')
    edgeCoordinates += edge.points.length
    if (edgeCoordinates > 1_800_000) fail('边曲线超出预览大小限制。')
  }
  return payload
}

/** Render the same triangles and edge curves that supply the picked selectors. */
export function createCadTopologyScene(payload, { selectionOnly = false } = {}) {
  validateCadViewportGeometry(payload)
  const group = new THREE.Group(), geometries = [], materials = []
  const faceVertices = new Map(payload.faces.map(face => [face.id, []]))
  const positions = new Float32Array(payload.mesh.indices.length * 3)
  for (let i = 0; i < payload.mesh.indices.length; i++) {
    const source = payload.mesh.indices[i] * 3
    positions.set(payload.mesh.positions.slice(source, source + 3), i * 3)
    faceVertices.get(payload.mesh.faceIds[Math.floor(i / 3)]).push(i)
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(positions.length), 3))
  if (payload.mesh.normals) {
    const normals = new Float32Array(positions.length)
    payload.mesh.indices.forEach((index, offset) => normals.set(payload.mesh.normals.slice(index * 3, index * 3 + 3), offset * 3))
    geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3))
  } else geometry.computeVertexNormals()
  geometry.computeBoundingBox(); geometry.computeBoundingSphere()
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, color: '#ffffff', metalness: 0.12, roughness: 0.67, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 })
  const mesh = new THREE.Mesh(geometry, material)
  mesh.visible = !selectionOnly
  mesh.name = 'CAD faces'; group.add(mesh); geometries.push(geometry); materials.push(material)
  const edgeObjects = payload.edges.map(edge => {
    const geometry = new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(edge.points, 3))
    const material = new THREE.LineBasicMaterial({ color: CAD_VIEW_COLORS.edge, transparent: true, opacity: 0.9 })
    const line = new THREE.Line(geometry, material)
    line.userData.cadEdge = edge; line.renderOrder = 1; line.name = edge.id
    line.visible = !selectionOnly && !edge.hidden
    group.add(line); geometries.push(geometry); materials.push(material)
    return line
  })
  const faceById = new Map(payload.faces.map(face => [face.id, face]))
  const bounds = geometry.boundingBox.clone(), extent = Math.max(bounds.getSize(new THREE.Vector3()).length(), 0.01)
  const overlay = new THREE.Group(); group.add(overlay)
  const resolution = new THREE.Vector2(1, 1)
  let wireframe = false, lastSelection = [], lastHover = null
  function clearOverlay() { for (const child of overlay.children) { child.geometry.dispose(); child.material.dispose() }; overlay.clear() }
  const colors = Object.fromEntries(Object.entries(CAD_VIEW_COLORS).map(([key, value]) => [key, new THREE.Color(value)]))
  const validSelection = value => {
    if (!value || !['face', 'edge'].includes(value.kind)) return false
    const item = value.kind === 'face' ? faceById.get(value.id) : payload.edges.find(edge => edge.id === value.id)
    return Boolean(item && (!value.selector || (value.selector.geometryVersion === payload.geometryVersion && value.selector.signature === item.selector.signature)))
  }
  function highlight(selection = [], hover = null) {
    lastSelection = selection; lastHover = hover
    const selected = new Set(selection.filter(validSelection).map(cadSelectionKey))
    const hovered = validSelection(hover) ? cadSelectionKey(hover) : ''
    for (const face of payload.faces) {
      const key = cadSelectionKey({ ...face, kind: 'face' })
      const color = selected.has(key) ? colors.selected : hovered === key ? colors.hover : /^#[a-f\d]{6}$/i.test(face.color||'') ? new THREE.Color(face.color) : colors.body
      for (const vertex of faceVertices.get(face.id)) geometry.attributes.color.setXYZ(vertex, color.r, color.g, color.b)
    }
    geometry.attributes.color.needsUpdate = true
    clearOverlay()
    if (selectionOnly || wireframe) for (const face of payload.faces) {
      const key = cadSelectionKey({ kind: 'face', id: face.id })
      if (!selected.has(key) && hovered !== key) continue
      const vertices = faceVertices.get(face.id), points = new Float32Array(vertices.length * 3)
      vertices.forEach((vertex, index) => points.set(positions.subarray(vertex * 3, vertex * 3 + 3), index * 3))
      const faceGeometry = new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(points, 3))
      const faceMaterial = new THREE.MeshBasicMaterial({ color: selected.has(key) ? colors.selected : colors.hover, transparent: true, opacity: 0.38, side: THREE.DoubleSide, depthTest: false, depthWrite: false })
      const highlight = new THREE.Mesh(faceGeometry, faceMaterial)
      highlight.renderOrder = 8; overlay.add(highlight)
    }
    for (const line of edgeObjects) {
      const key = cadSelectionKey({ ...line.userData.cadEdge, kind: 'edge' })
      const selectedEdge = selected.has(key), hoveredEdge = hovered === key
      line.material.color.copy(selectedEdge ? colors.edgeSelected : hoveredEdge ? colors.edgeHover : colors.edge)
      line.material.opacity = selectedEdge || hoveredEdge ? 1 : 0.9
      // A true curve overlay follows all sampled points; it is not a center marker.
      if (!line.userData.cadEdge.hidden && (selectedEdge || hoveredEdge)) {
        const edgeGeometry = new LineGeometry().setPositions(line.userData.cadEdge.points)
        const edgeMaterial = new LineMaterial({ color: selectedEdge ? CAD_VIEW_COLORS.edgeSelected : CAD_VIEW_COLORS.edgeHover, linewidth: selectedEdge ? 3.5 : 2.5, resolution: resolution.clone(), depthTest: !selectionOnly, depthWrite: false })
        const emphasis = new Line2(edgeGeometry, edgeMaterial)
        emphasis.computeLineDistances(); emphasis.renderOrder = 9; overlay.add(emphasis)
      }
    }
  }
  highlight()
  const hitForFace = hit => {
    const face = faceById.get(payload.mesh.faceIds[hit.faceIndex])
    if (!face) return null
    return { kind: 'face', id: face.id, index: face.selector.index, selector: { ...face.selector }, point: hit.point.toArray(), worldPoint: hit.point.toArray(), surfaceType: face.surfaceType, plane: cadPlaneForFace(face) }
  }
  function pick(raycaster, pickKind = 'all', tolerance = extent / 250) {
    if (payload.allBodiesHidden || !['face', 'edge', 'all'].includes(pickKind)) return null
    group.updateMatrixWorld(true)
    const faceHit = raycaster.intersectObject(mesh, false)[0]
    if (pickKind !== 'face') {
      raycaster.params.Line.threshold = Math.max(tolerance, extent * 1e-7)
      const edges = raycaster.intersectObjects(edgeObjects.filter(line=>!line.userData.cadEdge.hidden), false)
      const edgeHit = edges.find(hit => !faceHit || hit.distance <= faceHit.distance + Math.max(tolerance * 1.6, extent * 1e-6))
      if (edgeHit) {
        const edge = edgeHit.object.userData.cadEdge
        return { kind: 'edge', id: edge.id, index: edge.selector.index, selector: { ...edge.selector }, point: edgeHit.point.toArray(), worldPoint: edgeHit.point.toArray() }
      }
    }
    return pickKind !== 'edge' && faceHit ? hitForFace(faceHit) : null
  }
  return { group, mesh, edges: edgeObjects, bounds, extent, payload, pick, highlight,
    setAppearance(appearance) {
      const next = appearance === 'wireframe'
      mesh.visible = !selectionOnly && !next && !payload.allBodiesHidden; edgeObjects.forEach(line => { line.material.depthTest = !next; line.visible = !selectionOnly && !line.userData.cadEdge.hidden && !payload.allBodiesHidden })
      if (wireframe !== next) { wireframe = next; highlight(lastSelection, lastHover) }
    },
    setResolution(width, height) { resolution.set(width, height); for (const child of overlay.children) child.material.resolution?.copy(resolution) },
    dispose() { clearOverlay(); geometries.forEach(value => value.dispose()); materials.forEach(value => value.dispose()); group.clear() },
  }
}

/** Transaction previews show the result while selecting the original input. */
export function createCadViewportTopology(geometry, selectionGeometry = geometry) {
  const display = createCadTopologyScene(geometry), group = new THREE.Group()
  const separate = Boolean(selectionGeometry && (selectionGeometry.geometryVersion !== geometry.geometryVersion || selectionGeometry.sourceFeatureId !== geometry.sourceFeatureId))
  let picking
  try { picking = separate ? createCadTopologyScene(selectionGeometry, { selectionOnly: true }) : display }
  catch (error) { display.dispose(); throw error }
  group.add(display.group); if (separate) group.add(picking.group)
  return { group, display, picking, separate, bounds: display.bounds,
    pick: (...args) => picking.pick(...args),
    highlight: (...args) => picking.highlight(...args),
    setAppearance(value) { display.setAppearance(value) },
    setResolution(width, height) { display.setResolution(width, height); if (separate) picking.setResolution(width, height) },
    dispose() { display.dispose(); if (separate) picking.dispose(); group.clear() },
  }
}

export function nextCadSelection(selection, hit, multiple = false) {
  if (!hit) return []
  if (!multiple) return [hit]
  const key = cadSelectionKey(hit), found = selection.some(item => cadSelectionKey(item) === key)
  return found ? selection.filter(item => cadSelectionKey(item) !== key) : [...selection, hit]
}

export function cadCameraPreset(name) {
  return ({ iso: { direction: [1, -1, 0.85], up: [0, 0, 1] }, top: { direction: [0, 0, 1], up: [0, 1, 0] }, front: { direction: [0, -1, 0], up: [0, 0, 1] }, right: { direction: [1, 0, 0], up: [0, 0, 1] } })[name] || null
}

export function fitCadCamera(camera, bounds, { view = 'iso', aspect = 1, preserveDirection = false } = {}) {
  const center = bounds.getCenter(new THREE.Vector3()), extent = Math.max(bounds.getSize(new THREE.Vector3()).length(), 0.01)
  const preset = cadCameraPreset(view) || cadCameraPreset('iso')
  const direction = preserveDirection ? camera.getWorldDirection(new THREE.Vector3()).negate() : new THREE.Vector3(...preset.direction).normalize()
  if (!preserveDirection) camera.up.fromArray(preset.up)
  camera.position.copy(center).addScaledVector(direction, extent * 3)
  camera.near = extent / 10000; camera.far = extent * 100; camera.zoom = 1
  camera.lookAt(center); camera.updateMatrixWorld(true)
  const projected = new THREE.Box3()
  for (const x of [bounds.min.x, bounds.max.x]) for (const y of [bounds.min.y, bounds.max.y]) for (const z of [bounds.min.z, bounds.max.z]) projected.expandByPoint(new THREE.Vector3(x, y, z).applyMatrix4(camera.matrixWorldInverse))
  const size = projected.getSize(new THREE.Vector3()), ratio = Number.isFinite(aspect) && aspect > 0 ? aspect : 1
  const height = Math.max(size.y, size.x / ratio, extent * 0.05) * 1.3
  camera.top = height / 2; camera.bottom = -height / 2; camera.right = height * ratio / 2; camera.left = -height * ratio / 2
  camera.updateProjectionMatrix()
  return center
}

/** Keep the user's framing through topology/preview replacements in one document. */
export function captureCadCamera(camera, target, documentScope) {
  return { documentScope, position:camera.position.toArray(), quaternion:camera.quaternion.toArray(), up:camera.up.toArray(), target:target.toArray(),
    zoom:camera.zoom, left:camera.left, right:camera.right, top:camera.top, bottom:camera.bottom, near:camera.near, far:camera.far }
}

export function restoreCadCamera(camera, snapshot, documentScope) {
  if (!snapshot || !Object.is(snapshot.documentScope, documentScope)) return null
  camera.position.fromArray(snapshot.position); camera.quaternion.fromArray(snapshot.quaternion); camera.up.fromArray(snapshot.up)
  for (const key of ['zoom','left','right','top','bottom','near','far']) camera[key] = snapshot[key]
  camera.updateProjectionMatrix(); camera.updateMatrixWorld(true)
  return new THREE.Vector3(...snapshot.target)
}

export function cadPickTolerance(camera, height, pixels = 6) {
  return Math.abs(camera.top - camera.bottom) / Math.max(camera.zoom, 1e-8) / Math.max(height, 1) * pixels
}

export function cadCanvasActive({ active, hidden, width, height }) {
  return Boolean(active && !hidden && width > 0 && height > 0 && Number.isFinite(width) && Number.isFinite(height))
}

export function cadFallbackUrl(value, apiBase) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('模型预览地址缺失。')
  const base = new URL(apiBase), url = new URL(value, base)
  if (!['http:', 'https:', 'blob:'].includes(url.protocol) || url.username || url.password || url.origin !== base.origin) throw new Error('模型预览地址与当前服务不一致。')
  return url.href
}
