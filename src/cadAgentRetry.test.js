import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithOxc } from 'vite'
import * as state from './cadAgentState.js'
import { recoverWorkspaceSnapshot, sanitizeWorkspaceSnapshot } from './projectStore.js'

// Exercise the real JSX button callbacks using the project's installed compiler.
const raw = readFileSync(new URL('./CadAgentPanel.jsx', import.meta.url), 'utf8')
const compiled = await transformWithOxc(raw.replace(/^import .*$/gm, '').replace(/export default function /g, 'function ').replace(/export function /g, 'function '), 'CadAgentPanel.jsx', { jsx: { runtime: 'classic' } })
const context = vm.createContext({ React, ...state, cadArtifactUrl: () => '' })
vm.runInContext(compiled.code, context)
const buttons = (element) => !element || typeof element !== 'object' ? []
  : [...(element.type === 'button' ? [element] : []), ...React.Children.toArray(element.props?.children).flatMap(buttons)]
const model = (run = {}) => ({ kind: 'feature_model', name: '图纸项目', cadPlan: null,
  agentRun: { runId: 'saved-failure', revision: 1, status: 'failed', questions: [], sourceTranscription: { status: 'failed' }, ...run } })

test('summary, empty parameter panel and checks panel expose a real retry action and respect busy state', () => {
  let sends = 0
  const props = { model: model(), busy: false, onConfirm: () => { sends += 1 },
    onAnswer: () => { throw new Error('there is no question to answer') }, onAsk: () => { throw new Error('retry must not prefill a message') } }
  for (const render of [() => context.CadAgentSummary(props), () => context.CadAgentPanel({ ...props, tab: '参数' }), () => context.CadAgentPanel({ ...props, tab: '检查' })]) {
    const tree = render()
    const retry = buttons(tree).find((button) => button.props.children === '重试读取')
    assert.ok(retry)
    assert.equal(retry.props.disabled, false)
    retry.props.onClick()
    assert.doesNotMatch(renderToStaticMarkup(tree), /请在对话中补充 AI 提出的问题|请回答 AI|继续修正/)
  }
  assert.equal(sends, 3)
  const reading = context.CadAgentPanel({ ...props, tab: '参数' })
  assert.match(renderToStaticMarkup(reading), /已保存的原图/)
  for (const tree of [context.CadAgentSummary({ ...props, busy: true }), context.CadAgentPanel({ ...props, tab: '参数', busy: true })]) {
    const primary = buttons(tree).find((button) => button.props.className.includes('primary-button'))
    assert.equal(primary.props.disabled, true)
  }
})

test('the summary keeps concrete needs_input questions and focuses the answer instead of retrying', () => {
  let answers = 0
  const tree = context.CadAgentSummary({ model: model({ status: 'needs_input', questions: ['板厚是多少？'] }), busy: false,
    onAnswer: () => { answers += 1 }, onConfirm: () => { throw new Error('cannot build without the answer') } })
  const answer = buttons(tree).find((button) => button.props.children === '回答问题')
  answer.props.onClick()
  assert.equal(answers, 1)
  assert.match(renderToStaticMarkup(tree), /板厚是多少/)
})

const questionReview = (overrides = {}) => ({ scope: 'source_question_reread', status: 'succeeded', candidateEvidence: true, verified: false,
  inputFingerprint: 'local-source-identity', questions: ['孔的位置从哪条基准边测量？', '背面是否有槽？'],
  answers: [{ questionIndex: 0, questionId: 'question-0', status: 'answered', answer: '候选：从左侧底边测量。',
    source: { imageId: 'source-0', location: '左下视图', evidence: '尺寸引线连接左侧底边与孔中心线。' }, confidence: 'high' },
    { questionIndex: 1, questionId: 'question-1', status: 'unresolved', answer: null, source: null, confidence: 'uncertain' }], ...overrides })

test('question rereading displays source candidates and unresolved questions without converting them into user answers', () => {
  const current = model({ sourceQuestionReviews: [questionReview()] })
  const tree = context.CadAgentSummary({ model: current, busy: false, onConfirm: () => {}, onAnswer: () => {} })
  const markup = renderToStaticMarkup(tree)
  assert.match(markup, /提问前原图复读/)
  assert.match(markup, /AI 候选依据 · 待核对/)
  assert.match(markup, /不是用户回答，也不是已确认尺寸/)
  assert.match(markup, /孔的位置从哪条基准边测量/)
  assert.match(markup, /左下视图/)
  assert.match(markup, /尺寸引线连接左侧底边与孔中心线/)
  assert.match(markup, /背面是否有槽/)
  assert.match(markup, /仍未确定/)
  assert.doesNotMatch(markup, /复读通过|用户已回答|原图一致|校核通过/)
  assert.equal(current.agentRun.status, 'failed')
  assert.equal(current.cadPlan, null)
})

