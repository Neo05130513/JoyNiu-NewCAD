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

function material(color = 0xa8bfd2, opacity = 1) {
  return new THREE.MeshStandardMaterial({
    color,
    metalness: 0.58,
    roughness: 0.28,
    transparent: opacity < 1,
    opacity,
    side: THREE.DoubleSide,
  })
}

function addEdgeOverlay(mesh, color = 0x172c42) {
  if (!mesh?.geometry) return
  const edges = new THREE.EdgesGeometry(mesh.geometry, 32)
  const lines = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity: 0.72,
    depthTest: true,
  }))
  lines.name = `${mesh.name || 'mesh'} · edges`
  lines.renderOrder = 2
  mesh.add(lines)
}

function makeBracketFallback(model) {
  const L = Math.max(20, number(model?.baseLength, 100))
  const W = Math.max(16, number(model?.baseWidth, 50))
  const T = Math.max(1, number(model?.baseThickness, 10))
  const UL = Math.min(L - 2, Math.max(4, number(model?.upperLength, 70)))
  const UW = Math.min(W - 2, Math.max(4, number(model?.upperWidth, 30)))
  const H = Math.max(T + 1, number(model?.totalHeight, T + number(model?.upperHeight, 30)))
  const opening = Math.min(UL - 2, Math.max(2, number(model?.notchOpening, 40)))
  const radius = Math.min(opening / 2, Math.max(1, number(model?.notchRadius, 15)))
  const bossDiameter = Math.min(Math.min(L, W) - 2, Math.max(2, number(model?.bossDiameter, 20)))
  const bossDistance = Math.min(L - bossDiameter, Math.max(bossDiameter, number(model?.bossCenterDistance, 70)))
  // Keep the same origin convention as the CadQuery recipe: X/Y are centred
  // on the base plate and Z starts at the bottom face.  This makes the local
  // fallback visually interchangeable with the downloaded GLB.
  const x0 = -UL / 2
  const x1 = UL / 2
  const cx = 0
  const notchLeft = cx - opening / 2
  const notchRight = cx + opening / 2

  const root = new THREE.Group()
  root.name = 'JoyNiu bracket · parametric WebGL fallback'
  const base = new THREE.Mesh(new THREE.BoxGeometry(L, W, T), material(0x8ea8be))
  base.name = 'Base plate'
  base.position.z = T / 2
  base.castShadow = true
  base.receiveShadow = true
  addEdgeOverlay(base)
  root.add(base)

  // Make the upper body from its actual X/Z profile and extrude it through
  // the drawing's 30 mm depth.  The lower semicircle is the R15 saddle, so
  // this fallback has the same silhouette as the OCCT result.
  const profile = new THREE.Shape()
  profile.moveTo(x0, T)
  profile.lineTo(x1, T)
  profile.lineTo(x1, H)
  profile.lineTo(notchRight, H)
  for (let index = 0; index <= 24; index += 1) {
    const angle = (Math.PI * index) / 24
    profile.lineTo(cx + radius * Math.cos(angle), H - radius * Math.sin(angle))
  }
  profile.lineTo(notchLeft, H)
  profile.lineTo(x0, H)
  profile.closePath()
  const upperGeometry = new THREE.ExtrudeGeometry(profile, {
    depth: UW,
    steps: 1,
    curveSegments: 32,
    bevelEnabled: false,
  })
  // ExtrudeGeometry grows along local Z.  Rotate it so the profile's local Y
  // is world Z and the 30 mm depth is world Y, centred on the part.
  upperGeometry.rotateX(Math.PI / 2)
  upperGeometry.translate(0, UW / 2, 0)
  upperGeometry.computeVertexNormals()
  const upper = new THREE.Mesh(upperGeometry, material(0xa9bfd1))
  upper.name = 'Upper body with R15 saddle'
  upper.castShadow = true
  upper.receiveShadow = true
  addEdgeOverlay(upper)
  root.add(upper)

  const bossRadius = bossDiameter / 2
  const bossHeight = Math.max(1, number(model?.bossHeight, H - T))
  for (const x of [-bossDistance / 2, bossDistance / 2]) {
    const bossGeometry = new THREE.CylinderGeometry(bossRadius, bossRadius, bossHeight, 64)
    bossGeometry.rotateX(Math.PI / 2)
    const boss = new THREE.Mesh(bossGeometry, material(0x9eb8cb))
    boss.position.set(x, 0, T + bossHeight / 2)
    boss.name = `Boss Ø${bossDiameter}`
    boss.castShadow = true
    boss.receiveShadow = true
    addEdgeOverlay(boss, 0x213a52)
    root.add(boss)
  }

  // A small centre line makes the model orientation obvious without turning
  // the viewport into a drawing overlay.
  const centre = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-L / 2 - 8, 0, T + 0.04),
      new THREE.Vector3(L / 2 + 8, 0, T + 0.04),
    ]),
    new THREE.LineDashedMaterial({ color: 0x78a9d4, dashSize: 2, gapSize: 2, transparent: true, opacity: 0.42 }),
  )
  centre.computeLineDistances()
  centre.name = 'centre line'
  root.add(centre)
  return root
}

