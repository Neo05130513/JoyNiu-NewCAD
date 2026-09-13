import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { appendFeatureTool } from './featureTools.js'
import { featureDependencyIssues } from './directFeatureModel.js'

// Run the real controller handlers and effects. The child surface is a props
// boundary; service calls are stubbed, while caching and transactions are real.
function harness(Component, client, initialProps) {
  const slots = [], effects = [], events = new Map()
  let cursor = 0, dirty = true, tree, props = initialProps
  const previousWindow = globalThis.window
  globalThis.window = {
    addEventListener(name, fn) { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(fn) },
    removeEventListener(name, fn) { events.get(name)?.delete(fn) },
  }
  globalThis.__featureClient = client
  globalThis.__featureHooks = {
    useState(initial) {
      const index = cursor++
      if (!slots[index]) slots[index] = { value: typeof initial === 'function' ? initial() : initial }
      return [slots[index].value, value => {
        const next = typeof value === 'function' ? value(slots[index].value) : value
        if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true }
      }]
    },
    useRef(initial) { const index = cursor++; if (!slots[index]) slots[index] = { current: initial }; return slots[index] },
    useEffect(fn, deps) {
      const index = cursor++, old = slots[index]
      if (!old || !deps || deps.some((value, i) => !Object.is(value, old.deps?.[i]))) {
        slots[index] = { deps, cleanup: old?.cleanup }
        effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = fn() })
      }
    },
  }
  const render = () => {
    let rounds = 0
    do { dirty = false; cursor = 0; tree = Component(props); while (effects.length) effects.shift()(); assert.ok(++rounds < 30, 'workspace must settle') } while (dirty)
    return tree
  }
  const flush = async () => { for (let i = 0; i < 4; i++) { await new Promise(resolve => setImmediate(resolve)); render() } }
  const nodes = (value = tree) => React.isValidElement(value) ? [value, ...React.Children.toArray(value.props.children).flatMap(nodes)] : []
  const text = value => React.isValidElement(value) ? React.Children.toArray(value.props.children).map(text).join('') : String(value ?? '')
  const find = predicate => { const value = nodes().find(predicate); assert.ok(value, 'expected matching control'); return value }
  const button = name => find(node => node.type === 'button' && text(node) === name)
  const input = () => find(node => node.type === 'input' && node.props.maxLength === 180)
  const event = async (node, name, arg) => { await node.props[name](arg); render(); await flush() }
  const update = async next => { props = { ...props, ...next }; render(); await flush() }
  const unmount = () => { for (const slot of slots) slot?.cleanup?.(); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow }
  render()
  return { render, flush, find, nodes, button, event, update, events, text: () => text(tree), unmount, surface: () => find(node => node.props.onComplete && node.props.onSave && node.props.draft).props, async call(name, ...args) { const result = await find(node => node.props.onComplete && node.props.onSave && node.props.draft).props[name](...args); render(); await flush(); return result } }
}

let directory, Component
async function loadComponent() {
  if (Component) return Component
  directory = await mkdtemp(resolve('node_modules/.feature-ui-tests-'))
  const bundle = await rolldown({ input: resolve('src/DirectFeatureWorkspace.jsx'), external: ['react/jsx-runtime'], transform: { jsx: { runtime: 'automatic' } }, plugins: [{
    name: 'feature-ui-harness',
    resolveId(source) { if (source === 'react') return '\0feature-hooks' },
    load(id) {
      if (id === '\0feature-hooks') return { code: 'export const useState=(...a)=>globalThis.__featureHooks.useState(...a);export const useRef=(...a)=>globalThis.__featureHooks.useRef(...a);export const useEffect=(...a)=>globalThis.__featureHooks.useEffect(...a);', moduleType: 'js' }
      if (id.endsWith('.css')) return { code: '', moduleType: 'js' }
      if (id.endsWith('/directFeatureClient.js')) return { code: "export const directFeatureClient=Object.fromEntries(['get','list','versions','save','build','commit','download'].map(name=>[name,(...args)=>globalThis.__featureClient[name](...args)]));", moduleType: 'js' }
      if (id.endsWith('/CadEditorSurface.jsx')) return { code: 'export default function CadEditorSurface(){return null}', moduleType: 'js' }
      if (id.endsWith('/ThreeDViewer.jsx') || id.endsWith('/FeatureSketchEditor.jsx')) return { code: 'export default function Viewer(){return null}', moduleType: 'js' }
    },
  }] })
  await bundle.write({ dir: directory, format: 'esm' }); await bundle.close()
  Component = (await import(pathToFileURL(resolve(directory, 'DirectFeatureWorkspace.js')).href)).default
  return Component
}
test.after(async () => { if (directory) await rm(directory, { recursive: true, force: true }) })

