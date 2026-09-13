import { useEffect, useRef, useState } from 'react'
import { cadAgent } from './cadAgentClient.js'
import { activeTaskStatuses, cancellationResult, createTaskPoller, filterTaskPage, taskPageOffset, taskStatusLabel } from './taskWorkspaceState.js'
import './task-workspace.css'

export { activeTaskStatuses } from './taskWorkspaceState.js'
const PAGE_SIZE = 20
const dateText = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN') : '—'

function TaskSession({ token, onOpen, onStart }) {
  const [records, setRecords] = useState(null)
  const [offset, setOffset] = useState(0)
  const [filter, setFilter] = useState('all')
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const active = useRef(false)
  const operations = useRef(null)
  const poller = useRef(null)
  const busyRef = useRef(false)
  const currentToken = useRef(token)
  currentToken.current = token
  useEffect(() => {
    active.current = true
    operations.current = new AbortController()
    return () => { active.current = false; operations.current.abort() }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    const reader = createTaskPoller({
      isVisible: () => document.visibilityState === 'visible',
      load: () => cadAgent.jobs({ token, signal: controller.signal, offset, limit: PAGE_SIZE }),
      onValue: result => {
        if (controller.signal.aborted || currentToken.current !== token) return
        if (!Array.isArray(result?.items) || !Number.isSafeInteger(result.total) || result.total < 0) { setError('任务列表响应无效，请刷新重试。'); setLoading(false); return }
        const bounded = taskPageOffset(result.total, offset)
        if (bounded !== offset) { setRecords(null); setOffset(bounded); return }
        setRecords(result); setError(''); setLoading(false)
      },
      onError: cause => { if (!controller.signal.aborted && currentToken.current === token) { setError(cause.message); setLoading(false) } },
    })
    poller.current = reader
    const visibility = () => reader.visibilityChanged()
    document.addEventListener('visibilitychange', visibility)
    reader.start()
    return () => { reader.stop(); controller.abort(); document.removeEventListener('visibilitychange', visibility); if (poller.current === reader) poller.current = null }
  }, [token, offset])
  async function act(job, kind) {
    if (busyRef.current) return
    busyRef.current = true; setBusy(`${kind}:${job.runId}`); setActionError(''); setNotice('')
    try {
      if (kind === 'cancel') {
        const result = await cadAgent.cancel({ token, runId: job.runId, signal: operations.current.signal })
        if (!active.current || currentToken.current !== token) return
        const accepted = cancellationResult(result, job.runId)
        setRecords(current => current ? { ...current, items: current.items.map(item => item.runId === job.runId ? { ...item, ...accepted.patch } : item) } : current)
        setNotice(accepted.notice); poller.current?.refresh()
      } else if (kind === 'revoke') {
        const result = await cadAgent.revokeFileLinks({ token, runId: job.runId, signal: operations.current.signal })
        if (!active.current || currentToken.current !== token) return
        if (result?.runId !== job.runId || !result.revokedAt) throw new Error('尚未取得链接撤销结果，请刷新后重试。')
        setNotice('此前的临时下载链接已失效。重新打开模型即可取得新链接；已下载的文件不受影响。')
      } else {
        if (typeof onOpen !== 'function') throw new Error('工作台暂不可打开，请稍后重试。')
        const opened = await onOpen(job, operations.current.signal)
        if (opened === false) throw new Error('任务未能打开，请重试。')
      }
    } catch (cause) { if (active.current && currentToken.current === token && cause.name !== 'AbortError') setActionError(cause.message) }
    finally { busyRef.current = false; if (active.current) setBusy('') }
  }
  const items = filterTaskPage(records?.items || [], filter)
  const changePage = next => { setRecords(null); setOffset(next) }
  return <section className="task-workspace" aria-label="我的建模任务">
    <header><div><span className="task-eyebrow">JOYNIU CAD · TASKS</span><h1>我的任务</h1><p>离开页面后仍可查看进度，完成后打开模型继续核对。</p></div><button className="secondary-button" onClick={() => { setLoading(true); poller.current?.refresh() }} disabled={loading}>{loading ? '正在读取…' : '刷新任务'}</button></header>
    <div className="task-toolbar"><label>筛选本页任务 <select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部状态</option><option value="active">正在进行</option><option value="attention">需要处理</option></select></label><span>{records ? `共 ${records.total} 次处理记录` : '正在读取记录总数'} · 同一任务的修改和补充信息分别保留</span></div>
    {error && <p className="task-error" role="alert">{error}<button onClick={() => poller.current?.refresh()}>重试读取</button></p>}
    {actionError && <p className="task-error" role="alert">{actionError}</p>}{notice && <p className="task-notice" role="status">{notice}</p>}
    {loading && !records && <p className="task-loading" role="status">正在读取任务…</p>}
    {!loading && records && !items.length && <div className="task-empty"><h2>{records.total ? '本页没有符合条件的任务' : '还没有建模任务'}</h2><p>{records.total ? '可切换筛选或翻页查看其他记录。' : '上传图纸或提交建模要求后，处理记录会出现在这里。'}</p>{!records.total&&onStart&&<button className="primary-button" onClick={onStart}>开始设计</button>}</div>}
    <div className="task-list">{items.map(job => <article key={job.runId} className="task-card">
      <div className="task-card-title"><h2>{job.name || '建模任务'}</h2><span className={`task-status ${job.status}`}>{taskStatusLabel(job.status)}</span></div>
      <p>{job.progress?.message && activeTaskStatuses.includes(job.status) ? job.progress.message : job.message || '处理记录已保存。'}</p>
      {job.sourceFiles?.length > 0 && <p className="task-files">图纸：{job.sourceFiles.map(file => file.filename).filter(Boolean).join('、')}</p>}
      <div className="task-meta"><span>开始于 {dateText(job.createdAt)}</span><span>第 {job.revision || 1} 版</span><span>{({ new_drawing: '新图纸', continuation: '补充与继续', revision: '模型修改' })[job.changeKind] || '建模'}</span></div>
      {job.completedAt && <p className="task-files">完成于 {dateText(job.completedAt)}</p>}
      <details><summary>任务编号与文件访问</summary><p className="task-identifier">{job.jobId}<br />本次记录：{job.runId}</p><p>若曾分享过临时下载链接，可使旧链接立即失效。已下载的文件不受影响。</p><button className="secondary-button" disabled={Boolean(busy)} onClick={() => act(job, 'revoke')}>{busy === `revoke:${job.runId}` ? '正在撤销…' : '使旧下载链接失效'}</button></details>
      <footer><button className="primary-button" disabled={Boolean(busy)} onClick={() => act(job, 'open')}>{busy === `open:${job.runId}` ? '正在打开…' : activeTaskStatuses.includes(job.status) ? '打开工作台' : ['ready', 'review_required'].includes(job.status) ? '查看模型' : '查看并继续'}</button>{activeTaskStatuses.includes(job.status) && <button className="secondary-button" disabled={Boolean(busy) || job.status === 'cancel_requested'} onClick={() => act(job, 'cancel')}>{busy === `cancel:${job.runId}` ? '正在请求停止…' : job.status === 'cancel_requested' ? '正在等待停止…' : '取消任务'}</button>}</footer>
    </article>)}</div>
    {records && <nav className="task-pagination" aria-label="任务分页"><button disabled={offset === 0 || loading} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}>上一页</button><span>第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(records.total / PAGE_SIZE))} 页</span><button disabled={offset + PAGE_SIZE >= records.total || loading} onClick={() => changePage(offset + PAGE_SIZE)}>下一页</button></nav>}
  </section>
}

export default function TaskWorkspace({ account, onLogin, onStart, onOpen }) {
  if (!account?.session?.access_token) return <section className="task-workspace"><h1>我的任务</h1><p>登录后查看你的建模进度和处理记录。</p><button className="primary-button" onClick={onLogin}>登录账号</button></section>
  return <TaskSession key={account.session.user?.id || account.session.access_token} token={account.session.access_token} onOpen={onOpen} onStart={onStart} />
}
