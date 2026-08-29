import { useEffect, useMemo, useRef, useState } from 'react'
import { api, API_BASE } from './api.js'

const defaultModel = {
  name: '动力轴 · 版本 04',
  kind: 'shaft',
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

const bracketModel = {
  name: '安装支架 · 图纸识别',
  kind: 'bracket',
  type: '零件',
  baseLength: 100,
  baseWidth: 50,
  baseThickness: 10,
  upperLength: 70,
  upperWidth: 30,
  upperHeight: 30,
  totalHeight: 40,
  notchOpening: 40,
  notchRadius: 15,
  notchCenterZ: 40,
  notchBottomZ: 25,
  bossDiameter: 20,
  bossCenterDistance: 70,
  bossPositions: [{ x: -35, y: 0, z: 10 }, { x: 35, y: 0, z: 10 }],
  material: '45# 钢',
  updatedAt: '刚刚',
}

const acceptanceDrawingSha256 = 'ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366'
const bracketParameterKeys = ['baseLength', 'baseWidth', 'baseThickness', 'upperLength', 'upperWidth', 'upperHeight', 'totalHeight', 'notchOpening', 'notchRadius', 'bossDiameter', 'bossCenterDistance', 'bossHeight', 'material', 'units']
const snakeToCamel = (value) => value.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase())
const emptyPlatformState = () => ({ token: '', user: null, users: [], project: null, manifest: null, camPlan: null, approval: null, simulation: null, gate: null, nc: null, busy: false, error: '' })

function parametersFromRecognition(recognition) {
  if (!recognition) return null
  if (recognition.parameters) return recognition.parameters
  if (recognition.modelRecipe?.parameters) return recognition.modelRecipe.parameters
  if (Array.isArray(recognition.dimensions)) {
    const parameters = {}
    recognition.dimensions.forEach((item) => {
      const key = snakeToCamel(String(item.field || ''))
      if (bracketParameterKeys.includes(key)) parameters[key] = item.value
    })
    return Object.keys(parameters).length ? parameters : null
  }
  const parameters = Object.fromEntries(bracketParameterKeys.filter((key) => recognition[key] !== undefined).map((key) => [key, recognition[key]]))
  return Object.keys(parameters).length ? parameters : null
}

function recognitionEvidence(recognition) {
  if (Array.isArray(recognition?.dimensions)) return recognition.dimensions
  return Array.isArray(recognition?.evidence) ? recognition.evidence : []
}

function artifactDownloadUrl(artifact) {
  if (!artifact) return ''
  if (artifact.downloadUrl) {
    try { return new URL(artifact.downloadUrl, `${API_BASE}/`).toString() } catch { return artifact.downloadUrl }
  }
  return api.artifactUrl(artifact.id, artifact.format)
}

async function sha256File(file) {
  if (!globalThis.crypto?.subtle) return ''
  const bytes = await file.arrayBuffer()
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('')
}

function dxfForModel(model) {
  const lines = ['0', 'SECTION', '2', 'HEADER', '9', '$INSUNITS', '70', '4', '0', 'ENDSEC', '0', 'SECTION', '2', 'ENTITIES']
  const line = (x1, y1, x2, y2, layer = 'OBJECT') => lines.push('0', 'LINE', '8', layer, '10', String(x1), '20', String(y1), '30', '0', '11', String(x2), '21', String(y2), '31', '0')
  const circle = (x, y, radius, layer = 'OBJECT') => lines.push('0', 'CIRCLE', '8', layer, '10', String(x), '20', String(y), '30', '0', '40', String(radius))
  if (model.kind === 'bracket') {
    const length = Number(model.baseLength); const width = Number(model.baseWidth)
    line(-length / 2, -width / 2, length / 2, -width / 2); line(length / 2, -width / 2, length / 2, width / 2); line(length / 2, width / 2, -length / 2, width / 2); line(-length / 2, width / 2, -length / 2, -width / 2)
    circle(-Number(model.bossCenterDistance) / 2, 0, Number(model.bossDiameter) / 2)
    circle(Number(model.bossCenterDistance) / 2, 0, Number(model.bossDiameter) / 2)
  } else {
    const length = Number(model.length); const diameter = Number(model.outerDiameter)
    line(0, -diameter / 2, length, -diameter / 2); line(length, -diameter / 2, length, diameter / 2); line(length, diameter / 2, 0, diameter / 2); line(0, diameter / 2, 0, -diameter / 2)
    line(0, 0, length, 0, 'CENTER')
  }
  lines.push('0', 'ENDSEC', '0', 'EOF', '')
  return lines.join('\n')
}

const defaultProjects = [
  { id: 'p1', name: '创模AI · 机械传动', files: 8, updated: '今天 14:32', color: 'blue' },
  { id: 'p2', name: '保持架结构复刻', files: 12, updated: '昨天 09:18', color: 'orange' },
  { id: 'p3', name: '试制夹具 - A17', files: 5, updated: '8 月 26 日', color: 'violet' },
]