const clone = value => structuredClone(value)
const runId = `cad_${'a'.repeat(32)}`
const draft = (name = '来源长方体') => ({ name, plan: { version: 'cad-plan-v1', name, units: 'mm', parameters: { width: { value: 20 }, half: { value: null, expression: 'width / 2' } }, features: [{ id: 'body', op: 'box', size: [40, 'width', 10] }], result: 'body' }, suppressed: [], sourceRun: { runId, revision: 2 }, changeNote: '保留来源尺寸继续编辑' })
let sequence = 0
const props = (extra = {}) => ({ token: 'credential', accountKey: `feature-ui-account-${++sequence}`, active: true, initialDraft: draft(), initialPlanKey: `source-${sequence}`, sourceFileId: `file-${sequence}`, ...extra })
const record = (options = {}) => ({ ...draft(), id: 'feature_saved', fileId: 'file', revision: 1, status: 'built', ...options })
const client = overrides => ({ list: async () => ({ items: [] }), versions: async () => ({ items: [] }), get: async () => { throw new Error('unexpected get') }, save: async () => { throw new Error('unexpected legacy save') }, build: async () => { throw new Error('unexpected two-phase build') }, commit: async () => { throw new Error('unexpected commit') }, ...overrides })
const namedDraft = (value, name) => ({ ...clone(value), name, plan: { ...clone(value.plan), name } })

test('completing a transaction atomically preserves source file and AI revision and publishes only the built result', async () => {
  const commits = [], callbacks = [], created = [], initial = props({ initialPlanKey: 'long-editor-session-key-'.repeat(30), onSaved: value => callbacks.push(clone(value)), onCreate: value => created.push(clone(value)) })
  const original = clone(initial.initialDraft), next = namedDraft(original, '新的手工特征版本')
  next.plan.parameters.width.value = 25
  const ui = harness(await loadComponent(), client({ commit: async (token, previous, payload) => {
    assert.equal(token(), 'credential'); assert.equal(previous, null); commits.push(clone(payload))
    return record({ ...payload, id: 'feature_source', sharedDesign: { id: 'design_result' } })
  } }), initial)
  try {
    await ui.flush(); assert.deepEqual(ui.surface().draft, original)
    assert.equal(ui.surface().sourceFileId, initial.sourceFileId)
    const result = await ui.call('onComplete', next)
    assert.equal(commits.length, 1); assert.equal(commits[0].fileId, initial.sourceFileId)
    assert.deepEqual(commits[0].sourceRun, original.sourceRun); assert.deepEqual(commits[0].plan, next.plan)
    assert.match(commits[0].requestId, /^[0-9a-f-]{36}$/)
    assert.equal(result.id, 'feature_source'); assert.equal(result.status, 'built')
    assert.deepEqual(callbacks.map(value => value.status), ['built'])
    assert.equal(created.length, 1); assert.equal(created[0].sharedDesign.id, 'design_result')
    assert.equal(ui.surface().dirty, false); assert.equal(ui.surface().record.id, 'feature_source')
    assert.equal(ui.surface().canUndo, true); assert.deepEqual(initial.initialDraft, original)
  } finally { ui.unmount() }
})

