import { useEffect, useMemo, useState } from 'react'
import { buildDrawingScene, drawingScaleFactor, formatDimension, dxfForModel, isDrawingKernelReady } from './drawingGeometry.js'
import { validateModelParameters, validationParameterKeys } from './modelValidation.js'
import './drawing-workspace.css'

const sourceNames = { drawing: '原图读值', ai: 'AI 候选', manual: '人工修改', derived: '推导值', direct_dimension: '原生尺寸', vector_derived: '矢量量测', ai_interpreted: 'AI 解释', template_default: '模板默认', current_model: '当前模型' }
const labels = {
  outerDiameter: '外径', length: '总长度', holeDiameter: '通孔直径', keywayWidth: '键槽宽度', keywayDepth: '键槽深度', keywayLength: '键槽长度',
  baseLength: '底板长度', baseWidth: '底板宽度', baseThickness: '底板厚度', upperLength: '上部长度', upperWidth: '上部宽度', upperHeight: '上部高度', totalHeight: '总高',
  notchOpening: '鞍槽开口', notchRadius: '鞍槽半径', slotLength: '浅槽长度', slotWidth: '浅槽宽度', pocketDepth: '浅槽深度', bossDiameter: '侧孔直径', bossCenterDistance: '侧孔中心距',
  baseMainDepth: '主段深度', frontTongueWidth: '前舌宽度', rearBridgeWidth: '后桥宽度', pedestalOuterRadius: '夹座外半径', pedestalCenterFromRear: '夹座距后缘', pedestalHeight: '低夹座高度', rearClampRise: '后夹耳加高',
  boreDiameter: '盲孔直径', boreFloorZ: '盲孔底面 Z', splitWidth: '开缝宽度', mountHoleCount: '安装孔数量', mountHoleDiameter: '安装孔直径', mountHoleCenterDistance: '安装孔中心距', mountHoleCenterFromRear: '安装孔距后缘',
  crossHoleDiameter: '横孔直径', crossHoleCenterZ: '横孔中心 Z', ribHeight: '加强筋高度', ribThickness: '加强筋厚度', outerCornerRadius: '底板外圆角', neckConcaveRadius: '肩部内凹圆角', neckConvexRadius: '前舌外圆角',
  mainLength: '主件总长', headLength: '前段长度', neckLength: '颈段长度', headLeftDiameter: '前段左端直径', headRightDiameter: '前段右端直径', neckDiameter: '颈段直径', tipDiameter: '末段直径',
  counterboreDiameter: '沉孔直径', counterboreDepth: '沉孔深度', axialBoreDiameter: '轴向孔径', outletDiameter: '出口直径', outletTaperHalfAngle: '出口锥半角', insertOuterDiameter: '镶件外径', insertLength: '镶件长度', insertThreadDesignation: '螺纹标注', insertAxialOffset: '镶件轴向偏置',
}

function Entity({ entity: e }) {
  const shared = { 'data-feature': e.feature || undefined, 'data-entity-id': e.id, vectorEffect: 'non-scaling-stroke' }
  if (e.type === 'line') return <line {...shared} x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2} />
  if (e.type === 'circle') return <circle {...shared} cx={e.cx} cy={e.cy} r={e.r} />
  if (e.type === 'polyline') return e.closed
    ? <polygon {...shared} points={e.points.map((point) => point.join(',')).join(' ')} />
    : <polyline {...shared} points={e.points.map((point) => point.join(',')).join(' ')} />
  if (e.type === 'arc') {
    const start = e.start * Math.PI / 180, end = e.end * Math.PI / 180
    return <path {...shared} d={`M ${e.cx + e.r * Math.cos(start)} ${e.cy + e.r * Math.sin(start)} A ${e.r} ${e.r} 0 ${e.end - e.start > 180 ? 1 : 0} 1 ${e.cx + e.r * Math.cos(end)} ${e.cy + e.r * Math.sin(end)}`} />
  }
  return <text transform={`translate(${e.x} ${e.y}) scale(1 -1)`} textAnchor="middle" fontSize={e.height} fill="currentColor" stroke="none">{e.text}</text>
}

