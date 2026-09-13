import test from 'node:test'
import assert from 'node:assert/strict'
import { adminPath, readAppRoute } from './adminNavigation.js'

test('admin navigation preserves deep links and admits only known query fields', () => {
  const path = adminPath('tasks', { runId: 'cad/a?b', status: 'failed', token: 'never-persist', ownerId: '客户 1' })
  const url = new URL(path, 'https://cad.example')
  assert.deepEqual(readAppRoute(url), { isAdmin: true, section: 'tasks', valid: true, params: { runId: 'cad/a?b', status: 'failed', ownerId: '客户 1' } })
  assert.ok(!path.includes('never-persist'))
})

test('unknown admin sections cannot silently display an authorized page', () => {
  assert.equal(readAppRoute({ pathname: '/admin/unknown' }).valid, false)
  assert.equal(readAppRoute({ pathname: '/administrator' }).isAdmin, false)
  assert.equal(readAppRoute({ pathname: '/admin/' }).section, '')
  assert.equal(readAppRoute({ pathname: '/admin/account' }).valid, true)
  assert.throws(() => adminPath('//example.com'))
})