test('opening a saved reference reads the latest head and commits to that same record with its actual revision', async () => {
  const reads = [], commits = [], callbacks = [], initial = props({ initialReference: { featureId: 'feature_existing', revision: 2 }, onSaved: value => callbacks.push(clone(value)) })
  const head = record({ id: 'feature_existing', revision: 4, fileId: initial.sourceFileId, ...draft('云端较新版本') })
  const ui = harness(await loadComponent(), client({
    get: async (_token, id, revision) => { reads.push({ id, revision }); return clone(head) },
    commit: async (_token, previous, payload) => { commits.push({ previous: clone(previous), payload: clone(payload) }); return { ...head, ...payload, revision: 5 } },
  }), initial)
  try {
    await ui.flush(); assert.deepEqual(reads, [{ id: 'feature_existing', revision: undefined }])
    assert.equal(ui.surface().draft.name, '云端较新版本'); assert.match(ui.surface().error, /最新保存版本 r4.*引用的是 r2/)
    await ui.call('onComplete', namedDraft(ui.surface().draft, '继续修改云端版本'))
    assert.equal(commits.length, 1); assert.equal(commits[0].previous.id, head.id); assert.equal(commits[0].previous.revision, 4)
    assert.equal(commits[0].payload.expectedRevision, 4); assert.equal(commits[0].payload.fileId, initial.sourceFileId)
    assert.deepEqual(commits[0].payload.sourceRun, head.sourceRun); assert.equal(callbacks[0].revision, 5)
  } finally { ui.unmount() }
})

test('failed atomic commits keep the original draft and record and retry the same request id with refreshed credentials', async () => {
  let fail = true
  const commits = [], callbacks = [], initial = props({ initialReference: { featureId: 'feature_atomic', revision: 4 }, onSaved: value => callbacks.push(value) })
  const head = record({ id: 'feature_atomic', fileId: initial.sourceFileId, revision: 4 })
  const ui = harness(await loadComponent(), client({
    get: async () => clone(head),
    commit: async (token, previous, payload) => {
      commits.push({ token: token(), previous: clone(previous), payload: clone(payload) })
      if (fail) throw Object.assign(new Error('box size must be greater than 0.0000001 mm'), { featureId: 'body', status: 400 })
      return record({ ...payload, id: previous.id, revision: previous.revision + 1 })
    },
  }), initial)
  try {
    await ui.flush(); const before = clone(ui.surface().draft), next = namedDraft(before, '操作草稿')
    next.plan.parameters.width.value = 30
    assert.equal(await ui.call('onComplete', next), undefined)
    assert.deepEqual(ui.surface().draft, before); assert.deepEqual(ui.surface().record, head)
    assert.equal(ui.surface().canUndo, false); assert.equal(ui.surface().dirty, false); assert.deepEqual(callbacks, [])
    assert.match(ui.surface().error, /长方体的长、宽、高必须大于 0/)
    assert.match(ui.surface().errorDetails, /box size/); assert.equal(ui.surface().selected, 'body')
    await ui.update({ token: 'renewed-credential' }); fail = false
    await ui.call('onComplete', next)
    assert.equal(commits.length, 2); assert.deepEqual(commits[0].payload, commits[1].payload)
    assert.equal(commits[1].token, 'renewed-credential'); assert.equal(callbacks.length, 1)
    assert.equal(ui.surface().record.revision, 5); assert.equal(ui.surface().draft.name, next.name)
    const another = namedDraft(ui.surface().draft, '下一次操作'); await ui.call('onComplete', another)
    assert.notEqual(commits[2].payload.requestId, commits[1].payload.requestId)
    assert.equal(commits[2].payload.expectedRevision, 5)
  } finally { ui.unmount() }
})

