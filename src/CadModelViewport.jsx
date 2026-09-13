import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { API_BASE } from './api.js'
import { CAD_VIEW_NAMES, CAD_VIEW_COLORS, cadCameraPreset, cadCanvasActive, cadFallbackUrl, cadPickTolerance, cadPlaneForFace, cadSelectionKey, captureCadCamera, createCadViewportTopology, fitCadCamera, nextCadSelection, restoreCadCamera } from './cadModelViewport.js'
import './cad-model-viewport.css'

function disposeObject(root) {
  const geometries = new Set(), materials = new Set(), textures = new Set()
  root?.traverse(node => {
    if (node.geometry) geometries.add(node.geometry)
    for (const material of Array.isArray(node.material) ? node.material : [node.material]) if (material) {
      materials.add(material)
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value)
    }
  })
  geometries.forEach(value => value.dispose()); materials.forEach(value => value.dispose()); textures.forEach(value => value.dispose())
}

async function fallbackObject(fallback, token, signal) {
  let buffer
  if (fallback?.blob instanceof Blob) buffer = await fallback.blob.arrayBuffer()
  else if (fallback?.arrayBuffer instanceof ArrayBuffer) buffer = fallback.arrayBuffer
  else {
    const url = cadFallbackUrl(typeof fallback === 'string' ? fallback : fallback?.glbUrl || fallback?.url, API_BASE)
    const response = await fetch(url, { signal, credentials: 'omit', headers: token && !url.startsWith('blob:') ? { Authorization: `Bearer ${token}` } : {} })
    if (!response.ok) throw new Error(`暂时无法读取模型预览（${response.status}）。`)
    if (Number(response.headers.get('content-length')) > 40 * 1024 * 1024) throw new Error('模型预览文件超过大小限制。')
    buffer = await response.arrayBuffer()
  }
  if (signal.aborted) throw new DOMException('已取消', 'AbortError')
  if (buffer.byteLength > 40 * 1024 * 1024) throw new Error('模型预览文件超过大小限制。')
  const manager = new THREE.LoadingManager()
  manager.setURLModifier(url => { if (url.startsWith('data:') || url.startsWith('blob:')) return url; throw new Error('模型预览包含不支持的外部资源。') })
  const gltf = await new GLTFLoader(manager).parseAsync(buffer, '')
  if (signal.aborted) { disposeObject(gltf.scene); throw new DOMException('已取消', 'AbortError') }
  gltf.scene.traverse(node => {
    if (!node.isMesh) return
    const old = Array.isArray(node.material) ? node.material : [node.material]
    old.forEach(material => { for (const value of Object.values(material || {})) if (value?.isTexture) value.dispose(); material?.dispose() })
    node.material = new THREE.MeshStandardMaterial({ color: CAD_VIEW_COLORS.body, metalness: 0.12, roughness: 0.67, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 })
    node.add(new THREE.LineSegments(new THREE.EdgesGeometry(node.geometry, 24), new THREE.LineBasicMaterial({ color: CAD_VIEW_COLORS.edge, transparent: true, opacity: 0.85 })))
  })
  return gltf.scene
}

/**
 * geometry: current display tessellation; selectionGeometry: optional original
 * input tessellation used while previewing a transaction. Picked references
 * always come from the latter, including geometryVersion and signature.
 * onSelect(nextSelection, hit) receives a real worldPoint and optional plane.
 * onPlaneSelect(frame, face) is an explicit user action, never an auto-edit.
 * fallback is a GLB URL/blob only: it cannot provide editable topology.
 * documentScope resets framing only when the active account/document changes.
 */
