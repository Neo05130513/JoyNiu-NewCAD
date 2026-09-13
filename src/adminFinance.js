export const adminFinanceViews = {
  billing: { title: '订单管理', description: '登记线下到账、核对订单凭证与客户记录。', kind: 'orders' },
  orders: { title: '订单管理', description: '登记线下到账、核对订单凭证与客户记录。', kind: 'orders' },
  credits: { title: '积分流水', description: '追溯每笔积分变动、所属客户和账本余额。', kind: 'ledger' },
  settlements: { title: '任务结算', description: '核对每次建模尝试的实际扣除、待补缴和结算状态。', kind: 'settlements' },
  refunds: { title: '退款处理', description: '查看客户退款申请，记录审核意见并核对渠道结果。', kind: 'refund-requests' },
  invoices: { title: '发票处理', description: '核对开票资料，记录处理状态与外部凭证。', kind: 'invoice-requests' },
  usage: { title: '模型用量', description: '按实际调用记录核对 Token 和已知供应商成本。', kind: 'usage' },
  packages: { title: '积分套餐', description: '管理套餐内容、价格和上下架状态。', kind: null },
}
export function financeFilters(params = {}) {
  const result = {}
  for (const key of ['q', 'ownerId', 'status', 'dateFrom', 'dateTo', 'source']) {
    if (typeof params[key] === 'string' && params[key].trim()) result[key] = params[key].trim().slice(0, key === 'ownerId' ? 160 : 128)
  }
  return result
}
export function financeCsv(kind, items) {
  const fields = kind === 'orders' ? ['id','ownerId','status','amountFen','creditUnits','provider','transactionId','createdAt']
    : kind === 'ledger' ? ['id','ownerId','kind','deltaUnits','balanceAfter','referenceId','createdAt']
      : kind === 'usage' ? ['callId','ownerId','jobId','model','inputTokens','cachedInputTokens','outputTokens','reasoningOutputTokens','costMicroUsd','createdAt']
        : kind === 'settlements' ? ['attemptId','ownerId','jobId','status','terminalStatus','expectedCreditUnits','chargedUnits','deferredUnits','platformAbsorbedUnits']
          : ['id','ownerId','orderId','status','createdAt']
  const labels = { provider:'收款方式',transactionId:'到账凭证编号',id:'记录编号',ownerId:'客户编号',status:'状态',amountFen:'金额（分）',creditUnits:'积分',createdAt:'记录时间',kind:'类型',deltaUnits:'积分变动',balanceAfter:'变更后余额',referenceId:'关联编号',callId:'调用编号',jobId:'任务组编号',model:'模型',inputTokens:'输入Token',cachedInputTokens:'缓存输入Token',outputTokens:'输出Token',reasoningOutputTokens:'推理输出Token',costMicroUsd:'成本（百万分之一美元）',attemptId:'尝试编号',terminalStatus:'任务终态',expectedCreditUnits:'规则核算积分',chargedUnits:'实际扣除',deferredUnits:'待补缴',platformAbsorbedUnits:'平台承担',orderId:'订单编号' }
  const cell = value => { const text = value == null ? '' : String(value); const safe = typeof value === 'string' && /^[\s]*[=+\-@\t\r]/.test(text) ? `'${text}` : text; return `"${safe.replaceAll('"','""')}"` }
  return '\ufeff' + [fields.map(key=>labels[key]), ...items.map(item=>fields.map(key=>item[key]))].map(row=>row.map(cell).join(',')).join('\r\n')
}
