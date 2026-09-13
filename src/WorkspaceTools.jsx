import { useEffect, useRef, useState } from 'react'

export function WorkspaceDialog({ title, onClose, children }) {
  const ref = useRef(null)
  useEffect(() => {
    const dialog = ref.current
    dialog.showModal()
    dialog.querySelector('input:not([type="hidden"]), textarea, select')?.focus()
    return () => dialog.close()
  }, [])
  return <dialog ref={ref} className="workspace-dialog" aria-label={title} onCancel={onClose} onClick={(event) => {
    if (event.target !== event.currentTarget) return
    const bounds = event.currentTarget.getBoundingClientRect()
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose()
  }}>
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
  const [selected, setSelected] = useState(0)
  const listRef = useRef(null)
  const visible = commands.filter((command) => command.label.toLowerCase().includes(query.trim().toLowerCase()))
  const run = (command) => { if (command) { onClose(); command.action() } }
  useEffect(() => { listRef.current?.children[selected]?.scrollIntoView({ block: 'nearest' }) }, [selected])
  return <WorkspaceDialog title="快捷命令" onClose={onClose}><input autoFocus aria-label="搜索命令" role="combobox" aria-expanded="true" aria-controls="workspace-command-list" aria-activedescendant={visible.length ? `workspace-command-${selected}` : undefined} placeholder="搜索工作台、项目或操作" value={query} onChange={(event) => { setQuery(event.target.value); setSelected(0) }} onKeyDown={(event) => {
    if (event.nativeEvent.isComposing) return
    if (['ArrowDown', 'ArrowUp'].includes(event.key) && visible.length) { event.preventDefault(); setSelected((index) => (index + (event.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length) }
    if (event.key === 'Enter') { event.preventDefault(); run(visible[selected]) }
  }} />
    <p>↑ ↓ 选择命令 · Enter 执行 · Esc 关闭</p>
    <div ref={listRef} id="workspace-command-list" role="listbox" aria-label="快捷命令列表" className="command-list">{visible.map((command, index) => <button id={`workspace-command-${index}`} role="option" aria-selected={selected === index} key={command.label} onFocus={() => setSelected(index)} onClick={() => run(command)}><span>{command.label}</span><small>{command.shortcut || '↗'}</small></button>)}{!visible.length && <p>没有匹配的命令。</p>}</div>
  </WorkspaceDialog>
}

export function SettingsWorkspace({ settings, onChange, onExportBackup, backupImport }) {
  return <section className="secondary-workspace utilities-workspace"><div className="secondary-heading"><div><h1>设置</h1><p>偏好保存在当前浏览器，应用于新建文件。</p></div></div><div className="panel-card settings-form">
    <label>新零件默认材料<select value={settings.defaultMaterial} onChange={(event) => onChange({ ...settings, defaultMaterial: event.target.value })}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select></label>
    <label>默认三维视角<select value={settings.defaultView} onChange={(event) => onChange({ ...settings, defaultView: event.target.value })}><option value="isometric">等轴测</option><option value="front">前视</option><option value="top">俯视</option></select></label>
    <label>界面文字大小<select value={settings.textSize} onChange={(event) => onChange({ ...settings, textSize: event.target.value })}><option value="normal">标准</option><option value="large">大号</option></select></label>
    <div className="settings-note"><b>草稿自动保存与备份</b><p>登录后项目按当前账号保存到云端，并保留此浏览器的草稿缓存；未登录时只保存到访客工作区。请留意顶部保存状态，点击“保存版本”建立恢复点。备份保存参数、文档、对话及版本记录；原图和实体文件仍由原服务器提供。导入备份会添加项目，保留现有内容。</p><div className="settings-backup-actions"><button className="secondary-button" onClick={onExportBackup}>导出所有项目备份</button>{backupImport}</div></div>
  </div></section>
}

export function HelpWorkspace({ onNavigate, onDiagnostics }) {
  return <section className="secondary-workspace utilities-workspace"><div className="secondary-heading"><div><h1>帮助与反馈</h1><p>按当前版本支持的流程继续设计。</p></div></div><div className="panel-card help-content">
    <h2>从草稿到交付</h2><ol><li>登录自己的账号，新建项目与零件文件。</li><li>在 3D 建模页描述设计或上传原图。任务会保存在当前账号中。</li><li>逐项核对名义参数、派生尺寸和原图依据；补充未决问题，检查实体与视图差异。</li><li>确认当前模型后，到 2D 工程图查看实际投影，并导出当前版本支持的实体、图纸或核对报告。</li><li>保存版本和项目备份。核对报告只记录已经检查的范围；制造放行仍需责任人员确认。</li></ol>
    <h2>上传前准备</h2><p>当前每次最多 4 个文件，每个不超过 20 MB。可上传 DWG、DXF、PDF，以及 PNG、JPG/JPEG、WebP、GIF、BMP 图片；格式可上传不等于所有版本、页数或零件均已验收。首批建议使用视图清楚、单位与关键尺寸齐全的单个机械零件图，说明需要哪些零件、螺纹及小特征是否建模。多页 PDF 请先提取相关页，确认图纸核对页中需要的视图都可见。</p><p>DWG 转换失败时，保留原图并用原 CAD 软件另存或导出清晰 PDF/PNG；不要只改文件扩展名。尺寸缺失、视图矛盾或形状不确定时先补充依据，候选模型仍须人工核对。</p>
    <h2>查看与修改</h2><p>建模页保留 3D 预览，可旋转、缩放、切换视角和剖切。参数用中文显示；有上传原图时，打开“图纸核对”，悬停参数查看对应尺寸，点击可固定核对。修改尺寸后，重新构建并检查当前版本再导出。</p>
    <h2>可用能力与入口</h2><ul><li>“特征编辑”可修改参数、公式、草图和特征顺序，添加齿轮、弹簧、扫掠、放样、抽壳及阵列，重建后保存版本并下载实体。</li><li>“原生二维”直接编辑 DWG / DXF 中的线、圆、弧、轮廓、文字和尺寸；提供图层、捕捉、约束、参数图库、专业构件、气泡检验图，以及 DXF / DWG / PDF / 尺寸 Excel 导出。</li><li>“工程设计”可上传 STEP，选择真实拓扑测量，建立多零件装配、配合求解和实体干涉检查，导出装配 STEP / BOM，并从实体生成带尺寸的二维工程图。</li><li>“照片建模”可添加多角度照片和已知尺度；“案例与教程”提供可编辑的机械案例及分步操作说明。</li></ul><p>原生二维、手工特征设计和工程设计分别保存自己的文档与版本。手工重建成功的零件会加入工程设计库；从 AI 任务导入时仍需先核对并确认对应实体。</p>
    <h2>项目与数据恢复</h2><p>在“我的项目”中管理文件、复制设计、编辑文档和保存版本。误删内容可从回收站恢复；恢复历史版本前会自动保留当前草稿。设置中的备份导入可恢复项目、文件及版本记录。</p>
    <h2>云保存与账号切换</h2><p>登录后的工作区属于当前账号，切换账号会加载另一个账号自己的项目。顶部显示“已保存”后才表示最新更改已写入云端；网络异常时保留本地草稿，恢复连接后可重试。多个窗口修改同一账号会产生版本冲突，先导出本地备份，再决定读取云端版本，避免覆盖未同步内容。</p><p>旧浏览器公共项目不会自动进入任何登录账号。只有确认这些旧项目属于自己后，才能点击一次性导入；导入会添加项目，不会用旧库替换当前项目。</p>
    <h2>任务进度、恢复与取消</h2><p>“我的任务”列出当前账号的实际处理记录，排队和处理中会自动刷新。离开页面或停止等待不等于取消后台任务；需要停止时使用“取消任务”，只有服务端返回“已取消”才表示确实停止。取消请求、网络错误和连接中断都不会显示成建模完成。</p><p>刷新后可从保存的任务编号继续查询；连接异常时先检查后台结果，不要重复上传同一任务。已取消或失败的记录会保留已保存的原图与草稿，可打开继续处理。</p>
    <h2>线下充值与积分结算</h2><p>当前采用线下收款、管理员充值，基础兑换为 10 元充值 1000 积分。联系为你开通账号的运营人员，核对客户账号与收款信息；付款后由运营登记充值单。到“积分与订单”查看到账记录、实际余额及流水；发现未到账或金额有误时，提交“积分与订单”工单并提供充值单号，避免重复付款。</p><p>计费启用并发布完整规则后，按实际 token 用量、已发布单价及管理员设置的倍率结算，不按模型个数固定扣分。成功候选（待确认或已确认）进入结算；待补充、失败、取消和中断的本次尝试不扣分。候选结果仍需核对，不代表已经达到制造放行要求。缺少用量或计价依据时保持待核账，不猜测扣分。</p><p>余额不足时先扣已有积分，差额记为待补缴，后续充值优先补缴；存在待补缴时不能开始新的收费任务。已生成文件的查看、旋转和重复下载不重复计费。修改或重试会产生新的尝试，按其实际状态及记录的规则结算；具体费率、倍率、启用状态以当前发布规则和任务结算记录为准。</p>
    <h2>线下退款、发票与质量申诉</h2><p>通过“支持与工单”选择“积分与订单”，填写充值单号、任务编号及退款或开票需求，由运营核对实际收款与积分使用情况。申请或审核通过不表示人民币已经退回；以实际线下退款凭证和处理记录为准。积分补偿、现金退款与发票是不同记录。质量问题请提供有差异的尺寸、视图、当前版本和预期结果，不必重复上传已经保存的图纸。</p>
    <h2>支持工单</h2><p>在“支持与工单”提交问题并保留实际工单编号，之后可追加说明、查看回复或关闭工单。可选关联自己的任务编号；附图授权默认不勾选，授权后也可撤回，关闭工单后仍可撤回。当前授权只记录人工处理依据，不向管理员开放原图下载。请在工单内查看回复，当前不发送邮件或短信，也不承诺处理时效。</p>
    <h2>导出与客户 CAD 验收</h2><p>确认前核对单位、关键尺寸、孔槽位置、壁厚、实体数量与未建模的小特征。导出的 STEP 需要在你实际使用的 CAD 软件中打开，复查单位、实体数量、包络尺寸和关键孔径，并保存当前版本的核对报告。STEP 是实体交换文件，不承诺包含 NX、SolidWorks 或 Creo 的原生特征树；模型预览可旋转也不代表 STEP 已在你的软件中验收。</p>
    <h2>数据保留与删除申请</h2><p>回收站用于恢复项目，撤回附图授权用于停止当前授权，它们都不等于服务器物理删除。到“支持与工单”点击“申请删除数据”，选择原图、模型、项目与对话、账号、工单或备份范围，并填写项目、任务编号和时间范围。运营核对后会提供人工处理回执，列明实际处理范围、仍保留的数据及原因、保留期限和备份安排。未提供回执前不能认定已经删除；申请或关闭工单不会自动删除数据。</p>
    <h2>快捷键</h2><p>⌘ / Ctrl + K：命令面板；⌘ / Ctrl + N：新建项目；⌘ / Ctrl + S：保存当前文件版本；Esc：关闭对话框或菜单。</p>
    <h2>遇到问题</h2><p>已打开的项目在网络异常时保留本地草稿；重新登录需要连接服务端读取对应账号，不能用其他账号或公共旧库代替。已保存的原图可在原服务器在线时继续核对；尚未上传完成的附件需要重新选择。原图定位失败可点击重试；登录或服务异常可在“账号与服务”刷新状态，再继续操作。</p>
    <div className="dialog-actions"><button className="secondary-button" onClick={() => onNavigate('支持与工单')}>打开支持与工单</button><button className="secondary-button" onClick={() => onNavigate('平台服务')}>打开账号与服务</button><button className="secondary-button" onClick={onDiagnostics}>下载诊断信息</button></div>
  </div></section>
}
