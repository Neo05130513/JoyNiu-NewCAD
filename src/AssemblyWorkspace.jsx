import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import {
  applyPartTransform, assemblyFingerprint, checkAssembly, duplicatePartInstance,
  instanceBounds, mainModelEnvelope, validatePartDefinition, validateTransform,
} from './assemblyModel.js'
import './assembly-workspace.css'

function makePartMesh(item, selected) {
  const group = new THREE.Group()
  const d = item.dimensions
  const material = new THREE.MeshStandardMaterial({ color: selected ? 0x4286d9 : item.group === '轴承' ? 0x99a9b8 : item.group === '自定义' ? 0xc49b65 : 0xb8c5d0, metalness: 0.38, roughness: 0.43, side: THREE.DoubleSide })
  const addMesh = (geometry) => {
    const mesh = new THREE.Mesh(geometry, material)
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry, 24), new THREE.LineBasicMaterial({ color: selected ? 0x175399 : 0x5e7288, transparent: true, opacity: 0.5 }))
    mesh.add(edges)
    group.add(mesh)
    return mesh
  }
  if (item.shape === 'ring') {
    const profile = new THREE.Shape()
    profile.absarc(0, 0, d.outerDiameter / 2, 0, Math.PI * 2, false)
    const hole = new THREE.Path()
    hole.absarc(0, 0, d.innerDiameter / 2, 0, Math.PI * 2, true)
    profile.holes.push(hole)
    const geometry = new THREE.ExtrudeGeometry(profile, { depth: d.length, bevelEnabled: false, curveSegments: 48, steps: 1 })
    geometry.rotateY(Math.PI / 2)
    geometry.translate(-d.length / 2, 0, 0)
    addMesh(geometry)
  } else {
    const cylinder = new THREE.CylinderGeometry(d.outerDiameter / 2, d.outerDiameter / 2, d.length, 48)
    cylinder.rotateZ(Math.PI / 2)
    addMesh(cylinder)
    if (item.shape === 'bolt') {
      const head = new THREE.CylinderGeometry(d.headDiameter / 2, d.headDiameter / 2, d.headLength, 48)
      head.rotateZ(Math.PI / 2)
      head.translate(-d.length / 2 - d.headLength / 2, 0, 0)
      addMesh(head)
    }
  }
  group.rotation.set(...['x', 'y', 'z'].map((axis) => Number(item.rotation[axis]) * Math.PI / 180))
  group.position.set(...['x', 'y', 'z'].map((axis) => Number(item.position[axis])))
  return group
}

