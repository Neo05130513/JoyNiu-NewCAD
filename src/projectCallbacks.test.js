import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import * as ProjectStore from './projectStore.js'

const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const repository = fileURLToPath(new URL('../', import.meta.url))
const python = fileURLToPath(new URL('../apps/api/.venv/bin/python', import.meta.url))
const emptyStorage = { getItem: () => null }
const fresh = () => ProjectStore.loadProjectStore(emptyStorage)

// Compile the actual callbacks, with only React state and browser I/O replaced.
function callback(name) {
  const start = app.indexOf(`  const ${name} = `)
  const end = app.indexOf('\n  const ', start + 1)
  assert.ok(start >= 0 && end > start, `App callback ${name} must exist`)
  return app.slice(start, end)
}

function workbench(store = fresh()) {
  const downloads = [], toasts = [], restored = []
  const context = vm.createContext({
    ProjectStore, JSON, Date,
    storeRef: { current: store },
    currentSnapshotRef: { current: ProjectStore.getFileSnapshot(store) },
    drawingJobRef: { current: {} }, chatAttachmentsRef: { current: [] },
    transientFilesRef: { current: new Map() },
    chatAbortRef: { current: null }, cadConfirmRef: { current: null },
    isGenerating: false, isAccepting: false, platform: { busy: false },
    model: ProjectStore.getFileSnapshot(store).model,
    setWorkspaceStore: () => {},
    setModel: (update) => {
      context.model = typeof update === 'function' ? update(context.model) : update
      // The next render places the actual model state into the snapshot ref.
      context.currentSnapshotRef.current = { ...context.currentSnapshotRef.current, model: context.model }
    },
    restoreWorkspace: (next, mode) => {
      context.storeRef.current = next
      context.currentSnapshotRef.current = ProjectStore.getFileSnapshot(next)
      context.model = context.currentSnapshotRef.current?.model ?? null
      restored.push({ next, mode })
    },
    downloadBlob: (text, name) => downloads.push({ text, name }),
    showToast: (text, type) => toasts.push({ text, type }),
  })
  const names = ['commitStore', 'flushWorkspace', 'canSwitch', 'renameLocalFile', 'exportBackup', 'importBackup', 'restoreLocalTrash']
  vm.runInContext(`${names.map(callback).join('\n')}\nObject.assign(globalThis, { ${names.join(', ')} })`, context)
  return { context, downloads, toasts, restored }
}

test('real App blank-file rename exports a backup that can be imported again without inventing a model', () => {
  const { context, downloads, restored } = workbench()
  const before = context.storeRef.current
  assert.equal(context.renameLocalFile(before.activeFileId, '客户空白零件', before.activeProjectId), true)
  assert.equal(context.model, null)
  context.exportBackup()
  assert.equal(downloads.length, 1)
  assert.equal(downloads[0].name, 'JoyNiu-项目备份.json')
  const backup = JSON.parse(downloads[0].text)
  assert.equal(backup.projects[0].files[0].name, '客户空白零件')
  assert.equal(backup.projects[0].files[0].snapshot.model, null)
  assert.equal(context.importBackup(backup), true)
  assert.equal(restored.length, 1)
  const imported = ProjectStore.getActiveFile(context.storeRef.current)
  assert.equal(imported.name, '客户空白零件')
  assert.equal(imported.snapshot.model, null)
  assert.notEqual(imported.id, before.activeFileId)
  assert.equal(context.storeRef.current.projects.length, 2)
})

test('real App canSwitch refusal returns false and never restores trash or reports success', () => {
  let store = fresh()
  store = ProjectStore.deleteProjectFile(store, store.activeProjectId, store.activeFileId)
  // Give the remaining workspace a current file, as a customer working elsewhere would have.
  store = ProjectStore.createProjectFile(store, store.activeProjectId, { name: '当前工作' })
  const cases = [context => { context.isGenerating = true }, context => { context.isAccepting = true },
    context => { context.chatAbortRef.current = {} }, context => { context.cadConfirmRef.current = {} },
    context => { context.platform.busy = true }]
  for (const makeBusy of cases) {
    const { context, restored, toasts, downloads } = workbench(store)
    const before = JSON.stringify(context.storeRef.current)
    makeBusy(context)
    assert.equal(context.restoreLocalTrash(store.trash[0].id), false)
    assert.equal(JSON.stringify(context.storeRef.current), before)
    assert.equal(restored.length, 0)
    assert.equal(downloads.length, 0)
    assert.equal(toasts.length, 1)
    assert.equal(toasts[0].type, 'info')
    assert.match(toasts[0].text, /正在处理/)
    assert.doesNotMatch(toasts[0].text, /已恢复/)
  }
})

const storeWorker = String.raw`
import { readFileSync } from 'node:fs'
import * as P from './src/projectStore.js'
const input = JSON.parse(readFileSync(0, 'utf8'))
let result
if (input.action === 'import') result = P.importProjectBackup(P.loadProjectStore({ getItem: () => null }), input.backup)
else {
  const store = P.loadProjectStore({ getItem: key => key === P.PROJECT_STORE_KEY ? JSON.stringify(input.snapshot) : null })
  const record = store.trash.find(record => input.item === 'project' ? record.kind === 'project' : record.kind === 'file' && record.item.name === input.item)
  if (!record) throw new Error('Expected recoverable item is missing')
  result = P.restoreTrashedItem(store, record.id)
}
process.stdout.write(JSON.stringify(P.persistProjectStore(result, { setItem() {} })))
`

