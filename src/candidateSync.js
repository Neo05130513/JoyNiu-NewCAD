// Pure normalization helpers for the AI -> editable CAD handoff.  Keeping
// this code outside the React component makes the response contract and the
// concurrent-edit guard independently testable.

const isRecord = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const normalizeField = (field, aliases) => aliases?.[field] || field.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase())

export function isRecipeIncompatible(value) {
  const compatibility = value?.recipeCompatibility || value?.recipe_compatibility
  return compatibility?.status === 'unsupported' || (Boolean(compatibility?.status) && compatibility.status !== 'supported'
    && Boolean((compatibility.unsupportedFeatures || compatibility.unsupported_features || []).length))
}

export function resolveAiPartKind(value, { definitions, fallback = 'bracket', kindAliases = {}, parameterAliases = {} }) {
  if (isRecipeIncompatible(value)) return ''
  const canonicalKind = (kind) => {
    const normalized = String(kind || '').trim().toLowerCase()
    return kindAliases[normalized] || normalized
  }
  const kinds = Object.keys(definitions)
  const direct = [
    value?.partType, value?.part_type, value?.kind,
    value?.recipeId, value?.recipe_id,
    value?.modelRecipe?.recipeId, value?.modelRecipe?.recipe_id,
    value?.model_recipe?.recipeId, value?.model_recipe?.recipe_id,
  ].map(canonicalKind).find((kind) => kinds.includes(kind))
  if (direct) return direct

  const containers = [
    value?.parameterPatch, value?.parameter_patch,
    value?.candidateParameters, value?.candidate_parameters,
    value?.recognizedParameters, value?.recognized_parameters,
    value?.parameters, value?.modelRecipe?.parameters, value?.model_recipe?.parameters,
  ].filter(isRecord)
  const keys = new Set((containers.length ? containers : [value || {}])
    .flatMap((container) => Object.entries(container).filter(([, value]) => value !== undefined && value !== null && value !== '').map(([key]) => key))
    .map((key) => normalizeField(key, parameterAliases)))
  // Only a field unique to a recipe identifies its topology. Shared fields
  // such as material and units must leave the customer's current part intact.
  const inferredKinds = kinds.filter((kind) => {
    const otherKeys = new Set(kinds.filter((other) => other !== kind).flatMap((other) => definitions[other].keys))
    return definitions[kind].keys.some((key) => !otherKeys.has(key) && keys.has(key))
  })
  // Mixed recipe fields are not enough to choose a new topology. Keep an
  // existing part (or an empty document) until the response identifies one.
  if (inferredKinds.length === 1) return inferredKinds[0]
  return canonicalKind(fallback)
}

export function hasAiGeometryParameters(patch) {
  return Object.entries(patch || {}).some(([key, value]) => !['material', 'units', 'insertThreadDesignation', 'holeThrough'].includes(key)
    && (typeof value === 'number' || (typeof value === 'string' && value.trim())) && Number.isFinite(Number(value)))
}

export function canApplyAiModelEdit({ baseModel, accepted, identityChanged }) {
  // A blank document has no geometry to edit. A greeting, material-only reply
  // or an identity without dimensions must not create a default sample part.
  if (!baseModel?.kind) return hasAiGeometryParameters(accepted)
  return identityChanged || Object.keys(accepted || {}).length > 0
}

export function seedAiModel({ baseModel, definition, recognizedParameters, hasAttachments = false, currentKind = baseModel?.kind }) {
  if (recognizedParameters) {
    return {
      ...recognizedParameters,
      kind: definition.kind,
      recipeId: definition.recipeId,
      name: recognizedParameters.name || definition.preview.name,
      updatedAt: '刚刚',
    }
  }
  return {
    ...definition.preview,
    ...(!hasAttachments && currentKind === definition.kind ? baseModel : {}),
    ...(!baseModel?.kind && baseModel?.name ? { name: baseModel.name } : {}),
    kind: definition.kind,
    recipeId: definition.recipeId,
  }
}

