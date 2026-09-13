import { shouldUseCadAgent } from './cadAgentState.js'
import { drawingPreprocessFailure } from './drawingPreprocessFailure.js'

export function configuredAiProvider(conversation = {}, route = 'cad') {
  // Service configuration is independent of a saved model's historical run.
  const service = conversation.serviceStatus || (!conversation.providerRoute ? conversation.status : null)
  return route === 'cad' ? service?.cadProvider || service || null : service || null
}

export function workspaceAiProvider(model, files = [], conversation = {}) {
  const route = shouldUseCadAgent(model, files) ? 'cad' : 'legacy'
  return conversation.providerRoute === route && conversation.status
    ? conversation.status
    : configuredAiProvider(conversation, route)
}

export function aiProviderPresentation(provider, { error = '', failureCode = '', attempts = 0, route = 'legacy' } = {}) {
  const isCodex = provider?.mode === 'codex' || provider?.name === 'codex-cli'
  const codexLocation = provider?.deployment === 'server' ? '服务器' : '本机'
  const codexLabel = `${codexLocation} Codex`
  const degraded = Boolean(provider?.configured && provider?.mode === 'local-fallback')
  const failed = Boolean(error || degraded)
  const labels = { timeout: '响应超时', time_limit: '本轮达到时间限制', transport: '连接中断', incomplete: '输出未完成',
    invalid_json: '结构化结果不完整', empty_response: '未返回结果', invalid_stream: '响应中断', invalid_response: '返回格式异常',
    not_configured: '未配置', launch_failed: `${codexLocation}进程未启动`, process_failed: `${codexLocation}进程未完成`, turn_failed: '本轮执行失败',
    provider_failure: '建模服务未完成', upstream_error: '上游服务返回错误', unexpected_tool: '执行超出允许范围',
    missing_terminal: '未取得完成状态', missing_final: '未返回最终结果', invalid_input: '输入无效', input_limit: '输入超出限制',
    output_limit: '输出超出限制', tool_event: '执行超出允许范围', source_transcription_failed: '原图转录未完成',
    source_required: '需要原始图纸', source_limit: '图纸超出限制', turn_limit: '本轮达到处理次数限制' }
  const code = provider?.lastErrorCode || failureCode
  const nonnegativeCount = (value) => Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : 0
  const count = nonnegativeCount(provider?.attempts ?? attempts)
  const retryCount = nonnegativeCount(provider?.retryCount)
  const counts = route === 'cad'
    ? `${count ? ` · 本轮模型调用 ${count} 次` : ''}${retryCount ? ` · 自动重试 ${retryCount} 次` : ''}`
    : count ? ` · 已尝试 ${count} 次` : ''
  const failureText = drawingPreprocessFailure(code)?.label || labels[code] || code || '本轮未完成'
  const ready = !failed && provider?.configured !== false && (isCodex || ['remote', 'verified-local'].includes(provider?.mode))
  const label = failed
    ? `${isCodex ? codexLabel : '本次失败'} · ${failureText}${counts}`
    : isCodex ? `${codexLabel} · ${provider.configured ? '逐步建模' : '尚未就绪'}`
      : provider?.mode === 'verified-local' ? '图纸校准'
        : ready ? provider.streaming === false ? '中转站 · 逐步建模' : '中转站在线 · SSE 流式'
          : provider ? '服务未就绪' : '正在连接 AI 服务'
  return { isCodex, degraded, failed, ready, label, failureText, failureCode: code, attempts: count, retryCount,
    configurationHint: provider?.configured === false
      ? isCodex ? `${codexLabel} 尚未就绪，${provider.deployment === 'server' ? '请联系管理员检查服务器安装和配置。' : '请检查本机安装和配置。'}` : '远程大模型尚未配置；配置中转站后即可开始对话和图纸分析。'
      : '' }
}
