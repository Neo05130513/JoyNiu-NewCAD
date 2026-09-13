import test from 'node:test'
import assert from 'node:assert/strict'
import { createDirectFeatureClient } from './directFeatureClient.js'
import { cacheFeatureWorkspace, cachedFeatureWorkspace, draftFromFeatureRecord, featureWorkspaceKey, deleteFeature, featureDependencyIssues, featureErrorFeedback, featureScalar, initialFeatureDraft, moveFeature, newFeature, workspacePayload } from './directFeatureModel.js'

test('case plans initialize as independent drafts without mutating their source', () => {
  const original = initialFeatureDraft().plan
  const draft = initialFeatureDraft(original)
  draft.plan.features[0].size[0] = 100
  assert.equal(original.features[0].size[0], 20)
  assert.equal(draft.sourceRun, null)
  assert.equal(featureScalar('width / 2'), 'width / 2')
  assert.equal(featureScalar('0'), 0)
  assert.equal(featureScalar(''), '')
})
test('direct feature deletion and reorder reject dangling or future references', () => {
  const draft = initialFeatureDraft()
  draft.plan.features.push(newFeature('fillet', draft.plan.features))
  draft.plan.result = 'fillet1'
  assert.throws(() => deleteFeature(draft, 'box1'), /仍引用/)
  assert.throws(() => moveFeature(draft, 'fillet1', -1), /前置特征/)
  const removed = deleteFeature(draft, 'fillet1')
  assert.equal(removed.plan.result, 'box1')
  assert.equal(draft.plan.features.length, 2)
  assert.equal(featureDependencyIssues(draft.plan, ['box1']).length, 1)
  assert.throws(() => workspacePayload({ ...draft, suppressed: ['box1'] }, null, 'file'), /已抑制/)
})
test('independent feature movement and generated identifiers preserve valid references', () => {
  const draft = initialFeatureDraft()
  draft.plan.features.push(newFeature('box', draft.plan.features))
  assert.equal(draft.plan.features[1].id, 'box2')
  const moved = moveFeature(draft, 'box2', -1)
  assert.equal(moved.plan.features[0].id, 'box2')
  assert.deepEqual(featureDependencyIssues(moved.plan), [])
})
test('face-attached sketches keep their source dependency when created as a separate body', () => {
  const draft = initialFeatureDraft()
  const sketch = { ...newFeature('profile_extrude', draft.plan.features), planeSource: { sourceFeatureId: 'box1' } }
  draft.plan.features.push(sketch); draft.plan.result = sketch.id
  assert.throws(() => deleteFeature(draft, 'box1'), /仍引用/)
  assert.throws(() => moveFeature(draft, sketch.id, -1), /前置特征/)
  assert.match(featureDependencyIssues(draft.plan, ['box1']).join(' '), /已抑制/)
  assert.deepEqual(featureDependencyIssues(draft.plan), [])
})
test('independent-body compounds protect both source dependencies through delete, reorder and suppression', () => {
  const draft=initialFeatureDraft(),body=newFeature('box',draft.plan.features)
  draft.plan.features.push(body);const compound=newFeature('compound',draft.plan.features)
  draft.plan.features.push(compound);draft.plan.result=compound.id
  assert.deepEqual(compound.inputs,['box1',body.id]);assert.match(compound.label,/多实体组合/)
  assert.deepEqual(featureDependencyIssues(draft.plan),[])
  for(const id of compound.inputs)assert.throws(()=>deleteFeature(draft,id),/仍引用/)
  assert.throws(()=>moveFeature(draft,compound.id,-1),/前置特征/)
  assert.match(featureDependencyIssues(draft.plan,[body.id]).join(' '),/已抑制/)
})
test('manual workspace client binds downloads to authenticated exact versions', async () => {
  let captured
  const client = createDirectFeatureClient({ base: 'https://example.test/api/cad/features', fetchImpl: async (url, options) => { captured = { url, options }; return { ok: true, blob: async () => new Blob(['STEP']) } } })
  await client.download('private-token', { id: 'feature_abc', revision: 4 }, 'step')
  assert.equal(captured.url, 'https://example.test/api/cad/features/feature_abc/versions/4/artifacts/step')
  assert.equal(captured.options.headers.Authorization, 'Bearer private-token')
  assert.ok(!captured.url.includes('private-token'))
  await assert.rejects(client.list(''), /请登录/)
})

test('a stalled build response recovers durable geometry without a second POST', async () => {
  const calls = []
  const client = createDirectFeatureClient({ base: 'https://example.test/api/cad/features', pollIntervalMs: 1, fetchImpl: async (url, options) => {
    calls.push({ url, method: options.method })
    if (options.method === 'POST') return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }))
    return { ok: true, json: async () => ({ id: 'feature_a', revision: 2, status: 'built' }) }
  } })
  const result = await client.build('token', { id: 'feature_a', revision: 1 })
  assert.equal(result.status, 'built')
  assert.equal(calls.filter(call => call.method === 'POST').length, 1)
})

test('build recovery will not accept another concurrent saved draft as success', async () => {
  const client = createDirectFeatureClient({ base: 'https://example.test', pollIntervalMs: 1, fetchImpl: async (_url, options) => {
    if (options.method === 'POST') throw new TypeError('connection reset')
    return { ok: true, json: async () => ({ id: 'feature_a', revision: 2, status: 'draft' }) }
  } })
  await assert.rejects(client.build('token', { id: 'feature_a', revision: 1 }), /已有其他更新/)
})

