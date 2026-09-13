import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
let folder, Pdm, Standard, ImportBody, Community
before(async () => {
  folder = await mkdtemp(resolve('node_modules/.cad-panels-'))
  const bundle = await rolldown({ input: [resolve('src/CadPdmPanel.jsx'), resolve('src/CadStandardPartPanel.jsx'), resolve('src/CadImportBodyPanel.jsx'), resolve('src/CadCommunityPanel.jsx')], external: ['react/jsx-runtime'], transform: { jsx: { runtime: 'automatic' } }, plugins: [{
    name: 'real-panel-hooks', resolveId(source) { if (source === 'react') return '\0panel-hooks'; if (source.endsWith('/cadPdmClient.js')) return '\0pdm-client'; if (source.endsWith('/cadImportClient.js')) return '\0import-client'; if (source.endsWith('/cadCommunityClient.js')) return '\0community-client' },
    load(id) { if (id === '\0panel-hooks') return { code: ['useState', 'useRef', 'useEffect'].map(name => `export const ${name}=(...a)=>globalThis.__panelHooks.${name}(...a);`).join('\n'), moduleType: 'js' }; if (id === '\0pdm-client') return { code: 'export const cadPdmClient=new Proxy({}, {get:(_,key)=>(...args)=>globalThis.__pdmClient[key](...args)});', moduleType: 'js' }; if (id === '\0import-client') return { code: 'export const cadImportClient={upload:(...args)=>globalThis.__importClient.upload(...args)};', moduleType: 'js' }; if (id === '\0community-client') return { code: 'export const cadCommunityClient=new Proxy({}, {get:(_,key)=>(...args)=>globalThis.__communityClient[key](...args)});', moduleType: 'js' }; if (id.endsWith('/CadModelViewport.jsx')) return { code: 'export default function Viewport(){return null}', moduleType: 'js' }; if (id.endsWith('/workspaceFeedback.js')) return { code: 'export const readableError=e=>e.message;export const downloadBlob=(...args)=>globalThis.__communityDownload(...args);', moduleType: 'js' }; if (id.endsWith('.css')) return { code: '', moduleType: 'js' } },
  }] })
  try { await bundle.write({ dir: folder, format: 'esm', entryFileNames: '[name].mjs' }) } finally { await bundle.close() }
  Pdm = (await import(pathToFileURL(resolve(folder, 'CadPdmPanel.mjs')))).default
  Standard = (await import(pathToFileURL(resolve(folder, 'CadStandardPartPanel.mjs')))).default
  Community = (await import(pathToFileURL(resolve(folder, 'CadCommunityPanel.mjs')))).default
  ImportBody = (await import(pathToFileURL(resolve(folder, 'CadImportBodyPanel.mjs')))).default
})
after(async () => { await rm(folder, { recursive: true, force: true }); delete globalThis.__panelHooks; delete globalThis.__pdmClient; delete globalThis.__importClient; delete globalThis.__communityClient; delete globalThis.__communityDownload })
function harness(Component, initial) {
  let props = initial, slots = [], cursor = 0, pending = [], dirty = true, tree
  globalThis.__panelHooks = {
    useState(value) { const i = cursor++; slots[i] ||= { value: typeof value === 'function' ? value() : value }; return [slots[i].value, next => { const value = typeof next === 'function' ? next(slots[i].value) : next; if (!Object.is(value, slots[i].value)) { slots[i].value = value; dirty = true } }] },
    useRef(value) { return slots[cursor++] ||= { current: value } },
    useEffect(fn, deps) { const i = cursor++, old = slots[i]; if (!old || deps.some((v, index) => !Object.is(v, old.deps[index]))) { slots[i] = { deps, cleanup: old?.cleanup }; pending.push(() => { slots[i].cleanup?.(); slots[i].cleanup = fn() }) } },
  }
  const render = () => { let count = 0; do { dirty = false; cursor = 0; tree = Component(props); while (pending.length) pending.shift()(); assert.ok(++count < 25) } while (dirty) }
  const nodes = (value = tree) => React.isValidElement(value) ? [value, ...React.Children.toArray(value.props.children).flatMap(nodes)] : []
  const text = value => React.isValidElement(value) ? React.Children.toArray(value.props.children).map(text).join('') : String(value ?? '')
  const find = predicate => { const found = nodes().find(predicate); assert.ok(found, 'expected panel control'); return found }
  const settle = async () => { for (let i = 0; i < 6; i++) { await new Promise(resolve => setImmediate(resolve)); render() } }
  const input = (label, value) => { find(node => node.props['aria-label'] === label).props.onChange({ target: { value } }); render() }
  const submit = () => { find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); render() }
  const click = label => { find(node => node.type === 'button' && text(node) === label).props.onClick(); render() }
  render()
  return { settle, input, submit, click, find, text: () => text(tree), update(patch) { props = { ...props, ...patch }; render() }, unmount() { slots.forEach(slot => slot?.cleanup?.()) } }
}
const record = { id: 'feature-exact', revision: 2, status: 'built', name: '当前实体' }
const success = body => ({ source: { featureId: body.featureId, revision: body.revision }, document: { project_id: body.projectId }, version: { revision: 1 }, engineeringDesignId: 'actual-import' })
const api = extra => { globalThis.__pdmClient = { projects: async () => ({ items: [{ id: 'p1', name: '项目一', canWrite: true }, { id: 'p2', name: '项目二', canWrite: true }] }), documents: async () => ({ items: [] }), ...extra } }