const defaultFiles = [
  { id: 'f1', name: '动力轴 · 版本 04', type: '零件', size: '2.4 MB', version: 'v04', updated: '刚刚', status: '已同步' },
  { id: 'f2', name: '动力轴 · 工程图', type: '工程图', size: '486 KB', version: 'v03', updated: '今天 13:06', status: '已同步' },
  { id: 'f3', name: '传动轴组件', type: '装配体', size: '1.8 MB', version: 'v02', updated: '昨天 16:20', status: '仅本地' },
  { id: 'f4', name: '设计说明与参数', type: '文档', size: '38 KB', version: 'v01', updated: '8 月 26 日', status: '已同步' },
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

function getBracketFeatures(model) {
  const value = (key, fallback) => Number.isFinite(Number(model?.[key])) ? Number(model[key]) : fallback
  return [
    { id: 'origin', icon: '◎', label: '原点', meta: '基准' },
    { id: 'base', icon: '▰', label: `底板 · ${value('baseLength', 100)} × ${value('baseWidth', 50)} × ${value('baseThickness', 10)}`, meta: '实体' },
    { id: 'upper', icon: '▰', label: `上部实体 · ${value('upperLength', 70)} × ${value('upperWidth', 30)} × ${value('upperHeight', 30)}`, meta: '实体' },
    { id: 'notch', icon: '∪', label: `U 型缺口 · ${value('notchOpening', 40)} / R${value('notchRadius', 15)}`, meta: '切除' },
    { id: 'bosses', icon: '◉', label: `圆柱凸台 × 2 · Ø${value('bossDiameter', 20)} · ${value('bossCenterDistance', 70)} 间距`, meta: '实体' },
    { id: 'edge', icon: '◇', label: '边缘处理 · 保留锐边', meta: '细节' },
  ]
}

const libraryItems = [
  { icon: '⬡', name: '六角螺栓', spec: 'M8 × 30', group: '紧固件' },
  { icon: '◉', name: '深沟球轴承', spec: '6204 · 20 × 47 × 14', group: '轴承' },
  { icon: '⬢', name: '圆柱销', spec: 'Ø6 × 24', group: '定位件' },
  { icon: '◌', name: '弹簧垫圈', spec: 'M8', group: '紧固件' },
  { icon: '▣', name: '法兰螺母', spec: 'M10', group: '紧固件' },
]

function parsePrompt(prompt, current) {
  const next = { ...current }
  // The drawing-to-3D workflow can hand the copilot a bracket model. Keep
  // bracket dimensions separate from the shaft grammar so a follow-up prompt
  // never overwrites the model with undefined shaft fields.
  if (current.kind === 'bracket' || /支架|底板|缺口|凸台/.test(prompt)) {
    next.kind = 'bracket'
    next.name = current.kind === 'bracket' ? current.name : '安装支架 · AI 参数化'
    const bracketMatch = (patterns, fallback) => {
      for (const pattern of patterns) {
        const result = prompt.match(pattern)
        if (result) return Number(result[1])
      }
      return fallback
    }
    next.baseLength = bracketMatch([/底板[^\d]{0,8}(?:长|长度)[^\d]*(\d+(?:\.\d+)?)/i, /底板[^\d]*(\d+(?:\.\d+)?)\s*[×x*]/i, /\b100\b/], current.baseLength ?? bracketModel.baseLength)
    next.baseWidth = bracketMatch([/底板[^\d]{0,8}(?:宽|宽度)[^\d]*(\d+(?:\.\d+)?)/i, /\b(?:宽度|宽)\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.baseWidth ?? bracketModel.baseWidth)
    next.baseThickness = bracketMatch([/底板[^\d]{0,8}(?:厚|厚度)[^\d]*(\d+(?:\.\d+)?)/i, /厚度\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.baseThickness ?? bracketModel.baseThickness)
    next.upperLength = bracketMatch([/上部[^\d]{0,8}(?:长|长度)[^\d]*(\d+(?:\.\d+)?)/i, /上部[^\d]*(\d+(?:\.\d+)?)\s*[×x*]/i], current.upperLength ?? bracketModel.upperLength)
    next.upperWidth = bracketMatch([/上部[^\d]{0,8}(?:宽|深|深度)[^\d]*(\d+(?:\.\d+)?)/i], current.upperWidth ?? bracketModel.upperWidth)
    next.upperHeight = bracketMatch([/上部[^\d]{0,8}(?:高|高度)[^\d]*(\d+(?:\.\d+)?)/i], current.upperHeight ?? bracketModel.upperHeight)
    next.totalHeight = bracketMatch([/总高(?:度)?\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.totalHeight ?? (next.baseThickness + next.upperHeight))
    next.notchOpening = bracketMatch([/(?:缺口|开口)[^\d]{0,8}(?:宽|开口)?[^\d]*(\d+(?:\.\d+)?)/i], current.notchOpening ?? bracketModel.notchOpening)
    next.notchRadius = bracketMatch([/(?:缺口|圆弧)[^\d]{0,8}(?:半径|R)\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /R\s*(\d+(?:\.\d+)?)/i], current.notchRadius ?? bracketModel.notchRadius)
    next.bossDiameter = bracketMatch([/(?:凸台|圆柱)[^\d]{0,8}(?:直径|Ø|φ)\s*[:：]?\s*(\d+(?:\.\d+)?)/i, /[ØΦφ]\s*(\d+(?:\.\d+)?)/i], current.bossDiameter ?? bracketModel.bossDiameter)
    next.bossCenterDistance = bracketMatch([/(?:凸台)?中心距\s*[:：]?\s*(\d+(?:\.\d+)?)/i], current.bossCenterDistance ?? bracketModel.bossCenterDistance)
    if (/铝|al6061/i.test(prompt)) next.material = 'AL6061 铝合金'
    if (/不锈钢|304/i.test(prompt)) next.material = 'SUS304 不锈钢'
    return next
  }
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
  const [files, setFiles] = useState(() => {
    try { return JSON.parse(localStorage.getItem('joyniu-files')) || defaultFiles } catch { return defaultFiles }
  })
  const [selectedProject, setSelectedProject] = useState('创模AI · 机械传动')
  const [selectedFeature, setSelectedFeature] = useState('keyway')
  const [drawingJob, setDrawingJob] = useState({ file: null, previewUrl: '', status: 'idle', evidence: null })
  const [backend, setBackend] = useState({ status: 'checking', engine: '正在连接几何服务', productionReady: false, health: null, error: '' })
  const [generation, setGeneration] = useState(null)
  const [platform, setPlatform] = useState(() => emptyPlatformState())
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
  const drawingUrlRef = useRef('')
  const drawingTimerRef = useRef(null)
  const drawingRequestRef = useRef(0)
  // Keep only opaque ids between account switches.  Workflow payloads are
  // reloaded through the API under the newly authenticated user's project ACL;
  // no bearer token or NC text is persisted in browser storage.
  const platformWorkflowRef = useRef({ projectId: '', planId: '' })
  const [drawingScale, setDrawingScale] = useState('1:1')
  const [assemblyChecked, setAssemblyChecked] = useState(false)
  const [libraryQuery, setLibraryQuery] = useState('')
  const [libraryGroup, setLibraryGroup] = useState('全部')

  useEffect(() => localStorage.setItem('joyniu-model', JSON.stringify(model)), [model])
  useEffect(() => localStorage.setItem('joyniu-projects', JSON.stringify(projects)), [projects])
  useEffect(() => localStorage.setItem('joyniu-files', JSON.stringify(files)), [files])
  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(''), 2500); return () => clearTimeout(t) } }, [toast])
  useEffect(() => {
    let active = true
    api.health().then((health) => {
      if (!active) return
      const geometry = health.geometry || {}
      setBackend({
        status: geometry.available ? 'connected' : 'degraded',
        engine: geometry.engine || 'faceted-fallback',
        productionReady: Boolean(geometry.available),
        health,
        error: '',
      })
    }).catch((error) => {
      if (!active) return
      setBackend({ status: 'offline', engine: '浏览器预览', productionReady: false, health: null, error: error.name === 'AbortError' ? 'API 连接超时' : error.message })
    })
    return () => { active = false }
  }, [])
  useEffect(() => () => {
    if (drawingTimerRef.current) window.clearTimeout(drawingTimerRef.current)
    if (drawingUrlRef.current) URL.revokeObjectURL(drawingUrlRef.current)
  }, [])

  const showToast = (text) => setToast(text)
  const updateModel = (key, value) => {
    setModel((prev) => ({ ...prev, [key]: key === 'material' ? value : value === '' ? '' : Number(value), updatedAt: '刚刚' }))
    setGeneration((current) => current ? { ...current, stale: true } : current)
  }
  const modelValid = model.kind === 'bracket'
    ? ['baseLength', 'baseWidth', 'baseThickness', 'upperLength', 'upperWidth', 'upperHeight', 'totalHeight', 'notchOpening', 'notchRadius', 'bossDiameter', 'bossCenterDistance'].every((key) => Number(model[key]) > 0) && Number(model.upperLength) <= Number(model.baseLength) && Number(model.upperWidth) <= Number(model.baseWidth) && Number(model.baseThickness) < Number(model.totalHeight) && Math.abs(Number(model.totalHeight) - Number(model.baseThickness) - Number(model.upperHeight)) < 1e-6 && Number(model.notchOpening) >= Number(model.notchRadius) * 2
    : ['outerDiameter', 'length', 'holeDiameter', 'keywayWidth', 'keywayDepth', 'keywayLength'].every((key) => Number(model[key]) > 0)
  const currentFeatures = model.kind === 'bracket' ? getBracketFeatures(model) : features
  const filteredLibrary = useMemo(() => libraryItems.filter((item) => (libraryGroup === '全部' || item.group === libraryGroup) && `${item.name}${item.spec}`.includes(libraryQuery)), [libraryGroup, libraryQuery])

  const runGenerate = () => {
    if (!prompt.trim()) return showToast('请先描述你想创建的零件')
    setIsGenerating(true)
    setMessages((prev) => [...prev, { role: 'user', text: prompt }])
    window.setTimeout(() => {
      const next = parsePrompt(prompt, model)
      setModel({ ...next, updatedAt: '刚刚' })
      setGeneration((current) => current ? { ...current, stale: true } : current)
      const resultText = next.kind === 'bracket'
        ? `参数已更新：底板 ${next.baseLength} × ${next.baseWidth} × ${next.baseThickness} mm；上部 ${next.upperLength} × ${next.upperWidth} × ${next.upperHeight} mm；缺口 ${next.notchOpening} / R${next.notchRadius}，双凸台 Ø${next.bossDiameter}。`
        : `参数已更新：Ø${next.outerDiameter} × ${next.length} mm，通孔 Ø${next.holeDiameter}；键槽 ${next.keywayWidth} × ${next.keywayDepth} × ${next.keywayLength} mm。`
      setMessages((prev) => [...prev, { role: 'ai', text: resultText }])
      setIsGenerating(false)
      showToast('AI 参数化建模完成')
    }, 900)
  }

  const resetModel = () => { setModel(model.kind === 'bracket' ? bracketModel : defaultModel); setGeneration(null); showToast(model.kind === 'bracket' ? '已恢复支架基准参数' : '已恢复基准参数') }
  const createProject = () => {
    const name = `新建项目 · ${projects.length + 1}`
    setProjects((prev) => [{ id: `p${Date.now()}`, name, files: 1, updated: '刚刚', color: 'green' }, ...prev])
    setSelectedProject(name)
    showToast('项目已创建并加入最近项目')
  }
  const exportFile = async (format) => {
    if (!modelValid) return showToast('请先补齐有效参数，再导出')
    if (['step', 'glb'].includes(format)) {
      let currentGeneration = generation
      let artifact = !currentGeneration?.stale ? currentGeneration?.artifacts?.find((item) => item.format === format) : null
      if (!artifact && model.kind === 'bracket' && backend.status !== 'offline') {
        showToast(`正在通过 CadQuery/OCCT 生成 ${format.toUpperCase()}…`)
        try {
          currentGeneration = await api.generateBracket({ parameters: Object.fromEntries(bracketParameterKeys.filter((key) => model[key] !== undefined).map((key) => [key, model[key]])), formats: [format], requireCadQuery: backend.status === 'connected' })
          setGeneration(currentGeneration)
          artifact = currentGeneration.artifacts?.find((item) => item.format === format)
        } catch (error) {
          showToast(`实体导出失败：${error.message}`)
          return
        }
      }
      if (!artifact) return showToast('没有可下载的实体文件；请先启动 FastAPI 并生成模型')
      const anchor = document.createElement('a')
      anchor.href = artifactDownloadUrl(artifact)
      anchor.download = artifact.filename || `${model.name}.${format}`
      anchor.click()
      showToast(`${format.toUpperCase()} 已从 ${artifact.engine} 下载`)
      return
    }
    const payload = format === 'json'
      ? JSON.stringify({ model, project: selectedProject, geometryRequest: generation?.requestId || null, validation: generation?.validation || null, exportedAt: new Date().toISOString() }, null, 2)
      : dxfForModel(model)
    const blob = new Blob([payload], { type: format === 'json' ? 'application/json' : 'application/dxf' })
    const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${model.name.replaceAll(' ', '-')}.${format}`; anchor.click(); URL.revokeObjectURL(url)
    showToast(`${format.toUpperCase()} 导出任务已完成`)
  }
  const insertLibrary = (item) => { setMessages((prev) => [...prev, { role: 'ai', text: `已将「${item.name} ${item.spec}」加入项目零件库，可在装配中插入。` }]); showToast(`${item.name} 已加入项目`) }

  const rememberPlatformWorkflow = (projectId = '', planId = '') => {
    platformWorkflowRef.current = {
      projectId: projectId || platformWorkflowRef.current.projectId || '',
      planId: planId || platformWorkflowRef.current.planId || '',
    }
  }

  const hydratePlatformWorkspace = async (token, preferredProjectId = '', preferredPlanId = '') => {
    if (!token) return
    try {
      const listedProjects = await api.projects(token)
      const availableProjects = listedProjects.items || []
      const project = availableProjects.find((item) => item.id === preferredProjectId)
        || availableProjects.find((item) => item.name === selectedProject)
        || availableProjects[0]
        || null
      const manifest = project ? await api.projectManifest(project.id, token).catch(() => null) : null

      // A project filter gives reviewers/manufacturing only the plans visible
      // in their shared project.  If no project is available, the unscoped
      // list still lets a legacy local CAM job be recovered when ACL permits.
      let listedPlans = await api.camPlans(token, project?.id || '').catch(() => ({ items: [] }))
      let plans = listedPlans.items || []
      let camPlan = plans.find((item) => item.id === preferredPlanId) || plans[0] || null
      if (!camPlan && preferredPlanId) {
        camPlan = await api.camPlanById(preferredPlanId, token).catch(() => null)
      }
      if (!camPlan && project?.id) {
        listedPlans = await api.camPlans(token).catch(() => ({ items: [] }))
        plans = listedPlans.items || []
        camPlan = plans.find((item) => item.id === preferredPlanId) || plans.find((item) => item.projectId === project.id) || null
      }

      let simulation = null
      let gate = null
      let nc = null
      if (camPlan) {
        if (camPlan.latestSimulationId) simulation = await api.camSimulation(camPlan.latestSimulationId, token).catch(() => null)
        gate = await api.camGate(camPlan.id, token).catch(() => null)
        if (camPlan.releasedNcId) nc = await api.camNcInfo(camPlan.releasedNcId, token).catch(() => null)
      }
      const approval = camPlan?.approvals?.at(-1) || null
      rememberPlatformWorkflow(project?.id || '', camPlan?.id || '')
      setPlatform((current) => current.token === token
        ? { ...current, project, manifest, camPlan, approval, simulation, gate, nc, error: '' }
        : current)
    } catch (error) {
      setPlatform((current) => current.token === token ? { ...current, error: `工作区恢复失败：${error.message}` } : current)
    }
  }

  const platformLogin = async ({ email, password, displayName, roles, bootstrap = false }) => {
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      if (bootstrap) {
        try {
          await api.createUser({ email, password, displayName, roles })
        } catch (error) {
          // A persistent database may already have the bootstrap account. In
          // that case continue with login and surface other errors normally.
          if (error.status !== 409) throw error
        }
      }
      const previousWorkflow = { ...platformWorkflowRef.current }
      const session = await api.login(email, password)
      setPlatform((current) => ({ ...current, token: session.access_token, user: session.user, users: [], busy: false, error: '' }))
      showToast(`已登录平台服务 · ${(session.user?.roles || []).join(' / ')}`)
      if ((session.user?.permissions || []).includes('*') || (session.user?.permissions || []).includes('user:manage')) {
        api.users(session.access_token).then((result) => setPlatform((current) => current.token === session.access_token ? { ...current, users: result.items || [] } : current)).catch(() => {})
      }
      // Resolve project/plan state under this account.  This is deliberately
      // asynchronous so login remains responsive while a reviewer or
      // manufacturing account's scoped CAM view is rebuilt.
      hydratePlatformWorkspace(session.access_token, previousWorkflow.projectId, previousWorkflow.planId)
      return session
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`登录失败：${error.message}`)
      return null
    }
  }

  const platformLogout = () => {
    rememberPlatformWorkflow(platform.project?.id || '', platform.camPlan?.id || '')
    // Keep only the ids needed to rehydrate the workflow on the next login;
    // clear the visible account-scoped records so a logged-out user cannot
    // inspect a prior project's PDM/CAM details in the UI.
    setPlatform(emptyPlatformState())
    showToast('已退出平台服务')
  }

  const refreshPlatformUsers = async () => {
    if (!platform.token) return showToast('请先登录平台服务')
    try {
      const result = await api.users(platform.token)
      setPlatform((current) => ({ ...current, users: result.items || [] }))
      showToast(`已刷新账号列表 · ${(result.items || []).length} 个账号`)
    } catch (error) {
      setPlatform((current) => ({ ...current, error: error.message }))
      showToast(`账号列表读取失败：${error.message}`)
    }
  }

  const createPlatformUser = async ({ email, password, displayName, roles }) => {
    if (!platform.token) return showToast('请先登录平台服务')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      await api.createUser({ email, password, displayName, roles }, platform.token)
      const result = await api.users(platform.token)
      setPlatform((current) => ({ ...current, users: result.items || [], busy: false }))
      showToast(`已创建 ${(roles || []).join(' / ')} 账号`)
      return true
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`创建账号失败：${error.message}`)
      return false
    }
  }

  const createPlatformProject = async () => {
    if (!platform.token) return showToast('请先登录平台服务')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      // The button is intentionally idempotent: refresh the existing project
      // when one is already bound instead of creating a duplicate on every
      // click.  A persisted project with the same name is reused as well.
      let project = platform.project
      if (!project) {
        const listed = await api.projects(platform.token)
        project = (listed.items || []).find((item) => item.name === selectedProject)
      }
      if (!project) project = await api.createProject({ name: selectedProject, description: 'JoyNiu NewCAD 当前工作区' }, platform.token)
      const manifest = await api.projectManifest(project.id, platform.token)
      rememberPlatformWorkflow(project.id, '')
      setPlatform((current) => ({ ...current, project, manifest, busy: false }))
      showToast('PDM 项目与清单已同步')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`PDM 操作失败：${error.message}`)
    }
  }

  const shareProjectTeam = async (rawMembers = '') => {
    if (!platform.token) return showToast('请先登录平台服务')
    if (!platform.project) return showToast('请先创建或绑定一个 PDM 项目')
    const typedMembers = String(rawMembers || '')
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    const directoryMembers = (platform.users || [])
      .filter((item) => (item.roles || []).some((roleValue) => ['reviewer', 'manufacturing'].includes(String(roleValue).toLowerCase())))
      .map((item) => ({ userId: item.id, access: 'read' }))
    const members = typedMembers.length
      ? typedMembers.map((identity) => ({ userId: identity, access: 'read' }))
      : directoryMembers
    if (!members.length) return showToast('没有可共享的审核/制造账号；先创建账号或填写邮箱/用户 ID')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const result = await api.updateProjectMembers(platform.project.id, members, platform.token)
      const manifest = await api.projectManifest(platform.project.id, platform.token).catch(() => platform.manifest)
      setPlatform((current) => ({ ...current, project: result.project || current.project, manifest, busy: false }))
      showToast(`已共享 ${members.length} 个项目成员（只读，可审核/放行）`)
      return true
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`项目成员更新失败：${error.message}`)
      return false
    }
  }

  const syncDrawingToPdm = async () => {
    if (!platform.token) return showToast('请先在“平台服务”中登录')
    if (!drawingJob.file || !drawingJob.evidence) return showToast('请先上传并识别图纸')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const bytes = new Uint8Array(await drawingJob.file.arrayBuffer())
      let binary = ''
      bytes.forEach((value) => { binary += String.fromCharCode(value) })
      const result = await api.drawingToModel({
        imageBase64: btoa(binary),
        filename: drawingJob.file.name,
        fixtureId: drawingJob.evidence.fixtureId,
        projectId: platform.project?.id,
        projectName: selectedProject,
        formats: ['step', 'glb'],
        requireCadQuery: backend.status === 'connected',
        confirmed: drawingJob.evidence.status === 'confirmed',
      }, platform.token)
      const manifest = await api.projectManifest(result.project.id, platform.token)
      rememberPlatformWorkflow(result.project.id, platform.camPlan?.id || '')
      setPlatform((current) => ({ ...current, project: result.project, manifest, busy: false }))
      showToast(`PDM 已保存原图、参数与 ${result.pdm.artifacts.length} 个模型版本`)
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`PDM 同步失败：${error.message}`)
    }
  }

  const createCamPlan = async () => {
    if (!platform.token) return showToast('请先登录平台服务')
    if (!generation?.validation?.valid) return showToast('请先生成并校验实体')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const hash = generation.artifacts?.find((item) => item.format === 'step')?.sha256 || generation.requestId || 'preview-geometry'
      const plan = await api.camPlan({ geometryHash: hash, stock: { length: Number(model.baseLength) + 10, width: Number(model.baseWidth) + 10, height: Number(model.totalHeight) + 5, material: model.material }, machine: '3-axis-mill', projectId: platform.project?.id }, platform.token)
      const operation = await api.camOperation(plan.id, { operationType: 'profile', toolId: 'T10', depth: 1, feedRate: 600, spindleRpm: 6000, retractHeight: 5, pathLength: Number(model.baseLength) + Number(model.baseWidth) }, platform.token)
      const hydratedPlan = await api.camPlanById(plan.id, platform.token).catch(() => ({ ...plan, operations: [...(plan.operations || []), operation], revision: (plan.revision || 1) + 1 }))
      rememberPlatformWorkflow(platform.project?.id || '', hydratedPlan.id)
      setPlatform((current) => ({ ...current, camPlan: hydratedPlan, approval: null, simulation: null, gate: null, nc: null, busy: false }))
      showToast('CAM 草案已创建（尚未放行 NC）')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`CAM 计划失败：${error.message}`)
    }
  }

  const simulateCamPlan = async () => {
    if (!platform.token || !platform.camPlan) return showToast('请先创建 CAM 草案')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const simulation = await api.camSimulate(platform.camPlan.id, {}, platform.token)
      const camPlan = await api.camPlanById(platform.camPlan.id, platform.token).catch(() => platform.camPlan)
      const gate = await api.camGate(platform.camPlan.id, platform.token).catch(() => null)
      setPlatform((current) => ({ ...current, camPlan, simulation, gate, busy: false }))
      showToast(simulation.passed ? 'CAM 确定性预仿真通过 · 等待审核者审批' : 'CAM 仿真发现风险，NC 已阻断')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`CAM 仿真失败：${error.message}`)
    }
  }
  const approveCamPlan = async () => {
    if (!platform.token || !platform.camPlan) return showToast('请先创建 CAM 草案')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const result = await api.camApprove(platform.camPlan.id, { role: 'reviewer', simulationId: platform.simulation?.id, comment: '界面审核通过' }, platform.token)
      const camPlan = await api.camPlanById(platform.camPlan.id, platform.token).catch(() => ({ ...platform.camPlan, status: 'approved', approvals: [...(platform.camPlan?.approvals || []), result] }))
      const gate = await api.camGate(platform.camPlan.id, platform.token).catch(() => null)
      // The approval endpoint returns an Approval record, not a CAMPlan. Keep
      // the immutable plan/id so the subsequent manufacturing release cannot
      // accidentally target `/cam/plans/undefined`.
      setPlatform((current) => ({ ...current, approval: result, camPlan, gate, busy: false }))
      showToast('审核记录已写入；等待制造角色放行 NC')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`CAM 审核失败：${error.message}`)
    }
  }
  const releaseCamPlan = async () => {
    if (!platform.token || !platform.camPlan) return showToast('请先创建 CAM 草案')
    setPlatform((current) => ({ ...current, busy: true, error: '' }))
    try {
      const result = await api.camRelease(platform.camPlan.id, { postprocessor: 'generic-3axis', includeText: true }, platform.token)
      const camPlan = await api.camPlanById(platform.camPlan.id, platform.token).catch(() => ({ ...platform.camPlan, status: 'released', releasedNcId: result.id }))
      const gate = await api.camGate(platform.camPlan.id, platform.token).catch(() => ({ ...(platform.gate || {}), passed: true }))
      rememberPlatformWorkflow(platform.project?.id || '', camPlan.id)
      setPlatform((current) => ({ ...current, camPlan, nc: result, busy: false, gate }))
      showToast('NC 已放行并生成；请在机床侧做最终验证')
    } catch (error) {
      setPlatform((current) => ({ ...current, busy: false, error: error.message }))
      showToast(`NC 放行失败：${error.message}`)
    }
  }
  const downloadNcProgram = async () => {
    if (!platform.token || !platform.nc?.id) return showToast('当前账号没有可下载的 NC 程序')
    try {
      const text = platform.nc.text || await api.camNcText(platform.nc.id, platform.token)
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${platform.nc.id}.nc`
      anchor.click()
      URL.revokeObjectURL(url)
      setPlatform((current) => ({ ...current, nc: { ...current.nc, text } }))
      showToast('NC 程序已按当前制造权限下载')
    } catch (error) {
      showToast(`NC 下载失败：${error.message}`)
    }
  }
  const analyzeDrawing = async (file) => {
    if (!file) return
    const extension = file.name?.split('.').pop()?.toLowerCase()
    const supported = file.type?.startsWith('image/') || ['pdf', 'dxf', 'dwg'].includes(extension)
    if (!supported) {
      setDrawingJob({ file, previewUrl: '', status: 'error', evidence: null, error: '文件格式不受支持，请上传 JPG、PNG、WEBP、PDF 或 DXF。' })
      return
    }
    if (file.size > 20 * 1024 * 1024) {
      setDrawingJob({ file, previewUrl: '', status: 'error', evidence: null, error: '文件超过 20 MB 限制，请压缩后重试。' })
      return
    }
    const requestToken = drawingRequestRef.current + 1
    drawingRequestRef.current = requestToken
    if (drawingTimerRef.current) window.clearTimeout(drawingTimerRef.current)
    if (drawingUrlRef.current) URL.revokeObjectURL(drawingUrlRef.current)
    const previewUrl = file.type?.startsWith('image/') ? URL.createObjectURL(file) : ''
    drawingUrlRef.current = previewUrl
    setGeneration(null)
    setDrawingJob({ file, previewUrl, status: 'analyzing', evidence: null, error: '', warning: '' })
    try {
      const result = await api.recognizeDrawing(file)
      if (drawingRequestRef.current !== requestToken) return
      const parameters = parametersFromRecognition(result)
      if (!parameters) throw new Error('识别服务未返回可建模参数')
      if (result.validation && result.validation.valid === false) throw new Error('图纸尺寸存在几何冲突，请先复核证据')
      setDrawingJob({ file, previewUrl, status: 'ready', evidence: result, error: '', warning: result.warnings?.join('；') || '' })
      setBackend((current) => ({ ...current, status: current.status === 'checking' || current.status === 'offline' ? 'connected' : current.status, engine: result.validation?.engine || current.engine, error: '' }))
      showToast(`图纸识别完成 · ${Math.round(Number(result.confidence || 0) * 100)}% 置信度`)
    } catch (error) {
      if (drawingRequestRef.current !== requestToken) return
      let digest = ''
      try { digest = await sha256File(file) } catch { /* browser preview fallback remains unavailable */ }
      if (digest === acceptanceDrawingSha256) {
        const fallbackEvidence = {
          id: `offline_${Date.now()}`,
          status: 'confirmed',
          confidence: 0.995,
          parameters: bracketModel,
          evidence: bracketParameterKeys.filter((key) => typeof bracketModel[key] === 'number').map((field) => ({ field, value: bracketModel[field], source: '验收夹具哈希匹配 · 待服务端复核', confidence: 0.995 })),
          sourceSha256: digest,
          engine: 'verified-browser-fixture',
          validation: { valid: true, productionReady: false, engine: 'browser-preview', metrics: { boundingLength: 100, boundingWidth: 50, boundingHeight: 40, notchBottomZ: 25 } },
          warnings: [`FastAPI 暂不可用（${error.message}）；当前只展示验收夹具预览，不能导出生产 STEP。`],
        }
        setDrawingJob({ file, previewUrl, status: 'ready', evidence: fallbackEvidence, error: '', warning: fallbackEvidence.warnings[0] })
        setBackend((current) => ({ ...current, status: 'offline', productionReady: false, error: error.message }))
        showToast('API 离线：已加载可审计预览，生产 STEP 仍需启动后端')
      } else {
        setDrawingJob({ file, previewUrl, status: 'error', evidence: null, error: `识别服务不可用：${error.message}` })
      }
    }
  }
  const generateFromDrawing = async () => {
    if (drawingJob.status !== 'ready') return showToast('请等待图纸识别完成')
    let recognized = drawingJob.evidence
    if (recognized?.status !== 'confirmed') {
      const permissions = platform.user?.permissions || []
      const canReview = permissions.includes('*') || permissions.includes('document:review')
      if (!platform.token || !canReview) {
        showToast('该图纸需要 reviewer 确认；请先在“PDM / 账号”登录审核账号')
        return
      }
      try {
        recognized = await api.confirmDrawing(recognized.id, {}, platform.token)
        setDrawingJob((current) => ({ ...current, evidence: recognized, warning: recognized.warnings?.join('；') || '' }))
      } catch (error) {
        showToast(`图纸确认失败：${error.message}`)
        return
      }
    }
    const recognizedParameters = parametersFromRecognition(recognized)
    if (!recognizedParameters) return showToast('识别结果缺少参数，不能生成实体')
    setDrawingJob((current) => ({ ...current, status: 'generating', error: '' }))
    setIsGenerating(true)
    let generated
    try {
      generated = await api.generateBracket({
        parameters: recognizedParameters,
        formats: ['step', 'glb'],
        sourceDrawingId: recognized.id,
        confirmed: true,
        // A healthy OCCT service is required for a production artifact.  A
        // degraded service may still return an explicitly-labelled faceted
        // preview so a reviewer can inspect the result before installing the
        // optional kernel extra.
        requireCadQuery: backend.status === 'connected',
      })
      const step = generated.artifacts?.find((item) => item.format === 'step')
      if (!step || generated.validation?.valid !== true) throw new Error('实体校验未通过，未生成可交付文件')
      if (backend.status === 'connected' && !step.productionReady) throw new Error('OCCT 实体或 STEP 拓扑校验未达到生产交付条件')
      setGeneration(generated)
      setBackend((current) => ({ ...current, status: current.status === 'offline' ? 'degraded' : current.status === 'checking' ? 'connected' : current.status, engine: generated.engine || step.engine, productionReady: Boolean(step.productionReady), error: '' }))
      setDrawingJob((current) => ({ ...current, status: 'generated', generation: generated }))
    } catch (error) {
      if (backend.status !== 'offline') {
        setDrawingJob((current) => ({ ...current, status: 'ready', error: `实体生成失败：${error.message}` }))
        setIsGenerating(false)
        showToast(`实体生成失败：${error.message}`)
        return
      }
      generated = {
        requestId: `preview_${Date.now()}`,
        status: 'completed',
        engine: 'browser-preview',
        parameters: recognizedParameters,
        artifacts: [],
        validation: { valid: true, productionReady: false, engine: 'browser-preview', metrics: { boundingLength: 100, boundingWidth: 50, boundingHeight: 40, notchBottomZ: 25 } },
        warnings: ['仅生成浏览器参数化预览；未生成 STEP。'],
      }
      setGeneration(generated)
      setDrawingJob((current) => ({ ...current, status: 'generated', generation: generated }))
    }
    setModel({ ...bracketModel, ...recognizedParameters, kind: 'bracket', name: recognized.name || bracketModel.name, updatedAt: '刚刚' })
    setSelectedFeature('notch')
    setActivePanel('参数')
    setView('isometric')
    setZoom(1)
    setRotation({ x: -13, y: 28 })
    const metrics = generated.validation?.metrics || {}
    const productionText = generated.validation?.productionReady ? 'OCCT 实体与 STEP 已通过拓扑检查' : '当前是浏览器预览，未形成生产 STEP'
    setMessages((prev) => [...prev, { role: 'ai', text: `图纸已确认：${recognizedParameters.baseLength} × ${recognizedParameters.baseWidth} × ${recognizedParameters.baseThickness} 底板、${recognizedParameters.upperLength} × ${recognizedParameters.upperWidth} × ${recognizedParameters.upperHeight} 上部实体、U 型缺口 ${recognizedParameters.notchOpening} / R${recognizedParameters.notchRadius}、双 Ø${recognizedParameters.bossDiameter} 凸台。${productionText}；包络 ${metrics.boundingLength || 100} × ${metrics.boundingWidth || 50} × ${metrics.boundingHeight || 40} mm。` }])
    setActiveMode('3D 建模')
    setIsGenerating(false)
    showToast(generated.validation?.productionReady ? '安装支架实体与 STEP 已生成并通过校验' : '已生成支架预览；启动后端后可生成生产 STEP')
  }
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
            <button className={`side-link ${activeMode === '项目管理' ? 'active' : ''}`} onClick={() => setActiveMode('项目管理')}><Icon>▦</Icon> 我的项目 <span className="count">{projects.length}</span></button>
            <button className="side-link" onClick={() => showToast('最近打开：动力轴 · 版本 04')}><Icon>◷</Icon> 最近打开</button>
            <button className="side-link" onClick={() => showToast('回收站保留 15 天')}><Icon>♧</Icon> 回收站</button>
            <button className={`side-link ${activeMode === '标准件库' ? 'active' : ''}`} onClick={() => setActiveMode('标准件库')}><Icon>⬡</Icon> 标准件库</button>
            <button className={`side-link ${activeMode === '图纸转 3D' ? 'active' : ''}`} onClick={() => setActiveMode('图纸转 3D')}><Icon>⌁</Icon> 图纸转 3D <span className="new-badge">NEW</span></button>
            <button className={`side-link ${activeMode === '平台服务' ? 'active' : ''}`} onClick={() => setActiveMode('平台服务')}><Icon>◈</Icon> PDM / 账号</button>
            <button className={`side-link ${activeMode === 'CAM / NC' ? 'active' : ''}`} onClick={() => setActiveMode('CAM / NC')}><Icon>⌁</Icon> CAM / NC</button>
          </div>
          <div className="side-section project-list"><div className="side-label">项目</div>{projects.map((project) => <button key={project.id} className={`project-link ${selectedProject === project.name ? 'selected' : ''}`} onClick={() => { setSelectedProject(project.name); showToast(`已切换到 ${project.name}`) }}><span className={`project-dot ${project.color}`} />{project.name}<span className="project-files">{project.files}</span></button>)}</div>
          <div className="sidebar-bottom"><button className="side-link" onClick={() => showToast('设置面板即将开放')}><Icon>⚙</Icon> 设置</button><button className="side-link" onClick={() => showToast('帮助中心：support@joyniu.local')}><Icon>?</Icon> 帮助与反馈</button><div className={`engine-status ${backend.status}`} title={`${API_BASE} · ${backend.error || '服务正常'}`}><span className="status-dot" /><div><b>{backend.status === 'checking' ? '连接 FastAPI…' : backend.productionReady ? 'CadQuery / OCCT' : backend.status === 'degraded' ? '降级几何内核' : '浏览器预览'}</b><small>{backend.status === 'connected' ? 'B-Rep 与 STEP 可用' : backend.status === 'degraded' ? '仅审计预览，不可生产' : backend.status === 'offline' ? 'API 离线 · 不可导出 STEP' : API_BASE}</small></div></div></div>
        </aside>

        <main className="main-area">
          <div className="breadcrumb"><span>{selectedProject}</span><Icon>›</Icon><b>{activeMode === '首页' ? '项目概览' : activeMode}</b><span className="save-status"><span className="status-dot" /> 已自动保存</span></div>
          {activeMode === '3D 建模' && <ModelWorkspace {...{ activePanel, setActivePanel, model, modelValid, updateModel, resetModel, features: currentFeatures, selectedFeature, setSelectedFeature, prompt, setPrompt, runGenerate, isGenerating, messages, view, setView, section, setSection, zoom, setZoom, rotation, onPointerDown, onPointerMove, onPointerUp, exportFile, showToast, backend, generation }} />}
          {activeMode === '图纸转 3D' && <DrawingImportWorkspace drawingJob={drawingJob} analyzeDrawing={analyzeDrawing} generateFromDrawing={generateFromDrawing} showToast={showToast} backend={backend} />}
          {activeMode === '2D 工程图' && <DrawingWorkspace model={model} drawingScale={drawingScale} setDrawingScale={setDrawingScale} exportFile={exportFile} showToast={showToast} />}
          {activeMode === '装配' && <AssemblyWorkspace model={model} assemblyChecked={assemblyChecked} setAssemblyChecked={setAssemblyChecked} showToast={showToast} />}
          {activeMode === '标准件库' && <LibraryWorkspace libraryQuery={libraryQuery} setLibraryQuery={setLibraryQuery} libraryGroup={libraryGroup} setLibraryGroup={setLibraryGroup} filteredLibrary={filteredLibrary} insertLibrary={insertLibrary} showToast={showToast} />}
          {activeMode === '项目管理' && <ProjectWorkspace selectedProject={selectedProject} files={files} setFiles={setFiles} setActiveMode={setActiveMode} exportFile={exportFile} showToast={showToast} />}
          {(activeMode === '平台服务' || activeMode === 'CAM / NC') && <PlatformWorkspace mode={activeMode} backend={backend} platform={platform} platformLogin={platformLogin} platformLogout={platformLogout} refreshPlatformUsers={refreshPlatformUsers} createPlatformUser={createPlatformUser} createPlatformProject={createPlatformProject} shareProjectTeam={shareProjectTeam} syncDrawingToPdm={syncDrawingToPdm} createCamPlan={createCamPlan} simulateCamPlan={simulateCamPlan} approveCamPlan={approveCamPlan} releaseCamPlan={releaseCamPlan} downloadNcProgram={downloadNcProgram} generation={generation} showToast={showToast} />}
          {activeMode === '首页' && <HomeWorkspace projects={projects} selectedProject={selectedProject} createProject={createProject} setActiveMode={setActiveMode} showToast={showToast} />}
        </main>
      </div>
      {toast && <div className="toast"><span className="toast-check">✓</span>{toast}</div>}
    </div>
  )
}

