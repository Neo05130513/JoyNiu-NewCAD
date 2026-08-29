import { useEffect, useMemo, useRef, useState } from 'react'

const defaultModel = {
  name: '动力轴 · 版本 04',
  type: '零件',
  outerDiameter: 24,
  length: 70,
  holeDiameter: 10,
  keywayWidth: 6,
  keywayDepth: 3,
  keywayLength: 40,
  material: '45# 钢',
  updatedAt: '刚刚',
}

const defaultProjects = [
  { id: 'p1', name: '创模AI · 机械传动', files: 8, updated: '今天 14:32', color: 'blue' },
  { id: 'p2', name: '保持架结构复刻', files: 12, updated: '昨天 09:18', color: 'orange' },
  { id: 'p3', name: '试制夹具 - A17', files: 5, updated: '8 月 26 日', color: 'violet' },
]

const features = [
  { id: 'origin', icon: '◎', label: '原点', meta: '基准' },
  { id: 'plane', icon: '△', label: 'XY 基准面', meta: '基准面' },
  { id: 'sketch', icon: '▱', label: '草图 01 · 轴截面', meta: '已约束' },
  { id: 'pad', icon: '▰', label: '拉伸 · 70 mm', meta: '实体' },
  { id: 'hole', icon: '◉', label: '通孔 · Ø10 mm', meta: '切除' },
  { id: 'keyway', icon: '⌗', label: '键槽 · 6 × 3 × 40', meta: '切除' },
  { id: 'chamfer', icon: '◇', label: '倒角 · 1 mm', meta: '细节' },
]

const libraryItems = [
  { icon: '⬡', name: '六角螺栓', spec: 'M8 × 30', group: '紧固件' },
  { icon: '◉', name: '深沟球轴承', spec: '6204 · 20 × 47 × 14', group: '轴承' },
  { icon: '⬢', name: '圆柱销', spec: 'Ø6 × 24', group: '定位件' },
  { icon: '◌', name: '弹簧垫圈', spec: 'M8', group: '紧固件' },
  { icon: '▣', name: '法兰螺母', spec: 'M10', group: '紧固件' },
]

