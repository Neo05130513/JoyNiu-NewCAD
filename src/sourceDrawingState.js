import { cadParameterRows } from './cadAgentState.js'

export function normalizedBox(value) {
  if (!Array.isArray(value) || value.length !== 4 || !value.every((n) => typeof n === 'number' && Number.isFinite(n))) return null
  const [x, y, w, h] = value
  return x >= 0 && y >= 0 && w > 0 && h > 0 && x + w <= 1.000001 && y + h <= 1.000001 ? value : null
}

export function normalizedDimensionBox(value) {
  const box = normalizedBox(value)
  return box && box[2] * box[3] <= .018 && Math.max(box[2], box[3]) <= .3 ? box : null
}

export function sourceAnnotationIds(source) {
  const explicit = typeof source === 'object' && source ? source.annotationIds : []
  const text = typeof source === 'string' ? source : [source?.text, source?.description, source?.evidence].filter(Boolean).join(' ')
  // Older saved plans cite the stable IDs in prose. Never match numeric values.
  const original = source?.drawingSource ? sourceAnnotationIds(source.drawingSource) : []
  return [...new Set([...original, ...(Array.isArray(explicit) ? explicit.filter((id) => typeof id === 'string') : []),
    ...[...text.matchAll(/(?<![A-Za-z0-9_-])annotation-\d+(?![A-Za-z0-9_-])/g)].map((match) => match[0])])]
}

function boxOnPage(box, region, precision) {
  if (!normalizedBox(region)) return null
  const left = Math.max(box[0], region[0]), top = Math.max(box[1], region[1])
  const right = Math.min(box[0] + box[2], region[0] + region[2]), bottom = Math.min(box[1] + box[3], region[1] + region[3])
  if (right <= left || bottom <= top) return null
  // A text box crossing pages is not a reliable callout on either page.
  if (precision === 'exact' && ((right - left) * (bottom - top)) / (box[2] * box[3]) < .98) return null
  return [(left - region[0]) / region[2], (top - region[1]) / region[3], (right - left) / region[2], (bottom - top) / region[3]]
}

function annotationLocations(annotation, documents, transcription) {
  if (annotation.bboxFrame && annotation.bboxFrame !== 'prepared_source_image') return []
  const exact = normalizedDimensionBox(annotation.bbox), region = normalizedBox(annotation.sourceRegion)
  const box = exact || region
  if (!box) return []
  const precision = exact ? 'exact' : 'region'
  const locations = []
  for (const document of documents) for (const page of document.pages || []) {
    if (!Number.isInteger(page.preparedFileIndex) || page.preparedFileIndex !== annotation.fileIndex) continue
    const preparedMatch = page.preparedSha256 && annotation.preparedSha256 && page.preparedSha256 === annotation.preparedSha256
    if (page.preparedSha256 && annotation.preparedSha256 && !preparedMatch) continue
    const original = transcription?.sourceFiles?.find((file) => file.fileIndex === (document.fileIndex ?? page.preparedFileIndex))
    const originalMatch = original?.sha256 && document.sha256 && original.sha256 === document.sha256
    if (original?.sha256 && document.sha256 && !originalMatch) continue
    if (!preparedMatch && !originalMatch) continue
    // null explicitly marks pages not included in the reader's prepared image.
    const localBox = boxOnPage(box, Object.hasOwn(page, 'preparedRegion') ? page.preparedRegion : [0, 0, 1, 1], precision)
    if (localBox) locations.push({ documentId: document.id, pageId: page.id, bbox: localBox, text: annotation.text || '', precision,
      annotationId: annotation.id, description: [annotation.location, annotation.endpointsOrDatum].filter(Boolean).join(' · ') })
  }
  return locations
}