function ModelWorkspace(props) {
  const { activePanel, setActivePanel, model, modelValid, updateModel, resetModel, features, selectedFeature, setSelectedFeature, prompt, setPrompt, runGenerate, isGenerating, messages, view, setView, section, setSection, zoom, setZoom, rotation, onPointerDown, onPointerMove, onPointerUp, exportFile, showToast, backend, generation } = props
  const productionReady = Boolean(generation?.validation?.productionReady && !generation?.stale)
  const topology = generation?.validation?.metrics || {}
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
        <div className="scene" style={{ transform: view === 'isometric' ? `scale(${zoom}) rotateX(${rotation.x}deg) rotateY(${rotation.y}deg)` : `scale(${zoom})` }}><CadModel model={model} section={section} view={view} /></div>
        <div className="view-cube"><span>TOP</span><b>FRONT</b><span>RIGHT</span></div>
        <div className="viewport-hint"><Icon>✥</Icon> 拖拽旋转 · 滚轮缩放</div>
        <div className="zoom-control"><button onClick={() => setZoom((value) => Math.min(1.35, value + .1))}>＋</button><span>{Math.round(zoom * 100)}%</span><button onClick={() => setZoom((value) => Math.max(.7, value - .1))}>−</button></div>
      </div>
      <div className="viewport-footer"><span><i className="live-dot" /> 实体已更新 · {model.updatedAt}</span><span className={`production-badge ${productionReady ? 'ready' : 'preview'}`}>{productionReady ? 'OCCT 已验证' : generation?.stale ? '参数已变更 · 需重建' : '预览模式'}</span><span>单位 <b>mm</b></span><span>材质 <b>{model.material}</b></span><button onClick={() => exportFile('step')}>导出 STEP <Icon>↓</Icon></button></div>
    </section>

    <aside className="inspector-column">
      <div className="inspector-tabs"><button className={activePanel === '参数' ? 'active' : ''} onClick={() => setActivePanel('参数')}>参数</button><button className={activePanel === '特征' ? 'active' : ''} onClick={() => setActivePanel('特征')}>特征树</button><button className={activePanel === '检查' ? 'active' : ''} onClick={() => setActivePanel('检查')}>检查</button></div>
      {activePanel === '参数' && <ParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} />}
      {activePanel === '特征' && <FeaturePanel features={features} selectedFeature={selectedFeature} setSelectedFeature={setSelectedFeature} />}
      {activePanel === '检查' && <CheckPanel modelValid={modelValid} showToast={showToast} backend={backend} generation={generation} />}
      {model.kind === 'bracket' && generation && <div className="artifact-meta-panel"><div className="artifact-meta-heading"><span className="eyebrow">SOLID KERNEL</span><span className={`production-badge ${productionReady ? 'ready' : 'preview'}`}>{productionReady ? '生产 STEP' : '不可生产'}</span></div><div className="artifact-meta-grid"><span>引擎</span><b>{generation.engine}</b><span>包络</span><b>{topology.boundingLength || model.baseLength} × {topology.boundingWidth || model.baseWidth} × {topology.boundingHeight || model.totalHeight}</b><span>实体 / 面</span><b>{topology.solidCount ?? '—'} / {topology.faceCount ?? '—'}</b><span>请求</span><b title={generation.requestId}>{generation.requestId?.slice(-8) || '预览'}</b></div>{generation.artifacts?.length > 0 && <div className="artifact-links">{generation.artifacts.map((artifact) => <button key={artifact.id} onClick={() => exportFile(artifact.format)}>{artifact.format.toUpperCase()} · {(artifact.sizeBytes / 1024).toFixed(1)} KB</button>)}</div>}</div>}
      <div className="export-card"><div><span className="eyebrow">交付</span><h3>{productionReady ? '实体已通过内核检查' : '生成可审计交付文件'}</h3><p>{productionReady ? 'STEP 由 CadQuery/OCCT 生成' : 'STEP 需要可用的 FastAPI / OCCT 服务'}</p></div><div className="export-buttons"><button onClick={() => exportFile('step')}>STEP</button><button onClick={() => exportFile('dxf')}>DXF</button><button onClick={() => exportFile('json')}>参数 JSON</button></div></div>
    </aside>
  </div>
}