test('account and file source sessions isolate drafts and keep undo history across same-account token refresh', async () => {
  const initial = props(), ui = harness(await loadComponent(), client({}), initial)
  try {
    await ui.flush(); await ui.call('onEdit', namedDraft(ui.surface().draft, '文件 A 未保存'))
    await ui.update({ initialPlanKey: `${initial.initialPlanKey}-b`, sourceFileId: 'file-b', initialDraft: draft('文件 B') })
    assert.equal(ui.surface().draft.name, '文件 B'); assert.equal(ui.surface().canUndo, false)
    await ui.call('onEdit', namedDraft(ui.surface().draft, '文件 B 未保存'))
    await ui.update({ initialPlanKey: initial.initialPlanKey, sourceFileId: initial.sourceFileId, initialDraft: initial.initialDraft })
    assert.equal(ui.surface().draft.name, '文件 A 未保存'); assert.equal(ui.surface().canUndo, true)
    await ui.update({ token: 'refreshed-credential' }); assert.equal(ui.surface().draft.name, '文件 A 未保存')
    await ui.update({ accountKey: 'different-account', token: 'other-token', initialDraft: draft('另一个账号') })
    assert.equal(ui.surface().draft.name, '另一个账号'); assert.equal(ui.surface().canUndo, false); assert.equal(ui.surface().record, null)
    await ui.update({ accountKey: initial.accountKey, token: 'refreshed-credential', initialDraft: initial.initialDraft })
    assert.equal(ui.surface().draft.name, '文件 A 未保存')
    await ui.call('onUndo'); assert.equal(ui.surface().draft.name, initial.initialDraft.name)
    await ui.call('onRedo'); assert.equal(ui.surface().draft.name, '文件 A 未保存')
    await ui.update({ initialPlanKey: `${initial.initialPlanKey}-b`, sourceFileId: 'file-b', initialDraft: draft('文件 B') })
    assert.equal(ui.surface().draft.name, '文件 B 未保存')
  } finally { ui.unmount() }
})

test('undo and redo change only the local draft and preserve the current server revision for the next commit', async () => {
  const commits = [], initial = props({ initialReference: { featureId: 'feature_undo', revision: 2 } })
  const head = record({ id: 'feature_undo', fileId: initial.sourceFileId, revision: 2 })
  const ui = harness(await loadComponent(), client({ get: async () => clone(head), commit: async (_token, previous, payload) => { commits.push(clone(payload)); return record({ ...payload, id: previous.id, revision: previous.revision + 1 }) } }), initial)
  try {
    await ui.flush(); await ui.call('onComplete', namedDraft(ui.surface().draft, '已完成的修改'))
    assert.equal(ui.surface().record.revision, 3); assert.equal(ui.surface().canUndo, true)
    await ui.call('onUndo'); assert.equal(ui.surface().draft.name, head.name); assert.equal(ui.surface().dirty, true); assert.equal(ui.surface().record.revision, 3)
    await ui.call('onRedo'); assert.equal(ui.surface().draft.name, '已完成的修改'); assert.equal(ui.surface().record.revision, 3)
    await ui.call('onUndo'); await ui.call('onSave', true)
    assert.equal(commits.length, 2); assert.equal(commits[1].expectedRevision, 3)
    assert.equal(ui.surface().record.revision, 4); assert.equal(ui.surface().draft.name, head.name)
  } finally { ui.unmount() }
})

test('late reference reads and atomic results are aborted and cannot update a different source or call onSaved', async () => {
  for (const kind of ['read', 'commit']) {
    let finish, requestSignal, pending
    const callbacks = [], initial = props({ ...(kind === 'read' ? { initialReference: { featureId: 'feature_old', revision: 1 } } : {}), onSaved: value => callbacks.push(value) })
    const ui = harness(await loadComponent(), client({
      get: async (_token, _id, _revision, signal) => { requestSignal = signal; return new Promise(resolve => { finish = resolve }) },
      commit: async (_token, _previous, _payload, signal) => { requestSignal = signal; return new Promise(resolve => { finish = resolve }) },
    }), initial)
    try {
      await ui.flush()
      if (kind === 'commit') { pending = ui.surface().onComplete(namedDraft(ui.surface().draft, '等待中的事务')); ui.render(); await ui.flush() }
      await ui.update({ initialReference: null, initialPlanKey: `${initial.initialPlanKey}-new`, sourceFileId: 'file-new', initialDraft: draft('新文件') })
      assert.equal(requestSignal.aborted, true)
      finish(record({ id: 'feature_old', fileId: initial.sourceFileId, ...draft('迟到旧文件') })); if (pending) assert.equal(await pending, false); await ui.flush()
      assert.equal(ui.surface().draft.name, '新文件'); assert.equal(ui.surface().record, null); assert.deepEqual(callbacks, [])
      assert.equal(ui.surface().busy, ''); assert.equal(ui.surface().canUndo, false)
    } finally { ui.unmount() }
  }
})

