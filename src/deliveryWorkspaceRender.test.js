import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { rolldown } from 'rolldown'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { deliveryFileDownloadName, deliveryFileSourceIndex } from './deliveryWorkspaceClient.js'

let directory, bundle, Workspace, SourceList, PackageDetail
before(async () => {
  directory = await mkdtemp(resolve('node_modules/.delivery-render-'))
  bundle = await rolldown({ input: resolve('src/DeliveryWorkspace.jsx'), external: ['react', 'react/jsx-runtime'], transform: { jsx: { runtime: 'automatic' } }, plugins: [{ name: 'empty-css', load(id) { if (id.endsWith('.css')) return { code: '', moduleType: 'js' } } }] })
  await bundle.write({ file: resolve(directory, 'workspace.mjs'), format: 'esm' })
  const module = await import(pathToFileURL(resolve(directory, 'workspace.mjs')).href)
  Workspace = module.default; SourceList = module.DeliverySourceList; PackageDetail = module.DeliveryPackageDetail
})
after(async () => { await bundle?.close(); if (directory) await rm(directory, { recursive: true, force: true }) })
const render = (component, props = {}) => renderToStaticMarkup(React.createElement(component, props))
const detail = { id: 'pack-1', title: '减速器交付', notes: '核对配合尺寸', createdAt: '2026-09-12T11:00:00Z', sources: [{ kind: 'feature', id: 'part-a', revision: 4, name: '机架', provenance: { sourceRunId: 'run-1', selected: false } }], files: [{ id: 'file-1', name: '机架.step', bytes: 2048, sha256: 'a'.repeat(64), mimeType: 'application/step' }], verification: { status: 'requires_review', message: '手工设计须核对尺寸。' } }

test('logged-out workspace exposes only sign-in guidance', () => {
  const html = render(Workspace)
  assert.match(html, /请先登录/)
  assert.doesNotMatch(html, /type="submit"|下载整包|type="checkbox"/)
})

test('initial load distinguishes loading from empty and blocks premature creation', () => {
  const html = render(Workspace, { token: 'test-token', accountKey: 'owner-1' })
  assert.match(html, /正在读取当前账号的可交付来源/)
  assert.match(html, /disabled=""[^>]*>创建交付包/)
  assert.match(html, /maxLength="180"/)
  assert.match(html, /maxLength="4000"/)
  assert.match(html, /不会自动确认模型或完成人工验收/)
  assert.doesNotMatch(html, /NaN|undefined 个/)
})

test('same-account credential refresh preserves the workspace key; other accounts reset it', () => {
  const old = Workspace({ accountKey: 'owner-1', token: 'token-a' })
  const renewed = Workspace({ accountKey: 'owner-1', token: 'token-b' })
  const another = Workspace({ accountKey: 'owner-2', token: 'token-c' })
  assert.equal(old.key, renewed.key)
  assert.equal(old.type, renewed.type)
  assert.notEqual(renewed.key, another.key)
  assert.equal(renewed.props.token, 'token-b')
})

test('source load failures do not become empty states, and unavailable results explain why', () => {
  const failure = render(SourceList, { error: '无法连接服务' })
  assert.match(failure, /role="alert"/)
  assert.match(failure, /重新读取来源/)
  assert.doesNotMatch(failure, /还没有可选来源/)
  const empty = render(SourceList)
  assert.match(empty, /还没有可选来源/)
  assert.match(empty, /完成 AI 建模结果的交付确认/)
  const blocked = render(SourceList, { sources: [{ kind: 'cad_run', id: 'run-1', revision: 2, name: '待核对模型', eligible: false, reason: '模型尚未确认' }] })
  assert.match(blocked, /模型尚未确认/)
  assert.match(blocked, /type="checkbox" disabled=""/)
  assert.match(blocked, /版本 2/)
})

test('sealed package shows the complete files, version and hash without treating source confirmation as acceptance', () => {
  const html = render(PackageDetail, { detail: { ...detail, verification: { status: 'confirmed_source', message: '来源已确认。' } } })
  assert.match(html, /已封存的交付包/)
  assert.match(html, /来源已确认 · 包待验收/)
  assert.match(html, /不代表制造放行/)
  assert.match(html, /下载整包 ZIP/)
  assert.match(html, /机架\.step/)
  assert.match(html, /版本 4/)
  assert.match(html, /自动关联/)
  assert.match(html, /a{64}/)
  assert.match(html, /<details><summary>查看文件校验值/)
  assert.doesNotMatch(html, /href=|Bearer|确认验收.*button/)
})

test('download in progress locks all download actions and source fields are escaped as text', () => {
  const html = render(PackageDetail, { detail: { ...detail, title: '<script>alert(1)</script>', files: [{ ...detail.files[0], name: '<img src=x onerror=alert(1)>.step' }] }, downloadKey: 'file-1' })
  assert.match(html, /disabled=""[^>]*>下载整包 ZIP/)
  assert.match(html, /disabled=""[^>]*aria-label=[^>]*>下载中…/)
  assert.match(html, /&lt;script&gt;/)
  assert.doesNotMatch(html, /<script>|<img src=x/)
})

