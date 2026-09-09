import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as candidateSync from './candidateSync.js'
import * as arched from './archedClevisSupport.js'
import { validateModelParameters } from './modelValidation.js'
import { archedClevisSupportGeometries } from './viewerGeometry.js'

// Exercise App's actual definitions, envelope normalization and candidate
// construction. A helper-only test would miss the template spread that caused
// an eleven-dimension AI answer to render a complete twelve-dimension support.
const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const context = vm.createContext({ ...candidateSync, ...arched, validateModelParameters })
vm.runInContext([
  app.slice(app.indexOf('const defaultPreferences ='), app.indexOf('const chatId =')),
  app.slice(app.indexOf('function rawParametersFromRecognition('), app.indexOf('function recognitionEvidence(')),
  app.slice(app.indexOf('const evidenceAcceptedForPreview ='), app.indexOf('\nfunction App()')),
  'globalThis.helpers = { partDefinition, parametersFromRecognition, rawParametersFromRecognition, modelFromCandidate, seedModelForAi, modelForPendingDrawing, modelBoundsText, requiredParameterPresent, recognitionParameterAliases }',
].join('\n'), context)
const h = context.helpers
const definition = h.partDefinition('arched_clevis_support')
const candidate = (omitted = 'mountHoleCenterDistance') => Object.fromEntries(definition.keys
  .filter((key) => key !== omitted).map((key) => [key, definition.preview[key]]))
const pending = (parameters) => ({ status: 'ready', evidence: {
  id: 'ocr_drawing_10', status: 'pending', partType: definition.kind,
  candidateParameters: parameters,
} })

test('the actual AI upload handoff never fills a missing installation pitch from the preview template', () => {
  const parameters = { ...candidate(), baseThickness: 6 }
  const recognition = { ...pending(parameters).evidence, engine: 'ai-candidate' }
  const recognized = h.parametersFromRecognition(recognition)
  const seed = h.seedModelForAi({ baseModel: { ...definition.preview }, definition, recognizedParameters: recognized, hasAttachments: true, currentKind: definition.kind })
  const { model } = candidateSync.applyAiModelPatch(seed, parameters, definition.keys)
  assert.equal(model.mountHoleCenterDistance, undefined)
  assert.equal(model.baseThickness, 6, 'preserve the returned number, including an unconfirmed interpretation')
  assert.equal(validateModelParameters(model).valid, false)
  assert.ok(Number.isNaN(arched.archedClevisSupportDimensions(model).baseLength))
  assert.throws(() => archedClevisSupportGeometries(model), /安装孔中心距/)
  assert.equal(h.modelBoundsText(model, { bboxLength: 110, bboxWidth: 50, bboxHeight: 55 }), '— × 50 × 55')
  const complete = candidateSync.applyAiModelPatch(model, { mountHoleCenterDistance: 80 }, definition.keys).model
  assert.equal(validateModelParameters(complete).valid, true)
  assert.equal(h.modelBoundsText(complete), '110 × 50 × 55')
  assert.equal(archedClevisSupportGeometries(complete).length, 5)
})

test('all twelve required dimensions remain absent through recognition and text-only AI seeding', () => {
  for (const omitted of definition.required) {
    const parameters = candidate(omitted)
    const recognized = h.parametersFromRecognition(pending(parameters).evidence)
    assert.equal(Object.hasOwn(recognized, omitted), false, omitted)
    assert.equal(validateModelParameters(recognized).valid, false, omitted)
    assert.throws(() => archedClevisSupportGeometries(recognized), RangeError)
    const seed = h.seedModelForAi({ baseModel: { name: '我的支座', kind: '' }, definition, hasAttachments: false, currentKind: '' })
    const applied = candidateSync.applyAiModelPatch(seed, parameters, definition.keys).model
    assert.equal(Object.hasOwn(applied, omitted), false, omitted)
    assert.equal(applied.name, '我的支座')
  }
})

test('pending snapshots and cleared values cannot regain stale recipe or saved-model dimensions', () => {
  for (const value of [undefined, null, '', '   ']) {
    const parameters = { ...candidate(), ...(value === undefined ? {} : { mountHoleCenterDistance: value }) }
    const job = pending(parameters)
    job.evidence.parameters = { ...definition.preview }
    job.evidence.recognizedParameters = { ...definition.preview }
    job.evidence.modelRecipe = { recipeId: definition.recipeId, parameters: { ...definition.preview } }
    const saved = { ...definition.preview, name: '客户支座' }
    const restored = h.modelForPendingDrawing(saved, job)
    assert.equal(restored.name, '客户支座')
    assert.equal(validateModelParameters(restored).valid, false)
    assert.ok(Number.isNaN(arched.archedClevisSupportDimensions(restored).baseLength))
    assert.equal(h.requiredParameterPresent('mountHoleCenterDistance', restored.mountHoleCenterDistance), false)
    assert.equal(h.modelBoundsText(restored), '— × 50 × 55')
    assert.equal(h.modelForPendingDrawing(restored, job), restored, 'stable snapshots must not cause a React update loop')
  }
  const saved = { ...definition.preview }
  const confirmed = pending(candidate())
  confirmed.evidence.status = 'confirmed'
  assert.equal(h.modelForPendingDrawing(saved, confirmed), saved, 'confirmed saved geometry is not rewritten by pending migration')
})

test('cleared operands are unknown and a legitimate zero insert offset stays present', () => {
  for (const value of [undefined, null, '', ' ', false, true]) {
    const dimensions = arched.archedClevisSupportDimensions({ ...definition.preview, mountHoleCenterDistance: value, earCenterHeight: value })
    assert.ok(Number.isNaN(dimensions.baseLength))
    assert.ok(Number.isNaN(dimensions.totalHeight))
    assert.equal(h.requiredParameterPresent('insertAxialOffset', value), false)
  }
  assert.equal(h.requiredParameterPresent('insertAxialOffset', 0), true)
  assert.equal(h.requiredParameterPresent('insertAxialOffset', '0'), true)
})

test('actual generation success messages use derived bounds and format both kernel metric contracts', () => {
  const tailStart = app.indexOf('    const recognizedName = recognized.name || generationDefinition.preview.name')
  const tailEnd = app.indexOf('\n  }\n  const attachDrawingToConversation', tailStart)
  assert.ok(tailStart > 0 && tailEnd > tailStart)
  for (const metrics of [{}, { bboxLength: 110, bboxWidth: 50.00000000000001, bboxHeight: 55 }, { boundingLength: 110, boundingWidth: 50.00000000000001, boundingHeight: 55 }]) {
    const messages = []
    Object.assign(context, {
      recognized: { name: '圆弧双耳支座 · AI 候选' }, recognizedParameters: { ...definition.preview }, generationDefinition: definition,
      generated: { validation: { productionReady: true, metrics } }, modelRef: { current: null },
      setModel: () => {}, setSelectedFeature: () => {}, setActivePanel: () => {}, setView: () => {}, setZoom: () => {},
      setActiveMode: () => {}, setIsGenerating: () => {}, showToast: () => {},
      setMessages: (updater) => messages.push(...updater([])),
    })
    vm.runInContext(`(() => { ${app.slice(tailStart, tailEnd)} })()`, context)
    assert.match(messages[0].text, /包络 110 × 50 × 55 mm/)
    assert.doesNotMatch(messages[0].text, /undefined|NaN|50\.000000/)
  }
})
