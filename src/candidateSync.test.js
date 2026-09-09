import test from 'node:test'
import assert from 'node:assert/strict'

import {
  aiEditOutcome,
  applyAiModelPatch,
  canApplyAiModelEdit,
  isRecipeIncompatible,
  syncAiDrawingEdit,
  normalizeAiParameterEvidence,
  normalizeAiParameterPatch,
  resolveAiPartKind,
  seedAiModel,
  shouldProtectConcurrentModelEdit,
} from './candidateSync.js'

const aliases = {
  main_length: 'mainLength',
  insert_axial_offset: 'insertAxialOffset',
}

const definitions = Object.fromEntries([
  ['stepped_tapered_nozzle', 'stepped_tapered_nozzle_with_insert_v1', { mainLength: 98, headLength: 50, neckLength: 20 }],
  ['split_clamp_support', 'split_clamp_support_v1', { baseLength: 125, baseWidth: 95, boreDiameter: 36 }],
  ['bracket', 'bracket_support_v1', { baseLength: 100, baseWidth: 50, upperLength: 70, notchRadius: 15 }],
  ['shaft', 'shaft_v1', { length: 70, outerDiameter: 24, holeDiameter: 10 }],
].map(([kind, recipeId, dimensions]) => [kind, {
  kind,
  recipeId,
  keys: [...Object.keys(dimensions), 'material', 'units'],
  preview: { kind, recipeId, name: kind, ...dimensions, material: '45# 钢', units: 'mm' },
}]))
const kindAliases = Object.fromEntries(Object.values(definitions).map(({ kind, recipeId }) => [recipeId, kind]))
const resolveKind = (result, fallback) => resolveAiPartKind(result, { definitions, fallback, kindAliases, parameterAliases: aliases })

test('a rejected drawing recipe cannot be revived by its identity or old parameter fields', () => {
  for (const compatibility of [{ status: 'unsupported', unsupportedFeatures: [] }, { status: 'uncertain', unsupportedFeatures: ['双耳外轮廓'] }]) {
    const result = { partType: 'bracket', recipeId: 'bracket_support_v1', parameterPatch: { notchRadius: 16 }, recipeCompatibility: compatibility }
    assert.equal(isRecipeIncompatible(result), true)
    assert.equal(resolveKind(result, 'bracket'), '')
  }
  assert.equal(isRecipeIncompatible({ parameterPatch: { material: '钢' } }), false)
  assert.equal(resolveKind({ partType: 'shaft', recipeCompatibility: { status: 'supported', unsupportedFeatures: [] } }, ''), 'shaft')
})

test('blank conversations do not infer a sample part from greetings or material-only responses', () => {
  for (const result of [{}, { parameterPatch: {} }, { parameterPatch: { material: 'AL6061 铝合金' } }]) {
    assert.equal(resolveKind(result, ''), '')
  }
  for (const accepted of [{}, { material: 'AL6061 铝合金' }, { units: 'mm', holeThrough: true }]) {
    assert.equal(canApplyAiModelEdit({ baseModel: { kind: '' }, accepted, identityChanged: true }), false)
  }
})

test('a blank project can create a real parameterized part and then accept property-only edits', () => {
  const seed = seedAiModel({ baseModel: { kind: '', name: '零件 01' }, definition: definitions.shaft })
  assert.equal(seed.name, '零件 01')
  assert.equal(seed.kind, 'shaft')
  for (const [parameterPatch, kind] of [[{ length: 100 }, 'shaft'], [{ notchRadius: 15 }, 'bracket'], [{ boreDiameter: 36 }, 'split_clamp_support'], [{ mainLength: 98 }, 'stepped_tapered_nozzle']]) {
    assert.equal(resolveKind({ parameterPatch }, ''), kind)
    assert.equal(canApplyAiModelEdit({ baseModel: { kind: '' }, accepted: parameterPatch, identityChanged: true }), true)
    assert.equal(canApplyAiModelEdit({ baseModel: { kind }, accepted: { material: 'AL6061 铝合金' }, identityChanged: false }), true)
  }
})

