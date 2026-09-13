import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as ProjectStore from './projectStore.js'
import { engineeringTabs, modeForProjectFile, primaryWorkspaceMode, standardLibraryAssemblyMode, workspaceContext, migrateWorkspaceSnapshotNavigation } from './workspaceNavigation.js'

test('opening project drawings and assemblies restores snapshot editors instead of the independent engineering library', () => {
  for (const type of ['工程图', '装配体']) {
    const mode = modeForProjectFile({ type })
    assert.equal(workspaceContext(mode), 'project')
    assert.equal(engineeringTabs[mode], undefined)
  }
  assert.equal(modeForProjectFile({ type: '装配体' }), standardLibraryAssemblyMode)
  assert.equal(modeForProjectFile({ type: '零件' }), '3D 建模')
  assert.equal(modeForProjectFile({ type: '文档' }), '项目管理')
})

test('independent engineering routes share primary navigation and never claim a project snapshot is saved', () => {
  for (const mode of Object.keys(engineeringTabs)) {
    assert.equal(primaryWorkspaceMode(mode), '工程设计')
    assert.equal(workspaceContext(mode), 'design')
  }
  for (const mode of ['原生二维', '特征编辑', '照片建模', '案例与教程']) assert.notEqual(workspaceContext(mode), 'project')
  assert.equal(workspaceContext('账号'), 'account')
  assert.equal(primaryWorkspaceMode('图纸核对'), '3D 建模')
})

test('only unversioned matching project aliases migrate while their design data stays intact', () => {
  for (const [type, oldMode, expected] of [['工程图', '2D 工程图', '基础工程图'], ['装配体', '装配', '装配草稿']]) {
    const saved = { activeMode: oldMode, assemblyItems: [{ id: 'bolt-1', position: [0, 12, 5] }], drawingPreferences: { selectedView: 'right', layers: { dimensions: false } } }
    const migrated = migrateWorkspaceSnapshotNavigation(saved, { type })
    assert.equal(migrated.activeMode, expected)
    assert.equal(saved.activeMode, oldMode)
    assert.equal(migrated.assemblyItems, saved.assemblyItems)
    assert.equal(migrated.drawingPreferences, saved.drawingPreferences)
    assert.equal(migrateWorkspaceSnapshotNavigation(migrated, { type }), migrated)
  }
})

test('v2 independent navigation and unrelated legacy modes are never remapped by project file type', () => {
  for (const type of ['工程图', '装配体', '零件', '文档']) {
    for (const activeMode of Object.keys(engineeringTabs)) {
      const snapshot = { workspaceModeVersion: 2, activeMode }
      assert.equal(migrateWorkspaceSnapshotNavigation(snapshot, { type }), snapshot)
    }
  }
  for (const [type, activeMode] of [['工程图', '装配'], ['装配体', '2D 工程图'], ['零件', '装配'], ['文档', '2D 工程图'], ['工程图', '账号'], ['装配体', '图纸转 3D']]) {
    const snapshot = { activeMode }
    assert.equal(migrateWorkspaceSnapshotNavigation(snapshot, { type }), snapshot)
  }
  assert.equal(migrateWorkspaceSnapshotNavigation({ activeMode: '装配' }, null).activeMode, '装配')
})

test('real App initialization migrates old aliases and newly saved snapshots record v2 navigation', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const initialization = app.match(/^  const initialSnapshot = .+$/m)[0]
  const initialMode = app.match(/^  const \[activeMode, setActiveMode\] = .+$/m)[0]
  const snapshotWrite = app.match(/  currentSnapshotRef.current = \{[\s\S]*?\n  \}/)[0]
  for (const [type, alias, expected] of [['工程图', '2D 工程图', '基础工程图'], ['装配体', '装配', '装配草稿']]) {
    for (const versioned of [false, true]) {
      const snapshot = { activeMode: alias, ...(versioned ? { workspaceModeVersion: 2 } : {}) }
      const state = { initialStoreRef: { current: {} }, ProjectStore: { getFileSnapshot: () => snapshot, getActiveFile: () => ({ type }) }, migrateWorkspaceSnapshotNavigation, normalizeCadWorkspaceMode: mode => mode, modeForFile: modeForProjectFile, useState: value => [value, () => {}], activeFile: { type, snapshot }, hasModel: false, model: null, currentSnapshotRef: {}, drawingJob: null, generation: null, messages: [], assemblyItems: [], prompt: '', drawingScale: '1:1', drawingPreferences: {}, view: 'isometric', section: false, zoom: 1 }
      vm.runInNewContext(`${initialization}\n${initialMode}\n${snapshotWrite}`, state)
      assert.equal(state.currentSnapshotRef.current.activeMode, versioned ? alias : expected)
      assert.equal(state.currentSnapshotRef.current.workspaceModeVersion, 2)
      assert.equal(migrateWorkspaceSnapshotNavigation(state.currentSnapshotRef.current, { type }).activeMode, versioned ? alias : expected)
    }
  }
})

test('navigation version and independent alias survive the actual project snapshot storage round trip', () => {
  const entries = new Map()
  const storage = { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value) }
  let store = ProjectStore.loadProjectStore(storage)
  const snapshot = { ...ProjectStore.getFileSnapshot(store), activeMode: '装配', workspaceModeVersion: 2 }
  store = ProjectStore.updateFileSnapshot(store, store.activeProjectId, store.activeFileId, snapshot)
  ProjectStore.persistProjectStore(store, storage)
  const restored = ProjectStore.getFileSnapshot(ProjectStore.loadProjectStore(storage))
  assert.equal(restored.workspaceModeVersion, 2)
  assert.equal(migrateWorkspaceSnapshotNavigation(restored, { type: '装配体' }).activeMode, '装配')
})