test('PDM user chooses project/name, token refresh keeps draft and uncertain save retries exact request ID', async () => {
  const calls = [], saved = []; api({ save: async (token, body) => { calls.push({ token: token(), body }); if (calls.length === 1) throw new Error('网络超时，可以重试'); return success(body) } })
  const ui = harness(Pdm, { record, token: 'old', accountKey: 'alice', onSaved: value => saved.push(value) })
  try {
    await ui.settle(); ui.input('PDM 项目', 'p2'); await ui.settle(); ui.input('PDM 保存名称', '  精确图纸  ')
    ui.update({ token: 'renewed' }); await ui.settle(); assert.equal(ui.find(n => n.props['aria-label'] === 'PDM 保存名称').props.value, '  精确图纸  ')
    ui.submit(); await ui.settle(); assert.match(ui.text(), /网络超时/); assert.equal(saved.length, 0)
    ui.submit(); await ui.settle(); assert.equal(calls.length, 2); assert.equal(calls[0].body.requestId, calls[1].body.requestId)
    assert.deepEqual({ ...calls[1].body, requestId: undefined }, { projectId: 'p2', name: '精确图纸', featureId: record.id, revision: 2, expectedCurrentRevision: 0, requestId: undefined })
    assert.equal(calls[1].token, 'renewed'); assert.equal(saved.length, 1); assert.match(ui.text(), /PDM v1 和工程库/)
  } finally { ui.unmount() }
})

test('PDM does not publish mismatched or stale account/source results and cancels pending work on switch', async () => {
  let release, signal; const saved = []; api({ save: (_token, body, abortSignal) => { signal = abortSignal; return new Promise(resolve => { release = () => resolve(success(body)) }) } })
  const ui = harness(Pdm, { record, token: 'alice', accountKey: 'alice', onSaved: result => saved.push(result) })
  try {
    await ui.settle(); ui.submit(); ui.update({ token: 'bob', accountKey: 'bob', record: { ...record, id: 'other' } }); assert.equal(signal.aborted, true)
    release(); await ui.settle(); assert.equal(saved.length, 0); assert.doesNotMatch(ui.text(), /已将 r/)
    globalThis.__pdmClient.save = async (_token, body) => ({ ...success(body), source: { featureId: 'wrong', revision: 2 } })
    ui.submit(); await ui.settle(); assert.equal(saved.length, 0); assert.match(ui.text(), /来源版本不一致/)
  } finally { ui.unmount() }
})

