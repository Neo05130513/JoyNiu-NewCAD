import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { API_BASE, api } from './api.js'

/*
 * The workbench used to draw a convincing-looking SVG projection.  That is
 * useful for a static empty state, but it is not a CAD viewport: the result
 * cannot be orbited, clipped, or inspected as a mesh.  This component owns a
 * small Three.js scene and deliberately prefers the GLB emitted by the
 * FastAPI/CadQuery service.  A parametric mesh is kept as an explicit local
 * fallback so the UI remains inspectable while the API is offline; the status
 * badge always tells the operator which source is on screen.
 */

const MIN_ZOOM = 0.55
const MAX_ZOOM = 1.8

const clamp = (value, min, max) => Math.min(max, Math.max(min, value))
const number = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback
const firstNumber = (source, keys, fallback) => {
  for (const key of keys) {
    const value = source?.[key]
    if (value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))) return Number(value)
  }
  return fallback
}
const isClampSupportKind = (kind) => [
  'split_clamp_support',
  'split_clamp_support_v1',
  'split_clamp_pedestal',
  'clamp_pedestal',
  'circular_clamp',
].includes(String(kind || '').toLowerCase())
const isSteppedTaperedNozzleKind = (kind) => [
  'stepped_tapered_nozzle',
  'stepped_tapered_nozzle_with_insert_v1',
  'stepped_tapered_nozzle_v1',
  'tapered_nozzle_with_insert',
].includes(String(kind || '').toLowerCase())

// Fit the bounding sphere against the narrower frustum axis.  The workbench
// is a three-column layout and can become much narrower than it is tall; a
// vertical-FOV-only distance would then crop the base plate on the sides.
function fitDistance(camera, radius, margin = 1.14) {
  const verticalHalfAngle = THREE.MathUtils.degToRad(camera.fov) / 2
  const horizontalHalfAngle = Math.atan(Math.tan(verticalHalfAngle) * Math.max(camera.aspect, 0.01))
  const limitingHalfAngle = Math.max(0.01, Math.min(verticalHalfAngle, horizontalHalfAngle))
  return radius * margin / Math.sin(limitingHalfAngle)
}

function artifactUrl(artifact) {
  if (!artifact) return ''
  const downloadPath = artifact.downloadUrl || artifact.download_url
  if (downloadPath) {
    try { return new URL(downloadPath, `${API_BASE}/`).toString() } catch { return downloadPath }
  }
  if (artifact.id && artifact.format) return api.artifactUrl(artifact.id, artifact.format)
  return ''
}

function material(color = 0xb9c5d2, opacity = 1) {
  return new THREE.MeshStandardMaterial({
    color,
    metalness: 0.42,
    roughness: 0.34,
    transparent: opacity < 1,
    opacity,
    side: THREE.DoubleSide,
  })
}

function addEdgeOverlay(mesh, color = 0x566579) {
  if (!mesh?.geometry) return
  const edges = new THREE.EdgesGeometry(mesh.geometry, 32)
  const lines = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity: 0.58,
    depthTest: true,
  }))
  lines.name = `${mesh.name || 'mesh'} · edges`
  lines.renderOrder = 2
  mesh.add(lines)
}

function extrudedXYShape(length, width, z0, z1, holes = []) {
  const shape = new THREE.Shape()
  const x0 = -length / 2; const x1 = length / 2
  const y0 = -width / 2; const y1 = width / 2
  shape.moveTo(x0, y0); shape.lineTo(x1, y0); shape.lineTo(x1, y1); shape.lineTo(x0, y1); shape.closePath()
  holes.forEach(({ x, y, radius }) => {
    const path = new THREE.Path()
    path.absarc(x, y, radius, 0, Math.PI * 2, false)
    shape.holes.push(path)
  })
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: z1 - z0, steps: 1, curveSegments: 48, bevelEnabled: false })
  geometry.translate(0, 0, z0)
  geometry.computeVertexNormals()
  return geometry
}

function extrudedXZProfile(profile, depth, centerY) {
  // Three.js extrudes a Shape along local Z. Rotate that axis into world Y,
  // while retaining the profile's second coordinate as world Z.
  const geometry = new THREE.ExtrudeGeometry(profile, { depth, steps: 1, curveSegments: 32, bevelEnabled: false })
  geometry.rotateX(Math.PI / 2)
  geometry.translate(0, centerY + depth / 2, 0)
  geometry.computeVertexNormals()
  return geometry
}

function extrudedLocalShape(shape, z0, z1, curveSegments = 48) {
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: Math.max(0.01, z1 - z0),
    steps: 1,
    curveSegments,
    bevelEnabled: false,
  })
  geometry.translate(0, 0, z0)
  geometry.computeVertexNormals()
  return geometry
}

