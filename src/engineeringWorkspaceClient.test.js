import test from 'node:test'
import assert from 'node:assert/strict'
import { createEngineeringClient, engineeringNumber, engineeringDirection, engineeringDrawingSettings, engineeringDraftEquals, removeEngineeringView, topologySelector, engineeringCanvasVisible } from './engineeringWorkspaceClient.js'

test('STEP import validates before upload and sends the real file with owner authorization', async () => {
  const calls = []
  const client = createEngineeringClient({ base: 'http://localhost/api/cad/designs', fetchImpl: async (...args) => { calls.push(args); return Response.json({ id: 'actual-import-response' }) } })
  for (const file of [new File(['text'], 'image.png'), new File([], 'empty.step'), { name: 'large.stp', size: 20 * 1024 * 1024 + 1 }]) assert.throws(() => client.importStep(file, 'library', { token: 'owner' }))
  assert.equal(calls.length, 0)
  const file = new File(['ISO-10303-21;'], '机架.STP')
  assert.equal((await client.importStep(file, 'library', { token: 'owner' })).id, 'actual-import-response')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer owner')
  assert.equal(calls[0][1].headers['Content-Type'], undefined)
  assert.equal(calls[0][1].body.get('fileId'), 'library')
  assert.equal(await calls[0][1].body.get('file').text(), 'ISO-10303-21;')
})

test('a stalled response body times out and tells the user to check whether saving completed', async () => {
  const client = createEngineeringClient({ fetchImpl: async (_url, { signal }) => ({ ok: true, json: () => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })) }) })
  await assert.rejects(client.assemble({}, { timeoutMs: 10 }), error => error.name === 'TimeoutError' && /确认保存结果/.test(error.message))
})

test('account cancellation remains AbortError and network errors are readable', async () => {
  const control = new AbortController()
  const client = createEngineeringClient({ fetchImpl: async (_url, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })) })
  const pending = client.list({ signal: control.signal })
  control.abort()
  await assert.rejects(pending, { name: 'AbortError' })
  const offline = createEngineeringClient({ fetchImpl: async () => { throw new TypeError('Failed to fetch') } })
  await assert.rejects(offline.list(), /无法连接工程服务/)
})

test('kernel validation detail is preserved for the user', async () => {
  const client = createEngineeringClient({ fetchImpl: async () => Response.json({ detail: '视图 2 超出图框，请缩小比例或调整布局。' }, { status: 422 }) })
  await assert.rejects(client.drawing('design-a', {}), error => error.status === 422 && error.message.includes('视图 2 超出图框'))
})

test('removing a middle view preserves other dimensions and rebases references', () => {
  const draft = { views: [{ view: 'front' }, { view: 'top' }, { view: 'right' }], dimensions: [{ viewIndex: 0, label: 'front length' }, { viewIndex: 1, label: 'top diameter' }, { viewIndex: 2, label: 'right height' }], dimension: { viewIndex: 2, a: 'vertex:1', b: 'vertex:4' } }
  const result = removeEngineeringView(draft, 1)
  assert.deepEqual(result.views, [{ view: 'front' }, { view: 'right' }])
  assert.deepEqual(result.dimensions, [{ viewIndex: 0, label: 'front length' }, { viewIndex: 1, label: 'right height' }])
  assert.deepEqual(result.dimension, { viewIndex: 1, a: 'vertex:1', b: 'vertex:4' })
  assert.equal(result.removedDimensions, 1)
  assert.equal(draft.dimensions.length, 3)
  assert.equal(removeEngineeringView(draft, 2).dimension.viewIndex, 1)
})

test('blank required coordinates never become zero and valid negative positions survive', () => {
  for (const value of ['', ' ', null, undefined, true, Infinity, 'not a number']) assert.throws(() => engineeringNumber(value, '位置 X'), /有效数字/)
  assert.equal(engineeringNumber('-12.5', '位置 X'), -12.5)
  assert.equal(engineeringNumber('0', '位置 X'), 0)
  assert.throws(() => engineeringDirection(['', 1, 0]), /有效数字/)
  assert.throws(() => engineeringDirection([0, 0, 0]), /不能全部为零/)
  assert.deepEqual(engineeringDirection(['1', '-2', '3']), [1, -2, 3])
})

test('drawing controls serialize automatic layout and compare saved settings without key-order drift', () => {
  const settings = engineeringDrawingSettings({ page: 'A4', hidden: false, views: [{ view: 'front', x: '', y: '', scale: '0.5' }, { view: 'section', normal: ['1', '2', '3'], position: '-2', sectionMode: 'cutaway' }], dimensions: [] })
  assert.deepEqual(settings.views[0], { view: 'front', scale: 0.5 })
  assert.deepEqual(settings.views[1].normal, [1, 2, 3])
  assert.equal(settings.views[1].position, -2)
  assert.ok(engineeringDraftEquals(settings, { dimensions: [], views: [{ scale: 0.5, view: 'front' }, settings.views[1]], hidden: false, page: 'A4' }))
  assert.ok(!engineeringDraftEquals(settings, { ...settings, page: 'A3' }))
  assert.throws(() => engineeringDrawingSettings({ ...settings, views: [{ view: 'front', scale: 0 }] }), /有效数字/)
})

test('topology selector does not accept trailing fields or unsafe indices', () => {
  assert.deepEqual(topologySelector('face:0'), { kind: 'face', index: 0 })
  for (const value of ['face:0:stale', 'face:-1', 'edge:9007199254740993', '']) assert.equal(topologySelector(value), null)
})

test('hidden workspaces and zero-size canvases cannot render or produce a zero aspect ratio', () => {
  const visible = { active: true, hidden: false, width: 800, height: 380 }
  assert.equal(engineeringCanvasVisible(visible), true)
  for (const patch of [{ active: false }, { hidden: true }, { width: 0 }, { height: 0 }, { width: undefined }, { width: NaN }]) assert.equal(engineeringCanvasVisible({ ...visible, ...patch }), false)
  assert.equal(engineeringCanvasVisible({ ...visible, width: 320, height: 260 }), true)
})
