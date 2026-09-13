import test from 'node:test'
import assert from 'node:assert/strict'
import { createDeliveryIntentTracker, createDeliveryWorkspaceClient, deliveryAccountScope, deliveryCreatePayload, deliveryErrorMessage, deliveryFileSize, deliveryFileDownloadName, deliveryFileSourceIndex, deliveryIntentFingerprint, deliverySeedSelection, deliverySelectionState, deliverySourceKey, deliverySourceRef, deliveryVerificationLabel, readDeliveryDraft, writeDeliveryDraft } from './deliveryWorkspaceClient.js'

const source = (overrides = {}) => ({ kind: 'feature', id: 'part-a', revision: 3, name: '泵体', status: 'ready', eligible: true, ...overrides })
const payload = overrides => deliveryCreatePayload({ title: ' 首轮交付 ', notes: ' 客户核对 ', selected: [source()], ...overrides }, [source(), source({ id: 'part-b' })])

// State tests exercise the same helpers used by the form, not a separate UI model.
test('creation fixes explicit source versions, trims text and canonicalizes selection order', () => {
  const a = payload({ selected: [source({ id: 'part-b' }), source()] })
  const b = payload({ selected: [source(), source({ id: 'part-b' })] })
  assert.deepEqual(a, b)
  assert.deepEqual(a, { title: '首轮交付', notes: '客户核对', sourceRefs: [{ kind: 'feature', id: 'part-a', revision: 3 }, { kind: 'feature', id: 'part-b', revision: 3 }] })
  assert.equal(deliveryIntentFingerprint(a), deliveryIntentFingerprint(b))
  assert.deepEqual(deliverySourceRef(source({ kind: 'engineering', revision: null })), { kind: 'engineering', id: 'part-a' })
})

test('ineligible, missing and stale revisions remain visible and block creation', () => {
  const unavailable = source({ eligible: false, reason: '尚未完成人工确认。' })
  const state = deliverySelectionState([source(), source({ id: 'removed' }), source({ revision: 2 })], [unavailable])
  assert.equal(state.length, 3)
  assert.equal(state[0].reason, '尚未完成人工确认。')
  assert.equal(state[1].eligible, false)
  assert.equal(state[2].eligible, false)
  assert.notEqual(deliverySourceKey(source()), deliverySourceKey(source({ revision: 2 })))
  assert.throws(() => deliveryCreatePayload({ title: 'test', selected: [source()] }, [unavailable]), /尚未完成人工确认/)
  assert.throws(() => deliveryCreatePayload({ title: 'test', selected: [source({ revision: 2 })] }, [source()]), /此版本已不在可选来源中/)
  assert.throws(() => deliveryCreatePayload({ title: 'test', selected: [source()] }, [source({ eligible: 'true' })]), /当前不可打包/)
})

test('user input respects backend limits without truncating the intended delivery', () => {
  assert.throws(() => payload({ title: '  ' }), /填写交付包名称/)
  assert.throws(() => payload({ title: 'a'.repeat(181) }), /180/)
  assert.throws(() => payload({ notes: 'a'.repeat(4001) }), /4000/)
  assert.throws(() => payload({ selected: [] }), /至少选择/)
  assert.throws(() => payload({ selected: Array.from({ length: 21 }, (_, i) => source({ id: String(i) })) }), /20/)
  assert.equal(payload({ title: 'a'.repeat(180), notes: 'a'.repeat(4000) }).notes.length, 4000)
})

test('logical retry keeps its request ID across reload and credential renewal; deliberate new package gets a new ID', () => {
  let number = 0
  const makeId = () => `request-${++number}`
  const first = createDeliveryIntentTracker(null, makeId)
  const original = first.get(payload())
  assert.equal(first.get(payload()), original)
  const restored = createDeliveryIntentTracker(JSON.parse(JSON.stringify(first.snapshot())), makeId)
  assert.equal(restored.get(payload()), original)
  assert.notEqual(restored.get(payload({ notes: '新要求' })), original)
  restored.clear()
  assert.notEqual(restored.get(payload()), original)
  assert.equal(deliveryAccountScope('owner-1', 'old-token'), deliveryAccountScope('owner-1', 'renewed-token'))
  assert.notEqual(deliveryAccountScope('owner-1', 'token'), deliveryAccountScope('owner-2', 'token'))
})