test('detail failures can be retried and empty manifests cannot be downloaded', () => {
  const failure = render(PackageDetail, { error: '此交付包不存在或无权访问' })
  assert.match(failure, /重试打开/)
  assert.doesNotMatch(failure, /下载整包 ZIP/)
  const empty = render(PackageDetail, { detail: { ...detail, files: [] } })
  assert.match(empty, /没有可下载文件/)
  assert.match(empty, /disabled=""[^>]*>下载整包 ZIP/)
})

test('layout keeps both retained views hidden correctly and primary hover remains readable', async () => {
  const css = await readFile(resolve('src/delivery-workspace.css'), 'utf8')
  assert.match(css, /\.delivery-workspace \[hidden\]\{display:none!important\}/)
  assert.match(css, /@media\(max-width:760px\)/)
  assert.match(css, /grid-template-columns:minmax\(0,1fr\)/)
  assert.match(css, /\.delivery-primary:hover[^}]*background:#194c91[^}]*color:#fff/)
  assert.match(css, /\.delivery-primary:disabled:hover[^}]*color:#526780/)
})

// Actual browser-created QA package, 2026-09-12. Public source/file fields retained; extended provenance omitted.
const browserPackageFixture = {
  "archive": {
    "bytes": 28009,
    "sha256": "75308e7aea8a33c0ccf027f184c8155e8cbe60fd9337dc2ad70629cced0cac34"
  },
  "createdAt": "2026-09-12T14:52:03.511874+00:00",
  "files": [
    {
      "bytes": 32489,
      "id": "file_1f351b0021964fef84536049",
      "mimeType": "application/step",
      "name": "model.step",
      "sha256": "0e14ec9d33753d2545e880940b6995eb2a8b821fc80d59c63ba46e13577b4eb9"
    },
    {
      "bytes": 2980,
      "id": "file_cf455d4e64714c42b58d21f2",
      "mimeType": "model/gltf-binary",
      "name": "model.glb",
      "sha256": "64b3f93c36c07bceab1b6d9f75526aee5d34e9ea010624761ce978cfc4cbead3"
    },
    {
      "bytes": 93,
      "id": "file_10e0b9adfedd44ba82a50476",
      "mimeType": "text/csv;charset=utf-8",
      "name": "bom.csv",
      "sha256": "da9e9923e906a35e5739ad5cd0ffda715f2063857d0e98252a8c537d80b648e8"
    },
    {
      "bytes": 1305,
      "id": "file_db31f80f14474d8c96add37e",
      "mimeType": "application/json",
      "name": "engineering-design.json",
      "sha256": "27c047d5bff4a1c494f886df5e185b94e7db9f8314784d1657ef5562d504ca6c"
    },
    {
      "bytes": 35607,
      "id": "file_1ab9ca07ea35467d9b1af3e3",
      "mimeType": "application/dxf",
      "name": "用户体验二维验收.dxf",
      "sha256": "c695b40955f0b42ebfc1d94c37b21970a982dd47d2552e502e4b78abe2c4c004"
    },
    {
      "bytes": 1029,
      "id": "file_5330e9c459994a6b91a80d3a",
      "mimeType": "application/json",
      "name": "native-document.json",
      "sha256": "183c82501523601932d0c22449c6a9ba9e00f724b9eb0ab1fb0e1e02d1777304"
    },
    {
      "bytes": 15429,
      "id": "file_f62bb5734a4c40108e451ba7",
      "mimeType": "application/step",
      "name": "model.step",
      "sha256": "a8840fee3f57444ea93f26432dff799fe02ee8ef64b63ff29a610513eb483aec"
    },
    {
      "bytes": 1972,
      "id": "file_e7f0b2738df7411a9b430828",
      "mimeType": "model/gltf-binary",
      "name": "model.glb",
      "sha256": "867ba6c16bbd9bfd5a081e2b217ad9dbb7d17bfff3beb5b7e29f69a82315b577"
    },
    {
      "bytes": 524,
      "id": "file_409371df87ca4d9f941ee630",
      "mimeType": "application/json",
      "name": "engineering-design.json",
      "sha256": "febf8b4be3d0a0fe04e632d0043f7266f29afc60b93b11aee6d567155cf86ba5"
    },
    {
      "bytes": 15429,
      "id": "file_33b97fd5bb6847e99bb3b885",
      "mimeType": "application/step",
      "name": "model.step",
      "sha256": "b5bc7921ffe740b258fa37a5ef37ceda086f9e368bff544059e0b7f4fadbefc8"
    },
    {
      "bytes": 1988,
      "id": "file_7d404292690347e288fea29e",
      "mimeType": "model/gltf-binary",
      "name": "model.glb",
      "sha256": "03d88ba06ad8dfdb0c262198ee9c1189183d44bb001c53db4e29c5f1fc1ffd57"
    },
    {
      "bytes": 222,
      "id": "file_0132796b17fa4ab6b54ad1d4",
      "mimeType": "application/json",
      "name": "plan.json",
      "sha256": "e8b730b2a4941680e4a2ee97dfd2051b0ceb9018446aaa7312f01c7a587bf7ba"
    },
    {
      "bytes": 478,
      "id": "file_832181398e8348a8b720037c",
      "mimeType": "application/json",
      "name": "feature-design.json",
      "sha256": "03bcfe77fb508399593e504058f5785d2f2a72f75c99df2170071331c468a904"
    }
  ],
  "id": "delivery_b7cbb105854f4f8b9c86ca8e00d1aa3f",
  "manifest": {
    "bytes": 4907,
    "name": "manifest.json",
    "sha256": "c4a816319abadffc117a5b3054e4424cab57b651644ccc745760f1c195cae272"
  },
  "notes": "验证：双长方体装配、子零件与原生二维图统一封存；后续修改不能改变本包。",
  "sources": [
    {
      "id": "design_6b331cbc664444d0aea3e01f1f186c2d",
      "kind": "engineering",
      "name": "用户体验双长方体装配",
      "revision": null,
      "status": "built",
      "fileIds": [
        "file_1f351b0021964fef84536049",
        "file_cf455d4e64714c42b58d21f2",
        "file_10e0b9adfedd44ba82a50476",
        "file_db31f80f14474d8c96add37e"
      ]
    },
    {
      "id": "nd_42f3cde33d984d29be23b64ce97a0d23",
      "kind": "native",
      "name": "用户体验二维验收",
      "revision": 7,
      "status": "saved",
      "fileIds": [
        "file_1ab9ca07ea35467d9b1af3e3",
        "file_5330e9c459994a6b91a80d3a"
      ]
    },
    {
      "id": "design_58638b5fd3f74f02b0d0b2ec3ae6e3b2",
      "kind": "engineering",
      "name": "用户验收长方体",
      "revision": null,
      "status": "built",
      "fileIds": [
        "file_f62bb5734a4c40108e451ba7",
        "file_e7f0b2738df7411a9b430828",
        "file_409371df87ca4d9f941ee630"
      ]
    },
    {
      "id": "feature_2887a2d0ce284d138ad79d1d16362874",
      "kind": "feature",
      "name": "用户验收长方体",
      "revision": 5,
      "status": "built",
      "fileIds": [
        "file_33b97fd5bb6847e99bb3b885",
        "file_7d404292690347e288fea29e",
        "file_0132796b17fa4ab6b54ad1d4",
        "file_832181398e8348a8b720037c"
      ]
    }
  ],
  "title": "上线验收 · 装配与二维图 · 0912",
  "verification": {
    "message": "文件已按来源版本封存；包含手工、工程或二维设计，未作制造确认。请核对尺寸、工程图和适用工况。",
    "status": "requires_review"
  }
}