export function normalizeAiParameterPatch(result, aliases = {}) {
  if (!isRecord(result)) return {}

  // Older API builds and a few OpenAI-compatible relays use snake_case or a
  // generic candidate/parameters container.  Merge from least to most
  // authoritative so the canonical parameterPatch always wins.
  const containers = [
    result.parameters,
    result.candidate_parameters,
    result.candidateParameters,
    result.parameter_patch,
    result.parameterPatch,
  ]
  const normalized = {}
  containers.forEach((container) => {
    if (!isRecord(container)) return
    Object.entries(container).forEach(([field, value]) => {
      if (value === undefined || value === null || value === '') return
      normalized[normalizeField(field, aliases)] = value
    })
  })
  return normalized
}

// Report only changes that the active recipe can actually consume. Provider
// prose and the presence of a patch alone are not evidence of a model edit.
export function applyAiModelPatch(base, patch, allowedKeys) {
  const allowed = new Set(allowedKeys)
  const accepted = {}, rejected = []
  for (const [key, value] of Object.entries(patch || {})) {
    let normalized
    if (!allowed.has(key)) { rejected.push(key); continue }
    if (['material', 'insertThreadDesignation'].includes(key)) {
      if (typeof value === 'string' && value.trim()) normalized = value.trim()
    } else if (key === 'units') {
      if (String(value).toLowerCase() === 'mm') normalized = 'mm'
    } else if (key === 'holeThrough') {
      if (typeof value === 'boolean') normalized = value
    } else if ((typeof value === 'number' || (typeof value === 'string' && value.trim())) && Number.isFinite(Number(value))) {
      const number = Number(value)
      if (key === 'insertAxialOffset' ? number >= 0 : number > 0) normalized = number
    }
    if (normalized === undefined) rejected.push(key)
    else accepted[key] = normalized
  }
  // These legacy bracket fields track their corresponding main dimensions.
  if (base.kind === 'bracket') {
    if (accepted.totalHeight !== undefined && accepted.holeDepth === undefined) accepted.holeDepth = accepted.totalHeight
    if (accepted.upperWidth !== undefined && accepted.saddleDepth === undefined) accepted.saddleDepth = accepted.upperWidth
  }
  const changed = Object.fromEntries(Object.entries(accepted).filter(([key, value]) => (
    typeof value === 'number' && base[key] !== null && base[key] !== undefined && base[key] !== ''
      ? Number(base[key]) !== value
      : base[key] !== value
  )))
  return { model: Object.keys(changed).length ? { ...base, ...accepted, updatedAt: '刚刚' } : base, accepted, changed, rejected }
}