test('PDM drafts cannot claim saved geometry; opening selects one immutable PDM version', async () => {
  const opened = []; api({ documents: async () => ({ items: [{ document: { id: 'doc', name: '版本图' }, versions: [{ id: 'exact-v3', revision: 3, source: { revision: 7 } }] }] }) })
  const ui = harness(Pdm, { record: { ...record, status: 'draft' }, token: 'alice', accountKey: 'alice', onOpen: value => opened.push(value) })
  try {
    await ui.settle(); assert.equal(ui.find(n => n.type === 'button' && n.props.type === 'submit').props.disabled, true)
    ui.click('打开此版本'); await ui.settle(); assert.deepEqual(opened, [{ versionId: 'exact-v3', name: '版本图', sourceRevision: 7 }])
  } finally { ui.unmount() }
})

test('standard-part form validates coordinates and passes editable compound geometry once to parent transaction', async () => {
  const changed = []; let finish
  const plan = { version: 'cad-plan-v1', units: 'mm', parameters: {}, features: [{ id: 'original', op: 'box', size: [20, 16, 10] }], result: 'original' }
  const ui = harness(Standard, { plan, onApply: (value, info) => { changed.push({ value, info }); return new Promise(resolve => { finish = resolve }) } })
  try {
    ui.input('标准件位置 X', ''); ui.submit(); assert.match(ui.text(), /三个位置坐标/); assert.equal(changed.length, 0)
    ui.input('标准件位置 X', '40.5'); ui.input('标准件规格', 'socket-m6-20'); ui.input('标准件轴向', 'Y'); ui.submit(); ui.submit()
    assert.equal(changed.length, 1); assert.deepEqual(changed[0].value.features[0], plan.features[0]); assert.equal(changed[0].value.features.at(-1).op, 'compound')
    const geometry = changed[0].value.features.find(f => f.op === 'standard_part'); assert.equal(geometry.catalogId, 'socket-m6-20'); assert.equal(changed[0].value.parameters[geometry.dimensions.pitch].value, 1)
    finish(); await ui.settle(); assert.equal(ui.find(n => n.type === 'button' && n.props.type === 'submit').props.disabled, false)
  } finally { ui.unmount() }
})

const importAsset = { id: 'asset_' + 'a'.repeat(32), sha256: 'b'.repeat(64), name: '真实源体', originalFormat: 'step', inspection: { valid: true, solidCount: 1 } }
const emptyPlan = { version: 'cad-plan-v1', units: 'mm', parameters: {}, features: [], result: '' }
const chooseImport = (ui, name = 'source.step') => { ui.find(n => n.props['aria-label'] === '实体源文件').props.onChange({ target: { files: [new File(['ISO-10303-21;'], name)] } }); ui.update({}) }

test('real import form uses file bytes, refreshed credentials, exact retry ID and preserves old bodies', async () => {
  const calls = [], applied = []
  globalThis.__importClient = { upload: async (token, file, options) => { calls.push({ token: token(), file, options }); if (calls.length === 1) throw new Error('网络连接中断'); return importAsset } }
  const plan = { ...emptyPlan, features: [{ id: 'old', op: 'box', size: [20,16,10] }], result: 'old' }
  const ui = harness(ImportBody, { plan, token: 'old-token', accountKey: 'alice', onApply: (value, info) => applied.push({ value, info }) })
  try {
    chooseImport(ui); ui.input('导入实体名称', '外部原始 STEP'); ui.input('导入位移 X', '40.5'); ui.submit(); await ui.settle()
    assert.match(ui.text(), /网络连接中断/); ui.update({ token: 'new-token' }); ui.submit(); await ui.settle()
    assert.equal(calls.length, 2); assert.equal(calls[0].options.requestId, calls[1].options.requestId)
    assert.equal(calls[1].token, 'new-token'); assert.equal(calls[1].file.name, 'source.step'); assert.equal(await calls[1].file.text(), 'ISO-10303-21;')
    assert.equal(applied.length, 1); assert.deepEqual(applied[0].value.features[0], plan.features[0])
    assert.equal(applied[0].value.features[1].op, 'import_step'); assert.equal(applied[0].value.features.at(-1).op, 'compound')
    assert.equal(applied[0].info.assetId, importAsset.id)
    const translate = applied[0].value.features.find(f => f.op === 'translate'); assert.equal(applied[0].value.parameters[translate.vector[0]].value, 40.5)
  } finally { ui.unmount() }
})

