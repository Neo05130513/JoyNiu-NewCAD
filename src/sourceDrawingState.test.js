import test from 'node:test'
import assert from 'node:assert/strict'
import { drawingParameterBindings, localizedSourceAnnotations, normalizedBox, sourceAnnotationIds } from './sourceDrawingState.js'
import { editCadParameter } from './cadAgentState.js'

const fullPage = [0, 0, 1, 1]
const annotation = (overrides = {}) => ({ id: 'annotation-1', fileIndex: 0, imageId: 'source-0-original',
  preparedSha256: 'prepared-a', bboxFrame: 'prepared_source_image', bbox: [.2, .3, .1, .05],
  sourceRegion: [.1, .1, .6, .7], text: '60', location: '上侧尺寸线', endpointsOrDatum: '左右外边缘', ...overrides })
const document = (overrides = {}) => ({ id: 'drawing-a', fileIndex: 0, sha256: 'original-a', filename: 'drawing.png',
  pages: [{ id: 'page-a', preparedFileIndex: 0, preparedSha256: 'prepared-a', preparedRegion: fullPage }], ...overrides })
const drawingSource = (ids = ['annotation-1']) => ({ type: 'drawing', text: '原图板宽', annotationIds: ids, fileIndex: 0, view: 'front' })
const model = (parameters = { width: { label: '板宽', value: 60, source: drawingSource() } }, annotations = [annotation()]) => ({
  kind: 'feature_model', cadPlan: { version: 'cad-plan-v1', units: 'mm', parameters },
  agentRun: { dirty: false, resolvedParameters: {}, sourceTranscription: { candidateEvidence: true, verified: false,
    sourceFiles: [{ fileIndex: 0, sha256: 'original-a' }], annotations } },
})
const binding = (value = model(), documents = [document()], sourceRun) => drawingParameterBindings(value, documents, sourceRun)[0]

test('repeated manual edits retain legacy string evidence and object provenance for comparison', () => {
  for (const source of ['annotation-1：原图板宽60', drawingSource()]) {
    const initial = model({ width: { label: '板宽', value: 60, source } })
    const changed = editCadParameter(editCadParameter(initial, 'width', '65'), 'width', '70')
    assert.deepEqual(changed.cadPlan.parameters.width.source.drawingSource, source)
    assert.equal(changed.cadPlan.parameters.width.source.type, 'manual')
    assert.equal(binding(changed).value, 70)
    assert.equal(binding(changed).status, 'located')
    assert.equal(initial.cadPlan.parameters.width.value, 60)
  }
})
const approximately = (actual, expected) => {
  assert.equal(actual.length, expected.length)
  actual.forEach((value, index) => assert.ok(Math.abs(value - expected[index]) < 1e-10, `${value} != ${expected[index]}`))
}

test('explicit annotation IDs locate the original callout while showing the current parameter value', () => {
  const value = model({ width: { label: '板宽', value: 72, source: drawingSource() } })
  const result = binding(value)
  assert.equal(result.label, '板宽')
  assert.equal(result.value, 72)
  assert.equal(result.unit, 'mm')
  assert.equal(result.status, 'located')
  assert.equal(result.sourceText, '60 · 上侧尺寸线 · 左右外边缘')
  assert.equal(result.locations.length, 1)
  const { bbox, ...location } = result.locations[0]
  approximately(bbox, [.2, .3, .1, .05])
  assert.deepEqual(location, { documentId: 'drawing-a', pageId: 'page-a', text: '60', precision: 'exact',
    annotationId: 'annotation-1', description: '上侧尺寸线 · 左右外边缘' })
})

test('legacy prose IDs and structured IDs are deduplicated without matching partial identifiers', () => {
  assert.deepEqual(sourceAnnotationIds({ annotationIds: ['annotation-2', 'annotation-2', 6],
    text: '依据 annotation-1、annotation-2。不要认作 xannotation-3 / annotation-4-extra / annotation-5_ignored。' }),
  ['annotation-2', 'annotation-1'])
  assert.deepEqual(sourceAnnotationIds('参见 [annotation-1]'), ['annotation-1'])
  for (const source of ['参见 annotation-1', { type: 'drawing', text: '尺寸见 annotation-1' }]) {
    const result = binding(model({ width: { value: 60, source } }))
    assert.equal(result.status, 'located')
    assert.equal(result.locations[0].annotationId, 'annotation-1')
  }
})

