/** UI affordances mirror server RBAC. API authorization remains authoritative. */
export const adminRolePermissions = Object.freeze({
  admin: ['*'],
  ops: ['admin:overview', 'admin:customers', 'admin:tasks', 'admin:task-manage', 'admin:system', 'admin:customer-manage', 'admin:task-followup'],
  finance: ['admin:overview', 'admin:customers', 'billing:read', 'billing:manage', 'billing:adjust'],
  support: ['admin:customers', 'admin:tasks', 'support:manage', 'admin:customer-manage', 'admin:task-followup'],
  auditor: ['admin:overview', 'admin:customers', 'admin:tasks', 'admin:system', 'admin:audit', 'billing:read'],
})

export function hasAdminPermission(user, permission) {
  if (!user || user.active === false || typeof permission !== 'string') return false
  const permissions = Array.isArray(user.permissions) ? user.permissions
    : (user.roles || []).flatMap(role => adminRolePermissions[role] || [])
  return permissions.includes('*') || permissions.includes(permission)
}

export const canAdmin = hasAdminPermission
export const billingAdminCapabilities = user => ({
  read: hasAdminPermission(user, 'billing:read'),
  manage: hasAdminPermission(user, 'billing:manage'),
  adjust: hasAdminPermission(user, 'billing:adjust'),
  policy: hasAdminPermission(user, 'billing:policy'),
})