function parsePrompt(prompt, current) {
  const next = { ...current }
  const match = (patterns, fallback) => {
    for (const pattern of patterns) {
      const result = prompt.match(pattern)
      if (result) return Number(result[1])
    }
    return fallback
  }
  next.outerDiameter = match([/外径\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /直径\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /OD\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.outerDiameter)
  next.length = match([/总长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /长度\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /length\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.length)
  if (next.length === current.length) {
    const shortLength = prompt.match(/长\s*[:：]?\s*(\d+(?:\.\d+)?)/i)
    const keywayIndex = prompt.indexOf('键槽')
    const featureIndex = prompt.search(/(?:宽|深)\s*[:：]?\s*\d/i)
    if (shortLength && (featureIndex < 0 || shortLength.index < featureIndex) && (keywayIndex < 0 || shortLength.index < keywayIndex)) next.length = Number(shortLength[1])
  }
  next.holeDiameter = match([/通孔\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /内径\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /孔径\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.holeDiameter)
  next.keywayWidth = match([/(?:键槽[^\d]*?)?(?:槽宽|宽度?|宽)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.keywayWidth)
  next.keywayDepth = match([/(?:键槽[^\d]*?)?(?:槽深|深度?|深)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.keywayDepth)
  next.keywayLength = match([/(?:键槽[^\d]*?)?(?:槽长|长度?|长)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.keywayLength)
  if (/铝|al6061/i.test(prompt)) next.material = 'AL6061 铝合金'
  if (/不锈钢|304/i.test(prompt)) next.material = 'SUS304 不锈钢'
  return next
}

function Icon({ children, className = '' }) {
  return <span className={`icon ${className}`}>{children}</span>
}

function App() {
  const [activeMode, setActiveMode] = useState('3D 建模')
  const [activePanel, setActivePanel] = useState('参数')
  const [model, setModel] = useState(() => {
    try { return JSON.parse(localStorage.getItem('joyniu-model')) || defaultModel } catch { return defaultModel }
  })
  const [projects, setProjects] = useState(() => {
    try { return JSON.parse(localStorage.getItem('joyniu-projects')) || defaultProjects } catch { return defaultProjects }
  })
  const [selectedProject, setSelectedProject] = useState('创模AI · 机械传动')
  const [selectedFeature, setSelectedFeature] = useState('keyway')
  const [prompt, setPrompt] = useState('创建一根动力轴：外径24，长度70，通孔10；增加一条宽6、深3、长40的键槽')
  const [messages, setMessages] = useState([
    { role: 'ai', text: '已加载「动力轴 · 版本 04」。我会把你的描述转成可编辑的参数和特征。' },
    { role: 'ai', text: '当前模型包含 7 个特征，关键尺寸均已标注。你可以直接修改参数，或继续告诉我想要的结构。' },
  ])
  const [isGenerating, setIsGenerating] = useState(false)
  const [toast, setToast] = useState('')
  const [view, setView] = useState('isometric')
  const [section, setSection] = useState(false)
  const [zoom, setZoom] = useState(1)
  const [rotation, setRotation] = useState({ x: -13, y: 28 })
  const dragRef = useRef(null)
  const [drawingScale, setDrawingScale] = useState('1:1')
  const [assemblyChecked, setAssemblyChecked] = useState(false)
  const [libraryQuery, setLibraryQuery] = useState('')
  const [libraryGroup, setLibraryGroup] = useState('全部')

  useEffect(() => localStorage.setItem('joyniu-model', JSON.stringify(model)), [model])
  useEffect(() => localStorage.setItem('joyniu-projects', JSON.stringify(projects)), [projects])
  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(''), 2500); return () => clearTimeout(t) } }, [toast])

  const showToast = (text) => setToast(text)
  const updateModel = (key, value) => setModel((prev) => ({ ...prev, [key]: key === 'material' ? value : value === '' ? '' : Number(value) }))
  const modelValid = Object.entries(model).filter(([key]) => ['outerDiameter', 'length', 'holeDiameter', 'keywayWidth', 'keywayDepth', 'keywayLength'].includes(key)).every(([, value]) => Number(value) > 0)
  const filteredLibrary = useMemo(() => libraryItems.filter((item) => (libraryGroup === '全部' || item.group === libraryGroup) && `${item.name}${item.spec}`.includes(libraryQuery)), [libraryGroup, libraryQuery])

  const runGenerate = () => {
    if (!prompt.trim()) return showToast('请先描述你想创建的零件')
    setIsGenerating(true)
    setMessages((prev) => [...prev, { role: 'user', text: prompt }])
    window.setTimeout(() => {
      const next = parsePrompt(prompt, model)
      setModel({ ...next, updatedAt: '刚刚' })
      setMessages((prev) => [...prev, { role: 'ai', text: `参数已更新：Ø${next.outerDiameter} × ${next.length} mm，通孔 Ø${next.holeDiameter}；键槽 ${next.keywayWidth} × ${next.keywayDepth} × ${next.keywayLength} mm。` }])
      setIsGenerating(false)
      showToast('AI 参数化建模完成')
    }, 900)
  }

  const resetModel = () => { setModel(defaultModel); showToast('已恢复基准参数') }
  const createProject = () => {
    const name = `新建项目 · ${projects.length + 1}`
    setProjects((prev) => [{ id: `p${Date.now()}`, name, files: 1, updated: '刚刚', color: 'green' }, ...prev])
    setSelectedProject(name)
    showToast('项目已创建并加入最近项目')
  }
  const exportFile = (format) => {
    if (!modelValid) return showToast('请先补齐有效参数，再导出')
    const payload = format === 'json' ? JSON.stringify({ model, project: selectedProject, exportedAt: new Date().toISOString() }, null, 2) : `JoyNiu NewCAD demo export\nproject=${selectedProject}\nOD=${model.outerDiameter}\nL=${model.length}\nID=${model.holeDiameter}\nkeyway=${model.keywayWidth}x${model.keywayDepth}x${model.keywayLength}`
    const blob = new Blob([payload], { type: format === 'json' ? 'application/json' : 'text/plain' })
    const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `${model.name.replaceAll(' ', '-')}.${format === 'json' ? 'json' : format}`; a.click(); URL.revokeObjectURL(url)
    showToast(`${format.toUpperCase()} 导出任务已完成`)
  }
  const insertLibrary = (item) => { setMessages((prev) => [...prev, { role: 'ai', text: `已将「${item.name} ${item.spec}」加入项目零件库，可在装配中插入。` }]); showToast(`${item.name} 已加入项目`) }
  const onPointerDown = (event) => { dragRef.current = { x: event.clientX, y: event.clientY, rotation } }
  const onPointerMove = (event) => { if (!dragRef.current) return; setRotation({ x: dragRef.current.rotation.x + (event.clientY - dragRef.current.y) * -0.35, y: dragRef.current.rotation.y + (event.clientX - dragRef.current.x) * 0.35 }) }
  const onPointerUp = () => { dragRef.current = null }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><div className="brand-mark">N</div><div><strong>JoyNiu</strong><span>NEW CAD</span></div></div>
        <div className="topbar-center">
          {['首页', '3D 建模', '2D 工程图', '装配'].map((mode) => <button key={mode} className={`mode-tab ${activeMode === mode ? 'active' : ''}`} onClick={() => setActiveMode(mode)}>{mode === '3D 建模' && <Icon>✦</Icon>}{mode}</button>)}
        </div>
        <div className="topbar-actions"><span className="credits"><Icon>◈</Icon> 972 积分</span><button className="icon-button" onClick={() => showToast('快捷键：⌘K 打开 AI 命令')}>⌘K</button><button className="avatar">J</button></div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <button className="new-project" onClick={createProject}><span>＋</span> 新建项目 <kbd>⌘N</kbd></button>
          <div className="side-section"><div className="side-label">工作台</div>
            <button className="side-link active"><Icon>▦</Icon> 我的项目 <span className="count">{projects.length}</span></button>
            <button className="side-link" onClick={() => showToast('最近打开：动力轴 · 版本 04')}><Icon>◷</Icon> 最近打开</button>
            <button className="side-link" onClick={() => showToast('回收站保留 15 天')}><Icon>♧</Icon> 回收站</button>
            <button className={`side-link ${activeMode === '标准件库' ? 'active' : ''}`} onClick={() => setActiveMode('标准件库')}><Icon>⬡</Icon> 标准件库</button>
          </div>
          <div className="side-section project-list"><div className="side-label">项目</div>{projects.map((project) => <button key={project.id} className={`project-link ${selectedProject === project.name ? 'selected' : ''}`} onClick={() => { setSelectedProject(project.name); showToast(`已切换到 ${project.name}`) }}><span className={`project-dot ${project.color}`} />{project.name}<span className="project-files">{project.files}</span></button>)}</div>
          <div className="sidebar-bottom"><button className="side-link" onClick={() => showToast('设置面板即将开放')}><Icon>⚙</Icon> 设置</button><button className="side-link" onClick={() => showToast('帮助中心：support@joyniu.local')}><Icon>?</Icon> 帮助与反馈</button><div className="engine-status"><span className="status-dot" /><div><b>演示内核</b><small>本地可审计模式</small></div></div></div>
        </aside>

        <main className="main-area">
          <div className="breadcrumb"><span>{selectedProject}</span><Icon>›</Icon><b>{activeMode === '首页' ? '项目概览' : activeMode}</b><span className="save-status"><span className="status-dot" /> 已自动保存</span></div>
          {activeMode === '3D 建模' && <ModelWorkspace {...{ activePanel, setActivePanel, model, modelValid, updateModel, resetModel, features, selectedFeature, setSelectedFeature, prompt, setPrompt, runGenerate, isGenerating, messages, view, setView, section, setSection, zoom, setZoom, rotation, onPointerDown, onPointerMove, onPointerUp, exportFile, showToast }} />}
          {activeMode === '2D 工程图' && <DrawingWorkspace model={model} drawingScale={drawingScale} setDrawingScale={setDrawingScale} exportFile={exportFile} showToast={showToast} />}
          {activeMode === '装配' && <AssemblyWorkspace model={model} assemblyChecked={assemblyChecked} setAssemblyChecked={setAssemblyChecked} showToast={showToast} />}
          {activeMode === '标准件库' && <LibraryWorkspace libraryQuery={libraryQuery} setLibraryQuery={setLibraryQuery} libraryGroup={libraryGroup} setLibraryGroup={setLibraryGroup} filteredLibrary={filteredLibrary} insertLibrary={insertLibrary} showToast={showToast} />}
          {activeMode === '首页' && <HomeWorkspace projects={projects} selectedProject={selectedProject} createProject={createProject} setActiveMode={setActiveMode} showToast={showToast} />}
        </main>
      </div>
      {toast && <div className="toast"><span className="toast-check">✓</span>{toast}</div>}
    </div>
  )
}

function ModelWorkspace(props) {
  const { activePanel, setActivePanel, model, modelValid, updateModel, resetModel, features, selectedFeature, setSelectedFeature, prompt, setPrompt, runGenerate, isGenerating, messages, view, setView, section, setSection, zoom, setZoom, rotation, onPointerDown, onPointerMove, onPointerUp, exportFile, showToast } = props
  return <div className="model-workspace">
    <section className="ai-column panel-card">
      <div className="panel-heading"><div><span className="eyebrow">AI COPILOT</span><h2>描述你的设计</h2></div><button className="more-button" onClick={() => showToast('已打开 AI 历史记录')}>•••</button></div>
      <div className="ai-mode-pill"><span className="sparkle">✦</span><b>参数化零件 Agent</b><span className="chevron">⌄</span></div>
      <div className="message-list">{messages.map((message, index) => <div key={index} className={`message ${message.role}`}><div className="message-avatar">{message.role === 'ai' ? '✦' : 'J'}</div><div className="message-bubble">{message.text}</div></div>)}{isGenerating && <div className="message ai"><div className="message-avatar">✦</div><div className="message-bubble typing"><i /><i /><i /></div></div>}</div>
      <div className="prompt-box"><textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="告诉 AI 你想设计什么…" onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') runGenerate() }} /><div className="prompt-actions"><button className="attach" onClick={() => showToast('支持上传图片、PDF、DXF')}><Icon>⌕</Icon></button><span>⌘ ↵ 运行</span><button className="run-button" disabled={isGenerating} onClick={runGenerate}>{isGenerating ? '生成中…' : '运行'}<Icon>↑</Icon></button></div></div>
      <div className="suggestions"><span>试试：</span><button onClick={() => setPrompt('创建一个带法兰和 4 个安装孔的支架')}>带法兰的支架</button><button onClick={() => setPrompt('将当前模型材质改为 AL6061 铝合金')}>更换材质</button></div>
    </section>

    <section className="viewport-column">
      <div className="viewport-toolbar"><div className="toolbar-group"><button className={view === 'isometric' ? 'selected' : ''} onClick={() => setView('isometric')}>等轴测</button><button className={view === 'front' ? 'selected' : ''} onClick={() => setView('front')}>前视</button><button className={view === 'top' ? 'selected' : ''} onClick={() => setView('top')}>俯视</button></div><div className="toolbar-group"><button onClick={() => setSection((value) => !value)} className={section ? 'selected' : ''}><Icon>◐</Icon> 剖切</button><button onClick={() => { setZoom(1); showToast('视图已重置') }}>重置视图</button></div></div>
      <div className="viewport" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerLeave={onPointerUp}>
        <div className="viewport-grid" />
        <div className="axis axis-x">X</div><div className="axis axis-y">Y</div><div className="axis axis-z">Z</div>
        <div className="scene" style={{ transform: `scale(${zoom}) rotateX(${rotation.x}deg) rotateY(${rotation.y}deg)` }}><CadModel model={model} section={section} view={view} /></div>
        <div className="view-cube"><span>TOP</span><b>FRONT</b><span>RIGHT</span></div>
        <div className="viewport-hint"><Icon>✥</Icon> 拖拽旋转 · 滚轮缩放</div>
        <div className="zoom-control"><button onClick={() => setZoom((value) => Math.min(1.35, value + .1))}>＋</button><span>{Math.round(zoom * 100)}%</span><button onClick={() => setZoom((value) => Math.max(.7, value - .1))}>−</button></div>
      </div>
      <div className="viewport-footer"><span><i className="live-dot" /> 实体已更新 · {model.updatedAt}</span><span>单位 <b>mm</b></span><span>材质 <b>{model.material}</b></span><button onClick={() => exportFile('step')}>导出 STEP <Icon>↓</Icon></button></div>
    </section>

    <aside className="inspector-column">
      <div className="inspector-tabs"><button className={activePanel === '参数' ? 'active' : ''} onClick={() => setActivePanel('参数')}>参数</button><button className={activePanel === '特征' ? 'active' : ''} onClick={() => setActivePanel('特征')}>特征树</button><button className={activePanel === '检查' ? 'active' : ''} onClick={() => setActivePanel('检查')}>检查</button></div>
      {activePanel === '参数' && <ParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} />}
      {activePanel === '特征' && <FeaturePanel features={features} selectedFeature={selectedFeature} setSelectedFeature={setSelectedFeature} />}
      {activePanel === '检查' && <CheckPanel modelValid={modelValid} showToast={showToast} />}
      <div className="export-card"><div><span className="eyebrow">交付</span><h3>准备好导出了吗？</h3><p>生成经过检查的交付文件</p></div><div className="export-buttons"><button onClick={() => exportFile('step')}>STEP</button><button onClick={() => exportFile('dxf')}>DXF</button><button onClick={() => exportFile('json')}>参数 JSON</button></div></div>
    </aside>
  </div>
}

