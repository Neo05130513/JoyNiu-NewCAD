import test from 'node:test'
import assert from 'node:assert/strict'
import { readableError, notificationFor } from './workspaceFeedback.js'

test('authentication and network errors offer actionable messages', () => {
  assert.match(readableError({ status: 401, message: 'bearer token is required' }), /登录/)
  assert.match(readableError(new Error('Failed to fetch')), /连接服务/)
  assert.equal(notificationFor('AI 对话失败：bearer token is required').type, 'error')
  assert.equal(notificationFor('文件已保存').type, 'success')
  assert.equal(notificationFor('服务连接中', 'info').type, 'info')
})

test('invalid login credentials ask for correction rather than merely asking to log in', () => {
  for (const message of ['invalid credentials', 'invalid email or password', 'invalid username or password', 'incorrect password']) {
    assert.equal(readableError({ status: 401, message }), '账号或密码不正确，请重新输入。')
  }
  assert.equal(readableError({ status: 401, message: 'invalid token' }), '请先登录后再继续此操作。')
  assert.equal(notificationFor('登录失败：invalid credentials').type, 'error')
  assert.equal(notificationFor('账号或密码不正确，请重新输入。').type, 'error')
})

test('unfinished or malformed AI responses explain why the preview did not change', () => {
  assert.match(readableError('AI provider returned a non-completed response'), /模型保持原样/)
  assert.match(readableError('AI provider returned an invalid numeric parameter'), /本轮未修改模型/)
})
