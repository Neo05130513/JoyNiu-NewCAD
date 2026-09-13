import { useCallback, useEffect, useRef, useState } from 'react'
import { accountChangeBlock, accountRoleNames, adminUserPage, adminUsersClient, canManageAccounts, validateAccountRoles, validateNewAccountPassword } from './adminUsersClient.js'
import './admin-users.css'

function UserDialog({ action, users, actorId, busy, error, onClose, onSubmit }) {
  const dialog = useRef(null)
  const [values, setValues] = useState({ email: '', displayName: '', password: '', confirmPassword: '', roles: action.user?.roles || ['designer'] })
  const [validation, setValidation] = useState('')
  const target = action.user
  const title = { create: '创建账号', roles: '修改账号角色', password: '重置账号密码', active: target?.active ? '停用账号' : '启用账号' }[action.kind]
  useEffect(() => { const focused = document.activeElement; dialog.current?.showModal(); return () => focused?.focus?.() }, [])
  const set = key => event => setValues(current => ({ ...current, [key]: event.target.value }))
  async function submit(event) {
    event.preventDefault(); setValidation('')
    try {
      let payload
      if (action.kind === 'create') payload = { email: values.email.trim(), displayName: values.displayName.trim(), password: validateNewAccountPassword(values.password, values.confirmPassword), roles: validateAccountRoles(values.roles) }
      if (action.kind === 'password') payload = { newPassword: validateNewAccountPassword(values.password, values.confirmPassword) }
      if (action.kind === 'roles') payload = { roles: validateAccountRoles(values.roles) }
      if (action.kind === 'active') payload = { active: !target.active }
      const block = target ? accountChangeBlock(users, target, payload, actorId) : ''
      if (block) throw new Error(block)
      await onSubmit(payload)
    } catch (failure) { setValidation(failure.message) }
  }
  return <dialog className="admin-users-dialog" ref={dialog} aria-label={title} onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <header><h2>{title}</h2><button type="button" aria-label="关闭弹窗" disabled={busy} onClick={onClose}>×</button></header>
    <form onSubmit={submit}>
      {target && <div className="admin-users-target"><b>{target.display_name || target.displayName || target.email}</b><span>{target.email}</span><code>{target.id}</code></div>}
      {action.kind === 'create' && <><label>邮箱<input autoFocus required type="email" autoComplete="off" maxLength={254} value={values.email} onChange={set('email')} /></label><label>显示名称<input required maxLength={120} value={values.displayName} onChange={set('displayName')} /></label></>}
      {['create', 'password'].includes(action.kind) && <><label>{action.kind === 'create' ? '初始密码' : '新密码'}<input required type="password" autoComplete="new-password" minLength={8} maxLength={256} value={values.password} onChange={set('password')} /></label><label>再次输入密码<input required type="password" autoComplete="new-password" minLength={8} maxLength={256} value={values.confirmPassword} onChange={set('confirmPassword')} /></label><p className="admin-users-muted">{action.kind === 'password' ? '重置后该账号现有登录会话会结束。请通过安全渠道向账号本人交付新密码。' : '请自行设置初始密码，并通过安全渠道交付账号本人。'}</p></>}
      {['create', 'roles'].includes(action.kind) && <fieldset><legend>角色（至少选择一项）</legend>{Object.entries(accountRoleNames).map(([role, name]) => <label className="admin-users-check" key={role}><input type="checkbox" checked={values.roles.includes(role)} onChange={event => setValues(current => ({ ...current, roles: event.target.checked ? [...current.roles, role] : current.roles.filter(item => item !== role) }))} />{name}</label>)}</fieldset>}
      {action.kind === 'active' && <p>{target.active ? '停用后，此账号将无法继续登录，现有登录会话会结束。项目和账单记录会保留。' : '启用后，此账号可以使用原有账号信息重新登录。'}</p>}
      {(validation || error) && <p className="admin-users-error" role="alert">{validation || error}</p>}
      <footer><button type="button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="admin-users-primary" disabled={busy}>{busy ? '正在保存…' : action.kind === 'active' && target.active ? '确认停用账号' : '确认保存'}</button></footer>
    </form>
  </dialog>
}