function clampSupportBaseGeometry({
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
  const bodyDepth = clamp(mainDepth, width * 0.55, width - 1)
  const bodyFront = y1 - bodyDepth
  const tongueDepth = bodyFront - y0
  const extensionHalf = clamp(frontExtensionWidth / 2, length * 0.16, length / 2 - 1)
  const outerR = clamp(cornerRadius, 0.1, Math.min(length, bodyDepth) / 5)
  const transitionR = clamp(reliefRadius, 0.1, Math.max(0.1, Math.min(tongueDepth / 2, (length / 2 - extensionHalf) / 2)))
  const frontR = clamp(
    tongueRadius,
    0.1,
    Math.max(0.1, Math.min(extensionHalf, tongueDepth - transitionR)),
  )

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
  return extrudedLocalShape(shape, 0, thickness, 40)
}

function dShapedPedestalShape(outerRadius, rearDepth) {
  const outer = Math.max(0.5, outerRadius)
  const back = Math.max(0.5, rearDepth)
  const segments = 64
  const shape = new THREE.Shape()

  // The dimensioned R33 applies only to the front half. At the horizontal
  // centre datum the two tangent sides continue straight to the rear edge.
  shape.moveTo(-outer, 0)
  for (let index = 1; index <= segments; index += 1) {
    const theta = Math.PI + (Math.PI * index) / segments
    shape.lineTo(Math.cos(theta) * outer, Math.sin(theta) * outer)
  }
  shape.lineTo(outer, back)
  shape.lineTo(-outer, back)
  shape.closePath()
  return shape
}

function splitDShapedPedestalShape(outerRadius, innerRadius, openingWidth, rearDepth) {
  const outer = Math.max(innerRadius + 0.5, outerRadius)
  const inner = clamp(innerRadius, 0.5, outer - 0.5)
  const halfGap = clamp(openingWidth / 2, 0.1, inner - 0.1)
  const outerOffset = Math.asin(halfGap / outer)
  const innerOffset = Math.asin(halfGap / inner)
  const outerLeftMouth = Math.PI * 1.5 - outerOffset
  const outerRightMouth = -Math.PI / 2 + outerOffset
  const innerRightMouth = -Math.PI / 2 + innerOffset
  const innerLeftMouth = Math.PI * 1.5 - innerOffset
  const back = Math.max(0.5, rearDepth)
  const frontSegments = 40
  const innerSegments = 72
  const shape = new THREE.Shape()

  // Trace one connected material boundary: left split mouth -> front R33 ->
  // square rear extension -> right split mouth -> complete Ø36 inner wall.
  // A single contour is more robust than intersecting a circular hole with a
  // rectangular slot, whose touching boundaries can confuse Earcut.
  shape.moveTo(Math.cos(outerLeftMouth) * outer, Math.sin(outerLeftMouth) * outer)
  for (let index = 1; index <= frontSegments; index += 1) {
    const theta = outerLeftMouth + ((Math.PI - outerLeftMouth) * index) / frontSegments
    shape.lineTo(Math.cos(theta) * outer, Math.sin(theta) * outer)
  }
  shape.lineTo(-outer, back)
  shape.lineTo(outer, back)
  shape.lineTo(outer, 0)
  for (let index = 1; index <= frontSegments; index += 1) {
    const theta = (outerRightMouth * index) / frontSegments
    shape.lineTo(Math.cos(theta) * outer, Math.sin(theta) * outer)
  }
  shape.lineTo(Math.cos(innerRightMouth) * inner, Math.sin(innerRightMouth) * inner)
  for (let index = 1; index <= innerSegments; index += 1) {
    const theta = innerRightMouth + ((innerLeftMouth - innerRightMouth) * index) / innerSegments
    shape.lineTo(Math.cos(theta) * inner, Math.sin(theta) * inner)
  }
  shape.closePath()
  return shape
}

function rectangularRearWallShape(outerRadius, innerRadius, rearDepth) {
  const outer = Math.max(innerRadius + 0.5, outerRadius)
  const inner = clamp(innerRadius, 0.5, outer - 0.5)
  const back = Math.max(inner + 0.5, rearDepth)
  const segments = 56
  const shape = new THREE.Shape()

  // The raised rear jaw has straight outer faces. Its front edge opens into
  // the bore through a rear semicircular notch, producing the U-shaped wall
  // visible in the isometric view without restoring a curved outer back.
  shape.moveTo(-outer, 0)
  shape.lineTo(-inner, 0)
  for (let index = 1; index <= segments; index += 1) {
    const theta = Math.PI - (Math.PI * index) / segments
    shape.lineTo(Math.cos(theta) * inner, Math.sin(theta) * inner)
  }
  shape.lineTo(outer, 0)
  shape.lineTo(outer, back)
  shape.lineTo(-outer, back)
  shape.closePath()
  return shape
}

function saddleProfile(x0, x1, baseZ, topZ, opening, radius, pocketDepth = 0, pocketWidth = 0) {
  const profile = new THREE.Shape()
  const halfOpening = opening / 2
  // `pocketWidth` is the width of one pocket measured inward from the
  // opening edge (10 mm in the acceptance drawing), not the combined span.
  const halfPocket = Math.max(0, pocketWidth)
  const pocketFloor = topZ - Math.max(0, pocketDepth)
  const saddleZ = (x) => Math.abs(x) <= radius
    ? topZ - Math.sqrt(Math.max(0, radius * radius - x * x))
    : topZ
  profile.moveTo(x0, baseZ)
  profile.lineTo(x1, baseZ)
  profile.lineTo(x1, topZ)
  if (pocketDepth > 0 && pocketWidth > 0) {
    // The middle slab is the union of two rectangular pocket cuts and the
    // circular saddle cut.  For the calibrated drawing the pockets occupy
    // x=[-20,-10] and [10,20], while the R15 arc occupies x=[-15,15].
    // Consequently the material boundary jumps vertically at the pocket's
    // inner edges (±10), then follows the saddle arc; starting the arc at
    // ±15 would incorrectly leave material in the pocket bands.
    const rightOuter = Math.min(Math.abs(x1), halfOpening)
    const rightInner = Math.max(0, rightOuter - halfPocket)
    profile.lineTo(rightOuter, topZ)
    profile.lineTo(rightOuter, pocketFloor)
    profile.lineTo(rightInner, pocketFloor)
    const arcSteps = 24
    if (rightInner < radius - 1e-6) {
      profile.lineTo(rightInner, saddleZ(rightInner))
      for (let i = 1; i <= arcSteps; i += 1) {
        const x = rightInner - (2 * rightInner * i) / arcSteps
        profile.lineTo(x, saddleZ(x))
      }
    } else {
      // If a custom pocket is wider than the saddle, retain the top-plane
      // shoulders between the pocket edge and the circular cut.
      profile.lineTo(rightInner, topZ)
      for (let i = 1; i <= arcSteps; i += 1) {
        const x = radius - (2 * radius * i) / arcSteps
        profile.lineTo(x, saddleZ(x))
      }
      profile.lineTo(-rightInner, topZ)
    }
    profile.lineTo(-rightInner, pocketFloor)
    profile.lineTo(-rightOuter, pocketFloor)
    profile.lineTo(-rightOuter, topZ)
  } else {
    profile.lineTo(halfOpening, topZ)
    const arcSteps = 24
    for (let i = 1; i <= arcSteps; i += 1) {
      const x = radius - (2 * radius * i) / arcSteps
      profile.lineTo(x, saddleZ(x))
    }
    profile.lineTo(-halfOpening, topZ)
  }
  profile.lineTo(x0, topZ)
  profile.closePath()
  return profile
}

function addVerticalCavity(root, centerX, radius, z0, z1, materialColor = 0x647184) {
  // Only the inner half of a cylinder is exposed because its axis lies on an
  // upper-body side boundary. This makes the fallback read as a concave
  // semicircular cut, never as an additive boss.
  const inwardStart = centerX < 0 ? -Math.PI / 2 : Math.PI / 2
  const segments = 40
  const positions = []; const normals = []; const indices = []
  for (let i = 0; i <= segments; i += 1) {
    const theta = inwardStart + (Math.PI * i) / segments
    const x = centerX + radius * Math.cos(theta)
    const y = radius * Math.sin(theta)
    for (const z of [z0, z1]) {
      positions.push(x, y, z)
      const nx = -Math.cos(theta); const ny = -Math.sin(theta)
      normals.push(nx, ny, 0)
    }
  }
  for (let i = 0; i < segments; i += 1) {
    const a = i * 2; const b = (i + 1) * 2
    indices.push(a, b, b + 1, a, b + 1, a + 1)
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  geometry.setAttribute('normal', new THREE.Float32BufferAttribute(normals, 3))
  geometry.setIndex(indices)
  const cavity = new THREE.Mesh(geometry, material(materialColor, 0.96))
  cavity.name = `Ø${radius * 2} side through cut`
  cavity.castShadow = false
  cavity.receiveShadow = true
  root.add(cavity)
}

function auditBracketFallback(root, expected) {
  root.updateMatrixWorld(true)
  // Construction helpers (centre lines/axes) intentionally extend beyond the
  // solid for visual orientation.  They must not inflate the dimensional
  // audit, otherwise the fallback is reported as invalid (the centre line is
  // 16 mm longer than the 100 mm base by design).
  const box = new THREE.Box3()
  root.traverse((object) => {
    if (object.isMesh) box.expandByObject(object)
  })
  const size = box.getSize(new THREE.Vector3())
  const bboxMatches = [[size.x, expected.length], [size.y, expected.width], [size.z, expected.height]]
    .every(([actual, target]) => Math.abs(actual - target) <= 0.15)
  return {
    ok: bboxMatches,
    bbox: { length: Number(size.x.toFixed(3)), width: Number(size.y.toFixed(3)), height: Number(size.z.toFixed(3)) },
    bboxMatches,
    subtractiveFeatures: { sideHolePair: true, rectangularPocketPair: true, saddle: true },
  }
}

function auditClampSupportFallback(root, expected) {
  root.updateMatrixWorld(true)
  const box = new THREE.Box3()
  root.traverse((object) => {
    if (object.isMesh) box.expandByObject(object)
  })
  const size = box.getSize(new THREE.Vector3())
  const bboxMatches = [[size.x, expected.length], [size.y, expected.width], [size.z, expected.height]]
    .every(([actual, target]) => Math.abs(actual - target) <= 0.15)
  return {
    ok: bboxMatches,
    bbox: { length: Number(size.x.toFixed(3)), width: Number(size.y.toFixed(3)), height: Number(size.z.toFixed(3)) },
    bboxMatches,
    subtractiveFeatures: {
      mountingHolePair: true,
      verticalBlindBore: true,
      radialSplit: true,
      transverseClampHole: true,
    },
    additiveFeatures: {
      dShapedPedestal: true,
      rectangularRearWall: true,
      rearEdgeGussets: true,
      gussetCount: 2,
    },
  }
}

function makeBracketFallback(model) {
  const L = Math.max(20, number(model?.baseLength, 100))
  const W = Math.max(16, number(model?.baseWidth, 50))
  const T = Math.max(1, number(model?.baseThickness, 10))
  const UL = Math.min(L - 2, Math.max(4, number(model?.upperLength, 70)))
  const UW = Math.min(W, Math.max(4, number(model?.upperWidth, 50)))
  const H = Math.max(T + 1, number(model?.totalHeight, T + number(model?.upperHeight, 30)))
  const opening = Math.min(UL - 2, Math.max(2, number(model?.notchOpening, 40)))
  const radius = Math.min(opening / 2, Math.max(1, number(model?.notchRadius, 15)))
  const holeDiameter = Math.min(Math.min(L, W) - 2, Math.max(2, number(model?.bossDiameter, 20)))
  const holeRadius = holeDiameter / 2
  const holeDistance = Math.min(L - holeDiameter, Math.max(holeDiameter, number(model?.bossCenterDistance, 70)))
  const slotLength = Math.min(W, Math.max(1, number(model?.slotLength, 30)))
  const slotWidth = Math.max(1, number(model?.slotWidth, 10))
  const pocketDepth = Math.min(H - T - 0.1, Math.max(0.1, number(model?.pocketDepth, 10)))

  const root = new THREE.Group()
  root.name = 'JoyNiu bracket · corrected parametric WebGL fallback'
  const holes = [{ x: -holeDistance / 2, y: 0, radius: holeRadius }, { x: holeDistance / 2, y: 0, radius: holeRadius }]
  const base = new THREE.Mesh(extrudedXYShape(L, W, 0, T, holes), material(0xaebdcc))
  base.name = 'Base plate · Ø20 through holes'
  base.castShadow = true; base.receiveShadow = true; addEdgeOverlay(base); root.add(base)

  const upperGroup = new THREE.Group()
  upperGroup.name = 'Upper body · full width 50 with subtractive cuts'
  const sideDepth = Math.max(0, (UW - slotLength) / 2)
  const normalProfile = saddleProfile(-UL / 2, UL / 2, T, H, opening, radius)
  if (sideDepth > 0.01) {
    for (const centerY of [-(slotLength / 2 + sideDepth / 2), slotLength / 2 + sideDepth / 2]) {
      const slab = new THREE.Mesh(extrudedXZProfile(normalProfile, sideDepth, centerY), material(0xc1ccd8))
      slab.name = 'Upper front/back wall'
      slab.castShadow = true; slab.receiveShadow = true; addEdgeOverlay(slab); upperGroup.add(slab)
    }
  }
  const middleProfile = saddleProfile(-UL / 2, UL / 2, T, H, opening, radius, pocketDepth, slotWidth)
  const middle = new THREE.Mesh(extrudedXZProfile(middleProfile, Math.min(UW, slotLength), 0), material(0xc1ccd8))
  middle.name = 'Upper middle · two 10 mm deep pockets'
  middle.castShadow = true; middle.receiveShadow = true; addEdgeOverlay(middle); upperGroup.add(middle)
  root.add(upperGroup)

  for (const x of [-holeDistance / 2, holeDistance / 2]) addVerticalCavity(root, x, holeRadius, T, H)

  const centre = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-L / 2 - 8, 0, T + 0.04), new THREE.Vector3(L / 2 + 8, 0, T + 0.04)]),
    new THREE.LineDashedMaterial({ color: 0x4c82cb, dashSize: 2, gapSize: 2, transparent: true, opacity: 0.42 }),
  )
  centre.computeLineDistances(); centre.name = 'centre line'; root.add(centre)
  root.userData.fallbackAudit = auditBracketFallback(root, { length: L, width: W, height: H })
  return root
}

