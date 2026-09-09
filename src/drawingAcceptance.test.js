import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { readableError } from './workspaceFeedback.js'
import { archedClevisSupportDefinition as definition } from './archedClevisSupport.js'
import { isFeatureModel } from './cadAgentState.js'

// Execute the actual UI handler with its state/API boundary supplied, so an
// HTTP rejection cannot regress into a success toast or confirmed candidate.
const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const handler = app.slice(app.indexOf('const acceptDrawingData = async () => {'), app.indexOf('const generateFromDrawing = async () => {'))
function harness({ error, result, backendStatus = 'connected', id = 'ocr_test', duringRequest } = {}) {
  const parameters = Object.fromEntries(definition.keys.map((key) => [key, definition.preview[key]]))
  const model = { ...definition.preview }
  const state = { job: { status: 'ready', evidence: { id, status: 'pending', partType: definition.kind, candidateParameters: parameters } }, model, toasts: [], accepting: false, apiCalls: 0 }
  const drawingJobRef = { current: state.job }, modelRef = { current: model }, revision = { current: 1 }
  const context = vm.createContext({
    isGenerating: false, isAccepting: false, aiConversation: { turnStatus: 'idle' },
    isFeatureModel,
    drawingJobRef, modelRef, modelInteractionRevisionRef: revision, modelValid: true,
    isRecipeIncompatible: () => false, partKindFromEnvelope: () => definition.kind,
    partDefinition: () => definition, rawParametersFromRecognition: (evidence) => evidence?.candidateParameters || evidence?.parameters || null,
    requiredParameterPresent: (key, value) => value !== undefined && value !== null && value !== '',
    productionPartKinds: [definition.kind], canonicalPartKind: (kind) => kind,
    backend: { status: backendStatus }, platform: { token: '' }, readableError,
    showToast: (message, type) => state.toasts.push({ message, type }),
    setIsAccepting: (value) => { state.accepting = value },
    setDrawingJob: (updater) => { state.job = typeof updater === 'function' ? updater(state.job) : updater; drawingJobRef.current = state.job },
    setModel: (updater) => { state.model = updater(state.model) },
    api: { acceptDrawing: async () => {
      state.apiCalls += 1
      duringRequest?.(state, drawingJobRef, revision)
      if (error) throw error
      return result || { id, status: 'confirmed', parameters }
    } },
  })
  vm.runInContext(`${handler}\nglobalThis.runAcceptance = acceptDrawingData`, context)
  return { state, run: context.runAcceptance }
}

test('HTTP confirmation failures retain candidates and never claim local or server success', async () => {
  for (const status of [400, 401, 403, 404, 409, 422, 500, 503]) {
    for (const backendStatus of ['connected', 'offline']) {
      const h = harness({ error: Object.assign(new Error('earGap 必须匹配总宽'), { status }), backendStatus })
      const originalParameters = h.state.job.evidence.candidateParameters
      const originalModel = h.state.model
      assert.equal(await h.run(), false)
      assert.equal(h.state.job.status, 'ready')
      assert.equal(h.state.job.evidence.status, 'pending')
      assert.equal(h.state.job.customerAccepted, false)
      assert.equal(h.state.job.humanConfirmed, false)
      assert.equal(h.state.job.evidence.candidateParameters, originalParameters)
      assert.equal(h.state.model, originalModel)
      assert.match(h.state.job.error, /确认失败/)
      assert.equal(h.state.toasts.at(-1).type, 'error')
      assert.ok(h.state.toasts.every(({ message }) => !message.includes('已记录你的确认') && !message.includes('数据已确认')))
      assert.equal(h.state.accepting, false)
    }
  }
})

test('only confirmed server responses advance the confirmation state', async () => {
  const good = harness()
  assert.equal(await good.run(), true)
  assert.equal(good.state.job.evidence.status, 'confirmed')
  assert.equal(good.state.job.customerAccepted, true)
  assert.equal(good.state.job.humanConfirmed, true)
  const pending = harness({ result: { status: 'pending' } })
  assert.equal(await pending.run(), false)
  assert.equal(pending.state.job.evidence.status, 'pending')
  assert.match(pending.state.toasts.at(-1).message, /服务端尚未确认/)
})

test('local preview confirmation is restricted to explicit offline transport or offline candidates', async () => {
  const offline = harness({ error: new TypeError('Failed to fetch'), backendStatus: 'offline' })
  assert.equal(await offline.run(), true)
  assert.equal(offline.state.job.evidence.status, 'preview_confirmed')
  assert.equal(offline.state.job.humanConfirmed, false)
  assert.match(offline.state.job.evidence.warning, /联网后仍需服务端确认/)
  const local = harness({ id: 'offline_test', backendStatus: 'offline' })
  assert.equal(await local.run(), true)
  assert.equal(local.state.apiCalls, 0)
  for (const options of [
    { error: new TypeError('Failed to fetch') },
    { error: Object.assign(new Error('aborted'), { name: 'AbortError' }), backendStatus: 'offline' },
    { error: new SyntaxError('invalid JSON'), backendStatus: 'offline' },
    { id: 'offline_test', backendStatus: 'connected' },
  ]) {
    const h = harness(options)
    assert.equal(await h.run(), false)
    assert.equal(h.state.job.evidence.status, 'pending')
  }
})

test('a failed old confirmation cannot overwrite a newer candidate', async () => {
  const h = harness({ error: Object.assign(new Error('rejected'), { status: 422 }), duringRequest: (state, ref, revision) => {
    state.job = { status: 'ready', evidence: { id: 'new_candidate', status: 'pending', candidateParameters: { archOuterRadius: 32 } } }
    ref.current = state.job
    revision.current += 1
  } })
  assert.equal(await h.run(), false)
  assert.equal(h.state.job.evidence.id, 'new_candidate')
  assert.equal(h.state.job.error, undefined)
})
