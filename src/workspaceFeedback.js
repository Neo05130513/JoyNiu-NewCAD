export function readableError(error) {
  const raw = typeof error === 'string' ? error : error?.message || ''
  const status = error?.status
  if (/invalid credentials|invalid (?:email|username)(?: or| and|\/) password|incorrect (?:email|username|password)|wrong password/i.test(raw)) return '账号或密码不正确，请重新输入。'
  if (status === 401 || /bearer token|token.*expired|not authenticated|invalid.*token/i.test(raw)) return '请先登录后再继续此操作。'
  if (status === 403 || /permission.*denied|forbidden|not permitted/i.test(raw)) return '当前账号没有执行此操作的权限，请联系项目管理员。'
  if (/failed to fetch|networkerror|network request failed/i.test(raw)) return '无法连接服务，请检查网络和服务状态后重试。'
  if (/timeout|timed out|超时/i.test(raw)) return '服务响应超时，请重试；当前草稿仍然保留。'
  if (/quota|storage.*full/i.test(raw)) return '浏览器存储空间不足，请先导出项目备份；当前修改尚未保存。'
  return raw || '操作未完成，请重试。'
}

export function notificationFor(message, type) {
  const text = readableError(message)
  const inferred = /失败|错误|不正确|无法|未完成|不足|超时|不支持|不可|不能|没有|请先|请检查|尚未|未就绪|未连接|待修正|超出|超过|不允许/.test(text) ? 'error' : /正在|稍候|仅|预览|候选|保留|未实现/.test(text) ? 'info' : 'success'
  return { message: text, type: type || inferred }
}

export function downloadBlob(content, filename, type = 'application/json') {
  const blob = content instanceof Blob ? content : new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename.replace(/[\\/:*?"<>|]/g, '-')
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}
