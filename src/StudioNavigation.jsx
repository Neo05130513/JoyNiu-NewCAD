import { useEffect, useId, useRef, useState } from 'react'

const primaryItems = [
  { mode: '首页', title: '创模 AI', icon: 'spark', aliases: ['3D 建模', '图纸核对'] },
  { mode: '项目管理', title: '我的项目', icon: 'folder' },
  { mode: '我的任务', title: '我的任务', icon: 'clock' },
  { mode: '交付中心', title: '交付中心', icon: 'package' },
  { mode: '案例与教程', title: '案例与教程', icon: 'book' },
]
const toolItems = [
  { mode: '特征编辑', title: '特征编辑', icon: 'cube' },
  { mode: '原生二维', title: '二维绘图', icon: 'drawing' },
  { mode: '工程设计', title: '工程设计', icon: 'layers', aliases: ['2D 工程图', '装配', '基础工程图', '装配草稿'] },
  { mode: '照片建模', title: '照片建模', icon: 'image' },
  { mode: '标准件库', title: '标准件库', icon: 'parts' },
  { mode: '平台服务', title: 'PDM 平台服务', icon: 'archive' },
  { mode: 'CAM / NC', title: 'CAM / NC', icon: 'tool' },
]
const accountItems = [
  { mode: '积分与订单', title: '积分与订单', icon: 'wallet' },
  { mode: '账号', title: '账号与安全', icon: 'user' },
  { mode: '设置', title: '设置', icon: 'settings' },
  { mode: '支持与工单', title: '支持与工单', icon: 'message' },
  { mode: '服务与积分规则', title: '服务规则', icon: 'document' },
  { mode: '帮助与反馈', title: '帮助', icon: 'help' },
]
const iconPaths = {
  spark: 'M12 3 14.7 9.3 21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7L12 3Z',
  folder: 'M3 7V5h6l2 2h10v12H3V7Z',
  clock: 'M12 8v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  package: 'M12 3 21 8v9l-9 5-9-5V8l9-5Z M3 8l9 5 9-5 M12 13v9 M7.5 5.5l9 5',
  book: 'M12 5C9 3 6 3 3 4v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15',
  cube: 'm12 3 9 5v9l-9 5-9-5V8l9-5Z M3 8l9 5 9-5 M12 13v9',
  drawing: 'M4 4h16v16H4V4Z M8 16l8-8 M8 8h3 M16 13v3',
  layers: 'm12 3 10 5-10 5L2 8l10-5Z M2 12l10 5 10-5 M2 16l10 5 10-5',
  image: 'M3 4h18v16H3V4Z M3 16l5-5 4 4 3-3 6 6 M9 8h.01',
  parts: 'M4 4h6v6H4V4Z M14 4h6v6h-6V4Z M4 14h6v6H4v-6Z M14 14h6v6h-6v-6Z',
  archive: 'M3 4h18v4H3V4Z M5 8v12h14V8 M9 12h6',
  tool: 'M14 4a6 6 0 0 0-7 7L3 17a3 3 0 0 0 4 4l6-6a6 6 0 0 0 7-7l-4 4-4-4 4-4h-2Z',
  wallet: 'M3 6h16v14H3V6Z M3 6l13-3v3 M15 11h6v5h-6v-5Z M17 13.5h.01',
  user: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z M4 21v-2a8 8 0 0 1 16 0v2',
  settings: 'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  message: 'M3 4h18v13H9l-6 4V4Z M7 8h10 M7 12h7',
  document: 'M5 3h9l5 5v13H5V3Z M14 3v5h5 M9 12h6 M9 16h6',
  help: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z M9 9a3 3 0 1 1 4 2.8c-1 .4-1 1.2-1 2.2 M12 17h.01',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  close: 'm6 6 12 12 M6 18 18 6',
  collapse: 'M3 4h18v16H3V4Z M8 4v16 M16 9l-3 3 3 3',
  expand: 'M3 4h18v16H3V4Z M8 4v16 M13 9l3 3-3 3',
  chevron: 'm9 5 7 7-7 7',
  down: 'm6 9 6 6 6-6',
  plus: 'M12 5v14 M5 12h14',
  search: 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z m-2 5 6 6',
  admin: 'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Z M8 12l3 3 5-6',
}
function StudioIcon({ name, className = '' }) {
  return <svg className={`studio-icon ${className}`} width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={iconPaths[name] || iconPaths.document}/></svg>
}
const isSelected = (item, mode) => item.mode === mode || item.aliases?.includes(mode)
const displayTitle = mode => ({ '首页': '创模 AI', '项目管理': '我的项目', '原生二维': '二维绘图', '装配草稿': '标准件装配草稿', '账号': '账号与安全' }[mode] || mode || '创模 AI')