test('material and unit edits preserve all four existing model types and their custom dimensions', () => {
  for (const definition of Object.values(definitions)) {
    const baseModel = Object.fromEntries(Object.entries(definition.preview)
      .map(([key, value]) => [key, typeof value === 'number' ? value + 3 : value]))
    baseModel.name = '用户已修改的零件'
    for (const parameterPatch of [{ material: 'AL6061 铝合金' }, { units: 'mm' }, { material: 'SUS304 不锈钢', units: 'mm' }]) {
      for (const identity of [{}, { partType: definition.kind }, { recipeId: definition.recipeId }]) {
        const result = { ...identity, parameterPatch }
        const kind = resolveKind(result, baseModel.kind)
        const seed = seedAiModel({ baseModel, definition: definitions[kind] })
        assert.equal(kind, baseModel.kind)
        assert.deepEqual(seed, baseModel)
      }
    }
  }
})

test('supported explicit types and recipe ids override conflicting parameter field hints', () => {
  for (const definition of Object.values(definitions)) {
    const conflictingPatch = { mainLength: 98, boreDiameter: 36, notchRadius: 15, outerDiameter: 24, material: 'AL6061 铝合金' }
    const identities = [
      { partType: definition.kind },
      { part_type: definition.kind },
      { partType: 'unknown', recipeId: definition.recipeId },
      { partType: 'unknown', model_recipe: { recipe_id: definition.recipeId } },
    ]
    for (const identity of identities) {
      const kind = resolveKind({ ...identity, parameterPatch: conflictingPatch }, 'shaft')
      assert.equal(kind, definition.kind)
      const seed = seedAiModel({ baseModel: definitions.shaft.preview, definition: definitions[kind] })
      assert.equal(seed.kind, definition.kind)
      assert.equal(seed.recipeId, definition.recipeId)
    }
  }
})

test('recipe-specific fields still classify legacy results that omit a supported identity', () => {
  for (const [field, kind] of [
    ['mainLength', 'stepped_tapered_nozzle'],
    ['boreDiameter', 'split_clamp_support'],
    ['notchRadius', 'bracket'],
    ['length', 'shaft'],
    ['outerDiameter', 'shaft'],
  ]) {
    assert.equal(resolveKind({ partType: 'unknown', parameterPatch: { [field]: 42, material: 'AL6061 铝合金' } }, 'bracket'), kind)
  }
  assert.equal(resolveKind({ parameterPatch: {}, candidate_parameters: { main_length: 98 } }, 'shaft'), 'stepped_tapered_nozzle')
  assert.equal(resolveKind({ baseLength: 140, material: 'AL6061 铝合金' }, 'split_clamp_support'), 'split_clamp_support')
})

test('ambiguous recipe fields cannot create a sample part or switch an existing topology', () => {
  const parameterPatch = { outerDiameter: 36, length: 100, notchRadius: 15 }
  assert.equal(resolveKind({ parameterPatch }, ''), '')
  assert.equal(resolveKind({ parameterPatch }, 'shaft'), 'shaft')
  assert.equal(resolveKind({ parameterPatch }, 'split_clamp_support'), 'split_clamp_support')
  assert.equal(resolveKind({ parameterPatch: { outerDiameter: 36, notchRadius: null, mainLength: null } }, ''), 'shaft')
  assert.equal(resolveKind({ parameterPatch: { outerDiameter: null, notchRadius: null } }, ''), '')
})

test('switching recipes uses the resolved recipe without inheriting unrelated old dimensions', () => {
  const baseModel = { ...definitions.shaft.preview, length: 120 }
  const kind = resolveKind({ partType: 'bracket', parameterPatch: { material: 'AL6061 铝合金' } }, baseModel.kind)
  const seed = seedAiModel({ baseModel, definition: definitions[kind] })
  assert.deepEqual(seed, definitions.bracket.preview)
  assert.equal(seed.length, undefined)
})