function ParameterPanel({ model, modelValid, updateModel, resetModel }) {
  const fields = [['outerDiameter', '外径', 'Ø', 'mm'], ['length', '总长度', '', 'mm'], ['holeDiameter', '通孔直径', 'Ø', 'mm'], ['keywayWidth', '键槽宽度', '', 'mm'], ['keywayDepth', '键槽深度', '', 'mm'], ['keywayLength', '键槽长度', '', 'mm']]
  return <div className="inspector-content"><div className="selection-title"><span className="feature-icon blue">◒</span><div><b>{model.name}</b><small>参数化实体 · 已锁定</small></div><span className={`valid-chip ${modelValid ? '' : 'invalid'}`}>{modelValid ? '有效' : '待修正'}</span></div><div className="field-group"><div className="field-group-title">基本尺寸 <span>单位：mm</span></div>{fields.slice(0, 3).map(([key, label, prefix, suffix]) => <NumberField key={key} label={label} value={model[key]} prefix={prefix} suffix={suffix} onChange={(value) => updateModel(key, value)} />)}</div><div className="field-group"><div className="field-group-title">键槽特征 <span className="muted">切除</span></div>{fields.slice(3).map(([key, label, prefix, suffix]) => <NumberField key={key} label={label} value={model[key]} prefix={prefix} suffix={suffix} onChange={(value) => updateModel(key, value)} />)}</div><div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material} onChange={(e) => updateModel('material', e.target.value)}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div><button className="reset-link" onClick={resetModel}>↻ 恢复基准参数</button></div>
}