test('import account/document changes abort pending uploads and never apply a late body to another file', async () => {
  let resolveUpload, signal, closed = 0; const applied = []
  globalThis.__importClient = { upload: (_token, _file, _body, abort) => { signal = abort; return new Promise(resolve => { resolveUpload = resolve }) } }
  const ui = harness(ImportBody, { plan: emptyPlan, token: 'alice', accountKey: 'alice', onApply: plan => applied.push(plan), onClose: () => { closed++ } })
  try {
    chooseImport(ui); ui.submit(); assert.equal(signal.aborted, false)
    ui.update({ token: 'bob', accountKey: 'bob' }); assert.equal(signal.aborted, true); resolveUpload(importAsset); await ui.settle()
    assert.equal(applied.length, 0); assert.equal(ui.find(n => n.props['aria-label'] === '导入实体名称').props.value, '')
    ui.submit(); await ui.settle(); assert.match(ui.text(), /请选择 STEP/)
    chooseImport(ui, 'mesh.stl'); ui.input('STL 原始单位', 'inch'); ui.submit()
    ui.update({ plan: { ...emptyPlan, name: 'another document' } }); assert.equal(signal.aborted, true)
    resolveUpload(importAsset); await ui.settle(); assert.equal(applied.length, 0)
    chooseImport(ui); ui.submit(); ui.click('取消导入'); assert.equal(signal.aborted, true); assert.equal(closed, 1)
    resolveUpload(importAsset); await ui.settle(); assert.equal(applied.length, 0)
  } finally { ui.unmount() }
})

test('invalid geometry/import positions never apply and a new selected file gets a new retry request', async () => {
  const calls = [], applied = []
  globalThis.__importClient = { upload: async (_token, _file, body) => { calls.push(body); return { ...importAsset, inspection: { valid: false } } } }
  const ui = harness(ImportBody, { plan: emptyPlan, token: 'alice', accountKey: 'alice', onApply: value => applied.push(value) })
  try {
    chooseImport(ui, 'mesh.stl'); ui.input('STL 原始单位', 'cm'); ui.input('导入位移 X', ''); ui.submit(); assert.equal(calls.length, 0)
    ui.input('导入位移 X', '0'); ui.submit(); await ui.settle(); assert.match(ui.text(), /真实实体校验/); assert.equal(calls[0].units, 'cm')
    chooseImport(ui, 'mesh.stl'); ui.submit(); await ui.settle(); assert.notEqual(calls[0].requestId, calls[1].requestId); assert.equal(applied.length, 0)
  } finally { ui.unmount() }
})

const publicResource = { id: 'resource_' + 'a'.repeat(32), name: '共享圆角件', description: '参数化示例', status: 'published', sharing: 'public-copy-download', canWithdraw: true, inspection: { solidCount: 1 } }
const communityApi = extra => { globalThis.__communityClient = { list: async () => ({ items: [] }), ...extra } }
const check = (ui, label, checked) => { ui.find(node => node.props['aria-label'] === label).props.onChange({ target: { checked } }); ui.update({}) }