test('new drawing seeds and recognized candidates do not inherit prior model edits', () => {
  const definition = definitions.bracket
  const baseModel = { ...definition.preview, baseLength: 180, name: '上一张图纸' }
  assert.deepEqual(seedAiModel({ baseModel, definition, hasAttachments: true }), definition.preview)
  const recognizedParameters = { baseLength: 132, name: '新图纸候选' }
  assert.deepEqual(seedAiModel({ baseModel, definition, recognizedParameters, hasAttachments: true }), {
    ...recognizedParameters,
    kind: definition.kind,
    recipeId: definition.recipeId,
    updatedAt: '刚刚',
  })
})

test('normalizes DWG recipe values and preserves a valid zero offset', () => {
  const patch = normalizeAiParameterPatch({
    parameter_patch: { main_length: 98, insert_axial_offset: 0 },
    parameterPatch: { headLength: 50 },
  }, aliases)

  assert.deepEqual(patch, { mainLength: 98, insertAxialOffset: 0, headLength: 50 })
})

test('canonical parameterPatch wins over compatibility containers', () => {
  const patch = normalizeAiParameterPatch({
    parameters: { mainLength: 90 },
    candidateParameters: { mainLength: 95 },
    parameterPatch: { mainLength: 98 },
  })

  assert.equal(patch.mainLength, 98)
})

test('normalizes list-shaped parameter evidence for the inspector', () => {
  const evidence = normalizeAiParameterEvidence([
    { parameter: 'main_length', sourceType: 'direct_dimension', confidence: 0.99 },
    { field: 'insert_axial_offset', sourceType: 'ai_interpreted', confidence: 0.5 },
  ], aliases)

  assert.equal(evidence.mainLength.sourceType, 'direct_dimension')
  assert.equal(evidence.insertAxialOffset.sourceType, 'ai_interpreted')
})

test('concurrent edit protection applies only to text-only model edits', () => {
  assert.equal(shouldProtectConcurrentModelEdit({
    hasAttachments: false,
    patchChanged: true,
    baseRevision: 4,
    currentRevision: 5,
  }), true)
  assert.equal(shouldProtectConcurrentModelEdit({
    hasAttachments: true,
    patchChanged: true,
    baseRevision: 4,
    currentRevision: 5,
  }), false)
  assert.equal(shouldProtectConcurrentModelEdit({
    hasAttachments: false,
    patchChanged: false,
    baseRevision: 4,
    currentRevision: 5,
  }), false)
})

test('chat dimension changes accept snake case and numeric strings, rejecting unsupported and invalid fields', () => {
  const base = definitions.shaft.preview
  const patch = normalizeAiParameterPatch({ parameterPatch: { outer_diameter: '36', length: 100, bevel: 5, mainLength: 120, holeDiameter: true } })
  const result = applyAiModelPatch(base, patch, definitions.shaft.keys)
  assert.equal(result.model.outerDiameter, 36)
  assert.equal(result.model.length, 100)
  assert.equal(result.model.holeDiameter, base.holeDiameter)
  assert.deepEqual(result.changed, { outerDiameter: 36, length: 100 })
  assert.deepEqual(result.rejected, ['bevel', 'mainLength', 'holeDiameter'])
  assert.equal(base.outerDiameter, 24)
})

test('empty, ignored and numerically unchanged replies cannot report an applied geometry change', () => {
  const base = definitions.shaft.preview
  for (const patch of [{}, { bevel: 4 }, { length: '70' }, { length: null }, { length: '' }]) {
    const result = applyAiModelPatch(base, patch, definitions.shaft.keys)
    assert.equal(result.model, base)
    assert.deepEqual(result.changed, {})
    assert.match(aiEditOutcome({ ...result, changed: false }), /预览未改变/)
  }
  const material = applyAiModelPatch(base, { material: 'AL6061 铝合金' }, definitions.shaft.keys)
  assert.deepEqual(material.changed, { material: 'AL6061 铝合金' })
  assert.match(aiEditOutcome({ changed: true, geometryChanged: false }), /没有改变几何尺寸/)
  assert.match(aiEditOutcome({ changed: true, geometryChanged: true, modelValid: false }), /预览已暂停/)
})