test('equal numeric values and unknown IDs never invent a link to a drawing annotation', () => {
  const value = model({ width: { value: 60, source: { text: '原图显示 60', view: 'front', fileIndex: 0 } },
    depth: { value: 60, source: drawingSource(['annotation-999']) }, height: { value: 60 } },
  [annotation(), annotation({ id: 'annotation-2', bbox: [.7, .3, .1, .05] })])
  for (const result of drawingParameterBindings(value, [document()])) {
    assert.equal(result.status, 'unlocated')
    assert.deepEqual(result.locations, [])
  }
})

test('duplicate printed numbers resolve to the cited physical annotation, not the first matching value', () => {
  const value = model({ width: { value: 60, source: drawingSource(['annotation-2']) } },
    [annotation(), annotation({ id: 'annotation-2', bbox: [.7, .3, .1, .05], location: '下侧尺寸线' })])
  const result = binding(value)
  assert.equal(result.locations.length, 1)
  assert.equal(result.locations[0].annotationId, 'annotation-2')
  approximately(result.locations[0].bbox, [.7, .3, .1, .05])
})

test('conflicting prepared or original hashes prevent highlights even if the other hash matches', () => {
  for (const changed of [document({ sha256: 'replacement-original' }),
    document({ pages: [{ id: 'page-a', preparedFileIndex: 0, preparedSha256: 'replacement-prepared', preparedRegion: fullPage }] })]) {
    const result = binding(model(), [changed])
    assert.equal(result.status, 'unlocated')
    assert.deepEqual(result.locations, [])
    assert.match(result.sourceText, /60/)
  }
})

test('a matching filename and file index without any matching content identity are insufficient', () => {
  const value = model()
  delete value.agentRun.sourceTranscription.sourceFiles
  delete value.agentRun.sourceTranscription.annotations[0].preparedSha256
  assert.equal(binding(value).status, 'unlocated')
  assert.deepEqual(binding(value).locations, [])
})

test('a surviving original hash or prepared hash supports older saved evidence independently', () => {
  const originalOnly = model()
  delete originalOnly.agentRun.sourceTranscription.annotations[0].preparedSha256
  assert.equal(binding(originalOnly).status, 'located')
  const preparedOnly = model()
  delete preparedOnly.agentRun.sourceTranscription.sourceFiles
  assert.equal(binding(preparedOnly).status, 'located')
})

test('a matching hash cannot override a different prepared image index or an unsupported coordinate frame', () => {
  const wrongIndex = document({ pages: [{ id: 'page-a', preparedFileIndex: 1, preparedSha256: 'prepared-a' }] })
  assert.equal(binding(model(), [wrongIndex]).status, 'unlocated')
  const wrongFrame = model(undefined, [annotation({ bboxFrame: 'cropped_image' })])
  assert.equal(binding(wrongFrame).status, 'unlocated')
})

test('invalid boxes are rejected and cannot be presented as precise coordinates', () => {
  const invalid = [null, [], [0, 0, 1], [0, 0, 0, .1], [-.1, 0, .1, .1], [.8, 0, .3, .1],
    [0, .9, .1, .2], ['0', 0, .1, .1], [true, 0, .1, .1], [0, 0, Infinity, .1], [0, NaN, .1, .1]]
  for (const bbox of invalid) {
    assert.equal(normalizedBox(bbox), null)
    const result = binding(model(undefined, [annotation({ bbox, sourceRegion: null })]))
    assert.equal(result.status, 'unlocated')
    assert.deepEqual(result.locations, [])
  }
  assert.deepEqual(normalizedBox([0, 0, 1, 1]), [0, 0, 1, 1])
})

test('old evidence without a text box uses its containing region and explicitly reports region precision', () => {
  for (const bbox of [undefined, null, [.9, .9, .5, .5]]) {
    const result = binding(model(undefined, [annotation({ bbox })]))
    assert.equal(result.status, 'region')
    assert.equal(result.locations[0].precision, 'region')
    approximately(result.locations[0].bbox, [.1, .1, .6, .7])
    assert.match(result.sourceText, /上侧尺寸线/)
  }
})

test('preparedRegion null means the page was not supplied to the reader and must never receive a highlight', () => {
  const unseenPage = document({ pages: [{ id: 'page-a', preparedFileIndex: 0, preparedSha256: 'prepared-a', preparedRegion: null }] })
  assert.equal(binding(model(), [unseenPage]).status, 'unlocated')
  const legacyPage = document({ pages: [{ id: 'page-a', preparedFileIndex: 0, preparedSha256: 'prepared-a' }] })
  assert.equal(binding(model(), [legacyPage]).status, 'located')
})

const twoPagePdf = () => document({ pages: [
  { id: 'pdf-page-1', preparedFileIndex: 0, preparedSha256: 'prepared-a', preparedRegion: [0, 0, 1, .5] },
  { id: 'pdf-page-2', preparedFileIndex: 0, preparedSha256: 'prepared-a', preparedRegion: [0, .5, 1, .5] },
] })