// Localization is separate display metadata. It must match the saved reading's
// ID, text and image bytes; it never replaces the dimension or model value.
export function localizedSourceAnnotations(sourceRun) {
  const transcription = sourceRun?.sourceTranscription
  const supplement = sourceRun?.sourceDimensionLocations
  const matchesRun = supplement?.version === 'cad-source-locations-v1' && typeof sourceRun.runId === 'string'
    && Number.isInteger(sourceRun.revision) && supplement.runId === sourceRun.runId && supplement.revision === sourceRun.revision
  const locations = new Map((matchesRun && Array.isArray(supplement.annotations) ? supplement.annotations : []).map((item) => [item.id, item]))
  return (transcription?.annotations || []).map((annotation) => {
    const location = locations.get(annotation.id)
    // A completed recheck may withdraw an earlier estimated position. Keep the
    // reading itself intact, but do not fall back to that rejected text box.
    if (matchesRun && !location && Array.isArray(supplement.unlocatedAnnotationIds)
      && supplement.unlocatedAnnotationIds.includes(annotation.id)) return { ...annotation, bbox: null }
    const preparedSha256 = annotation.preparedSha256 || transcription?.preparedFiles?.find((file) => file.fileIndex === annotation.fileIndex)?.sha256
    if (!location || !preparedSha256 || location.preparedSha256 !== preparedSha256 || location.fileIndex !== annotation.fileIndex
      || location.text !== annotation.text || location.bboxFrame !== 'prepared_source_image' || !normalizedDimensionBox(location.bbox)) return annotation
    return { ...annotation, bbox: location.bbox, bboxFrame: location.bboxFrame, preparedSha256,
      locationPrecision: location.locationPrecision }
  })
}

export function drawingParameterBindings(model, documents = [], sourceRun = model?.agentRun) {
  const rows = cadParameterRows(model?.cadPlan), byKey = new Map(rows.map((row) => [row.key, row]))
  const transcription = sourceRun?.sourceTranscription
  const annotations = new Map(localizedSourceAnnotations(sourceRun).map((annotation) => [annotation.id, annotation]))
  const idsFor = (row, visited = new Set()) => {
    if (visited.has(row.key)) return []
    visited.add(row.key)
    const ids = sourceAnnotationIds(row.source)
    // Derived dimensions can point to several independent callouts.
    for (const key of String(row.expression || '').match(/[A-Za-z_][A-Za-z0-9_]*/g) || []) {
      if (byKey.has(key)) ids.push(...idsFor(byKey.get(key), visited))
    }
    return [...new Set(ids)]
  }
  return rows.map((row) => {
    const annotationIds = idsFor(row)
    const references = annotationIds.map((id) => annotations.get(id)).filter(Boolean)
    const locations = references.flatMap((annotation) => annotationLocations(annotation, documents, transcription))
    const locatedIds = new Set(locations.filter((location) => location.precision === 'exact').map((location) => location.annotationId))
    const evidence = typeof row.source === 'string' ? row.source : row.source?.text || row.source?.description || row.source?.evidence || ''
    const functions = { sqrt: '平方根', abs: '绝对值', min: '最小值', max: '最大值', sin: '正弦', cos: '余弦', radians: '转弧度' }
    const expressionLabel = row.expression ? String(row.expression).replace(/[A-Za-z_][A-Za-z0-9_]*/g,
      (name) => byKey.get(name)?.label || functions[name] || name).replaceAll('**', '^').replaceAll('*', ' × ').replaceAll('/', ' ÷ ').replace(/\s+/g, ' ').trim() : undefined
    return { key: row.key, label: row.label, value: row.expression ? (!model.agentRun?.dirty ? model.agentRun?.resolvedParameters?.[row.key] : null) : row.value,
      unit: row.unit, expression: row.expression, expressionLabel, sourceText: references.map((annotation) => [annotation.text, annotation.location, annotation.endpointsOrDatum].filter(Boolean).join(' · ')).join('；') || evidence,
      annotationIds, missingAnnotationCount: annotationIds.filter((id) => !locatedIds.has(id)).length,
      status: locatedIds.size ? (locatedIds.size < annotationIds.length ? 'partial' : 'located') : locations.length ? 'region' : 'unlocated', locations }
  })
}