test('bracket chat edits keep through-depth helper parameters aligned', () => {
  const base = { kind: 'bracket', upperWidth: 50, saddleDepth: 50, totalHeight: 40, holeDepth: 40 }
  const result = applyAiModelPatch(base, { upperWidth: 60, totalHeight: 45 }, Object.keys(base))
  assert.equal(result.model.saddleDepth, 60)
  assert.equal(result.model.holeDepth, 45)
})

test('follow-up chat updates every drawing candidate consumer before confirm and generate', () => {
  const definition = { ...definitions.bracket, required: ['baseLength', 'baseWidth', 'upperLength', 'notchRadius'] }
  const parameters = { baseLength: 100, baseWidth: 50, upperLength: 70 }
  const job = {
    status: 'ready', customerAccepted: false, humanConfirmed: false,
    evidence: { id: 'drawing-1', partType: 'bracket', status: 'pending', parameters, candidateParameters: parameters, modelRecipe: { recipeId: definition.recipeId, parameters } },
    analysis: { candidateSources: { baseLength: 'manual' }, defaultedFields: ['notchRadius'] }, humanEditedFields: ['baseLength'],
  }
  const result = syncAiDrawingEdit(job, { model: { ...definition.preview, baseLength: 140 }, patch: { baseLength: 140, notchRadius: 15 }, definition })
  for (const candidate of [result.evidence.parameters, result.evidence.candidateParameters, result.evidence.recognizedParameters, result.evidence.modelRecipe.parameters, result.analysis.candidateParameters]) {
    assert.equal(candidate.baseLength, 140)
    assert.equal(candidate.notchRadius, 15)
  }
  assert.equal(result.evidence.id, 'drawing-1')
  assert.equal(result.evidence.status, 'pending')
  assert.equal(result.analysis.needsInput, false)
  assert.deepEqual(result.analysis.defaultedFields, [])
  assert.deepEqual(result.humanEditedFields, [])
  assert.equal(job.evidence.parameters.baseLength, 100)
})

test('no-op chat preserves generated drawing status and topology changes detach old evidence', () => {
  const definition = { ...definitions.bracket, required: ['baseLength'] }
  const job = { status: 'generated', generation: { requestId: 'old' }, evidence: { partType: 'bracket', status: 'confirmed', candidateParameters: { baseLength: 100 } }, customerAccepted: true }
  assert.equal(syncAiDrawingEdit(job, { model: definition.preview, patch: { baseLength: 100 }, definition }), job)
  const switched = syncAiDrawingEdit(job, { model: definitions.shaft.preview, patch: { outerDiameter: 36 }, definition: { ...definitions.shaft, required: ['length'] } })
  assert.equal(switched.evidence, null)
  assert.equal(switched.generation, null)
  assert.equal(switched.customerAccepted, false)
})

test('recipe-only legacy evidence survives follow-up edits and unchanged manual fields keep provenance', () => {
  const definition = { ...definitions.bracket, required: ['baseLength', 'baseWidth', 'upperLength', 'notchRadius'] }
  const parameters = { base_length: 100, base_width: 50, upper_length: 70, notch_radius: 15 }
  const job = { status: 'ready', evidence: { partType: 'bracket', status: 'pending', model_recipe: { parameters }, candidateParameterMeta: { baseLength: { source: 'manual' } } }, humanEditedFields: ['baseLength'], analysis: { candidateSources: { baseLength: 'manual' } } }
  const result = syncAiDrawingEdit(job, { model: definition.preview, patch: { baseLength: 100, notchRadius: 20 }, definition })
  assert.deepEqual(result.evidence.candidateParameters, { baseLength: 100, baseWidth: 50, upperLength: 70, notchRadius: 20 })
  assert.deepEqual(result.humanEditedFields, ['baseLength'])
  assert.equal(result.analysis.candidateSources.baseLength, 'manual')
  assert.equal(result.evidence.candidateParameterMeta.baseLength.source, 'manual')
  assert.equal(result.analysis.needsInput, false)
  const review = syncAiDrawingEdit(job, { model: definition.preview, patch: {}, definition, needsReview: true })
  assert.equal(Object.keys(review.evidence.candidateParameters).length, 4)
})