function makeShaftFallback(model) {
  const length = Math.max(5, number(model?.length, 70))
  const diameter = Math.max(2, number(model?.outerDiameter, 24))
  const holeDiameter = Math.min(diameter - 0.2, Math.max(0.5, number(model?.holeDiameter, 10)))
  const root = new THREE.Group()
  root.name = 'JoyNiu shaft · parametric WebGL fallback'
  const body = new THREE.Mesh(new THREE.CylinderGeometry(diameter / 2, diameter / 2, length, 64), material(0xa8bfd2))
  body.rotateZ(Math.PI / 2)
  body.position.x = length / 2
  body.name = 'shaft body'
  body.castShadow = true
  body.receiveShadow = true
  addEdgeOverlay(body)
  root.add(body)
  // A dark inner cylinder communicates the through hole in the fallback. The
  // production GLB remains authoritative whenever the API has generated it.
  const bore = new THREE.Mesh(new THREE.CylinderGeometry(holeDiameter / 2, holeDiameter / 2, length + 0.4, 48), new THREE.MeshStandardMaterial({ color: 0x142438, metalness: 0.05, roughness: 0.8, side: THREE.DoubleSide }))
  bore.rotateZ(Math.PI / 2)
  bore.position.x = length / 2
  bore.name = `through bore Ø${holeDiameter}`
  root.add(bore)
  const keywayWidth = Math.max(0.2, number(model?.keywayWidth, 6))
  const keywayLength = Math.min(length, Math.max(0.2, number(model?.keywayLength, 40)))
  const keyway = new THREE.Mesh(new THREE.BoxGeometry(keywayLength, keywayWidth, Math.max(0.2, number(model?.keywayDepth, 3))), new THREE.MeshStandardMaterial({ color: 0x29445d, metalness: 0.2, roughness: 0.7, side: THREE.DoubleSide }))
  keyway.position.set(keywayLength / 2, 0, diameter / 2 - number(model?.keywayDepth, 3) / 2)
  keyway.name = 'keyway'
  root.add(keyway)
  return root
}

function makeFallback(model) {
  return model?.kind === 'bracket' ? makeBracketFallback(model) : makeShaftFallback(model)
}