test('failed and cross-file reference loads cannot commit a default box, and retry reopens the real source record', async () => {
  for (const kind of ['offline', 'wrong-file']) {
    let failed = true
    const commits = [], initial = props({ initialDraft: undefined, initialReference: { featureId: 'feature_retry', revision: 2 } })
    const ui = harness(await loadComponent(), client({
      get: async () => {
        if (failed && kind === 'offline') throw new Error('暂时无法读取已有特征')
        return record({ id: 'feature_retry', fileId: failed ? 'another-file' : initial.sourceFileId, revision: 3, ...draft('恢复的原模型') })
      },
      commit: async (_token, previous, payload) => { commits.push({ previous, payload }); return record({ ...payload, id: previous.id, revision: 4 }) },
    }), initial)
    try {
      await ui.flush(); assert.equal(ui.surface().unavailable, true)
      assert.match(ui.surface().error, kind === 'offline' ? /暂时无法读取/ : /来源文件不一致/)
      await ui.call('onComplete', draft()); await ui.call('onSave', true)
      assert.equal(commits.length, 0); assert.match(ui.surface().error, /先成功打开/)
      failed = false; await ui.call('onRetry')
      assert.equal(ui.surface().unavailable, false); assert.equal(ui.surface().draft.name, '恢复的原模型')
      await ui.call('onComplete', namedDraft(ui.surface().draft, '继续原模型'))
      assert.equal(commits.length, 1); assert.equal(commits[0].previous.id, 'feature_retry'); assert.equal(commits[0].payload.expectedRevision, 3)
      assert.equal(commits[0].payload.plan.features[0].id, 'body')
    } finally { ui.unmount() }
  }
})

test('a saved draft loads its last built version for fallback preview without replacing the current draft or version', async () => {
  const reads = [], initial = props({ initialReference: { featureId: 'feature_history', revision: 4 } })
  const head = record({ id: 'feature_history', fileId: initial.sourceFileId, revision: 4, status: 'draft', ...draft('尚未构建的草稿') })
  const built = record({ ...head, status: 'built', revision: 3, ...draft('上次实体'), artifacts: { glb: { url: '/api/cad/features/feature_history/versions/3/artifacts/glb' } } })
  const ui = harness(await loadComponent(), client({
    get: async (_token, id, revision) => { reads.push({ id, revision }); return clone(revision === 3 ? built : head) },
    versions: async () => ({ items: [{ revision: 4, status: 'draft' }, { revision: 3, status: 'built' }] }),
  }), initial)
  try {
    await ui.flush(); assert.deepEqual(reads, [{ id: head.id, revision: undefined }, { id: head.id, revision: 3 }])
    assert.equal(ui.surface().record.revision, 4); assert.equal(ui.surface().draft.name, '尚未构建的草稿')
    assert.equal(ui.surface().fallback.glbUrl, built.artifacts.glb.url); assert.equal(ui.surface().fallback.token, 'credential')
  } finally { ui.unmount() }
})

test('added cut and unary tools target the chosen output rather than an unrelated last feature', () => {
  const initial = draft()
  initial.plan.features.push({ id: 'unrelatedTool', op: 'cylinder', radius: 1, height: 1, origin: [100, 0, 0] })
  const original = clone(initial)
  const cut = appendFeatureTool(initial, 'profile_extrude', 'cut')
  const tool = cut.draft.plan.features.at(-2), result = cut.draft.plan.features.at(-1)
  assert.equal(tool.op, 'profile_extrude'); assert.equal(result.op, 'cut')
  assert.deepEqual(result.inputs, ['body', tool.id]); assert.equal(cut.draft.plan.result, result.id); assert.equal(cut.selected, tool.id)
  assert.deepEqual(featureDependencyIssues(cut.draft.plan), []); assert.deepEqual(initial, original)
  for (const op of ['fillet', 'chamfer', 'shell', 'linear_pattern', 'circular_pattern']) {
    const next = appendFeatureTool(initial, op)
    assert.equal(next.draft.plan.features.at(-1).input, 'body', op)
    assert.deepEqual(featureDependencyIssues(next.draft.plan), [])
  }
  assert.throws(() => appendFeatureTool({ ...initial, suppressed: ['body'] }, 'profile_extrude', 'cut'), /有效的输出实体/)
})

