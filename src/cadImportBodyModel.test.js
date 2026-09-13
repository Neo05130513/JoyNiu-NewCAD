import test from 'node:test'
import assert from 'node:assert/strict'
import { insertImportedBody } from './cadImportBodyModel.js'
import { createCadImportClient } from './cadImportClient.js'

const asset = { id: 'asset_' + 'a'.repeat(32), sha256: 'b'.repeat(64), name: '实际源体', inspection: { valid: true, solidCount: 1 } }
const empty = { version: 'cad-plan-v1', units: 'mm', parameters: {}, features: [], result: '' }

test('import creates an opaque source body with editable placement, keeps prior result and never invents geometry', () => {
  const old = { ...empty, parameters: { width: { value: 20 } }, features: [{ id: 'old', op: 'box', size: ['width', 16, 10] }], result: 'old' }
  const before = structuredClone(old)
  const { plan } = insertImportedBody(old, asset, { instanceId: 'placed', position: [40.5, -2, 3] })
  assert.deepEqual(old, before)
  assert.deepEqual(plan.features[0], old.features[0])
  assert.deepEqual(plan.features[1], { id: 'import_placed', op: 'import_step', label: '实际源体', assetId: asset.id, sha256: asset.sha256 })
  assert.deepEqual(plan.features.at(-1).inputs, ['old', 'import_placed_position'])
  assert.deepEqual(plan.features.find(f => f.op === 'translate').vector.map(name => plan.parameters[name].value), [40.5, -2, 3])
  assert.equal(insertImportedBody(empty, asset, { instanceId: 'empty' }).plan.features.length, 2)
  assert.throws(() => insertImportedBody(plan, asset, { instanceId: 'placed' }), /冲突/)
  assert.throws(() => insertImportedBody(empty, { ...asset, inspection: { valid: true } }), /实体校验/)
  assert.throws(() => insertImportedBody(empty, asset, { position: [Infinity, 0, 0] }), /定位/)
  assert.throws(() => insertImportedBody({ ...old, result: 'missing' }, asset), /结果引用已失效/)
})

test('multipart import sends the real selected file with scoped credentials and forwards cancellation', async () => {
  const input = new File(['solid source\nendsolid source'], 'body.stl')
  const abort = new AbortController(); let received
  const client = createCadImportClient({ base: '/api/cad/designs/imports', fetchImpl: async (url, options) => { received = { url, options }; return { ok: true, json: async () => asset } } })
  assert.deepEqual(await client.upload(() => 'renewed-token', input, { name: '导入体', units: 'cm', requestId: 'fixed-request' }, abort.signal), asset)
  assert.equal(received.url, '/api/cad/designs/imports')
  assert.equal(received.options.headers.Authorization, 'Bearer renewed-token')
  assert.equal(received.options.body.get('file').name, 'body.stl')
  assert.equal(await received.options.body.get('file').text(), await input.text())
  assert.equal(received.options.body.get('units'), 'cm'); assert.equal(received.options.body.get('requestId'), 'fixed-request')
  let requestSignal
  const pending = createCadImportClient({ fetchImpl: (_url, options) => new Promise((_resolve, reject) => {
    requestSignal = options.signal; options.signal.addEventListener('abort', () => reject(new DOMException('已取消', 'AbortError')))
  }) })
  const result = pending.upload('token', input, { name: 'pending', requestId: 'same-request' }, abort.signal)
  abort.abort(); await assert.rejects(result, { name: 'AbortError' }); assert.equal(requestSignal.aborted, true)
  const denied = createCadImportClient({ fetchImpl: async () => ({ ok: false, status: 422, json: async () => ({ detail: { message: '网格不是闭合流形', code: 'open_mesh' } }) }) })
  await assert.rejects(denied.upload('token', input, { name: 'bad', requestId: 'bad-request' }), /不是闭合流形/)
})
