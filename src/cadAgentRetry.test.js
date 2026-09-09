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
