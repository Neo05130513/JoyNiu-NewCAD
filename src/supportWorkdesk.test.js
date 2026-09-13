import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

test('actual workdesk renders queues, assignment and private notes with unambiguous visibility', async () => {
  const directory = await mkdtemp(resolve('node_modules/.support-desk-'))
  const bundle = await rolldown({ input: resolve('src/SupportWorkspace.jsx'), external: ['react', 'react/jsx-runtime'], transform: { jsx: { runtime: 'automatic' } }, plugins: [{ name: 'no-css', load: id => id.endsWith('.css') ? { code: '', moduleType: 'js' } : null }] })
  try {
    await bundle.write({ dir: directory, format: 'esm' })
    const { default: SupportWorkspace, SupportAssignmentForm, SupportInternalNotes } = await import(pathToFileURL(resolve(directory, 'SupportWorkspace.js')).href)
    const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
    const account = role => ({ session: { access_token: 'local-test', user: { id: role, roles: [role], active: true } } })
    const admin = render(SupportWorkspace, { account: account('support'), admin: true })
    assert.match(admin, /客服工作台/)
    for (const label of ['全部待处理', '分配给我', '未分配', '优先级', '搜索工单']) assert.ok(admin.includes(label))
    const linked = render(SupportWorkspace, { account: account('support'), admin: true, params: { q: '尺寸复核', status: 'resolved', priority: 'urgent', assignedTo: 'me', offset: '20' } })
    assert.match(linked, /value="尺寸复核"/)
    assert.match(linked, /value="resolved" selected/)
    assert.match(linked, /value="urgent" selected/)
    const customer = render(SupportWorkspace, { account: account('designer') })
    assert.doesNotMatch(customer, /内部备注|工单负责人|全部待处理|优先级/)
    assert.match(render(SupportWorkspace, { account: account('designer'), admin: true }), /没有工单管理权限/)
    const form = render(SupportAssignmentForm, { ticket: { id: 'SUP-1', revision: 2, assignedTo: 'staff', priority: 'urgent', assignee: { displayName: '老客服' } }, assignees: [{ id: 'staff', displayName: '客服甲', email: 'staff@example.test' }], busy: false, onSubmit: () => assert.fail('SSR must not submit') })
    assert.match(form, /工单负责人/)
    assert.match(form, /value="urgent" selected/)
    assert.match(form, /分派说明/)
    const notes = render(SupportInternalNotes, { notes: { items: [{ id: 'note1', authorName: '客服甲', body: '内部交接信息', createdAt: '2026-09-10T00:00:00Z' }], total: 1, offset: 0 }, draft: '', busy: false, onSubmit: () => assert.fail('SSR must not submit'), onDraft: () => {}, onPage: () => {} })
    assert.match(notes, /仅工单管理员可见，不会发送给客户/)
    assert.match(notes, /内部交接信息/)
    assert.match(notes, /保存内部备注/)
    assert.doesNotMatch(notes, /保存并回复客户/)
  } finally { await bundle.close(); await rm(directory, { recursive: true, force: true }) }
})
