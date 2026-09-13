export const dataRequestScopes = { source_files: '原始图纸与预览', models: '模型与导出文件', workspace: '项目、对话与历史版本', account: '账号与联系资料', support: '支持工单与附件授权记录', backups: '备份副本' }
export const dataReceiptOutcomes = { pending_confirmation: '等待范围确认', processing: '人工处理中', completed: '申请范围已处理', partial: '已部分处理', not_processed: '本次未执行删除' }
export const emptyDataRequest = () => ({ scopes: [], scopeDescription: '', acknowledged: false })

const requiredText = (value, label, maximum) => {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > maximum) throw new Error(`${label}须填写 1 至 ${maximum} 个字符。`)
  return value.trim()
}

export function dataRequestPayload(value) {
  if (!Array.isArray(value?.scopes) || !value.scopes.length || value.scopes.some(scope => !Object.hasOwn(dataRequestScopes, scope)) || new Set(value.scopes).size !== value.scopes.length) throw new Error('请勾选需要申请删除的数据范围。')
  if (value.acknowledged !== true) throw new Error('请确认提交申请不会立即删除数据。')
  return { scopes: [...value.scopes].sort(), scopeDescription: requiredText(value.scopeDescription, '数据范围说明', 2000), acknowledged: true }
}

export function dataReceiptPayload(ticket, value) {
  if (ticket?.category !== 'data_deletion' || !Number.isSafeInteger(ticket?.revision) || ticket.revision < 1) throw new Error('请先读取数据删除申请的最新版本。')
  if (!Object.hasOwn(dataReceiptOutcomes, value.outcome) || value.confirmed !== true) throw new Error('请选择实际处理结果，并确认回执内容。')
  return { revision: ticket.revision, outcome: value.outcome, confirmed: true, ...Object.fromEntries([
    ['handledScope', '已处理或待处理范围', 2000], ['retainedScope', '仍保留的数据与原因', 2000], ['retentionPlan', '保留期限与备份处理安排', 2000], ['evidenceRef', '可核对的处理记录编号', 500],
  ].map(([key, label, maximum]) => [key, requiredText(value[key], label, maximum)])) }
}
