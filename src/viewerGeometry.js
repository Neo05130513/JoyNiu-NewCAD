import * as THREE from 'three'
import { archedClevisSupportDimensions, validateArchedClevisSupport } from './archedClevisSupport.js'

/** The two transverse ear holes and the underside arch are real openings. */
export function archedClevisSupportGeometries(model) {
  const validation = validateArchedClevisSupport(model)
  if (!validation.valid) throw new RangeError(validation.errors.map(({ message }) => message).join('；'))
  const n = (field) => Number(model[field])
  const outer = n('archOuterRadius'), inner = n('archInnerRadius'), ear = n('earRadius')
  const { bridgeHeight, baseJoinX, earRangesY } = archedClevisSupportDimensions(model)
  const shoulderAngle = Math.acos(ear / outer)
  const profile = (withEar) => {
    const shape = new THREE.Shape()
    shape.moveTo(-outer, 0)
    shape.lineTo(-inner, 0)
    shape.absarc(0, 0, inner, Math.PI, 0, true)
    shape.lineTo(outer, 0)
    shape.absarc(0, 0, outer, 0, shoulderAngle, false)
    if (withEar) {
      shape.lineTo(ear, n('earCenterHeight'))
      shape.absarc(0, n('earCenterHeight'), ear, 0, Math.PI, false)
      shape.lineTo(-ear, bridgeHeight)
      const hole = new THREE.Path()
      hole.absarc(0, n('earCenterHeight'), n('earHoleDiameter') / 2, 0, Math.PI * 2, true)
      shape.holes.push(hole)
    } else shape.lineTo(-ear, bridgeHeight)
    shape.absarc(0, 0, outer, Math.PI - shoulderAngle, Math.PI, false)
    shape.closePath()
    return shape
  }
  const extrudeY = (shape, from, to) => {
    const geometry = new THREE.ExtrudeGeometry(shape, { depth: to - from, steps: 1, curveSegments: 96, bevelEnabled: false })
    geometry.rotateX(Math.PI / 2)
    geometry.translate(0, to, 0)
    geometry.computeVertexNormals()
    return geometry
  }
  const segments = [
    { name: '圆弧拱座 · 中间连接平台', feature: 'bridge', geometry: extrudeY(profile(false), -n('earGap') / 2, n('earGap') / 2) },
    ...earRangesY.map(([from, to], index) => ({
      name: `${index === 0 ? '前' : '后'}侧上耳 · 真孔 Ø${n('earHoleDiameter')}`,
      feature: 'ear', geometry: extrudeY(profile(true), from, to),
    })),
  ]
  // Join the feet to the arch after cutting the underside passage. Thick
  // but valid feet can meet inside the inner radius, so this connector needs
  // the same curved lower boundary as the arch instead of a solid rectangle.
  const cx = n('mountHoleCenterDistance') / 2, radius = n('mountEarRadius')
  const connector = new THREE.Shape()
  connector.moveTo(baseJoinX, baseJoinX < inner ? Math.sqrt(inner ** 2 - baseJoinX ** 2) : 0)
  if (baseJoinX < inner) connector.absarc(0, 0, inner, Math.acos(baseJoinX / inner), 0, true)
  connector.lineTo(outer, 0)
  connector.lineTo(outer, n('baseThickness'))
  connector.lineTo(baseJoinX, n('baseThickness'))
  connector.closePath()
  const connectorGeometry = extrudeY(connector, -radius, radius)
  const pad = new THREE.Shape()
  pad.moveTo(outer, -radius)
  pad.lineTo(cx, -radius)
  pad.absarc(cx, 0, radius, -Math.PI / 2, Math.PI / 2, false)
  pad.lineTo(outer, radius)
  pad.closePath()
  const mountHole = new THREE.Path()
  mountHole.absarc(cx, 0, n('mountHoleDiameter') / 2, 0, Math.PI * 2, true)
  pad.holes.push(mountHole)
  const padGeometry = new THREE.ExtrudeGeometry(pad, { depth: n('baseThickness'), steps: 1, curveSegments: 96, bevelEnabled: false })
  // Remove the shared interior faces before combining the connector and
  // rounded pad, so edge overlays do not invent a seam on the flat foot top.
  const positions = []
  for (const geometry of [connectorGeometry, padGeometry]) {
    const attribute = geometry.attributes.position
    for (let index = 0; index < attribute.count; index += 3) {
      if ([0, 1, 2].every((offset) => Math.abs(attribute.getX(index + offset) - outer) < Math.max(1, outer) * 1e-7)) continue
      for (let offset = 0; offset < 3; offset += 1) positions.push(attribute.getX(index + offset), attribute.getY(index + offset), attribute.getZ(index + offset))
    }
    geometry.dispose()
  }
  const rightFootGeometry = new THREE.BufferGeometry()
  rightFootGeometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  rightFootGeometry.computeVertexNormals()
  for (const direction of [-1, 1]) {
    const geometry = rightFootGeometry.clone()
    if (direction < 0) geometry.rotateZ(Math.PI)
    geometry.computeVertexNormals()
    segments.push({ name: `${direction < 0 ? '左' : '右'}安装耳 · 真孔 Ø${n('mountHoleDiameter')}`, feature: 'mounting-ear', geometry })
  }
  rightFootGeometry.dispose()
  return segments
}