function AdminUserSession({ session, initialQuery }) {
  const token = session.access_token
  const currentToken = useRef(token)
  currentToken.current = token
  const alive = useRef(false)
  const controller = useRef(null)
  const requestSequence = useRef(0)
  const actionPending = useRef(false)
  const [users, setUsers] = useState(null)
  const [query, setQuery] = useState(initialQuery || '')
  const [activeFilter, setActiveFilter] = useState('all')
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [action, setAction] = useState(null)
  const [actionError, setActionError] = useState('')
  const refresh = useCallback(async () => {
    const run = ++requestSequence.current
    setLoading(true); setError('')
    try {
      const result = await adminUsersClient.list(token, controller.current.signal)
      if (!alive.current || currentToken.current !== token || run !== requestSequence.current) return
      if (!Array.isArray(result?.items)) throw new Error('账号列表响应无效，请刷新重试。')
      setUsers(result.items)
    } catch (failure) { if (alive.current && currentToken.current === token && run === requestSequence.current && failure.name !== 'AbortError') setError(failure.message) }
    finally { if (alive.current && currentToken.current === token && run === requestSequence.current) setLoading(false) }
  }, [token])
  const latestRefresh = useRef(refresh); latestRefresh.current = refresh
  useEffect(() => { alive.current = true; controller.current = new AbortController(); return () => { alive.current = false; controller.current.abort(); requestSequence.current++ } }, [])
  useEffect(() => { refresh() }, [refresh])
  const display = adminUserPage(users || [], query, activeFilter, page)
  const open = (kind, user = null) => { setActionError(''); setAction({ kind, user }) }
  async function submit(values) {
    if (actionPending.current) return
    actionPending.current = true; setBusy(true); setActionError(''); setNotice('')
    try {
      const signal = controller.current.signal
      const kind = action.kind
      if (kind === 'create') await adminUsersClient.create(token, values, signal)
      else if (kind === 'roles') await adminUsersClient.roles(token, action.user.id, values.roles, signal)
      else if (kind === 'active') await adminUsersClient.active(token, action.user.id, values.active, signal)
      else await adminUsersClient.resetPassword(token, action.user.id, values.newPassword, signal)
      if (!alive.current || currentToken.current !== token) return
      setAction(null); setNotice(kind === 'password' ? '密码已重置，该账号原有登录会话已结束。' : '账号变更已保存。')
      await latestRefresh.current()
    } catch (failure) { if (alive.current && currentToken.current === token && failure.name !== 'AbortError') setActionError(failure.message) }
    finally { actionPending.current = false; if (alive.current) setBusy(false) }
  }
  return <section className="admin-users-workspace" aria-label="账号管理"><header><div><span className="admin-users-eyebrow">JOYNIU CAD · ADMINISTRATION</span><h1>账号管理</h1><p>管理登录权限、人员角色与账号安全。</p></div><div><button disabled={loading || busy} onClick={refresh}>{loading ? '正在读取…' : '刷新列表'}</button><button className="admin-users-primary" disabled={busy} onClick={() => open('create')}>＋ 创建账号</button></div></header>
    {error && <p className="admin-users-error" role="alert">{error}</p>}{notice && <p className="admin-users-notice" role="status">{notice}</p>}
    <div className="admin-users-toolbar"><input type="search" aria-label="搜索账号" placeholder="搜索邮箱、名称、角色或账号 ID" value={query} onChange={event => { setQuery(event.target.value); setPage(0) }} /><select aria-label="账号状态" value={activeFilter} onChange={event => { setActiveFilter(event.target.value); setPage(0) }}><option value="all">全部账号</option><option value="active">已启用</option><option value="inactive">已停用</option></select><span>{users ? `共 ${display.total} 个账号` : '正在读取账号'}</span></div>
    <div className="admin-users-card">{users === null ? <p className="admin-users-empty">{loading ? '正在加载账号…' : '账号尚未加载，请刷新重试。'}</p> : !display.items.length ? <p className="admin-users-empty">没有符合条件的账号。</p> : <div className="admin-users-table"><table><thead><tr><th>账号</th><th>状态</th><th>角色</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{display.items.map(user => {
      const disableReason = accountChangeBlock(users, user, { active: false }, session.user.id)
      return <tr key={user.id}><td><strong>{user.display_name || user.displayName || user.email}{user.id === session.user.id && <small>当前账号</small>}</strong><span>{user.email}</span><code>{user.id}</code></td><td><span className={`admin-users-badge ${user.active ? 'active' : ''}`}>{user.active ? '已启用' : '已停用'}</span></td><td><div className="admin-users-roles">{user.roles?.map(role => <span key={role}>{accountRoleNames[role] || role}</span>)}</div></td><td>{user.created_at ? new Date(user.created_at).toLocaleDateString('zh-CN') : '—'}</td><td><div className="admin-users-row-actions"><button disabled={busy} onClick={() => open('roles', user)}>角色</button><button disabled={busy || user.id === session.user.id} title={user.id === session.user.id ? '请在账号与安全中修改自己的密码' : ''} onClick={() => open('password', user)}>重置密码</button><button disabled={busy || (user.active && Boolean(disableReason))} title={user.active ? disableReason : ''} onClick={() => open('active', user)}>{user.active ? '停用' : '启用'}</button></div></td></tr>
    })}</tbody></table></div>}
    {users && <nav className="admin-users-pagination" aria-label="账号分页"><span>第 {display.page + 1} / {display.pages} 页</span><div><button disabled={display.page === 0} onClick={() => setPage(display.page - 1)}>上一页</button><button disabled={display.page + 1 >= display.pages} onClick={() => setPage(display.page + 1)}>下一页</button></div></nav>}</div>
    {action && <UserDialog key={`${action.kind}:${action.user?.id || 'new'}`} action={action} users={users || []} actorId={session.user.id} busy={busy} error={actionError} onClose={() => setAction(null)} onSubmit={submit} />}
  </section>
}

export default function AdminUsersWorkspace({ account, onLogin, initialQuery = '' }) {
  const session = account?.session
  if (!session?.access_token) return <section className="admin-users-workspace"><h1>账号管理</h1><p>登录管理员账号后继续。</p><button className="admin-users-primary" onClick={onLogin}>登录账号</button></section>
  if (!canManageAccounts(session.user)) return <section className="admin-users-workspace"><h1>账号管理</h1><p role="alert">当前账号没有账号管理权限。</p></section>
  return <AdminUserSession key={`${session.user.id}:${initialQuery}`} session={session} initialQuery={initialQuery} />
}