test('same-project tab navigation prompts for dirty drafts and discarded edits never revive from the cache', async () => {
  const opens=[], initial=props({sourceProjectId:'project_tabs'})
  const a={accountKey:initial.accountKey,projectId:'project_tabs',fileId:initial.sourceFileId,name:'A',status:'plan'}
  const b={...a,fileId:'document_b',name:'B'}
  initial.documents=[a,b,{...b,fileId:'foreign',accountKey:'someone_else'},{...b,fileId:'other_project',projectId:'elsewhere'}]
  initial.onOpenDocument=value=>opens.push(value)
  const ui=harness(await loadComponent(),client({}),initial)
  try {
    await ui.flush();assert.deepEqual(ui.surface().documents,[a,b])
    await ui.call('onEdit',namedDraft(ui.surface().draft,'A 的未保存修改'))
    await ui.call('onOpenDocument',b);assert.deepEqual(opens,[])
    await ui.event(ui.button('继续编辑'),'onClick');assert.equal(ui.surface().draft.name,'A 的未保存修改')
    await ui.call('onOpenDocument',b);await ui.event(ui.button('放弃修改并继续'),'onClick')
    assert.deepEqual(opens,[b]);assert.equal(ui.surface().draft.name,initial.initialDraft.name);assert.equal(ui.surface().canUndo,false)
    const firstSurfaceScope=ui.surface().scope
    await ui.update({sourceFileId:b.fileId,initialDraft:draft('B 初始'),initialPlanKey:initial.initialPlanKey})
    assert.notEqual(ui.surface().scope,firstSurfaceScope,'file switch also resets isolated child operations')
    assert.equal(ui.surface().draft.name,'B 初始','source file isolates even identical plan keys')
    await ui.update({sourceFileId:a.fileId,initialDraft:initial.initialDraft})
    assert.equal(ui.surface().draft.name,initial.initialDraft.name);assert.equal(ui.surface().canUndo,false)
    await ui.call('onOpenDocument',{...b,accountKey:'someone_else'});assert.equal(opens.length,1)
    assert.match(ui.surface().error,/不属于当前账号和项目/)
  } finally {ui.unmount()}
})

test('back, new document and workspace navigation all retain dirty drafts until explicit discard', async () => {
  const calls=[],initial=props({onBack:()=>calls.push('back'),onNewDocument:()=>calls.push('new'),onNavigate:mode=>calls.push(mode)})
  const ui=harness(await loadComponent(),client({}),initial)
  try {
    await ui.flush()
    for(const [method,arg,expected] of [['onBack',undefined,'back'],['onNewDocument',undefined,'new'],['onNavigate','首页','首页']]){
      await ui.call('onEdit',namedDraft(ui.surface().draft,`待保存 ${method}`))
      const count=calls.length;await ui.call(method,arg);assert.equal(calls.length,count)
      await ui.event(ui.button('继续编辑'),'onClick');assert.match(ui.surface().draft.name,/待保存/)
      await ui.call(method,arg);await ui.event(ui.button('放弃修改并继续'),'onClick')
      assert.equal(calls.at(-1),expected);assert.equal(ui.surface().draft.name,initial.initialDraft.name)
    }
    await ui.call('onEdit',namedDraft(ui.surface().draft,'旧账号修改'));await ui.call('onBack')
    const oldConfirm=ui.button('放弃修改并继续').props.onClick
    await ui.update({accountKey:'another-account',initialDraft:draft('新账号空会话')})
    oldConfirm();await ui.flush();assert.equal(calls.length,3);assert.equal(ui.surface().draft.name,'新账号空会话')
  } finally {ui.unmount()}
})

