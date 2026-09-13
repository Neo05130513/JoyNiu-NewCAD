import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import React from 'react'
import { transformWithOxc } from 'vite'
import { createManualFeatureReference, normalizeManualFeatureReference, manualFeatureRecordMatchesReference } from './manualFeatureReference.js'

const raw = readFileSync(new URL('./ManualFeatureResult.jsx', import.meta.url), 'utf8')
const compiled = await transformWithOxc(raw.replace(/^import .*$/gm, '').replace('export default function', 'function'), 'ManualFeatureResult.jsx', { jsx: { runtime: 'classic' } })
const built = (changes = {}) => ({ id: 'feature_a', revision: 2, fileId: 'file_a', name: '法兰', status: 'built', sourceRun: { runId: 'cad_source', revision: 4 },
  sharedDesign: { id: 'design_a', sourceFeature: { id: 'feature_a', revision: 2 } },
  artifacts: { step: { format: 'step' }, glb: { format: 'glb' } }, ...changes })
const reference = value => createManualFeatureReference(value, { accountKey: 'alice', projectId: 'project_a', fileId: 'file_a' })

function harness(client, initial = {}) {
  const slots = [], effects = [], downloads = [], notices = []
  let cursor = 0, dirty = true, tree, props = { reference: reference(built()), token: 'token-a', accountKey: 'alice', active: true, showToast: (...args) => notices.push(args), ...initial }
  const Canvas = () => null
  const context = vm.createContext({ React, AbortController, Blob, directFeatureClient: client, EngineeringModelCanvas: Canvas,
    normalizeManualFeatureReference, manualFeatureRecordMatchesReference, readableError: error => error.message,
    downloadBlob: (...args) => downloads.push(args),
    useState(initial) { const index = cursor++; if (!slots[index]) slots[index] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[index].value, value => { const next = typeof value === 'function' ? value(slots[index].value) : value; if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true } }] },
    useRef(initial) { const index = cursor++; if (!slots[index]) slots[index] = { current: initial }; return slots[index] },
    useEffect(callback, dependencies) { const index = cursor++, previous = slots[index]; if (!previous || dependencies.some((value, item) => !Object.is(value, previous.dependencies[item]))) { slots[index] = { dependencies, cleanup: previous?.cleanup }; effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = callback() }) } },
  })
  vm.runInContext(compiled.code, context)
  const render = () => { let round = 0; do { dirty = false; cursor = 0; tree = context.ManualFeatureResult(props); while (effects.length) effects.shift()(); assert.ok(round++ < 20) } while (dirty); return tree }
  const flush = async () => { for (let i = 0; i < 3; i++) { await new Promise(resolve => setImmediate(resolve)); render() } }
  const nodes = (element = tree) => !React.isValidElement(element) ? [] : [element, ...React.Children.toArray(element.props.children).flatMap(nodes)]
  const text = (element = tree) => React.isValidElement(element) ? React.Children.toArray(element.props.children).map(text).join('') : String(element ?? '')
  const button = label => { const found = nodes().find(element => element.type === 'button' && text(element) === label); assert.ok(found, label); return found }
  const click = async label => { await button(label).props.onClick(); render(); await flush() }
  const update = async patch => { props = { ...props, ...patch }; render(); await flush() }
  const unmount = () => { for (const slot of slots) slot?.cleanup?.() }
  render()
  return { flush, nodes, text, button, click, update, render, unmount, downloads, notices, Canvas }
}