// A follow-up on a drawing must edit its confirmation payload as well as the
// viewport; otherwise accepting/generating it restores the old dimensions.
export function syncAiDrawingEdit(job, { model, patch, definition, existingParameters, sameKind: matchingKind, needsReview = false }) {
  if (!job?.evidence) return job
  const sameKind = matchingKind ?? [job.evidence.partType, job.evidence.kind, job.evidence.recipeId, job.evidence.modelRecipe?.recipeId]
    .some((kind) => kind === definition.kind || kind === definition.recipeId)
  if (!sameKind) return { ...job, evidence: null, analysis: null, generation: null, status: 'idle', candidateFields: [], humanEditedFields: [], customerAccepted: false, humanConfirmed: false, questions: [] }
  const evidence = job.evidence
  const previous = existingParameters ?? Object.fromEntries(Object.entries({
    ...(evidence.model_recipe?.parameters || {}), ...(evidence.modelRecipe?.parameters || {}),
    ...(evidence.parameters || {}), ...(evidence.recognized_parameters || {}), ...(evidence.recognizedParameters || {}),
    ...(evidence.candidate_parameters || {}), ...(evidence.candidateParameters || {}),
  }).map(([key, value]) => [normalizeField(key), value]))
  if (!needsReview && Object.entries(patch).every(([key, value]) => value === previous[key])) return job
  const merged = { ...previous, ...patch }
  const candidateParameters = Object.fromEntries(Object.entries(merged).filter(([key, value]) => definition.keys.includes(key) && value !== undefined && value !== null && value !== ''))
  const missingFields = definition.required.filter((key) => candidateParameters[key] === undefined || candidateParameters[key] === null || candidateParameters[key] === '')
  const changedFields = Object.keys(patch).filter((key) => typeof patch[key] === 'number' && previous[key] !== undefined && previous[key] !== null && previous[key] !== ''
    ? patch[key] !== Number(previous[key]) : patch[key] !== previous[key])
  const pending = needsReview || !['confirmed', 'preview_confirmed'].includes(evidence.status)
  return {
    ...job,
    status: 'ready', generation: null,
    ...(pending ? { customerAccepted: false, humanConfirmed: false } : {}),
    evidence: { ...evidence, partType: model.kind, recipeId: definition.recipeId,
      parameters: candidateParameters, candidateParameters, recognizedParameters: candidateParameters,
      modelRecipe: { ...(evidence.modelRecipe || {}), recipeId: definition.recipeId, parameters: candidateParameters },
      candidateParameterMeta: { ...(evidence.candidateParameterMeta || {}), ...Object.fromEntries(changedFields.map((key) => [key, { source: 'ai', confidence: null, requiresConfirmation: pending }])) },
      status: pending ? 'pending' : evidence.status },
    candidateFields: Object.keys(candidateParameters),
    humanEditedFields: (job.humanEditedFields || []).filter((key) => !changedFields.includes(key)),
    analysis: { ...(job.analysis || {}), candidateParameters, missingFields, needsInput: missingFields.length > 0,
      candidateSources: { ...(job.analysis?.candidateSources || {}), ...Object.fromEntries(changedFields.map((key) => [key, 'ai'])) },
      defaultedFields: (job.analysis?.defaultedFields || []).filter((key) => !changedFields.includes(key)) },
  }
}

export function aiEditOutcome({ changed, rejected = [], accepted = {}, geometryChanged, modelValid = true }) {
  if (!changed) return Object.keys(accepted).length
    ? '本轮返回的参数与当前模型相同，预览未改变。'
    : '本轮未返回可应用的参数修改，预览未改变。请提供具体尺寸，或在参数面板中修改。'
  if (!modelValid) return '参数已写入，但尺寸约束存在冲突，预览已暂停。请修正参数面板中的标红项。'
  const message = geometryChanged ? '尺寸修改已应用到当前预览。' : '属性已更新；本轮没有改变几何尺寸。'
  return rejected.length ? `${message}部分返回字段不受当前零件支持，未应用。` : message
}

export function normalizeAiParameterEvidence(rawEvidence, aliases = {}) {
  const normalized = {}
  if (isRecord(rawEvidence)) {
    Object.entries(rawEvidence).forEach(([field, evidence]) => {
      normalized[normalizeField(field, aliases)] = isRecord(evidence) ? evidence : {}
    })
    return normalized
  }
  if (!Array.isArray(rawEvidence)) return normalized

  rawEvidence.forEach((item) => {
    if (!isRecord(item)) return
    const rawField = String(item.parameter || item.field || item.parameterName || '').trim()
    if (!rawField) return
    const field = normalizeField(rawField, aliases)
    const { parameter: _parameter, field: _field, parameterName: _parameterName, ...evidence } = item
    // First evidence wins as the primary source.  Keep additional rows for
    // audit without allowing them to silently replace the displayed source.
    if (!normalized[field]) normalized[field] = evidence
    else normalized[field] = {
      ...normalized[field],
      additionalEvidence: [...(normalized[field].additionalEvidence || []), evidence],
    }
  })
  return normalized
}

export function shouldProtectConcurrentModelEdit({
  hasAttachments,
  patchChanged,
  baseRevision,
  currentRevision,
}) {
  // A text-only edit targets the current model, so a concurrent human change
  // wins.  An attachment starts a new drawing context: its successful recipe
  // and candidates must replace the old preview even if that old model was
  // touched while the analysis was running.
  return !hasAttachments && Boolean(patchChanged) && currentRevision !== baseRevision
}
