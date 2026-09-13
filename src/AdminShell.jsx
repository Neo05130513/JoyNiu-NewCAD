import { useEffect, useRef, useState } from 'react'
import AccountWorkspace from './AccountWorkspace.jsx'
import AdminUsersWorkspace from './AdminUsersWorkspace.jsx'
import AdminBillingPolicyWorkspace from './AdminBillingPolicyWorkspace.jsx'
import BillingWorkspace from './BillingWorkspace.jsx'
import CommercialTermsWorkspace from './CommercialTermsWorkspace.jsx'
import SupportWorkspace from './SupportWorkspace.jsx'
import AdminOperationsWorkspace from './AdminOperationsWorkspace.jsx'
import { canAdmin } from './adminPermissions.js'
import { adminPath, adminSections, navigateApplication } from './adminNavigation.js'
import './admin-shell.css'
import { adminFinanceViews } from './adminFinance.js'
import AdminIcon from './AdminIcon.jsx'

export default function AdminShell({ account, route }) {
  const user = account.session?.user
  const sections = adminSections.filter(item => canAdmin(user, item.permission))
  const selected = route.section || sections[0]?.id || 'overview'
  const current = sections.find(item => item.id === selected)
  const [menuOpen, setMenuOpen] = useState(false)
  const [logoutError, setLogoutError] = useState('')
  const [search, setSearch] = useState('')
  const [searchScope, setSearchScope] = useState('')
  const [collapsed, setCollapsed] = useState(false)
  const searchSections = sections.filter(item => ['customers','tasks','orders','support'].includes(item.id))
  const main = useRef(null)
  const sidebar = useRef(null)
  const navigate = (section, params = {}) => { setMenuOpen(false); navigateApplication(adminPath(section, params)) }
  useEffect(() => {
    document.title = `${current?.label || '管理后台'} · JoyNiu CAD`
    if (user) main.current?.focus()
    return () => { document.title = 'JoyNiu NewCAD · AI 工程设计工作台' }
  }, [route.section, current?.label, user?.id])
  useEffect(() => { setMenuOpen(false) }, [route.section])
  useEffect(() => {
    if (!menuOpen) return
    if (sidebar.current) sidebar.current.scrollTop = 0
    const escape = event => { if (event.key === 'Escape') setMenuOpen(false) }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [menuOpen])
  async function logout() {
    setLogoutError('')
    try { await account.logout() } catch { setLogoutError('退出请求未完成，请重试。') }
  }
  if (!user) return <div className="admin-login"><header><a href="/" onClick={event => { event.preventDefault(); navigateApplication('/') }}>JoyNiu CAD</a><span>管理后台</span></header><div className="admin-login-body"><h1>登录管理后台</h1><p>使用已分配后台权限的账号登录。</p><AccountWorkspace account={account} adminLogin onOpenProjects={() => navigateApplication('/')} /></div></div>
  if (!sections.length) return <div className="admin-login"><header><strong>JoyNiu CAD · 管理后台</strong></header><main className="admin-access-message"><h1>当前账号没有后台权限</h1><p>你可以返回设计工作台，或切换到管理员分配的后台账号。</p><div><button onClick={() => navigateApplication('/')}>返回设计工作台</button><button onClick={logout}>退出并切换账号</button></div>{logoutError && <p role="alert">{logoutError}</p>}</main></div>
  const content = !route.valid || (!current && selected !== 'account') ? <section className="admin-access-message"><h1>{route.valid ? '此页面需要额外权限' : '后台页面不存在'}</h1><p>{route.valid ? '请使用左侧已授权的管理功能。' : '请检查地址，或返回运营后台。'}</p><button onClick={() => navigate(sections[0].id)}>返回{sections[0].label}</button></section>
    : selected === 'account' ? <AccountWorkspace account={account} onOpenProjects={() => navigateApplication('/')} />
      : Object.hasOwn(adminFinanceViews, selected) ? <BillingWorkspace account={account} admin adminView={selected} params={route.params} onNavigate={navigate} />
        : selected === 'users' ? <AdminUsersWorkspace account={account} initialQuery={route.params.q || route.params.ownerId || ''} />
          : selected === 'policy' ? <AdminBillingPolicyWorkspace account={account} />
            : selected === 'terms' ? <CommercialTermsWorkspace account={account} admin />
              : selected === 'support' ? <SupportWorkspace account={account} admin initialTicketId={route.params.ticketId} params={route.params} onNavigate={navigate} />
                : <AdminOperationsWorkspace account={account} section={selected} params={route.params} onNavigate={navigate} />
  return <div className={`admin-shell ${collapsed ? "admin-is-collapsed" : ""}`}>
    <a className="admin-skip" href="#admin-main">跳转到主要内容</a>
    <header className="admin-topbar">
      <div className="admin-brand"><button className="admin-menu-toggle" aria-label="展开后台导航" aria-expanded={menuOpen} onClick={() => setMenuOpen(value => !value)}><AdminIcon name="menu" /></button><span className="admin-brand-symbol">J</span><strong>JoyNiu <small>CAD 运营管理</small></strong></div>
      <form className="admin-global-search" role="search" onSubmit={event => { event.preventDefault(); if (search.trim() && searchSections.length) navigate(searchScope || searchSections[0].id, {q:search.trim()}) }}>
        <AdminIcon name="search" /><select aria-label="后台搜索范围" value={searchScope || searchSections[0]?.id || ''} onChange={event=>setSearchScope(event.target.value)}>{searchSections.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select><input type="search" aria-label="后台快捷搜索" maxLength={128} placeholder="搜索客户、任务或记录编号" value={search} onChange={event=>setSearch(event.target.value)} /><button type="submit" disabled={!search.trim() || !searchSections.length}>搜索</button>
      </form>
      <div className="admin-topbar-actions"><button className="admin-workbench-link" onClick={() => navigateApplication('/')}><AdminIcon name="external" />设计工作台</button><button className="admin-user-button" onClick={() => navigate('account')}><span className="admin-avatar">{(user.displayName || user.display_name || user.email || 'J').slice(0,1).toUpperCase()}</span><span>{user.displayName || user.display_name || user.email}<small>{user.roles?.map(role=>({admin:'管理员',ops:'运营',finance:'财务',support:'客服',auditor:'审计'})[role]).filter(Boolean).join(' / ') || '后台成员'}</small></span></button><button className="admin-logout" onClick={logout}>退出</button></div>
    </header>
    <div className="admin-layout"><aside ref={sidebar} className={`admin-sidebar ${menuOpen ? 'is-open' : ''}`} aria-label="管理后台导航">
      <div className="admin-nav-intro"><span className="admin-workspace-dot" />运营控制台<button aria-label={collapsed ? '展开侧栏' : '收起侧栏'} onClick={()=>setCollapsed(value=>!value)}><AdminIcon name="panel" /></button></div>
      {[...new Set(sections.filter(item=>!item.hidden).map(item=>item.group))].map(group => <nav key={group} aria-label={group}><span className="admin-nav-group">{group}</span>{sections.filter(item => item.group === group && !item.hidden).map(item => <a key={item.id} title={item.label} href={adminPath(item.id)} aria-current={selected === item.id || (selected==='billing' && item.id==='orders') ? 'page' : undefined} onClick={event => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(item.id) }}><AdminIcon name={item.icon} /><span>{item.label}</span><i aria-hidden="true" /></a>)}</nav>)}
      <footer><span>JoyNiu CAD</span><small>客户服务与业务管理</small></footer></aside>
      {menuOpen && <button className="admin-menu-backdrop" aria-label="关闭后台导航" onClick={() => setMenuOpen(false)} />}

      <main id="admin-main" className="admin-main" ref={main} tabIndex={-1}><div className="admin-breadcrumb">管理后台 <span>/</span> {current?.label || (selected === 'account' ? '账号与安全' : '页面访问')}</div>{logoutError && <p role="alert">{logoutError}</p>}{content}</main>
    </div>
  </div>
}
