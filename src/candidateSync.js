// Pure normalization helpers for the AI -> editable CAD handoff.  Keeping
// this code outside the React component makes the response contract and the
// concurrent-edit guard independently testable.

const isRecord = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const normalizeField = (field, aliases) => aliases?.[field] || field

export function resolveAiPartKind(value, { definitions, fallback = 'bracket', kindAliases = {}, parameterAliases = {} }) {
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
    .flatMap((container) => Object.keys(container))
    .map((key) => normalizeField(key, parameterAliases)))
  // Only a field unique to a recipe identifies its topology. Shared fields
  // such as material and units must leave the customer's current part intact.
  for (const kind of kinds) {
    const otherKeys = new Set(kinds.filter((other) => other !== kind).flatMap((other) => definitions[other].keys))
    if (definitions[kind].keys.some((key) => !otherKeys.has(key) && keys.has(key))) return kind
  }
  return canonicalKind(fallback) || 'bracket'
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