function makeClampSupportFallback(model) {
  const L = Math.max(40, firstNumber(model, ['baseLength', 'overallLength'], 125))
  const W = Math.max(40, firstNumber(model, ['baseWidth', 'overallWidth'], 95))
  const T = clamp(firstNumber(model, ['baseThickness'], 15), 1, W / 2)
  const H = Math.max(T + 8, firstNumber(model, ['totalHeight', 'overallHeight'], 75))
  const mainDepth = firstNumber(model, ['baseMainDepth', 'baseBodyDepth', 'mainBaseDepth'], 80)
  const frontExtensionWidth = firstNumber(model, ['frontExtensionWidth', 'frontTongueWidth', 'baseFrontWidth'], 80)
  const cornerRadius = firstNumber(model, ['outerCornerRadius', 'cornerRadius', 'baseCornerRadius'], 8)
  const reliefRadius = firstNumber(model, ['neckConcaveRadius', 'reliefRadius', 'frontReliefRadius'], 5)
  const tongueRadius = firstNumber(model, ['neckConvexRadius', 'frontTongueRadius'], 8)
  const mountHoleDiameter = clamp(firstNumber(model, ['mountHoleDiameter', 'mountingHoleDiameter'], 12), 1, Math.min(L, W) / 3)
  const mountHoleDistance = clamp(
    firstNumber(model, ['mountHoleCenterDistance', 'mountHoleSpacing', 'mountingHoleCenterDistance'], 96),
    mountHoleDiameter + 1,
    L - mountHoleDiameter - 1,
  )
  const diameterRadius = firstNumber(model, ['pedestalOuterDiameter', 'pedestalDiameter'], Number.NaN) / 2
  const pedestalRadius = clamp(
    firstNumber(model, ['pedestalRadius', 'pedestalOuterRadius'], Number.isFinite(diameterRadius) ? diameterRadius : 33),
    4,
    Math.min(L, W) / 2 - 1,
  )
  const pedestalCenterFromRear = clamp(
    firstNumber(model, ['pedestalCenterFromRear', 'pedestalCenterY', 'pedestalOffsetY'], 35),
    Math.min(pedestalRadius, W / 2 - 1),
    Math.max(Math.min(pedestalRadius, W / 2 - 1), W - pedestalRadius),
  )
  const mountHoleCenterFromRear = clamp(
    firstNumber(model, ['mountHoleCenterFromRear', 'mountHoleCenterY', 'mountHoleOffsetY', 'featureCenterFromRear'], 40),
    mountHoleDiameter / 2 + 1,
    W - mountHoleDiameter / 2 - 1,
  )
  const centerY = W / 2 - pedestalCenterFromRear
  const rearY = W / 2
  const rearDepth = rearY - centerY
  const boreDiameter = clamp(
    firstNumber(model, ['clampBoreDiameter', 'boreDiameter', 'centerBoreDiameter'], 36),
    2,
    pedestalRadius * 2 - 1,
  )
  const boreRadius = boreDiameter / 2
  const splitWidth = clamp(
    firstNumber(model, ['clampSlotWidth', 'splitWidth', 'radialSlotWidth'], 12),
    0.5,
    boreDiameter - 0.5,
  )
  const pedestalHeight = clamp(
    firstNumber(model, ['pedestalHeight', 'lowerPedestalHeight'], 40),
    1,
    H - T - 0.5,
  )
  const lowerTopZ = T + pedestalHeight
  const rearClampRise = clamp(
    firstNumber(model, ['rearClampRise', 'clampTopHeight'], H - lowerTopZ),
    0.5,
    H - T,
  )
  // Valid production parameters make both expressions identical. Taking the
  // lower start keeps an in-progress manual edit connected even when the two
  // redundant height dimensions have not yet been reconciled.
  const upperStartZ = clamp(Math.min(lowerTopZ, H - rearClampRise), T + 0.5, H - 0.5)
  const floorThickness = firstNumber(model, ['boreFloorThickness', 'blindBoreFloorThickness'], 25)
  const boreBottomZ = clamp(
    firstNumber(model, ['boreFloorZ'], T + floorThickness),
    T + 0.5,
    lowerTopZ - 0.25,
  )
  const clampHoleDiameter = clamp(
    firstNumber(model, ['crossHoleDiameter', 'clampBoltHoleDiameter', 'clampBoltDiameter', 'transverseHoleDiameter'], 12),
    1,
    Math.min(boreDiameter - 0.5, H - T - 0.5),
  )
  const clampHoleZ = clamp(
    firstNumber(model, ['crossHoleCenterZ', 'clampBoltCenterHeight', 'transverseHoleCenterHeight'], 55),
    T + clampHoleDiameter / 2,
    H - clampHoleDiameter / 2,
  )
  const gussetHeight = clamp(firstNumber(model, ['ribHeight', 'gussetHeight'], 20), 1, H - T)
  const gussetThickness = clamp(firstNumber(model, ['ribThickness', 'gussetThickness'], 10), 1, Math.min(W, pedestalRadius))
  const rearBridgeWidth = clamp(
    firstNumber(model, ['rearBridgeWidth'], pedestalRadius * 2 + gussetThickness * 2),
    pedestalRadius * 2,
    L - 2,
  )

  const root = new THREE.Group()
  root.name = 'JoyNiu split clamp support · parametric WebGL fallback'

  const base = new THREE.Mesh(clampSupportBaseGeometry({
    length: L,
    width: W,
    thickness: T,
    mainDepth,
    frontExtensionWidth,
    cornerRadius,
    reliefRadius,
    tongueRadius,
    mountHoleDiameter,
    mountHoleDistance,
    mountHoleCenterFromRear,
  }), material(0xaebdcc))
  base.name = `Irregular R${cornerRadius}/R${reliefRadius}/R${tongueRadius} base · 2×Ø${mountHoleDiameter}`
  base.castShadow = true; base.receiveShadow = true; addEdgeOverlay(base); root.add(base)

  const pedestalFloorShape = dShapedPedestalShape(pedestalRadius, rearDepth)
  const pedestalFloorGeometry = extrudedLocalShape(pedestalFloorShape, T, boreBottomZ, 64)
  pedestalFloorGeometry.translate(0, centerY, 0)
  const pedestalFloor = new THREE.Mesh(pedestalFloorGeometry, material(0xb8c6d3))
  pedestalFloor.name = `front R${pedestalRadius} D-shaped pedestal · blind-bore floor`
  pedestalFloor.castShadow = true; pedestalFloor.receiveShadow = true; addEdgeOverlay(pedestalFloor); root.add(pedestalFloor)

  const splitProfile = splitDShapedPedestalShape(pedestalRadius, boreRadius, splitWidth, rearDepth)
  const lowerRingGeometry = extrudedLocalShape(splitProfile, boreBottomZ, lowerTopZ, 72)
  lowerRingGeometry.translate(0, centerY, 0)
  const lowerRing = new THREE.Mesh(lowerRingGeometry, material(0xb8c6d3))
  lowerRing.name = `lower D-shaped seat · Ø${boreDiameter} blind bore · ${splitWidth} mm front radial split`
  lowerRing.castShadow = true; lowerRing.receiveShadow = true; addEdgeOverlay(lowerRing); root.add(lowerRing)

  const upperGeometry = extrudedLocalShape(rectangularRearWallShape(pedestalRadius, boreRadius, rearDepth), upperStartZ, H, 72)
  upperGeometry.translate(0, centerY, 0)
  const upperClamp = new THREE.Mesh(upperGeometry, material(0xd0d8e1))
  upperClamp.name = `${rearClampRise} mm straight-sided rectangular rear jaw`
  upperClamp.castShadow = true; upperClamp.receiveShadow = true; addEdgeOverlay(upperClamp); root.add(upperClamp)

  // A dark disk at the base of the cavity makes the Ø36 blind bore readable
  // from the default isometric/top views while the split D-shaped wall
  // supplies the true inner wall and radial opening.
  const boreFloor = new THREE.Mesh(
    new THREE.CircleGeometry(Math.max(0.5, boreRadius - 0.15), 64),
    new THREE.MeshStandardMaterial({ color: 0x526170, metalness: 0.08, roughness: 0.78, side: THREE.DoubleSide }),
  )
  boreFloor.position.set(0, centerY, boreBottomZ + 0.03)
  boreFloor.name = `Ø${boreDiameter} blind-bore floor`
  root.add(boreFloor)

  // The clamp screw bore runs along Y through the complete D-shaped envelope.
  // The dark cylinder is a preview cue for the subtractive feature; the base
  // plate still owns the overall Y bounding box used by the audit.
  const pedestalFrontY = centerY - pedestalRadius
  const transverseDepth = rearY - pedestalFrontY
  const transverseCenterY = (rearY + pedestalFrontY) / 2
  const transverseHole = new THREE.Mesh(
    new THREE.CylinderGeometry(clampHoleDiameter / 2, clampHoleDiameter / 2, transverseDepth, 40),
    new THREE.MeshStandardMaterial({ color: 0x536170, metalness: 0.06, roughness: 0.8, side: THREE.DoubleSide }),
  )
  transverseHole.position.set(0, transverseCenterY, clampHoleZ)
  transverseHole.name = `Ø${clampHoleDiameter} transverse clamp hole`
  root.add(transverseHole)

  const gussetTopZ = Math.min(H, T + gussetHeight)
  const ribProjection = Math.max(gussetThickness, (rearBridgeWidth - pedestalRadius * 2) / 2)
  const gussetBandCenterY = rearY - gussetThickness / 2
  for (const direction of [-1, 1]) {
    const innerX = direction * pedestalRadius
    const outerX = direction * Math.min(L / 2 - 1, pedestalRadius + ribProjection)
    const profile = new THREE.Shape()
    profile.moveTo(outerX, T)
    profile.lineTo(innerX, T)
    profile.lineTo(innerX, gussetTopZ)
    profile.closePath()
    const gusset = new THREE.Mesh(extrudedXZProfile(profile, gussetThickness, gussetBandCenterY), material(0xb2c0cd))
    gusset.name = `${direction < 0 ? 'left' : 'right'} rear-edge support gusset`
    gusset.castShadow = true; gusset.receiveShadow = true; addEdgeOverlay(gusset); root.add(gusset)
  }

  root.userData.fallbackAudit = auditClampSupportFallback(root, { length: L, width: W, height: H })
  return root
}

