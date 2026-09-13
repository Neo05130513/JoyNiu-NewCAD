import test from 'node:test'
import assert from 'node:assert/strict'
import { createClientId } from './clientId.js'
import { cadAgent } from './cadAgentClient.js'
import { createProject, createProjectFile, saveFileVersion, getActiveFile } from './projectStore.js'
import { createPartInstance, standardParts } from './assemblyModel.js'

const uuidPattern = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/
function httpCrypto(context) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  let calls = 0
  const value = { getRandomValues(bytes) { assert.equal(this, value); bytes.fill(++calls); return bytes } }
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value })
  context.after(() => {
    if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor)
    else delete globalThis.crypto
  })
  return { calls: () => calls }
}

test('client IDs prefer native UUID and preserve the Crypto method receiver', () => {
  const cryptoApi = {
    randomUUID() { assert.equal(this, cryptoApi); return '12345678-1234-4234-8234-123456789abc' },
    getRandomValues() { assert.fail('native UUID should be preferred') },
  }
  assert.equal(createClientId(cryptoApi), '12345678-1234-4234-8234-123456789abc')
})

test('HTTP without randomUUID generates valid UUIDs from getRandomValues', (context) => {
  const crypto = httpCrypto(context)
  const first = createClientId(), second = createClientId()
  assert.match(first, uuidPattern)
  assert.match(second, uuidPattern)
  assert.notEqual(first, second)
  assert.equal(crypto.calls(), 2)
})

test('legacy clients without Web Crypto retain distinct non-secret local IDs in the same millisecond', (context) => {
  context.mock.method(Date, 'now', () => 1234567890)
  context.mock.method(Math, 'random', () => 0.5)
  const first = createClientId(null), second = createClientId(null)
  assert.notEqual(first, second)
  assert.match(first, /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/)
})

test('project, file, version and assembly creation work over HTTP without randomUUID', (context) => {
  httpCrypto(context)
  let store = createProject({ schemaVersion: 1, projects: [] }, { name: 'HTTP 项目' })
  const projectId = store.activeProjectId
  store = createProjectFile(store, projectId, { name: 'HTTP 文档', type: '文档' })
  store = saveFileVersion(store, projectId, store.activeFileId)
  const file = getActiveFile(store)
  assert.match(projectId.replace(/^project_/, ''), uuidPattern)
  assert.match(file.id.replace(/^file_/, ''), uuidPattern)
  assert.match(file.versions[0].id.replace(/^version_/, ''), uuidPattern)
  assert.match(createPartInstance(standardParts[0]).id, uuidPattern)
})

test('source dimension locating starts and completes over HTTP with a generated operation ID', async (context) => {
  const crypto = httpCrypto(context)
  let requestedOperation
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.match(url, /\/cad-agent\/runs\/cad_http\/source-locations$/)
    assert.equal(options.method, 'POST')
    requestedOperation = JSON.parse(options.body).operationId
    assert.match(requestedOperation, uuidPattern)
    return new Response(JSON.stringify({ version: 'cad-source-locations-v1', runId: 'cad_http', revision: 1,
      operationId: requestedOperation, status: 'succeeded', annotations: [], unlocatedAnnotationIds: [] }), { headers: { 'Content-Type': 'application/json' } })
  })
  const result = await cadAgent.waitForSourceDimensions({ runId: 'cad_http', revision: 1 })
  assert.equal(result.status, 'succeeded')
  assert.equal(result.operationId, requestedOperation)
  assert.equal(crypto.calls(), 1)
})