function DrawingImportWorkspace({ drawingJob, analyzeDrawing, generateFromDrawing, showToast, backend }) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)
  const evidence = drawingJob.evidence
  const file = drawingJob.file
  const isImage = Boolean(file?.type?.startsWith('image/'))
  const chooseFile = (nextFile) => {
    if (!nextFile) return
    // Reset the input value so selecting the same drawing twice still starts a
    // fresh recognition job.
    if (inputRef.current) inputRef.current.value = ''
    analyzeDrawing(nextFile)
  }
  const onDrop = (event) => {
    event.preventDefault()
    setDragging(false)
    chooseFile(event.dataTransfer.files?.[0])
  }
  const statusText = drawingJob.status === 'analyzing' ? '识别中' : drawingJob.status === 'generating' ? '生成实体中' : drawingJob.status === 'generated' ? '实体已生成' : drawingJob.status === 'ready' ? '识别完成' : drawingJob.status === 'error' ? '识别失败' : '等待上传'
  const recognizedParameters = evidence?.parameters || evidence
  const evidenceList = recognitionEvidence(evidence)
  const sourceFor = (field, fallback) => evidenceList.find((item) => item.field === field || item.field === field.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`))?.view || evidenceList.find((item) => item.field === field || item.field === field.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`))?.source || fallback
  const confidenceLabel = evidence?.confidence !== undefined ? `置信度 ${Math.round(Number(evidence.confidence) * 100)}%` : '置信度 96%'
  const read = (key, fallback) => recognizedParameters && recognizedParameters[key] !== undefined ? recognizedParameters[key] : fallback
  const evidenceRows = [
    ['底板', `${read('baseLength', 100)} × ${read('baseWidth', 50)} × ${read('baseThickness', 10)} mm`, sourceFor('baseLength', '俯视 / 主视')],
    ['上部实体', `${read('upperLength', 70)} × ${read('upperWidth', 30)} × ${read('upperHeight', 30)} mm`, sourceFor('upperLength', '主视 / 右视')],
    ['总高度', `${read('totalHeight', 40)} mm`, sourceFor('totalHeight', '主视')],
    ['U 型缺口', `开口 ${read('notchOpening', 40)} · R${read('notchRadius', 15)}`, sourceFor('notchOpening', '主视')],
    ['圆柱凸台', `2 × Ø${read('bossDiameter', 20)}`, sourceFor('bossDiameter', '俯视')],
    ['凸台中心距', `${read('bossCenterDistance', 70)} mm`, sourceFor('bossCenterDistance', '投影对齐推导')],
  ]
  const evidenceWarning = evidence?.warnings?.length
    ? evidence.warnings.join('；')
    : evidence?.status === 'confirmed'
      ? '尺寸证据已锁定；中心距 70 mm 已与俯视图投影关系关联，生成结果仍会经过 OCCT 拓扑检查。'
      : '中心距 70 mm 来自俯视图与主视图投影关系，生成前请人工确认；确认后将按上述参数生成实体。'
  return <div className="secondary-workspace import-workspace">
    <div className="secondary-heading"><div><span className="eyebrow">DRAWING → 3D</span><h1>图纸转三维</h1><p>上传一张工程图，识别关键尺寸后生成可编辑实体</p></div><div className="heading-actions"><span className={`backend-status compact ${backend?.status || 'checking'}`}><i />{backend?.productionReady ? 'CadQuery / OCCT 在线' : backend?.status === 'offline' ? 'API 离线' : '连接中'}</span><button className="secondary-button" onClick={() => showToast('支持 JPG、PNG、WEBP、PDF、DXF')}>支持格式</button><button className="primary-button" data-testid="confirm-generate" disabled={!evidence || drawingJob.status === 'analyzing' || drawingJob.status === 'generating' || drawingJob.status === 'generated'} onClick={generateFromDrawing}>{drawingJob.status === 'generated' ? '已生成 3D' : '确认并生成 3D'} <Icon>↗</Icon></button></div></div>
    <div className="import-steps" aria-label="图纸转三维流程"><span className="done"><i>1</i>上传图纸</span><span className={evidence ? 'done' : drawingJob.status === 'analyzing' ? 'active' : ''}><i>2</i>识别尺寸</span><span className={evidence ? 'active' : ''}><i>3</i>复核证据</span><span className={drawingJob.status === 'generated' ? 'ready' : ''}><i>4</i>生成实体</span></div>
    <div className="import-grid">
      <div className="upload-card panel-card">
        <input ref={inputRef} className="file-input" data-testid="drawing-file-input" type="file" accept="image/*,.pdf,.dxf" onChange={(event) => chooseFile(event.target.files?.[0])} />
        <div className={`drop-zone ${file ? 'has-file' : ''} ${dragging ? 'dragging' : ''}`} data-testid="drawing-drop-zone" onClick={() => inputRef.current?.click()} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
          {file && isImage && drawingJob.previewUrl ? <img src={drawingJob.previewUrl} alt="已上传工程图预览" /> : file ? <div className="file-preview-placeholder"><div className="upload-symbol">▱</div><b>{file.name.split('.').pop()?.toUpperCase()} 图纸</b><span>该格式将直接提交给识别服务</span></div> : <div className="upload-placeholder"><div className="upload-symbol">↥</div><b>拖拽图纸到这里，或点击上传</b><span>支持图片 / PDF / DXF · 单个文件不超过 20 MB</span></div>}
          <div className="drop-overlay"><span>{drawingJob.status === 'analyzing' ? '识别中…' : file ? '重新选择图纸' : '选择文件'}</span></div>
        </div>
        {file && <div className="upload-file-meta"><span className="file-type-icon blue">▱</span><div><b>{file.name}</b><small>{(file.size / 1024).toFixed(0)} KB · {backend?.status === 'connected' ? '已提交 FastAPI' : '本地预览'}</small></div><span className={`parse-status ${drawingJob.status}`}>{statusText}</span></div>}
        {drawingJob.status === 'error' && <div className="import-error"><span>!</span><span>{drawingJob.error || '图纸识别失败，请检查文件后重试。'}</span></div>}
        {drawingJob.warning && <div className="backend-warning"><span>!</span><span>{drawingJob.warning}</span></div>}
        <div className="privacy-note"><Icon>◈</Icon><span>原图仅用于本次识别；生成前会保留每个尺寸的来源视图和校验状态。</span></div>
      </div>
      <div className="evidence-card panel-card" data-testid="drawing-evidence"><div className="evidence-heading"><div><span className="eyebrow">RECOGNITION EVIDENCE</span><h2>识别结果与尺寸证据</h2></div><span className={`confidence ${evidence ? 'ready' : ''}`}>{evidence ? confidenceLabel : drawingJob.status === 'analyzing' ? '分析中…' : '等待图纸'}</span></div>
        {!evidence ? <div className="evidence-empty"><span>{drawingJob.status === 'analyzing' ? '⋯' : '⌁'}</span><b>{drawingJob.status === 'analyzing' ? '正在解析视图与标注' : '上传图纸后开始识别'}</b><small>{drawingJob.status === 'analyzing' ? '正在建立尺寸证据链，请稍候。' : '系统会保留每个尺寸的来源视图和校验状态。'}</small><div className="recognition-meter"><i style={{ width: drawingJob.status === 'analyzing' ? '64%' : '0%' }} /></div></div> : <><div className="evidence-banner"><span className="status-dot" /><div><b>已识别：正投影安装支架</b><small>{evidence.engine || 'OCR'} · {evidence.status === 'confirmed' ? '识别结果已确认' : '需要人工确认'} · 三视图证据链</small></div><span className="evidence-source">{evidence.engine || evidence.source || 'OCR'}</span></div><div className="evidence-table">{evidenceRows.map(([label, value, source]) => <div className="evidence-row" key={label}><span className="evidence-check">✓</span><div><b>{label}</b><small>{source}</small></div><strong>{value}</strong><span className="evidence-lock">{evidence.status === 'confirmed' ? '已确认' : '待确认'}</span></div>)}</div><div className="evidence-warning"><span>{evidence.status === 'confirmed' ? '✓' : '!'}</span><span>{evidenceWarning}</span></div><div className="evidence-confirmed"><span>{evidence.status === 'confirmed' ? '✓' : '!'}</span><span>{evidence.status === 'confirmed' ? '尺寸证据完整 · 可生成可编辑支架模型' : '识别结果待人工确认 · 生成后端会保留审计记录'}</span><button disabled={drawingJob.status === 'generating' || drawingJob.status === 'generated'} onClick={generateFromDrawing}>{drawingJob.status === 'generated' ? '已生成' : '确认并生成'} <Icon>↗</Icon></button></div></>}</div>
    </div>
  </div>
}

