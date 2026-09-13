import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

let directory, bundle, Workspace, DrawingPreview
before(async () => {
  directory = await mkdtemp(resolve('node_modules/.engineering-render-'))
  bundle = await rolldown({ input: resolve('src/EngineeringWorkspace.jsx'), external: ['react', 'react/jsx-runtime', 'three', 'three/examples/jsm/controls/OrbitControls.js', 'three/examples/jsm/loaders/GLTFLoader.js'], transform: { jsx: { runtime: 'automatic' } }, plugins: [{ name: 'empty-css', load(id) { if (id.endsWith('.css')) return { code: '', moduleType: 'js' } } }] })
  await bundle.write({ file: resolve(directory, 'workspace.mjs'), format: 'esm' })
  const module = await import(pathToFileURL(resolve(directory, 'workspace.mjs')).href)
  Workspace = module.default; DrawingPreview = module.EngineeringDrawingPreview
})
after(async () => { await bundle?.close(); if (directory) await rm(directory, { recursive: true, force: true }) })

test('logged-out engineering workspace does not expose upload, download or AI actions', () => {
  const html = renderToStaticMarkup(React.createElement(Workspace, {}))
  assert.match(html, /请先登录/)
  assert.doesNotMatch(html, /type="file"|AI 规划并生成装配|STEP ↓/)
})

test('empty assembly explains the available workflow and disables unavailable actions', () => {
  const html = renderToStaticMarkup(React.createElement(Workspace, { token: 'test-owner', initialTab: 'assembly' }))
  assert.match(html, /装配草稿为空/)
  assert.match(html, /正在加载工程设计/)
  assert.match(html, /disabled=""[^>]*>导入当前模型/)
  assert.match(html, /disabled=""[^>]*>生成装配 · 检查实体干涉/)
  assert.match(html, /disabled=""[^>]*>添加实例/)
  assert.match(html, /disabled=""[^>]*>AI 规划并生成装配/)
  assert.doesNotMatch(html, /NaN|undefined 个实体/)
})

test('assembly composer remains mounted when viewing measurements so its draft survives tab navigation', () => {
  const html = renderToStaticMarkup(React.createElement(Workspace, { token: 'test-owner', initialTab: 'viewer' }))
  assert.match(html, /class="eng-panel" hidden=""/)
  assert.match(html, /完整装配需求/)
  assert.match(html, /先选择或上传一个 STEP/)
})

test('rotating a credential keeps the account workspace identity while another account is isolated', () => {
  const before = Workspace({ accountKey: 'account-a', token: 'credential-1' })
  const renewed = Workspace({ accountKey: 'account-a', token: 'credential-2' })
  const another = Workspace({ accountKey: 'account-b', token: 'credential-3' })
  assert.equal(before.type, renewed.type)
  assert.equal(before.key, renewed.key)
  assert.notEqual(renewed.key, another.key)
  assert.equal(renewed.props.token, 'credential-2')
})

test('drawing preview exposes display-only zoom and restores fit without editing output scale', () => {
  const html = renderToStaticMarkup(React.createElement(DrawingPreview, { src: 'blob:owned-drawing-preview' }))
  assert.match(html, /工程图预览缩放/)
  assert.match(html, /type="range" min="50" max="800" step="50" value="100"/)
  assert.match(html, /适合窗口/)
  assert.match(html, /不改变图纸比例或 DXF \/ PDF 文件/)
  assert.match(html, /src="blob:owned-drawing-preview"/)
})