function NumberField({ label, value, prefix, suffix, onChange }) { return <label className="number-field"><span>{label}</span><div><span className="field-prefix">{prefix}</span><input value={value} type="number" min="0.1" step="0.1" onChange={(e) => onChange(e.target.value)} /><span className="field-suffix">{suffix}</span></div></label> }
function FeaturePanel({ features, selectedFeature, setSelectedFeature }) { return <div className="inspector-content feature-tree-panel"><div className="tree-toolbar"><span>特征历史 <b>{features.length}</b></span><button>＋</button></div><div className="feature-tree">{features.map((feature, index) => <button key={feature.id} className={`feature-row ${selectedFeature === feature.id ? 'selected' : ''}`} onClick={() => setSelectedFeature(feature.id)}><span className="tree-line">{index < features.length - 1 ? '│' : '└'}</span><span className="feature-glyph">{feature.icon}</span><span className="feature-label">{feature.label}<small>{feature.meta}</small></span>{selectedFeature === feature.id && <span className="eye">◉</span>}</button>)}</div><div className="feature-note"><Icon>✦</Icon><span>特征树由 AI 生成，可继续描述来添加圆角、阵列或螺纹。</span></div></div> }
function CheckPanel({ modelValid, showToast }) { const checks = [{ label: '参数完整性', status: modelValid ? '通过' : '待修正' }, { label: '实体拓扑', status: '通过' }, { label: '关键尺寸', status: '通过' }, { label: '制造可行性', status: '提示' }]; return <div className="inspector-content check-panel"><div className="check-summary"><div className={`check-ring ${modelValid ? 'ok' : 'warn'}`}>{modelValid ? '✓' : '!'}</div><div><b>{modelValid ? '模型检查通过' : '需要修正参数'}</b><small>最近检查：刚刚</small></div></div>{checks.map((check) => <div className="check-row" key={check.label}><span>{check.label}</span><span className={`check-status ${check.status === '通过' ? 'pass' : check.status === '提示' ? 'hint' : 'warn'}`}>{check.status}</span></div>)}<button className="primary-outline" onClick={() => showToast('检查报告已更新')}>重新运行检查 <Icon>↗</Icon></button></div> }

