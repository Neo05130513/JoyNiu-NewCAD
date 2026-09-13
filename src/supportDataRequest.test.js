import test from 'node:test'
import assert from 'node:assert/strict'
import { dataRequestPayload, dataReceiptPayload, emptyDataRequest } from './supportDataRequest.js'
import { supportCreatePayload, emptySupportDraft, createSupportClient } from './supportClient.js'

test('data deletion requires explicit scopes and acknowledgement without granting drawing access', () => {
  assert.equal(emptyDataRequest().acknowledged, false)
  assert.throws(() => dataRequestPayload(emptyDataRequest()), /范围/)
  const draft = { ...emptySupportDraft(), subject: '删除申请', body: '先核对', category: 'data_deletion', dataRequest: { scopes: ['models', 'backups'], scopeDescription: '指定任务及备份', acknowledged: true } }
  const payload = supportCreatePayload(draft)
  assert.equal(payload.drawingConsent, false)
  assert.deepEqual(payload.dataRequest.scopes, ['backups', 'models'])
  assert.throws(() => supportCreatePayload({ ...draft, dataRequest: { ...draft.dataRequest, acknowledged: false } }), /不会立即删除/)
  assert.throws(() => dataRequestPayload({ ...draft.dataRequest, scopes: ['models', 'models'] }), /范围/)
})

test('manual outcome receipt requires evidence and records only to the authenticated admin endpoint', async () => {
  const ticket = { category: 'data_deletion', revision: 4 }
  const draft = { outcome: 'not_processed', handledScope: '仅核对', retainedScope: '全部保留', retentionPlan: '等待确认', evidenceRef: 'REVIEW-001', confirmed: true }
  assert.throws(() => dataReceiptPayload(ticket, { ...draft, evidenceRef: '' }), /记录编号/)
  assert.throws(() => dataReceiptPayload(ticket, { ...draft, confirmed: false }), /确认/)
  const calls = []
  const client = createSupportClient({ apiBase: '/api/v1', fetchImpl: async (url, options) => { calls.push([url, options]); return { ok: true, json: async () => ({ id: 'SUP-1' }) } } })
  await client.recordDataReceipt('local-token', 'SUP-1', dataReceiptPayload(ticket, draft), 'same-intent')
  assert.equal(calls[0][0], '/api/v1/support/admin/tickets/SUP-1/data-receipts')
  assert.equal(JSON.parse(calls[0][1].body).revision, 4)
  assert.equal(JSON.parse(calls[0][1].body).idempotencyKey, 'same-intent')
  assert.equal(calls.length, 1)
})
