import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { hasAdminPermission, billingAdminCapabilities } from './adminPermissions.js'
import { validateAccountRoles, canManageAccounts } from './adminUsersClient.js'

test('staff permissions separate finance writes, audit reads and customer support', () => {
  assert.deepEqual(billingAdminCapabilities({ roles: ['finance'] }), { read: true, manage: true, adjust: true, policy: false })
  assert.deepEqual(billingAdminCapabilities({ roles: ['auditor'] }), { read: true, manage: false, adjust: false, policy: false })
  for (const role of ['ops', 'support', 'designer', 'reviewer', 'manufacturing', 'viewer']) {
    assert.deepEqual(billingAdminCapabilities({ roles: [role] }), { read: false, manage: false, adjust: false, policy: false })
  }
  assert(hasAdminPermission({ roles: ['ops'] }, 'admin:task-manage'))
  assert(!hasAdminPermission({ roles: ['support'] }, 'admin:task-manage'))
  assert(hasAdminPermission({ roles: ['support'] }, 'support:manage'))
  assert.deepEqual(validateAccountRoles(['finance', 'ops', 'auditor', 'support']), ['auditor', 'finance', 'ops', 'support'])
  for (const role of ['finance', 'ops', 'support', 'auditor']) assert.equal(canManageAccounts({ roles: [role] }), false)
})

test('current server permissions and inactive state override stale role labels', () => {
  assert.equal(hasAdminPermission({ roles: ['admin'], permissions: [] }, 'billing:adjust'), false)
  assert.equal(hasAdminPermission({ roles: ['admin'], permissions: ['billing:read'] }, 'billing:adjust'), false)
  assert.equal(hasAdminPermission({ roles: ['admin'], permissions: ['*'], active: false }, 'billing:adjust'), false)
  assert.equal(hasAdminPermission(null, 'admin:overview'), false)
  assert.equal(hasAdminPermission({ permissions: ['*'] }, 'billing:policy'), true)
})

// Bundle the actual JSX with the project's existing bundler. No browser or server
// mutations occur; effects do not run during server rendering.
test('real management pages and refund controls match the business role matrix', async () => {
  const directory = await mkdtemp(resolve('node_modules/.rbac-render-'))
  const bundle = await rolldown({
    input: { billing: resolve('src/BillingWorkspace.jsx'), support: resolve('src/SupportWorkspace.jsx'), policy: resolve('src/AdminBillingPolicyWorkspace.jsx'), terms: resolve('src/CommercialTermsWorkspace.jsx'), users: resolve('src/AdminUsersWorkspace.jsx') },
    external: ['react', 'react/jsx-runtime', 'qrcode'],
    transform: { jsx: { runtime: 'automatic' } },
    plugins: [{
      name: 'rbac-render-fixtures',
      load: async id => {
        if (id.endsWith('.css')) return { code: '', moduleType: 'js' }
        if (id === resolve('src/BillingWorkspace.jsx')) return { code: (await readFile(id, 'utf8')) + '\nexport { Records as TestRecords };', moduleType: 'jsx' }
      },
    }],
  })
  try {
    await bundle.write({ dir: directory, format: 'esm' })
    const components = Object.fromEntries(await Promise.all(['billing', 'support', 'policy', 'terms', 'users'].map(async key => [key, await import(pathToFileURL(resolve(directory, `${key}.js`)).href)])))
    const render = (key, role, extra = {}) => renderToStaticMarkup(React.createElement(components[key].default, { account: { session: { access_token: 'local-test', user: { id: role, roles: [role], active: true } } }, admin: true, ...extra }))
    for (const role of ['ops', 'support', 'designer']) assert.match(render('billing', role), /当前账号没有管理权限/)
    const audit = render('billing', 'auditor')
    assert.match(audit, /全部订单/)
    assert.doesNotMatch(audit, /赠送 \/ 补偿 \/ 调账|登记线下充值|新增套餐/)
    const finance = render('billing', 'finance')
    assert.match(finance, /赠送 \/ 补偿 \/ 调账/)
    assert.match(finance, /登记线下充值/)
    assert.doesNotMatch(finance, /新增套餐/)
    assert.match(render('billing', 'admin'), /新增套餐/)
    assert.match(render('support', 'support'), /工单状态/)
    assert.match(render('support', 'finance'), /没有工单管理权限/)
    assert.match(render('policy', 'finance'), /没有计费规则管理权限/)
    assert.match(render('terms', 'ops'), /没有管理权限/)
    assert.match(render('users', 'finance'), /没有账号管理权限/)
    const props = { kind: 'refund-requests', page: { items: [{ id: 'r1', kind: 'refund', status: 'approved', data: { amountFen: 100 }, orderId: 'o1', ownerId: 'u1' }] }, admin: true, refundEnabled: true }
    const records = canManage => renderToStaticMarkup(React.createElement(components.billing.TestRecords, { ...props, canManage }))
    assert.match(records(true), /执行真实退款/)
    assert.doesNotMatch(records(false), /执行真实退款|查询退款结果|处理申请/)
    const offlineRefund = canManage => renderToStaticMarkup(React.createElement(components.billing.TestRecords, { ...props, canManage, page: { items: [{ ...props.page.items[0], data: { amountFen: 100, provider: 'offline' } }] } }))
    assert.match(offlineRefund(true), /登记已完成的线下退款/)
    assert.doesNotMatch(offlineRefund(false), /登记已完成的线下退款/)
  } finally { await bundle.close(); await rm(directory, { recursive: true, force: true }) }
})