function CadModel({ model, section, view }) {
  const bodyHeight = Math.max(100, Math.min(260, model.length * 3.1)); const bodyWidth = Math.max(96, Math.min(210, model.outerDiameter * 5.5)); const hole = Math.max(12, Math.min(60, model.holeDiameter * 2.2));
  return <svg className={`cad-svg ${view}`} viewBox="0 0 420 420" role="img" aria-label="参数化轴三维预览"><defs><linearGradient id="metal" x1="0" x2="1"><stop offset="0" stopColor="#718096" /><stop offset=".22" stopColor="#d8e2ed" /><stop offset=".5" stopColor="#8fa0b5" /><stop offset=".76" stopColor="#e8eef4" /><stop offset="1" stopColor="#5d7088" /></linearGradient><linearGradient id="metalDark" x1="0" x2="1"><stop offset="0" stopColor="#53667e" /><stop offset=".5" stopColor="#afbdd0" /><stop offset="1" stopColor="#485b72" /></linearGradient><filter id="shadow"><feGaussianBlur stdDeviation="8" /></filter></defs><ellipse cx="210" cy="352" rx={bodyWidth * .72} ry="20" fill="#08101c" opacity=".6" filter="url(#shadow)" /><g transform={view === 'front' ? 'translate(35 4) rotate(-2 210 210)' : view === 'top' ? 'translate(0 54)' : 'translate(0 0)'}><ellipse cx="210" cy={210 - bodyHeight / 2} rx={bodyWidth / 2} ry="34" fill="url(#metalDark)" stroke="#d6e2ef" strokeWidth="2" /><rect x={210 - bodyWidth / 2} y={210 - bodyHeight / 2} width={bodyWidth} height={bodyHeight} rx="10" fill="url(#metal)" stroke="#b7c9dc" strokeWidth="2" /><ellipse cx="210" cy={210 + bodyHeight / 2} rx={bodyWidth / 2} ry="34" fill="url(#metalDark)" stroke="#9db0c7" strokeWidth="2" /><ellipse cx="210" cy={210 - bodyHeight / 2} rx={hole / 2} ry="10" fill="#111c2b" stroke="#d7e5f3" strokeWidth="2" /><ellipse cx="210" cy={210 + bodyHeight / 2} rx={hole / 2} ry="10" fill="#182537" stroke="#9db0c7" strokeWidth="2" /><path d={`M ${210 - model.keywayWidth * 2.4} ${210 - bodyHeight / 2 - 3} L ${210 + model.keywayWidth * 2.4} ${210 - bodyHeight / 2 - 3} L ${210 + model.keywayWidth * 2.4} ${210 - bodyHeight / 2 + model.keywayLength * 1.8} L ${210 - model.keywayWidth * 2.4} ${210 - bodyHeight / 2 + model.keywayLength * 1.8} Z`} fill={section ? '#ffb65c' : '#34465d'} opacity=".85" /><line x1={210 - bodyWidth / 2 - 24} y1={210 - bodyHeight / 2} x2={210 - bodyWidth / 2 - 24} y2={210 + bodyHeight / 2} stroke="#6f89a7" strokeDasharray="3 5" /><line x1="102" y1={210 - bodyHeight / 2} x2="102" y2={210 + bodyHeight / 2} stroke="#87a1c0" strokeWidth="1" /><text x="78" y="205" fill="#91a7c0" fontSize="11" textAnchor="middle">{model.length}</text><text x="210" y={182 - bodyHeight / 2} fill="#a8bad0" fontSize="11" textAnchor="middle">Ø{model.outerDiameter}</text></g></svg>
}

