import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithOxc } from 'vite'
import { cadPrimaryAction, cadModelFromResult } from './cadAgentState.js'

const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const between = (start, end) => {
  const index = app.indexOf(start)
  assert.notEqual(index, -1, `Missing integration boundary: ${start}`)
  const stop = app.indexOf(end, index + start.length)
  assert.notEqual(stop, -1, `Missing integration boundary: ${end}`)
  return app.slice(index, stop)
}
const chatCode = between('  const showChat = (text) => {', '  useEffect(() => {')
const primaryCode = between('  const primaryAction = () => {', '  // A queued drawing')

function chatHarness(extra = {}) {
  const events = [], frames = []
  const context = vm.createContext({
    setPrompt: text => events.push(['prompt', text]),
    setMobilePane: pane => events.push(['pane', pane]),
    requestAnimationFrame: callback => { events.push(['frame']); frames.push(callback) },
    document: { querySelector: selector => {
      assert.equal(selector, '[aria-label="给 AI 发送消息"]')
      return { focus: () => events.push(['focus']) }
    } },
    ...extra,
  })
  vm.runInContext(chatCode, context)
  return { events, frames, context }
}

test('model chat actions reveal the chat pane before focusing and preserve an unfinished answer', () => {
  const { events, frames, context } = chatHarness()
  vm.runInContext("showChat('请调整孔径')", context)
  assert.deepEqual(events, [['prompt', '请调整孔径'], ['pane', 'chat'], ['frame']])
  frames.shift()()
  assert.deepEqual(events.at(-1), ['focus'])
  events.length = 0
  vm.runInContext('showChat()', context)
  assert.deepEqual(events, [['pane', 'chat'], ['frame']], 'Answering a question must not erase the current answer')
  frames.shift()()
  assert.deepEqual(events.at(-1), ['focus'])
})

test('the primary question action reveals chat and never confirms or exports an incomplete design', () => {
  const model = cadModelFromResult({ runId: 'question-run', revision: 1, status: 'needs_input', plan: null,
    questions: ['孔深是多少？'], artifacts: [] })
  const notices = []
  const { context, events, frames } = chatHarness({ model, cadPrimaryAction, featureModel: true, chatAttachments: [],
    showToast: message => notices.push(message),
    acceptDrawingData: () => assert.fail('A question must not confirm incomplete geometry'),
    exportFile: () => assert.fail('A question must not export incomplete geometry'),
  })
  vm.runInContext(`${primaryCode}\nprimaryAction()`, context)
  assert.deepEqual(events, [['pane', 'chat'], ['frame']])
  assert.match(notices[0], /孔深/)
  frames.shift()()
  assert.deepEqual(events.at(-1), ['focus'])
})

