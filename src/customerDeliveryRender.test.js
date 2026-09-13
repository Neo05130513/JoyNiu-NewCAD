import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

test('customer help and deletion receipts distinguish requests, local validation and actual manual handling', async () => {
  const directory = await mkdtemp(resolve('node_modules/.customer-delivery-'))
  const bundle = await rolldown({ input: { help: resolve('src/WorkspaceTools.jsx'), data: resolve('src/SupportDataRequest.jsx') }, external: ['react', 'react/jsx-runtime'], transform: { jsx: { runtime: 'automatic' } } })
  try {
    await bundle.write({ dir: directory, format: 'esm' })
    const { HelpWorkspace } = await import(pathToFileURL(resolve(directory, 'help.js')).href)
    const { DataRequestSummary, DataReceiptForm, DataRequestFields } = await import(pathToFileURL(resolve(directory, 'data.js')).href)
    const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
    const help = render(HelpWorkspace, { onNavigate: () => {}, onDiagnostics: () => {} })
    for (const phrase of ['10 元充值 1000 积分', '实际 token', '待补缴', '待补充、失败、取消和中断', '申请删除数据', 'STEP']) assert.ok(help.includes(phrase))
    assert.doesNotMatch(help, /关闭二维码|支付后可以等待自动查询|真实退款执行与支付渠道/)
    const ticket = { id: 'SUP-1', category: 'data_deletion', revision: 1, status: 'open', dataRequest: { scopes: ['models', 'backups'], scopeDescription: '本次任务和相关备份' }, dataReceipts: [] }
    assert.match(render(DataRequestSummary, { ticket }), /尚无人工处理回执，不能据此认定数据已删除/)
    const fields = render(DataRequestFields, { value: { scopes: [], scopeDescription: '', acknowledged: false }, onChange: () => {} })
    assert.doesNotMatch(fields, /checked=""/)
    assert.match(fields, /提交或关闭工单不会立即删除数据/)
    const recorded = { ...ticket, dataReceipts: [{ id: 'receipt-1', outcome: 'not_processed', handledScope: '仅核对', retainedScope: '原图保留', retentionPlan: '待确认具体期限', evidenceRef: 'REVIEW-01', authorName: '操作人', createdAt: '2026-09-12T00:00:00Z' }] }
    const summary = render(DataRequestSummary, { ticket: recorded })
    for (const phrase of ['本次未执行删除', '原图保留', '待确认具体期限', 'REVIEW-01']) assert.ok(summary.includes(phrase))
    const form = render(DataReceiptForm, { ticket, busy: false, onSubmit: () => assert.fail('render must not submit') })
    assert.match(form, /此操作只保存实际处理记录，不执行删除/)
    assert.match(form, /客户可见/)
    assert.match(form, /disabled="" type="submit"/)
  } finally { await bundle.close(); await rm(directory, { recursive: true, force: true }) }
})