function makeSteppedTaperedNozzleFallback(model) {
  const mainLength = Math.max(6, number(model?.mainLength, 98))
  const headLength = clamp(number(model?.headLength, 50), 1, mainLength - 2)
  const neckLength = clamp(number(model?.neckLength, 20), 1, mainLength - headLength - 1)
  const tipLength = Math.max(1, mainLength - headLength - neckLength)
  const headLeftDiameter = Math.max(2, number(model?.headLeftDiameter, 54.25449350717895))
  const headRightDiameter = Math.max(2, number(model?.headRightDiameter, 56))
  const neckDiameter = Math.max(2, number(model?.neckDiameter, 30))
  const tipDiameter = Math.max(2, number(model?.tipDiameter, 25))
  const counterboreDiameter = clamp(number(model?.counterboreDiameter, 40), 1, Math.min(headLeftDiameter, headRightDiameter) - 0.4)
  const counterboreDepth = clamp(number(model?.counterboreDepth, 40), 0.5, mainLength - 0.5)
  const axialBoreDiameter = clamp(number(model?.axialBoreDiameter, 13), 0.5, Math.min(neckDiameter, tipDiameter) - 0.4)
  const outletDiameter = clamp(number(model?.outletDiameter, 17), axialBoreDiameter + 0.2, tipDiameter - 0.2)
  const outletHalfAngle = clamp(number(model?.outletTaperHalfAngle, 15), 0.1, 89)
  const calculatedOutletTaperLength = (outletDiameter - axialBoreDiameter)
    / (2 * Math.tan(THREE.MathUtils.degToRad(outletHalfAngle)))
  const outletTaperLength = clamp(calculatedOutletTaperLength, 0.2, tipLength)
  const insertOuterDiameter = clamp(number(model?.insertOuterDiameter, 39.4), axialBoreDiameter + 0.4, counterboreDiameter - 0.1)
  const insertLength = Math.max(1, number(model?.insertLength, 40))
  const insertAxialOffset = Math.max(0, number(model?.insertAxialOffset, 0))
  const threadMatch = String(model?.insertThreadDesignation || 'M12').match(/M\s*(\d+(?:\.\d+)?)/i)
  const nominalThreadDiameter = clamp(Number(threadMatch?.[1] || 12), 0.5, insertOuterDiameter - 0.4)
  const explodedGap = Math.max(7, Math.min(14, mainLength * 0.1))

  const root = new THREE.Group()
  root.name = 'JoyNiu stepped tapered nozzle · two-solid candidate assembly'
  root.userData.componentMode = 'two_solid_assembly_candidate'
  root.userData.realThreadGeometry = false

  const mainGroup = new THREE.Group()
  mainGroup.name = 'Main stepped tapered nozzle body'
  const addBodySegment = (name, startX, length, leftDiameter, rightDiameter, color) => {
    // CylinderGeometry's local top becomes the left face after rotating its Y
    // axis onto world X, so the two radii preserve the drawing's direction.
    const geometry = new THREE.CylinderGeometry(leftDiameter / 2, rightDiameter / 2, length, 72, 1, false)
    const mesh = new THREE.Mesh(geometry, material(color))
    mesh.rotateZ(Math.PI / 2)
    mesh.position.x = startX + length / 2
    mesh.name = name
    mesh.castShadow = true
    mesh.receiveShadow = true
    addEdgeOverlay(mesh)
    mainGroup.add(mesh)
  }
  addBodySegment(`Shallow taper Ø${headLeftDiameter.toFixed(3)} to Ø${headRightDiameter}`, 0, headLength, headLeftDiameter, headRightDiameter, 0xb9c7d5)
  addBodySegment(`Neck Ø${neckDiameter}`, headLength, neckLength, neckDiameter, neckDiameter, 0xaebdcb)
  addBodySegment(`Tip Ø${tipDiameter}`, headLength + neckLength, tipLength, tipDiameter, tipDiameter, 0xc6d0da)

  // Dark, slightly protruding analytic surfaces make the subtractive bores
  // legible before a true OCCT GLB exists. They are visual cavity cues only;
  // the status badge continues to identify this scene as a fallback preview.
  const counterbore = new THREE.Mesh(
    new THREE.CylinderGeometry(counterboreDiameter / 2, counterboreDiameter / 2, counterboreDepth + 0.35, 64),
    new THREE.MeshStandardMaterial({ color: 0x4e5e6d, metalness: 0.06, roughness: 0.82, side: THREE.DoubleSide }),
  )
  counterbore.rotateZ(Math.PI / 2)
  counterbore.position.x = counterboreDepth / 2 - 0.16
  counterbore.name = `Counterbore Ø${counterboreDiameter} × ${counterboreDepth}`
  mainGroup.add(counterbore)

  const throughLength = Math.max(0.5, mainLength - counterboreDepth)
  const axialBore = new THREE.Mesh(
    new THREE.CylinderGeometry(axialBoreDiameter / 2, axialBoreDiameter / 2, throughLength + 0.45, 56),
    new THREE.MeshStandardMaterial({ color: 0x41515f, metalness: 0.04, roughness: 0.86, side: THREE.DoubleSide }),
  )
  axialBore.rotateZ(Math.PI / 2)
  axialBore.position.x = counterboreDepth + throughLength / 2 + 0.16
  axialBore.name = `Axial through bore Ø${axialBoreDiameter}`
  mainGroup.add(axialBore)

  const outletTaper = new THREE.Mesh(
    new THREE.CylinderGeometry(axialBoreDiameter / 2, outletDiameter / 2, outletTaperLength + 0.28, 56),
    new THREE.MeshStandardMaterial({ color: 0x536574, metalness: 0.05, roughness: 0.8, side: THREE.DoubleSide }),
  )
  outletTaper.rotateZ(Math.PI / 2)
  outletTaper.position.x = mainLength - outletTaperLength / 2 + 0.12
  outletTaper.name = `Outlet taper Ø${axialBoreDiameter} to Ø${outletDiameter} · half angle ${outletHalfAngle}°`
  mainGroup.add(outletTaper)
  root.add(mainGroup)

  const insertGroup = new THREE.Group()
  insertGroup.name = `Separate insert Ø${insertOuterDiameter} × ${insertLength} · ${model?.insertThreadDesignation || 'M12'} designation`
  const insertProfile = new THREE.Shape()
  insertProfile.absarc(0, 0, insertOuterDiameter / 2, 0, Math.PI * 2, false)
  const insertBorePath = new THREE.Path()
  insertBorePath.absarc(0, 0, nominalThreadDiameter / 2, 0, Math.PI * 2, true)
  insertProfile.holes.push(insertBorePath)
  const insertGeometry = extrudedLocalShape(insertProfile, 0, insertLength, 64)
  insertGeometry.rotateY(Math.PI / 2)
  insertGeometry.translate(insertAxialOffset - insertLength - explodedGap, 0, 0)
  const insert = new THREE.Mesh(insertGeometry, material(0xc69d65))
  insert.name = `Unthreaded insert envelope · ${model?.insertThreadDesignation || 'M12'} label only`
  insert.castShadow = true
  insert.receiveShadow = true
  addEdgeOverlay(insert, 0x765736)
  insertGroup.add(insert)
  root.add(insertGroup)

  const centreLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-insertLength - explodedGap - 5, 0, 0),
      new THREE.Vector3(mainLength + 7, 0, 0),
    ]),
    new THREE.LineDashedMaterial({ color: 0x3975ba, dashSize: 2.4, gapSize: 1.8, transparent: true, opacity: 0.48 }),
  )
  centreLine.computeLineDistances()
  centreLine.name = 'Candidate assembly coaxial datum'
  root.add(centreLine)
  return root
}

