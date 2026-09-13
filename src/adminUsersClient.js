import { API_BASE } from './api.js'
import { hasAdminPermission } from './adminPermissions.js'

export const accountRoleNames = { viewer: '只读成员', designer: '设计师', reviewer: '审核员', manufacturing: '制造人员', ops: '运营', finance: '财务', support: '客服', auditor: '审计', admin: '管理员' }
export const canManageAccounts = user => hasAdminPermission(user, 'user:manage')

export function validateAccountRoles(roles) {
  if (!Array.isArray(roles) || !roles.length || roles.some(role => !Object.hasOwn(accountRoleNames, role))) throw new Error('请至少选择一个有效角色。')
  return [...new Set(roles)].sort()
}

export function validateNewAccountPassword(password, confirmation) {
  if (typeof password !== 'string' || password.length < 8 || password.length > 256) throw new Error('新密码需包含 8 至 256 个字符。')
  if (password !== confirmation) throw new Error('两次输入的密码不一致。')
  return password
}

export function accountChangeBlock(users, target, values, actorId) {
  const disabling = values.active === false
  const removingAdmin = Array.isArray(values.roles) && !values.roles.includes('admin')
  if (target.id === actorId && disabling) return '不能停用当前登录账号，请使用另一个管理员账号操作。'
  if (target.id === actorId && target.roles?.includes('admin') && removingAdmin) return '不能移除当前登录账号的管理员角色。'
  if (target.active && target.roles?.includes('admin') && (disabling || removingAdmin)
    && users.filter(user => user.active && user.roles?.includes('admin')).length <= 1) return '至少需要保留一个可登录的管理员账号。'
  return ''
}

export function adminUserPage(users, query = '', activeFilter = 'all', page = 0, pageSize = 20) {
  const needle = query.trim().toLocaleLowerCase()
  const found = users.filter(user => (activeFilter === 'all' || (activeFilter === 'active' ? user.active : !user.active))
    && (!needle || [user.email, user.display_name, user.displayName, user.id, ...(user.roles || []).map(role => accountRoleNames[role] || role)].join(' ').toLocaleLowerCase().includes(needle)))
  const pages = Math.max(1, Math.ceil(found.length / pageSize))
  const boundedPage = Math.min(Math.max(0, page), pages - 1)
  return { items: found.slice(boundedPage * pageSize, (boundedPage + 1) * pageSize), total: found.length, pages, page: boundedPage }
}

export function createAdminUsersClient({ apiBase = API_BASE, fetchImpl = globalThis.fetch } = {}) {
  async function request(path, { token, method = 'GET', body, signal }) {
    if (!token) throw Object.assign(new Error('请登录管理员账号。'), { status: 401 })
    const response = await fetchImpl(`${apiBase.replace(/\/+$/, '')}/auth/users${path}`, {
      method, signal, credentials: 'include', cache: 'no-store',
      headers: { Accept: 'application/json', Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    let payload
    try { payload = await response.json() } catch { throw Object.assign(new Error('账号服务返回无效响应，请重试。'), { status: response.status }) }
    if (!response.ok) {
      const detail = payload?.detail
      const message = typeof detail === 'string' ? detail : detail?.message || '账号操作失败，请重试。'
      throw Object.assign(new Error(message), { status: response.status, payload })
    }
    return payload
  }
  return {
    list: (token, signal) => request('?include_inactive=true', { token, signal }),
    create: (token, values, signal) => request('', { token, method: 'POST', body: values, signal }),
    roles: (token, id, roles, signal) => request(`/${encodeURIComponent(id)}/roles`, { token, method: 'PATCH', body: { roles: validateAccountRoles(roles) }, signal }),
    active: (token, id, active, signal) => {
      if (typeof active !== 'boolean') throw new Error('账号启用状态无效。')
      return request(`/${encodeURIComponent(id)}/active`, { token, method: 'PATCH', body: { active }, signal })
    },
    resetPassword: (token, id, newPassword, signal) => request(`/${encodeURIComponent(id)}/password/reset`, { token, method: 'POST', body: { newPassword }, signal }),
  }
}

export const adminUsersClient = createAdminUsersClient()