const cloudIntegration = String.raw`
import json, pathlib, subprocess, sys, tempfile
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.account_workspace_api import create_account_workspace_router
from app.platform import AuthService

payload = json.load(sys.stdin)
def workspace_action(value):
    completed = subprocess.run([sys.argv[1], '--input-type=module', '-e', payload['worker']],
        input=json.dumps(value, ensure_ascii=False), text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)

with tempfile.TemporaryDirectory(prefix='joyniu-project-cloud-test-') as directory:
    auth = AuthService(pathlib.Path(directory) / 'accounts.sqlite3', token_secret='local-project-test-secret-' * 3)
    users = [auth.create_user(name + '@example.test', 'local-test-password-123', name, roles=['viewer']) for name in ['owner-A', 'owner-B']]
    headers = [{'Authorization': 'Bearer ' + auth.issue_token(user).token} for user in users]
    application = FastAPI()
    application.include_router(create_account_workspace_router(SimpleNamespace(auth=auth)), prefix='/api/v1')
    path = '/api/v1/account/workspace'
    revision = 0
    outcomes = []
    try:
        with TestClient(application) as client:
            def save_and_load(snapshot):
                global revision
                response = client.put(path, headers=headers[0], json={'expectedRevision': revision, 'snapshot': snapshot})
                assert response.status_code == 200, response.text
                revision += 1
                saved = response.json()
                assert saved['revision'] == revision and saved['snapshot'] == snapshot
                loaded = client.get(path, headers=headers[0]).json()
                assert loaded == saved
                assert saved['updatedAt']
                return loaded['snapshot']

            for order in payload['orders']:
                snapshot = workspace_action({'action': 'import', 'backup': payload['backup']})
                snapshot = save_and_load(snapshot)
                for item in order:
                    snapshot = workspace_action({'action': 'restore', 'snapshot': snapshot, 'item': item})
                    snapshot = save_and_load(snapshot)
                project = next(project for project in snapshot['projects'] if any(file['name'] == '说明' for file in project['files']))
                files = {file['name']: file for file in project['files']}
                assert set(files) == {'零件 01', '说明', '草图'}
                assert not snapshot['trash']
                assert len(set(project['id'] for project in snapshot['projects'])) == len(snapshot['projects'])
                assert client.get(path, headers=headers[1]).json() == {'revision': 0, 'snapshot': None, 'updatedAt': None}
                outcomes.append({'order': order, 'revision': revision, 'fileCount': len(files),
                    'length': files['零件 01']['snapshot']['model']['length'],
                    'versionNote': files['零件 01']['versions'][0]['note'],
                    'documentText': files['说明']['snapshot']['documentText'], 'trashCount': len(snapshot['trash'])})
            conflict = client.put(path, headers=headers[0], json={'expectedRevision': 0, 'snapshot': payload['backup']})
            assert conflict.status_code == 409
            assert conflict.json()['detail']['revision'] == revision
            assert 'snapshot' not in conflict.json()['detail']
            assert client.get(path, headers=headers[0]).json()['snapshot'] == snapshot
        print(json.dumps({'revision': revision, 'outcomes': outcomes, 'otherAccountRevision': 0, 'staleSaveStatus': conflict.status_code}, ensure_ascii=False))
    finally:
        auth.close()
`

test('imported backups survive real local cloud save/load and every parent/child trash recovery order', {
  skip: !existsSync(python) && 'Local API Python environment is not installed', timeout: 30_000,
}, context => {
  let store = fresh()
  const parent = store.activeProjectId, part = store.activeFileId
  store = ProjectStore.updateFileSnapshot(store, parent, part, { model: { kind: 'shaft', name: '测试轴', length: 70, outerDiameter: 24 } })
  store = ProjectStore.saveFileVersion(store, parent, part, { note: '云往返保留版本' })
  store = ProjectStore.createProjectFile(store, parent, { name: '说明', type: '文档', snapshot: { documentText: '孔距 36 mm，保留中文说明。' } })
  const document = store.activeFileId
  store = ProjectStore.createProjectFile(store, parent, { name: '草图', type: '工程图' })
  store = ProjectStore.deleteProjectFile(store, parent, part)
  store = ProjectStore.deleteProjectFile(store, parent, document)
  store = ProjectStore.deleteProject(store, parent)
  const orders = [['零件 01', '说明', 'project'], ['零件 01', 'project', '说明'], ['说明', '零件 01', 'project'],
    ['说明', 'project', '零件 01'], ['project', '零件 01', '说明'], ['project', '说明', '零件 01']]
  const execution = spawnSync(python, ['-c', cloudIntegration, process.execPath], {
    cwd: repository, env: { ...process.env, PYTHONPATH: `${repository}apps/api` },
    input: JSON.stringify({ worker: storeWorker, backup: ProjectStore.exportProjectBackup(store), orders }),
    encoding: 'utf8', timeout: 25_000, maxBuffer: 1024 * 1024,
  })
  assert.equal(execution.error, undefined, execution.error?.message)
  assert.equal(execution.status, 0, execution.stderr)
  const result = JSON.parse(execution.stdout)
  assert.equal(result.revision, 24)
  assert.equal(result.otherAccountRevision, 0)
  assert.equal(result.staleSaveStatus, 409)
  assert.equal(result.outcomes.length, 6)
  for (const outcome of result.outcomes) {
    assert.equal(outcome.fileCount, 3)
    assert.equal(outcome.length, 70)
    assert.equal(outcome.versionNote, '云往返保留版本')
    assert.equal(outcome.documentText, '孔距 36 mm，保留中文说明。')
    assert.equal(outcome.trashCount, 0)
  }
  context.diagnostic('6 种恢复顺序；真实本地 PUT/GET 往返 24 次；最终 revision=24；每组 3 文件、轴长 70、中文文档和版本说明保留；回收站 0；账号 B revision=0；过期写入 409。')
})
