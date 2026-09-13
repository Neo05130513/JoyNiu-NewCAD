import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { cadArtifactUrl } from './cadAgentClient.js'
import { refreshCadResourceLinks } from './cadFileResources.js'

// Exercise the actual workbench callback without making a modelling call.
const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const start = app.indexOf('  const recoverExpiredProductionGlb = async')
const callback = app.slice(start, app.indexOf('  const rebuildCurrentModel', start))
function harness(getRun) {
  const original = { runId: 'cad_preview', revision: 2, artifacts: [{ format: 'glb', url: '/api/v1/cad-agent/runs/cad_preview/2/artifacts/glb?access=old' }] }
  const modelRef = { current: { kind: 'feature_model', cadPlan: { parameters: { width: 88 } }, agentRun: { ...original, dirty: true } } }
  const generationRef = { current: { ...original, stale: true } }
  const workspaceIdRef = { current: 'test-file' }, notices = [], resourceDownloadsRef = { current: new Set() }
  const ctx = vm.createContext({ modelRef, generationRef, workspaceIdRef, resourceDownloadsRef, AbortController,
    accountTokenRef: { current: 'test-owner-session' }, cadArtifactRecoveryRef: { current: { inFlight: new Set(), attemptedUrls: new Set() } },
    isFeatureModel: model => model?.kind === 'feature_model', cadAgent: { getRun }, cadArtifactUrl, refreshCadResourceLinks,
    setModel: value => { modelRef.current = value }, setGeneration: value => { generationRef.current = typeof value === 'function' ? value(generationRef.current) : value },
    readableError: error => error.message, showToast: message => notices.push(message),
  })
  vm.runInContext(`${callback}\nglobalThis.recover = recoverExpiredProductionGlb`, ctx)
  return { ...ctx, notices, failure: { ...original, artifactUrl: cadArtifactUrl(original.artifacts[0]), status: 403 },
    fresh: { ...original, artifacts: [{ format: 'glb', url: original.artifacts[0].url.replace('old', 'renewed') }] } }
}

test('expired GLB renews owner links, retaining edited dimensions without rebuilding', async () => {
  let calls = 0
  const state = harness(async request => { calls++; assert.equal(request.token(), 'test-owner-session'); return state.fresh })
  await state.recover(state.failure)
  assert.equal(calls, 1)
  assert.equal(state.modelRef.current.cadPlan.parameters.width, 88)
  assert.equal(state.modelRef.current.agentRun.dirty, true)
  assert.equal(state.generationRef.current.stale, true)
  assert.match(state.generationRef.current.artifacts[0].url, /renewed/)
  assert.equal(state.generationRef.current.artifactStatus, 'available')
  assert.equal(state.resourceDownloadsRef.current.size, 0)
  await state.recover(state.failure)
  assert.equal(calls, 1, 'late failure for an obsolete URL does not overwrite fresh links')
})

test('late preview recovery cannot change a newly opened file', async () => {
  let finish
  const state = harness(() => new Promise(resolve => { finish = resolve }))
  const pending = state.recover(state.failure)
  state.workspaceIdRef.current = 'another-file'
  const before = state.modelRef.current
  finish(state.fresh)
  await pending
  assert.equal(state.modelRef.current, before)
  assert.equal(state.resourceDownloadsRef.current.size, 0)
})

test('missing files show a bounded recovery error, and obsolete revisions never request', async () => {
  let calls = 0
  const state = harness(async () => { calls++; throw new Error('原实体文件不存在') })
  await state.recover({ ...state.failure, revision: 1 })
  assert.equal(calls, 0)
  await state.recover(state.failure)
  await state.recover(state.failure)
  assert.equal(calls, 1)
  assert.equal(state.generationRef.current.artifactStatus, 'unavailable')
  assert.match(state.notices[0], /原实体文件不存在/)
})