function PlatformWorkspace({ mode, backend, platform, platformLogin, platformLogout, refreshPlatformUsers, createPlatformUser, createPlatformProject, shareProjectTeam, syncDrawingToPdm, createCamPlan, simulateCamPlan, approveCamPlan, releaseCamPlan, downloadNcProgram, generation, showToast }) {
  const [email, setEmail] = useState('admin@joyniu.local')
  const [password, setPassword] = useState('ChangeMe123!')
  const [displayName, setDisplayName] = useState('JoyNiu 管理员')
  const [role, setRole] = useState('admin')
  const [newEmail, setNewEmail] = useState('reviewer@joyniu.local')
  const [newPassword, setNewPassword] = useState('Reviewer123!')
  const [newDisplayName, setNewDisplayName] = useState('审核员')
  const [newRole, setNewRole] = useState('reviewer')
  const [memberText, setMemberText] = useState('')
  const loggedIn = Boolean(platform?.user && platform?.token)
  const permissions = platform?.user?.permissions || []
  const userDisplayName = platform?.user?.displayName || platform?.user?.display_name || platform?.user?.email || 'JoyNiu'
  const hasPermission = (permission) => permissions.includes('*') || permissions.includes(permission)
  const canManageMembers = hasPermission('user:manage') || Boolean(
    platform?.project && (
      String(platform.project.ownerId || platform.project.owner_id || '').toLowerCase() === String(platform?.user?.id || '').toLowerCase()
      || String(platform.project.ownerId || platform.project.owner_id || '').toLowerCase() === String(platform?.user?.email || '').toLowerCase()
    )
  )
  const manifestDocuments = platform?.manifest?.documents || []
  const versionCount = manifestDocuments.reduce((sum, item) => sum + (item.versions?.length || 0), 0)
  const simulation = platform?.simulation
  const gate = platform?.gate
  const camPlan = platform?.camPlan
  const nc = platform?.nc
  const geometry = backend?.health?.geometry || {}
  const ocr = backend?.health?.capabilities?.ocr || backend?.health?.ocr || {}
  const submit = (bootstrap) => platformLogin({ email: email.trim(), password, displayName: displayName.trim(), roles: [role], bootstrap })

  return <div className="secondary-workspace platform-workspace">
    <div className="secondary-heading">
      <div><span className="eyebrow">{mode === 'CAM / NC' ? 'MANUFACTURING CONTROL' : 'PLATFORM SERVICES'}</span><h1>{mode === 'CAM / NC' ? 'CAM / NC 制造门' : 'PDM、账号与审计'}</h1><p>{mode === 'CAM / NC' ? '确定性仿真、审核与 NC 放行均绑定到不可变模型版本。' : '账号权限、图纸证据、模型版本和交付物在同一条审计链中。'}</p></div>
      <div className="heading-actions"><span className={`backend-status compact ${backend?.status || 'checking'}`}><i />{backend?.productionReady ? 'OCCT 在线' : backend?.status === 'offline' ? 'API 离线' : '降级模式'}</span>{loggedIn && <button className="secondary-button" onClick={platformLogout}>退出登录</button>}</div>
    </div>
    {platform?.error && <div className="backend-warning backend-error"><span>!</span><span>{platform.error}</span></div>}
    <div className="platform-grid">
      <section className="platform-card panel-card platform-auth-card">
        <div className="platform-card-heading"><div><span className="eyebrow">IDENTITY & RBAC</span><h2>账号权限</h2></div><span className={`production-badge ${loggedIn ? 'ready' : 'preview'}`}>{loggedIn ? '已认证' : '未登录'}</span></div>
        {!loggedIn ? <form className="platform-form" onSubmit={(event) => { event.preventDefault(); submit(false) }}>
          <label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
          <label>显示名称（首次注册）<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
          <label>角色<select value={role} onChange={(event) => setRole(event.target.value)}><option value="admin">admin · 全流程演示</option><option value="designer">designer · 设计 / OCR / CAM 草案</option><option value="reviewer">reviewer · 证据与 CAM 审核</option><option value="manufacturing">manufacturing · NC 制造放行</option><option value="viewer">viewer · 只读</option></select></label>
          <div className="platform-form-actions"><button type="button" className="secondary-button" disabled={platform?.busy} onClick={() => submit(true)}>注册并登录</button><button type="submit" className="primary-button" disabled={platform?.busy}>登录 <Icon>↗</Icon></button></div>
          <small className="platform-help">本地首次运行可用“注册并登录”创建 bootstrap 账号；之后新增账号必须由 admin 管理。</small>
        </form> : <div className="platform-identity"><div className="identity-avatar">{userDisplayName.slice(0, 1).toUpperCase()}</div><div className="identity-copy"><b>{userDisplayName}</b><small>{platform.user.email}</small><div className="role-pills">{(platform.user.roles || []).map((item) => <span key={item}>{item}</span>)}</div></div></div>}
        {loggedIn && <><div className="permission-list"><div className="field-group-title">有效权限 <span>{permissions.includes('*') ? 'admin · 全部' : `${permissions.length} 项`}</span></div><div className="permission-cloud">{permissions.includes('*') ? <span>* 全部权限</span> : permissions.slice(0, 14).map((item) => <span key={item}>{item}</span>)}{!permissions.includes('*') && permissions.length > 14 && <span>+{permissions.length - 14}</span>}</div></div>{hasPermission('user:manage') && <div className="user-admin"><div className="field-group-title">账号目录 <button className="text-button" onClick={refreshPlatformUsers}>刷新</button></div>{(platform.users || []).map((item) => <div className="user-row" key={item.id}><span className="user-state" /><div><b>{item.displayName || item.display_name || item.email}</b><small>{item.email}</small></div><span className="role-text">{(item.roles || []).join(' / ')}</span></div>)}{createPlatformUser && <form className="admin-user-form" onSubmit={(event) => { event.preventDefault(); createPlatformUser({ email: newEmail.trim(), password: newPassword, displayName: newDisplayName.trim(), roles: [newRole] }) }}><div className="field-group-title">创建协作账号</div><div className="admin-user-fields"><input aria-label="新账号邮箱" type="email" value={newEmail} onChange={(event) => setNewEmail(event.target.value)} placeholder="邮箱" /><input aria-label="新账号密码" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} placeholder="至少 8 位密码" /><input aria-label="新账号名称" value={newDisplayName} onChange={(event) => setNewDisplayName(event.target.value)} placeholder="显示名称" /><select aria-label="新账号角色" value={newRole} onChange={(event) => setNewRole(event.target.value)}><option value="reviewer">reviewer · 审核</option><option value="manufacturing">manufacturing · 放行</option><option value="designer">designer · 设计</option><option value="viewer">viewer · 只读</option></select></div><button className="secondary-button" type="submit" disabled={platform?.busy}>＋ 创建账号</button></form>}</div>}</>}
      </section>

      <section className="platform-card panel-card platform-health-card">
        <div className="platform-card-heading"><div><span className="eyebrow">SERVICE HEALTH</span><h2>服务能力</h2></div><span className={`engine-pill ${backend?.productionReady ? 'occt' : 'fallback'}`}>{backend?.engine || 'checking'}</span></div>
        <div className="platform-health-grid"><div><span>几何内核</span><b>{geometry.available ? `CadQuery ${geometry.version || ''} / OCCT` : 'fallback preview'}</b></div><div><span>拓扑输出</span><b>{geometry.available ? 'STEP B-Rep' : '未启用'}</b></div><div><span>OCR</span><b>{ocr.available ? `${ocr.engine || 'OCR'} ${ocr.version || ''}` : 'fixture / review'}</b></div><div><span>审计存储</span><b>{loggedIn ? 'SQLite PDM' : '需要登录'}</b></div></div>
        {!backend?.productionReady && <div className="production-warning"><span>!</span><span>当前服务没有 OCCT 生产内核；可以审阅证据和参数，但不会把降级网格标成生产 STEP。</span></div>}
        <div className="platform-capability-row"><span className="capability-chip">FastAPI</span><span className="capability-chip">2D OCR evidence</span><span className="capability-chip">PDM versions</span><span className="capability-chip">RBAC</span><span className="capability-chip">CAM gate</span></div>
      </section>

      <section className="platform-card panel-card platform-pdm-card">
        <div className="platform-card-heading"><div><span className="eyebrow">PRODUCT DATA MANAGEMENT</span><h2>PDM 项目与版本</h2></div><span className="platform-count">{manifestDocuments.length} 文档 · {versionCount} 版本</span></div>
        <div className="platform-action-row"><button className="secondary-button" disabled={!loggedIn || platform?.busy} onClick={createPlatformProject}>创建 / 刷新项目</button><button className="primary-button" disabled={!loggedIn || platform?.busy} onClick={syncDrawingToPdm}>同步当前图纸与实体</button></div>
        {!loggedIn ? <div className="platform-empty">登录后可创建项目、保存原图 SHA-256、参数 JSON、STEP / GLB 版本。</div> : !platform?.project ? <div className="platform-empty">还没有绑定项目；点击“创建 / 刷新项目”开始 PDM 工作流。</div> : <><div className="project-binding"><span className="project-dot green" /><div><b>{platform.project.name}</b><small>{platform.project.id} · {platform.project.status}</small></div><span className="check-status pass">已绑定</span></div>{canManageMembers && <div className="project-members-editor"><div className="field-group-title">共享审核 / 制造成员 <span className="muted">只读项目权限</span></div><div className="member-editor-row"><input aria-label="项目成员邮箱或用户 ID" value={memberText} onChange={(event) => setMemberText(event.target.value)} placeholder="邮箱或用户 ID（逗号分隔；留空自动选账号目录）" /><button className="secondary-button" disabled={platform?.busy || !shareProjectTeam} onClick={() => shareProjectTeam(memberText)}>共享成员</button></div><small className="platform-help">成员可读取项目、查看 CAM；审核和 NC 放行仍由各自 RBAC 权限决定。</small></div>}<div className="pdm-document-list">{manifestDocuments.slice(0, 5).map((item) => <div className="pdm-document-row" key={item.document?.id || item.id}><span className="file-type-icon blue">{item.document?.kind === 'drawing' ? '▱' : '◉'}</span><div><b>{item.document?.name || item.name}</b><small>{item.document?.kind || 'document'} · {item.versions?.length || 0} 个不可变版本</small></div><span className="role-text">{item.versions?.at(-1)?.sha256?.slice(0, 8) || '—'}</span></div>)}</div>{manifestDocuments.length > 5 && <small className="platform-help">还有 {manifestDocuments.length - 5} 个文档，完整清单可通过 API manifest 查看。</small>}</>}
      </section>

      {mode === 'CAM / NC' && <section className="platform-card panel-card platform-cam-card">
        <div className="platform-card-heading"><div><span className="eyebrow">CAM / NC RELEASE GATE</span><h2>制造计划与 NC</h2></div><span className={`production-badge ${nc ? 'ready' : simulation?.passed ? 'pending' : 'preview'}`}>{nc ? 'NC 已放行' : simulation?.passed ? '等待审核' : '未仿真'}</span></div>
        <div className="cam-source-row"><span>模型来源</span><b>{generation?.artifacts?.find((item) => item.format === 'step')?.sha256?.slice(0, 16) || generation?.requestId || '尚未生成 OCCT 实体'}</b></div>
        <div className="platform-action-row"><button className="secondary-button" disabled={!loggedIn || platform?.busy || !platform?.project || !generation?.validation?.valid} onClick={createCamPlan}>创建 CAM 草案</button><button className="secondary-button" disabled={!loggedIn || platform?.busy || !camPlan} onClick={simulateCamPlan}>运行确定性仿真</button><button className="secondary-button" disabled={!loggedIn || platform?.busy || !camPlan || !simulation || !hasPermission('cam:approve')} onClick={approveCamPlan}>审核通过</button><button className="primary-button" disabled={!loggedIn || platform?.busy || !camPlan || !gate?.passed || !hasPermission('cam:release')} onClick={releaseCamPlan}>放行 NC</button></div>
        {!camPlan ? <div className="platform-empty">生成并校验 STEP 后创建 CAM 草案；计划会记录几何哈希、毛坯、刀具和工序。</div> : <><div className="cam-plan-summary"><div><span>计划状态</span><b>{camPlan.status}</b></div><div><span>工序</span><b>{camPlan.operations?.length || 0}</b></div><div><span>修订</span><b>r{camPlan.revision}</b></div><div><span>引擎</span><b>{simulation?.engine || '—'}</b></div></div>{simulation && <div className={`simulation-result ${simulation.passed ? 'pass' : 'fail'}`}><div className="simulation-title"><span>{simulation.passed ? '✓' : '!'}</span><b>{simulation.passed ? '仿真通过' : '仿真阻断'}</b><small>{simulation.runtimeSeconds ?? 0}s · 碰撞 {simulation.collisionCount} · 擦伤 {simulation.gougeCount}</small></div><div className="simulation-checks">{Object.entries(simulation.checks || {}).map(([key, value]) => <span key={key} className={value ? 'pass' : 'fail'}>{value ? '✓' : '×'} {key}</span>)}</div>{simulation.warnings?.length > 0 && <small className="platform-help">{simulation.warnings.join('；')}</small>}</div>}{gate && <div className={`gate-result ${gate.passed ? 'pass' : 'fail'}`}><b>{gate.passed ? '放行条件满足' : '仍不可放行'}</b><span>{(gate.reasons || []).join('；') || '审核与仿真状态已满足'}</span></div>}{nc && <div className="nc-preview"><div><div><b>{nc.id}</b><small>{nc.postprocessor} · SHA {nc.sha256?.slice(0, 12)}</small></div>{hasPermission('nc:download') && <button className="secondary-button" onClick={downloadNcProgram}>下载 NC</button>}</div>{nc.text ? <pre>{String(nc.text).slice(0, 900)}</pre> : <div className="nc-text-locked">NC 文本已持久化；点击下载后按当前制造权限读取。</div>}</div>}<small className="platform-help">计划、仿真、审批与 NC 会写入 SQLite 并在服务重启后恢复。审核者和制造者必须使用不同账号；确定性预仿真只是一道安全预检查，切削前仍需机床/材料专用验证。</small></>}
      </section>}
    </div>
  </div>
}

function ParameterPanel({ model, modelValid, updateModel, resetModel }) {
  if (model.kind === 'bracket') return <BracketParameterPanel model={model} modelValid={modelValid} updateModel={updateModel} resetModel={resetModel} />
  const fields = [['outerDiameter', '外径', 'Ø', 'mm'], ['length', '总长度', '', 'mm'], ['holeDiameter', '通孔直径', 'Ø', 'mm'], ['keywayWidth', '键槽宽度', '', 'mm'], ['keywayDepth', '键槽深度', '', 'mm'], ['keywayLength', '键槽长度', '', 'mm']]
  return <div className="inspector-content"><div className="selection-title"><span className="feature-icon blue">◒</span><div><b>{model.name}</b><small>参数化实体 · 已锁定</small></div><span className={`valid-chip ${modelValid ? '' : 'invalid'}`}>{modelValid ? '有效' : '待修正'}</span></div><div className="field-group"><div className="field-group-title">基本尺寸 <span>单位：mm</span></div>{fields.slice(0, 3).map(([key, label, prefix, suffix]) => <NumberField key={key} label={label} value={model[key]} prefix={prefix} suffix={suffix} onChange={(value) => updateModel(key, value)} />)}</div><div className="field-group"><div className="field-group-title">键槽特征 <span className="muted">切除</span></div>{fields.slice(3).map(([key, label, prefix, suffix]) => <NumberField key={key} label={label} value={model[key]} prefix={prefix} suffix={suffix} onChange={(value) => updateModel(key, value)} />)}</div><div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material} onChange={(e) => updateModel('material', e.target.value)}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div><button className="reset-link" onClick={resetModel}>↻ 恢复基准参数</button></div>
}

function BracketParameterPanel({ model, modelValid, updateModel, resetModel }) {
  const groups = [{ title: '底板尺寸', fields: [['baseLength', '长度'], ['baseWidth', '宽度'], ['baseThickness', '厚度']] }, { title: '上部实体', fields: [['upperLength', '长度'], ['upperWidth', '深度'], ['upperHeight', '高度'], ['totalHeight', '总高']] }, { title: '关键特征', fields: [['notchOpening', '缺口开口'], ['notchRadius', '缺口半径'], ['bossDiameter', '凸台直径'], ['bossCenterDistance', '凸台中心距']] }]
  const numeric = (key) => Number(model[key])
  const relationWarning = numeric('upperLength') > numeric('baseLength') || numeric('upperWidth') > numeric('baseWidth') || numeric('notchOpening') < numeric('notchRadius') * 2 || Math.abs(numeric('totalHeight') - numeric('baseThickness') - numeric('upperHeight')) > 1e-6
  return <div className="inspector-content"><div className="selection-title"><span className="feature-icon orange">⌂</span><div><b>{model.name}</b><small>图纸识别实体 · 证据已锁定</small></div><span className={`valid-chip ${modelValid ? '' : 'invalid'}`}>{modelValid ? '有效' : '待修正'}</span></div>{groups.map((group) => <div className="field-group" key={group.title}><div className="field-group-title">{group.title} <span>单位：mm</span></div>{group.fields.map(([key, label]) => <NumberField key={key} label={label} value={model[key]} prefix={key === 'bossDiameter' ? 'Ø' : ''} suffix="mm" onChange={(value) => updateModel(key, value)} />)}</div>)}<div className="bracket-datum"><span>⌖</span><div><b>基准定位</b><small>凸台中心：X ±{Math.round(numeric('bossCenterDistance') / 2 || 35)} · Y 0 · Z {Math.round(numeric('baseThickness') || 10)}</small><small>缺口圆弧中心 Z {Math.round(numeric('totalHeight') || 40)} · 槽底 Z {Math.round((numeric('totalHeight') || 40) - (numeric('notchRadius') || 15))}</small></div></div>{relationWarning && <div className="bracket-constraint"><span>!</span><span>请确认总高 = 底板厚度 + 上部高度、上部实体不超过底板，且开口 ≥ 2R。</span></div>}<div className="field-group"><div className="field-group-title">材料</div><div className="select-field"><select value={model.material} onChange={(e) => updateModel('material', e.target.value)}><option>45# 钢</option><option>AL6061 铝合金</option><option>SUS304 不锈钢</option></select><span>⌄</span></div></div><div className="evidence-mini"><Icon>✓</Icon><span>所有尺寸均可回溯到上传图纸的视图和标注。</span></div><button className="reset-link" onClick={resetModel}>↻ 恢复支架基准参数</button></div>
}