test('failed rereading is visible, old snapshots remain compatible, and unverified progress preserves geometry state', () => {
  const failed = questionReview({ status: 'failed', answers: [] })
  const saved = state.cadModelFromResult({ runId: 'new-source', revision: 1, status: 'failed', plan: null,
    questions: ['背面是否有槽？'], sourceQuestionReviews: [failed] })
  assert.deepEqual(saved.agentRun.sourceQuestionReviews, [failed])
  const restored = recoverWorkspaceSnapshot(JSON.parse(JSON.stringify(sanitizeWorkspaceSnapshot({ model: saved, generation: null }))))
  assert.deepEqual(restored.model.agentRun.sourceQuestionReviews, [failed])
  assert.equal(restored.model.agentRun.status, 'failed')
  assert.equal(restored.generation, null)
  assert.equal(state.cadGenerationFromResult(saved.agentRun, saved.cadPlan), null)
  const markup = renderToStaticMarkup(context.CadAgentSummary({ model: saved, onConfirm: () => {}, onAnswer: () => {} }))
  assert.match(markup, /第 1 次复读 · 未完成/)
  assert.doesNotMatch(renderToStaticMarkup(context.CadAgentSummary({ model: model(), onConfirm: () => {}, onAnswer: () => {} })), /提问前原图复读/)
  assert.deepEqual(state.cadSourceQuestionReviews([{ ...questionReview(), verified: true }]), [])
  assert.equal(state.cadSourceQuestionReviews([failed, questionReview(), failed]).length, 2)
  assert.match(state.cadTraceMessage({ action: 'source_question_review', status: 'failed' }), /复读未完成/)
  assert.match(state.cadTraceMessage({ action: 'source_question_review', status: 'succeeded' }), /候选.*待核对/)
  for (const stage of ['source_question_review', 'source_question_resolved']) {
    const progress = state.cadProgressFromEvent({ stage }, { phase: 'generate' })
    assert.equal(progress.phase, 'generate')
    assert.match(progress.label, /核对/)
    assert.doesNotMatch(progress.label, /已确认|已生成|通过/)
    assert.equal(state.cadRunPresentation({ status: 'ready' }, true).confirmed, false)
  }
})

// Follow native <details> visibility, while still rendering the real JSX.
// These assertions catch warnings accidentally moved into collapsed records.
const visibleText = (element) => {
  if (element === null || element === undefined || typeof element === 'boolean') return ''
  if (typeof element !== 'object') return String(element)
  if (typeof element.type === 'function') return visibleText(element.type(element.props))
  const children = React.Children.toArray(element.props?.children)
  if (element.type === 'details' && !element.props.open) return children.filter((child) => child.type === 'summary').map(visibleText).join(' ').replace(/\s+/g, ' ')
  return children.map(visibleText).join(' ').replace(/\s+/g, ' ')
}

test('compact confirmed summary keeps a quiet version label and folds historical process records', () => {
  const current = model({ status: 'ready', message: '完整检查说明应在记录中查看',
    trace: [{ action: 'read_source', message: '逐项原图转录过程' }] })
  current.cadPlan = { parameters: {}, features: [] }
  const tree = context.CadAgentSummary({ model: current, compact: true, onConfirm: () => assert.fail('confirmed must not submit') })
  assert.match(visibleText(tree), /已确认并生成.*第 1 版/)
  assert.match(visibleText(tree), /版本与检查记录/)
  assert.doesNotMatch(visibleText(tree), /完整检查说明应在记录中查看|逐项原图转录过程/)
  assert.match(renderToStaticMarkup(tree), /完整检查说明应在记录中查看/)
  assert.equal(buttons(tree).length, 0)
})

test('compact status can delegate its CTA without hiding questions or blocked delivery', () => {
  const current = model({ status: 'needs_input', questions: ['板厚是多少？', '通孔直径是多少？', '孔中心距是多少？'] })
  const delegated = context.CadAgentSummary({ model: current, compact: true, showAction: false })
  assert.equal(buttons(delegated).length, 0)
  assert.match(visibleText(delegated), /板厚是多少.*通孔直径是多少.*查看其余 1 项问题/)
  assert.match(renderToStaticMarkup(delegated), /孔中心距是多少/)
  let confirms = 0
  const blocked = model({ status: 'ready', deliveryBlockedReason: '轮廓核对记录不属于当前版本' })
  blocked.cadPlan = { parameters: {}, features: [] }
  const tree = context.CadAgentSummary({ model: blocked, compact: true, onConfirm: () => { confirms += 1 } })
  assert.match(visibleText(tree), /轮廓核对记录不属于当前版本/)
  assert.doesNotMatch(visibleText(tree), /已确认并生成/)
  buttons(tree)[0].props.onClick()
  assert.equal(confirms, 1)
  assert.equal(blocked.agentRun.deliveryBlockedReason, '轮廓核对记录不属于当前版本')
})

