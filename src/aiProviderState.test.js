import test from 'node:test'
import assert from 'node:assert/strict'
import { configuredAiProvider, workspaceAiProvider, aiProviderPresentation } from './aiProviderState.js'

const codex = { name: 'codex-cli', mode: 'codex', model: 'gpt-6-astra', reasoningEffort: 'high', configured: true, streaming: false, authentication: 'cli-managed' }
const relay = { mode: 'remote', model: 'gpt-5.6-sol', reasoningEffort: 'high', configured: true, streaming: true }
const serviceStatus = { ...relay, cadProvider: codex }

test('CAD entry points select current CAD configuration, while recipe text edits keep the relay', () => {
  const conversation = { serviceStatus, status: relay, providerRoute: 'legacy' }
  for (const model of [null, { kind: '' }, { kind: 'feature_model', agentRun: { provider: relay } }]) {
    assert.equal(workspaceAiProvider(model, [], conversation), codex)
  }
  assert.equal(workspaceAiProvider({ kind: 'shaft' }, [{}], conversation), codex)
  assert.equal(workspaceAiProvider({ kind: 'shaft' }, [], conversation), relay)
  assert.equal(configuredAiProvider(conversation, 'legacy'), serviceStatus)
  assert.equal(workspaceAiProvider({ kind: 'feature_model', agentRun: { provider: codex } }, [], { serviceStatus: relay }), relay)
  assert.equal(workspaceAiProvider({ kind: '' }, [], { status: serviceStatus }), codex)
})

test('configuration loading and CLI configuration never claim remote streaming or authenticated online status', () => {
  assert.equal(aiProviderPresentation(null).configurationHint, '')
  assert.equal(aiProviderPresentation(null).ready, false)
  const available = aiProviderPresentation(codex)
  assert.equal(available.label, '本机 Codex · 逐步建模')
  assert.doesNotMatch(available.label, /中转站|SSE|在线|已认证/)
  const unavailable = aiProviderPresentation({ ...codex, configured: false })
  assert.equal(unavailable.ready, false)
  assert.match(unavailable.configurationHint, /本机.*安装和配置/)
  assert.doesNotMatch(unavailable.configurationHint, /中转站|平台|token/)
  assert.equal(aiProviderPresentation(relay).label, '中转站在线 · SSE 流式')
})

test('runtime failure overrides configured availability and successful retries do not remain failed', () => {
  const runtime = { ...codex, attempts: 2, lastErrorCode: 'timeout' }
  const failed = aiProviderPresentation(runtime, { error: '本轮建模未完成' })
  assert.equal(failed.ready, false)
  assert.equal(failed.failed, true)
  assert.match(failed.label, /响应超时.*2 次/)
  assert.equal(aiProviderPresentation(runtime).failed, false)
  const legacyFailure = aiProviderPresentation({ ...relay, mode: 'local-fallback', lastErrorCode: 'transport', attempts: 3 })
  assert.equal(legacyFailure.degraded, true)
  assert.match(legacyFailure.label, /本次失败.*连接中断.*3 次/)
})

test('normalized CAD provider errors are shown in Chinese without claiming the engine is ready', () => {
  for (const [code, label] of Object.entries({ provider_failure: '建模服务未完成', upstream_error: '上游服务返回错误', unexpected_tool: '执行超出允许范围' })) {
    const display = aiProviderPresentation({ ...codex, lastErrorCode: code }, { error: '本轮未完成' })
    assert.equal(display.ready, false)
    assert.equal(display.failureText, label)
    assert.equal(display.label, `本机 Codex · ${label}`)
  }
})
