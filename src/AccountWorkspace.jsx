import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { validateNewAccountPassword } from './adminUsersClient.js'

export default function AccountWorkspace({ account, onOpenProjects, adminLogin = false }) {
  const [mode, setMode] = useState('login')
  const [capabilities, setCapabilities] = useState(null)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const user = account.session?.user
  const alive = useRef(false)
  const pending = useRef(false)
  useEffect(() => { let active = true; alive.current = true; api.accountCapabilities().then(value => { if (active) setCapabilities(value) }).catch(e => { if(active) setError(e.message) }); return () => { active=false; alive.current=false } }, [])
  async function submit(event) {
    event.preventDefault()
    if (pending.current) return
    pending.current = true; setBusy(true); setError(''); setNotice('')
    try {
      if (user) { validateNewAccountPassword(newPassword, confirmPassword); await account.changePassword({ currentPassword: password, newPassword }); if (alive.current) { setNotice('密码已修改，其他登录会话已退出。'); setPassword(''); setNewPassword(''); setConfirmPassword('') } }
      else if (mode === 'register') { validateNewAccountPassword(password, confirmPassword); await account.register({ email: email.trim(), password, displayName: displayName.trim() }) }
      else if (mode === 'forgot') { await api.forgotPassword(email.trim()); setNotice('如该邮箱已注册，请按邮件指引重置密码。') }
      else await account.login({ email: email.trim(), password })
    } catch (e) { if (alive.current && e.name !== 'AbortError') setError(e.status===401 && !user ? '账号或密码不正确，请重新输入。' : e.message) }
    finally { pending.current = false; if (alive.current) setBusy(false) }
  }
  const switchMode = value => { if (pending.current) return; setMode(value); setPassword(''); setConfirmPassword(''); setError(''); setNotice('') }
  return <section className="customer-account" aria-label="账号与安全">
    <div className="customer-section-heading"><div><span className="eyebrow">JOYNIU CAD</span><h1>{user ? '账号与安全' : '欢迎使用 JoyNiu CAD'}</h1><p>{user ? '管理登录信息和账号安全' : '登录后，图纸、模型和项目保存在你的独立空间。'}</p></div>{user && <button className="secondary-button" onClick={account.logout}>退出登录</button>}</div>
    <div className="account-card">
      {user ? <><h2>{user.displayName || user.display_name || user.email}</h2><p>{user.email}</p><button className="text-button" onClick={onOpenProjects}>打开我的项目 →</button>{account.session?.session_notice && <p className="customer-notice">{account.session.session_notice}</p>}<h3>修改密码</h3></> : <div className="customer-tabs"><button aria-pressed={mode==='login'} onClick={()=>switchMode('login')}>登录</button>{!adminLogin && <button aria-pressed={mode==='register'} onClick={()=>switchMode('register')}>注册账号</button>}<button aria-pressed={mode==='forgot'} onClick={()=>switchMode('forgot')}>找回密码</button></div>}
      {error && <p role="alert" className="customer-error">{error}</p>}{notice && <p role="status" className="customer-notice">{notice}</p>}
      {!user && account.notice && <p role="status" className="customer-notice">{account.notice}</p>}
      {mode==='register' && !user && capabilities && !capabilities.registrationEnabled ? <p>自助注册暂未开放，请联系管理员开通账号。</p> : mode==='forgot' && !user && !capabilities?.passwordResetEmailAvailable ? <p>邮件找回暂未开放，请联系管理员重置密码。</p> : <form onSubmit={submit}>
        {!user && <label>{mode==='login'?'账号或邮箱':'邮箱'}<input required autoComplete="username" autoCapitalize="none" spellCheck={false} type={mode==='login'?'text':'email'} maxLength={254} value={email} onChange={e=>setEmail(e.target.value)} /></label>}
        {mode==='register' && !user && <label>称呼<input required maxLength={80} autoComplete="name" value={displayName} onChange={e=>setDisplayName(e.target.value)} /></label>}
        {(user || mode!=='forgot') && <label>{user?'当前密码':'密码'}<input required minLength={mode==='register'?8:1} type="password" autoComplete={mode==='register'?'new-password':'current-password'} value={password} onChange={e=>setPassword(e.target.value)} /></label>}
        {user && <label>新密码<input required minLength={8} type="password" autoComplete="new-password" value={newPassword} onChange={e=>setNewPassword(e.target.value)} /></label>}
        {(user || mode==='register') && <label>再次输入{user ? '新密码' : '密码'}<input required minLength={8} maxLength={256} type="password" autoComplete="new-password" value={confirmPassword} onChange={e=>setConfirmPassword(e.target.value)} /></label>}
        <button type="submit" className="primary-button" disabled={busy || (!user && mode==='register' && !capabilities?.registrationEnabled)}>{busy?'正在处理…':user?'保存新密码':mode==='register'?'创建账号':mode==='forgot'?'发送重置邮件':'登录'}</button>
      </form>}
    </div>
  </section>
}