test('an external inspector request opens parameters once and ordinary visits remain quiet', () => {
  const code = between('  const showInspector = (tab = activePanel) => {', '  useEffect(() => {\n    if (!previewExpanded) return')
  const events = [], frames = []
  let effects = [], previousDependencies = []
  const props = { inspectorRequest: false, onInspectorHandled: () => events.push(['handled']),
    chatRequest: false, onChatHandled: () => events.push(['chat-handled']) }
  const context = vm.createContext({ props, activePanel: '检查',
    setActivePanel: value => events.push(['tab', value]),
    setInspectorOpen: value => events.push(['open', value]),
    setMobilePane: value => events.push(['pane', value]),
    inspectorRef: { current: { scrollIntoView: () => events.push(['scroll']), focus: () => events.push(['focus']) } },
    setPrompt: () => assert.fail('An external chat request must preserve the recovery draft'),
    document: { querySelector: () => ({ focus: () => events.push(['chat-focus']) }) },
    requestAnimationFrame: callback => frames.push(callback),
    useEffect: (callback, dependencies) => { effects.push({ callback, dependencies: [...dependencies] }) },
  })
  const renderEffects = () => {
    // Re-execute the real hook registration on each render without replacing its body.
    effects = []
    vm.runInContext(`{ ${code} }`, context)
    effects.forEach((effect, index) => {
      const previous = previousDependencies[index]
      const changed = !previous || effect.dependencies.some((value, item) => !Object.is(value, previous[item]))
      previousDependencies[index] = effect.dependencies
      if (changed) effect.callback()
    })
  }
  renderEffects()
  assert.deepEqual(events, [])
  props.inspectorRequest = true
  renderEffects()
  assert.deepEqual(events, [['tab', '参数'], ['open', true], ['pane', 'inspector'], ['handled']])
  assert.equal(frames.length, 1)
  frames.shift()()
  assert.deepEqual(events.slice(-2), [['scroll'], ['focus']])
  events.length = 0
  props.inspectorRequest = false
  renderEffects()
  context.activePanel = '特征'
  renderEffects()
  assert.deepEqual(events, [], 'A consumed request and ordinary tab updates must not reopen the inspector')
  props.inspectorRequest = true
  renderEffects()
  assert.deepEqual(events, [['tab', '参数'], ['open', true], ['pane', 'inspector'], ['handled']])
  frames.length = 0
  events.length = 0
  props.inspectorRequest = false
  props.chatRequest = true
  renderEffects()
  assert.deepEqual(events, [['pane', 'chat'], ['chat-handled']])
  frames.shift()()
  assert.deepEqual(events.at(-1), ['chat-focus'])
  events.length = 0
  props.chatRequest = false
  renderEffects()
  assert.deepEqual(events, [])
})

const homeRaw = readFileSync(new URL('./StudioHome.jsx', import.meta.url), 'utf8')
const homeCompiled = await transformWithOxc(homeRaw.replace(/^import .*$/gm, '').replace('export default function', 'function'), 'StudioHome.jsx', { jsx: { runtime: 'classic' } })
const nodes = element => !element || typeof element !== 'object' ? [] : [element, ...React.Children.toArray(element.props?.children).flatMap(nodes)]
function renderHome(props) {
  const context = vm.createContext({ React, useRef: () => ({ current: null }) })
  vm.runInContext(homeCompiled.code, context)
  return context.StudioHome(props)
}

test('home description survives unmount and return, and starting sends the retained text', () => {
  let description = '', mode = '首页'
  const starts = []
  const properties = () => ({ projects: [{ id: 'design-a', name: '验收零件', files: 2, updated: '刚刚' }], description,
    setDescription: value => { description = value }, setActiveMode: value => { mode = value },
    onSelectProject: () => { mode = '3D 建模' }, onStartText: text => starts.push(text),
  })
  const initial = renderHome(properties())
  assert.equal(nodes(initial).find(node => node.props['aria-label'] === '开始文字设计').props.disabled, true)
  nodes(initial).find(node => node.type === 'textarea').props.onChange({ target: { value: '保留草稿：带孔法兰' } })
  const typed = renderHome(properties())
  nodes(typed).find(node => node.props.className === 'studio-project-row').props.onClick()
  assert.equal(mode, '3D 建模')
  mode = '首页'
  const returned = renderHome(properties())
  assert.equal(nodes(returned).find(node => node.type === 'textarea').props.value, '保留草稿：带孔法兰')
  nodes(returned).find(node => node.props['aria-label'] === '开始文字设计').props.onClick()
  assert.deepEqual(starts, ['保留草稿：带孔法兰'])
  const markup = renderToStaticMarkup(returned)
  assert.match(markup, /验收零件/)
  assert.match(markup, /studio-home-project-name/)
  assert.doesNotMatch(markup, /class="studio-project-name"|class="studio-recent"/)
  assert.match(app, /<HomeWorkspace description=\{homePrompt\} setDescription=\{setHomePrompt\}/, 'Draft state must belong to the account workspace, outside the conditional Home component')
})