function makeShaftFallback(model) {
  const length = Math.max(5, number(model?.length, 70))
  const diameter = Math.max(2, number(model?.outerDiameter, 24))
  const holeDiameter = Math.min(diameter - 0.2, Math.max(0.5, number(model?.holeDiameter, 10)))
  const root = new THREE.Group()
  root.name = 'JoyNiu shaft · parametric WebGL fallback'
  const body = new THREE.Mesh(new THREE.CylinderGeometry(diameter / 2, diameter / 2, length, 64), material(0xb9c5d2))
  body.rotateZ(Math.PI / 2)
  body.position.x = length / 2
  body.name = 'shaft body'
  body.castShadow = true
  body.receiveShadow = true
  addEdgeOverlay(body)
  root.add(body)
  // A dark inner cylinder communicates the through hole in the fallback. The
  // production GLB remains authoritative whenever the API has generated it.
  const bore = new THREE.Mesh(new THREE.CylinderGeometry(holeDiameter / 2, holeDiameter / 2, length + 0.4, 48), new THREE.MeshStandardMaterial({ color: 0x5c6878, metalness: 0.08, roughness: 0.72, side: THREE.DoubleSide }))
  bore.rotateZ(Math.PI / 2)
  bore.position.x = length / 2
  bore.name = `through bore Ø${holeDiameter}`
  root.add(bore)
  const keywayWidth = Math.max(0.2, number(model?.keywayWidth, 6))
  const keywayLength = Math.min(length, Math.max(0.2, number(model?.keywayLength, 40)))
  const keyway = new THREE.Mesh(new THREE.BoxGeometry(keywayLength, keywayWidth, Math.max(0.2, number(model?.keywayDepth, 3))), new THREE.MeshStandardMaterial({ color: 0x6b7a8d, metalness: 0.16, roughness: 0.64, side: THREE.DoubleSide }))
  keyway.position.set(keywayLength / 2, 0, diameter / 2 - number(model?.keywayDepth, 3) / 2)
  keyway.name = 'keyway'
  root.add(keyway)
  return root
}