test('a box on a combined PDF image maps into only its actual page with page-local coordinates', () => {
  const value = model(undefined, [annotation({ bbox: [.2, .6, .1, .1] })])
  const result = binding(value, [twoPagePdf()])
  assert.equal(result.locations.length, 1)
  assert.equal(result.locations[0].pageId, 'pdf-page-2')
  approximately(result.locations[0].bbox, [.2, .2, .1, .2])
})

test('a purported text box spanning a PDF page boundary is not a precise callout on either page', () => {
  const value = model(undefined, [annotation({ bbox: [.2, .45, .1, .1] })])
  const result = binding(value, [twoPagePdf()])
  assert.equal(result.status, 'unlocated')
  assert.deepEqual(result.locations, [])
})

test('an old containing region can cover several PDF pages while retaining region precision', () => {
  const value = model(undefined, [annotation({ bbox: null, sourceRegion: [.2, .4, .2, .2] })])
  const result = binding(value, [twoPagePdf()])
  assert.equal(result.status, 'region')
  assert.deepEqual(result.locations.map((location) => location.pageId), ['pdf-page-1', 'pdf-page-2'])
  approximately(result.locations[0].bbox, [.2, .8, .2, .2])
  approximately(result.locations[1].bbox, [.2, 0, .2, .2])
  assert.ok(result.locations.every((location) => location.precision === 'region'))
})

test('derived expressions recursively collect several annotations across source files without duplicates', () => {
  const value = model({ width: { value: 60, source: drawingSource() },
    thickness: { value: 6, source: drawingSource(['annotation-2']) },
    half: { value: null, expression: 'width / 2' },
    offset: { value: null, expression: 'max(half - thickness, 0)', source: drawingSource(['annotation-1']) } },
  [annotation(), annotation({ id: 'annotation-2', fileIndex: 1, preparedSha256: 'prepared-b', text: '6' })])
  value.agentRun.sourceTranscription.sourceFiles.push({ fileIndex: 1, sha256: 'original-b' })
  value.agentRun.resolvedParameters = { half: 30, offset: 24 }
  const documents = [document(), document({ id: 'drawing-b', fileIndex: 1, sha256: 'original-b',
    pages: [{ id: 'page-b', preparedFileIndex: 1, preparedSha256: 'prepared-b', preparedRegion: fullPage }] })]
  const result = drawingParameterBindings(value, documents).find((item) => item.key === 'offset')
  assert.equal(result.value, 24)
  assert.equal(result.expression, 'max(half - thickness, 0)')
  assert.deepEqual(result.locations.map((location) => [location.annotationId, location.documentId]),
    [['annotation-1', 'drawing-a'], ['annotation-2', 'drawing-b']])
  assert.match(result.sourceText, /60/)
  assert.match(result.sourceText, /6/)
})

test('cyclic or missing expression dependencies cannot loop forever or fabricate annotations', () => {
  const value = model({ a: { value: null, expression: 'b + missing', source: drawingSource() },
    b: { value: null, expression: 'a / 2' } })
  const results = drawingParameterBindings(value, [document()])
  assert.equal(results.length, 2)
  assert.ok(results.every((result) => result.locations.length === 1 && result.locations[0].annotationId === 'annotation-1'))
})

test('manual edits retain original drawingSource references without replacing the current user value', () => {
  const source = { type: 'user', text: '用户将板宽改为72', drawingSource: drawingSource() }
  const result = binding(model({ width: { value: 72, source } }))
  assert.deepEqual(sourceAnnotationIds(source), ['annotation-1'])
  assert.equal(result.value, 72)
  assert.equal(result.status, 'located')
  assert.equal(result.locations[0].text, '60')
  assert.match(result.sourceText, /^60/)
})

test('dirty derived dimensions hide stale resolved values but retain source locations for checking', () => {
  const value = model({ width: { value: 72, source: drawingSource() }, half: { value: null, expression: 'width / 2' } })
  value.agentRun.resolvedParameters = { width: 60, half: 30 }
  value.agentRun.dirty = true
  const [width, half] = drawingParameterBindings(value, [document()])
  assert.equal(width.value, 72)
  assert.equal(half.value, null)
  assert.equal(half.status, 'located')
  value.agentRun.dirty = false
  value.agentRun.resolvedParameters.half = 36
  assert.equal(drawingParameterBindings(value, [document()])[1].value, 36)
})