test('home upload carries the typed description and original file, while cancelling preserves the draft', () => {
  const events = []
  const draft = '保留图纸尺寸，中心孔改为 12 mm'
  const tree = renderHome({ projects: [], description: draft, setDescription: () => assert.fail('Uploading must not erase the home draft'),
    onStartText: () => assert.fail('The upload handler must create a file only once through its parent attachment callback'),
    attachDrawingToConversation: (files, text) => events.push(['attach', files, text]),
  })
  const upload = nodes(tree).find(node => node.type === 'input' && node.props.type === 'file')
  upload.props.onChange({ currentTarget: { files: [], value: '' } })
  assert.deepEqual(events, [], 'Cancelling the file picker must not navigate away')
  const original = new File(['original source bytes'], '法兰.dxf', { type: 'application/dxf' })
  const target = { files: [original], value: 'selected-file' }
  upload.props.onChange({ currentTarget: target })
  assert.equal(events.length, 1)
  assert.equal(events[0][0], 'attach')
  assert.equal(events[0][1].length, 1)
  assert.equal(events[0][1][0], original, 'Pass the actual File rather than saved filename metadata')
  assert.equal(events[0][2], draft)
  assert.equal(target.value, '', 'Reset the input so the same source can be selected again')
})

test('home attachment creates one usable file before setting its prompt and blocks invalid or busy submissions', () => {
  const code = between('  const attachDrawingToConversation = ', '  const remodelLegacyDrawing = ')
  const normalizer = between('function normalizeFilesInput(input) {', '\nfunction ')
  const file = new File(['original DXF'], 'original.dxf', { type: 'application/dxf' })
  for (const activeFile of [null, { type: '文档' }, { type: '零件', contentUnavailable: true }, { type: '零件' }]) {
    const state = { prompt: 'previous draft', files: null, mode: '首页', created: 0, showOriginalModel: false }
    const context = vm.createContext({ activeFile, isGenerating: false, isAccepting: false,
      chatAbortRef: { current: null }, cadConfirmRef: { current: null }, drawingJobRef: { current: null },
      canSwitch: () => true, createFile: () => { state.created += 1; state.prompt = ''; return true },
      setPrompt: value => { state.prompt = value }, setChatAttachments: value => { state.files = value },
      setActiveMode: value => { state.mode = value }, setShowOriginalModel: value => { state.showOriginalModel = value }, showToast: () => {},
    })
    vm.runInContext(`${normalizer}\n${code}\nglobalThis.attach = attachDrawingToConversation`, context)
    context.attach([file], '图纸上未写明的厚度为 8 mm')
    assert.equal(state.created, !activeFile || activeFile.type === '文档' || activeFile.contentUnavailable ? 1 : 0)
    assert.equal(state.prompt, '图纸上未写明的厚度为 8 mm', 'The new file snapshot must not overwrite the home description')
    assert.equal(state.files[0], file)
    assert.equal(state.mode, '3D 建模')
    assert.equal(state.showOriginalModel, true, 'Uploading must open the AI composer even if the prior file showed its manual result')
    const before = { ...state }
    context.attach([new File(['bad'], 'document.txt', { type: 'text/plain' })], 'must not apply')
    assert.deepEqual(state, before)
    context.isGenerating = true
    context.attach([file], 'must not apply while busy')
    assert.deepEqual(state, before)
  }
})

test('command and new-project keyboard shortcuts close mobile navigation before opening their dialog', () => {
  const start = app.indexOf('    const onKeyDown = (event) => {\n      if (suspended || event.defaultPrevented) return')
  assert.notEqual(start, -1)
  const code = app.slice(start, app.indexOf("    window.addEventListener('keydown', onKeyDown)", start))
  const events = []
  const context = vm.createContext({ suspended: false, setMobileMenuOpen: value => events.push(['menu', value]), setDialog: value => events.push(['dialog', value]) })
  vm.runInContext(`${code}\nglobalThis.onShortcut = onKeyDown`, context)
  for (const [key, dialog] of [['k', 'commands'], ['n', 'project']]) {
    events.length = 0
    context.onShortcut({ key, metaKey: true, preventDefault: () => events.push(['prevent']) })
    assert.deepEqual(events, [['prevent'], ['menu', false], ['dialog', dialog]])
  }
  events.length = 0
  context.onShortcut({ key: 'k', metaKey: true, defaultPrevented: true })
  assert.deepEqual(events, [], 'A workspace that consumed a shortcut must keep ownership')
})