test('new empty file commits its first model to that file and pending server work blocks document navigation', async () => {
  let finish;const commits=[],leaves=[],initial=props({initialDraft:{name:'新零件',plan:{version:'cad-plan-v1',units:'mm',name:'新零件',parameters:{},features:[],result:''},suppressed:[],sourceRun:null,changeNote:'新建零件'},onNewDocument:()=>leaves.push('new')})
  const ui=harness(await loadComponent(),client({commit:(_token,_record,payload)=>{commits.push(payload);return new Promise(resolve=>{finish=resolve})}}),initial)
  try {
    await ui.flush();assert.deepEqual(ui.surface().draft.plan.features,[])
    const pending=ui.surface().onComplete(draft('第一实体'));ui.render()
    await ui.call('onNewDocument');assert.deepEqual(leaves,[]);assert.match(ui.surface().error,/仍在处理/)
    assert.equal(commits[0].fileId,initial.sourceFileId)
    finish(record({...commits[0],id:'feature_first'}));await pending;await ui.flush()
    assert.equal(ui.surface().record.fileId,initial.sourceFileId);assert.equal(ui.surface().dirty,false)
    await ui.call('onNewDocument');assert.deepEqual(leaves,['new'])
  } finally {ui.unmount()}
})

test('reopening a clean cached document fetches the latest head while dirty caches require explicit reload', async () => {
  const initial = props({ initialReference: { featureId: 'feature_cache_head', revision: 1 } }), reads = []
  let head = record({ id: 'feature_cache_head', fileId: initial.sourceFileId, revision: 1 })
  const api = client({ get: async (_token,id) => { reads.push(id); return clone(head) } })
  let ui = harness(await loadComponent(), api, initial)
  await ui.flush(); assert.equal(ui.surface().record.revision, 1); ui.unmount()
  head = { ...head, revision: 2, name: '另一标签已保存' }
  ui = harness(await loadComponent(), api, initial)
  await ui.flush(); assert.equal(ui.surface().record.revision, 2); assert.equal(reads.length, 2)
  await ui.call('onEdit', namedDraft(ui.surface().draft, '保留的本地修改')); ui.unmount()
  head = { ...head, revision: 3 }
  ui = harness(await loadComponent(), api, initial)
  try {
    await ui.flush(); assert.equal(reads.length, 2); assert.equal(ui.surface().draft.name, '保留的本地修改')
    await ui.call('onReload'); assert.equal(reads.length, 2)
    await ui.event(ui.button('继续编辑'), 'onClick'); assert.equal(ui.surface().draft.name, '保留的本地修改')
    await ui.call('onReload'); await ui.event(ui.button('放弃修改并继续'), 'onClick')
    assert.equal(ui.surface().record.revision, 3); assert.equal(ui.surface().dirty, false)
  } finally { ui.unmount() }
})

test('community copy navigation protects an unsaved draft and invalidates late results when the account changes', async () => {
  const calls=[]
  let finish
  const initial=props({initialReference:{featureId:'feature_community_source',revision:1},onOpenCommunity:(entry,context)=>{
    calls.push({entry,context});return new Promise(resolve=>{finish=resolve})
  }})
  const head=record({id:'feature_community_source',fileId:initial.sourceFileId})
  const ui=harness(await loadComponent(),client({get:async()=>clone(head)}),initial)
  try {
    await ui.flush()
    await ui.call('onEdit',namedDraft(ui.surface().draft,'尚未保存的用户修改'))
    assert.equal(await ui.call('onOpenCommunity',{resourceId:'resource_public',name:'资源副本'}),false)
    assert.equal(calls.length,0)
    assert.match(ui.text(),/未保存修改/)
    ui.button('放弃修改并继续').props.onClick();ui.render();await ui.flush()
    assert.equal(calls.length,1);assert.equal(calls[0].entry.resourceId,'resource_public')
    assert.equal(calls[0].context.current(),true);assert.ok(ui.surface().busy)
    await ui.update({accountKey:'different-community-account',token:'different-token',initialReference:null,initialPlanKey:'other-file',sourceFileId:'other-file',initialDraft:draft('另一个账号的文档')})
    assert.equal(calls[0].context.signal.aborted,true);assert.equal(calls[0].context.current(),false)
    finish(true);await ui.flush()
    assert.equal(ui.surface().draft.name,'另一个账号的文档');assert.equal(Boolean(ui.surface().busy),false)
  } finally {ui.unmount()}
})
