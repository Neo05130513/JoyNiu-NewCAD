import test from 'node:test'
import assert from 'node:assert/strict'
import { API_BASE } from './api.js'
import { downloadSourceFile, latestSourceFileRun, refreshProjectionFile, refreshSourceFileRun } from './sourceFileAccess.js'

const run = (patch = {}) => ({ runId: 'cad_saved', revision: 2, fileLinksEpoch: 0,
  fileLinksExpiresAt: '2026-09-10T03:00:00Z', fileLinksAvailable: true,
  sourceDocuments: [{ id: 'source-0', filename: '客户原图.dwg', downloadUrl: `${API_BASE}/cad-agent/runs/cad_saved/2/sources/0/download?access=fresh-signature` }], ...patch })
const scope = { runId: 'cad_saved', revision: 2, token: 'fixture-token' }

test('parent renewals replace cached drawing URLs but not a different run or revision', () => {
  const cached = run(), refreshed = run({ fileLinksExpiresAt: '2026-09-10T04:00:00Z' })
  assert.equal(latestSourceFileRun(refreshed, cached), refreshed)
  assert.equal(latestSourceFileRun(cached, refreshed), refreshed)
  const revoked = run({ fileLinksEpoch: 1, fileLinksExpiresAt: '2026-09-10T02:00:00Z' })
  assert.equal(latestSourceFileRun(cached, refreshed, revoked), revoked)
  assert.equal(latestSourceFileRun(cached, run({ runId: 'other', fileLinksEpoch: 10 }), run({ revision: 3, fileLinksEpoch: 10 })), cached)
  assert.equal(latestSourceFileRun(undefined), undefined)
  assert.equal(latestSourceFileRun(run({ fileLinksExpiresAt: 2000000000 }), run({ fileLinksExpiresAt: 1000000000 })).fileLinksExpiresAt, 2000000000)
})

test('source download renews authenticated run access and binds the exact original file', async () => {
  const steps = [], result = run()
  const saved = await downloadSourceFile({ ...scope, documentId: 'source-0', token: () => 'current-token',
    getRun: async options => { steps.push('get'); assert.equal(options.runId, scope.runId); return result },
    fetcher: async (url, options) => {
      steps.push('download'); assert.equal(url, result.sourceDocuments[0].downloadUrl)
      assert.equal(options.headers.Authorization, 'Bearer current-token')
      assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error')
      return new Response('saved DWG bytes')
    } })
  assert.deepEqual(steps, ['get', 'download'])
  assert.equal(saved.run, result); assert.equal(saved.filename, '客户原图.dwg'); assert.equal(await saved.blob.text(), 'saved DWG bytes')
})

test('foreign, missing, revoked or mismatched resources are refused before credentials or bytes are fetched', async () => {
  let fetches = 0
  const fetcher = async () => { fetches++; return new Response('must not download') }
  for (const result of [run({ runId: 'other' }), run({ revision: 3 }), run({ fileLinksAvailable: false }), run({ sourceDocuments: [] }),
    ...['https://untrusted.example/file', `${API_BASE}/cad-agent/runs/other/2/sources/0/download`,
      `${API_BASE}/cad-agent/runs/cad_saved/3/sources/0/download`, `${API_BASE}/cad-agent/runs/cad_saved/2/sources/1/download`,
      'javascript:alert(1)'].map(downloadUrl => run({ sourceDocuments: [{ id: 'source-0', downloadUrl }] }))]) {
    await assert.rejects(downloadSourceFile({ ...scope, documentId: 'source-0', getRun: async () => result, fetcher }))
  }
  await assert.rejects(downloadSourceFile({ ...scope, documentId: 'local-0', getRun: async () => run(), fetcher }))
  assert.equal(fetches, 0)
})

test('expired or denied file access is a recoverable download error, not a saved file', async () => {
  for (const status of [401, 403, 404, 500]) await assert.rejects(downloadSourceFile({ ...scope,
    documentId: 'source-0', getRun: async () => run(), fetcher: async () => new Response('error', { status }) }),
  status === 401 || status === 403 ? /重新登录/ : /下载失败/)
})

test('leaving a project cancels renewal and prevents late bytes from becoming a download', async () => {
  const before = new AbortController(); before.abort()
  await assert.rejects(refreshSourceFileRun({ ...scope, signal: before.signal, getRun: async () => { assert.fail('aborted renewal must not start') } }), { name: 'AbortError' })
  const during = new AbortController()
  await assert.rejects(downloadSourceFile({ ...scope, documentId: 'source-0', signal: during.signal,
    getRun: async () => run(), fetcher: async () => { during.abort(); return new Response('late result') } }), { name: 'AbortError' })
})

test('projection renewal returns only the requested current-version SVG or PNG', async () => {
  const artifact = { id: 'view-front', format: 'svg', url: `${API_BASE}/cad-agent/runs/cad_saved/2/artifacts/view-front?access=renewed` }
  assert.equal(await refreshProjectionFile({ ...scope, artifactId: 'view-front', getRun: async () => run({ artifacts: [artifact] }) }), artifact)
  for (const artifacts of [[], [{ ...artifact, id: 'view-top' }], [{ ...artifact, format: 'step' }],
    [{ ...artifact, url: `${API_BASE}/cad-agent/runs/cad_saved/3/artifacts/view-front` }],
    [{ ...artifact, url: `${API_BASE}/cad-agent/runs/cad_saved/2/artifacts/view-top` }],
    [{ ...artifact, url: 'https://untrusted.example/view.svg' }]]) {
    await assert.rejects(refreshProjectionFile({ ...scope, artifactId: 'view-front', getRun: async () => run({ artifacts }) }))
  }
})
