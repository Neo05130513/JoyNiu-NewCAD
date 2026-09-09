import { useEffect, useRef, useState } from 'react'

export function WorkspaceDialog({ title, onClose, children }) {
  const ref = useRef(null)
  useEffect(() => {
    const dialog = ref.current
    dialog.showModal()
    dialog.querySelector('input:not([type="hidden"]), textarea, select')?.focus()
    return () => dialog.close()
  }, [])
  return <dialog ref={ref} className="workspace-dialog" aria-label={title} onCancel={onClose} onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <header><h2>{title}</h2><button type="button" aria-label="关闭对话框" onClick={onClose}>×</button></header>
    {children}
  </dialog>
}

export function NewProjectDialog({ suggestedName, onSubmit, onClose }) {
  const [name, setName] = useState(suggestedName)
  return <WorkspaceDialog title="新建项目" onClose={onClose}><form onSubmit={(event) => { event.preventDefault(); if (name.trim()) onSubmit(name.trim()) }}>
    <label>项目名称<input autoFocus required maxLength={100} value={name} onChange={(event) => setName(event.target.value)} /></label>
    <p>创建独立项目和首个空白零件文件。模型、图纸、对话与版本按文件保存。</p>
    <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" disabled={!name.trim()}>创建项目</button></div>
  </form></WorkspaceDialog>
}

export function CommandDialog({ commands, onClose }) {
  const [query, setQuery] = useState('')
  const visible = commands.filter((command) => command.label.toLowerCase().includes(query.trim().toLowerCase()))
  return <WorkspaceDialog title="快捷命令" onClose={onClose}><input autoFocus aria-label="搜索命令" placeholder="搜索工作台、项目或操作" value={query} onChange={(event) => setQuery(event.target.value)} />
    <div className="command-list">{visible.map((command) => <button key={command.label} onClick={() => { onClose(); command.action() }}><span>{command.label}</span><small>{command.shortcut || '↗'}</small></button>)}{!visible.length && <p>没有匹配的命令。</p>}</div>
  </WorkspaceDialog>
}

export function SettingsWorkspace({ settings, onChange, onExportBackup }) {
  return <section className="secondary-workspace utilities-workspace"><div className="secondary-heading"><div><h1>设置</h1><p>偏好保存在当前浏览器，应用于新建文件。</p></div></div><div className="panel-card settings-form">
    <label>新零件默认材料<select value={settings.defaultMaterial} onChange={(event) => onChange({ ...settings, defaultMaterial: event.target.value })}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select></label>
    <label>默认三维视角<select value={settings.defaultView} onChange={(event) => onChange({ ...settings, defaultView: event.target.value })}><option value="isometric">等轴测</option><option value="front">前视</option><option value="top">俯视</option></select></label>
    <label>界面文字大小<select value={settings.textSize} onChange={(event) => onChange({ ...settings, textSize: event.target.value })}><option value="normal">标准</option><option value="large">大号</option></select></label>
    <div className="settings-note"><b>草稿自动保存</b><p>项目和文件自动保存到此浏览器。点击“保存版本”建立独立恢复点。平台同步状态仅在服务器保存成功后更新。</p><button className="secondary-button" onClick={onExportBackup}>导出所有项目备份</button></div>
  </div></section>
}

export function HelpWorkspace({ onNavigate, onDiagnostics }) {
  return <section className="secondary-workspace utilities-workspace"><div className="secondary-heading"><div><h1>帮助与反馈</h1><p>按当前版本支持的流程继续设计。</p></div></div><div className="panel-card help-content">
    <h2>从草稿到交付</h2><ol><li>新建项目，在项目中创建零件、工程图、装配或文档文件。</li><li>在 3D 工作台描述设计或上传图纸。AI 返回的图纸尺寸需要逐项确认。</li><li>检查参数与尺寸关系，生成受支持配方的 OCCT 实体。</li><li>保存版本；到工程图查看投影、图层和尺寸，导出 DXF。</li><li>在 PDM 中登录并同步当前文件。生产 STEP 的实体校验与制造放行分别执行。</li></ol>
    <h2>可用能力</h2><p>轴类提供参数草稿；安装支架、开口夹紧座和阶梯锥管嘴可由几何服务生成生产实体。工程图是参数投影视图。装配提供实例编辑与包络、同轴间隙预检查，正式加工仍需实体及工艺审核。</p>
    <h2>快捷键</h2><p>⌘ / Ctrl + K：命令面板；⌘ / Ctrl + N：新建项目；⌘ / Ctrl + S：保存当前文件版本；Esc：关闭对话框或菜单。</p>
    <h2>遇到问题</h2><p>服务离线时保留本地草稿；需要认证时进入账号登录，然后回到工作台重试。刷新后文件内容需要重新选择，但已保存的尺寸和对话仍可恢复。</p>
    <div className="dialog-actions"><button className="secondary-button" onClick={() => onNavigate('平台服务')}>打开账号与服务</button><button className="secondary-button" onClick={onDiagnostics}>下载诊断信息</button></div>
  </div></section>
}