function AssemblyViewport({ model, items, selectedId, exploded, section, sectionZ, resetNonce }) {
  const hostRef = useRef(null)
  const savedView = useRef(null)
  const [error, setError] = useState('')
  const modelKey = JSON.stringify(model)
  const itemsKey = JSON.stringify(items)
  useEffect(() => {
    const host = hostRef.current
    if (!host) return undefined
    let renderer; let controls; let frame; let observer; let scene
    try {
      setError('')
      scene = new THREE.Scene()
      scene.background = new THREE.Color(0xf3f6fa)
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
      renderer.clippingPlanes = section ? [new THREE.Plane(new THREE.Vector3(0, 0, -1), sectionZ)] : []
      renderer.domElement.setAttribute('aria-label', '装配三维预览：拖动旋转，滚轮缩放。当前模型显示包络，标准件显示简化几何。')
      host.appendChild(renderer.domElement)
      const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 10000000)
      camera.up.set(0, 0, 1)
      controls = new OrbitControls(camera, renderer.domElement)
      controls.enableDamping = true
      controls.dampingFactor = 0.12
      const root = new THREE.Group()
      const envelope = mainModelEnvelope(model)
      if (envelope) {
        const boxGeometry = new THREE.BoxGeometry(envelope.length, envelope.width, envelope.height)
        const box = new THREE.Mesh(boxGeometry, new THREE.MeshStandardMaterial({ color: 0x8fa2b6, transparent: true, opacity: 0.15, depthWrite: false, side: THREE.DoubleSide }))
        box.position.set((envelope.min.x + envelope.max.x) / 2, 0, (envelope.min.z + envelope.max.z) / 2)
        box.add(new THREE.LineSegments(new THREE.EdgesGeometry(boxGeometry), new THREE.LineBasicMaterial({ color: 0x6a829b, transparent: true, opacity: 0.8 })))
        root.add(box)
      }
      items.forEach((item, index) => {
        if (validatePartDefinition(item).length || validateTransform(item.position, item.rotation).length) return
        const mesh = makePartMesh(item, item.id === selectedId)
        if (exploded) mesh.position.y += (index + 1) * exploded
        root.add(mesh)
      })
      scene.add(root)
      const boundingBox = new THREE.Box3().setFromObject(root)
      if (boundingBox.isEmpty()) boundingBox.set(new THREE.Vector3(-50, -50, -50), new THREE.Vector3(50, 50, 50))
      const center = boundingBox.getCenter(new THREE.Vector3())
      const size = boundingBox.getSize(new THREE.Vector3())
      const diameter = Math.max(size.length(), 20)
      const gridSize = Math.max(100, Math.ceil(diameter / 20) * 20)
      const grid = new THREE.GridHelper(gridSize, 20, 0xc0ccda, 0xe0e7ee)
      grid.rotation.x = Math.PI / 2
      grid.position.set(center.x, center.y, boundingBox.min.z - 1)
      scene.add(grid)
      scene.add(new THREE.AmbientLight(0xffffff, 2.1))
      const light = new THREE.DirectionalLight(0xffffff, 3.0)
      light.position.set(diameter, -diameter, diameter * 2)
      scene.add(light)
      const fill = new THREE.DirectionalLight(0xd4e4ff, 1.7)
      fill.position.set(-diameter, diameter, diameter)
      scene.add(fill)
      const axes = new THREE.AxesHelper(Math.max(12, Math.min(diameter * 0.16, 60)))
      scene.add(axes)
      const oldView = savedView.current
      const needsFit = !oldView || oldView.resetNonce !== resetNonce
      const resize = () => {
        const width = Math.max(host.clientWidth, 1); const height = Math.max(host.clientHeight, 1)
        renderer.setSize(width, height)
        camera.aspect = width / height
        camera.updateProjectionMatrix()
      }
      resize()
      if (needsFit) {
        const vertical = THREE.MathUtils.degToRad(camera.fov) / 2
        const horizontal = Math.atan(Math.tan(vertical) * camera.aspect)
        const distance = diameter * 0.62 / Math.sin(Math.min(vertical, horizontal))
        controls.target.copy(center)
        camera.position.copy(center).add(new THREE.Vector3(1.35, -1.7, 1.25).normalize().multiplyScalar(distance))
      } else {
        camera.position.fromArray(oldView.position)
        controls.target.fromArray(oldView.target)
      }
      controls.minDistance = diameter * 0.03
      controls.maxDistance = Math.max(diameter * 30, camera.position.distanceTo(controls.target) * 2)
      controls.update()
      observer = new ResizeObserver(resize)
      observer.observe(host)
      const render = () => { controls.update(); renderer.render(scene, camera); frame = requestAnimationFrame(render) }
      render()
      return () => {
        savedView.current = { position: camera.position.toArray(), target: controls.target.toArray(), resetNonce }
        cancelAnimationFrame(frame)
        observer?.disconnect()
        controls.dispose()
        scene.traverse((object) => {
          object.geometry?.dispose()
          const materials = object.material ? (Array.isArray(object.material) ? object.material : [object.material]) : []
          materials.forEach((material) => material.dispose())
        })
        renderer.dispose()
        renderer.domElement.remove()
      }
    } catch (cause) {
      cancelAnimationFrame(frame)
      observer?.disconnect()
      controls?.dispose()
      renderer?.dispose()
      renderer?.domElement.remove()
      setError(`三维预览暂不可用：${cause.message}。仍可编辑实例位置和运行尺寸预检查。`)
      return undefined
    }
  }, [modelKey, itemsKey, selectedId, exploded, section, sectionZ, resetNonce])
  return <div className="aw-viewport" ref={hostRef}>{error && <div className="aw-webgl-error" role="alert">{error}</div>}</div>
}

function TransformEditor({ item, onApply, canAlign, onAlign }) {
  const [position, setPosition] = useState(item.position)
  const [rotation, setRotation] = useState(item.rotation)
  const [errors, setErrors] = useState([])
  useEffect(() => { setPosition(item.position); setRotation(item.rotation); setErrors([]) }, [item])
  const submit = (event) => {
    event.preventDefault()
    const issues = validateTransform(position, rotation)
    setErrors(issues)
    if (!issues.length) onApply(applyPartTransform(item, position, rotation))
  }
  return <form className="aw-transform" onSubmit={submit}><h3>实例位置</h3><p>位置为杆段 / 环件中心，旋转顺序 XYZ。</p>{[['位置 / mm', position, setPosition], ['旋转 / °', rotation, setRotation]].map(([label, values, setValues]) => <fieldset key={label}><legend>{label}</legend><div className="aw-coordinate-fields">{['x', 'y', 'z'].map((axis) => <label key={axis}>{axis.toUpperCase()}<input aria-label={`${label.split(' / ')[0]} ${axis.toUpperCase()}`} type="number" step="any" value={values[axis]} onChange={(event) => setValues({ ...values, [axis]: event.target.value })} /></label>)}</div></fieldset>)}{errors.length > 0 && <div className="aw-error" role="alert">{errors.join(' ')}</div>}<div className="aw-actions"><button className="aw-primary" type="submit">应用位置</button>{canAlign && <button type="button" onClick={onAlign}>对齐主件 X 轴</button>}</div></form>
}

