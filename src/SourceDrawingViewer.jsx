import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import './source-drawing.css'

const MIN_ZOOM = 1
const MAX_ZOOM = 12
const clamp = (value, min, max) => Math.min(max, Math.max(min, value))
const validBox = (box) => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
  && box[0] >= 0 && box[1] >= 0 && box[2] > 0 && box[3] > 0
  && box[0] + box[2] <= 1.001 && box[1] + box[3] <= 1.001
const pageKey = (documentId, pageId) => JSON.stringify([documentId, pageId])
const formatValue = (value, unit) => `${value === null || value === undefined || value === '' ? '—' : String(value)}${unit ? ` ${unit}` : ''}`
const isExactLocation = (location) => location.precision !== 'region' && validBox(location.bbox)

function SourceDimensionCrop({ page, location, number, onLocate }) {
  const width = Number(page.width) || 1000
  const height = Number(page.height) || 700
  const [x, y, w, h] = location.bbox.map((value, index) => value * (index % 2 ? height : width))
  const padding = Math.max(8, Math.min(w, h) * .65)
  const left = Math.max(0, x - padding)
  const top = Math.max(0, y - padding)
  const cropWidth = Math.min(width, x + w + padding) - left
  const cropHeight = Math.min(height, y + h + padding) - top
  return <button type="button" className="source-dimension-crop" onClick={onLocate} aria-label={`定位依据 ${number}：${location.text || '图纸尺寸标注'}`}>
    <span className="source-dimension-crop-heading"><b>{number}</b><strong title={location.text}>{location.text || '尺寸标注'}</strong><span>第 {page.page} 页</span></span>
    <svg className="source-dimension-crop-image" viewBox={`${left} ${top} ${cropWidth} ${cropHeight}`} role="img" aria-label={`原图尺寸局部放大：${location.text || '尺寸标注'}`}>
      <image href={page.url} width={width} height={height} />
      <rect x={x - 1} y={y - 1} width={w + 2} height={h + 2} rx="1" fill="none" stroke="#2275ed" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  </button>
}

function DrawingIcon({ type = 'drawing' }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {type === 'fit' ? <><path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5" /><rect x="7" y="7" width="10" height="10" rx="1" /></>
      : type === 'locate' ? <><circle cx="12" cy="12" r="6" /><path d="M12 2v5m0 10v5M2 12h5m10 0h5" /></>
        : <><path d="M14 3H5v18h14V8Z" /><path d="M14 3v5h5M8 12h8m-8 4h5" /></>}
  </svg>
}

