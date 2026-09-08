import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from './api.js'

globalThis.window ??= { setTimeout, clearTimeout }

test('API authentication errors are actionable Chinese while preserving status and payload', async (context) => {
  const payload = { detail: 'bearer token is required' }
  context.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(payload), { status: 401, headers: { 'content-type': 'application/json' } }))
  await assert.rejects(api.health(), (error) => {
    assert.equal(error.message, '请先登录后再继续此操作。')
    assert.equal(error.status, 401)
    assert.deepEqual(error.payload, payload)
    return true
  })
})

test('FastAPI detail arrays expose field messages rather than [object Object]', async (context) => {
  const payload = { detail: [{ loc: ['body', 'email'], msg: 'Field required' }, { loc: ['body', 'password'], msg: 'String should have at least 8 characters' }] }
  context.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(payload), { status: 422, headers: { 'content-type': 'application/json' } }))
  await assert.rejects(api.login('', ''), (error) => {
    assert.equal(error.message, '邮箱：此项必填；密码：至少需要 8 个字符')
    assert.equal(error.status, 422)
    assert.deepEqual(error.payload, payload)
    return true
  })
})

test('SSE turn.error localizes both emitted event and thrown error, retaining original payload', async (context) => {
  const payload = { message: 'permission denied', status: 403, code: 'permission_denied' }
  context.mock.method(globalThis, 'fetch', async () => new Response(`event: turn.error\ndata: ${JSON.stringify(payload)}\n\n`, { headers: { 'content-type': 'text/event-stream' } }))
  const events = []
  await assert.rejects(api.aiConversationStream('test', [], {}, [], '', '', (name, event) => events.push([name, event])), (error) => {
    assert.match(error.message, /当前账号没有/)
    assert.equal(error.status, 403)
    assert.deepEqual(error.payload, payload)
    return true
  })
  assert.equal(events[0][0], 'turn.error')
  assert.match(events[0][1].message, /当前账号没有/)
  assert.equal(events[0][1].code, 'permission_denied')
})

test('network failure becomes readable without changing cancellation identity', async (context) => {
  const abort = new DOMException('The operation was aborted.', 'AbortError')
  const mocked = context.mock.method(globalThis, 'fetch', async () => { throw abort })
  await assert.rejects(api.health(), (error) => error === abort && error.name === 'AbortError')
  mocked.mock.mockImplementation(async () => { throw new TypeError('Failed to fetch') })
  await assert.rejects(api.health(), (error) => /无法连接服务/.test(error.message) && error.name === 'TypeError')
})

test('successful SSE events and results pass through unchanged', async (context) => {
  const result = { message: '完成', parameters: { material: '45# 钢' } }
  context.mock.method(globalThis, 'fetch', async () => new Response(`event: turn.result\ndata: ${JSON.stringify(result)}\n\n`, { headers: { 'content-type': 'text/event-stream' } }))
  assert.deepEqual(await api.aiConversationStream('test', []), result)
})