test('draft storage isolates accounts and never persists a credential or unrelated fields', () => {
  const values = new Map()
  const storage = { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) }
  const draft = { title: '泵体交付', notes: '初稿', selected: [source()], intent: { fingerprint: 'payload', requestId: 'request-1' }, token: 'secret-token' }
  assert.equal(writeDeliveryDraft(storage, 'owner-1', draft), true)
  assert.deepEqual(readDeliveryDraft(storage, 'owner-1').selected, [{ kind: 'feature', id: 'part-a', revision: 3 }])
  assert.equal(readDeliveryDraft(storage, 'owner-2').title, '')
  assert.doesNotMatch([...values.entries()].flat().join(''), /secret-token/)
  assert.equal(writeDeliveryDraft(storage, undefined, draft), false)
  assert.equal(values.size, 1)
  assert.equal(readDeliveryDraft({ getItem() { throw new Error('blocked') } }, 'owner-1').title, '')
  assert.equal(writeDeliveryDraft({ setItem() { throw new Error('full') } }, 'owner-1', draft), false)
})

test('initial source selection accepts only eligible matching versions and reports missing sources', () => {
  const result = deliverySeedSelection([source(), source({ revision: 1 }), source({ id: 'blocked' }), source({ id: 'missing' })], [source(), source({ id: 'blocked', eligible: false, reason: '仍待确认' })])
  assert.deepEqual(result.selected, [{ kind: 'feature', id: 'part-a', revision: 3 }])
  assert.equal(result.unavailable.length, 3)
  assert.match(result.unavailable.join(' '), /仍待确认/)
  assert.equal(deliverySeedSelection({ kind: 'feature', id: 'part-a' }, [source()]).selected[0].revision, 3)
})

test('confirmed AI sources never imply delivery acceptance or manufacturing clearance', () => {
  assert.equal(deliveryVerificationLabel('confirmed_source'), '来源已确认 · 包待验收')
  for (const status of ['requires_review', 'unknown', undefined, 'confirmed']) assert.equal(deliveryVerificationLabel(status), '待人工验收')
  assert.equal(deliveryFileSize(0), '0 B')
  assert.equal(deliveryFileSize(2048), '2.0 KB')
  assert.equal(deliveryFileSize(2 * 1024 ** 2), '2.0 MB')
  assert.equal(deliveryFileSize(undefined), '大小待核对')
  assert.equal(deliveryFileSize(null), '大小待核对')
})

test('all API operations use the delivery route and latest Bearer credential, with opaque IDs escaped', async () => {
  const calls = []
  let credential = 'old-token'
  const client = createDeliveryWorkspaceClient({ apiBase: 'https://example.test/api/v1', fetchImpl: async (url, options) => {
    calls.push({ url, options })
    return options.headers.Accept === 'application/octet-stream' ? new Response('binary') : Response.json({ items: [], id: 'package-1', title: '首轮交付', sources: [], files: [], verification: { status: 'requires_review' } })
  } })
  await client.sources(() => credential)
  credential = 'renewed-token'
  await client.list(() => credential)
  await client.create(() => credential, payload(), 'request-123')
  await client.detail(() => credential, 'package/1')
  assert.equal(await (await client.archive(() => credential, 'package/1')).text(), 'binary')
  assert.equal(await (await client.file(() => credential, 'package/1', 'file#1')).text(), 'binary')
  assert.deepEqual(calls.map(call => call.url), [
    'https://example.test/api/cad/deliveries/sources', 'https://example.test/api/cad/deliveries', 'https://example.test/api/cad/deliveries',
    'https://example.test/api/cad/deliveries/package%2F1', 'https://example.test/api/cad/deliveries/package%2F1/archive', 'https://example.test/api/cad/deliveries/package%2F1/files/file%231',
  ])
  assert.equal(calls[0].options.headers.Authorization, 'Bearer old-token')
  assert.equal(calls[1].options.headers.Authorization, 'Bearer renewed-token')
  for (const { url, options } of calls) { assert.doesNotMatch(url, /token|Bearer/); assert.equal(options.cache, 'no-store'); assert.equal(options.credentials, 'include') }
  assert.equal(calls[2].options.method, 'POST')
  assert.deepEqual(JSON.parse(calls[2].options.body), { requestId: 'request-123', ...payload() })
})

test('missing session is rejected before any network call', async () => {
  const client = createDeliveryWorkspaceClient({ fetchImpl() { throw new Error('unexpected request') } })
  await assert.rejects(client.sources(() => ''), error => error.status === 401 && /请先登录/.test(error.message))
})