test('actual 4-source 13-file package distinguishes all three model.step artifacts by source and version', () => {
  const before = JSON.stringify(browserPackageFixture)
  const html = render(PackageDetail, { detail: browserPackageFixture })
  assert.match(html, /4 个来源 · 13 个文件/)
  for (const label of [
    '下载 用户体验双长方体装配 · 工程设计 · 封存版 00d1aa3f · model.step',
    '下载 用户验收长方体 · 工程设计 · 封存版 00d1aa3f · model.step',
    '下载 用户验收长方体 · 特征设计 · 版本 5 · model.step',
  ]) assert.ok(html.includes(`aria-label="${label}"`), label)
  assert.match(html, /用户体验二维验收 · 二维图纸 · 版本 7/)
  const owners = deliveryFileSourceIndex(browserPackageFixture.sources)
  assert.equal(owners.size, 13)
  const filenames = browserPackageFixture.files.map(file => deliveryFileDownloadName(browserPackageFixture, file))
  assert.equal(new Set(filenames).size, 13)
  assert.deepEqual(browserPackageFixture.files.filter(file => file.name === 'model.step').map(file => deliveryFileDownloadName(browserPackageFixture, file)), [
    '用户体验双长方体装配-工程设计-封存版00d1aa3f_model.step',
    '用户验收长方体-工程设计-封存版00d1aa3f_model.step',
    '用户验收长方体-特征设计-v5_model.step',
  ])
  for (const file of browserPackageFixture.files) assert.ok(html.includes(file.sha256))
  assert.ok(html.includes(browserPackageFixture.manifest.sha256))
  assert.ok(html.includes(browserPackageFixture.archive.sha256))
  assert.equal(JSON.stringify(browserPackageFixture), before)
})
