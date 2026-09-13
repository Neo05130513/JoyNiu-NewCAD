/** Isolated local Chromium acceptance; no system-browser session or storage-state export. */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/Users/neo/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')
const base = process.env.CAD_IMPORT_E2E_URL || 'http://127.0.0.1:5186'
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'This acceptance script is restricted to the local app.')
const email = process.env.CAD_IMPORT_E2E_EMAIL, password = process.env.CAD_IMPORT_E2E_PASSWORD, source = process.env.CAD_IMPORT_E2E_STEP
assert.ok(email && password && source, 'Provide dedicated local QA credentials and CAD_IMPORT_E2E_STEP (20 × 16 × 10 mm box).')
const output = resolve(process.env.CAD_IMPORT_E2E_OUTPUT || '/tmp/joyniu-cad-import-pdm-e2e')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-webgl'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 980 }, acceptDownloads: true })
const page = await context.newPage(), requests = [], errors = []
page.on('response', response => { const url = new URL(response.url()); if (url.pathname.startsWith('/api/')) requests.push({ path: url.pathname, method: response.request().method(), status: response.status() }) })
page.on('pageerror', error => errors.push(error.message))
const shot = name => page.screenshot({ path: resolve(output, name) })
const details = record => ({ id: record.id, revision: record.revision, fileId: record.fileId, name: record.name, inspection: record.inspection, plan: record.plan })
async function actionResponse(path, action, status = 200) {
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === path && response.request().method() === 'POST', { timeout: 180000 })
  await action(); const response = await pending, value = await response.json()
  assert.equal(response.status(), status, JSON.stringify(value.detail || value))
  return value
}
async function finish() {
  const record = await actionResponse('/api/cad/features/commit', () => page.getByRole('button', { name: '完成', exact: true }).click())
  await page.getByText(`已保存 r${record.revision}`, { exact: true }).waitFor({ timeout: 30000 })
  assert.equal(record.inspection.stepReadback.valid, true)
  return record
}
try {
  await page.goto(base)
  await page.getByRole('button', { name: '登录 / 注册', exact: true }).click()
  await page.getByLabel('账号或邮箱').fill(email); await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).last().click()
  await page.getByRole('button', { name: '登录 / 注册', exact: true }).waitFor({ state: 'hidden' })
  if (!await page.getByRole('button', { name: '＋ 新建', exact: true }).count()) {
    await page.getByText('设计工具', { exact: true }).click()
    await page.getByRole('button', { name: '特征编辑', exact: true }).click()
  }
  await page.getByRole('button', { name: '＋ 新建', exact: true }).click()
  await page.getByText('空图档', { exact: true }).waitFor()
  await page.getByRole('button', { name: '导入实体', exact: true }).click()
  const chooser = page.waitForEvent('filechooser'); await page.getByLabel('实体源文件').click(); await (await chooser).setFiles(source)
  await page.getByLabel('导入实体名称').fill('验收-导入PDM')
  await shot('01-import-form.png')
  await actionResponse('/api/cad/designs/imports', () => page.getByRole('button', { name: '导入并开始编辑', exact: true }).click(), 201)
  await page.getByRole('button', { name: '完成', exact: true }).waitFor({ timeout: 60000 }); await shot('02-import-preview.png')
  const imported = await finish()
  assert.deepEqual(imported.inspection.bbox.size, [20, 16, 10]); assert.equal(imported.plan.features[0].op, 'import_step')
  await page.getByRole('button', { name: '标准件', exact: true }).click()
  await page.getByLabel('标准件规格').selectOption('socket-m6-20'); await page.getByLabel('标准件位置 X').fill('40')
  await shot('03-standard-form.png'); await page.getByRole('button', { name: '插入当前模型', exact: true }).click()
  const standard = await finish(); assert.equal(standard.inspection.solidCount, 2); await shot('04-standard-committed.png')
  await page.getByRole('button', { name: '保存到 PDM', exact: true }).click()
  await page.getByText('新建 PDM 项目', { exact: true }).click()
  const projectName = '验收-导入PDM项目-' + Date.now()
  await page.getByLabel('新 PDM 项目名称').fill(projectName)
  await actionResponse('/api/cad/designs/pdm/projects', () => page.getByRole('button', { name: '新建', exact: true }).click(), 201)
  await page.getByLabel('PDM 项目').locator('option').filter({ hasText: projectName }).waitFor({ state: 'attached' })
  await page.getByLabel('PDM 保存名称').fill('验收-导入PDM'); await shot('05-pdm-save-form.png')
  const saved = await actionResponse('/api/cad/designs/pdm/versions', () => page.getByRole('button', { name: '保存当前版本', exact: true }).click(), 201)
  assert.equal(saved.source.featureId, standard.id); assert.equal(saved.source.revision, standard.revision); assert.ok(saved.engineeringDesignId)
  await page.getByText('验收-导入PDM · 1 个版本', { exact: true }).click(); await shot('06-pdm-version.png')
  const opened = await actionResponse(`/api/cad/designs/pdm/versions/${saved.version.id}/open`, () => page.getByRole('button', { name: '打开此版本', exact: true }).click(), 201)
  assert.notEqual(opened.record.id, standard.id); assert.equal(opened.record.revision, 1)
  assert.equal(opened.record.pdmSource.versionId, saved.version.id)
  await page.getByText('已保存 r1', { exact: true }).waitFor(); await shot('07-pdm-opened-copy.png')
  await page.getByRole('button', { name: /方程式 \(16\)/ }).click()
  await page.getByLabel(/^sp_.*_x 数值或表达式$/).fill('50'); await shot('08-copy-parameter-edit.png')
  const modified = await finish(); assert.equal(modified.id, opened.record.id); assert.equal(modified.revision, 2); assert.equal(modified.inspection.solidCount, 2)
  const position = Object.entries(modified.plan.parameters).find(([key]) => /^sp_.*_x$/.test(key))
  assert.equal(position[1].value, 50)
  await shot('09-modified-copy.png')
  const downloading = page.waitForEvent('download'); await page.getByRole('button', { name: '导出 STP', exact: true }).click()
  const download = await downloading; await download.saveAs(resolve(output, 'modified-copy.step')); assert.equal(await download.failure(), null)
  await page.reload(); await page.getByText('已保存 r2', { exact: true }).waitFor({ timeout: 60000 })
  await shot('10-reopened-after-refresh.png')
  const { readFile } = await import('node:fs/promises'), bytes = await readFile(resolve(output, 'modified-copy.step'))
  assert.ok(bytes.subarray(0, 200).includes(Buffer.from('ISO-10303-21'))); assert.equal(errors.length, 0)
  const report = { browser: 'isolated headless Chromium', base, source: { bytes: (await readFile(source)).length }, imported: details(imported), standard: details(standard), pdm: { versionId: saved.version.id, source: saved.source, engineeringDesignId: saved.engineeringDesignId }, opened: details(opened.record), modified: details(modified), download: { filename: download.suggestedFilename(), bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }, requests, errors, refreshReopened: true }
  await writeFile(resolve(output, 'browser-verification.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ success: true, output, featureId: modified.id, revision: modified.revision }))
} catch (error) {
  await shot('failure.png').catch(() => {})
  await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, requests, errors }, null, 2))
  throw error
} finally { await context.close(); await browser.close() }