test('decimal entry remains editable and direct parameter values normalize only when saving', () => {
  assert.equal(featureScalar('0.'), '0.')
  assert.equal(featureScalar('-2.'), '-2.')
  assert.equal(featureScalar('0.25'), 0.25)
  const draft = initialFeatureDraft()
  draft.plan.parameters.width = { value: featureScalar('0.') }
  const payload = workspacePayload(draft, { revision: 3, fileId: 'original-file' }, 'different-open-file')
  assert.equal(payload.plan.parameters.width.value, 0)
  assert.equal(payload.fileId, 'original-file')
  assert.equal(draft.plan.parameters.width.value, '0.')
  draft.plan.parameters.width.value = 'height / 2'
  assert.throws(() => workspacePayload(draft), /width.*数值/)
})

test('unfinished feature drafts survive reopening but remain isolated by account and case', () => {
  const key = featureWorkspaceKey('account-a', 'gear')
  const draft = initialFeatureDraft()
  const record = { name: draft.name, plan: draft.plan, suppressed: [], changeNote: '已保存' }
  const snapshot = {draft: draftFromFeatureRecord(record), dirty: true, history: [draft]}
  snapshot.draft.name = '未保存齿轮'
  cacheFeatureWorkspace(key, snapshot)
  snapshot.draft.name = '其他编辑'
  assert.equal(cachedFeatureWorkspace(key).draft.name, '未保存齿轮')
  const restored = cachedFeatureWorkspace(key)
  restored.draft.plan.features[0].size[0] = 999
  assert.notEqual(cachedFeatureWorkspace(key).draft.plan.features[0].size[0], 999)
  assert.equal(cachedFeatureWorkspace(featureWorkspaceKey('account-b', 'gear')), null)
  assert.equal(cachedFeatureWorkspace(featureWorkspaceKey('account-a', 'spring')), null)
  assert.notEqual(record.name, '未保存齿轮')
})

test('geometry errors preserve the failing feature for locating its editable form', async () => {
  const client = createDirectFeatureClient({fetchImpl: async () => ({ok: false, status: 422, json: async () => ({detail: {message: '特征 cup 无法抽壳', featureId: 'cup'}})})})
  await assert.rejects(client.build('token', {id: 'feature_a', revision: 1}), error => error.featureId === 'cup' && error.status === 422)
})

test('negative box sizes have clear Chinese guidance while preserving exact kernel detail and feature location', async () => {
  const raw = '实体未通过重建：特征 box1：box size must be greater than 0.0000001 mm'
  const client = createDirectFeatureClient({fetchImpl: async () => ({ok: false, status: 422, json: async () => ({detail: {message: raw, featureId: 'box1'}})})})
  try { await client.build('token', {id: 'feature_a', revision: 3}); assert.fail('invalid geometry was accepted') }
  catch (error) {
    assert.equal(error.featureId, 'box1')
    const feedback = featureErrorFeedback(error)
    assert.equal(feedback.message, '实体未通过重建：特征 box1：长方体的长、宽、高必须大于 0')
    assert.equal(feedback.detail, raw)
    assert.equal(error.message, raw)
  }
})

test('common parameter and dependency errors translate without guessing unknown kernel failures', () => {
  for (const [raw, expected] of [
    ['radius must be greater than 0.0000001 mm', '半径必须大于 0'],
    ['fillet radius must be greater than 0.0000001 mm', '圆角半径必须大于 0'],
    ['height must be greater than 0.0000001 mm', '高度必须大于 0'],
    ['Inner diameter must be smaller than outer diameter', '内径必须小于外径'],
    ['Inner radius must be less than the outer radius', '内半径必须小于外半径'],
    ['inner >= outer', '内尺寸必须小于对应外尺寸'],
    ['Input must name an earlier feature', '引用实体必须是排在当前特征之前的特征'],
    ['Boolean inputs must contain 2 to 32 earlier feature IDs', '布尔运算须选择 2 至 32 个前置特征'],
    ['Expression references unknown parameters: width', '表达式引用了不存在的参数，请补充或修正：width'],
  ]) {
    const feedback = featureErrorFeedback(new Error(raw))
    assert.ok(feedback.message.startsWith(expected), feedback.message)
    assert.equal(feedback.detail, raw)
  }
  assert.deepEqual(featureErrorFeedback(new Error('Unrecognized kernel exception')), {message: 'Unrecognized kernel exception', detail: ''})
})

test('token renewal keeps the same account draft while a different account remains isolated', () => {
  const before = featureWorkspaceKey('old-access-token', 'gear', 'user-alice')
  const after = featureWorkspaceKey('new-access-token', 'gear', 'user-alice')
  const draft = initialFeatureDraft()
  draft.name = '刷新登录前的未保存修改'
  cacheFeatureWorkspace(before, {draft, dirty: true, history: [draft], lastBuilt: {id: 'feature_a', revision: 2}})
  assert.equal(after, before)
  assert.equal(cachedFeatureWorkspace(after).draft.name, draft.name)
  assert.equal(cachedFeatureWorkspace(after).lastBuilt.revision, 2)
  assert.equal(cachedFeatureWorkspace(featureWorkspaceKey('new-access-token', 'gear', 'user-bob')), null)
  assert.ok(!before.includes('access-token'))
})

test('build recovery uses refreshed credentials without repeating the build mutation', async () => {
  let token = 'initial-token'
  const calls = []
  const client = createDirectFeatureClient({pollIntervalMs: 1, fetchImpl: async (_url, options) => {
    calls.push({method: options.method, authorization: options.headers.Authorization})
    if (options.method === 'POST') { token = 'renewed-token'; throw new TypeError('connection reset') }
    return {ok: true, json: async () => ({id: 'feature_a', revision: 2, status: 'built'})}
  }})
  const result = await client.build(() => token, {id: 'feature_a', revision: 1})
  assert.equal(result.status, 'built')
  assert.deepEqual(calls, [{method: 'POST', authorization: 'Bearer initial-token'}, {method: 'GET', authorization: 'Bearer renewed-token'}])
})