test('a refreshed source run provides evidence without replacing the local model edits', () => {
  const value = model({ width: { value: 72, source: drawingSource() } })
  const savedRun = structuredClone(value.agentRun)
  delete value.agentRun.sourceTranscription
  assert.equal(binding(value).status, 'unlocated')
  const result = binding(value, [document()], savedRun)
  assert.equal(result.status, 'located')
  assert.equal(result.value, 72)
  assert.equal(result.locations[0].text, '60')
})

const withLocalization = () => {
  const value = model({ width: { value: 72, source: drawingSource(['annotation-2']) } },
    [annotation({ bbox: null }), annotation({ id: 'annotation-2', bbox: null, location: '下侧尺寸线' })])
  Object.assign(value.agentRun, { runId: 'cad_saved', revision: 2,
    sourceDimensionLocations: { version: 'cad-source-locations-v1', runId: 'cad_saved', revision: 2, status: 'partial',
      annotations: [{ id: 'annotation-2', text: '60', fileIndex: 0, preparedSha256: 'prepared-a',
        bboxFrame: 'prepared_source_image', bbox: [.7, .7, .03, .02], locationPrecision: 'model_estimated_text_box' }] } })
  return value
}

test('supplementary text positions precisely locate old drawings without modifying evidence or model values', () => {
  const value = withLocalization(), before = structuredClone(value)
  const result = binding(value)
  assert.equal(result.status, 'located')
  assert.equal(result.value, 72)
  assert.equal(result.missingAnnotationCount, 0)
  assert.deepEqual(result.annotationIds, ['annotation-2'])
  approximately(result.locations[0].bbox, [.7, .7, .03, .02])
  assert.equal(result.locations[0].annotationId, 'annotation-2')
  assert.equal(result.locations[0].description, '下侧尺寸线 · 左右外边缘')
  assert.equal(localizedSourceAnnotations(value.agentRun)[0].bbox, null)
  assert.deepEqual(value, before)
})

test('stale run, revision, source bytes, text and coordinate frame never supply exact positions', () => {
  const changes = [
    (s) => { s.runId = 'other-run' }, (s) => { s.revision = 1 }, (s) => { s.version = 'unknown' },
    (s) => { s.annotations[0].preparedSha256 = 'other-image' }, (s) => { s.annotations[0].fileIndex = 1 },
    (s) => { s.annotations[0].id = 'unknown' }, (s) => { s.annotations[0].text = '72' },
    (s) => { s.annotations[0].bboxFrame = 'crop_image' }, (s) => { s.annotations[0].bbox = [0, 0, 4, 5] },
  ]
  for (const change of changes) {
    const value = withLocalization(); change(value.agentRun.sourceDimensionLocations)
    assert.equal(binding(value).status, 'region')
    assert.equal(binding(value).missingAnnotationCount, 1)
    assert.ok(binding(value).locations.every((location) => location.precision === 'region'))
  }
})

test('derived dimensions explicitly report partially located supporting callouts', () => {
  const value = withLocalization()
  value.cadPlan.parameters.width.source = drawingSource(['annotation-1', 'annotation-2', 'annotation-999'])
  const result = binding(value)
  assert.equal(result.status, 'partial')
  assert.equal(result.missingAnnotationCount, 2)
  assert.deepEqual(result.locations.map((location) => location.precision), ['region', 'exact'])
})

test('supplementary positions use the prepared file identity when older annotations omit it', () => {
  const value = withLocalization()
  delete value.agentRun.sourceTranscription.annotations[1].preparedSha256
  assert.equal(binding(value).status, 'region')
  value.agentRun.sourceTranscription.preparedFiles = [{ fileIndex: 0, sha256: 'prepared-a' }]
  assert.equal(binding(value).status, 'located')
})

test('an entire region supplied as a bbox is not accepted as a precise dimension text position', () => {
  const value = model(undefined, [annotation({ bbox: [.1, .1, .6, .7] })])
  assert.equal(binding(value).status, 'region')
  assert.equal(binding(value).missingAnnotationCount, 1)
  const supplemented = withLocalization()
  supplemented.agentRun.sourceDimensionLocations.annotations[0].bbox = [.1, .1, .6, .7]
  assert.equal(binding(supplemented).status, 'region')
})

test('a completed recheck can withdraw an earlier estimated box without changing its reading', () => {
  const value = withLocalization()
  value.agentRun.sourceTranscription.annotations[1].bbox = [.7, .6, .03, .02]
  value.agentRun.sourceDimensionLocations.annotations = []
  value.agentRun.sourceDimensionLocations.unlocatedAnnotationIds = ['annotation-2']
  const original = structuredClone(value.agentRun.sourceTranscription)
  assert.equal(binding(value).status, 'region')
  assert.equal(binding(value).missingAnnotationCount, 1)
  assert.deepEqual(value.agentRun.sourceTranscription, original)
})