export default function DrawingWorkspace({ model, generation, drawingJob, drawingScale, setDrawingScale, drawingPreferences, setDrawingPreferences, onSaveVersion, onExport, onEditParameters, showToast = () => {} }) {
  const [localScale, setLocalScale] = useState('1:1')
  const scale = drawingScale || localScale
  const changeScale = setDrawingScale || setLocalScale
  const scene = useMemo(() => buildDrawingScene(model, { scale }), [model, scale])
  const [localPreferences, setLocalPreferences] = useState({ layers: {}, selectedView: 'all' })
  const preferences = drawingPreferences || localPreferences
  const changePreferences = setDrawingPreferences || setLocalPreferences
  const layers = preferences.layers || {}
  const selectedView = scene.views.some((view) => view.id === preferences.selectedView) ? preferences.selectedView : 'all'
  const setLayers = (value) => changePreferences((current) => ({ ...current, layers: typeof value === 'function' ? value(current?.layers || {}) : value }))
  const setSelectedView = (value) => changePreferences((current) => ({ ...current, selectedView: value }))
  const [dimensionCheck, setDimensionCheck] = useState(null)
  const modelSignature = JSON.stringify(model)
  const currentCheck = dimensionCheck?.signature === modelSignature ? dimensionCheck : null
  useEffect(() => { setDimensionCheck(null) }, [modelSignature])
  const [exportError, setExportError] = useState('')
  const evidence = drawingJob?.evidence
  const pending = Boolean(evidence && evidence.status !== 'confirmed') || ['queued', 'analyzing', 'generating'].includes(drawingJob?.status)
  const kernelReady = isDrawingKernelReady(generation, pending)
  const visibleEntities = scene.entities.filter((e) => layers[e.layer] !== false && (selectedView === 'all' || e.view === selectedView || e.view === 'sheet'))
  const selectedBounds = scene.views.find((view) => view.id === selectedView)?.bounds
  const viewport = selectedBounds
    ? { x: selectedBounds.x - 10, y: scene.height - selectedBounds.y - selectedBounds.height - 12, width: selectedBounds.width + 20, height: selectedBounds.height + 28 }
    : { x: 0, y: 0, width: scene.width, height: scene.height }
  const zoom = drawingScaleFactor(scale)
  const sources = drawingJob?.analysis?.candidateSources || {}
  const recheckDimensions = () => {
    const validation = validateModelParameters(model)
    setDimensionCheck({ signature: modelSignature, validation })
    showToast(validation.valid ? `已重新计算尺寸约束：${scene.dimensions.length} 处标注使用当前参数` : `尺寸检查发现 ${validation.errors.length} 个问题，请先修正参数`)
  }
  const exportDrawing = async () => {
    setExportError('')
    if (!scene.valid || pending) return
    try {
      if (onExport) return await onExport('dxf', { scene, scale, layers })
      const blob = new Blob([dxfForModel(model, { scene, layers })], { type: 'application/dxf;charset=utf-8' })
      const url = URL.createObjectURL(blob), anchor = document.createElement('a')
      anchor.href = url; anchor.download = `${model.name || 'drawing'}.dxf`; anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      showToast('DXF 已导出：三视图、中心剖面和当前可见图层，模型空间 1:1')
    } catch (error) { setExportError(error.message || '导出失败，请重试。') }
  }
  return <div className="technical-drawing-workspace">
    <header className="technical-drawing-heading"><div><span className="eyebrow">PARAMETRIC DRAWING</span><h1>{model?.name || '当前零件'} · 工程图</h1><p>三视图与中心剖面随参数更新，图形与 DXF 使用同一份几何数据。</p></div><div className="technical-drawing-actions">
      {onSaveVersion && <button type="button" className="secondary-button" onClick={onSaveVersion}>保存图纸版本</button>}
      <button type="button" className="primary-button" onClick={exportDrawing} disabled={!scene.valid || pending}>导出 DXF ↓</button>
    </div></header>
    {exportError && <p className="drawing-validation-error" role="alert">{exportError}</p>}
    <div className={`drawing-validation-banner ${scene.valid ? 'valid' : 'invalid'}`} role="status"><b>{!scene.valid ? '尺寸存在问题，暂不生成工程图' : pending ? '候选参数图 · 数据尚未确认' : '参数尺寸检查通过'}</b><span>{scene.valid ? `${scene.views.length} 个视图 · ${scene.dimensions.length} 处尺寸/引线 · ${kernelReady ? '当前生产实体已通过内核校验' : '生产实体内核校验另行进行'}` : `${scene.errors.length} 个问题需要修正`}</span></div>
    {!scene.valid ? <section className="drawing-errors-card"><h2>{scene.unsupported ? '此零件尚无工程图配方' : '请先修正参数'}</h2><ul>{scene.errors.map((error, index) => <li key={`${error.field}-${index}`}><b>{labels[error.field] || error.field}</b>：{error.message}</li>)}</ul>{onEditParameters && <button type="button" className="primary-button" onClick={onEditParameters}>返回参数编辑</button>}</section> : <div className="technical-drawing-layout">
      <section className="technical-drawing-canvas"><div className="technical-drawing-toolbar"><label>显示比例 <select aria-label="工程图显示比例" value={scale} onChange={(event) => changeScale(event.target.value)}><option>1:2</option><option>1:1</option><option>2:1</option></select></label><label>视图 <select aria-label="工程图视图" value={selectedView} onChange={(event) => setSelectedView(event.target.value)}><option value="all">全部视图</option>{scene.views.map((view) => <option key={view.id} value={view.id}>{view.title}</option>)}</select></label><button type="button" onClick={() => { changeScale('1:1'); setSelectedView('all'); setLayers({}) }}>重置图纸视图</button></div>
        <div className="technical-drawing-scroll" tabIndex={0} aria-label="工程图画布，可滚动查看"><svg className="technical-drawing-svg" role="img" aria-label={`${model.name}参数工程图，包含三视图和中心剖面`} viewBox={`${viewport.x} ${viewport.y} ${viewport.width} ${viewport.height}`} width={viewport.width * 2.4 * zoom} height={viewport.height * 2.4 * zoom} data-drawing-scale={scale} data-entity-count={visibleEntities.length}>
          <rect x={viewport.x} y={viewport.y} width={viewport.width} height={viewport.height} fill="#101925" />
          <g transform={`translate(0 ${scene.height}) scale(1 -1)`}>{scene.layers.filter((layer) => layers[layer.id] !== false).map((layer) => <g key={layer.id} data-layer={layer.id} fill="none" stroke={layer.color} color={layer.color} strokeWidth={layer.id === 'SECTION' ? .65 : 1.05} strokeDasharray={layer.dash || undefined}>{visibleEntities.filter((entity) => entity.layer === layer.id).map((entity) => <Entity key={entity.id} entity={entity} />)}</g>)}</g>
        </svg></div><div className="technical-drawing-footnote">显示比例会缩放画布；可滚动查看。DXF 始终保存实际毫米尺寸（1:1），包含全部视图和当前可见图层。</div>
        <div className="drawing-scope-notes">{scene.notes.map((note) => <p key={note}>{note}</p>)}</div>
      </section>
      <aside className="technical-drawing-inspector"><section><h2>图层</h2>{scene.layers.map((layer) => <label className="drawing-layer-toggle" key={layer.id}><input type="checkbox" checked={layers[layer.id] !== false} onChange={(event) => setLayers((current) => ({ ...current, [layer.id]: event.target.checked }))} /><i style={{ background: layer.color }} /><span>{layer.label}</span><small>{scene.entities.filter((entity) => entity.layer === layer.id).length}</small></label>)}</section>
        <section><h2>图纸来源</h2><dl><div><dt>参数版本</dt><dd>{model.updatedAt || '当前编辑值'}</dd></div><div><dt>原始图纸</dt><dd>{evidence?.sourceFilename || drawingJob?.fileMeta?.name || drawingJob?.file?.name || '文字或参数设计'}</dd></div><div><dt>确认状态</dt><dd>{pending ? '候选待确认' : evidence ? '已确认' : '无图纸识别候选'}</dd></div><div><dt>单位</dt><dd>mm</dd></div><div><dt>几何来源</dt><dd>当前参数化配方</dd></div></dl><button type="button" className="secondary-button" onClick={recheckDimensions}>重新检查尺寸</button>{currentCheck && <p className="drawing-check-result" role="status">{currentCheck.validation.valid ? '当前参数尺寸约束已重新计算并通过；未运行制造工艺或实体拓扑检查。' : `尺寸约束检查未通过：${currentCheck.validation.errors.map((error) => error.message).join(' ')}`}</p>}</section>
        <details className="drawing-parameter-details"><summary>全部参数与来源（{validationParameterKeys(model).length}）</summary><div className="drawing-parameter-table">{validationParameterKeys(model).map((field) => <div key={field}><b>{labels[field] || field}</b><span>{typeof model[field] === 'number' ? formatDimension(model[field]) : model[field]}{field === 'outletTaperHalfAngle' ? '°' : ['mountHoleCount', 'insertThreadDesignation'].includes(field) ? '' : ' mm'}</span><small>{sourceNames[sources[field]] || (evidence ? '来源未标注' : '当前模型参数')}</small></div>)}</div></details>
      </aside>
    </div>}
  </div>
}