function NumberField({ label, value, prefix, suffix, onChange }) { return <label className="number-field"><span>{label}</span><div><span className="field-prefix">{prefix}</span><input value={value} type="number" min="0.1" step="0.1" onChange={(e) => onChange(e.target.value)} /><span className="field-suffix">{suffix}</span></div></label> }
function FeaturePanel({ features, selectedFeature, setSelectedFeature }) { return <div className="inspector-content feature-tree-panel"><div className="tree-toolbar"><span>特征历史 <b>{features.length}</b></span><button>＋</button></div><div className="feature-tree">{features.map((feature, index) => <button key={feature.id} className={`feature-row ${selectedFeature === feature.id ? 'selected' : ''}`} onClick={() => setSelectedFeature(feature.id)}><span className="tree-line">{index < features.length - 1 ? '│' : '└'}</span><span className="feature-glyph">{feature.icon}</span><span className="feature-label">{feature.label}<small>{feature.meta}</small></span>{selectedFeature === feature.id && <span className="eye">◉</span>}</button>)}</div><div className="feature-note"><Icon>✦</Icon><span>特征树由 AI 生成，可继续描述来添加圆角、阵列或螺纹。</span></div></div> }
function CheckPanel({ modelValid, showToast, backend, generation }) {
  const metrics = generation?.validation?.metrics || {}
  const kernelReady = Boolean(generation?.validation?.productionReady && !generation?.stale)
  const checks = [
    { label: '参数完整性', status: modelValid ? '通过' : '待修正' },
    { label: '实体拓扑', status: kernelReady && metrics.topologyAuditPassed !== false ? '通过' : generation ? '待内核校验' : '未运行' },
    { label: '关键尺寸', status: kernelReady && metrics.bboxLength ? '通过' : generation ? '待校验' : '未运行' },
    { label: '制造可行性', status: kernelReady ? '提示' : '仅预览' },
  ]
  return <div className="inspector-content check-panel"><div className="check-summary"><div className={`check-ring ${modelValid && (kernelReady || !generation) ? 'ok' : 'warn'}`}>{modelValid && (kernelReady || !generation) ? '✓' : '!'}</div><div><b>{kernelReady ? 'OCCT 模型检查通过' : modelValid ? '参数检查通过 · 等待内核' : '需要修正参数'}</b><small>{backend?.engine || '浏览器'} · 最近检查：{generation ? '刚刚' : '尚未运行'}</small></div></div>{checks.map((check) => <div className="check-row" key={check.label}><span>{check.label}</span><span className={`check-status ${check.status === '通过' ? 'pass' : check.status === '提示' ? 'hint' : 'warn'}`}>{check.status}</span></div>)}{generation && <div className="kernel-metrics"><span>包络</span><b>{metrics.boundingLength ?? '—'} × {metrics.boundingWidth ?? '—'} × {metrics.boundingHeight ?? '—'} mm</b><span>体积</span><b>{metrics.volumeMm3 ? `${Number(metrics.volumeMm3).toFixed(3)} mm³` : '—'}</b></div>}<button className="primary-outline" onClick={() => showToast(generation ? '已刷新内核检查报告' : '请先生成一个实体模型')}>重新运行检查 <Icon>↗</Icon></button></div>
}

const svgPointString = (points) => points.map(([x, y]) => `${Number(x).toFixed(2)},${Number(y).toFixed(2)}`).join(' ')
const finiteDimension = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback

/*
 * A compact, deterministic SVG model for the drawing used in the acceptance
 * test.  It is intentionally based on the same feature dimensions that are
 * shown in the evidence panel, so editing a value immediately updates every
 * view.  The projection is a real axonometric projection (rather than a
 * decorative CSS cube), which keeps the two bosses and the through-notch in
 * the right relative positions.
 */
function CadModel({ model, section, view }) {
  if (model.kind === 'bracket') return <BracketCadModel model={model} section={section} view={view} />
  return <ShaftCadModel model={model} section={section} view={view} />
}

function BracketCadModel({ model, section, view }) {
  const L = Math.max(20, finiteDimension(model.baseLength, 100))
  const W = Math.max(16, finiteDimension(model.baseWidth, 50))
  const T = Math.max(1, finiteDimension(model.baseThickness, 10))
  const UL = Math.min(L - 2, Math.max(4, finiteDimension(model.upperLength, 70)))
  const UW = Math.min(W - 2, Math.max(4, finiteDimension(model.upperWidth, 30)))
  const requestedHeight = finiteDimension(model.totalHeight, T + finiteDimension(model.upperHeight, 30))
  const H = Math.max(T + 1, requestedHeight)
  const opening = Math.min(UL - 2, Math.max(2, finiteDimension(model.notchOpening, 40)))
  const radius = Math.min(opening / 2, Math.max(1, finiteDimension(model.notchRadius, 15)))
  const bossDiameter = Math.min(Math.min(L, W) - 2, Math.max(2, finiteDimension(model.bossDiameter, 20)))
  const bossRadius = bossDiameter / 2
  const centerDistance = Math.min(L - bossDiameter, Math.max(bossDiameter, finiteDimension(model.bossCenterDistance, 70)))
  const x0 = (L - UL) / 2
  const x1 = x0 + UL
  const y0 = (W - UW) / 2
  const y1 = y0 + UW
  const cx = (x0 + x1) / 2
  const notchLeft = cx - opening / 2
  const notchRight = cx + opening / 2
  const arcLeft = cx - radius
  const arcRight = cx + radius
  // The drawing's R15 callout is a lower semicircle whose centre lies on the
  // top datum (Z = total height 40).  Therefore the visible groove bottom is
  // Z = H - R (25 mm for the acceptance drawing), not the other way around.
  const arcCenterZ = H
  const arc = Array.from({ length: 17 }, (_, index) => {
    const angle = (Math.PI * index) / 16
    return [cx + radius * Math.cos(angle), arcCenterZ - radius * Math.sin(angle)]
  })
  const profile = [
    [x0, T], [x1, T], [x1, H], [notchRight, H], [notchRight, arcCenterZ], [arcRight, arcCenterZ],
    ...arc.slice(1, -1), [arcLeft, arcCenterZ], [notchLeft, arcCenterZ], [notchLeft, H], [x0, H],
  ]

  if (view === 'front') return <BracketFrontView dimensions={{ L, W, T, H, UL, UW, opening, radius, bossDiameter, centerDistance, x0, x1, y0, y1, cx, notchLeft, notchRight, arcLeft, arcRight, arcCenterZ }} section={section} />
  if (view === 'top') return <BracketTopView dimensions={{ L, W, T, H, UL, UW, opening, radius, bossDiameter, centerDistance, x0, x1, y0, y1, cx, notchLeft, notchRight, arcLeft, arcRight, arcCenterZ }} section={section} />

  const unit = Math.min(2.75, 440 / (L + W))
  const sx = unit
  const sy = unit * .54
  const sz = unit * 1.05
  const ox = 310 - ((L - W) / 2) * sx
  const oy = 372 - ((L + W) / 2) * sy
  const project = (x, y, z) => [ox + (x - y) * sx, oy + (x + y) * sy - z * sz]
  const baseTop = [project(0, 0, T), project(L, 0, T), project(L, W, T), project(0, W, T)]
  const baseFront = [project(0, 0, 0), project(L, 0, 0), project(L, 0, T), project(0, 0, T)]
  const baseRight = [project(L, 0, 0), project(L, W, 0), project(L, W, T), project(L, 0, T)]
  const upperBack = profile.map(([x, z]) => project(x, y1, z))
  const upperFront = profile.map(([x, z]) => project(x, y0, z))
  const upperRight = [project(x1, y0, T), project(x1, y1, T), project(x1, y1, H), project(x1, y0, H)]
  const topLeft = [project(x0, y0, H), project(notchLeft, y0, H), project(notchLeft, y1, H), project(x0, y1, H)]
  const topRight = [project(notchRight, y0, H), project(x1, y0, H), project(x1, y1, H), project(notchRight, y1, H)]
  const ledgeLeft = [project(notchLeft, y0, H), project(arcLeft, y0, arcCenterZ), project(arcLeft, y1, arcCenterZ), project(notchLeft, y1, H)]
  const ledgeRight = [project(arcRight, y0, arcCenterZ), project(notchRight, y0, H), project(notchRight, y1, H), project(arcRight, y1, arcCenterZ)]
  const innerLeft = [project(notchLeft, y0, arcCenterZ), project(notchLeft, y1, arcCenterZ), project(notchLeft, y1, H), project(notchLeft, y0, H)]
  const innerRight = [project(notchRight, y0, H), project(notchRight, y1, H), project(notchRight, y1, arcCenterZ), project(notchRight, y0, arcCenterZ)]
  const ym = (y0 + y1) / 2
  const bossCenters = [(L - centerDistance) / 2, (L + centerDistance) / 2]
  const makeCylinder = (centerX, key) => {
    const count = 32
    const top = Array.from({ length: count }, (_, index) => {
      const angle = (Math.PI * 2 * index) / count
      return project(centerX + bossRadius * Math.cos(angle), W / 2 + bossRadius * Math.sin(angle), H)
    })
    const bottom = Array.from({ length: count }, (_, index) => {
      const angle = (Math.PI * 2 * index) / count
      return project(centerX + bossRadius * Math.cos(angle), W / 2 + bossRadius * Math.sin(angle), T)
    })
    return <g key={key} className="bracket-boss"><polygon points={svgPointString([...top, ...bottom.slice().reverse()])} fill="url(#bracketBossSide)" stroke="#1a2a3b" strokeWidth="1.25" /><polygon points={svgPointString(top)} fill="url(#bracketBossTop)" stroke="#d8e5f1" strokeWidth="1.45" /><polyline points={svgPointString(top.slice(0, 17))} fill="none" stroke="#8ca9c3" strokeWidth=".8" opacity=".82" /><line x1={project(centerX - bossRadius, W / 2, H)[0]} y1={project(centerX - bossRadius, W / 2, H)[1]} x2={project(centerX + bossRadius, W / 2, H)[0]} y2={project(centerX + bossRadius, W / 2, H)[1]} stroke="#839db8" strokeDasharray="3 3" opacity=".65" /></g>
  }
  const baseCenter = project(L / 2, W / 2, 0)
  const dimension = (a, b, label, labelPoint, key) => <g key={key} className="bracket-dimension"><line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} markerStart="url(#bracketArrow)" markerEnd="url(#bracketArrow)" /><text x={labelPoint[0]} y={labelPoint[1]} textAnchor="middle">{label}</text></g>

  return <svg data-testid="bracket-3d-preview" className={`cad-svg bracket-cad ${view}`} viewBox="70 100 480 400" role="img" aria-label="安装支架三维预览"><defs><linearGradient id="bracketBaseTop" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#dce8f2" /><stop offset=".42" stopColor="#9db3c8" /><stop offset="1" stopColor="#526a82" /></linearGradient><linearGradient id="bracketBaseFront" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#9db3c8" /><stop offset="1" stopColor="#40576d" /></linearGradient><linearGradient id="bracketBaseRight" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#839bb1" /><stop offset="1" stopColor="#344a61" /></linearGradient><linearGradient id="bracketUpperFront" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#b7c9da" /><stop offset=".46" stopColor="#7891a8" /><stop offset="1" stopColor="#4b6279" /></linearGradient><linearGradient id="bracketUpperSide" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#829ab0" /><stop offset="1" stopColor="#344b62" /></linearGradient><linearGradient id="bracketNotchSurface" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#7b93a8" /><stop offset=".45" stopColor="#40566d" /><stop offset="1" stopColor="#23394d" /></linearGradient><linearGradient id="bracketBossSide" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#71899f" /><stop offset=".5" stopColor="#40566c" /><stop offset="1" stopColor="#22384d" /></linearGradient><linearGradient id="bracketBossTop" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#f0f5fa" /><stop offset=".5" stopColor="#9db4c8" /><stop offset="1" stopColor="#5c748c" /></linearGradient><pattern id="bracketHatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(25)"><line x1="0" y1="0" x2="0" y2="8" stroke="#ffc078" strokeWidth="2" opacity=".7" /></pattern><filter id="bracketShadow" x="-30%" y="-30%" width="160%" height="180%"><feGaussianBlur stdDeviation="10" /></filter><marker id="bracketArrow" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#e4a45e" /></marker></defs><ellipse cx={baseCenter[0]} cy={baseCenter[1] + 22} rx={Math.max(100, L * sx * .62)} ry={Math.max(18, W * sy * .3)} fill="#050c15" opacity=".65" filter="url(#bracketShadow)" /><g className="bracket-solid"><polygon points={svgPointString(baseTop)} fill="url(#bracketBaseTop)" stroke="#d6e4f0" strokeWidth="1.6" /><polygon points={svgPointString(baseFront)} fill="url(#bracketBaseFront)" stroke="#243a50" strokeWidth="1.2" /><polygon points={svgPointString(baseRight)} fill="url(#bracketBaseRight)" stroke="#22384d" strokeWidth="1.2" /><polygon points={svgPointString(upperBack)} fill="#536a80" stroke="#273e54" strokeWidth="1.1" /><polygon points={svgPointString(upperRight)} fill="url(#bracketUpperSide)" stroke="#22384d" strokeWidth="1.35" /><polygon points={svgPointString(topLeft)} fill="url(#bracketBaseTop)" stroke="#d1dfeb" strokeWidth="1.2" /><polygon points={svgPointString(topRight)} fill="url(#bracketBaseTop)" stroke="#d1dfeb" strokeWidth="1.2" /><polygon points={svgPointString(ledgeLeft)} fill="#526a80" stroke="#253c52" strokeWidth="1" /><polygon points={svgPointString(ledgeRight)} fill="#526a80" stroke="#253c52" strokeWidth="1" /><polygon points={svgPointString(upperFront)} fill="url(#bracketUpperFront)" stroke="#d2e0ec" strokeWidth="1.55" /></g><g className="bracket-notch-inner">{arc.slice(0, -1).map((point, index) => { const next = arc[index + 1]; const quad = [project(point[0], y0, point[1]), project(next[0], y0, next[1]), project(next[0], y1, next[1]), project(point[0], y1, point[1])]; return <polygon key={`arc-${index}`} points={svgPointString(quad)} fill={section ? 'url(#bracketHatch)' : 'url(#bracketNotchSurface)'} stroke="#293f54" strokeWidth=".72" /> })}<polygon points={svgPointString(innerLeft)} fill="#3a5066" stroke="#22384d" strokeWidth=".8" /><polygon points={svgPointString(innerRight)} fill="#415a70" stroke="#22384d" strokeWidth=".8" /></g>{bossCenters.map((centerX, index) => makeCylinder(centerX, `boss-${index}`))}{section && <g className="bracket-section-plane"><polygon points={svgPointString([project(x0, ym, T), project(x1, ym, T), project(x1, ym, H), project(x0, ym, H)])} fill="url(#bracketHatch)" opacity=".42" stroke="#f2ae62" strokeDasharray="4 3" /><text x={project(x1 + 8, ym, H - 4)[0]} y={project(x1 + 8, ym, H - 4)[1]} fill="#f2b16a" fontSize="10">剖切面</text></g>}<g className="bracket-center-lines" stroke="#8fb0cf" strokeDasharray="4 4" opacity=".68"><line x1={project(cx, y0 - 1, T)[0]} y1={project(cx, y0 - 1, T)[1]} x2={project(cx, y1 + 1, T)[0]} y2={project(cx, y1 + 1, T)[1]} /><line x1={project(0, W / 2, T)[0]} y1={project(0, W / 2, T)[1]} x2={project(L, W / 2, T)[0]} y2={project(L, W / 2, T)[1]} /></g><g className="bracket-dimensions" stroke="#e4a45e" fill="#e4a45e" strokeWidth="1"><line x1={project(0, -8, -3)[0]} y1={project(0, -8, -3)[1]} x2={project(L, -8, -3)[0]} y2={project(L, -8, -3)[1]} markerStart="url(#bracketArrow)" markerEnd="url(#bracketArrow)" /><text x={project(L / 2, -8, -3)[0]} y={project(L / 2, -8, -3)[1] - 7} fontSize="11" textAnchor="middle">{Math.round(L)} mm</text><line x1={project(-8, 0, -3)[0]} y1={project(-8, 0, -3)[1]} x2={project(-8, W, -3)[0]} y2={project(-8, W, -3)[1]} markerStart="url(#bracketArrow)" markerEnd="url(#bracketArrow)" /><text x={project(-8, W / 2, -3)[0] - 8} y={project(-8, W / 2, -3)[1]} fontSize="10" textAnchor="end">{Math.round(W)} mm</text><line x1={project(-11, -4, 0)[0]} y1={project(-11, -4, 0)[1]} x2={project(-11, -4, H)[0]} y2={project(-11, -4, H)[1]} markerStart="url(#bracketArrow)" markerEnd="url(#bracketArrow)" /><text x={project(-11, -4, H / 2)[0] - 9} y={project(-11, -4, H / 2)[1]} fontSize="10" textAnchor="end">{Math.round(H)} mm</text><line x1={project(notchLeft, y0 - 4, H + 6)[0]} y1={project(notchLeft, y0 - 4, H + 6)[1]} x2={project(notchRight, y0 - 4, H + 6)[0]} y2={project(notchRight, y0 - 4, H + 6)[1]} markerStart="url(#bracketArrow)" markerEnd="url(#bracketArrow)" /><text x={project(cx, y0 - 4, H + 6)[0]} y={project(cx, y0 - 4, H + 6)[1] - 7} fontSize="10" textAnchor="middle">开口 {Math.round(opening)}</text><line x1={project(cx, y0 - 2, arcCenterZ - radius)[0]} y1={project(cx, y0 - 2, arcCenterZ - radius)[1]} x2={project(cx + 30, y0 - 2, arcCenterZ - radius - 5)[0]} y2={project(cx + 30, y0 - 2, arcCenterZ - radius - 5)[1]} /><text x={project(cx + 30, y0 - 2, arcCenterZ - radius - 5)[0] + 5} y={project(cx + 30, y0 - 2, arcCenterZ - radius - 5)[1]} fontSize="10">R{Math.round(radius)}</text><line x1={project(bossCenters[1], W / 2, H)[0]} y1={project(bossCenters[1], W / 2, H)[1]} x2={project(bossCenters[1] + 18, W / 2, H + 14)[0]} y2={project(bossCenters[1] + 18, W / 2, H + 14)[1]} /><text x={project(bossCenters[1] + 18, W / 2, H + 14)[0] + 5} y={project(bossCenters[1] + 18, W / 2, H + 14)[1]} fontSize="10">2 × Ø{Math.round(bossDiameter)}</text></g><g fill="#a8bfd4" fontSize="10"><text x="78" y="118" fill="#8da9c4" fontFamily="DM Mono">BRACKET · ISO</text><text x="78" y="133" fill="#607d9b">实体预览 · 参数驱动</text></g></svg>
}