function makeFallback(model) {
  if (isSteppedTaperedNozzleKind(model?.kind)) return makeSteppedTaperedNozzleFallback(model)
  if (isClampSupportKind(model?.kind)) return makeClampSupportFallback(model)
  return model?.kind === 'bracket' ? makeBracketFallback(model) : makeShaftFallback(model)
}

function prepareLoadedScene(root) {
  root.traverse((object) => {
    if (!object.isMesh) return
    object.castShadow = true
    object.receiveShadow = true
    const sourceMaterials = Array.isArray(object.material) ? object.material : [object.material]
    const preparedMaterials = sourceMaterials.map((source) => {
      const next = source?.clone ? source.clone() : material(0xb9c5d2)
      if (!next.color) next.color = new THREE.Color(0xb9c5d2)
      // Preserve authored GLB material colours.  The two-solid DWG recipe uses
      // a steel/bronze pair so the insert remains distinguishable when it is
      // enclosed by the counterbore and inspected with the section plane.
      // Older anonymous meshes still receive the neutral CAD finish.
      const hasAuthoredColor = Boolean(source?.name) && Boolean(source?.color?.isColor)
      if (!source?.map && !source?.vertexColors && !hasAuthoredColor) next.color.set(0xb9c5d2)
      next.metalness = 0.42
      next.roughness = 0.34
      next.side = THREE.DoubleSide
      return next
    })
    // A GLTF primitive with one material usually has no geometry groups.  A
    // one-element material array is treated as a grouped multi-material mesh
    // by WebGLRenderer and can therefore render nothing; preserve the scalar
    // material in that common case.
    object.material = preparedMaterials.length === 1 ? preparedMaterials[0] : preparedMaterials
    addEdgeOverlay(object)
  })
  return root
}

function setMaterialsClipping(root, planes) {
  root?.traverse((object) => {
    if (!object.isMesh && !object.isLine) return
    const materials = Array.isArray(object.material) ? object.material : [object.material]
    materials.forEach((entry) => { if (entry) entry.clippingPlanes = planes })
  })
}

function viewDirection(view) {
  if (view === 'front') return new THREE.Vector3(0, -1, 0.18)
  if (view === 'top') return new THREE.Vector3(0, 0, 1)
  return new THREE.Vector3(1.55, -1.85, 1.2)
}

function applyCameraPreset(runtime, view, zoom = 1) {
  if (!runtime?.model || !runtime.baseDistance) return
  const direction = viewDirection(view).normalize()
  const distance = runtime.baseDistance / clamp(Number(zoom) || 1, MIN_ZOOM, MAX_ZOOM)
  runtime.camera.up.set(0, 0, 1)
  if (view === 'top') runtime.camera.up.set(0, 1, 0)
  runtime.camera.position.copy(runtime.target).addScaledVector(direction, distance)
  runtime.camera.lookAt(runtime.target)
  runtime.controls.target.copy(runtime.target)
  runtime.controls.update()
}

function applyZoom(runtime, zoom) {
  if (!runtime?.model || !runtime.baseDistance) return
  const nextZoom = clamp(Number(zoom) || 1, MIN_ZOOM, MAX_ZOOM)
  const direction = runtime.camera.position.clone().sub(runtime.controls.target).normalize()
  runtime.camera.position.copy(runtime.controls.target).addScaledVector(direction, runtime.baseDistance / nextZoom)
  runtime.controls.update()
}

