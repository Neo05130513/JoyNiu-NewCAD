import { validateModelParameters } from './modelValidation.js'
import { createClientId } from './clientId.js'
import { cadGenerationFromResult } from './cadAgentState.js'

export const PROJECT_STORE_KEY = 'joyniu-workspace-v1'
export const FILE_TYPES = ['零件', '工程图', '装配体', '文档']
export const PROJECT_BACKUP_FORMAT = 'joyniu-project-backup'
export const MAX_BACKUP_BYTES = 20 * 1024 * 1024

// An optional starter template. Blank projects never select it implicitly.
export const DEFAULT_PROJECT_MODEL = {
  name: '新建零件', kind: 'shaft', type: '零件', outerDiameter: 24, length: 70,
  holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40,
  material: '45# 钢', updatedAt: '刚刚',
}

const timestamp = () => new Date().toISOString()
const id = (prefix) => `${prefix}_${createClientId()}`
const clone = (value) => JSON.parse(JSON.stringify(value))
const cleanName = (name, fallback) => String(name || '').trim().slice(0, 120) || fallback
const blockedKey = /^(?:access_?token|refresh_?token|session_?token|auth_?token|login_?token|authorization|password|api_?key|secret|previewUrl|previewURL|objectUrl|controller|abortController|platform|imageBase64|contentBase64|fileBase64)$/i
const artifactTokenParents = new Set(['model', 'artifact', 'artifacts', 'generation', 'sourceFiles', 'sourceDocuments', 'agentRun'])

function serializable(value, seen = new WeakSet(), key = '', parent = '') {
  if (blockedKey.test(key) || (key.toLowerCase() === 'token' && !artifactTokenParents.has(parent)) || typeof value === 'function' || typeof value === 'undefined') return undefined
  if (typeof value === 'string') return /^(?:blob:|data:)/i.test(value) ? undefined : value
  if (value === null || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'object') return undefined
  if ((typeof Blob !== 'undefined' && value instanceof Blob) || seen.has(value)) return undefined
  if (value instanceof Date) return value.toISOString()
  seen.add(value)
  const result = Array.isArray(value)
    ? value.map((item) => serializable(item, seen, key, parent)).filter((item) => item !== undefined)
    : Object.fromEntries(Object.entries(value).map(([field, item]) => [field, serializable(item, seen, field, key)]).filter(([, item]) => item !== undefined))
  seen.delete(value)
  return result
}

/** A durable snapshot contains data only; browser file bytes and credentials never belong to a project. */
export function sanitizeWorkspaceSnapshot(snapshot = {}) {
  snapshot = snapshot && typeof snapshot === 'object' && !Array.isArray(snapshot) ? snapshot : {}
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
  const recoverableTask = Boolean((job.cadTask?.runId || job.cadTask?.requestId) && ['submitted', 'queued', 'running', 'cancel_requested'].includes(job.cadTask.status))
  const durableAgentRun = result.model?.kind === 'feature_model' && result.model.agentRun?.runId
  const interrupted = !recoverableTask && (['queued', 'analyzing', 'generating'].includes(job.status) || (job.status === 'error' && job.interrupted === true))
  // Older clients discarded real GLBs when only the independent review failed.
  // Recover that narrow preview case; never replace an existing/stale generation.
  if (!result.generation && durableAgentRun && !recoverableTask && !interrupted && !result.model.stale && !result.model.agentRun.dirty && !result.model.agentRun.stale) {
    const preview = cadGenerationFromResult(result.model.agentRun, result.model.cadPlan)
    if (preview?.reviewIncomplete) result.generation = preview
  }
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
  const project = { id: projectId, name: cleanName(name, `新建项目 ${store.projects.length + 1}`), color: 'blue', createdAt: now, updatedAt: now, files: [file], lastActiveFileId: file.id }
  const projects = store.projects.map((item) => item.id === store.activeProjectId ? { ...item, lastActiveFileId: store.activeFileId } : item)
  return { ...store, projects: [...projects, project], activeProjectId: project.id, activeFileId: file.id }
}