test('server errors retain code and clear detail for JSON and binary downloads', async () => {
  const client = createDeliveryWorkspaceClient({ fetchImpl: async () => Response.json({ detail: { code: 'SOURCE_REVISION_CHANGED', message: '来源版本已更新，请重新选择。' } }, { status: 409 }) })
  for (const operation of [() => client.create('token', payload(), 'request-123'), () => client.file('token', 'pack', 'file')]) {
    await assert.rejects(operation(), error => error.status === 409 && error.code === 'SOURCE_REVISION_CHANGED' && /来源版本已更新/.test(error.message))
  }
  const expired = Object.assign(new Error('expired'), { status: 401 })
  assert.match(deliveryErrorMessage(expired), /草稿已保留/)
  assert.match(deliveryErrorMessage(new Error('Failed to fetch')), /刷新已保存清单确认结果/)
})

test('invalid response does not claim success and asks the user to check saved packages', async () => {
  const client = createDeliveryWorkspaceClient({ fetchImpl: async () => new Response('<html>gateway</html>') })
  await assert.rejects(client.create('token', payload(), 'request-123'), /刷新已保存清单确认创建结果/)
})

test('abort cancels the request and timeout explains the uncertain creation outcome', async () => {
  const pending = async (_url, { signal }) => await new Promise((resolve, reject) => {
    const abort = () => reject(Object.assign(new Error('Aborted'), { name: 'AbortError' }))
    if (signal.aborted) abort()
    else signal.addEventListener('abort', abort, { once: true })
  })
  const client = createDeliveryWorkspaceClient({ timeoutMs: 1000, fetchImpl: pending })
  const controller = new AbortController()
  const operation = client.list('token', controller.signal)
  controller.abort()
  await assert.rejects(operation, error => error.name === 'AbortError')
  const timed = createDeliveryWorkspaceClient({ timeoutMs: 5, fetchImpl: pending })
  await assert.rejects(timed.create('token', payload(), 'request-123'), /请求编号已保留/)
})

test('timeout remains active while downloading the response body', async () => {
  const client = createDeliveryWorkspaceClient({ timeoutMs: 5, fetchImpl: async (_url, { signal }) => ({ ok: true, blob: () => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('Aborted')), { once: true })) }) })
  await assert.rejects(client.archive('token', 'pack'), /交付请求超时/)
})

test('malformed successful JSON cannot be presented as a sealed package', async () => {
  const client = createDeliveryWorkspaceClient({ fetchImpl: async () => Response.json({}) })
  await assert.rejects(client.create('token', payload(), 'request-123'), /交付清单内容不完整/)
  await assert.rejects(client.detail('token', 'pack-1'), /交付清单内容不完整/)
})


test('single-file download names include source and version, handle collisions and retain safe extensions', () => {
  const files = [{ id: 'file_111111111111111111111111', name: 'model.step', sha256: 'unchanged-a' }, { id: 'file_222222222222222222222222', name: 'model.step', sha256: 'unchanged-b' }]
  const detail = { id: 'delivery_12345678', sources: [{ kind: 'feature', id: 'part-a', revision: 5, name: '../../齿轮:设计', fileIds: [files[0].id] }, { kind: 'feature', id: 'part-b', revision: 5, name: '../../齿轮:设计', fileIds: [files[1].id] }], files }
  const before = JSON.stringify(detail)
  const index = deliveryFileSourceIndex(detail.sources)
  assert.equal(index.get(files[0].id)[0].id, 'part-a')
  const names = files.map(file => deliveryFileDownloadName(detail, file))
  assert.equal(new Set(names).size, 2)
  for (const name of names) { assert.match(name, /齿轮-设计-特征设计-v5_/); assert.match(name, /\.step$/); assert.doesNotMatch(name, /[\\/:*?"<>|\x00-\x1f]/) }
  assert.equal(JSON.stringify(detail), before)
  const long = { ...detail, sources: [{ ...detail.sources[0], name: '长'*180 }] }
  assert.ok(new TextEncoder().encode(deliveryFileDownloadName(long, files[0])).length <= 240)
  assert.match(deliveryFileDownloadName(long, files[0]), /特征设计-v5_/)
  const casing = { ...detail, sources: detail.sources.map((item, i) => ({ ...item, name: i ? 'Part' : 'PART' })) }
  assert.notEqual(deliveryFileDownloadName(casing, files[0]).toLowerCase(), deliveryFileDownloadName(casing, files[1]).toLowerCase())
})
