import test from 'node:test'
import assert from 'node:assert/strict'
import { cadParameterRows, cadParameterValues, cadConfirmationParameters, cadParameterEditMessage, editCadParameter, cadPlanSignature } from './cadAgentState.js'
import { drawingParameterBindings } from './sourceDrawingState.js'

const plan = () => ({ version: 'cad-plan-v1', units: 'mm', parameters: {
  length: { value: 100, label: '安装板长度', source: { type: 'drawing', annotationIds: ['annotation-1'] } },
  slotAngle: { value: 23, source: { type: 'drawing', annotationIds: ['annotation-2'] } },
  halfLength: { value: null, expression: 'length / 2' },
  holeDepthFromTop: { value: 38 },
} })

test('Chinese presentation preserves original parameter keys, values, expressions and plan signature', () => {
  const original = plan(), before = structuredClone(original), signature = cadPlanSignature(original)
  const rows = cadParameterRows(original)
  assert.equal(rows[0].label, '安装板长度')
  assert.ok(rows.every((row) => /\p{Script=Han}/u.test(row.label)))
  assert.deepEqual(rows.map((row) => row.key), Object.keys(original.parameters))
  assert.equal(rows[1].unit, '°')
  assert.equal(rows[2].expression, 'length / 2')
  assert.deepEqual(cadParameterValues(original), { length: 100, slotAngle: 23, halfLength: null, holeDepthFromTop: 38 })
  assert.deepEqual(cadConfirmationParameters(original), { length: 100, slotAngle: 23, holeDepthFromTop: 38 })
  assert.deepEqual(original, before)
  assert.equal(cadPlanSignature(original), signature)
})

test('drawing review uses Chinese formulas and angle units while source links keep the same IDs', () => {
  const model = { kind: 'feature_model', cadPlan: plan(), agentRun: { dirty: false, resolvedParameters: { halfLength: 50 } } }
  const bindings = drawingParameterBindings(model)
  assert.equal(bindings[1].unit, '°')
  assert.equal(bindings[2].expression, 'length / 2')
  assert.equal(bindings[2].expressionLabel, '安装板长度 ÷ 2')
  assert.deepEqual(bindings[2].annotationIds, ['annotation-1'])
  assert.equal(bindings[2].value, 50)
})

test('editing a Chinese-named field still updates the original key and retains its source evidence', () => {
  const model = { kind: 'feature_model', cadPlan: plan(), agentRun: { parameterBaseline: { length: 100, slotAngle: 23, holeDepthFromTop: 38 } } }
  const updated = editCadParameter(model, 'slotAngle', 25)
  assert.equal(updated.cadPlan.parameters.slotAngle.value, 25)
  assert.deepEqual(updated.cadPlan.parameters.slotAngle.source.drawingSource.annotationIds, ['annotation-2'])
  assert.deepEqual(Object.keys(updated.cadPlan.parameters), Object.keys(model.cadPlan.parameters))
  const text = cadParameterEditMessage(updated)
  assert.match(text, /从23改为25/)
  assert.doesNotMatch(text, /slotAngle/)
})