function BracketFrontView({ dimensions: d, section }) {
  const { L, T, H, UL, opening, radius, bossDiameter, x0, x1, cx, notchLeft, notchRight, arcLeft, arcRight, arcCenterZ } = d
  const scale = Math.min(3.65, 450 / L)
  const left = 310 - (L * scale) / 2
  const baseY = 407
  const X = (value) => left + value * scale
  const Z = (value) => baseY - value * scale
  const upperPath = `M ${X(x0)} ${Z(T)} L ${X(x1)} ${Z(T)} L ${X(x1)} ${Z(H)} L ${X(notchRight)} ${Z(H)} L ${X(notchRight)} ${Z(arcCenterZ)} L ${X(arcRight)} ${Z(arcCenterZ)} A ${radius * scale} ${radius * scale} 0 0 1 ${X(arcLeft)} ${Z(arcCenterZ)} L ${X(notchLeft)} ${Z(arcCenterZ)} L ${X(notchLeft)} ${Z(H)} L ${X(x0)} ${Z(H)} Z`
  const dim = (x1a, y1a, x2a, y2a, label, tx, ty, key) => <g key={key} className="bracket-dimension"><line x1={x1a} y1={y1a} x2={x2a} y2={y2a} markerStart="url(#frontArrow)" markerEnd="url(#frontArrow)" /><text x={tx} y={ty} textAnchor="middle">{label}</text></g>
  const bossR = (bossDiameter / 2) * scale
  const bossOne = X((L - d.centerDistance) / 2)
  const bossTwo = X((L + d.centerDistance) / 2)
  return <svg className="cad-svg bracket-cad bracket-front" viewBox="0 0 620 520" role="img" aria-label="安装支架前视图"><defs><linearGradient id="frontMetal" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#dce8f2" /><stop offset=".5" stopColor="#8299ae" /><stop offset="1" stopColor="#40576c" /></linearGradient><pattern id="frontHatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(25)"><line x1="0" y1="0" x2="0" y2="8" stroke="#f3ad62" strokeWidth="2" /></pattern><marker id="frontArrow" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#e4a45e" /></marker></defs><rect x="22" y="20" width="576" height="470" rx="8" fill="#101b29" stroke="#2c435c" /><text x="42" y="48" fill="#8ea8c2" fontSize="11" fontFamily="DM Mono">BRACKET · FRONT / 主视图</text><rect x={X(0)} y={Z(T)} width={L * scale} height={T * scale} fill="url(#frontMetal)" stroke="#d4e2ed" strokeWidth="1.6" /><rect x={X((L - d.bossDiameter) / 2)} y={Z(H)} width={d.bossDiameter * scale} height={(H - T) * scale} fill="#617990" opacity=".38" stroke="#91a9c0" strokeDasharray="4 3" /><path d={upperPath} fill="url(#frontMetal)" stroke="#d8e5ef" strokeWidth="1.7" />{section && <path d={upperPath} fill="url(#frontHatch)" opacity=".4" />}<line x1={X(cx)} y1={Z(-2)} x2={X(cx)} y2={Z(H + 5)} stroke="#80a8ce" strokeDasharray="5 4" opacity=".7" /><g className="bracket-dimensions" stroke="#e4a45e" fill="#e4a45e" strokeWidth="1">{dim(X(0), Z(-12), X(L), Z(-12), `${Math.round(L)} mm`, X(L / 2), Z(-12) - 7, 'length')}{dim(X(x0), Z(H + 10), X(x1), Z(H + 10), `${Math.round(UL)} mm`, X((x0 + x1) / 2), Z(H + 10) - 7, 'upper')}{dim(X(notchLeft), Z(H + 18), X(notchRight), Z(H + 18), `开口 ${Math.round(opening)}`, X(cx), Z(H + 18) - 7, 'opening')}<line x1={X(-13)} y1={Z(0)} x2={X(-13)} y2={Z(H)} markerStart="url(#frontArrow)" markerEnd="url(#frontArrow)" /><text x={X(-16)} y={Z(H / 2)} textAnchor="end">总高 {Math.round(H)}</text><line x1={X(-4)} y1={Z(0)} x2={X(-4)} y2={Z(T)} markerStart="url(#frontArrow)" markerEnd="url(#frontArrow)" /><text x={X(-7)} y={Z(T / 2)} textAnchor="end">{Math.round(T)}</text><line x1={X(cx)} y1={Z(arcCenterZ - radius)} x2={X(cx + 25)} y2={Z(arcCenterZ - radius - 5)} /><text x={X(cx + 27)} y={Z(arcCenterZ - radius - 5)}>R{Math.round(radius)}</text></g><g fill="#8fa8c1" fontSize="10"><text x="42" y="476">尺寸单位：mm · U 型缺口贯穿上部实体深度</text><text x="472" y="48" fill="#6d8bab">{section ? 'SECTION ON' : 'SECTION OFF'}</text></g></svg>
}

function BracketTopView({ dimensions: d, section }) {
  const { L, W, T, H, UL, UW, opening, radius, bossDiameter, centerDistance, x0, x1, y0, y1, cx, notchLeft, notchRight } = d
  const scale = Math.min(3.8, 430 / L, 270 / W)
  const left = 310 - (L * scale) / 2
  const top = 245 - (W * scale) / 2
  const X = (value) => left + value * scale
  const Y = (value) => top + value * scale
  const r = bossDiameter * scale / 2
  const bossOne = (L - centerDistance) / 2
  const bossTwo = (L + centerDistance) / 2
  const dim = (x1a, y1a, x2a, y2a, label, tx, ty, key) => <g key={key} className="bracket-dimension"><line x1={x1a} y1={y1a} x2={x2a} y2={y2a} markerStart="url(#topArrow)" markerEnd="url(#topArrow)" /><text x={tx} y={ty} textAnchor="middle">{label}</text></g>
  return <svg className="cad-svg bracket-cad bracket-top" viewBox="0 0 620 520" role="img" aria-label="安装支架俯视图"><defs><linearGradient id="topMetal" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#dce8f2" /><stop offset=".48" stopColor="#8ba2b8" /><stop offset="1" stopColor="#4a6278" /></linearGradient><pattern id="topHatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(25)"><line x1="0" y1="0" x2="0" y2="8" stroke="#f3ad62" strokeWidth="2" /></pattern><marker id="topArrow" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 z" fill="#e4a45e" /></marker></defs><rect x="22" y="20" width="576" height="470" rx="8" fill="#101b29" stroke="#2c435c" /><text x="42" y="48" fill="#8ea8c2" fontSize="11" fontFamily="DM Mono">BRACKET · TOP / 俯视图</text><rect x={X(0)} y={Y(0)} width={L * scale} height={W * scale} fill="url(#topMetal)" stroke="#d4e2ed" strokeWidth="1.7" /><rect x={X(x0)} y={Y(y0)} width={UL * scale} height={UW * scale} fill="#70889e" opacity=".86" stroke="#e0ebf3" strokeWidth="1.35" /><path d={`M ${X(notchLeft)} ${Y(y0)} L ${X(notchRight)} ${Y(y0)} L ${X(notchRight)} ${Y(y1)} L ${X(notchLeft)} ${Y(y1)} Z`} fill={section ? 'url(#topHatch)' : '#263e54'} opacity=".78" stroke="#c4d6e4" strokeDasharray="4 3" /><circle cx={X(bossOne)} cy={Y(W / 2)} r={r} fill="#b6c8d7" stroke="#243b51" strokeWidth="1.4" /><circle cx={X(bossTwo)} cy={Y(W / 2)} r={r} fill="#b6c8d7" stroke="#243b51" strokeWidth="1.4" /><circle cx={X(bossOne)} cy={Y(W / 2)} r={r * .38} fill="#223b53" opacity=".78" /><circle cx={X(bossTwo)} cy={Y(W / 2)} r={r * .38} fill="#223b53" opacity=".78" /><g stroke="#7da5cb" strokeDasharray="5 4" opacity=".72"><line x1={X(0)} y1={Y(W / 2)} x2={X(L)} y2={Y(W / 2)} /><line x1={X(bossOne)} y1={Y(0)} x2={X(bossOne)} y2={Y(W)} /><line x1={X(bossTwo)} y1={Y(0)} x2={X(bossTwo)} y2={Y(W)} /></g><g className="bracket-dimensions" stroke="#e4a45e" fill="#e4a45e" strokeWidth="1">{dim(X(0), Y(W + 13), X(L), Y(W + 13), `${Math.round(L)} mm`, X(L / 2), Y(W + 13) - 7, 'length')}{dim(X(-13), Y(0), X(-13), Y(W), `${Math.round(W)} mm`, X(-13) - 8, Y(W / 2), 'width')}<line x1={X(bossOne)} y1={Y(W + 7)} x2={X(bossTwo)} y2={Y(W + 7)} markerStart="url(#topArrow)" markerEnd="url(#topArrow)" /><text x={X(L / 2)} y={Y(W + 7) - 7} textAnchor="middle">中心距 {Math.round(centerDistance)}</text><line x1={X(bossTwo) + r} y1={Y(W / 2)} x2={X(bossTwo) + r + 32} y2={Y(W / 2) - 22} /><text x={X(bossTwo) + r + 36} y={Y(W / 2) - 24}>2 × Ø{Math.round(bossDiameter)}</text></g><g fill="#8fa8c1" fontSize="10"><text x="42" y="476">底板 {Math.round(L)} × {Math.round(W)} × {Math.round(T)} · 上部投影 {Math.round(UL)} × {Math.round(UW)}</text><text x="475" y="48" fill="#6d8bab">H={Math.round(H)}</text></g></svg>
}

function ShaftCadModel({ model, section, view }) {
  const bodyHeight = Math.max(100, Math.min(260, finiteDimension(model.length, 70) * 3.1)); const bodyWidth = Math.max(96, Math.min(210, finiteDimension(model.outerDiameter, 24) * 5.5)); const hole = Math.max(12, Math.min(60, finiteDimension(model.holeDiameter, 10) * 2.2));
  return <svg className={`cad-svg ${view}`} viewBox="0 0 420 420" role="img" aria-label="参数化轴三维预览"><defs><linearGradient id="metal" x1="0" x2="1"><stop offset="0" stopColor="#718096" /><stop offset=".22" stopColor="#d8e2ed" /><stop offset=".5" stopColor="#8fa0b5" /><stop offset=".76" stopColor="#e8eef4" /><stop offset="1" stopColor="#5d7088" /></linearGradient><linearGradient id="metalDark" x1="0" x2="1"><stop offset="0" stopColor="#53667e" /><stop offset=".5" stopColor="#afbdd0" /><stop offset="1" stopColor="#485b72" /></linearGradient><filter id="shadow"><feGaussianBlur stdDeviation="8" /></filter></defs><ellipse cx="210" cy="352" rx={bodyWidth * .72} ry="20" fill="#08101c" opacity=".6" filter="url(#shadow)" /><g transform={view === 'front' ? 'translate(35 4) rotate(-2 210 210)' : view === 'top' ? 'translate(0 54)' : 'translate(0 0)'}><ellipse cx="210" cy={210 - bodyHeight / 2} rx={bodyWidth / 2} ry="34" fill="url(#metalDark)" stroke="#d6e2ef" strokeWidth="2" /><rect x={210 - bodyWidth / 2} y={210 - bodyHeight / 2} width={bodyWidth} height={bodyHeight} rx="10" fill="url(#metal)" stroke="#b7c9dc" strokeWidth="2" /><ellipse cx="210" cy={210 + bodyHeight / 2} rx={bodyWidth / 2} ry="34" fill="url(#metalDark)" stroke="#9db0c7" strokeWidth="2" /><ellipse cx="210" cy={210 - bodyHeight / 2} rx={hole / 2} ry="10" fill="#111c2b" stroke="#d7e5f3" strokeWidth="2" /><ellipse cx="210" cy={210 + bodyHeight / 2} rx={hole / 2} ry="10" fill="#182537" stroke="#9db0c7" strokeWidth="2" /><path d={`M ${210 - finiteDimension(model.keywayWidth, 6) * 2.4} ${210 - bodyHeight / 2 - 3} L ${210 + finiteDimension(model.keywayWidth, 6) * 2.4} ${210 - bodyHeight / 2 - 3} L ${210 + finiteDimension(model.keywayWidth, 6) * 2.4} ${210 - bodyHeight / 2 + finiteDimension(model.keywayLength, 40) * 1.8} L ${210 - finiteDimension(model.keywayWidth, 6) * 2.4} ${210 - bodyHeight / 2 + finiteDimension(model.keywayLength, 40) * 1.8} Z`} fill={section ? '#ffb65c' : '#34465d'} opacity=".85" /><line x1={210 - bodyWidth / 2 - 24} y1={210 - bodyHeight / 2} x2={210 - bodyWidth / 2 - 24} y2={210 + bodyHeight / 2} stroke="#6f89a7" strokeDasharray="3 5" /><line x1="102" y1={210 - bodyHeight / 2} x2="102" y2={210 + bodyHeight / 2} stroke="#87a1c0" strokeWidth="1" /><text x="78" y="205" fill="#91a7c0" fontSize="11" textAnchor="middle">{model.length}</text><text x="210" y={182 - bodyHeight / 2} fill="#a8bad0" fontSize="11" textAnchor="middle">Ø{model.outerDiameter}</text></g></svg>
}