// The shaft recipe cuts a rectangular keyway from the +Z side, starting at
// X=0. Extruding the actual notched cross-section makes the slot floor and
// bore inspectable without a boolean/CSG dependency. The remaining shaft
// length uses an annulus; both segments share the same circular bore.
export function shaftPreviewSegments(model) {
  const radius = Number(model.outerDiameter) / 2
  const boreRadius = Number(model.holeDiameter) / 2
  const length = Number(model.length)
  const slotLength = Number(model.keywayLength)
  const halfWidth = Number(model.keywayWidth) / 2
  const floorZ = radius - Number(model.keywayDepth)
  const makeProfile = (notched) => {
    const shape = new THREE.Shape()
    if (notched) {
      const mouthAngle = Math.asin(halfWidth / radius)
      const mouthZ = Math.sqrt(radius * radius - halfWidth * halfWidth)
      shape.moveTo(-halfWidth, mouthZ)
      shape.absarc(0, 0, radius, Math.PI / 2 + mouthAngle, Math.PI * 2.5 - mouthAngle, false)
      shape.lineTo(halfWidth, floorZ)
      shape.lineTo(-halfWidth, floorZ)
      shape.closePath()
    } else shape.absarc(0, 0, radius, 0, Math.PI * 2, false)
    const bore = new THREE.Path()
    bore.absarc(0, 0, boreRadius, 0, Math.PI * 2, true)
    shape.holes.push(bore)
    return shape
  }
  const profileToWorld = new THREE.Matrix4().set(
    0, 0, 1, 0,
    1, 0, 0, 0,
    0, 1, 0, 0,
    0, 0, 0, 1,
  )
  return [
    { name: 'shaft with cut keyway', start: 0, length: slotLength, notched: true },
    { name: 'shaft beyond keyway', start: slotLength, length: length - slotLength, notched: false },
  ].filter((segment) => segment.length > 1e-8).map((segment) => {
    const geometry = new THREE.ExtrudeGeometry(makeProfile(segment.notched), {
      depth: segment.length, steps: 1, curveSegments: 64, bevelEnabled: false,
    })
    geometry.applyMatrix4(profileToWorld)
    geometry.translate(segment.start, 0, 0)
    return { name: segment.name, geometry }
  })
}

// Callers validate dimensions before building these preview meshes. Preserve
// valid user values exactly; visual scaffolding must not silently resize them.
export function clampSupportBaseGeometry({
  length,
  width,
  thickness,
  mainDepth,
  frontExtensionWidth,
  cornerRadius,
  reliefRadius,
  tongueRadius,
  mountHoleDiameter,
  mountHoleDistance,
  mountHoleCenterFromRear,
}) {
  const x0 = -length / 2; const x1 = length / 2
  const y0 = -width / 2; const y1 = width / 2
  const bodyDepth = mainDepth
  const bodyFront = y1 - bodyDepth
  const extensionHalf = frontExtensionWidth / 2
  const outerR = cornerRadius
  const transitionR = reliefRadius
  const frontR = tongueRadius

  // +Y is the drawing's rear edge. The plan view is a full-width rear plate
  // with a narrower front tongue, matching the production recipe's datums.
  // Only the two front corners of the full-width plate are rounded. The rear
  // edge is square in the drawing; the R8/R5 callouts belong to the front
  // silhouette and its transition into the narrow tongue.
  const shape = new THREE.Shape()
  shape.moveTo(-extensionHalf + frontR, y0)
  shape.lineTo(extensionHalf - frontR, y0)
  shape.absarc(extensionHalf - frontR, y0 + frontR, frontR, -Math.PI / 2, 0, false)
  shape.lineTo(extensionHalf, bodyFront - transitionR)
  shape.absarc(extensionHalf + transitionR, bodyFront - transitionR, transitionR, Math.PI, Math.PI / 2, true)
  shape.lineTo(x1 - outerR, bodyFront)
  shape.absarc(x1 - outerR, bodyFront + outerR, outerR, -Math.PI / 2, 0, false)
  shape.lineTo(x1, y1)
  shape.lineTo(x0, y1)
  shape.lineTo(x0, bodyFront + outerR)
  shape.absarc(x0 + outerR, bodyFront + outerR, outerR, Math.PI, Math.PI * 1.5, false)
  shape.lineTo(-extensionHalf - transitionR, bodyFront)
  shape.absarc(-extensionHalf - transitionR, bodyFront - transitionR, transitionR, Math.PI / 2, 0, true)
  shape.lineTo(-extensionHalf, y0 + frontR)
  shape.absarc(-extensionHalf + frontR, y0 + frontR, frontR, Math.PI, Math.PI * 1.5, false)
  shape.closePath()

  const holeRadius = mountHoleDiameter / 2
  const holeY = y1 - mountHoleCenterFromRear
  for (const holeX of [-mountHoleDistance / 2, mountHoleDistance / 2]) {
    const hole = new THREE.Path()
    hole.absarc(holeX, holeY, holeRadius, 0, Math.PI * 2, false)
    shape.holes.push(hole)
  }
  return new THREE.ExtrudeGeometry(shape, { depth: thickness, steps: 1, curveSegments: 40, bevelEnabled: false })
}
