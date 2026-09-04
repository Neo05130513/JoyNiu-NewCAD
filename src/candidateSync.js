// Pure normalization helpers for the AI -> editable CAD handoff.  Keeping
// this code outside the React component makes the response contract and the
// concurrent-edit guard independently testable.

const isRecord = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value)

const normalizeField = (field, aliases) => aliases?.[field] || field

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
