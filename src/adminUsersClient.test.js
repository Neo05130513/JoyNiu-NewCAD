import test from 'node:test'
import assert from 'node:assert/strict'
import { accountChangeBlock, adminUserPage, canManageAccounts, createAdminUsersClient, validateAccountRoles, validateNewAccountPassword } from './adminUsersClient.js'

test('role and password validation reject invalid role sets and mistyped passwords', () => {
  assert.deepEqual(validateAccountRoles(['designer', 'viewer', 'designer']), ['designer', 'viewer'])
  assert.throws(() => validateAccountRoles([]))
  assert.throws(() => validateAccountRoles(['superadmin']))
  assert.equal(validateNewAccountPassword('user-entered-password', 'user-entered-password'), 'user-entered-password')
  assert.throws(() => validateNewAccountPassword('tooShort', 'different'))
  assert.throws(() => validateNewAccountPassword('short', 'short'))
  assert.throws(() => validateNewAccountPassword('x'.repeat(257), 'x'.repeat(257)))
})

test('account administration protects current login and the last active administrator', () => {
  const admin = { id: 'admin-a', active: true, roles: ['admin'] }
  const inactive = { id: 'admin-b', active: false, roles: ['admin'] }
  assert.match(accountChangeBlock([admin, inactive], admin, { active: false }, 'admin-a'), /当前登录/)
  assert.match(accountChangeBlock([admin, inactive], admin, { roles: ['viewer'] }, 'admin-a'), /当前登录/)
  assert.match(accountChangeBlock([admin, inactive], admin, { active: false }, 'admin-b'), /至少/)
  assert.match(accountChangeBlock([admin, inactive], admin, { roles: ['viewer'] }, 'admin-b'), /至少/)
  assert.equal(accountChangeBlock([admin, { ...inactive, active: true }], admin, { active: false }, 'admin-b'), '')
  assert.equal(canManageAccounts({ permissions: ['user:manage'] }), true)
  assert.equal(canManageAccounts({ roles: ['designer'] }), false)
})

test('user filtering searches actual metadata and clamps pages after status changes', () => {
  const users = [{ id: 'u1', email: 'one@example.com', display_name: '设计师甲', roles: ['designer'], active: true }, { id: 'u2', email: 'two@example.com', roles: ['admin'], active: false }]
  assert.deepEqual(adminUserPage(users, '管理员').items, [users[1]])
  assert.equal(adminUserPage(users, '', 'active', 99).page, 0)
  assert.deepEqual(adminUserPage(users, 'u1').items, [users[0]])
  assert.equal(adminUserPage(users, 'nobody').total, 0)
})

test('admin client uses existing protected routes and never supplies a default password', async () => {
  const calls = []
  const client = createAdminUsersClient({ apiBase: 'https://cad.example.com/api/v1', fetchImpl: async (...args) => {
    calls.push(args)
    return { ok: true, status: 200, json: async () => ({ items: [] }) }
  } })
  await client.list('session-token')
  await client.active('session-token', 'user/a', false)
  await client.roles('session-token', 'user-a', ['viewer'])
  await client.resetPassword('session-token', 'user-a', 'entered-new-password')
  assert.match(calls[0][0], /include_inactive=true$/)
  assert.equal(calls[0][1].credentials, 'include')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer session-token')
  assert.match(calls[1][0], /user%2Fa\/active$/)
  assert.deepEqual(JSON.parse(calls[1][1].body), { active: false })
  assert.deepEqual(JSON.parse(calls[2][1].body), { roles: ['viewer'] })
  assert.deepEqual(JSON.parse(calls[3][1].body), { newPassword: 'entered-new-password' })
  assert.doesNotMatch(calls[3][1].body, /session-token/)
  await assert.rejects(client.list(''), error => error.status === 401)
  assert.equal(calls.length, 4)
})
