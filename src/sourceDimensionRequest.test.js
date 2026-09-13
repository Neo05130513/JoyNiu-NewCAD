import test from 'node:test'
import assert from 'node:assert/strict'
import { awaitSourceDimensionLocations } from './sourceDimensionRequest.js'

const never = () => new Promise(() => {})
const final = (overrides = {}) => ({ version: 'cad-source-locations-v1', runId: 'cad_test', revision: 1,
  operationId: 'current-operation', status: 'succeeded', annotations: [{ id: 'annotation-1', text: '30' }], unlocatedAnnotationIds: [], ...overrides })
const options = (overrides = {}) => ({ runId: 'cad_test', revision: 1, operationId: 'current-operation',
  start: never, read: never, timeoutMs: 300, pollIntervalMs: 5, readTimeoutMs: 10, tickMs: 5, ...overrides })

test('a hanging POST recovers the completed result through a read without starting another operation', async () => {
  let starts = 0, reads = 0, postSignal
  const result = await awaitSourceDimensionLocations(options({
    start: ({ signal }) => { starts++; postSignal = signal; return never() },
    read: async () => { reads++; return { sourceDimensionLocations: final() } },
  }))
  assert.equal(result.status, 'succeeded')
  assert.equal(starts, 1)
  assert.equal(reads, 1)
  assert.equal(postSignal.aborted, true)
})

test('partial progress displays completed boxes and a dropped POST recovers the final result', async () => {
  let reads = 0
  const progress = []
  const result = await awaitSourceDimensionLocations(options({
    start: async () => { throw new TypeError('Failed to fetch') },
    read: async () => ({ sourceDimensionLocationProgress: ++reads === 1 ? final({ status: 'running', completedAnnotationCount: 1, totalAnnotationCount: 3 }) : final() }),
    onProgress: (value) => progress.push(value),
  }))
  assert.equal(result.status, 'succeeded')
  assert.ok(progress.some((value) => value.stage === 'recovering'))
  assert.ok(progress.some((value) => value.status === 'running' && value.annotations?.length === 1))
})

test('force polling ignores a previous result and only finishes for its own operation', async () => {
  let reads = 0
  const result = await awaitSourceDimensionLocations(options({ force: true, read: async () => {
    reads++
    return { sourceDimensionLocations: final({ operationId: 'old-operation' }),
      sourceDimensionLocationProgress: reads < 3 ? undefined : final({ status: 'partial' }) }
  } }))
  assert.equal(reads, 3)
  assert.equal(result.status, 'partial')
  assert.equal(result.operationId, 'current-operation')
})

test('a hanging POST and read end at the overall deadline even if they ignore AbortSignal', async () => {
  const before = Date.now()
  await assert.rejects(awaitSourceDimensionLocations(options({ timeoutMs: 35, readTimeoutMs: 500 })), { code: 'source_location_timeout' })
  assert.ok(Date.now() - before < 250)
})

test('leaving the page cancels pending work and late responses cannot update another model', async () => {
  const controller = new AbortController(), events = []
  let finishPost
  const result = awaitSourceDimensionLocations(options({ signal: controller.signal,
    start: () => new Promise((resolve) => { finishPost = resolve }), onProgress: (value) => events.push(value) }))
  await Promise.resolve()
  controller.abort()
  await assert.rejects(result, { name: 'AbortError' })
  const count = events.length
  finishPost(final())
  await new Promise((resolve) => setTimeout(resolve, 15))
  assert.equal(events.length, count)
})

test('a late POST error does not erase success obtained from the server cache', async () => {
  let failPost
  const result = await awaitSourceDimensionLocations(options({
    start: () => new Promise((_, reject) => { failPost = reject }),
    read: async () => ({ sourceDimensionLocations: final() }),
  }))
  failPost(new Error('Late disconnect'))
  await Promise.resolve()
  assert.equal(result.status, 'succeeded')
})

test('an inaccessible run fails immediately without polling or accepting another run cache', async () => {
  let reads = 0
  await assert.rejects(awaitSourceDimensionLocations(options({
    start: async () => { throw Object.assign(new Error('Not found'), { status: 404 }) },
    read: async () => { reads++; return { sourceDimensionLocations: final() } },
  })), { status: 404 })
  assert.equal(reads, 0)
  await assert.rejects(awaitSourceDimensionLocations(options({ timeoutMs: 30,
    read: async () => ({ sourceDimensionLocations: final({ runId: 'different-run' }) }),
  })), { code: 'source_location_timeout' })
})
