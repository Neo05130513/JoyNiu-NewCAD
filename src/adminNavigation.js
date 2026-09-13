export const adminSections = [
  { id: 'overview', label: '运营总览', icon: 'overview', permission: 'admin:overview', group: '运营工作台' },
  { id: 'customers', label: '客户管理', icon: 'customers', permission: 'admin:customers', group: '运营工作台' },
  { id: 'tasks', label: '建模任务', icon: 'tasks', permission: 'admin:tasks', group: '运营工作台' },
  { id: 'support', label: '客服工作台', icon: 'support', permission: 'support:manage', group: '运营工作台' },
  { id: 'orders', label: '充值订单', icon: 'orders', permission: 'billing:read', group: '交易与积分' },
  { id: 'credits', label: '积分流水', icon: 'credits', permission: 'billing:read', group: '交易与积分' },
  { id: 'settlements', label: '任务结算', icon: 'settlements', permission: 'billing:read', group: '交易与积分' },
  { id: 'refunds', label: '退款处理', icon: 'refunds', permission: 'billing:read', group: '交易与积分' },
  { id: 'invoices', label: '发票处理', icon: 'invoices', permission: 'billing:read', group: '交易与积分' },
  { id: 'usage', label: '模型用量', icon: 'usage', permission: 'billing:read', group: '交易与积分' },
  { id: 'packages', label: '积分套餐', icon: 'packages', permission: 'billing:policy', group: '平台配置' },
  { id: 'policy', label: '计费规则', icon: 'policy', permission: 'billing:policy', group: '平台配置' },
  { id: 'users', label: '账号与权限', icon: 'users', permission: 'user:manage', group: '平台配置' },
  { id: 'terms', label: '服务条款', icon: 'terms', permission: 'terms:manage', group: '平台配置' },
  { id: 'system', label: '系统状态', icon: 'system', permission: 'admin:system', group: '平台配置' },
  { id: 'audit', label: '操作审计', icon: 'audit', permission: 'admin:audit', group: '平台配置' },
  { id: 'billing', label: '订单管理', icon: 'orders', permission: 'billing:read', group: '交易与积分', hidden: true },
]
const queryKeys = new Set(['customerId', 'runId', 'ownerId', 'ticketId', 'status', 'q', 'source', 'offset', 'active', 'actorId', 'days', 'handlingStatus', 'priority', 'dateFrom', 'dateTo', 'assignedTo', 'view'])

export function readAppRoute(location = globalThis.location) {
  const path = location?.pathname || '/'
  const isAdmin = path === '/admin' || path.startsWith('/admin/')
  const section = isAdmin ? path.split('/')[2] || '' : ''
  const params = Object.fromEntries([...new URLSearchParams(location?.search || '')].filter(([key]) => queryKeys.has(key)))
  const knownSection = !section || section === 'account' || adminSections.some(item => item.id === section)
  return { isAdmin, section, params, valid: !isAdmin || (/^\/admin(?:\/[^/]+)?\/?$/.test(path) && knownSection) }
}

export function adminPath(section = '', params = {}) {
  if (section && section !== 'account' && !adminSections.some(item => item.id === section)) throw new Error('未知后台页面。')
  const query = new URLSearchParams(Object.entries(params).filter(([key, value]) => queryKeys.has(key) && value !== null && value !== undefined && value !== ''))
  return `/admin${section ? `/${section}` : ''}${query.size ? `?${query}` : ''}`
}

export function navigateApplication(path, { replace = false } = {}) {
  if (path !== '/' && !/^\/admin(?:[/?]|$)/.test(path)) throw new Error('导航地址无效。')
  const url = new URL(path, window.location.origin)
  if (url.origin !== window.location.origin) throw new Error('导航地址无效。')
  const target = `${url.pathname}${url.search}`
  if (target === `${window.location.pathname}${window.location.search}`) return
  window.history[replace ? 'replaceState' : 'pushState'](null, '', target)
  window.dispatchEvent(new Event('joyniu:navigate'))
}