/** Locations are explicit source evidence; this viewer never matches annotations by value. */
export default function SourceDrawingViewer({
  documents = [], bindings = [], activeKey, pinnedKey, onSelectParameter,
  onClearSelection, loading = false, error = '', locating = false,
  onRefreshFiles, onDownloadSource,
}) {
  const arrowId = useId().replaceAll(':', '')
  const viewportRef = useRef(null)
  const dragRef = useRef(null)
  const [selection, setSelection] = useState({ documentId: null, pageId: null })
  const [viewport, setViewport] = useState({ width: 0, height: 0 })
  const [loadedImage, setLoadedImage] = useState(null)
  const [failedImage, setFailedImage] = useState(null)
  const [retry, setRetry] = useState(0)
  const [fileAction, setFileAction] = useState(null)
  const fileActionRef = useRef(false)
  const automaticRefreshes = useRef(new Set())
  const activePageRef = useRef(null)
  const [{ zoom, pan }, setView] = useState({ zoom: 1, pan: { x: 0, y: 0 } })
  const [dragging, setDragging] = useState(false)
  const selectedKey = activeKey || pinnedKey || null
  const selectedBinding = bindings.find((binding) => binding.key === selectedKey)
  const pages = useMemo(() => documents.flatMap((document) => (document.pages || []).map((page) => ({
    ...page, documentId: document.id, filename: document.filename,
  }))), [documents])
  const currentDocument = documents.find((document) => document.id === selection.documentId) || documents[0]
  const currentPage = pages.find((page) => page.documentId === currentDocument?.id && page.id === selection.pageId)
    || pages.find((page) => page.documentId === currentDocument?.id)
  const currentPageKey = currentPage ? pageKey(currentPage.documentId, currentPage.id) : ''
  activePageRef.current = currentPageKey
  useEffect(() => { activePageRef.current = currentPageKey; return () => { activePageRef.current = null } }, [currentPageKey])
  const savedSource = /^source-[0-9]+$/.test(currentDocument?.id || '')
  const runFileAction = async (action) => {
    if (fileActionRef.current) return
    fileActionRef.current = true
    setFileAction({ page: currentPageKey, action, busy: true })
    try {
      if (action === 'download') await onDownloadSource(currentDocument.id)
      else {
        if (savedSource) await onRefreshFiles?.()
        if (activePageRef.current === currentPageKey) { setFailedImage(null); setRetry(value => value + 1) }
      }
      if (activePageRef.current === currentPageKey) setFileAction(null)
    } catch (failure) {
      if (activePageRef.current === currentPageKey && failure?.name !== 'AbortError') {
        setFileAction({ page: currentPageKey, error: failure.message || '文件暂时无法加载，请稍后重试。' })
      }
    } finally {
      fileActionRef.current = false
      if (activePageRef.current !== null) setFileAction(previous => previous?.page === currentPageKey ? { ...previous, busy: false } : previous)
    }
  }
  const fileError = fileAction?.page === currentPageKey ? fileAction.error : ''
  const fileBusy = Boolean(fileAction?.page === currentPageKey && fileAction.busy)
  const imageIdentity = `${currentPageKey}:${currentPage?.url || ''}`
  const availableLocations = useMemo(() => (selectedBinding?.locations || []).filter((location) => isExactLocation(location)
    && pages.some((page) => page.documentId === location.documentId && page.id === location.pageId)), [selectedBinding, pages])
  const locationIdentity = availableLocations.map((location) => `${pageKey(location.documentId, location.pageId)}:${location.bbox.join(',')}`).join('|')
  const selectedLocations = availableLocations.filter((location) => location.documentId === currentPage?.documentId && location.pageId === currentPage?.id)
  const pageAnnotations = useMemo(() => {
    const annotations = new Map()
    const priority = (binding) => binding.key === selectedKey ? 100 : (binding.locations?.length === 1 ? 2 : 0) + (binding.expression ? 0 : 1)
    for (const binding of bindings) {
      for (const location of binding.locations || []) {
        if (location.documentId !== currentPage?.documentId || location.pageId !== currentPage?.id || !isExactLocation(location)) continue
        const identity = location.annotationId || `${location.bbox.join(',')}:${location.text || ''}`
        if (!annotations.has(identity) || priority(binding) > priority(annotations.get(identity).binding)) annotations.set(identity, { binding, location, index: identity })
      }
    }
    return [...annotations.values()]
  }, [bindings, currentPage?.documentId, currentPage?.id, selectedKey])
  const pageWidth = Number(currentPage?.width) > 0 ? Number(currentPage.width) : loadedImage?.identity === imageIdentity ? loadedImage.width : 1000
  const pageHeight = Number(currentPage?.height) > 0 ? Number(currentPage.height) : loadedImage?.identity === imageIdentity ? loadedImage.height : 700
  const fitScale = viewport.width && viewport.height ? Math.min(Math.max(1, viewport.width - 32) / pageWidth, Math.max(1, viewport.height - 32) / pageHeight) : 1
  const scale = fitScale * zoom
  const imageFailed = failedImage === imageIdentity
  const imageLoaded = loadedImage?.identity === imageIdentity
  const hasImage = Boolean(currentPage?.url)
  const hasSourceValue = selectedBinding?.sourceValue !== undefined && selectedBinding?.sourceValue !== null && selectedBinding?.sourceValue !== ''
  const valueChanged = hasSourceValue && String(selectedBinding.sourceValue) !== String(selectedBinding.value)
  const sourceText = selectedBinding?.sourceText || [...new Set(availableLocations.map((location) => location.text).filter(Boolean))].join('；')
  const missingCount = selectedBinding?.missingAnnotationCount || 0
  const pointerBoxes = selectedLocations.map((location) => {
    const [x, y, width, height] = location.bbox
    const box = {
      left: viewport.width / 2 + pan.x + (x - .5) * pageWidth * scale,
      top: viewport.height / 2 + pan.y + (y - .5) * pageHeight * scale,
      width: width * pageWidth * scale, height: height * pageHeight * scale,
    }
    if (box.left + box.width < 0 || box.top + box.height < 0 || box.left > viewport.width || box.top > viewport.height) return null
    return { location, box }
  }).filter(Boolean)
  const dimensionPointers = pointerBoxes.reduce((pointers, { location, box }) => {
    const number = availableLocations.indexOf(location) + 1
    const text = location.text || '尺寸标注'
    const labelWidth = Math.min(Math.max(96, Array.from(text).length * 9 + 40), Math.min(230, viewport.width - 24))
    const toRight = box.left + box.width / 2 < viewport.width / 2
    const overlaps = (left, top, width, height, other) => left < other.left + other.width + 6 && left + width + 6 > other.left && top < other.top + other.height + 6 && top + height + 6 > other.top
    const candidates = [-52, box.height + 26, -92, box.height + 66, -132, box.height + 106].flatMap((offset) => [toRight, !toRight].map((right) => {
      const x = clamp(right ? box.left + box.width + 36 : box.left - labelWidth - 36, 12, Math.max(12, viewport.width - labelWidth - 12))
      const y = clamp(box.top + offset, 12, Math.max(12, viewport.height - 48))
      const collisions = pointers.filter((pointer) => overlaps(x, y, labelWidth, 30, { left: pointer.labelX, top: pointer.labelY, width: pointer.labelWidth, height: 30 })).length
      const obscured = pointerBoxes.filter((target) => overlaps(x, y, labelWidth, 30, target.box)).length
      return { x, y, score: collisions * 100000 + obscured * 10000 + Math.abs(offset) }
    }))
    candidates.sort((a, b) => a.score - b.score)
    const { x: labelX, y: labelY } = candidates[0]
    const below = labelY + 15 > box.top + box.height / 2
    pointers.push({ location, number, text, labelWidth, labelX, labelY,
      startX: labelX + labelWidth / 2, startY: below ? labelY : labelY + 30,
      endX: box.left + box.width / 2, endY: below ? box.top + box.height + 4 : box.top - 4,
    })
    return pointers
  }, [])

  const setPan = useCallback((nextPan) => setView((previous) => ({
    ...previous, pan: typeof nextPan === 'function' ? nextPan(previous.pan) : nextPan,
  })), [])
  const fitDrawing = useCallback(() => setView({ zoom: 1, pan: { x: 0, y: 0 } }), [])

  useEffect(() => {
    const node = viewportRef.current
    if (!node) return undefined
    const measure = () => setViewport({ width: node.clientWidth, height: node.clientHeight })
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const first = availableLocations[0]
    if (first) setSelection({ documentId: first.documentId, pageId: first.pageId })
  }, [selectedKey, locationIdentity])

  useEffect(() => { fitDrawing(); setDragging(false); dragRef.current = null }, [currentPageKey, fitDrawing])

  // Hover preserves the whole-sheet context; exact digits are enlarged separately.
  // Zooming into the sheet is an explicit action through the locate/zoom controls.
  useEffect(() => {
    fitDrawing()
  }, [selectedKey, currentPageKey, fitDrawing])

  const changeZoom = useCallback((nextZoom, point) => {
    setView((previous) => {
      const next = clamp(typeof nextZoom === 'function' ? nextZoom(previous.zoom) : nextZoom, MIN_ZOOM, MAX_ZOOM)
      if (next === previous.zoom) return previous
      const ratio = next / previous.zoom
      return {
        zoom: next,
        pan: next === MIN_ZOOM ? { x: 0, y: 0 } : {
          x: (point?.x || 0) - ((point?.x || 0) - previous.pan.x) * ratio,
          y: (point?.y || 0) - ((point?.y || 0) - previous.pan.y) * ratio,
        },
      }
    })
  }, [])

  useEffect(() => {
    const node = viewportRef.current
    if (!node || !hasImage) return undefined
    const wheel = (event) => {
      event.preventDefault()
      const rect = node.getBoundingClientRect()
      changeZoom((previous) => previous * Math.exp(-event.deltaY * 0.002), {
        x: event.clientX - rect.left - rect.width / 2,
        y: event.clientY - rect.top - rect.height / 2,
      })
    }
    node.addEventListener('wheel', wheel, { passive: false })
    return () => node.removeEventListener('wheel', wheel)
  }, [changeZoom, hasImage])

  const selectPage = (documentId, pageId) => setSelection({ documentId, pageId })
  const locateSelection = (specificLocation) => {
    const locations = specificLocation?.bbox ? [specificLocation] : selectedLocations.length ? selectedLocations : availableLocations.slice(0, 1)
    if (!locations.length) return
    const first = locations[0]
    if (first.documentId !== currentPage?.documentId || first.pageId !== currentPage?.id) {
      selectPage(first.documentId, first.pageId)
      return
    }
    const left = Math.min(...locations.map((location) => location.bbox[0]))
    const top = Math.min(...locations.map((location) => location.bbox[1]))
    const right = Math.max(...locations.map((location) => location.bbox[0] + location.bbox[2]))
    const bottom = Math.max(...locations.map((location) => location.bbox[1] + location.bbox[3]))
    const next = clamp(Math.min(viewport.width * 0.65 / ((right - left) * pageWidth * fitScale), viewport.height * 0.55 / ((bottom - top) * pageHeight * fitScale)), 1, 4)
    setView({ zoom: next, pan: { x: -(left + (right - left) / 2 - 0.5) * pageWidth * fitScale * next, y: -(top + (bottom - top) / 2 - 0.5) * pageHeight * fitScale * next } })
  }
  const endDrag = (event) => {
    if (dragRef.current?.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    dragRef.current = null
    setDragging(false)
  }
  const handleViewportKey = (event) => {
    if (event.target !== event.currentTarget) return
    if (event.key === 'Escape') { onClearSelection?.(); return }
    if (!hasImage) return
    if (event.key === '+' || event.key === '=') { event.preventDefault(); changeZoom((previous) => previous * 1.25) }
    else if (event.key === '-') { event.preventDefault(); changeZoom((previous) => previous / 1.25) }
    else if (event.key === '0' || event.key === 'Home') { event.preventDefault(); fitDrawing() }
    else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
      event.preventDefault()
      setPan((previous) => ({ x: previous.x + (event.key === 'ArrowLeft' ? 40 : event.key === 'ArrowRight' ? -40 : 0), y: previous.y + (event.key === 'ArrowUp' ? 40 : event.key === 'ArrowDown' ? -40 : 0) }))
    }
  }

  return <section className="source-drawing-viewer" aria-label="原图核对" aria-busy={loading}>
    <header className="source-drawing-header">
      <div className="source-drawing-title"><DrawingIcon /><strong>原图核对</strong><span>参数与图纸联动</span></div>
      {currentDocument?.downloadUrl && (savedSource && onDownloadSource
        ? <button type="button" className="source-drawing-download" disabled={fileBusy} onClick={() => runFileAction('download')} title={`下载原始文件：${currentDocument.filename}`}>{fileBusy && fileAction.action === 'download' ? '正在下载…' : '下载原文件 ↓'}</button>
        : <a className="source-drawing-download" href={currentDocument.downloadUrl} target="_blank" rel="noreferrer" title={`打开原始文件：${currentDocument.filename}`}>查看原文件 ↗</a>)}
    </header>
    {fileError && <p className="source-drawing-file-error" role="alert">{fileError}</p>}
    {documents.length > 0 && <div className="source-drawing-toolbar">
      <select aria-label="选择图纸文件" value={currentDocument?.id || ''} onChange={(event) => selectPage(event.target.value, documents.find((document) => document.id === event.target.value)?.pages?.[0]?.id)} title={currentDocument?.filename}>
        {documents.map((document) => <option key={document.id} value={document.id}>{document.filename || '未命名图纸'}</option>)}
      </select>
      <select className="source-drawing-page-select" aria-label="选择图纸页码" value={currentPage?.id || ''} disabled={!currentDocument?.pages?.length} onChange={(event) => selectPage(currentDocument.id, event.target.value)}>
        {(currentDocument?.pages || []).map((page) => <option key={page.id} value={page.id}>第 {page.page} 页</option>)}
        {!currentDocument?.pages?.length && <option value="">暂无预览</option>}
      </select>
      <div className="source-drawing-zoom" role="group" aria-label="图纸缩放">
        <button type="button" aria-label="缩小图纸" title="缩小（−）" onClick={() => changeZoom((previous) => previous / 1.25)} disabled={!hasImage || zoom <= MIN_ZOOM}>−</button>
        <span aria-label={`相对适应尺寸缩放 ${Math.round(zoom * 100)}%`}>{Math.round(zoom * 100)}%</span>
        <button type="button" aria-label="放大图纸" title="放大（+）" onClick={() => changeZoom((previous) => previous * 1.25)} disabled={!hasImage || zoom >= MAX_ZOOM}>+</button>
      </div>
      <button className="source-drawing-tool-button" type="button" aria-label="适应整张图纸" title="适应图纸（0）" disabled={!hasImage} onClick={fitDrawing}><DrawingIcon type="fit" /></button>
      <button className="source-drawing-tool-button" type="button" aria-label="放大定位当前参数标注" title="定位当前参数" disabled={!availableLocations.length || !hasImage} onClick={locateSelection}><DrawingIcon type="locate" /></button>
    </div>}
    <div ref={viewportRef} className={`source-drawing-viewport${dragging ? ' is-dragging' : ''}${hasImage ? ' has-image' : ''}`} tabIndex={0}
      role="group" aria-label="图纸预览。滚轮缩放，拖动平移；键盘加减号缩放，方向键平移，0 适应图纸。" onKeyDown={handleViewportKey}
      onPointerDown={(event) => {
        if (!hasImage || event.button !== 0) return
        event.currentTarget.focus({ preventScroll: true })
        dragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, pan }
        event.currentTarget.setPointerCapture(event.pointerId)
        setDragging(true)
      }} onPointerMove={(event) => {
        const drag = dragRef.current
        if (drag?.pointerId === event.pointerId) setPan({ x: drag.pan.x + event.clientX - drag.x, y: drag.pan.y + event.clientY - drag.y })
      }} onPointerUp={endDrag} onPointerCancel={endDrag} onLostPointerCapture={() => { dragRef.current = null; setDragging(false) }}>
      {hasImage && !imageFailed && <div className="source-drawing-plane" style={{ width: pageWidth, height: pageHeight, '--source-scale': scale, transform: `translate(-50%, -50%) translate(${pan.x}px, ${pan.y}px) scale(${scale})`, visibility: imageLoaded ? 'visible' : 'hidden' }}>
        <img key={`${imageIdentity}:${retry}`} src={currentPage.url} alt={`${currentDocument?.filename || '上传图纸'}，第 ${currentPage.page} 页`} draggable={false}
          onLoad={(event) => setLoadedImage({ identity: imageIdentity, width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
          onError={() => {
            setFailedImage(imageIdentity)
            if (savedSource && onRefreshFiles && !fileActionRef.current && !automaticRefreshes.current.has(currentPageKey)) {
              automaticRefreshes.current.add(currentPageKey)
              runFileAction('refresh')
            }
          }} />
        <div className="source-drawing-annotations">
          {pageAnnotations.map(({ binding, location, index }) => {
            const selected = binding.key === selectedKey
            return <button key={`${binding.key}:${index}`} className={`source-drawing-annotation${selected ? ' is-active' : ''}`}
              style={{ left: `${location.bbox[0] * 100}%`, top: `${location.bbox[1] * 100}%`, width: `${location.bbox[2] * 100}%`, height: `${location.bbox[3] * 100}%`, zIndex: selected ? 3 : 1 }}
              {...{
                type: 'button',
                'aria-label': `${binding.label || binding.key}：${location.text || binding.sourceText || formatValue(binding.value, binding.unit)}，点击固定核对`,
                'aria-pressed': pinnedKey === binding.key,
                title: `${binding.label || binding.key} · ${location.text || binding.sourceText || '点击固定核对'}`,
                onPointerDown: (event) => { event.stopPropagation(); event.preventDefault() },
                onClick: (event) => { onSelectParameter?.(binding.key); event.currentTarget.focus({ preventScroll: true }) },
              }} />
          })}
        </div>
      </div>}
      {hasImage && imageLoaded && !imageFailed && dimensionPointers.length > 0 && <div className="source-dimension-pointers" aria-hidden="true">
        <svg width={viewport.width} height={viewport.height}>
          <defs><marker id={arrowId} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#2266d4" /></marker></defs>
          {dimensionPointers.map((pointer) => <g key={pointer.number}>
            <path d={`M${pointer.startX} ${pointer.startY} L${pointer.endX} ${pointer.endY}`} stroke="white" strokeWidth="5" fill="none" />
            <path d={`M${pointer.startX} ${pointer.startY} L${pointer.endX} ${pointer.endY}`} stroke="#2266d4" strokeWidth="2" fill="none" markerEnd={`url(#${arrowId})`} />
          </g>)}
        </svg>
        {dimensionPointers.map((pointer) => <span key={pointer.number} className="source-dimension-pointer-label" style={{ left: pointer.labelX, top: pointer.labelY, width: pointer.labelWidth }}>
          <b>{pointer.number}</b><strong>{pointer.text}</strong>
        </span>)}
      </div>}
      {loading && <div className="source-drawing-state" role="status"><DrawingIcon /><strong>正在准备图纸预览…</strong><span>准备完成后即可与参数对照。</span></div>}
      {!loading && (!hasImage || imageFailed) && <div className="source-drawing-state" role={error || imageFailed ? 'status' : undefined}>
        <DrawingIcon /><strong>{imageFailed ? '图纸预览暂时无法加载' : documents.length ? '此文件暂无图纸预览' : '上传图纸后，在这里核对尺寸'}</strong>
        <span>{error || currentDocument?.error && (currentDocument.error === 'source_preview_unavailable' ? '图纸预览暂时不可用，请查看原文件或重新加载。' : currentDocument.error) || (imageFailed ? '可重新加载预览，或打开原文件查看。' : documents.length ? '可打开原文件核对；图纸预览准备完成后将在此显示。' : '悬停右侧参数，查看它在原图中的标注位置。')}</span>
        {imageFailed && <button type="button" disabled={fileBusy} onClick={() => runFileAction('refresh')}>{fileBusy ? '正在加载…' : '重新加载'}</button>}
      </div>}
      {!loading && hasImage && !imageFailed && !imageLoaded && <div className="source-drawing-state" role="status"><span>正在加载第 {currentPage.page} 页…</span></div>}
      {hasImage && imageLoaded && !imageFailed && <span className="source-drawing-gesture-hint" aria-hidden="true">滚轮缩放 · 拖动平移</span>}
    </div>
    <div className={`source-drawing-details${availableLocations.length > 0 ? ' has-evidence' : ''}`}>
    {availableLocations.length > 0 && <section className="source-dimension-evidence" aria-label="对应尺寸的原图局部放大">
      <div className="source-dimension-evidence-title"><strong>对应尺寸 · 原图放大</strong><span>{selectedBinding?.expression ? '编号对应推导依据' : '编号对应图中箭头'}</span></div>
      <div className="source-dimension-evidence-list">{availableLocations.map((location, index) => {
        const page = pages.find((item) => item.documentId === location.documentId && item.id === location.pageId)
        return <SourceDimensionCrop key={`${location.annotationId || index}:${pageKey(location.documentId, location.pageId)}`} page={page} location={location} number={index + 1} onLocate={() => locateSelection(location)} />
      })}</div>
    </section>}
    <footer className="source-drawing-footer" aria-live="polite" aria-atomic="true">
      {selectedBinding ? <>
        <div className="source-drawing-selection-title">
          <strong>{selectedBinding.label || selectedBinding.key}</strong>
          <span className={`source-drawing-status ${!availableLocations.length ? 'is-unlocated' : 'is-located'}`}>{!availableLocations.length ? locating ? '正在定位尺寸…' : '尺寸未定位' : missingCount ? '部分尺寸已定位' : '已指向具体尺寸'}</span>
          {pinnedKey === selectedKey && <span className="source-drawing-pinned">已固定</span>}
          <button type="button" className="source-drawing-clear" onClick={() => onClearSelection?.()} aria-label="取消参数核对">取消</button>
        </div>
        <dl className="source-drawing-comparison">
          <div><dt>图纸原文</dt><dd title={sourceText}>{sourceText || '未记录原始标注文字'}</dd></div>
          <div><dt>当前参数值{valueChanged && <span className="source-drawing-changed">已修改</span>}</dt><dd>{valueChanged && <del>{formatValue(selectedBinding.sourceValue, selectedBinding.unit)}</del>}{formatValue(selectedBinding.value, selectedBinding.unit)}</dd></div>
        </dl>
        {selectedBinding.expression && <p className="source-drawing-expression">推导关系：{selectedBinding.expressionLabel || selectedBinding.expression}</p>}
        {!availableLocations.length ? <p className="source-drawing-notice">{locating ? '正在识别原图中对应的尺寸文字，完成后显示箭头和局部放大。' : '暂未找到可确认的尺寸标注，当前不显示定位箭头。'}</p>
          : missingCount ? <p className="source-drawing-notice">已指出 {availableLocations.length} 处尺寸，另有 {missingCount} 处依据尚未定位。</p>
            : selectedBinding.expression ? <p className="source-drawing-notice">此参数由图中尺寸推导，请按编号核对原始尺寸与推导关系。</p>
              : null}
      </> : <div className="source-drawing-idle"><span className="source-drawing-indicator" /><p>悬停右侧参数，查看对应图纸标注。点击参数或标注可固定核对。</p></div>}
    </footer>
    </div>
  </section>
}
