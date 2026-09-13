import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { webcrypto } from 'node:crypto'
import { rolldown } from 'rolldown'
import React from 'react'

let directory, Composer
const originalFetch = globalThis.fetch
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const requirement = '两个边长 20 mm 的立方体，A 位于 (0,0,0)，B 位于 (30,0,0)，角度均为零，不增加配合。'
const response = value => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
const completed = (id = 'assembly-1') => ({ id, status: 'review_required', requirements: [requirement], parts: [] })

before(async () => {
  // Keep the real engineering HTTP client, but never contact a server or AI provider.
  globalThis.fetch = (...args) => globalThis.__assemblyFetch(...args)
  directory = await mkdtemp(resolve('node_modules/.engineering-submit-'))
  const bundle = await rolldown({
    input: resolve('src/EngineeringWorkspace.jsx'),
    external: ['react/jsx-runtime', 'three', 'three/examples/jsm/controls/OrbitControls.js', 'three/examples/jsm/loaders/GLTFLoader.js'],
    transform: { jsx: { runtime: 'automatic' } },
    plugins: [{
      name: 'assembly-component-harness',
      resolveId(source) { if (source === 'react') return '\0assembly-hooks' },
      async load(id) {
        if (id === '\0assembly-hooks') return { code: ['useState', 'useRef', 'useEffect', 'useLayoutEffect', 'useMemo'].map(name => `export const ${name}=(...args)=>globalThis.__assemblyHooks.${name}(...args);`).join('\n'), moduleType: 'js' }
        if (id === resolve('src/EngineeringWorkspace.jsx')) return { code: `${await readFile(id, 'utf8')}\nexport { AssemblyAIComposer as TestAssemblyAIComposer };`, moduleType: 'jsx' }
        if (id.endsWith('.css')) return { code: '', moduleType: 'js' }
      },
    }],
  })
  try { await bundle.write({ file: resolve(directory, 'composer.mjs'), format: 'esm' }) }
  finally { await bundle.close() }
  Composer = (await import(pathToFileURL(resolve(directory, 'composer.mjs')).href)).TestAssemblyAIComposer
})
after(async () => {
  globalThis.fetch = originalFetch
  delete globalThis.__assemblyFetch
  delete globalThis.__assemblyHooks
  if (directory) await rm(directory, { recursive: true, force: true })
})

// Execute the actual component's events, effects and request client; only React's
// hook scheduling is local so these regressions need no browser or paid AI call.
function harness(post) {
  const slots = [], effects = [], calls = []
  let cursor = 0, dirty = true, tree
  globalThis.__assemblyFetch = async (url, options) => {
    const pathname = new URL(url).pathname
    assert.equal(options.headers.Authorization, 'Bearer http-test-owner')
    if (options.method === 'GET') {
      assert.equal(pathname, '/api/cad/designs/assembly-jobs')
      return response({ items: [] })
    }
    assert.equal(pathname, '/api/cad/designs/ai-assemblies')
    assert.equal(options.method, 'POST')
    assert.equal(options.headers['Content-Type'], 'application/json')
    const body = JSON.parse(options.body)
    calls.push(body)
    return post(body, options)
  }
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!slots[index]) slots[index] = { value: typeof initial === 'function' ? initial() : initial }
      return [slots[index].value, value => {
        const next = typeof value === 'function' ? value(slots[index].value) : value
        if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true }
      }]
    },
    useRef(initial) { const index = cursor++; return slots[index] ||= { current: initial } },
    useEffect(fn, deps) {
      const index = cursor++, previous = slots[index]
      if (!previous || !deps || deps.some((value, i) => !Object.is(value, previous.deps?.[i]))) {
        slots[index] = { deps, cleanup: previous?.cleanup }
        effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = fn() })
      }
    },
  }
  globalThis.__assemblyHooks = hooks
  const render = () => {
    let rounds = 0
    do {
      dirty = false; cursor = 0
      tree = Composer({ token: 'http-test-owner', accountKey: 'owner-1', fileId: 'file-1', onReady: () => assert.fail('No generated design should open in these submission tests') })
      while (effects.length) effects.shift()()
      assert.ok(++rounds < 30, 'component should settle')
    } while (dirty)
    return tree
  }
  const flush = async () => { for (let i = 0; i < 4; i++) { await new Promise(resolve => setImmediate(resolve)); render() } }
  const nodes = (value = tree) => React.isValidElement(value) ? [value, ...React.Children.toArray(value.props.children).flatMap(nodes)] : []
  const text = value => React.isValidElement(value) ? React.Children.toArray(value.props.children).map(text).join('') : String(value ?? '')
  const find = predicate => { const node = nodes().find(predicate); assert.ok(node, 'expected component control'); return node }
  const button = () => find(node => node.type === 'button' && node.props.className === 'eng-primary')
  const textarea = () => find(node => node.type === 'textarea')
  const input = value => { textarea().props.onChange({ target: { value } }); render() }
  const submit = async () => { await button().props.onClick(); await flush() }
  const unmount = () => { for (const slot of slots) slot?.cleanup?.(); delete globalThis.__assemblyHooks; delete globalThis.__assemblyFetch }
  render()
  return { calls, render, flush, nodes, text: () => text(tree), button, textarea, input, submit, unmount }
}