export function createProjectFile(store, projectId, options = {}) {
  if (!store.projects.some((project) => project.id === projectId)) return store
  const file = makeFile(projectId, options)
  return { ...store, activeProjectId: projectId, activeFileId: file.id, projects: store.projects.map((project) => project.id !== projectId
    ? project.id === store.activeProjectId ? { ...project, lastActiveFileId: store.activeFileId } : project
    : { ...project, updatedAt: timestamp(), files: [...project.files, file], lastActiveFileId: file.id }) }
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

function availableName(name, entries, suffix = '副本') {
  const base = cleanName(name, '未命名')
  const names = new Set(entries.map((item) => item.name))
  let number = 1, candidate
  do {
    const ending = `（${suffix}${number > 1 ? ` ${number}` : ''}）`
    candidate = `${base.slice(0, 120 - ending.length)}${ending}`
    number += 1
  } while (names.has(candidate))
  return candidate
}

/** Copies the working draft. Its future edits and version history are independent. */
export function duplicateProjectFile(store, projectId, fileId, { name } = {}) {
  const file = getProjectFile(store, projectId, fileId)
  if (!file || file.contentUnavailable) return store
  const project = store.projects.find((item) => item.id === projectId)
  const nextName = cleanName(name, availableName(file.name, project.files))
  const snapshot = recoverWorkspaceSnapshot(file.snapshot)
  if (file.type === '零件' && snapshot.model) snapshot.model.name = nextName
  return createProjectFile(store, projectId, { name: nextName, type: file.type, snapshot })
}

const firstAvailableFile = (project) => project?.files.find((file) => !file.contentUnavailable)?.id || null

export function deleteProjectFile(store, projectId, fileId) {
  const file = getProjectFile(store, projectId, fileId)
  const project = store.projects.find((item) => item.id === projectId)
  if (!file || !project) return store
  const files = project.files.filter((item) => item.id !== fileId)
  const nextFileId = firstAvailableFile({ files })
  const removed = { id: id('trash'), kind: 'file', projectId, projectName: project.name, deletedAt: timestamp(), item: clone(file) }
  return { ...store, trash: [...(store.trash || []), removed],
    activeFileId: store.activeProjectId === projectId && store.activeFileId === fileId ? nextFileId : store.activeFileId,
    projects: store.projects.map((item) => item.id !== projectId ? item : { ...item, files, updatedAt: timestamp(),
      lastActiveFileId: item.lastActiveFileId === fileId ? nextFileId : item.lastActiveFileId }),
  }
}

export function deleteProject(store, projectId) {
  const project = store.projects.find((item) => item.id === projectId)
  if (!project) return store
  const removed = { id: id('trash'), kind: 'project', deletedAt: timestamp(), item: clone(project) }
  let next = { ...store, projects: store.projects.filter((item) => item.id !== projectId), trash: [...(store.trash || []), removed] }
  if (!next.projects.length) return createProject(next, { name: '新建项目' })
  if (store.activeProjectId === projectId) {
    const active = next.projects[0]
    next = { ...next, activeProjectId: active.id, activeFileId: firstAvailableFile(active) }
  }
  return next
}

export function restoreTrashedItem(store, trashId) {
  const record = store.trash?.find((item) => item.id === trashId)
  if (!record) return store
  const next = { ...store, trash: store.trash.filter((item) => item.id !== trashId) }
  if (record.kind === 'project') {
    let project = clone(record.item)
    const existing = next.projects.find((item) => item.id === project.id)
    if (existing) {
      // A child may already have been restored into its original project.
      // Keep those working drafts when the remaining project is restored.
      const files = [...existing.files]
      for (const saved of project.files) {
        const file = { ...saved }
        if (files.some((item) => item.id === file.id)) file.id = id('file')
        if (files.some((item) => item.name === file.name)) {
          file.name = availableName(file.name, files, '恢复')
          if (file.type === '零件' && file.snapshot.model) file.snapshot.model.name = file.name
        }
        files.push(file)
      }
      project = { ...project, ...existing, files, updatedAt: timestamp() }
    } else if (next.projects.some((item) => item.name === project.name)) project.name = availableName(project.name, next.projects, '恢复')
    return { ...next, projects: existing ? next.projects.map((item) => item.id === project.id ? project : item) : [...next.projects, project], activeProjectId: project.id,
      activeFileId: project.files.some((file) => file.id === project.lastActiveFileId && !file.contentUnavailable) ? project.lastActiveFileId : firstAvailableFile(project) }
  }
  let project = next.projects.find((item) => item.id === record.projectId)
  if (!project) {
    const now = timestamp()
    const name = cleanName(record.projectName, '恢复的项目')
    project = { id: record.projectId || record.item.projectId || id('project'),
      name: next.projects.some((item) => item.name === name) ? availableName(name, next.projects, '恢复') : name,
      color: 'blue', createdAt: now, updatedAt: now, files: [] }
    next.projects = [...next.projects, project]
  }
  const file = { ...clone(record.item), projectId: project.id, lastOpenedAt: timestamp() }
  if (project.files.some((item) => item.name === file.name)) {
    file.name = availableName(file.name, project.files, '恢复')
    if (file.type === '零件' && file.snapshot.model) file.snapshot.model.name = file.name
  }
  return { ...next, activeProjectId: project.id, activeFileId: file.contentUnavailable ? firstAvailableFile(project) : file.id,
    projects: next.projects.map((item) => item.id !== project.id ? item : { ...item, files: [...item.files, file], updatedAt: timestamp(), lastActiveFileId: file.id }) }
}

const persistedFile = (file) => ({ ...serializable(file), snapshot: sanitizeWorkspaceSnapshot(file.snapshot),
  versions: (file.versions || []).map((version) => ({ ...version, snapshot: sanitizeWorkspaceSnapshot(version.snapshot) })) })
const persistedProject = (project) => ({ ...serializable(project), files: project.files.map(persistedFile) })
const persistedTrash = (records = []) => records.map((record) => ({ ...record,
  item: record.kind === 'project' ? persistedProject(record.item) : persistedFile(record.item) }))

/** Browser backup data includes drafts, documents and history; binary artifacts stay on the CAD service. */
export function exportProjectBackup(store, projectId) {
  const projects = projectId ? store.projects.filter((project) => project.id === projectId) : store.projects
  if (!projects.length) throw new Error('没有可备份的项目。')
  return { format: PROJECT_BACKUP_FORMAT, schemaVersion: 1, exportedAt: timestamp(),
    projects: projects.map(persistedProject), ...(projectId ? {} : { trash: persistedTrash(store.trash) }) }
}

/** A pending browser file read must not import after its account/page is gone. */
export async function importProjectBackupFile(file, onImport, { signal } = {}) {
  const checkActive = () => { if (signal?.aborted) throw signal.reason || new DOMException('导入已取消。', 'AbortError') }
  checkActive()
  if (file.size > MAX_BACKUP_BYTES) throw new Error('备份文件超过 20 MB，请分项目导出后恢复。')
  if (!/\.json$/i.test(file.name)) throw new Error('请选择本应用导出的 .json 项目备份或文件快照。')
  const text = await file.text()
  checkActive()
  let payload
  try { payload = JSON.parse(text) } catch { throw new Error('文件内容不是有效的 JSON，请重新选择备份文件。') }
  if (typeof onImport !== 'function') throw new Error('当前页面暂时无法导入，请从项目管理页重试。')
  const result = await onImport(payload)
  checkActive()
  if (result === false) throw new Error('备份尚未导入，请确认当前任务已结束后重试。')
}

export function importProjectBackup(store, input) {
  let data = input
  if (typeof input === 'string') {
    if (new TextEncoder().encode(input).length > MAX_BACKUP_BYTES) throw new Error('备份文件超过 20 MB，请分项目导出后恢复。')
    try { data = JSON.parse(input) } catch { throw new Error('备份文件不是有效的 JSON，请选择从本应用导出的文件。') }
  }
  const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
  if (!isObject(data) || data.schemaVersion !== 1) throw new Error('不支持此备份版本，请选择本应用导出的项目备份或文件快照。')
  const singleFile = !data.format && isObject(data.snapshot) && typeof data.name === 'string'
  const workspaceBackup = data.format === PROJECT_BACKUP_FORMAT || !data.format && Array.isArray(data.projects)
  if (!singleFile && (!workspaceBackup || !Array.isArray(data.projects) || !data.projects.length || data.projects.length > 200)) throw new Error('备份中没有有效的项目。')
  if (new TextEncoder().encode(JSON.stringify(data)).length > MAX_BACKUP_BYTES) throw new Error('备份文件超过 20 MB，请分项目导出后恢复。')
  let fileCount = 0, versionCount = 0
  const projectIds = new Map()
  const importedProjectId = (original) => {
    if (typeof original !== 'string' || !original) return id('project')
    if (!projectIds.has(original)) projectIds.set(original, id('project'))
    return projectIds.get(original)
  }
  const validSnapshot = (snapshot) => {
    if (!isObject(snapshot)) return false
    const model = snapshot.model
    if (model != null && (!isObject(model) || typeof model.kind !== 'string')) return false
    const plan = model?.cadPlan
    if (plan != null) {
      if (!isObject(plan)) return false
      if (plan.parameters != null && !(isObject(plan.parameters) || Array.isArray(plan.parameters) && plan.parameters.every((item) => isObject(item) && typeof (item.id || item.name || item.key) === 'string'))) return false
      if (plan.features != null && (!Array.isArray(plan.features) || !plan.features.every((item) => isObject(item) && typeof item.id === 'string' && typeof item.op === 'string'))) return false
    }
    const run = model?.agentRun
    if (run != null && !isObject(run)) return false
    const objectLists = [[snapshot, 'messages'], [snapshot, 'assemblyItems'], [run, 'artifacts'], [run, 'sourceDocuments'],
      [run?.sourceTranscription, 'annotations'], [snapshot.generation, 'artifacts']]
    if (objectLists.some(([container, key]) => container?.[key] != null && (!Array.isArray(container[key]) || !container[key].every(isObject)))) return false
    return true
  }
  const readFile = (value, projectId) => {
    if (!isObject(value) || typeof value.name !== 'string' || !FILE_TYPES.includes(value.type) || !validSnapshot(value.snapshot)
      || ++fileCount > 5000
      || (value.versions != null && !Array.isArray(value.versions))) throw new Error('备份中的文件记录不完整，未导入任何内容。')
    const file = makeFile(projectId, { name: value.name, type: value.type, snapshot: recoverWorkspaceSnapshot(value.snapshot) })
    const versions = (value.versions || []).map((version, index) => {
      if (!isObject(version) || !validSnapshot(version.snapshot) || ++versionCount > 10000) throw new Error('备份中的版本记录不完整，未导入任何内容。')
      const number = Number.isInteger(version.number) && version.number > 0 ? version.number : index + 1
      return { id: id('version'), number, label: `v${String(number).padStart(2, '0')}`, note: String(version.note || '').slice(0, 500),
        createdAt: typeof version.createdAt === 'string' ? version.createdAt : timestamp(), snapshot: sanitizeWorkspaceSnapshot(version.snapshot) }
    })
    return { ...file, versions, ...(value.contentUnavailable ? { contentUnavailable: true, contentUnavailableReason: '备份仅包含旧版文件名，内容不可用。' } : {}) }
  }
  const readProject = (value) => {
    if (!isObject(value) || typeof value.name !== 'string' || !Array.isArray(value.files)) throw new Error('备份中的项目记录不完整，未导入任何内容。')
    const now = timestamp(), projectId = importedProjectId(value.id)
    return { id: projectId, name: cleanName(value.name, '导入的项目'), color: 'blue', createdAt: now, updatedAt: now,
      files: value.files.map((file) => readFile(file, projectId)) }
  }
  const imported = (singleFile ? [{ name: `${data.name} · 导入`, files: [{ ...data, versions: [] }] }] : data.projects).map(readProject)
  if (new Set(imported.map((project) => project.id)).size !== imported.length) throw new Error('备份中的项目标识重复，未导入任何内容。')
  const projects = [...store.projects]
  for (const project of imported) {
    if (projects.some((item) => item.name === project.name)) project.name = availableName(project.name, projects, '导入')
    projects.push(project)
  }
  const trash = [...(store.trash || [])]
  if (!singleFile && data.trash != null) {
    if (!Array.isArray(data.trash) || data.trash.length > 5000) throw new Error('备份中的回收站记录不完整，未导入任何内容。')
    const deletedProjects = new Map()
    for (const record of data.trash) {
      if (!isObject(record) || !['project', 'file'].includes(record.kind)) throw new Error('备份中的回收站记录不完整，未导入任何内容。')
      if (record.kind === 'project') deletedProjects.set(record, readProject(record.item))
    }
    for (const record of data.trash) {
      const item = record.kind === 'project' ? deletedProjects.get(record) : readFile(record.item, importedProjectId(record.projectId || record.item?.projectId))
      trash.push({ id: id('trash'), kind: record.kind, item, projectId: item.projectId,
        projectName: cleanName(record.projectName, '恢复的项目'), deletedAt: typeof record.deletedAt === 'string' ? record.deletedAt : timestamp() })
    }
  }
  return { ...store, projects, trash, activeProjectId: imported[0].id, activeFileId: firstAvailableFile(imported[0]) }
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
    if (!active) return createProject({ ...store, activeProjectId: null, activeFileId: null }, { model: defaultModel })
    const available = (fileId) => active.files.some((file) => file.id === fileId && !file.contentUnavailable)
    return { ...store, activeProjectId: active.id, activeFileId: available(store.activeFileId) ? store.activeFileId
      : available(active.lastActiveFileId) ? active.lastActiveFileId : firstAvailableFile(active) }
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
  const persisted = { ...store, projects: store.projects.map(persistedProject), ...(store.trash ? { trash: persistedTrash(store.trash) } : {}) }
  storage.setItem(PROJECT_STORE_KEY, JSON.stringify(persisted))
  return persisted
}

export function projectStatistics(project) {
  const files = project?.files || []
  return { files: files.length, versions: files.reduce((sum, file) => sum + file.versions.length, 0), types: Object.fromEntries(FILE_TYPES.map((type) => [type, files.filter((file) => file.type === type).length])) }
}
