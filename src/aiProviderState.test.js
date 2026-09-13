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

test('server Codex presentation identifies the server without implying a client installation or login', () => {
  const server = { ...codex, deployment: 'server', binaryAvailable: true }
  const available = aiProviderPresentation(server)
  assert.equal(available.label, '服务器 Codex · 逐步建模')
  assert.equal(available.ready, true)
  assert.doesNotMatch(available.label, /本机|SSE|在线|已认证/)
  const unavailable = aiProviderPresentation({ ...server, configured: false })
  assert.equal(unavailable.label, '服务器 Codex · 尚未就绪')
  assert.equal(unavailable.ready, false)
  assert.match(unavailable.configurationHint, /服务器 Codex.*管理员.*服务器安装和配置/)
  assert.doesNotMatch(unavailable.configurationHint, /本机|中转站|token/)
  for (const [code, message] of Object.entries({ launch_failed: '服务器进程未启动', process_failed: '服务器进程未完成', timeout: '响应超时' })) {
    const failed = aiProviderPresentation({ ...server, lastErrorCode: code }, { error: '建模未完成' })
    assert.equal(failed.label, `服务器 Codex · ${message}`)
    assert.equal(failed.ready, false)
    assert.doesNotMatch(failed.label, /本机/)
  }
})

test('local and older Codex provider records preserve the local deployment labels', () => {
  for (const deployment of [undefined, 'local']) {
    const local = { ...codex, deployment }
    assert.equal(aiProviderPresentation(local).label, '本机 Codex · 逐步建模')
    const failed = aiProviderPresentation(local, { error: '未完成', failureCode: 'launch_failed' })
    assert.equal(failed.label, '本机 Codex · 本机进程未启动')
    assert.match(aiProviderPresentation({ ...local, configured: false }).configurationHint, /检查本机安装和配置/)
  }
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

test('CAD model calls are not presented as failed attempts or inferred retries', () => {
  const provider = { ...relay, attempts: 8, sourceReaderAttempts: 1, sourceSpatialAttempts: 1, lastErrorCode: 'timeout' }
  const display = aiProviderPresentation(provider, { route: 'cad', error: '建模计划响应超时' })
  assert.match(display.label, /本轮模型调用 8 次/)
  assert.doesNotMatch(display.label, /已尝试|重试|失败 8 次/)
  assert.equal(display.retryCount, 0)
  const retried = aiProviderPresentation({ ...provider, retryCount: 1 }, { route: 'cad', error: '建模计划响应超时' })
  assert.match(retried.label, /本轮模型调用 8 次.*自动重试 1 次/)
  assert.equal(retried.retryCount, 1)
  const zero = aiProviderPresentation({ ...provider, attempts: 0, retryCount: 0 }, { route: 'cad', error: '未完成', attempts: 8 })
  assert.doesNotMatch(zero.label, /调用|重试|已尝试/)
  for (const invalid of [-1, Infinity, 'bad', 1.5]) {
    const invalidCounts = aiProviderPresentation({ ...provider, attempts: invalid, retryCount: invalid }, { route: 'cad', error: '未完成' })
    assert.doesNotMatch(invalidCounts.label, /调用|重试|NaN|Infinity/)
  }
})

test('DWG preprocessing codes have source-specific Chinese labels and no invented AI calls', () => {
  for (const [code, label] of Object.entries({
    dxf_parse_failed: 'DWG 图纸数据解析失败',
    dwg_converter_unavailable: 'DWG 转换服务暂不可用',
    dwg_conversion_failed: 'DWG 转换失败',
    invalid_dwg_input: 'DWG 文件格式无法识别',
  })) {
    const display = aiProviderPresentation({ ...codex, deployment: 'server', lastErrorCode: code, attempts: 0 },
      { error: '图纸预处理失败', route: 'cad' })
    assert.equal(display.failureText, label)
    assert.equal(display.label, `服务器 Codex · ${label}`)
    assert.equal(display.ready, false)
    assert.equal(display.failureCode, code)
    assert.doesNotMatch(display.label, /登录|额度|损坏|token|模型调用|重试|已尝试/)
  }
})