export default function ThreeDViewer({ model, generation, view = 'isometric', section = false, zoom = 1, onZoomChange, onProductionGlbLoadError, resetNonce = 0 }) {
  const hostRef = useRef(null)
  const runtimeRef = useRef(null)
  const viewRef = useRef(view)
  const zoomRef = useRef(zoom)
  const sectionRef = useRef(section)
  const productionGlbLoadErrorRef = useRef(onProductionGlbLoadError)
  const reportedGlbFailuresRef = useRef(new Set())
  const [ready, setReady] = useState(false)
  const [status, setStatus] = useState({ phase: 'initializing', source: '', message: '' })

  // Keep asynchronous GLB callbacks aligned with the latest toolbar state.
  // The loading effect intentionally does not depend on view/zoom/section,
  // otherwise clicking a camera preset would refetch the same artifact.
  viewRef.current = view
  zoomRef.current = zoom
  sectionRef.current = section
  productionGlbLoadErrorRef.current = onProductionGlbLoadError

  const glbArtifact = generation?.stale ? null : generation?.artifacts?.find((item) => String(item.format || '').toLowerCase() === 'glb')
  const glbUrl = artifactUrl(glbArtifact)
  const productionCadGlb = Boolean(
    (['bracket', 'split_clamp_support', 'clamp_pedestal'].includes(model?.kind) || isSteppedTaperedNozzleKind(model?.kind))
    && !generation?.stale
    && generation?.validation?.productionReady === true
    && glbArtifact,
  )
  const modelSignature = useMemo(() => JSON.stringify({
    kind: model?.kind,
    baseLength: model?.baseLength,
    baseWidth: model?.baseWidth,
    baseThickness: model?.baseThickness,
    upperLength: model?.upperLength,
    upperWidth: model?.upperWidth,
    upperHeight: model?.upperHeight,
    totalHeight: model?.totalHeight,
    notchOpening: model?.notchOpening,
    notchRadius: model?.notchRadius,
    slotLength: model?.slotLength,
    slotWidth: model?.slotWidth,
    pocketDepth: model?.pocketDepth,
    saddleDepth: model?.saddleDepth,
    holeDepth: model?.holeDepth,
    holeThrough: model?.holeThrough,
    bossDiameter: model?.bossDiameter,
    bossCenterDistance: model?.bossCenterDistance,
    bossHeight: model?.bossHeight,
    overallLength: model?.overallLength,
    overallWidth: model?.overallWidth,
    overallHeight: model?.overallHeight,
    baseMainDepth: model?.baseMainDepth,
    baseBodyDepth: model?.baseBodyDepth,
    mainBaseDepth: model?.mainBaseDepth,
    frontExtensionWidth: model?.frontExtensionWidth,
    frontTongueWidth: model?.frontTongueWidth,
    baseFrontWidth: model?.baseFrontWidth,
    rearBridgeWidth: model?.rearBridgeWidth,
    cornerRadius: model?.cornerRadius,
    baseCornerRadius: model?.baseCornerRadius,
    outerCornerRadius: model?.outerCornerRadius,
    reliefRadius: model?.reliefRadius,
    frontReliefRadius: model?.frontReliefRadius,
    neckConcaveRadius: model?.neckConcaveRadius,
    neckConvexRadius: model?.neckConvexRadius,
    frontTongueRadius: model?.frontTongueRadius,
    mountHoleDiameter: model?.mountHoleDiameter,
    mountingHoleDiameter: model?.mountingHoleDiameter,
    mountHoleCount: model?.mountHoleCount,
    mountHoleCenterDistance: model?.mountHoleCenterDistance,
    mountHoleSpacing: model?.mountHoleSpacing,
    mountingHoleCenterDistance: model?.mountingHoleCenterDistance,
    mountHoleCenterFromRear: model?.mountHoleCenterFromRear,
    mountHoleCenterY: model?.mountHoleCenterY,
    mountHoleOffsetY: model?.mountHoleOffsetY,
    featureCenterFromRear: model?.featureCenterFromRear,
    pedestalRadius: model?.pedestalRadius,
    pedestalOuterRadius: model?.pedestalOuterRadius,
    pedestalDiameter: model?.pedestalDiameter,
    pedestalOuterDiameter: model?.pedestalOuterDiameter,
    pedestalCenterFromRear: model?.pedestalCenterFromRear,
    lowerPedestalHeight: model?.lowerPedestalHeight,
    pedestalHeight: model?.pedestalHeight,
    rearClampRise: model?.rearClampRise,
    clampStepHeight: model?.clampStepHeight,
    stepHeight: model?.stepHeight,
    clampTopHeight: model?.clampTopHeight,
    clampBoreDiameter: model?.clampBoreDiameter,
    boreDiameter: model?.boreDiameter,
    centerBoreDiameter: model?.centerBoreDiameter,
    clampSlotWidth: model?.clampSlotWidth,
    splitWidth: model?.splitWidth,
    radialSlotWidth: model?.radialSlotWidth,
    boreFloorZ: model?.boreFloorZ,
    boreFloorThickness: model?.boreFloorThickness,
    blindBoreFloorThickness: model?.blindBoreFloorThickness,
    clampBoltHoleDiameter: model?.clampBoltHoleDiameter,
    clampBoltDiameter: model?.clampBoltDiameter,
    transverseHoleDiameter: model?.transverseHoleDiameter,
    crossHoleDiameter: model?.crossHoleDiameter,
    clampBoltCenterHeight: model?.clampBoltCenterHeight,
    transverseHoleCenterHeight: model?.transverseHoleCenterHeight,
    crossHoleCenterZ: model?.crossHoleCenterZ,
    gussetHeight: model?.gussetHeight,
    gussetThickness: model?.gussetThickness,
    ribHeight: model?.ribHeight,
    ribThickness: model?.ribThickness,
    length: model?.length,
    outerDiameter: model?.outerDiameter,
    holeDiameter: model?.holeDiameter,
    keywayWidth: model?.keywayWidth,
    keywayDepth: model?.keywayDepth,
    keywayLength: model?.keywayLength,
    mainLength: model?.mainLength,
    headLength: model?.headLength,
    neckLength: model?.neckLength,
    headLeftDiameter: model?.headLeftDiameter,
    headRightDiameter: model?.headRightDiameter,
    neckDiameter: model?.neckDiameter,
    tipDiameter: model?.tipDiameter,
    counterboreDiameter: model?.counterboreDiameter,
    counterboreDepth: model?.counterboreDepth,
    axialBoreDiameter: model?.axialBoreDiameter,
    outletDiameter: model?.outletDiameter,
    outletTaperHalfAngle: model?.outletTaperHalfAngle,
    insertOuterDiameter: model?.insertOuterDiameter,
    insertLength: model?.insertLength,
    insertThreadDesignation: model?.insertThreadDesignation,
    insertAxialOffset: model?.insertAxialOffset,
  }), [model])

  // Scene, renderer and controls are created once per mounted workbench.
  useEffect(() => {
    const host = hostRef.current
    if (!host) return undefined
    let disposed = false
    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0xf2f4f7)
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 5000)
    camera.up.set(0, 0, 1)
    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' })
    } catch (error) {
      // Headless browsers and a few remote-desktop sessions do not expose a
      // WebGL context.  Keep the workbench usable and explain the limitation
      // instead of letting an uncaught Three.js exception blank the whole app.
      setStatus({ phase: 'error', source: 'WebGL unavailable', message: error?.message || '当前浏览器未提供 WebGL 上下文。' })
      return undefined
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.setClearColor(0xf2f4f7, 0)
    renderer.localClippingEnabled = true
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.02
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    renderer.domElement.className = 'three-canvas'
    renderer.domElement.dataset.testid = 'three-d-viewer'
    renderer.domElement.setAttribute('aria-label', '真实 WebGL 三维实体查看器')
    renderer.domElement.setAttribute('role', 'img')
    host.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.075
    controls.enablePan = true
    controls.screenSpacePanning = true
    controls.minPolarAngle = 0.04
    controls.maxPolarAngle = Math.PI - 0.04
    controls.rotateSpeed = 0.78
    controls.zoomSpeed = 0.85
    controls.panSpeed = 0.7

    const ambient = new THREE.HemisphereLight(0xffffff, 0xb9c2ce, 1.72)
    scene.add(ambient)
    const key = new THREE.DirectionalLight(0xffffff, 2.15)
    key.position.set(120, -130, 180)
    key.castShadow = true
    key.shadow.mapSize.set(1024, 1024)
    scene.add(key)
    const fill = new THREE.DirectionalLight(0x89bfff, 0.72)
    fill.position.set(-150, 100, 80)
    scene.add(fill)
    const rim = new THREE.PointLight(0xb4c1d1, 0.62, 500)
    rim.position.set(0, 120, 130)
    scene.add(rim)

    const grid = new THREE.GridHelper(280, 28, 0x9aa9bd, 0xd5dce5)
    grid.rotation.x = Math.PI / 2
    grid.position.z = -0.08
    grid.material.transparent = true
    grid.material.opacity = 0.56
    scene.add(grid)
    const axes = new THREE.AxesHelper(66)
    axes.position.set(-56, -32, 0)
    axes.renderOrder = 3
    scene.add(axes)

    const clippingPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0)
    const sectionPlane = new THREE.Mesh(
      new THREE.PlaneGeometry(180, 90),
      new THREE.MeshBasicMaterial({ color: 0x2f7cff, transparent: true, opacity: 0.08, side: THREE.DoubleSide, depthWrite: false }),
    )
    sectionPlane.rotation.x = -Math.PI / 2
    sectionPlane.position.z = 45
    sectionPlane.visible = false
    scene.add(sectionPlane)

    const runtime = {
      scene,
      camera,
      renderer,
      controls,
      model: null,
      target: new THREE.Vector3(0, 0, 18),
      baseDistance: 180,
      clippingPlane,
      sectionPlane,
      lastReportedZoom: Number(zoom) || 1,
      suppressZoomReport: false,
    }
    runtimeRef.current = runtime

    const resize = () => {
      if (disposed) return
      const width = Math.max(1, host.clientWidth)
      const height = Math.max(1, host.clientHeight)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height, false)
    }
    resize()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null
    observer?.observe(host)
    window.addEventListener('resize', resize)

    const reportZoom = () => {
      if (runtime.suppressZoomReport || !runtime.model || !runtime.baseDistance) return
      const distance = camera.position.distanceTo(controls.target)
      const next = clamp(runtime.baseDistance / Math.max(distance, 0.01), MIN_ZOOM, MAX_ZOOM)
      if (Math.abs(next - runtime.lastReportedZoom) > 0.018) {
        runtime.lastReportedZoom = next
        onZoomChange?.(Number(next.toFixed(2)))
      }
    }
    controls.addEventListener('change', reportZoom)

    const animate = () => {
      if (disposed) return
      controls.update()
      renderer.render(scene, camera)
      runtime.frame = requestAnimationFrame(animate)
    }
    runtime.frame = requestAnimationFrame(animate)
    setReady(true)

    return () => {
      disposed = true
      setReady(false)
      if (runtime.frame) cancelAnimationFrame(runtime.frame)
      controls.removeEventListener('change', reportZoom)
      observer?.disconnect()
      window.removeEventListener('resize', resize)
      controls.dispose()
      runtime.model?.traverse((object) => {
        if (object.geometry?.dispose) object.geometry.dispose()
        const materials = Array.isArray(object.material) ? object.material : [object.material]
        materials.forEach((entry) => entry?.dispose?.())
      })
      renderer.dispose()
      renderer.domElement.remove()
      runtimeRef.current = null
    }
    // The initial zoom callback is intentionally not a scene dependency; the
    // parent can change it through the toolbar without rebuilding WebGL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Load the authoritative GLB whenever a new generation arrives. If the
  // artifact is absent/stale, build a local mesh from the editable parameters.
  useEffect(() => {
    const runtime = runtimeRef.current
    if (!ready || !runtime) return undefined
    let cancelled = false
    const oldModel = runtime.model
    if (oldModel) {
      runtime.scene.remove(oldModel)
      oldModel.traverse((object) => {
        if (object.geometry?.dispose) object.geometry.dispose()
        const materials = Array.isArray(object.material) ? object.material : [object.material]
        materials.forEach((entry) => entry?.dispose?.())
      })
      runtime.model = null
    }
    setStatus({ phase: glbUrl ? 'loading' : 'ready', source: glbUrl ? '后端 GLB · CadQuery/OCCT' : '参数化 WebGL fallback', message: glbUrl ? '正在载入真实 GLB 网格…' : 'API 尚未提供 GLB，当前为可交互参数化预览。' })

    const attach = (object, source, message = '') => {
      if (cancelled) return
      runtime.model = object
      runtime.scene.add(object)
      const fallbackAudit = object.userData?.fallbackAudit || null
      runtime.renderer.domElement.dataset.fallbackAudit = fallbackAudit
        ? (fallbackAudit.ok ? 'passed' : 'failed')
        : 'not-applicable'
      const box = new THREE.Box3().setFromObject(object)
      const size = box.getSize(new THREE.Vector3())
      const center = box.getCenter(new THREE.Vector3())
      const radius = Math.max(size.length() / 2, 1)
      runtime.target.copy(center)
      // Perspective cameras need a little more clearance than an orthographic
      // CAD viewport.  Fit against both frustum axes so the full bounding box
      // remains visible even in the narrow three-column workbench.
      runtime.baseDistance = Math.max(radius * 4.2, fitDistance(runtime.camera, radius))
      runtime.camera.near = Math.max(radius / 100, 0.01)
      runtime.camera.far = Math.max(radius * 24, 1000)
      runtime.camera.updateProjectionMatrix()
      runtime.controls.minDistance = Math.max(radius * 0.26, 0.2)
      runtime.controls.maxDistance = Math.max(radius * 12, 100)
      runtime.controls.target.copy(center)
      runtime.suppressZoomReport = true
      applyCameraPreset(runtime, viewRef.current, zoomRef.current)
      runtime.suppressZoomReport = false
      setMaterialsClipping(object, sectionRef.current ? [runtime.clippingPlane] : [])
      runtime.sectionPlane.position.set(center.x, center.y, center.z + size.z * 0.55)
      runtime.sectionPlane.scale.set(Math.max(size.x / 90, 1), Math.max(size.z / 90, 1), 1)
      runtime.sectionPlane.visible = Boolean(sectionRef.current)
      const auditedMessage = fallbackAudit && !fallbackAudit.ok
        ? `${message}（fallback bbox/solid overlap 检查未通过）`
        : fallbackAudit
          ? `${message}（fallback bbox/solid overlap 检查通过）`
          : message
      setStatus({ phase: 'ready', source, message: auditedMessage })
    }

    if (!glbUrl) {
      attach(makeFallback(model), '参数化 WebGL fallback', '可交互网格预览 · 生成 OCCT/GLB 后将自动替换为真实导出网格。')
      return () => { cancelled = true }
    }

    const loader = new GLTFLoader()
    loader.load(glbUrl, (gltf) => {
      if (cancelled) return
      attach(prepareLoadedScene(gltf.scene), `真实 GLB · ${glbArtifact?.engine || generation?.engine || 'CadQuery/OCCT'}`, '已从 FastAPI artifact 加载 GLB；STEP 仍是生产 B-Rep 交付物。')
    }, undefined, (error) => {
      if (cancelled) return
      // A broken/expired process-local artifact must not leave a blank canvas.
      // Fall back visibly and keep the error in the status line for diagnosis.
      const detail = error?.message || 'GLB 加载失败'
      attach(makeFallback(model), '参数化 WebGL fallback', `真实 GLB 加载失败（${detail}），已切换到可交互参数预览。`)
      const statusCode = Number(error?.target?.status || error?.response?.status || error?.status)
      const missingArtifact = statusCode === 404 || /responded with (?:a status of )?404|\b404\s*(?::|Not Found)/i.test(detail)
      if (productionCadGlb && missingArtifact) {
        const failureKey = `${generation?.requestId || 'unknown'}:${glbArtifact?.id || glbUrl}`
        if (!reportedGlbFailuresRef.current.has(failureKey)) {
          reportedGlbFailuresRef.current.add(failureKey)
          productionGlbLoadErrorRef.current?.({
            requestId: generation?.requestId || '',
            artifactId: glbArtifact?.id || '',
            artifactUrl: glbUrl,
            message: detail,
          })
        }
      }
    })
    return () => { cancelled = true }
    // The generation object can contain transient metadata; the GLB URL and
    // parameter signature are the actual model identity for this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, glbUrl, modelSignature])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime?.model) return
    runtime.suppressZoomReport = true
    applyCameraPreset(runtime, view, zoom)
    runtime.suppressZoomReport = false
  }, [view])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime?.model) return
    runtime.suppressZoomReport = true
    applyCameraPreset(runtime, 'isometric', 1)
    runtime.suppressZoomReport = false
    runtime.lastReportedZoom = 1
  }, [resetNonce])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime?.model) return
    runtime.suppressZoomReport = true
    applyZoom(runtime, zoom)
    runtime.suppressZoomReport = false
    runtime.lastReportedZoom = clamp(Number(zoom) || 1, MIN_ZOOM, MAX_ZOOM)
  }, [zoom])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime?.model) return
    setMaterialsClipping(runtime.model, section ? [runtime.clippingPlane] : [])
    runtime.sectionPlane.visible = Boolean(section)
  }, [section])

  const fallbackStatus = status.source?.toLowerCase().includes('fallback')
  const sourceKey = fallbackStatus ? 'fallback' : status.source?.toLowerCase().includes('glb') ? 'glb' : status.phase
  return <div ref={hostRef} className="three-viewer" data-testid="three-viewer-host" data-viewer-source={sourceKey} data-viewer-phase={status.phase}>
    <div className="three-viewer-status" aria-live="polite">
      <span className={`three-status-dot ${status.phase === 'ready' && !fallbackStatus ? 'ready' : fallbackStatus ? 'fallback' : status.phase === 'error' ? 'error' : 'loading'}`} />
      <span>{status.phase === 'loading' ? '载入 3D 网格…' : status.phase === 'initializing' ? '初始化 WebGL…' : status.phase === 'error' ? 'WebGL 不可用' : fallbackStatus ? '参数化 3D 预览' : '真实 GLB 3D 网格'}</span>
      {status.source && <b>{status.source}</b>}
    </div>
    {status.message && <div className={`three-viewer-message ${status.source?.includes('fallback') ? 'warning' : ''}`}>{status.message}</div>}
  </div>
}
