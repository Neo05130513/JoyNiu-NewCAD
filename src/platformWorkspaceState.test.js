import test from 'node:test'
import assert from 'node:assert/strict'
import { platformScope, platformScopeIsCurrent, pdmDocumentPage } from './platformWorkspaceState.js'

const context = { token: 'account-A', projectId: 'project-1', fileId: 'file-1', generation: { requestId: 'geometry-1' } }

test('late platform results cannot restore an account after logout or account switching', () => {
  const pending = platformScope(context)
  assert.equal(platformScopeIsCurrent(pending, platformScope(context)), true)
  assert.equal(platformScopeIsCurrent(pending, platformScope({ ...context, token: '' })), false)
  assert.equal(platformScopeIsCurrent(pending, platformScope({ ...context, token: 'account-B' })), false)
  assert.equal(platformScopeIsCurrent(pending, platformScope({ ...context, token: '' }), true), false)
})

test('same-project file changes and model revisions invalidate pending CAM results', () => {
  const pending = platformScope(context)
  for (const change of [{ fileId: 'file-2' }, { projectId: 'project-2' },
    { generation: { requestId: 'geometry-2' } }, { generation: { requestId: 'geometry-1', stale: true } }]) {
    assert.equal(platformScopeIsCurrent(pending, platformScope({ ...context, ...change })), false)
  }
  assert.equal(platformScopeIsCurrent(pending, platformScope({ ...context, fileId: 'file-2' }), true), true)
})

test('PDM browsing reaches every document and can find a version by its original filename', () => {
  const documents = Array.from({ length: 23 }, (_, index) => ({ document: { id: `doc-${index}`, metadata: { displayName: `零件 ${index + 1}` } },
    versions: [{ file_name: `drawing-${index + 1}.pdf` }] }))
  const all = [0, 1, 2].flatMap((page) => pdmDocumentPage(documents, '', page).items)
  assert.equal(new Set(all.map((entry) => entry.document.id)).size, 23)
  assert.equal(pdmDocumentPage(documents, '', 99).page, 2)
  assert.equal(pdmDocumentPage(documents, 'drawing-23.pdf', 2).items[0].document.id, 'doc-22')
  assert.equal(pdmDocumentPage(documents, '不存在').total, 0)
})
