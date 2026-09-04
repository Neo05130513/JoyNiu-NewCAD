import test from 'node:test'
import assert from 'node:assert/strict'

import {
  normalizeAiParameterEvidence,
  normalizeAiParameterPatch,
  shouldProtectConcurrentModelEdit,
} from './candidateSync.js'

const aliases = {
  main_length: 'mainLength',
  insert_axial_offset: 'insertAxialOffset',
}

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
