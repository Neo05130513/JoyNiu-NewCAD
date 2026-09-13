import { useEffect, useMemo, useRef, useState } from 'react'
import { isFeatureModel } from './cadAgentState.js'
import { drawingParameterBindings } from './sourceDrawingState.js'
import SourceDrawingViewer from './SourceDrawingViewer.jsx'
import useSourceDrawing from './useSourceDrawing.js'
import './drawing-review.css'

const sourceStatus = {
  located: '已指向具体尺寸',
  partial: '部分尺寸已定位',
  region: '尺寸未定位',
  unlocated: '尺寸未定位',
}
const displayValue = (value) => value === null || value === undefined || value === '' ? '—' : String(value)

export default function DrawingReviewWorkspace({ model, drawingJob, token, onBack, legacyLabels = {}, legacyKeys = [] }) {
  const titleRef = useRef(null)
  const parameterRefs = useRef(new Map())
  const sourceDrawing = useSourceDrawing(model, drawingJob, token)
  const [search, setSearch] = useState('')
  const [hoveredKey, setHoveredKey] = useState(null)
  const [focusedKey, setFocusedKey] = useState(null)
  const [pinnedKey, setPinnedKey] = useState(null)
  const bindings = useMemo(() => {
    if (isFeatureModel(model)) return drawingParameterBindings(model, sourceDrawing.documents, sourceDrawing.sourceRun)
    return legacyKeys.filter((key) => model?.[key] !== undefined).map((key) => {
      const evidence = drawingJob?.analysis?.parameterEvidence?.[key]
      return {
        key, label: legacyLabels[key] || key, value: model[key],
        unit: key === 'outletTaperHalfAngle' ? '°' : ['mountHoleCount', 'insertThreadDesignation'].includes(key) ? '' : 'mm',
        sourceText: typeof evidence === 'string' ? evidence : evidence?.sourceText || evidence?.text || '',
        status: 'unlocated', locations: [],
      }
    })
  }, [model, legacyKeys, legacyLabels, drawingJob?.analysis?.parameterEvidence, sourceDrawing.documents, sourceDrawing.sourceRun])
  const query = search.trim().toLocaleLowerCase()
  const visibleBindings = bindings.filter((binding) => [binding.label, binding.key, binding.sourceText, binding.expressionLabel, displayValue(binding.value)]
    .some((value) => String(value || '').toLocaleLowerCase().includes(query)))
  const activeKey = hoveredKey || focusedKey || pinnedKey
  const pinnedBinding = bindings.find((binding) => binding.key === pinnedKey)
  const locatedCount = bindings.filter((binding) => binding.status === 'located').length
  const missingDimensionCount = bindings.reduce((sum, binding) => sum + (binding.missingAnnotationCount || 0), 0)
  const locationProgress = sourceDrawing.locationProgress
  const totalAnnotations = locationProgress?.totalAnnotationCount || sourceDrawing.sourceRun?.sourceTranscription?.annotations?.length || 0
  const checkedAnnotations = Math.min(totalAnnotations, locationProgress?.completedAnnotationCount || 0)
  const elapsedSeconds = Math.max(0, Math.floor(locationProgress?.elapsedSeconds || 0))
  const elapsedLabel = elapsedSeconds >= 60 ? `${Math.floor(elapsedSeconds / 60)} 分 ${elapsedSeconds % 60} 秒` : `${elapsedSeconds} 秒`
  const progressLabel = locationProgress?.stage === 'recovering' ? '连接中断，正在恢复已完成的定位结果。'
    : locationProgress?.stage === 'queued' ? '等待尺寸识别，已有位置仍可核对。'
      : `已检查 ${checkedAnnotations}/${totalAnnotations} 处标注，已定位 ${locationProgress?.locatedAnnotationCount || 0} 处。完成的尺寸会立即显示。`

  useEffect(() => { titleRef.current?.focus({ preventScroll: true }) }, [])
  useEffect(() => {
    setHoveredKey(null)
    setFocusedKey(null)
    setPinnedKey(null)
    setSearch('')
  }, [model?.agentRun?.runId, drawingJob?.file, model?.kind])
  useEffect(() => {
    if (pinnedKey) parameterRefs.current.get(pinnedKey)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [pinnedKey])

  const clearSelection = () => { setPinnedKey(null); setHoveredKey(null); setFocusedKey(null) }
  const selectParameter = (key) => setPinnedKey((previous) => previous === key ? null : key)
  const selectAnnotation = (key) => {
    if (!visibleBindings.some((binding) => binding.key === key)) setSearch('')
    setHoveredKey(null)
    setFocusedKey(null)
    selectParameter(key)
  }

  return <section className="drawing-review-workspace" aria-labelledby="drawing-review-title" onKeyDown={(event) => {
    if (event.key === 'Escape') clearSelection()
  }}>
    <header className="drawing-review-header">
      <div>
        <h1 id="drawing-review-title" ref={titleRef} tabIndex={-1}>图纸参数核对</h1>
        <p>悬停右侧参数，箭头会指出具体尺寸，并放大对应的原图文字。点击可固定核对。</p>
      </div>
      <button type="button" className="drawing-review-back" aria-label="返回 3D 建模" onClick={onBack}>
        <span aria-hidden="true">←</span> 返回 3D 建模
      </button>
    </header>
    <div className="drawing-review-content">
      <div className="drawing-review-document">
        {sourceDrawing.error && <div className="drawing-review-error" role="status">
          <span>{sourceDrawing.error}</span>
          <button type="button" disabled={sourceDrawing.loading} onClick={sourceDrawing.retry}>{sourceDrawing.loading ? '正在加载…' : '重新加载原图'}</button>
        </div>}
        {(sourceDrawing.locating || sourceDrawing.locationError || sourceDrawing.canLocate && missingDimensionCount > 0) && <div className={`drawing-review-location-progress${sourceDrawing.locationError ? ' has-error' : ''}`} role="status">
          <div className="drawing-review-progress-content">
            <span>{sourceDrawing.locating ? progressLabel : sourceDrawing.locationError || '部分尺寸尚未确认位置，可重新识别。'}</span>
            {sourceDrawing.locating && <div className="drawing-review-progress-meter">
              <progress aria-label="尺寸标注识别进度" value={checkedAnnotations} max={Math.max(1, totalAnnotations)} />
              <span>已用时 {elapsedLabel}</span>
            </div>}
          </div>
          {!sourceDrawing.locating && <button type="button" onClick={sourceDrawing.locate}>重新定位尺寸</button>}
        </div>}
        <SourceDrawingViewer documents={sourceDrawing.documents} bindings={bindings} activeKey={activeKey} pinnedKey={pinnedKey}
          onRefreshFiles={sourceDrawing.refreshFiles} onDownloadSource={sourceDrawing.downloadSource}
          onSelectParameter={selectAnnotation} onClearSelection={clearSelection} loading={sourceDrawing.loading} error={sourceDrawing.error} locating={sourceDrawing.locating} />
      </div>
      <aside className="drawing-review-parameters" aria-label="待核对参数">
        <div className="drawing-review-parameters-header">
          <div className="drawing-review-parameters-title"><h2>模型参数</h2><span>{bindings.length} 项</span></div>
          <p>{bindings.length ? `${locatedCount} 项已对应具体尺寸${sourceDrawing.locating ? '，正在补充其余尺寸的位置。' : '，悬停即可查看箭头与原图放大。'}` : '生成模型后，在这里查看对应参数。'}</p>
          <label className="drawing-review-search">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg>
            <input type="search" aria-label="搜索核对参数" placeholder="搜索参数名称、值或图纸原文" value={search} onChange={(event) => {
              setSearch(event.target.value); setHoveredKey(null); setFocusedKey(null)
            }} />
          </label>
        </div>
        {pinnedBinding && <div className="drawing-review-pinned" role="status">
          <span title={pinnedBinding.label}>已固定：<strong>{pinnedBinding.label || pinnedBinding.key}</strong></span>
          <button type="button" onClick={clearSelection}>取消固定</button>
        </div>}
        <div className="drawing-review-parameter-list">
          {visibleBindings.length ? <ul aria-label="参数与图纸来源">{visibleBindings.map((binding) => {
            const status = sourceStatus[binding.status] ? binding.status : 'unlocated'
            const statusLabel = sourceDrawing.locating && (status === 'unlocated' || status === 'region') ? '正在定位尺寸…' : sourceStatus[status]
            const active = activeKey === binding.key
            const pinned = pinnedKey === binding.key
            return <li key={binding.key}>
              <button type="button" data-review-parameter={binding.key} className={`drawing-review-parameter${active ? ' is-active' : ''}${pinned ? ' is-pinned' : ''}`}
                ref={(node) => { if (node) parameterRefs.current.set(binding.key, node); else parameterRefs.current.delete(binding.key) }}
                aria-pressed={pinned} aria-label={`${binding.label || binding.key}，当前值 ${displayValue(binding.value)} ${binding.unit || ''}，${statusLabel}，点击${pinned ? '取消固定' : '固定核对'}`}
                onMouseEnter={() => setHoveredKey(binding.key)} onMouseLeave={() => setHoveredKey(null)}
                onFocus={() => setFocusedKey(binding.key)} onBlur={() => setFocusedKey(null)} onClick={() => selectParameter(binding.key)}>
                <span className="drawing-review-parameter-heading"><strong>{binding.label || binding.key}</strong>{pinned && <span className="drawing-review-pin-label">已固定</span>}</span>
                <span className="drawing-review-parameter-value"><span>当前值</span><strong>{displayValue(binding.value)}</strong>{binding.unit && <span className="drawing-review-unit">{binding.unit}</span>}</span>
                <span className={`drawing-review-source-status is-${status}`}>{statusLabel}</span>
                <span className="drawing-review-parameter-source" title={binding.sourceText || undefined}>{binding.sourceText ? `图纸原文：${binding.sourceText}` : binding.expression ? `推导关系：${binding.expressionLabel || binding.expression}` : '未记录原始标注文字，可结合原图核对。'}</span>
              </button>
            </li>
          })}</ul> : <div className="drawing-review-empty" role="status">
            <strong>{query ? '没有匹配的参数' : '暂无可核对参数'}</strong>
            <p>{query ? '尝试其他名称、数值或图纸原文。' : '返回 3D 建模并生成模型后，即可在这里核对尺寸。'}</p>
            {query && <button type="button" onClick={() => setSearch('')}>清空搜索</button>}
          </div>}
        </div>
        <div className="drawing-review-parameters-footer">尺寸需要调整时，返回 3D 建模修改参数。</div>
      </aside>
    </div>
  </section>
}