const fitLabels = { unaligned: '轴线尚未对齐', separated: '轴向尚未接触', 'too-small': '内径过小', 'nominal-contact': '公称直径相等', clearance: '存在公称径向间隙' }

export default function AssemblyWorkspace({ model, assemblyItems = [], setAssemblyItems, onBackToModel, onOpenLibrary, showToast }) {
  const [selectedId, setSelectedId] = useState(assemblyItems[0]?.id || '')
  const [exploded, setExploded] = useState(false)
  const [explodeGap, setExplodeGap] = useState(25)
  const [section, setSection] = useState(false)
  const [sectionZ, setSectionZ] = useState(0)
  const [resetNonce, setResetNonce] = useState(0)
  const [report, setReport] = useState(null)
  const [undo, setUndo] = useState(null)
  const selected = assemblyItems.find((item) => item.id === selectedId) || null
  const fingerprint = assemblyFingerprint(model, assemblyItems)
  const stale = report && report.fingerprint !== fingerprint
  const envelope = mainModelEnvelope(model)
  const limits = useMemo(() => {
    const list = [...(envelope ? [envelope] : []), ...assemblyItems.filter((item) => !validatePartDefinition(item).length && !validateTransform(item.position, item.rotation).length).map(instanceBounds)]
    return { min: Math.floor(Math.min(0, ...list.map((box) => box.min.z)) - 5), max: Math.ceil(Math.max(10, ...list.map((box) => box.max.z)) + 5) }
  }, [fingerprint])
  useEffect(() => { if (selectedId && !assemblyItems.some((item) => item.id === selectedId)) setSelectedId(assemblyItems[0]?.id || '') }, [assemblyItems, selectedId])
  const replaceItem = (item) => { setAssemblyItems((items) => items.map((current) => current.id === item.id ? item : current)); showToast?.('实例位置已更新', 'success') }
  const removeItem = () => {
    const index = assemblyItems.findIndex((item) => item.id === selectedId)
    if (index < 0) return
    setUndo({ item: assemblyItems[index], index })
    setAssemblyItems((items) => items.filter((item) => item.id !== selectedId))
  }
  const undoRemoval = () => {
    if (!undo) return
    setAssemblyItems((items) => {
      if (items.some((item) => item.id === undo.item.id)) return items
      const restored = [...items]
      restored.splice(Math.min(undo.index, restored.length), 0, undo.item)
      return restored
    })
    setSelectedId(undo.item.id)
    setUndo(null)
  }
  const runCheck = () => { setReport(checkAssembly(model, assemblyItems)) }
  const errors = report?.issues.filter((issue) => issue.severity === 'error') || []
  const warnings = report?.issues.filter((issue) => issue.severity === 'warning') || []
  return <div className="aw-workspace">
    <header className="aw-heading"><div><span className="aw-eyebrow">ASSEMBLY WORKSPACE</span><h1>{model?.name || '当前零件'} · 装配</h1><p>{assemblyItems.length + 1} 个实例 · 当前模型为固定基准 · 长度单位 mm</p></div><div className="aw-actions"><button onClick={onBackToModel}>返回 3D 建模</button><button onClick={onOpenLibrary}>＋ 添加标准件</button><button className="aw-primary" onClick={runCheck}>运行预检查</button></div></header>
    <div className="aw-assembly-layout"><main className="aw-card aw-view-card"><div className="aw-view-toolbar"><span>装配空间</span><div className="aw-actions"><button aria-pressed={exploded} className={exploded ? 'selected' : ''} onClick={() => { setExploded((value) => !value); setResetNonce((n) => n + 1) }}>爆炸视图</button><button aria-pressed={section} className={section ? 'selected' : ''} onClick={() => setSection((value) => !value)}>剖切 Z</button><button onClick={() => setResetNonce((value) => value + 1)}>适合窗口</button></div></div>
      {(exploded || section) && <div className="aw-view-options">{exploded && <label>爆炸间距 <input aria-label="爆炸间距" type="range" min="5" max="100" step="5" value={explodeGap} onChange={(event) => setExplodeGap(Number(event.target.value))} /><output>{explodeGap} mm</output></label>}{section && <label>剖切高度 <input aria-label="剖切高度" type="range" min={limits.min} max={limits.max} step="0.5" value={sectionZ} onChange={(event) => setSectionZ(Number(event.target.value))} /><output>{sectionZ} mm</output></label>}</div>}
      <AssemblyViewport model={model} items={assemblyItems} selectedId={selectedId} exploded={exploded ? explodeGap : 0} section={section} sectionZ={sectionZ} resetNonce={resetNonce} />
      <div className="aw-view-legend"><span><i className="aw-envelope-key" /> 当前模型包络</span><span><i className="aw-solid-key" /> 标准件简化几何</span><span>X 红 · Y 绿 · Z 蓝</span></div><p className="aw-scope">拖动旋转，滚轮缩放。{exploded ? '爆炸位移仅影响显示，保存位置和预检查仍使用装配坐标。' : '半透明框是主件外形包络，未显示其孔槽或内部结构。'}{section && '剖切只改变显示，不改变零件。'}</p>
      {!assemblyItems.length && <div className="aw-first-part"><div><h2>添加第一个装配实例</h2><p>从轴承、紧固件开始，也可创建自定义圆柱或套筒。</p></div><button className="aw-primary" onClick={onOpenLibrary}>打开标准件库 →</button></div>}
      <section className="aw-check-panel" aria-live="polite"><div className="aw-section-heading"><h2>装配预检查</h2>{report && <button onClick={runCheck}>{stale ? '重新检查' : '重新运行'}</button>}</div>{!report ? <p className="aw-muted">点击“运行预检查”，按已保存的位置计算包络重叠与可判定的轴孔间隙。</p> : stale ? <div className="aw-warning" role="status">模型或实例已变化，上一份结果已失效。请重新运行预检查。</div> : <><div className={`aw-check-summary ${errors.length ? 'error' : warnings.length ? 'warning' : ''}`}>{errors.length ? `发现 ${errors.length} 项尺寸 / 数据问题` : warnings.length ? `发现 ${warnings.length} 处包络重叠待复核` : '本次未发现包络重叠或可判定的尺寸冲突'}<small>该结论不代表实体无干涉或装配可制造。</small></div>{report.issues.length > 0 && <ul className="aw-issues">{report.issues.map((issue, index) => <li className={issue.severity} key={`${issue.code}-${index}`}>{issue.message}</li>)}</ul>}{report.fits.length > 0 && <div className="aw-fit-list">{report.fits.map((fit) => { const item = assemblyItems.find((entry) => entry.id === fit.itemId); return <div key={fit.itemId}><b>{item?.name} · {item?.spec}</b><span>{fitLabels[fit.status]}</span><small>轴线偏移 {fit.axisOffset} mm · 公称径向间隙 {fit.radialClearance} mm</small></div> })}</div>}<p className="aw-scope">检查范围：{report.scope}</p></>}</section>
    </main><aside className="aw-card aw-inspector"><h2>装配树 <span>{assemblyItems.length + 1} 实例</span></h2><div className="aw-current-model"><span>◇</span><div><b>{model?.name || '当前零件'}</b><small>固定 · 世界坐标基准</small></div></div><div className="aw-tree">{assemblyItems.map((item, index) => <button key={item.id} className={selectedId === item.id ? 'selected' : ''} aria-pressed={selectedId === item.id} onClick={() => setSelectedId(item.id)}><span>{String(index + 1).padStart(2, '0')}</span><div><b>{item.name}</b><small>{item.spec}</small></div></button>)}</div>{!assemblyItems.length && <p className="aw-muted">当前仅有主件；插入标准件后可编辑其位置。</p>}
      {selected && <><div className="aw-instance-spec"><b>{selected.spec}</b><small>{selected.source}</small><small>实例 {selected.id.slice(0, 8)} · 独立位置</small></div><TransformEditor key={selected.id} item={selected} onApply={replaceItem} canAlign={Boolean(envelope?.axial)} onAlign={() => replaceItem(applyPartTransform(selected, { x: envelope.length / 2, y: 0, z: 0 }, { x: 0, y: 0, z: 0 }))} /><div className="aw-instance-actions"><button onClick={() => { const copy = duplicatePartInstance(selected); setAssemblyItems((items) => [...items, copy]); setSelectedId(copy.id) }}>复制实例</button><button className="aw-remove" onClick={removeItem}>移除实例</button></div></>}
      {undo && <div className="aw-undo" role="status"><span>已移除 {undo.item.name}</span><button onClick={undoRemoval}>撤销</button></div>}
    </aside></div>
  </div>
}
