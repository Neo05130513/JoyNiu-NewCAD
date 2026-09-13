import test from 'node:test'
import assert from 'node:assert/strict'
import { cadFailureSummary, cadModelFromResult, cadRunPresentation } from './cadAgentState.js'

const failure = (overrides = {}) => ({ runId: 'cad_no_plan', revision: 1, status: 'failed', plan: null,
  provider: { lastErrorCode: 'timeout', attempts: 8, sourceReaderAttempts: 1, sourceSpatialAttempts: 1 },
  createdAt: '2026-09-11T02:00:00Z', completedAt: '2026-09-11T02:15:00Z',
  sourceTranscription: { status: 'succeeded' }, sourceFiles: [{ filename: '9.jpg' }],
  inspection: null, artifacts: [], message: '远程 CAD Agent 本轮未完成，已保留实际计划和工具记录，可重试继续。', ...overrides })

test('planning timeout with no plan identifies the stage, total time and exactly what was saved', () => {
  const run = failure(), message = cadFailureSummary(run)
  assert.equal(message, '建模计划阶段响应超时，本轮用时约 15 分钟。原图与处理记录已保存，尚未生成模型，可继续处理。')
  assert.doesNotMatch(message, /重试 8 次|草稿.*保留|实体.*已保存/)
  const model = cadModelFromResult(run)
  assert.equal(model.agentRun.message, run.message)
  assert.equal(cadRunPresentation(model.agentRun, false, model.cadPlan).message, message)
})

test('failure time is shown only with actual duration or a valid start/end pair', () => {
  assert.match(cadFailureSummary(failure({ elapsedSeconds: 125 })), /本轮用时约 2 分钟/)
  assert.match(cadFailureSummary(failure({ elapsedSeconds: 34 })), /本轮用时约 34 秒/)
  for (const overrides of [{ createdAt: undefined }, { completedAt: undefined }, { completedAt: 'invalid' }, { completedAt: '2026-09-11T01:00:00Z' }]) {
    assert.doesNotMatch(cadFailureSummary(failure(overrides)), /本轮用时|分钟|秒|NaN/)
  }
})

test('source failure, saved construction draft and total time limits keep distinct explanations', () => {
  assert.match(cadFailureSummary(failure({ sourceTranscription: { status: 'failed', errorCode: 'invalid_json' } })), /^原图读取阶段未返回完整有效的结果/)
  const draft = failure({ plan: { features: [{ id: 'body' }] } })
  assert.match(cadFailureSummary(draft), /^实体生成阶段响应超时/)
  assert.match(cadFailureSummary(draft), /建模草稿与处理记录已保存/)
  assert.doesNotMatch(cadFailureSummary(draft), /原图与处理记录已保存，尚未生成模型/)
  assert.match(cadFailureSummary(failure({ provider: { lastErrorCode: 'time_limit' } })), /本轮时间已用尽/)
  assert.equal(cadFailureSummary(failure({ provider: { lastErrorCode: 'custom_source_issue' }, message: '请重新选择可读取的原图。' })), '请重新选择可读取的原图。')
})

test('independent review errors retain the entity draft and do not reuse an earlier planner error', () => {
  const review = { status: 'uncertain', source: 'independent_drawing_review', humanConfirmed: false, planHash: 'same',
    independentReview: { status: 'uncertain', source: 'independent_drawing_review', planHash: 'same', errorCode: 'invalid_json' } }
  const run = failure({ plan: { features: [{ id: 'body' }] }, drawingReview: review,
    inspection: { valid: true, kernelBacked: true, engine: 'cadquery-occt', solidCount: 1 }, artifacts: [{ format: 'glb' }] })
  assert.match(cadFailureSummary(run), /^图纸复核阶段未返回完整有效的结果/)
  assert.match(cadFailureSummary(run), /实体草稿与处理记录已保存/)
  assert.doesNotMatch(cadFailureSummary(run), /响应超时|尚未生成模型/)
})

test('saved DWG parse failures explain the source boundary and preserve source/retry state', () => {
  const run = failure({ provider: { lastErrorCode: 'dxf_parse_failed', attempts: 0 },
    message: '图纸预处理或对话上下文准备失败，请检查输入后重试。',
    sourceFiles: [{ filename: 'part.dwg', sha256: 'original-source-hash' }],
    sourceTranscription: { status: 'failed', errorCode: 'timeout' } })
  const original = structuredClone(run)
  const message = cadFailureSummary(run)
  assert.match(message, /^DWG 转换后的图纸数据无法读取，本轮建模尚未开始/)
  assert.match(message, /重新保存 DWG.*PDF\/PNG.*管理员.*原图与处理记录已保留/)
  assert.doesNotMatch(message, /损坏|登录|额度|token|响应超时|建模计划阶段|可直接重新分析/)
  assert.deepEqual(run, original)
  assert.doesNotMatch(cadFailureSummary({ ...run, sourceFiles: [] }), /原图与处理记录已保留/)
  assert.match(cadRunPresentation(run).message, /^DWG 转换后的图纸数据无法读取/)
})