test('compact busy summary labels questions as saved history and keeps its action disabled', () => {
  const tree = context.CadAgentSummary({ model: model({ status: 'needs_input', revision: 3, questions: ['上一版待确认孔深'] }),
    compact: true, busy: true, onAnswer: () => assert.fail('busy') })
  assert.match(visibleText(tree), /本轮仍在处理.*第 3 版/)
  assert.match(visibleText(tree), /已保存版本中的问题，本轮完成后更新/)
  assert.equal(buttons(tree)[0].props.disabled, true)
})

test('checks fold passed evidence but expose failed and uncertain measurements and original drawing differences', () => {
  const current = model({ status: 'review_required', inspection: {
    valid: false, solidCount: 1,
    checks: [{ id: 'pass', label: '闭合实体检查', passed: true }, { id: 'fail', label: '薄壁厚度不足', passed: false }, { id: 'unknown', label: '倒角未核查' }],
    acceptance: { status: 'failed', checks: [{ id: 'good', label: '底板长度实測', passed: true, expected: 40, actual: 40 },
      { id: 'bad', label: '通孔深度不符', passed: false, expected: 10, actual: 8 }, { id: 'missing', label: '缺少材料依据', passed: null }] },
  }, projectionComparison: { views: [
    { view: 'front', status: 'supported' },
    { view: 'top', status: 'mismatch', reliability: 'high', differenceRegions: [{ finding: '左侧轮廓缺口' }] },
    { view: 'right', status: 'unverified' },
  ] }, drawingReview: { status: 'mismatch', message: '原图与实体存在差异', differences: ['后壁通孔缺失'], observations: ['仅供追溯的局部观察'] } })
  current.cadPlan = { parameters: {}, features: [] }
  const tree = context.CadAgentPanel({ model: current, tab: '检查', onConfirm: () => {}, onAsk: () => {} })
  const shown = visibleText(tree), markup = renderToStaticMarkup(tree)
  for (const warning of ['薄壁厚度不足', '倒角未核查', '通孔深度不符', '缺少材料依据', '左侧轮廓缺口', '核对范围不足', '后壁通孔缺失']) assert.ok(shown.includes(warning), warning)
  for (const record of ['闭合实体检查', '底板长度实測', '仅供追溯的局部观察']) {
    assert.equal(shown.includes(record), false, record)
    assert.ok(markup.includes(record), record)
  }
})

test('parameter source disclosure preserves visible questions and the original edit callback', () => {
  const edits = []
  const tree = context.CadParameterField({ row: { key: 'depth', label: '孔深', value: 8, unit: 'mm',
    source: { description: '右视图下方标注' }, question: '请确认这是盲孔深度还是总厚度' },
    run: {}, busy: false, onParameterChange: (...args) => edits.push(args) })
  assert.match(visibleText(tree), /请确认这是盲孔深度还是总厚度/)
  assert.doesNotMatch(visibleText(tree), /右视图下方标注/)
  assert.match(renderToStaticMarkup(tree), /右视图下方标注/)
  const label = React.Children.toArray(tree.props.children).find((child) => child.type === 'label')
  const input = React.Children.toArray(React.Children.toArray(label.props.children).find((child) => child.type === 'div').props.children)[0]
  assert.equal(input.props.disabled, false)
  input.props.onChange({ target: { value: '10.25' } })
  assert.deepEqual(edits, [['depth', '10.25']])
})

test('a healthy checks panel uses short disclosures instead of expanded success sections', () => {
  const current = model({ status: 'ready', inspection: { valid: true, solidCount: 1,
    checks: [{ label: '拓扑细节记录', passed: true }],
    acceptance: { status: 'passed', checks: [{ id: 'size', label: '精确尺寸记录', passed: true, expected: 40.5, actual: 40.5 }] } },
    projectionComparison: { views: [{ view: 'front', status: 'supported' }] },
    drawingReview: { status: 'consistent', message: '详细原图对照说明' } })
  current.cadPlan = { parameters: {}, features: [] }
  const tree = context.CadAgentPanel({ model: current, tab: '检查', onConfirm: () => {}, onAsk: () => {} })
  const shown = visibleText(tree)
  assert.match(shown, /实体几何检查通过.*实体数据.*尺寸实测.*轮廓核对.*原图对照记录/)
  for (const detail of ['拓扑细节记录', '精确尺寸记录', '详细原图对照说明']) {
    assert.equal(shown.includes(detail), false)
    assert.ok(renderToStaticMarkup(tree).includes(detail))
  }
  assert.doesNotMatch(shown, /实体检查结果|尺寸与结构/)
})