function prepareLoadedScene(root) {
  root.traverse((object) => {
    if (!object.isMesh) return
    object.castShadow = true
    object.receiveShadow = true
    const sourceMaterials = Array.isArray(object.material) ? object.material : [object.material]
    const preparedMaterials = sourceMaterials.map((source) => {
      const next = source?.clone ? source.clone() : material(0xa8bfd2)
      if (!next.color) next.color = new THREE.Color(0xa8bfd2)
      // The generated GLB has no user texture; a cool metal finish makes the
      // actual mesh readable against the dark workbench while retaining any
      // texture/color that a future exporter provides.
      if (!source?.map && !source?.vertexColors) next.color.set(0xa8bfd2)
      next.metalness = 0.58
      next.roughness = 0.28
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

export default function ThreeDViewer({ model, generation, view = 'isometric', section = false, zoom = 1, onZoomChange, resetNonce = 0 }) {
  const hostRef = useRef(null)
  const runtimeRef = useRef(null)
  const viewRef = useRef(view)
  const zoomRef = useRef(zoom)
  const sectionRef = useRef(section)
  const [ready, setReady] = useState(false)
  const [status, setStatus] = useState({ phase: 'initializing', source: '', message: '' })

  // Keep asynchronous GLB callbacks aligned with the latest toolbar state.
  // The loading effect intentionally does not depend on view/zoom/section,
  // otherwise clicking a camera preset would refetch the same artifact.
  viewRef.current = view
  zoomRef.current = zoom
  sectionRef.current = section

  const glbArtifact = generation?.stale ? null : generation?.artifacts?.find((item) => String(item.format || '').toLowerCase() === 'glb')
  const glbUrl = artifactUrl(glbArtifact)
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
    bossDiameter: model?.bossDiameter,
    bossCenterDistance: model?.bossCenterDistance,
    bossHeight: model?.bossHeight,
    length: model?.length,
    outerDiameter: model?.outerDiameter,
    holeDiameter: model?.holeDiameter,
    keywayWidth: model?.keywayWidth,
    keywayDepth: model?.keywayDepth,
    keywayLength: model?.keywayLength,
  }), [model])

  // Scene, renderer and controls are created once per mounted workbench.
  useEffect(() => {
    const host = hostRef.current
    if (!host) return undefined
    let disposed = false
    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x0b1522)
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
    renderer.setClearColor(0x0b1522, 0)
    renderer.localClippingEnabled = true
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.12
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

    const ambient = new THREE.HemisphereLight(0xc9e4ff, 0x132238, 1.45)
    scene.add(ambient)
    const key = new THREE.DirectionalLight(0xe9f5ff, 2.6)
    key.position.set(120, -130, 180)
    key.castShadow = true
    key.shadow.mapSize.set(1024, 1024)
    scene.add(key)
    const fill = new THREE.DirectionalLight(0x5e9ddd, 0.85)
    fill.position.set(-150, 100, 80)
    scene.add(fill)
    const rim = new THREE.PointLight(0x80b8ea, 0.9, 500)
    rim.position.set(0, 120, 130)
    scene.add(rim)

    const grid = new THREE.GridHelper(280, 28, 0x365572, 0x1b344d)
    grid.rotation.x = Math.PI / 2
    grid.position.z = -0.08
    grid.material.transparent = true
    grid.material.opacity = 0.42
    scene.add(grid)
    const axes = new THREE.AxesHelper(66)
    axes.position.set(-56, -32, 0)
    axes.renderOrder = 3
    scene.add(axes)

    const clippingPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0)
    const sectionPlane = new THREE.Mesh(
      new THREE.PlaneGeometry(180, 90),
      new THREE.MeshBasicMaterial({ color: 0xf2ae62, transparent: true, opacity: 0.07, side: THREE.DoubleSide, depthWrite: false }),
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
      setStatus({ phase: 'ready', source, message })
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

  return <div ref={hostRef} className="three-viewer" data-testid="three-viewer-host">
    <div className="three-viewer-status" aria-live="polite">
      <span className={`three-status-dot ${status.phase === 'ready' ? 'ready' : status.phase === 'error' ? 'error' : 'loading'}`} />
      <span>{status.phase === 'loading' ? '载入 3D 网格…' : status.phase === 'initializing' ? '初始化 WebGL…' : status.phase === 'error' ? 'WebGL 不可用' : 'WebGL 3D 实体'}</span>
      {status.source && <b>{status.source}</b>}
    </div>
    {status.message && <div className={`three-viewer-message ${status.source?.includes('fallback') ? 'warning' : ''}`}>{status.message}</div>}
  </div>
}
