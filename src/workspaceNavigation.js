export const engineeringTabs = { '工程设计': 'viewer', '2D 工程图': 'drawing', '装配': 'assembly' }
export const independentWorkspaces = ['特征编辑', '照片建模', '原生二维', '交付中心', ...Object.keys(engineeringTabs)]
export const projectFileModes = ['3D 建模', '图纸核对', '基础工程图', '装配草稿', '标准件库', '项目管理', '平台服务', 'CAM / NC']
export const standardLibraryAssemblyMode = '装配草稿'

// Project snapshots store the legacy drawing/assembly fields. Opening those
// files must restore their own data, rather than jump into the account library.
export function modeForProjectFile(file) {
  return { '工程图': '基础工程图', '装配体': standardLibraryAssemblyMode, '文档': '项目管理' }[file?.type] || '3D 建模'
}

export function migrateWorkspaceSnapshotNavigation(snapshot, file) {
  // A versioned snapshot records an intentional independent navigation choice.
  // Only unversioned, matching legacy project editors need this migration.
  if (Object.hasOwn(snapshot, 'workspaceModeVersion')) return snapshot
  const legacyEditor = file?.type === '工程图' && snapshot.activeMode === '2D 工程图'
    || file?.type === '装配体' && snapshot.activeMode === '装配'
  return legacyEditor ? { ...snapshot, activeMode: modeForProjectFile(file) } : snapshot
}

export function primaryWorkspaceMode(mode) {
  if (Object.hasOwn(engineeringTabs, mode)) return '工程设计'
  return mode === '图纸核对' ? '3D 建模' : mode
}

export function workspaceContext(mode) {
  if (projectFileModes.includes(mode)) return 'project'
  if (independentWorkspaces.includes(mode) || mode === '案例与教程') return 'design'
  return mode === '首页' ? 'home' : 'account'
}
