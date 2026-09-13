import test from 'node:test'
import assert from 'node:assert/strict'
import { cadDeliveryReportAvailability, cadDeliveryReportData, createCadDeliveryReport } from './cadDeliveryReport.js'
import { cadGenerationFromResult, cadModelFromResult, cadPlanSignature } from './cadAgentState.js'

function confirmed() {
  const run = { runId: 'cad-report-test', revision: 2, status: 'ready', confirmedAt: '2026-09-10T10:30:00Z', confirmedBy: { id: 'designer-1', displayName: '确认工程师' },
    plan: { name: '支座', units: 'mm', material: 'AL6061', parameters: { totalHeight: { value: 40, source: { view: 'front', text: '40', pageIndex: 0 } }, slotAngle: { value: 30 }, halfWidth: { expression: 'totalHeight / 2', value: null }, count: { value: 0 } }, features: [{ id: 'body', op: 'box', size: [20, 30, 'totalHeight'] }], result: 'body' },
    resolvedParameters: { halfWidth: 20 }, questions: ['请核对表面处理要求'], sourceFiles: [{ filename: '原始图纸.pdf', contentType: 'application/pdf', sha256: 'drawing-sha', token: 'must-not-export' }],
    inspection: { valid: true, kernelBacked: true, engine: 'cadquery-occt', solidCount: 1, volumeMm3: 24000, bbox: { size: [20, 30, 40] }, acceptance: { status: 'needs_input', checks: [
      { id: 'height', label: '总体高度', kind: 'bbox_size', expected: 40, actual: 40, passed: true, source: { text: '主视图总高40' } },
      { id: 'hole', label: '局部孔径', kind: 'cylinder', expected: 5, actual: null, passed: null },
    ] } },
    projectionComparison: { views: [{ view: 'front', status: 'supported', reliability: 'high' }, { view: 'top', status: 'not_checked' }] },
    artifacts: [{ format: 'glb', url: '/private/model?access=scoped-download-token' }, { format: 'step', productionReady: true, url: '/private/model?access=scoped-download-token' }],
    access_token: 'login-secret', refreshToken: 'refresh-secret',
  }
  const model = cadModelFromResult(run)
  return { model, generation: cadGenerationFromResult(run, model.cadPlan), exportedAt: '2026-09-10T11:30:00Z' }
}

test('default report requires the current confirmed model and actual delivery generation', () => {
  const value = confirmed()
  assert.equal(cadDeliveryReportAvailability(value).allowed, true)
  for (const status of ['queued', 'running', 'review_required', 'needs_input', 'failed', 'cancelled', 'interrupted']) {
    const options = structuredClone(value); options.model.agentRun.status = status
    assert.throws(() => createCadDeliveryReport(options), /尚未确认/)
  }
  for (const mutate of [
    options => { options.model.agentRun.dirty = true }, options => { options.model.agentRun.stale = true },
    options => { options.generation.stale = true }, options => { options.model.agentRun.deliveryBlockedReason = '检查缺失' },
    options => { options.generation.revision = 1 }, options => { options.generation.planSignature = 'wrong' },
    options => { options.generation.validation.productionReady = false }, options => { options.generation.artifacts = [] },
    options => { options.drawingJob = { cadTask: { requestId: 'accepted-without-run-id' } } },
  ]) { const options = structuredClone(value); mutate(options); assert.equal(cadDeliveryReportAvailability(options).allowed, false); assert.throws(() => createCadDeliveryReport(options)) }
})

test('parameter rows distinguish nominal values, server-resolved derived dimensions and units', () => {
  const report = cadDeliveryReportData(confirmed())
  assert.deepEqual(report.parameters.map(row => [row.label, row.value, row.unit, row.kind]), [['总高', 40, 'mm', '名义值'], ['槽倾角', 30, '°', '名义值'], ['半宽', 20, 'mm', '派生值'], ['数量', 0, '无量纲', '名义值']])
  assert.match(report.parameters[0].source, /主视图.*第 1 页.*40/)
  assert.equal(report.parameters[2].expression, 'totalHeight / 2')
  assert.equal(report.sources[0].sha256, 'drawing-sha')
  assert.equal(report.confirmedBy, '确认工程师')
  assert.equal(report.confirmedAt, '2026-09-10T10:30:00Z')
  assert.equal(report.exportedAt, '2026-09-10T11:30:00Z')
})

test('missing and stale derived values are never calculated in the browser or replaced with zero', () => {
  const options = confirmed(); options.model.agentRun.dirty = true
  options.model.cadPlan.parameters.totalHeight.value = null
  const report = cadDeliveryReportData({ ...options, candidate: true })
  assert.equal(report.parameters[0].value, null)
  assert.equal(report.parameters[2].value, null)
  const { html, filename } = createCadDeliveryReport({ ...options, candidate: true })
  assert.match(html, /候选核对报告 · 尚未确认交付/)
  assert.match(html, /待计算 \/ 未记录/)
  assert.match(filename, /候选/)
})

test('a check is measured only when an actual numeric geometry measurement was recorded', () => {
  const options = confirmed()
  options.model.agentRun.inspection.acceptance.checks.push({ label: '错误的通过声明', expected: 10, passed: true, actual: null })
  const report = cadDeliveryReportData(options)
  assert.equal(report.checks[0].status, '已测，符合记录依据')
  assert.equal(report.checks[1].measured, false)
  assert.equal(report.checks[2].status, '未取得实测值')
  const { html } = createCadDeliveryReport(options)
  assert.match(html, /已取得实测值的要求（1 项）/)
  assert.match(html, /尚未取得实测值（2 项）/)
  assert.match(html, /实测结论仅覆盖表内记录/)
  assert.match(html, /不是制造合格证明或加工放行文件/)
  assert.match(html, /表面处理要求/)
})

test('missing historical identity is not replaced by the exporter or current time', () => {
  const options = confirmed(); delete options.model.agentRun.confirmedBy; delete options.model.agentRun.confirmedAt
  const report = cadDeliveryReportData(options)
  assert.equal(report.confirmedBy, '未记录')
  assert.equal(report.confirmedAt, null)
  assert.notEqual(report.confirmedAt, report.exportedAt)
})

test('the self-contained HTML escapes untrusted content and omits session and download secrets', () => {
  const options = confirmed()
  options.model.name = '支座<script>alert("x")</script>'
  options.model.cadPlan.parameters.totalHeight.source.text = '<img src=x onerror=alert(1)>'
  options.generation.planSignature = cadPlanSignature(options.model.cadPlan)
  const { html, filename } = createCadDeliveryReport(options)
  assert.match(html, /&lt;script&gt;/)
  assert.match(html, /&lt;img src=x/)
  assert.doesNotMatch(html, /<script|<img|<link|<iframe|login-secret|refresh-secret|scoped-download-token|must-not-export/)
  assert.match(html, /Content-Security-Policy/)
  assert.doesNotMatch(filename, /[<>/]/)
  assert.match(filename, /已确认核对报告.html$/)
})
