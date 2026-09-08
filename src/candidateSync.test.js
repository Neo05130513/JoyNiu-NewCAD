import test from 'node:test'
import assert from 'node:assert/strict'

import {
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