test('manual results load and download the exact saved version and keep original AI navigation separate', async () => {
  const requests = [], edits = []; let originals = 0
  const client = { get: async (token, id, revision) => { requests.push(['get', token(), id, revision]); return built() },
    download: async (token, record, format) => { requests.push(['download', token(), record.id, record.revision, format]); return new Blob(['REAL MANUAL STEP'], { type: 'application/step' }) } }
  const ui = harness(client, { onEdit: value => edits.push(value), onShowOriginal: () => { originals++ } })
  try {
    await ui.flush(); assert.deepEqual(requests, [['get', 'token-a', 'feature_a', 2]])
    const canvas = ui.nodes().find(node => node.type === ui.Canvas)
    assert.equal(canvas.props.design.id, 'design_a'); assert.equal(canvas.props.active, true)
    assert.match(ui.text(), /原图一致性未核验/); assert.doesNotMatch(ui.text(), /原图一致性通过|AI 已确认/)
    await ui.click('下载手工 STEP')
    assert.deepEqual(requests[1], ['download', 'token-a', 'feature_a', 2, 'step'])
    assert.equal(await ui.downloads[0][0].text(), 'REAL MANUAL STEP'); assert.equal(ui.downloads[0][1], '法兰-手工-r2.step')
    await ui.click('继续编辑'); await ui.click('查看原 AI 版本')
    assert.deepEqual(edits[0], reference(built())); assert.equal(originals, 1)
  } finally { ui.unmount() }
})

test('saved manual drafts reopen without exposing historical preview or download controls', async () => {
  const draft = built({ status: 'draft', revision: 3 })
  const edits = [], reads = []
  const ui = harness({ get: async (_token, id, revision) => { reads.push([id, revision]); return draft }, download: () => assert.fail('A draft cannot download old geometry') },
    { reference: reference(draft), onEdit: value => edits.push(value) })
  try {
    await ui.flush(); assert.deepEqual(reads, [['feature_a', 3]])
    assert.match(ui.text(), /草稿已保存/); assert.equal(ui.nodes().some(node => node.type === ui.Canvas), false)
    assert.equal(ui.nodes().some(node => node.type === 'button' && /下载/.test(ui.text(node))), false)
    await ui.click('继续编辑'); assert.equal(edits[0].revision, 3)
  } finally { ui.unmount() }
})

test('mismatched fetched revisions block preview and download and allow a real read retry', async () => {
  let reads = 0
  const ui = harness({ get: async () => { reads++; return reads === 1 ? built({ revision: 9 }) : built() } })
  try {
    await ui.flush(); assert.match(ui.text(), /版本与来源记录不一致/)
    assert.equal(ui.nodes().some(node => node.type === ui.Canvas), false)
    await ui.click('重新读取版本'); assert.equal(reads, 2)
    assert.ok(ui.button('下载手工 GLB')); assert.ok(ui.nodes().find(node => node.type === ui.Canvas))
  } finally { ui.unmount() }
})

test('same-account token renewal aborts an old download and the next download uses the renewed token', async () => {
  let finish; const calls = [], signals = []
  const ui = harness({ get: async () => built(), download: async (token, _record, _format, signal) => {
    calls.push(token()); signals.push(signal)
    if (calls.length === 1) return new Promise(resolve => { finish = resolve })
    return new Blob(['new token STEP'])
  } })
  try {
    await ui.flush(); const pending = ui.button('下载手工 STEP').props.onClick(); ui.render()
    await ui.update({ token: 'token-renewed' }); assert.equal(signals[0].aborted, true)
    finish(new Blob(['late old response'])); await pending; await ui.flush(); assert.equal(ui.downloads.length, 0)
    await ui.click('下载手工 STEP'); assert.deepEqual(calls, ['token-a', 'token-renewed'])
    assert.equal(await ui.downloads[0][0].text(), 'new token STEP')
  } finally { ui.unmount() }
})

test('switching account discards a delayed version response before any preview or download is exposed', async () => {
  let finish, signal; const calls = []
  const ui = harness({ get: async (token, _id, _revision, requestSignal) => { calls.push(token()); signal = requestSignal; return new Promise(resolve => { finish = resolve }) } })
  try {
    await ui.update({ token: 'bob-token', accountKey: 'bob' }); assert.equal(signal.aborted, true)
    finish(built()); await ui.flush()
    assert.deepEqual(calls, ['token-a']); assert.match(ui.text(), /保存此版本的账号/)
    assert.equal(ui.nodes().some(node => node.type === ui.Canvas), false); assert.equal(ui.button('继续编辑').props.disabled, true)
    assert.equal(ui.downloads.length, 0)
  } finally { ui.unmount() }
})