async function withHttpCrypto(run) {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  // This is the Web Crypto capability available on the production HTTP origin.
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues: bytes => webcrypto.getRandomValues(bytes) } })
  try { assert.equal(globalThis.crypto.randomUUID, undefined); await run() }
  finally { if (original) Object.defineProperty(globalThis, 'crypto', original); else delete globalThis.crypto }
}

test('HTTP AI assembly submit creates a valid id, prevents duplicate clicks and releases busy after success', async () => withHttpCrypto(async () => {
  let resolvePost
  const ui = harness(() => new Promise(resolve => { resolvePost = resolve }))
  try {
    await ui.flush(); ui.input(requirement)
    assert.equal(ui.button().props.disabled, false)
    const click = ui.button().props.onClick
    const pending = click()
    ui.render()
    assert.equal(ui.textarea().props.disabled, true)
    assert.match(ui.text(), /正在提交…/)
    await click()
    assert.equal(ui.calls.length, 1, 'rapid repeated events must send only one billable request')
    assert.match(ui.calls[0].requestId, uuidPattern)
    assert.equal(ui.calls[0].message, requirement)
    assert.equal(ui.calls[0].fileId, 'file-1')
    resolvePost(response(completed()))
    await pending; await ui.flush()
    assert.equal(ui.textarea().props.disabled, false)
    assert.equal(ui.textarea().props.value, '')
    assert.doesNotMatch(ui.text(), /正在提交…/)
    assert.match(ui.text(), /已生成，请检查设计/)
    ui.input('新的完整装配需求')
    assert.equal(ui.button().props.disabled, false, 'success must release the submitting state')
  } finally { ui.unmount() }
}))

test('HTTP lost-response retry preserves the same payload and request id without leaving the form busy', async () => withHttpCrypto(async () => {
  const serverJobs = new Map()
  let loseResponse = true
  const ui = harness(body => {
    if (!serverJobs.has(body.requestId)) serverJobs.set(body.requestId, completed())
    if (loseResponse) { loseResponse = false; throw new TypeError('Connection closed after server accepted request') }
    return response(serverJobs.get(body.requestId))
  })
  try {
    await ui.flush(); ui.input(requirement); await ui.submit()
    assert.equal(ui.textarea().props.value, requirement, 'failed delivery must preserve the requirement')
    assert.equal(ui.textarea().props.disabled, false)
    assert.equal(ui.button().props.disabled, false)
    assert.match(ui.text(), /无法连接工程服务/)
    assert.ok(ui.nodes().some(node => node.props.role === 'alert'))
    await ui.submit()
    assert.equal(ui.calls.length, 2)
    assert.deepEqual(ui.calls[1], ui.calls[0], 'retry must reuse the original idempotency identity')
    assert.match(ui.calls[0].requestId, uuidPattern)
    assert.equal(serverJobs.size, 1, 'a lost response must not create another AI job')
    assert.equal(ui.textarea().props.disabled, false)
    assert.doesNotMatch(ui.text(), /无法连接工程服务|正在提交…/)
  } finally { ui.unmount() }
}))

test('HTTP server rejection releases busy and an edited requirement uses a new request id', async () => withHttpCrypto(async () => {
  const ui = harness(() => new Response(JSON.stringify({ detail: '积分不足，请先查看账户余额。' }), { status: 402 }))
  try {
    await ui.flush(); ui.input(requirement); await ui.submit()
    assert.match(ui.text(), /积分不足/)
    assert.equal(ui.button().props.disabled, false)
    assert.equal(ui.textarea().props.disabled, false)
    ui.input(`${requirement} 改为边长 25 mm。`)
    await ui.submit()
    assert.equal(ui.calls.length, 2)
    assert.notEqual(ui.calls[0].requestId, ui.calls[1].requestId, 'a changed payload must not collide with an earlier request')
    for (const body of ui.calls) assert.match(body.requestId, uuidPattern)
    assert.equal(ui.textarea().props.disabled, false)
    assert.equal(ui.button().props.disabled, false)
  } finally { ui.unmount() }
}))

test('request-id initialization errors are displayed and release the lock so submission can recover', async () => withHttpCrypto(async () => {
  const workingCrypto = globalThis.crypto
  const ui = harness(() => response(completed()))
  try {
    await ui.flush(); ui.input(requirement)
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues() { throw new Error('浏览器随机数服务暂不可用') } } })
    await ui.submit()
    assert.equal(ui.calls.length, 0, 'no HTTP call should start when request identity creation fails')
    assert.match(ui.text(), /浏览器随机数服务暂不可用/)
    assert.equal(ui.textarea().props.disabled, false)
    assert.equal(ui.button().props.disabled, false)
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: workingCrypto })
    await ui.submit()
    assert.equal(ui.calls.length, 1, 'the local submission lock must be released after initialization failure')
    assert.match(ui.calls[0].requestId, uuidPattern)
    assert.equal(ui.textarea().props.disabled, false)
    assert.doesNotMatch(ui.text(), /浏览器随机数服务暂不可用|正在提交…/)
  } finally { ui.unmount() }
}))
