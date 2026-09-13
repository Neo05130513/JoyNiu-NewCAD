import test from 'node:test'
import assert from 'node:assert/strict'
import { api, API_BASE, resolveApiBase } from './api.js'

globalThis.window ??= { setTimeout, clearTimeout }

test('relative API configuration resolves against the public HTTP origin for requests and artifacts', () => {
  const origin = 'http://122.51.168.205'
  for (const configured of ['/api/v1', '/api/v1/', 'api/v1']) {
    const base = resolveApiBase(configured, origin)
    assert.equal(base, `${origin}/api/v1`)
    assert.equal(`${base}/cad-agent/run`, `${origin}/api/v1/cad-agent/run`)
    assert.equal(new URL('/api/v1/cad-agent/runs/test/1/artifacts/glb?access=signed-value', `${base}/`).href,
      `${origin}/api/v1/cad-agent/runs/test/1/artifacts/glb?access=signed-value`)
  }
})

test('API base preserves explicit servers and the existing Node localhost default', () => {
  assert.equal(resolveApiBase('https://cad.example.com:8443/api/v1/', 'http://122.51.168.205'), 'https://cad.example.com:8443/api/v1')
  assert.equal(API_BASE, 'http://localhost:8010/api/v1')
  assert.equal(resolveApiBase(undefined, undefined), 'http://localhost:8010/api/v1')
  assert.equal(resolveApiBase('/api/v1', undefined), 'http://localhost:8010/api/v1')
})

test('relative API configuration uses window.location.origin by default', (context) => {
  const previous = globalThis.window
  globalThis.window = { ...previous, location: { origin: 'http://122.51.168.205', pathname: '/projects/design' } }
  context.after(() => { globalThis.window = previous })
  assert.equal(resolveApiBase('/api/v1'), 'http://122.51.168.205/api/v1')
})

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

test('logout sends the current bearer even when the browser has no refresh cookie', async (context) => {
  const requests = []
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options })
    return new Response('{"ok":true}', { headers: { 'content-type': 'application/json' } })
  })
  await api.logout('current-session-token')
  assert.equal(requests[0].url, `${API_BASE}/auth/logout`)
  assert.equal(requests[0].options.headers.Authorization, 'Bearer current-session-token')
  assert.equal(requests[0].options.headers['X-JoyNiu-CSRF'], '1')
  assert.equal(requests[0].options.credentials, 'include')
  assert.equal(requests[0].options.body, '{}')
  await api.logout()
  assert.equal(requests[1].options.headers.Authorization, undefined)
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

test('request deadline remains active until a stalled response body finishes', async (context) => {
  let expire, headersArrived, clearCount = 0
  const reading = new Promise(resolve => { headersArrived = resolve })
  context.mock.method(globalThis.window, 'setTimeout', callback => { expire = callback; return 123 })
  context.mock.method(globalThis.window, 'clearTimeout', () => { clearCount++ })
  context.mock.method(globalThis, 'fetch', async (_url, { signal }) => ({
    ok: true, headers: new Headers({ 'content-type': 'application/json' }),
    json: () => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      headersArrived()
    }),
  }))
  const pending = api.health()
  await reading
  assert.equal(clearCount, 0)
  expire()
  await assert.rejects(pending, error => error.name === 'TimeoutError' && /服务响应超时/.test(error.message))
  assert.equal(clearCount, 1)
})

test('successful SSE events and results pass through unchanged', async (context) => {
  const result = { message: '完成', parameters: { material: '45# 钢' } }
  context.mock.method(globalThis, 'fetch', async () => new Response(`event: turn.result\ndata: ${JSON.stringify(result)}\n\n`, { headers: { 'content-type': 'text/event-stream' } }))
  assert.deepEqual(await api.aiConversationStream('test', []), result)
})