export default function StudioNavigation({ activeMode, context, projectName, fileName, saveLabel, user, creditBalance,
  backend, adminAvailable = false, projects = [], onNavigate, onNewProject, onRecent, onCommands, onAdmin,
  onSelectProject, collapsed = false, onToggleSidebar, menuOpen = false, onMenuOpenChange }) {
  const toolActive = toolItems.some(item => isSelected(item, activeMode))
  const [toolsOpen, setToolsOpen] = useState(toolActive)
  const [accountOpen, setAccountOpen] = useState(false)
  const [mobile, setMobile] = useState(false)
  const sidebar = useRef(null), menuButton = useRef(null)
  const sidebarId = useId()
  const compact = collapsed && !mobile
  const fileContext = context === 'project' && activeMode !== '项目管理'
  const userName = user?.displayName || user?.display_name || '我的账号'
  const recentProjects = [...(Array.isArray(projects) ? projects : [])].filter(item => item?.id && item?.name)
    .sort((a, b) => (Date.parse(b.updatedAt) || 0) - (Date.parse(a.updatedAt) || 0)).slice(0, 3)
  const closeMenu = () => onMenuOpenChange?.(false)
  const navigate = mode => { onNavigate?.(mode); closeMenu(); setAccountOpen(false) }
  const action = fn => { fn?.(); closeMenu() }
  useEffect(() => { setToolsOpen(toolActive) }, [activeMode, toolActive])
  useEffect(() => {
    const query = window.matchMedia('(max-width: 900px)')
    const changed = () => setMobile(query.matches)
    changed(); query.addEventListener('change', changed)
    return () => query.removeEventListener('change', changed)
  }, [])
  useEffect(() => {
    if (!mobile || !menuOpen) return
    const before = document.activeElement
    sidebar.current?.querySelector('.studio-mobile-close')?.focus()
    const onKey = event => {
      if (event.key === 'Escape') { event.preventDefault(); onMenuOpenChange?.(false); return }
      if (event.key !== 'Tab') return
      const candidates = [...(sidebar.current?.querySelectorAll('button, summary, a[href]') || [])]
        .filter(node => !node.disabled && node.getClientRects().length)
      const first = candidates[0], last = candidates.at(-1)
      if (event.shiftKey && (document.activeElement === first || !sidebar.current?.contains(document.activeElement))) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !sidebar.current?.contains(document.activeElement))) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); if (before?.isConnected) before.focus(); else menuButton.current?.focus() }
  }, [mobile, menuOpen, onMenuOpenChange])
  const navButton = (item, extra = '') => <button key={item.mode} type="button" className={`studio-nav-item ${extra}${isSelected(item, activeMode) ? ' is-active' : ''}`} aria-current={isSelected(item, activeMode) ? 'page' : undefined} title={item.title} onClick={() => navigate(item.mode)}><StudioIcon name={item.icon}/><span className="studio-label">{item.title}</span></button>
  const openCollapsed = event => { if (compact) { event.preventDefault(); onToggleSidebar?.() } }
  return <>
    {menuOpen && <button type="button" className="studio-backdrop" aria-label="关闭导航菜单" tabIndex={-1} onClick={closeMenu}/>}
    <aside id={sidebarId} ref={sidebar} className={`studio-sidebar${menuOpen ? ' is-open' : ''}`} aria-label="工作台导航" role={mobile && menuOpen ? 'dialog' : undefined} aria-modal={mobile && menuOpen ? true : undefined} inert={mobile && !menuOpen ? true : undefined}>
      <div className="studio-brand-row"><button type="button" className="studio-brand" title="JoyNiu CAD 首页" aria-label="JoyNiu CAD 首页" onClick={() => navigate('首页')}><span className="studio-brand-mark"><StudioIcon name="cube"/></span><span className="studio-label">JoyNiu CAD</span></button><button type="button" className="studio-icon-button studio-mobile-close" aria-label="关闭侧栏" onClick={closeMenu}><StudioIcon name="close"/></button></div>
      <div className="studio-sidebar-scroll">
        <button type="button" className="studio-new-project" title="新建项目" onClick={() => action(onNewProject)}><StudioIcon name="plus"/><span className="studio-label">新建项目</span></button>
        <nav className="studio-primary-nav" aria-label="主要功能">{primaryItems.map(item => navButton(item))}</nav>
        <details className={`studio-tools${toolActive ? ' has-active-tool' : ''}`} open={toolsOpen} onToggle={event => setToolsOpen(event.currentTarget.open)}>
          <summary title="设计工具" onClick={event => { openCollapsed(event); if (compact) setToolsOpen(true) }}><StudioIcon name="drawing"/><span className="studio-label">设计工具</span><StudioIcon name="down" className="studio-disclosure-arrow"/></summary>
          <nav className="studio-tool-list" aria-label="设计工具">{toolItems.map(item => navButton(item))}</nav>
        </details>
        <section className="studio-recent" aria-label="最近项目"><div className="studio-recent-heading"><span>最近项目</span><button type="button" onClick={() => action(onRecent)} title="查看最近项目">全部</button></div>{recentProjects.length ? recentProjects.map(project => <button type="button" className="studio-recent-item" key={project.id} title={project.name} onClick={() => action(() => onSelectProject?.(project.id))}><StudioIcon name="folder"/><span>{project.name}</span></button>) : <p>新项目会显示在这里</p>}</section>
      </div>
      <div className="studio-sidebar-bottom">
        <details className="studio-account" open={accountOpen} onToggle={event => setAccountOpen(event.currentTarget.open)}>
          <summary title={user ? userName : '账号与帮助'} onClick={event => { openCollapsed(event); if (compact) setAccountOpen(true) }}><span className="studio-avatar">{user ? Array.from(userName)[0] : <StudioIcon name="user"/>}</span><span className="studio-account-label studio-label"><b>{user ? userName : '账号与帮助'}</b>{user && Number.isFinite(creditBalance) && <small>{creditBalance.toLocaleString('zh-CN')} 积分</small>}</span><StudioIcon name="down" className="studio-disclosure-arrow"/></summary>
          <nav className="studio-account-menu" aria-label="账号与服务">{accountItems.map(item => navButton(item))}{adminAvailable && <button type="button" className="studio-nav-item" onClick={() => action(onAdmin)}><StudioIcon name="admin"/><span>管理后台</span></button>}{backend?.status === 'offline' && <p className="studio-service-note">服务暂未连接</p>}</nav>
        </details>
        <button type="button" className="studio-collapse studio-nav-item" title={collapsed ? '展开侧栏' : '收起侧栏'} aria-label={collapsed ? '展开侧栏' : '收起侧栏'} onClick={onToggleSidebar}><StudioIcon name={collapsed ? 'expand' : 'collapse'}/><span className="studio-label">收起侧栏</span></button>
      </div>
    </aside>
    <header className="studio-header">
      <button ref={menuButton} type="button" className="studio-icon-button studio-menu-button" aria-label="打开导航菜单" aria-controls={sidebarId} aria-expanded={menuOpen} onClick={() => onMenuOpenChange?.(!menuOpen)}><StudioIcon name="menu"/></button>
      <div className="studio-page-heading">{fileContext && projectName && <><span className="studio-project-name" title={projectName}>{projectName}</span><StudioIcon name="chevron" className="studio-heading-chevron"/></>}<h1 title={fileContext && fileName ? fileName : displayTitle(activeMode)}>{fileContext && fileName ? fileName : displayTitle(activeMode)}</h1>{context === 'project' && saveLabel && <span className="studio-save-label" role="status">{saveLabel}</span>}</div>
      <div className="studio-header-actions"><button type="button" className="studio-command-button" aria-label="搜索或打开快捷命令" onClick={onCommands}><StudioIcon name="search"/><span>搜索或输入命令</span><kbd>⌘ K</kbd></button>{!user && <button type="button" className="studio-login" onClick={() => navigate('账号')}>登录 / 注册</button>}</div>
    </header>
  </>
}