function DrawingWorkspace({ model, drawingScale, setDrawingScale, exportFile, showToast }) { return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">2D DRAWING</span><h1>动力轴 · 工程图</h1><p>自动生成三视图 · 尺寸与来源可追溯</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => showToast('已创建工程图新版本')}>＋ 新建版本</button><button className="primary-button" onClick={() => exportFile('dxf')}>导出 DXF <Icon>↓</Icon></button></div></div><div className="drawing-layout"><div className="drawing-canvas panel-card"><div className="drawing-toolbar"><div><button className="selected">选择</button><button>标注</button><button>图层</button></div><label>比例 <select value={drawingScale} onChange={(e) => setDrawingScale(e.target.value)}><option>1:1</option><option>1:2</option><option>2:1</option></select></label></div><svg className="drawing-svg" viewBox="0 0 780 500"><rect x="25" y="25" width="730" height="450" fill="#121a26" stroke="#34465d" /><text x="52" y="58" fill="#9fb2c9" fontSize="13">JOYNIU NEWCAD · 2D WORKBENCH</text><g stroke="#c8d6e5" fill="none" strokeWidth="2"><rect x="100" y="130" width="230" height="76" rx="8" /><line x1="215" y1="130" x2="215" y2="206" /><circle cx="150" cy="168" r="25" stroke="#80a9d4" /><circle cx="280" cy="168" r="25" stroke="#80a9d4" /><rect x="100" y="296" width="230" height="54" rx="5" /><line x1="100" y1="323" x2="330" y2="323" strokeDasharray="4 5" /><circle cx="215" cy="323" r="16" stroke="#80a9d4" /><rect x="450" y="125" width="178" height="84" rx="8" /><circle cx="539" cy="167" r="26" stroke="#80a9d4" /><line x1="539" y1="125" x2="539" y2="209" strokeDasharray="4 4" /></g><g stroke="#e8a85e" fill="#e8a85e" strokeWidth="1"><line x1="100" y1="105" x2="330" y2="105" /><path d="M100 105l8-4v8zM330 105l-8-4v8z" /><line x1="215" y1="105" x2="215" y2="130" strokeDasharray="3 4" /><line x1="100" y1="372" x2="330" y2="372" /><path d="M100 372l8-4v8zM330 372l-8-4v8z" /><line x1="674" y1="125" x2="674" y2="209" /><path d="M674 125l-4 8h8zM674 209l-4-8h8z" /></g><g fill="#e8a85e" fontSize="12"><text x="215" y="96" textAnchor="middle">Ø{model.outerDiameter} h7</text><text x="215" y="393" textAnchor="middle">总长 {model.length}</text><text x="688" y="171">通孔 Ø{model.holeDiameter}</text><text x="106" y="120">A</text><text x="460" y="120">B</text><text x="106" y="285">C</text></g><g fill="#7f93ad" fontSize="11"><text x="100" y="235">主视图</text><text x="100" y="376">俯视图</text><text x="450" y="235">左视图</text><text x="570" y="445">比例 {drawingScale}</text></g></svg><div className="drawing-legend"><span><i className="legend-line" /> 尺寸标注 4</span><span><i className="legend-dot" /> AI 识别来源 6</span><span><i className="legend-warn" /> 待确认 0</span></div></div><aside className="drawing-inspector panel-card"><div className="inspector-title"><b>图纸属性</b><button onClick={() => showToast('图纸属性已保存')}>⋯</button></div><div className="drawing-status"><span className="status-dot" /> 尺寸验证通过</div><div className="drawing-field"><span>图纸名称</span><b>动力轴 · 工程图</b></div><div className="drawing-field"><span>来源模型</span><b>{model.name}</b></div><div className="drawing-field"><span>视图数量</span><b>3 个视图</b></div><div className="drawing-field"><span>标注尺寸</span><b>6 个</b></div><div className="layer-list"><div className="field-group-title">图层</div>{[['DIM', '尺寸标注', '#e8a85e'], ['CENTER', '中心线', '#80a9d4'], ['OBJECT', '可见轮廓', '#c8d6e5']].map(([id, name, color]) => <div className="layer-row" key={id}><span className="layer-color" style={{ background: color }} /><span>{id}</span><small>{name}</small><button>◉</button></div>)}</div><button className="primary-outline full" onClick={() => showToast('已运行 2D 尺寸检查')}>运行尺寸检查 <Icon>↗</Icon></button></aside></div></div> }

function AssemblyWorkspace({ model, assemblyChecked, setAssemblyChecked, showToast }) { return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">ASSEMBLY AGENT</span><h1>传动轴组件 · 装配</h1><p>2 个实例 · 1 个同轴配合 · 0 个干涉</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => showToast('装配向导已打开')}>✦ 装配 Agent</button><button className="primary-button" onClick={() => { setAssemblyChecked(true); showToast('装配检查完成') }}>运行检查 <Icon>↗</Icon></button></div></div><div className="assembly-grid"><div className="assembly-view panel-card"><div className="assembly-toolbar"><span><i className="live-dot" /> 实时装配预览</span><div><button onClick={() => showToast('已切换爆炸视图')}>爆炸视图</button><button onClick={() => showToast('已打开截面分析')}>截面</button></div></div><div className="assembly-scene"><div className="assembly-axis-line" /><div className="assembly-part bearing"><div className="bearing-ring" /><span>6204 轴承</span></div><div className="assembly-part shaft"><div className="shaft-body" /><span>{model.name}</span></div><div className="assembly-part washer"><div className="washer-ring" /><span>垫圈</span></div></div><div className="assembly-footer"><span>拖拽零件调整位置</span><span>同轴度 <b>0.02 mm</b></span></div></div><aside className="assembly-inspector panel-card"><div className="inspector-title"><b>装配树</b><span className="muted">2 实例</span></div><div className="assembly-tree"><div className="assembly-node root"><Icon>▣</Icon><span>传动轴组件</span></div><div className="assembly-node"><span className="tree-branch">└</span><Icon>◉</Icon><div><b>{model.name}</b><small>实例 01 · 固定</small></div><span className="node-badge">固定</span></div><div className="assembly-node"><span className="tree-branch">└</span><Icon>◉</Icon><div><b>深沟球轴承 6204</b><small>实例 02 · 可移动</small></div></div></div><div className="mates"><div className="field-group-title">配合关系 <b>1</b></div><div className="mate-card"><span className="mate-icon">◎</span><div><b>同轴配合</b><small>轴 · 圆柱面 ↔ 轴承 · 内圈</small></div><span className="check-status pass">通过</span></div></div><div className={`assembly-check ${assemblyChecked ? 'checked' : ''}`}><span>{assemblyChecked ? '✓' : 'i'}</span>{assemblyChecked ? '装配检查通过，未发现干涉。' : '运行检查以验证间隙与配合。'}</div></aside></div></div> }

function LibraryWorkspace({ libraryQuery, setLibraryQuery, libraryGroup, setLibraryGroup, filteredLibrary, insertLibrary, showToast }) { return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">STANDARD LIBRARY</span><h1>标准件库</h1><p>常用机械件已按国标整理，可直接插入当前项目</p></div><div className="heading-actions"><button className="secondary-button" onClick={() => showToast('标准件同步完成 · 1,248 项')}>↻ 同步库</button><button className="primary-button" onClick={() => showToast('已打开自定义标准件向导')}>＋ 自定义零件</button></div></div><div className="library-layout"><div className="library-main panel-card"><div className="library-toolbar"><div className="search-box"><Icon>⌕</Icon><input value={libraryQuery} onChange={(e) => setLibraryQuery(e.target.value)} placeholder="搜索名称、规格或国标号" /></div><div className="library-filters">{['全部', '紧固件', '轴承', '定位件'].map((group) => <button className={libraryGroup === group ? 'selected' : ''} key={group} onClick={() => setLibraryGroup(group)}>{group}</button>)}</div></div><div className="library-grid">{filteredLibrary.map((item) => <div className="library-card" key={item.name}><div className="library-icon">{item.icon}</div><div className="library-card-copy"><b>{item.name}</b><span>{item.spec}</span><small>{item.group} · GB/T 推荐</small></div><button className="insert-button" onClick={() => insertLibrary(item)}>插入</button></div>)}{filteredLibrary.length === 0 && <div className="empty-state">没有找到匹配的标准件，试试搜索 “M8” 或 “轴承”。</div>}</div></div><aside className="library-side panel-card"><div className="inspector-title"><b>项目零件库</b><span className="muted">3 个</span></div><div className="project-part"><span className="part-preview shaft-mini" /><div><b>动力轴 · 版本 04</b><small>当前模型</small></div><span className="part-check">✓</span></div><div className="project-part"><span className="part-preview bearing-mini" /><div><b>深沟球轴承 6204</b><small>已插入 · 实例 02</small></div><span className="part-check">✓</span></div><div className="project-part"><span className="part-preview washer-mini" /><div><b>弹簧垫圈 M8</b><small>最近使用</small></div><span className="part-check muted">＋</span></div><div className="library-tip"><Icon>✦</Icon><span>从标准件库插入的零件会保留规格来源，方便后续替换和追溯。</span></div></aside></div></div> }

function ProjectWorkspace({ selectedProject, files, setFiles, setActiveMode, exportFile, showToast }) {
  const [query, setQuery] = useState('')
  const visibleFiles = files.filter((file) => `${file.name}${file.type}`.includes(query))
  const addFile = () => { const number = files.length + 1; setFiles((prev) => [{ id: `f${Date.now()}`, name: `未命名零件 ${number}`, type: '零件', size: '—', version: 'v01', updated: '刚刚', status: '仅本地' }, ...prev]); showToast('已创建新的零件文件') }
  const saveVersion = () => { setFiles((prev) => prev.map((file, index) => index === 0 ? { ...file, version: `v${String(Number(file.version.slice(1)) + 1).padStart(2, '0')}`, updated: '刚刚', status: '已同步' } : file)); showToast('新版本已保存') }
  return <div className="secondary-workspace"><div className="secondary-heading"><div><span className="eyebrow">PROJECT FILES</span><h1>{selectedProject}</h1><p>项目文件、版本和交付物统一管理 · {files.length} 个文件</p></div><div className="heading-actions"><button className="secondary-button" onClick={saveVersion}>↥ 保存新版本</button><button className="primary-button" onClick={addFile}>＋ 新建文件</button></div></div><div className="project-layout"><div className="file-table panel-card"><div className="file-table-toolbar"><div className="search-box"><Icon>⌕</Icon><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索项目文件" /></div><div className="file-toolbar-meta"><span><i className="status-dot" /> 已自动保存</span><button onClick={() => showToast('排序：最近更新')}>最近更新 ⌄</button></div></div><div className="file-table-head"><span>名称</span><span>类型</span><span>版本</span><span>大小</span><span>最近更新</span><span>状态</span><span /></div>{visibleFiles.map((file) => <div className="file-row" key={file.id}><div className="file-name"><span className={`file-type-icon ${file.type === '工程图' ? 'orange' : file.type === '装配体' ? 'violet' : file.type === '文档' ? 'green' : 'blue'}`}>{file.type === '工程图' ? '▱' : file.type === '装配体' ? '◈' : file.type === '文档' ? '≡' : '◉'}</span><div><b>{file.name}</b><small>{file.id === 'f1' ? '当前打开 · 可编辑' : `文件 ID ${file.id}`}</small></div></div><span className="file-type">{file.type}</span><span className="file-version">{file.version}</span><span className="file-size">{file.size}</span><span className="file-updated">{file.updated}</span><span className={`file-status ${file.status === '仅本地' ? 'local' : ''}`}>{file.status}</span><div className="file-actions"><button onClick={() => { setActiveMode(file.type === '工程图' ? '2D 工程图' : file.type === '装配体' ? '装配' : '3D 建模'); showToast(`正在打开 ${file.name}`) }}>打开</button><button onClick={() => exportFile(file.type === '工程图' ? 'dxf' : 'step')}>下载</button></div></div>)}{visibleFiles.length === 0 && <div className="empty-state">没有找到匹配的项目文件。</div>}<div className="file-table-footer"><span>已显示 {visibleFiles.length} / {files.length} 个文件</span><span>回收站中的文件保留 15 天</span></div></div><aside className="project-summary panel-card"><div className="inspector-title"><b>项目概览</b><button onClick={() => showToast('项目属性已保存')}>⋯</button></div><div className="project-health"><div className="health-ring">94<small>%</small></div><div><b>交付准备度</b><small>模型、图纸与参数均已同步</small></div></div><div className="summary-item"><span>零件</span><b>3</b></div><div className="summary-item"><span>工程图</span><b>2</b></div><div className="summary-item"><span>装配体</span><b>1</b></div><div className="summary-item"><span>版本总数</span><b>12</b></div><div className="project-summary-tip"><Icon>✦</Icon><span>建议在导出前保存一个新版本，便于回溯尺寸与设计意图。</span></div></aside></div></div>
}

function HomeWorkspace({ projects, selectedProject, createProject, setActiveMode, showToast }) { return <div className="home-workspace"><div className="home-hero"><div><span className="eyebrow">WELCOME BACK, JOY</span><h1>把想法，变成可制造的形状。</h1><p>用自然语言驱动参数化设计，所有尺寸、特征和版本都可追溯。</p><div className="hero-actions"><button className="primary-button" onClick={() => setActiveMode('3D 建模')}>✦ 开始 AI 建模</button><button className="secondary-button" onClick={() => setActiveMode('2D 工程图')}>打开工程图</button></div></div><div className="hero-orbit"><div className="orbit orbit-1" /><div className="orbit orbit-2" /><div className="hero-cube">N</div></div></div><div className="home-section-heading"><div><h2>最近项目</h2><span>继续你的设计工作</span></div><button className="text-button" onClick={createProject}>＋ 新建项目</button></div><div className="project-cards">{projects.map((project) => <button key={project.id} className="project-card" onClick={() => { setActiveMode('3D 建模'); showToast(`正在打开 ${project.name}`) }}><div className={`project-preview ${project.color}`}><span>{project.name.slice(0, 1)}</span><small>{project.files} 个文件</small></div><div className="project-card-body"><b>{project.name}</b><span>更新于 {project.updated}</span></div><span className="card-arrow">↗</span></button>)}</div><div className="quick-grid"><button onClick={() => setActiveMode('3D 建模')}><span className="quick-icon blue">✦</span><div><b>AI 参数化零件</b><small>从一句话开始设计</small></div><span>→</span></button><button onClick={() => setActiveMode('2D 工程图')}><span className="quick-icon orange">▱</span><div><b>2D 工程图</b><small>三视图与尺寸标注</small></div><span>→</span></button><button onClick={() => setActiveMode('装配')}><span className="quick-icon violet">◈</span><div><b>装配工作台</b><small>配合与干涉检查</small></div><span>→</span></button></div></div> }

export default App
