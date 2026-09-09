import { validateModelParameters } from './modelValidation.js'

export const PROJECT_STORE_KEY = 'joyniu-workspace-v1'
export const FILE_TYPES = ['零件', '工程图', '装配体', '文档']

// An optional starter template. Blank projects never select it implicitly.
export const DEFAULT_PROJECT_MODEL = {
  name: '新建零件', kind: 'shaft', type: '零件', outerDiameter: 24, length: 70,
  holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40,
  material: '45# 钢', updatedAt: '刚刚',
}

const timestamp = () => new Date().toISOString()
const id = (prefix) => `${prefix}_${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`}`
const clone = (value) => JSON.parse(JSON.stringify(value))
const cleanName = (name, fallback) => String(name || '').trim().slice(0, 120) || fallback
const blockedKey = /^(?:token|access_?token|refresh_?token|authorization|password|api_?key|secret|previewUrl|previewURL|objectUrl|controller|abortController|platform|imageBase64|contentBase64|fileBase64)$/i

function serializable(value, seen = new WeakSet(), key = '') {
  if (blockedKey.test(key) || typeof value === 'function' || typeof value === 'undefined') return undefined
  if (typeof value === 'string') return /^(?:blob:|data:)/i.test(value) ? undefined : value
  if (value === null || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'object') return undefined
  if ((typeof Blob !== 'undefined' && value instanceof Blob) || seen.has(value)) return undefined
  if (value instanceof Date) return value.toISOString()
  seen.add(value)
  const result = Array.isArray(value)
    ? value.map((item) => serializable(item, seen)).filter((item) => item !== undefined)
    : Object.fromEntries(Object.entries(value).map(([field, item]) => [field, serializable(item, seen, field)]).filter(([, item]) => item !== undefined))
  seen.delete(value)
  return result
}

/** A durable snapshot contains data only; browser file bytes and credentials never belong to a project. */
export function sanitizeWorkspaceSnapshot(snapshot = {}) {
  const result = serializable(snapshot) || {}
  result.model = result.model ?? null
  const sourceFile = snapshot.drawingJob?.file
  result.drawingJob = { status: 'idle', evidence: null, ...(result.drawingJob || {}), file: null, previewUrl: '' }
  if (sourceFile?.name) result.drawingJob.fileMeta = { name: sourceFile.name, size: sourceFile.size, type: sourceFile.type, lastModified: sourceFile.lastModified }
  result.chatAttachments = []
  result.messages = Array.isArray(result.messages) ? result.messages : []
  result.assemblyItems = Array.isArray(result.assemblyItems) ? result.assemblyItems : []
  result.prompt = typeof result.prompt === 'string' ? result.prompt : ''
  return result
}

/** Restoring never pretends that a network request or a File survived page reload. */
export function recoverWorkspaceSnapshot(snapshot = {}) {
  const result = sanitizeWorkspaceSnapshot(snapshot)
  const job = result.drawingJob
  const recoverableTask = Boolean(job.cadTask?.runId && job.cadTask.status === 'running')
  const durableAgentRun = result.model?.kind === 'feature_model' && result.model.agentRun?.runId
  const interrupted = !recoverableTask && (['queued', 'analyzing', 'generating'].includes(job.status) || (job.status === 'error' && job.interrupted === true))
  const sourceFileUnavailable = Boolean(job.fileMeta?.name)
  const hasCompletedGeneration = Boolean(result.generation && !result.generation.pendingDrawing && !result.generation.stale)
  // A completed parameter edit can retain the drawing's filename as provenance.
  // Building those saved dimensions does not require reading the drawing again.
  // Only idle, valid drafts qualify; an interrupted upload may still carry an
  // unrelated old model and must continue to require its original source.
  const hasBuildableParameterDraft = job.status === 'idle' && !job.interrupted && !job.evidence
    && result.model?.kind !== 'feature_model' && validateModelParameters(result.model).valid
  const needsNewAnalysis = sourceFileUnavailable && !job.evidence && !hasCompletedGeneration
    && !hasBuildableParameterDraft && job.status !== 'generated'
  // Saved evidence and generated solids remain usable without the original
  // file. Only a missing analysis or interrupted request needs re-upload.
  job.sourceFileUnavailable = sourceFileUnavailable
  job.requiresFileReselection = !recoverableTask && !durableAgentRun && (interrupted || needsNewAnalysis)
  if (!interrupted) job.interrupted = false
  if (interrupted) {
    result.drawingJob.status = 'error'
    result.drawingJob.error = durableAgentRun ? '上次处理已中断；项目与原图记录已保存，可以继续发送要求。' : '上次处理已中断；浏览器只保留文件信息，请重新选择原图后继续。'
    result.drawingJob.requiresFileReselection = !durableAgentRun
    result.drawingJob.interrupted = true
    if (result.generation) result.generation = { ...result.generation, stale: true, pendingDrawing: false }
  }
  if (result.aiConversation) result.aiConversation = { ...result.aiConversation, previousResponseId: '', turnStatus: 'idle', activeTurnId: '', statusMessage: '', cadProgress: null }
  result.messages = result.messages.map((message) => ['streaming', 'pending', 'sending'].includes(message.status)
    ? { ...message, status: 'interrupted', text: message.text || '上次回复已中断，请重新发送。' }
    : message)
  result.isGenerating = false
  result.isAccepting = false
  return result
}

export function createWorkspaceSnapshot({ name = '新建零件', type = '零件', model = null, snapshot } = {}) {
  const initial = snapshot ? sanitizeWorkspaceSnapshot(snapshot) : {
    model: type === '文档' || model == null ? null : { ...clone(model), name },
    drawingJob: { status: 'idle', evidence: null }, generation: null, messages: [], assemblyItems: [], prompt: '',
  }
  if (type === '文档') initial.documentText = typeof initial.documentText === 'string' ? initial.documentText : ''
  return sanitizeWorkspaceSnapshot(initial)
}

function makeFile(projectId, { name, type = '零件', model, snapshot } = {}) {
  const fileType = FILE_TYPES.includes(type) ? type : '零件'
  const fileName = cleanName(name, `未命名${fileType}`)
  const now = timestamp()
  return { id: id('file'), projectId, name: fileName, type: fileType, createdAt: now, updatedAt: now, lastOpenedAt: now,
    snapshot: createWorkspaceSnapshot({ name: fileName, type: fileType, model, snapshot }), versions: [] }
}

export function getActiveProject(store) { return store?.projects?.find((project) => project.id === store.activeProjectId) || null }
export function getActiveFile(store) { return getActiveProject(store)?.files.find((file) => file.id === store.activeFileId) || null }
export function getProjectFile(store, projectId, fileId) { return store?.projects?.find((project) => project.id === projectId)?.files.find((file) => file.id === fileId) || null }
export function getFileSnapshot(store, projectId = store.activeProjectId, fileId = store.activeFileId) {
  const file = getProjectFile(store, projectId, fileId)
  return file && !file.contentUnavailable ? recoverWorkspaceSnapshot(file.snapshot) : null
}

function updateFile(store, projectId, fileId, update) {
  let found = false
  const projects = store.projects.map((project) => project.id !== projectId ? project : {
    ...project, updatedAt: timestamp(), files: project.files.map((file) => {
      if (file.id !== fileId) return file
      found = true
      return update(file)
    }),
  })
  return found ? { ...store, projects } : store
}

export function createProject(store, { name, model = null, snapshot } = {}) {
  const projectId = id('project')
  const now = timestamp()
  const file = makeFile(projectId, { name: '零件 01', type: '零件', model, snapshot })
  const project = { id: projectId, name: cleanName(name, `新建项目 ${store.projects.length + 1}`), color: 'blue', createdAt: now, updatedAt: now, files: [file] }
  return { ...store, projects: [...store.projects, project], activeProjectId: project.id, activeFileId: file.id }
}

export function createProjectFile(store, projectId, options = {}) {
  if (!store.projects.some((project) => project.id === projectId)) return store
  const file = makeFile(projectId, options)
  return { ...store, activeProjectId: projectId, activeFileId: file.id, projects: store.projects.map((project) => project.id !== projectId ? project : { ...project, updatedAt: timestamp(), files: [...project.files, file] }) }
}

export function renameProject(store, projectId, name) {
  const nextName = String(name || '').trim()
  if (!nextName) return store
  return { ...store, projects: store.projects.map((project) => project.id === projectId ? { ...project, name: cleanName(nextName, project.name), updatedAt: timestamp() } : project) }
}

export function renameProjectFile(store, projectId, fileId, name) {
  if (!String(name || '').trim()) return store
  return updateFile(store, projectId, fileId, (file) => {
    const nextName = cleanName(name, file.name)
    const snapshot = file.type === '零件' && file.snapshot.model ? { ...file.snapshot, model: { ...file.snapshot.model, name: nextName } } : file.snapshot
    return { ...file, name: nextName, snapshot, updatedAt: timestamp() }
  })
}

export function selectProject(store, projectId) {
  const project = store.projects.find((item) => item.id === projectId)
  if (!project) return store
  const nextFileId = project.id === store.activeProjectId && project.files.some((file) => file.id === store.activeFileId) ? store.activeFileId
    : project.files.some((file) => file.id === project.lastActiveFileId) ? project.lastActiveFileId : project.files.find((file) => !file.contentUnavailable)?.id || project.files[0]?.id || null
  return { ...store, activeProjectId: projectId, activeFileId: nextFileId, projects: store.projects.map((item) => ({
    ...item, ...(item.id === store.activeProjectId ? { lastActiveFileId: store.activeFileId } : {}),
    files: item.id === projectId ? item.files.map((file) => file.id === nextFileId ? { ...file, lastOpenedAt: timestamp() } : file) : item.files,
  })) }
}

export function selectProjectFile(store, projectId, fileId) {
  const file = getProjectFile(store, projectId, fileId)
  if (!file || file.contentUnavailable) return store
  return { ...store, activeProjectId: projectId, activeFileId: fileId, projects: store.projects.map((project) => ({ ...project,
    ...(project.id === store.activeProjectId ? { lastActiveFileId: store.activeFileId } : {}),
    ...(project.id === projectId ? { lastActiveFileId: fileId, files: project.files.map((item) => item.id === fileId ? { ...item, lastOpenedAt: timestamp() } : item) } : {}),
  })) }
}

export function updateFileSnapshot(store, projectId, fileId, snapshot) {
  const clean = sanitizeWorkspaceSnapshot(snapshot)
  const file = getProjectFile(store, projectId, fileId)
  if (file?.contentUnavailable) return store
  if (file && JSON.stringify(file.snapshot) === JSON.stringify(clean)) return store
  return updateFile(store, projectId, fileId, (current) => ({ ...current, snapshot: clean, updatedAt: timestamp() }))
}

export function saveFileVersion(store, projectId, fileId, { snapshot, note = '' } = {}) {
  if (getProjectFile(store, projectId, fileId)?.contentUnavailable) return store
  return updateFile(store, projectId, fileId, (file) => {
    const saved = sanitizeWorkspaceSnapshot(snapshot ?? file.snapshot)
    const number = Math.max(0, ...file.versions.map((version) => version.number || 0)) + 1
    const now = timestamp()
    const version = { id: id('version'), number, label: `v${String(number).padStart(2, '0')}`, note: String(note).trim().slice(0, 500), createdAt: now, snapshot: clone(saved) }
    return { ...file, updatedAt: now, snapshot: clone(saved), versions: [...file.versions, version] }
  })
}

export function saveBeforeDrawingReplacement(store, projectId, fileId, snapshot) {
  const file = getProjectFile(store, projectId, fileId)
  const saved = sanitizeWorkspaceSnapshot(snapshot ?? file?.snapshot)
  if (!file || file.contentUnavailable || !saved.model?.kind) return store
  const identity = (value) => JSON.stringify({ model: value?.model, generation: value?.generation })
  if (file.versions.some((version) => identity(version.snapshot) === identity(saved))) return store
  return saveFileVersion(store, projectId, fileId, { snapshot: saved, note: '更换图纸前自动保留；可恢复此版本继续设计' })
}

export function restoreFileVersion(store, projectId, fileId, versionId) {
  const version = getProjectFile(store, projectId, fileId)?.versions.find((item) => item.id === versionId)
  if (!version) return store
  return updateFile(store, projectId, fileId, (file) => ({ ...file, snapshot: recoverWorkspaceSnapshot(version.snapshot), updatedAt: timestamp(), restoredFromVersionId: version.id }))
}

function readJson(storage, key, fallback = null) {
  try { const raw = storage?.getItem(key); return raw ? JSON.parse(raw) : fallback } catch { return fallback }
}
function readText(storage, key, fallback = '') { try { return storage?.getItem(key) || fallback } catch { return fallback } }

export function loadProjectStore(storage = globalThis.localStorage, { defaultModel = null, defaultProjects = [], defaultFiles = [] } = {}) {
  const persisted = readJson(storage, PROJECT_STORE_KEY)
  if (persisted?.schemaVersion === 1 && Array.isArray(persisted.projects) && persisted.projects.every((project) => project?.id && Array.isArray(project.files))) {
    const projects = persisted.projects.map((project) => ({ ...project, files: project.files.filter((file) => file?.id).map((file) => ({
      ...file, projectId: project.id, snapshot: recoverWorkspaceSnapshot(file.snapshot),
      versions: (Array.isArray(file.versions) ? file.versions : []).map((version) => ({ ...version, snapshot: sanitizeWorkspaceSnapshot(version.snapshot) })),
    })) }))
    const store = { ...persisted, projects }
    const active = getActiveProject(store) || projects[0]
    if (!active) return createProject({ schemaVersion: 1, projects: [], activeProjectId: null, activeFileId: null }, { model: defaultModel })
    return { ...store, activeProjectId: active.id, activeFileId: active.files.some((file) => file.id === store.activeFileId) ? store.activeFileId : active.files[0]?.id || null }
  }

  const hasLegacy = ['joyniu-model', 'joyniu-projects', 'joyniu-files', 'joyniu-drawing-session', 'joyniu-generation'].some((key) => Boolean(readText(storage, key)))
  if (!hasLegacy) return createProject({ schemaVersion: 1, projects: [], activeProjectId: null, activeFileId: null }, { name: '我的第一个项目', model: defaultModel })
  const legacyProjects = readJson(storage, 'joyniu-projects', defaultProjects)
  const selectedName = readText(storage, 'joyniu-selected-project', legacyProjects?.[0]?.name || '我的第一个项目')
  const now = timestamp()
  const projects = (Array.isArray(legacyProjects) ? legacyProjects : []).filter((project) => project?.name).map((project) => ({ id: project.id || id('project'), name: project.name, color: project.color || 'blue', createdAt: now, updatedAt: now, files: [] }))
  let activeProject = projects.find((project) => project.name === selectedName)
  if (!activeProject) { activeProject = { id: id('project'), name: selectedName, color: 'blue', createdAt: now, updatedAt: now, files: [] }; projects.push(activeProject) }
  const legacyModel = readJson(storage, 'joyniu-model')
  const legacySnapshot = {
    model: legacyModel, drawingJob: readJson(storage, 'joyniu-drawing-session', { status: 'idle', evidence: null }),
    generation: readJson(storage, 'joyniu-generation'), messages: readJson(storage, 'joyniu-messages', []), assemblyItems: [], prompt: '',
  }
  const legacyFiles = readJson(storage, 'joyniu-files', defaultFiles)
  const sources = Array.isArray(legacyFiles) && legacyFiles.length ? legacyFiles : [{ name: legacySnapshot.model?.name || '零件 01', type: '零件' }]
  let legacyActiveIndex = sources.findIndex((file) => file.id === 'f1' && file.type === '零件')
  if (legacyActiveIndex < 0) legacyActiveIndex = sources.findIndex((file) => file.type === '零件')
  activeProject.files = sources.map((file, index) => {
    const hasContent = Boolean(legacyModel && index === legacyActiveIndex)
    return { ...makeFile(activeProject.id, { name: file.name, type: file.type, snapshot: hasContent ? recoverWorkspaceSnapshot(legacySnapshot) : { model: null } }),
      ...(!hasContent ? { contentUnavailable: true, contentUnavailableReason: '旧版仅记录文件名，未保存此文件内容。请新建独立内容。' } : {}),
    }
  })
  if (!activeProject.files.some((file) => !file.contentUnavailable)) activeProject.files.push(makeFile(activeProject.id, { name: legacyModel?.name || '零件 01', snapshot: legacyModel ? recoverWorkspaceSnapshot(legacySnapshot) : undefined }))
  return { schemaVersion: 1, projects, activeProjectId: activeProject.id, activeFileId: activeProject.files.find((file) => !file.contentUnavailable)?.id || null, migratedFromLegacy: true }
}

/** Throws storage errors so the UI can report a failed local save instead of claiming success. */
export function persistProjectStore(store, storage = globalThis.localStorage) {
  const persisted = { ...store, projects: store.projects.map((project) => ({ ...project, files: project.files.map((file) => ({
    ...file, snapshot: sanitizeWorkspaceSnapshot(file.snapshot), versions: file.versions.map((version) => ({ ...version, snapshot: sanitizeWorkspaceSnapshot(version.snapshot) })),
  })) })) }
  storage.setItem(PROJECT_STORE_KEY, JSON.stringify(persisted))
  return persisted
}

export function projectStatistics(project) {
  const files = project?.files || []
  return { files: files.length, versions: files.reduce((sum, file) => sum + file.versions.length, 0), types: Object.fromEntries(FILE_TYPES.map((type) => [type, files.filter((file) => file.type === type).length])) }
}