test('community publication requires explicit consent for exact saved version and retains request ID after uncertain failure', async () => {
  const calls = []
  communityApi({ publish: async (token, body) => { calls.push({ token: token(), body }); if (calls.length === 1) throw new Error('网络超时'); return { ...publicResource, name: body.name } } })
  const ui = harness(Community, { record, token: 'old', accountKey: 'author', initialMode: 'publish' })
  try {
    await ui.settle(); ui.input('社区资源名称', '公开新模型'); ui.input('社区资源说明', '真实尺寸')
    ui.submit(); await ui.settle(); assert.equal(calls.length, 0); assert.equal(ui.find(n => n.props['aria-label'] === '确认公开共享模型').props.checked, false)
    check(ui, '确认公开共享模型', true); ui.submit(); await ui.settle(); assert.match(ui.text(), /网络超时/)
    ui.update({ token: 'renewed' }); ui.submit(); await ui.settle()
    assert.equal(calls.length, 2); assert.equal(calls[0].body.requestId, calls[1].body.requestId); assert.equal(calls[1].token, 'renewed')
    assert.equal(calls[1].body.featureId, record.id); assert.equal(calls[1].body.revision, 2); assert.equal(calls[1].body.consent, 'public-copy-download')
    assert.match(ui.text(), /已公开发布/)
    ui.click('发布当前模型'); assert.equal(ui.find(n => n.props['aria-label'] === '确认公开共享模型').props.checked, false)
    check(ui, '确认公开共享模型', true); ui.update({ record: { ...record, revision: 3 } })
    assert.equal(ui.find(n => n.props['aria-label'] === '确认公开共享模型').props.checked, false)
  } finally { ui.unmount() }
})

test('community draft and late account result cannot publish another source or keep its disclosure', async () => {
  let release, signal
  communityApi({ publish: (_token, body, abort) => { signal = abort; return new Promise(resolve => { release = () => resolve({ ...publicResource, name: body.name }) }) } })
  const ui = harness(Community, { record: { ...record, status: 'draft' }, token: 'a', accountKey: 'a', initialMode: 'publish' })
  try {
    check(ui, '确认公开共享模型', true); ui.submit(); assert.equal(signal, undefined)
    ui.update({ record }); ui.submit(); assert.equal(signal.aborted, false)
    ui.update({ token: 'b', accountKey: 'b', record: { ...record, id: 'another' } }); assert.equal(signal.aborted, true)
    release(); await ui.settle(); assert.doesNotMatch(ui.text(), /已公开发布/)
    assert.equal(ui.find(n => n.props['aria-label'] === '确认公开共享模型').props.checked, false)
  } finally { ui.unmount() }
})

test('community search, real blob downloads, guarded copy and deliberate withdrawal use the selected resource only', async () => {
  const searches = [], downloads = [], opens = [], withdrawn = []
  globalThis.__communityDownload = (...args) => downloads.push(args)
  communityApi({ list: async (_token, query) => { searches.push(query); return { items: [publicResource] } }, artifact: async (_token, id, kind) => { assert.equal(id, publicResource.id); return new Blob([kind === 'step' ? 'ISO-10303-21;' : 'glTF']) }, withdraw: async (_token, id) => { withdrawn.push(id); return { ...publicResource, status: 'withdrawn' } } })
  const ui = harness(Community, { record, token: 'author', accountKey: 'author', onOpen: value => opens.push(value) })
  try {
    await ui.settle(); ui.input('搜索社区资源', '  圆角  '); ui.submit(); await ui.settle(); assert.equal(searches.at(-1).query, '圆角')
    ui.find(n => n.props.className === 'cad-community-resource-title').props.onClick(); ui.update({})
    ui.click('下载 STEP'); await ui.settle(); assert.equal(downloads.length, 1); assert.equal(await downloads[0][0].text(), 'ISO-10303-21;')
    ui.click('作为独立副本编辑'); await ui.settle(); assert.deepEqual(opens, [{ resourceId: publicResource.id, name: publicResource.name }])
    ui.click('撤下此资源'); assert.equal(withdrawn.length, 0); ui.click('取消撤下'); assert.equal(withdrawn.length, 0)
    ui.click('撤下此资源'); ui.click('确认撤下'); await ui.settle(); assert.deepEqual(withdrawn, [publicResource.id]); assert.match(ui.text(), /新的下载和副本创建已停止/)
  } finally { ui.unmount() }
})
