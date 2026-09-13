import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { adminPath, readAppRoute, navigateApplication } from './adminNavigation.js'
import { adminListParams, adminReadPath } from './adminOperationsClient.js'

const parse = path => readAppRoute(new URL(path, 'https://cad.example.test'))

test('next page and every operational filter survive navigation and history restoration', () => {
  const inputs = {
    customers: { q: '客户甲', active: 'false', offset: 40 },
    tasks: { ownerId: 'customer-b', status: 'failed', q: 'cad_run', offset: 20 },
    audit: { source: 'billing', actorId: 'finance-b', offset: 60 },
  }
  for (const [section, input] of Object.entries(inputs)) {
    const original = adminListParams(section, input)
    const route = parse(adminPath(section, original))
    assert.equal(route.valid, true)
    assert.deepEqual(adminListParams(section, route.params), original)
    assert.equal(adminReadPath(section, route.params), adminReadPath(section, original))
  }
  assert.deepEqual(parse(adminPath('support', { ticketId: 'SUP-local-only' })).params, { ticketId: 'SUP-local-only' })
  for (const path of ['/admin/tasks/extra', '/admin//tasks', '/admin/customers/record/extra']) assert.equal(parse(path).valid, false)
})

test('application navigation is same-origin, preserves queries, and publishes updates once', () => {
  const previous = globalThis.window
  const states = [], events = []
  const location = { origin: 'https://cad.example.test', pathname: '/', search: '' }
  const apply = (method, _state, _title, path) => { states.push({ method, path }); const url = new URL(path, location.origin); location.pathname=url.pathname; location.search=url.search }
  globalThis.window = { location, history: { pushState: (...args) => apply('push', ...args), replaceState: (...args) => apply('replace', ...args) }, dispatchEvent: event => events.push(event.type) }
  try {
    const path = adminPath('customers', { active: 'false', offset: 20 })
    navigateApplication(path)
    assert.deepEqual(readAppRoute(location).params, { active: 'false', offset: '20' })
    navigateApplication(path)
    assert.equal(states.length, 1)
    navigateApplication('/', { replace: true })
    assert.deepEqual(states.map(row=>row.method), ['push','replace'])
    assert.deepEqual(events, ['joyniu:navigate','joyniu:navigate'])
    assert.throws(()=>navigateApplication('https://outside.example/admin'))
    assert.throws(()=>navigateApplication('//outside.example/admin'))
  } finally { globalThis.window=previous }
})

test('actual admin shell admits only role links, denies unauthorized deep links, and has separate login', async () => {
  const directory=await mkdtemp(resolve('node_modules/.admin-integration-'))
  const bundle=await rolldown({ input:resolve('src/AdminShell.jsx'), external:['react','react/jsx-runtime','qrcode'], transform:{jsx:{runtime:'automatic'}}, plugins:[{name:'no-css',load:id=>id.endsWith('.css')?{code:'',moduleType:'js'}:null}] })
  try {
    await bundle.write({dir:directory,format:'esm'})
    const {default:AdminShell}=await import(pathToFileURL(resolve(directory,'AdminShell.js')).href)
    const account=role=>({session:{access_token:'test-local-token',user:{id:role,roles:[role],active:true}},logout:async()=>{}})
    const render=(role,path)=>renderToStaticMarkup(React.createElement(AdminShell,{account:role?account(role):{},route:parse(path)}))
    const navLinks=html=>[...html.matchAll(/href="(\/admin\/[a-z]+)"/g)].map(match=>match[1])
    assert.deepEqual(navLinks(render('finance','/admin')), ['/admin/overview','/admin/customers','/admin/orders','/admin/credits','/admin/settlements','/admin/refunds','/admin/invoices','/admin/usage'])
    assert.deepEqual(navLinks(render('support','/admin')), ['/admin/customers','/admin/tasks','/admin/support'])
    assert.deepEqual(navLinks(render('ops','/admin')), ['/admin/overview','/admin/customers','/admin/tasks','/admin/system'])
    const refused=render('finance','/admin/policy')
    assert.match(refused,/此页面需要额外权限/)
    assert.doesNotMatch(refused,/创建计费规则|新建规则/)
    assert.match(render('designer','/admin'),/当前账号没有后台权限/)
    assert.match(render('admin','/admin/missing'),/后台页面不存在/)
    const login=render(null,'/admin/billing')
    assert.match(login,/登录管理后台/)
    assert.match(login,/账号或邮箱<input[^>]+type="text"/)
    assert.doesNotMatch(login,/注册账号/)
  } finally {await bundle.close();await rm(directory,{recursive:true,force:true})}
})