export default function CadModelViewport({ geometry, selectionGeometry, selection = [], onSelect, pickKind = 'all', view = 'iso', onView, active = true, fallback, previewHint, token, onPlaneSelect, appearance = 'shaded', wireframe = false, className = '', documentScope, annotations = [], onAnnotationEdit, construction = [],controlPoints,onControlPointChange,onCaptureReady }) {
  const host = useRef(null), triad = useRef(null), runtime = useRef(null), latest = useRef(null)
  const cameraSnapshot = useRef(null)
  const annotationHost = useRef(null)
  const [error, setError] = useState(''), [loading, setLoading] = useState(false), [hover, setHover] = useState(null)
  const [localView, setLocalView] = useState(view), [navigation, setNavigation] = useState('rotate'), [retry, setRetry] = useState(0)
  const [renderReady, setRenderReady] = useState(false)
  const source = selectionGeometry || geometry
  const display = geometry || selectionGeometry
  const fallbackUrl = typeof fallback === 'string' ? fallback : fallback?.glbUrl || fallback?.url
  const displayStyle = wireframe || appearance === 'wireframe' ? 'wireframe' : 'shaded'
  latest.current = { selection, onSelect, pickKind, onView, active, token: token || fallback?.token, view: localView, navigation, displayStyle, annotations, construction,controlPoints,onControlPointChange,onCaptureReady }
  const selectionKey = JSON.stringify(selection.map(value => [cadSelectionKey(value), value.selector?.geometryVersion, value.selector?.signature]))
  const selectedFace = (Array.isArray(source?.faces) ? source.faces : []).find(face => face.selector && selection.some(value => value.kind === 'face' && value.id === face.id && (!value.selector || (value.selector.geometryVersion === source.geometryVersion && value.selector.signature === face.selector.signature))))
  const selectedPlane = cadPlaneForFace(selectedFace)

  useEffect(() => {
    if (!host.current) return
    if (cameraSnapshot.current && !Object.is(cameraSnapshot.current.documentScope, documentScope)) cameraSnapshot.current = null
    const initialView = cadCameraPreset(latest.current.view) ? latest.current.view : 'iso'
    if (!cameraSnapshot.current) setLocalView(initialView)
    const abort = new AbortController(), element = host.current
    let scene, camera, renderer, controls, topology, fallbackRoot, helperRoot, helperHandles=[], dragHandle=null, observer, frame, disposed = false, pointer = null
    const remove = []
    setLoading(Boolean(display || fallback)); setError(''); setHover(null); setRenderReady(false)
    function listen(target, event, handler, options) { target.addEventListener(event, handler, options); remove.push(() => target.removeEventListener(event, handler, options)) }
    const visible = () => !disposed && cadCanvasActive({ active: latest.current.active, hidden: document.hidden, width: element.clientWidth, height: element.clientHeight })
    const pause = () => { if (frame !== undefined) cancelAnimationFrame(frame); frame = undefined }
    function axisDirections() {
      if (!triad.current || !camera) return
      const inverse = camera.quaternion.clone().invert()
      for (const [i, axis] of ['X', 'Y', 'Z'].entries()) {
        const direction = new THREE.Vector3(...[0, 1, 2].map(value => Number(value === i))).applyQuaternion(inverse)
        const x = 40 + direction.x * 25, y = 40 - direction.y * 25
        const line = triad.current.querySelector(`[data-axis="${axis}"]`), label = triad.current.querySelector(`[data-label="${axis}"]`)
        line?.setAttribute('x2', x); line?.setAttribute('y2', y); label?.setAttribute('x', x + 3); label?.setAttribute('y', y - 3)
      }
    }
    function render() {
      frame = undefined
      if (!visible() || !renderer) return
      controls.update(); axisDirections(); renderer.render(scene, camera)
      const project = point => { const p = new THREE.Vector3(...point).project(camera); return [(p.x+1)*element.clientWidth/2,(1-p.y)*element.clientHeight/2,p.z] }
      for (const note of latest.current.annotations) {
        const node = annotationHost.current?.querySelector(`[data-pmi="${note.id}"]`), line = annotationHost.current?.querySelector(`[data-pmi-line="${note.id}"]`)
        const position = project(note.position)
        if (node) { node.style.left=`${position[0]}px`;node.style.top=`${position[1]}px`;node.style.visibility=Math.abs(position[2])>1?'hidden':'visible' }
        if (line) line.setAttribute('points',[...(note.points||[]),note.position].map(p=>project(p).slice(0,2).join(',')).join(' '))
      }
      for(const [row,points] of (latest.current.controlPoints||[]).entries())for(const [col,point] of points.entries()){const node=annotationHost.current?.querySelector(`[data-grid-control="${row}-${col}"]`);if(node){const p=project(point);node.style.left=`${p[0]}px`;node.style.top=`${p[1]}px`;node.style.visibility=Math.abs(p[2])>1?'hidden':'visible'}}
      frame = requestAnimationFrame(render)
    }
    function resize() {
      if (!visible() || !renderer) { pause(); return }
      const width = element.clientWidth, height = element.clientHeight, centerX = (camera.left + camera.right) / 2
      const halfWidth = (camera.top - camera.bottom) * width / height / 2
      camera.left = centerX - halfWidth; camera.right = centerX + halfWidth; camera.updateProjectionMatrix()
      renderer.setSize(width, height); topology?.setResolution(width, height)
      if (frame === undefined) render()
    }
    function updateHelpers() {
      if(!scene)return
      if(dragHandle){dragHandle=null;if(controls)controls.enabled=true}
      if(helperRoot){scene.remove(helperRoot);disposeObject(helperRoot)}
      helperRoot=new THREE.Group();helperHandles=[]
      const extent=topology?.bounds.getSize(new THREE.Vector3()).length()||100,span=Math.max(extent*.38,10)
      const line=(points,color='#737fad')=>{const geometry=new THREE.BufferGeometry().setFromPoints(points.map(p=>new THREE.Vector3(...p)));helperRoot.add(new THREE.Line(geometry,new THREE.LineBasicMaterial({color,transparent:true,opacity:.8})))}
      for(const ref of latest.current.construction){
        if(ref.kind==='plane'){
          const f=ref.frame,x=new THREE.Vector3(...f.xDir),y=new THREE.Vector3(...f.normal).cross(x),o=new THREE.Vector3(...f.origin)
          const corners=[[-1,-1],[1,-1],[1,1],[-1,1],[-1,-1]].map(([a,b])=>o.clone().addScaledVector(x,a*span).addScaledVector(y,b*span).toArray());line(corners)
        }else if(ref.kind==='axis')line([ref.start,ref.end],'#327fba')
        else if(ref.kind==='helix')line(ref.points,'#8470ab')
        else {const point=new THREE.Mesh(new THREE.SphereGeometry(span*.018,8,8),new THREE.MeshBasicMaterial({color:'#3d72b8'}));point.position.set(...ref.point);helperRoot.add(point)}
      }
      const grid=latest.current.controlPoints||[]
      grid.forEach((row,r)=>{line(row,'#287ece');row.forEach((point,c)=>{if(!point.every(Number.isFinite))return;const node=new THREE.Mesh(new THREE.SphereGeometry(Math.max(extent*.009,.3),12,8),new THREE.MeshBasicMaterial({color:'#076fff',depthTest:false}));node.position.set(...point);node.renderOrder=5;node.userData={row:r,col:c};helperHandles.push(node);helperRoot.add(node)})})
      if(grid.length)for(let c=0;c<grid[0].length;c++)line(grid.map(row=>row[c]),'#287ece')
      scene.add(helperRoot)
    }
    async function start() {
      if(!display&&!fallback&&!latest.current.construction.length&&!latest.current.controlPoints?.length)return
      scene = new THREE.Scene(); scene.background = new THREE.Color('#eceef5')
      let bounds
      if (display) {
        topology = createCadViewportTopology(display, source); scene.add(topology.group); bounds = topology.bounds
      } else if(fallback) {
        fallbackRoot = await fallbackObject(fallback, latest.current.token, abort.signal)
        if (disposed) { disposeObject(fallbackRoot); return }
        scene.add(fallbackRoot); bounds = new THREE.Box3().setFromObject(fallbackRoot)
      }
      if(!bounds)bounds=new THREE.Box3(new THREE.Vector3(-50,-50,-50),new THREE.Vector3(50,50,50))
      if (bounds.isEmpty()) throw new Error('当前预览没有可显示的实体。')
      const extent = Math.max(bounds.getSize(new THREE.Vector3()).length(), 0.01)
      camera = new THREE.OrthographicCamera(-1, 1, 1, -1, extent / 10000, extent * 100)
      const restoredTarget = restoreCadCamera(camera, cameraSnapshot.current, documentScope)
      const target = restoredTarget || fitCadCamera(camera, bounds, { view:latest.current.view, aspect:Math.max(element.clientWidth, 1) / Math.max(element.clientHeight, 1) })
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
      renderer.outputColorSpace = THREE.SRGBColorSpace
      renderer.domElement.setAttribute('aria-label', 'CAD 三维画布：拖动旋转，右键拖动平移，滚轮缩放，点击选择面或边')
      renderer.domElement.tabIndex = 0; element.appendChild(renderer.domElement)
      controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true; controls.dampingFactor = 0.1
      controls.target.copy(target); controls.update()
      controls.minZoom = 0.04; controls.maxZoom = 100
      controls.mouseButtons.LEFT = latest.current.navigation === 'pan' ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE
      controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN; controls.mouseButtons.RIGHT = THREE.MOUSE.PAN
      controls.touches.ONE = THREE.TOUCH.ROTATE; controls.touches.TWO = THREE.TOUCH.DOLLY_PAN
      const fit = (preset, preserveDirection = false) => {
        controls.target.copy(fitCadCamera(camera, bounds, { view: preset, aspect: Math.max(element.clientWidth, 1) / Math.max(element.clientHeight, 1), preserveDirection })); controls.update(); resize()
      }
      scene.add(new THREE.HemisphereLight('#ffffff', '#777482', 2.7))
      const center = bounds.getCenter(new THREE.Vector3())
      const key = new THREE.DirectionalLight('#ffffff', 2.2); key.position.copy(center).add(new THREE.Vector3(extent, -extent * 2, extent * 3)); key.target.position.copy(center); scene.add(key, key.target)
      const fill = new THREE.DirectionalLight('#c8c7dc', 1.2); fill.position.copy(center).add(new THREE.Vector3(-extent * 2, extent, extent)); fill.target.position.copy(center); scene.add(fill, fill.target)
      updateHelpers()
      const raycaster = new THREE.Raycaster()
      let hovered = null
      const repaint = () => topology?.highlight(latest.current.selection, hovered)
      function pick(event) {
        if (!topology || !visible() || latest.current.pickKind === 'none') return null
        const rect = renderer.domElement.getBoundingClientRect()
        if (!rect.width || !rect.height) return null
        raycaster.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2), camera)
        return topology.pick(raycaster, latest.current.pickKind, cadPickTolerance(camera, rect.height))
      }
      function updateHover(hit) {
        if (cadSelectionKey(hit) === cadSelectionKey(hovered)) return
        hovered = hit; setHover(hit); repaint()
        renderer.domElement.style.cursor = hit ? 'pointer' : latest.current.navigation === 'pan' ? 'grab' : 'default'
      }
      function controlRay(event){const rect=renderer.domElement.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,1-(event.clientY-rect.top)/rect.height*2),camera);return raycaster}
      listen(renderer.domElement,'pointerdown',event=>{if(event.button!==0||!latest.current.active)return;const hit=controlRay(event).intersectObjects(helperHandles)[0];if(!hit)return;event.stopImmediatePropagation();event.preventDefault();const normal=new THREE.Vector3();camera.getWorldDirection(normal);dragHandle={node:hit.object,plane:new THREE.Plane().setFromNormalAndCoplanarPoint(normal,hit.object.position),id:event.pointerId};controls.enabled=false;renderer.domElement.setPointerCapture?.(event.pointerId)},true)
      listen(renderer.domElement,'pointermove',event=>{if(!dragHandle||event.pointerId!==dragHandle.id)return;event.stopImmediatePropagation();const target=new THREE.Vector3();if(controlRay(event).ray.intersectPlane(dragHandle.plane,target))dragHandle.node.position.copy(target)},true)
      listen(renderer.domElement,'pointerup',event=>{if(!dragHandle||event.pointerId!==dragHandle.id)return;event.stopImmediatePropagation();const {node}=dragHandle;dragHandle=null;controls.enabled=true;renderer.domElement.releasePointerCapture?.(event.pointerId);latest.current.onControlPointChange?.(node.userData.row,node.userData.col,node.position.toArray().map(v=>Number(v.toFixed(6))))},true)
      listen(renderer.domElement,'pointercancel',()=>{if(dragHandle){dragHandle=null;controls.enabled=true;updateHelpers()}},true)
      listen(renderer.domElement, 'pointerdown', event => { if (event.button === 0) pointer = { x: event.clientX, y: event.clientY, id: event.pointerId, dragged: false }; else pointer = null })
      listen(renderer.domElement, 'pointermove', event => {
        if (pointer && event.pointerId === pointer.id && Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > 5) {
          if (!pointer.dragged && latest.current.navigation === 'rotate') { setLocalView('custom'); latest.current.onView?.('custom') }
          pointer.dragged = true; updateHover(null)
        } else if (!event.buttons) updateHover(pick(event))
      })
      listen(renderer.domElement, 'pointerup', event => {
        const click = pointer && pointer.id === event.pointerId && !pointer.dragged && event.button === 0 && Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) <= 5
        pointer = null
        if (!click || !topology || latest.current.pickKind === 'none') return
        const additive = Boolean(event.ctrlKey || event.metaKey), found = pick(event), hit = found ? { ...found, additive } : null
        latest.current.onSelect?.(nextCadSelection(latest.current.selection, hit, additive), hit)
        updateHover(hit)
      })
      listen(renderer.domElement, 'pointerleave', () => updateHover(null))
      listen(renderer.domElement, 'pointercancel', () => { pointer = null; updateHover(null) })
      listen(renderer.domElement, 'keydown', event => {
        if (!latest.current.active || event.ctrlKey || event.metaKey || event.altKey) return
        if (event.key === 'Escape') { latest.current.onSelect?.([], null); updateHover(null); event.preventDefault() }
        else if (event.key.toLowerCase() === 'f') { fit(latest.current.view, true); event.preventDefault() }
        else if (event.key === '+' || event.key === '=' || event.key === '-') { camera.zoom = THREE.MathUtils.clamp(camera.zoom * (event.key === '-' ? 1 / 1.2 : 1.2), controls.minZoom, controls.maxZoom); camera.updateProjectionMatrix(); event.preventDefault() }
      })
      const style = () => {
        topology?.setAppearance(latest.current.displayStyle)
        fallbackRoot?.traverse(node => { if (node.isMesh) node.material.wireframe = latest.current.displayStyle === 'wireframe' })
      }
      runtime.current = { resize, fit, repaint, style,updateHelpers, chooseView: preset => fit(preset), zoom(factor) { camera.zoom = THREE.MathUtils.clamp(camera.zoom * factor, controls.minZoom, controls.maxZoom); camera.updateProjectionMatrix() }, navigation(mode) { controls.mouseButtons.LEFT = mode === 'pan' ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE; controls.touches.ONE = mode === 'pan' ? THREE.TOUCH.PAN : THREE.TOUCH.ROTATE; renderer.domElement.style.cursor = mode === 'pan' ? 'grab' : 'default' } }
      latest.current.onCaptureReady?.(()=>{renderer.render(scene,camera);const width=element.clientWidth,height=element.clientHeight,project=point=>{const p=new THREE.Vector3(...point).project(camera);return [(p.x+1)*width/2,(1-p.y)*height/2]};return {image:renderer.domElement.toDataURL('image/png'),width,height,annotations:latest.current.annotations.map(note=>({...note,position:project(note.position),points:(note.points||[]).map(project)}))}})
      repaint(); style(); listen(document, 'visibilitychange', resize)
      observer = new ResizeObserver(resize); observer.observe(element); resize(); setRenderReady(true)
    }
    start().catch(reason => { if (!disposed && reason.name !== 'AbortError') setError(reason.message || '无法显示当前实体，请重新加载。') }).finally(() => { if (!disposed) setLoading(false) })
    return () => {
      if (camera && controls && (display||fallback)) cameraSnapshot.current = captureCadCamera(camera, controls.target, documentScope)
      disposed = true; abort.abort(); pause(); observer?.disconnect(); remove.forEach(fn => fn()); controls?.dispose(); runtime.current = null
      latest.current.onCaptureReady?.(null);topology?.dispose(); disposeObject(fallbackRoot);disposeObject(helperRoot); renderer?.dispose(); renderer?.domElement.remove()
    }
  }, [display, source, fallbackUrl, fallback?.blob, fallback?.arrayBuffer, retry, documentScope,Boolean(construction.length),Boolean(controlPoints?.length)])

  useEffect(() => { runtime.current?.updateHelpers() }, [construction,controlPoints])
  useEffect(() => { runtime.current?.repaint() }, [selectionKey])
  useEffect(() => { runtime.current?.resize() }, [active])
  useEffect(() => { runtime.current?.navigation(navigation) }, [navigation])
  useEffect(() => { runtime.current?.style() }, [displayStyle])
  useEffect(() => { if (cadCameraPreset(view)) { setLocalView(view); runtime.current?.chooseView(view) } }, [view])
  const chooseView = next => { setLocalView(next); runtime.current?.chooseView(next); onView?.(next) }
  const status = hover ? `${hover.kind === 'face' ? '面' : '边'} ${hover.index + 1}${hover.plane ? ' · 平面' : ''}` : selection.length ? `已选择 ${selection.length} 项` : source ? pickKind === 'edge' ? '选择实体上的边' : pickKind === 'face' ? '选择实体上的面' : pickKind === 'none' ? '查看实体' : '选择面或边' : '仅查看模型'

  return <section className={`cad-model-viewport ${className}`} aria-label="CAD 模型画布">
    <div ref={host} className="cmv-canvas" />
    <div ref={annotationHost} className="cmv-pmi-overlay" aria-label="三维 PMI 标注">{(controlPoints||[]).flatMap((row,r)=>row.map((_point,c)=><span key={`${r}-${c}`} data-grid-control={`${r}-${c}`} className="cmv-grid-handle-marker" aria-hidden="true"/>))}<svg aria-hidden="true">{annotations.map(note=><polyline key={note.id} data-pmi-line={note.id}/>)}</svg>{annotations.map(note=><button key={note.id} data-pmi={note.id} className="cmv-pmi-label" title="双击编辑标注" onDoubleClick={()=>onAnnotationEdit?.(note.id)}>{note.displayText}</button>)}</div>
    <div className="cmv-viewcube" aria-label="标准视角">
      <div className="cmv-cube-faces"><button type="button" className="cmv-cube-top" aria-label="上视图" aria-pressed={localView === 'top'} disabled={!renderReady} onClick={() => chooseView('top')}>上</button><button type="button" className="cmv-cube-front" aria-label="前视图" aria-pressed={localView === 'front'} disabled={!renderReady} onClick={() => chooseView('front')}>前</button><button type="button" className="cmv-cube-right" aria-label="右视图" aria-pressed={localView === 'right'} disabled={!renderReady} onClick={() => chooseView('right')}>右</button></div>
      <button type="button" className="cmv-iso" aria-pressed={localView === 'iso'} disabled={!renderReady} onClick={() => chooseView('iso')}>等轴测</button>
    </div>
    <div className="cmv-navigation" aria-label="画布操作">
      <button type="button" aria-label="拖动旋转模型" aria-pressed={navigation === 'rotate'} disabled={!renderReady} onClick={() => setNavigation('rotate')}>旋转</button><button type="button" aria-label="拖动平移模型" aria-pressed={navigation === 'pan'} disabled={!renderReady} onClick={() => setNavigation('pan')}>平移</button>
      <i aria-hidden="true" /><button type="button" aria-label="放大模型" disabled={!renderReady} onClick={() => runtime.current?.zoom(1.25)}>＋</button><button type="button" aria-label="缩小模型" disabled={!renderReady} onClick={() => runtime.current?.zoom(0.8)}>−</button><button type="button" disabled={!renderReady} onClick={() => runtime.current?.fit(localView, true)}>适合窗口</button>
    </div>
    <svg ref={triad} className="cmv-triad" viewBox="0 0 80 80" aria-label="全局坐标轴">{[['X', '#ae605c'], ['Y', '#52836f'], ['Z', '#537bae']].map(([axis, color]) => <g key={axis} stroke={color} fill={color}><line data-axis={axis} x1="40" y1="40" x2="40" y2="40" strokeWidth="1.6" /><text data-label={axis} x="40" y="40" stroke="none">{axis}</text></g>)}<circle cx="40" cy="40" r="2" fill="#74717e" /></svg>
    <div className="cmv-status"><span>{CAD_VIEW_NAMES[localView] || '自由视角'}</span><span role="status">{status}</span>{onPlaneSelect && selectedPlane && <button type="button" disabled={!renderReady} onClick={() => onPlaneSelect(selectedPlane, { kind: 'face', id: selectedFace.id, index: selectedFace.selector.index, selector: { ...selectedFace.selector }, plane: selectedPlane })}>在所选平面创建草图</button>}</div>
    {loading && <div className="cmv-loading" role="status">正在载入实体…</div>}
    {error && <div className="cmv-error" role="alert"><p>{error}</p><button type="button" onClick={() => setRetry(value => value + 1)}>重新加载</button></div>}
    {!display && !fallback && !construction.length && !controlPoints?.length && <div className="cmv-empty"><span>◈</span><p>创建或打开模型后，在这里选择面和边</p></div>}
    {!source && fallback && !loading && !error && <p className="cmv-preview-note">{previewHint || '此预览仅供查看；重建实体后可选择面和边。'}</p>}
    <p className="cmv-help">拖动旋转 · 右键平移 · 滚轮缩放 · Ctrl / ⌘ 多选</p>
  </section>
}