function DrawingWorkspace({ model, drawingScale, setDrawingScale, exportFile, showToast }) { return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">2D DRAWING</span><h1>动力轴 · 工程图</h1><p>自动生成三视图 · 尺寸与来源可追溯</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => showToast('已创建工程图新版本')}>＋ 新建版本</button><button className="primary-button" onClick={() => exportFile('dxf')}>导出 DXF <Icon>↓</Icon></button></div></div><div className="drawing-layout"><div className="drawing-canvas panel-card"><div className="drawing-toolbar"><div><button className="selected">选择</button><button>标注</button><button>图层</button></div><label>比例 <select value={drawingScale} onChange={(e) => setDrawingScale(e.target.value)}><option>1:1</option><option>1:2</option><option>2:1</option></select></label></div><svg className="drawing-svg" viewBox="0 0 780 500"><rect x="25" y="25" width="730" height="450" fill="#121a26" stroke="#34465d" /><text x="52" y="58" fill="#9fb2c9" fontSize="13">JOYNIU NEWCAD · 2D WORKBENCH</text><g stroke="#c8d6e5" fill="none" strokeWidth="2"><rect x="100" y="130" width="230" height="76" rx="8" /><line x1="215" y1="130" x2="215" y2="206" /><circle cx="150" cy="168" r="25" stroke="#80a9d4" /><circle cx="280" cy="168" r="25" stroke="#80a9d4" /><rect x="100" y="296" width="230" height="54" rx="5" /><line x1="100" y1="323" x2="330" y2="323" strokeDasharray="4 5" /><circle cx="215" cy="323" r="16" stroke="#80a9d4" /><rect x="450" y="125" width="178" height="84" rx="8" /><circle cx="539" cy="167" r="26" stroke="#80a9d4" /><line x1="539" y1="125" x2="539" y2="209" strokeDasharray="4 4" /></g><g stroke="#e8a85e" fill="#e8a85e" strokeWidth="1"><line x1="100" y1="105" x2="330" y2="105" /><path d="M100 105l8-4v8zM330 105l-8-4v8z" /><line x1="215" y1="105" x2="215" y2="130" strokeDasharray="3 4" /><line x1="100" y1="372" x2="330" y2="372" /><path d="M100 372l8-4v8zM330 372l-8-4v8z" /><line x1="674" y1="125" x2="674" y2="209" /><path d="M674 125l-4 8h8zM674 209l-4-8h8z" /></g><g fill="#e8a85e" fontSize="12"><text x="215" y="96" textAnchor="middle">Ø{model.outerDiameter} h7</text><text x="215" y="393" textAnchor="middle">总长 {model.length}</text><text x="688" y="171">通孔 Ø{model.holeDiameter}</text><text x="106" y="120">A</text><text x="460" y="120">B</text><text x="106" y="285">C</text></g><g fill="#7f93ad" fontSize="11"><text x="100" y="235">主视图</text><text x="100" y="376">俯视图</text><text x="450" y="235">左视图</text><text x="570" y="445">比例 {drawingScale}</text></g></svg><div className="drawing-legend"><span><i className="legend-line" /> 尺寸标注 4</span><span><i className="legend-dot" /> AI 识别来源 6</span><span><i className="legend-warn" /> 待确认 0</span></div></div><aside className="drawing-inspector panel-card"><div className="inspector-title"><b>图纸属性</b><button onClick={() => showToast('图纸属性已保存')}>⋯</button></div><div className="drawing-status"><span className="status-dot" /> 尺寸验证通过</div><div className="drawing-field"><span>图纸名称</span><b>动力轴 · 工程图</b></div><div className="drawing-field"><span>来源模型</span><b>{model.name}</b></div><div className="drawing-field"><span>视图数量</span><b>3 个视图</b></div><div className="drawing-field"><span>标注尺寸</span><b>6 个</b></div><div className="layer-list"><div className="field-group-title">图层</div>{[['DIM', '尺寸标注', '#e8a85e'], ['CENTER', '中心线', '#80a9d4'], ['OBJECT', '可见轮廓', '#c8d6e5']].map(([id, name, color]) => <div className="layer-row" key={id}><span className="layer-color" style={{ background: color }} /><span>{id}</span><small>{name}</small><button>◉</button></div>)}</div><button className="primary-outline full" onClick={() => showToast('已运行 2D 尺寸检查')}>运行尺寸检查 <Icon>↗</Icon></button></aside></div></div> }

function AssemblyWorkspace({ model, assemblyChecked, setAssemblyChecked, showToast }) { return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">ASSEMBLY AGENT</span><h1>传动轴组件 · 装配</h1><p>2 个实例 · 1 个同轴配合 · 0 个干涉</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => showToast('装配向导已打开')}>✦ 装配 Agent</button><button className="primary-button" onClick={() => { setAssemblyChecked(true); showToast('装配检查完成') }}>运行检查 <Icon>↗</Icon></button></div></div><div className="assembly-grid"><div className="assembly-view panel-card"><div className="assembly-toolbar"><span><i className="live-dot" /> 实时装配预览</span><div><button onClick={() => showToast('已切换爆炸视图')}>爆炸视图</button><button onClick={() => showToast('已打开截面分析')}>截面</button></div></div><div className="assembly-scene"><div className="assembly-axis-line" /><div className="assembly-part bearing"><div className="bearing-ring" /><span>6204 轴承</span></div><div className="assembly-part shaft"><div className="shaft-body" /><span>{model.name}</span></div><div className="assembly-part washer"><div className="washer-ring" /><span>垫圈</span></div></div><div className="assembly-footer"><span>拖拽零件调整位置</span><span>同轴度 <b>0.02 mm</b></span></div></div><aside className="assembly-inspector panel-card"><div className="inspector-title"><b>装配树</b><span className="muted">2 实例</span></div><div className="assembly-tree"><div className="assembly-node root"><Icon>▣</Icon><span>传动轴组件</span></div><div className="assembly-node"><span className="tree-branch">└</span><Icon>◉</Icon><div><b>{model.name}</b><small>实例 01 · 固定</small></div><span className="node-badge">固定</span></div><div className="assembly-node"><span className="tree-branch">└</span><Icon>◉</Icon><div><b>深沟球轴承 6204</b><small>实例 02 · 可移动</small></div></div></div><div className="mates"><div className="field-group-title">配合关系 <b>1</b></div><div className="mate-card"><span className="mate-icon">◎</span><div><b>同轴配合</b><small>轴 · 圆柱面 ↔ 轴承 · 内圈</small></div><span className="check-status pass">通过</span></div></div><div className={`assembly-check ${assemblyChecked ? 'checked' : ''}`}><span>{assemblyChecked ? '✓' : 'i'}</span>{assemblyChecked ? '装配检查通过，未发现干涉。' : '运行检查以验证间隙与配合。'}</div></aside></div></div> }

function LibraryWorkspace({ libraryQuery, setLibraryQuery, libraryGroup, setLibraryGroup, filteredLibrary, insertLibrary, showToast }) { return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">STANDARD LIBRARY</span><h1>标准件库</h1><p>常用机械件已按国标整理，可直接插入当前项目</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => showToast('标准件同步完成 · 1,248 项')}>↻ 同步库</button><button className="primary-button" onClick={() => showToast('已打开自定义标准件向导')}>＋ 自定义零件</button></div></div><div className="library-layout"><div className="library-main panel-card"><div className="library-toolbar"><div className="search-box"><Icon>⌕</Icon><input value={libraryQuery} onChange={(e) => setLibraryQuery(e.target.value)} placeholder="搜索名称、规格或国标号" /></div><div className="library-filters">{['全部', '紧固件', '轴承', '定位件'].map((group) => <button className={libraryGroup === group ? 'selected' : ''} key={group} onClick={() => setLibraryGroup(group)}>{group}</button>)}</div></div><div className="library-grid">{filteredLibrary.map((item) => <div className="library-card" key={item.name}><div className="library-icon">{item.icon}</div><div className="library-card-copy"><b>{item.name}</b><span>{item.spec}</span><small>{item.group} · GB/T 推荐</small></div><button className="insert-button" onClick={() => insertLibrary(item)}>插入</button></div>)}{filteredLibrary.length === 0 && <div className="empty-state">没有找到匹配的标准件，试试搜索 “M8” 或 “轴承”。</div>}</div></div><aside className="library-side panel-card"><div className="inspector-title"><b>项目零件库</b><span className="muted">3 个</span></div><div className="project-part"><span className="part-preview shaft-mini" /><div><b>动力轴 · 版本 04</b><small>当前模型</small></div><span className="part-check">✓</span></div><div className="project-part"><span className="part-preview bearing-mini" /><div><b>深沟球轴承 6204</b><small>已插入 · 实例 02</small></div><span className="part-check">✓</span></div><div className="project-part"><span className="part-preview washer-mini" /><div><b>弹簧垫圈 M8</b><small>最近使用</small></div><span className="part-check muted">＋</span></div><div className="library-tip"><Icon>✦</Icon><span>从标准件库插入的零件会保留规格来源，方便后续替换和追溯。</span></div></aside></div></div> }

function HomeWorkspace({ projects, selectedProject, createProject, setActiveMode, showToast }) { return <div className="home-workspace"><div className="home-hero"><div><span className="eyebrow">WELCOME BACK, JOY</span><h1>把想法，变成可制造的形状。</h1><p>用自然语言驱动参数化设计，所有尺寸、特征和版本都可追溯。</p><div className="hero-actions"><button className="primary-button" onClick={() => setActiveMode('3D 建模')}>✦ 开始 AI 建模</button><button className="secondary-button" onClick={() => setActiveMode('2D 工程图')}>打开工程图</button></div></div><div className="hero-orbit"><div className="orbit orbit-1" /><div className="orbit orbit-2" /><div className="hero-cube">N</div></div></div><div className="home-section-heading"><div><h2>最近项目</h2><span>继续你的设计工作</span></div><button className="text-button" onClick={createProject}>＋ 新建项目</button></div><div className="project-cards">{projects.map((project) => <button key={project.id} className="project-card" onClick={() => { setActiveMode('3D 建模'); showToast(`正在打开 ${project.name}`) }}><div className={`project-preview ${project.color}`}><span>{project.name.slice(0, 1)}</span><small>{project.files} 个文件</small></div><div className="project-card-body"><b>{project.name}</b><span>更新于 {project.updated}</span></div><span className="card-arrow">↗</span></button>)}</div><div className="quick-grid"><button onClick={() => setActiveMode('3D 建模')}><span className="quick-icon blue">✦</span><div><b>AI 参数化零件</b><small>从一句话开始设计</small></div><span>→</span></button><button onClick={() => setActiveMode('2D 工程图')}><span className="quick-icon orange">▱</span><div><b>2D 工程图</b><small>三视图与尺寸标注</small></div><span>→</span></button><button onClick={() => setActiveMode('装配')}><span className="quick-icon violet">◈</span><div><b>装配工作台</b><small>配合与干涉检查</small></div><span>→</span></button></div></div> }

export default App
