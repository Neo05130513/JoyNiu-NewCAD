import test from 'node:test'
import assert from 'node:assert/strict'
import { refreshCadResourceLinks, cadResourceRefreshDelay } from './cadFileResources.js'

test('refresh changes download capabilities while preserving manual edits and confirmation facts', () => {
  const model = { cadPlan: { parameters: { width: 20 } }, agentRun: { runId: 'cad_a', revision: 2, dirty: true, confirmedAt: 'original', status: 'ready' } }
  const generation = { runId: 'cad_a', revision: 2, stale: true, planSignature: 'old-plan' }
  const result = { runId: 'cad_a', revision: 2, plan: { parameters: { width: 10 } }, confirmedAt: 'wrong', artifacts: [{ url: '/fresh' }], sourceDocuments: [{ filename: 'a.dwg' }], fileLinksExpiresAt: 1000 }
  const changed = refreshCadResourceLinks(model, generation, result)
  assert.equal(changed.model.cadPlan.parameters.width, 20)
  assert.equal(changed.model.agentRun.dirty, true)
  assert.equal(changed.model.agentRun.confirmedAt, 'original')
  assert.equal(changed.generation.stale, true)
  assert.equal(changed.generation.planSignature, 'old-plan')
  assert.equal(changed.generation.artifacts[0].url, '/fresh')
  assert.equal(refreshCadResourceLinks(model, generation, { ...result, revision: 3 }), null)
  assert.equal(refreshCadResourceLinks(model, generation, { ...result, runId: 'another' }), null)
})

test('expired and short-lived capabilities retry at bounded intervals without a hot loop', () => {
  assert.equal(cadResourceRefreshDelay(1, 100000), 15000)
  assert.equal(cadResourceRefreshDelay(undefined), 45 * 60 * 1000)
  assert.equal(cadResourceRefreshDelay(4000, 1000000), 45 * 60 * 1000)
})
