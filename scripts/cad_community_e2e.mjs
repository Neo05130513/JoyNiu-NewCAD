/** Local-only, isolated Chromium acceptance. Credentials never enter evidence. */
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || '/Users/neo/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')
const base = process.env.CAD_COMMUNITY_E2E_URL || 'http://127.0.0.1:5186'
assert.ok(['localhost', '127.0.0.1'].includes(new URL(base).hostname), 'Only local acceptance is allowed.')
const users = JSON.parse(await readFile(process.env.CAD_COMMUNITY_E2E_CREDENTIALS, 'utf8'))
assert.equal(users.length, 2, 'Use two dedicated local QA accounts.')
const source = resolve(process.env.CAD_COMMUNITY_E2E_STEP)
const output = resolve(process.env.CAD_COMMUNITY_E2E_OUTPUT || '/tmp/joyniu-community-browser-e2e')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-webgl'] })
const contexts = [], pages = [], events = [], errors = []
let resource, withdrawn = false
const report = { browser: 'isolated headless Chromium', base, checks: {}, events, errors }
const shot = (page, name) => page.screenshot({ path: resolve(output, name) })
async function post(page, path, action, expected = 200) {
  const waiting = page.waitForResponse(response => new URL(response.url()).pathname === path && response.request().method() === 'POST', { timeout: 180000 })
  await action(); const response = await waiting, value = await response.json()
  assert.equal(response.status(), expected, JSON.stringify(value.detail || { status: response.status() }))
  return value
}
async function login(user, label) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 980 }, acceptDownloads: true })
  contexts.push(context); const page = await context.newPage(); pages.push(page)
  page.on('response', response => { const url = new URL(response.url()); if (url.pathname.startsWith('/api/')) events.push({ actor: label, path: url.pathname, method: response.request().method(), status: response.status() }) })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(base); await page.getByRole('button', { name: '登录 / 注册', exact: true }).click()
  await page.getByLabel('账号或邮箱').fill(user.email); await page.getByLabel('密码', { exact: true }).fill(user.password)
  await page.getByRole('button', { name: '登录', exact: true }).last().click()
  await page.getByRole('button', { name: '登录 / 注册', exact: true }).waitFor({ state: 'hidden' })
  if (!await page.getByRole('button', { name: '＋ 新建', exact: true }).count()) {
    await page.getByText('设计工具', { exact: true }).click(); await page.getByRole('button', { name: '特征编辑', exact: true }).click()
  }
  await page.getByRole('button', { name: '＋ 新建', exact: true }).click()
  return page
}
async function finish(page) {
  const result = await post(page, '/api/cad/features/commit', () => page.getByRole('button', { name: '完成', exact: true }).click())
  await page.getByText(`已保存 r${result.revision}`, { exact: true }).waitFor({ timeout: 60000 })
  assert.equal(result.inspection.stepReadback.valid, true)
  return result
}
try {
  const author = await login(users[0], 'author')
  await author.getByRole('button', { name: '导入实体', exact: true }).click()
  const chooser = author.waitForEvent('filechooser'); await author.getByLabel('实体源文件').click(); await (await chooser).setFiles(source)
  await author.getByLabel('导入实体名称').fill('合成社区模型')
  await post(author, '/api/cad/designs/imports', () => author.getByRole('button', { name: '导入并开始编辑', exact: true }).click(), 201)
  const original = await finish(author); report.original = { id: original.id, revision: original.revision, inspection: original.inspection }
  await author.getByRole('button', { name: 'AI 资源社区', exact: true }).click()
  await author.getByRole('button', { name: '发布当前模型', exact: true }).click()
  const name = '验收-社区独立副本-' + Date.now()
  await author.getByLabel('社区资源名称').fill(name); await author.getByLabel('社区资源说明').fill('合成 20 × 16 × 10 mm 实体，用于社区发布、复制与撤下验收。')
  assert.equal(await author.getByRole('button', { name: '确认公开发布', exact: true }).isDisabled(), true)
  await author.getByLabel('确认公开共享模型').check(); await shot(author, '01-explicit-publication.png')
  resource = await post(author, '/api/cad/community', () => author.getByRole('button', { name: '确认公开发布', exact: true }).click(), 201)
  report.resource = resource; report.checks.explicitConsent = true
  await author.locator('.cad-community-preview canvas').first().waitFor({ timeout: 60000 })
  await shot(author, '02-published-preview.png')
  const anonymous = await author.request.get(new URL('/api/cad/community/' + resource.id, 'http://127.0.0.1:8016').href)
  assert.equal(anonymous.status(), 200); report.publicResponse = await anonymous.json()
  assert.equal(JSON.stringify(report.publicResponse).includes(original.id), false)
  const reader = await login(users[1], 'reader')
  await reader.getByRole('button', { name: 'AI 资源社区', exact: true }).click()
  await reader.getByLabel('搜索社区资源').fill(name); await reader.getByRole('button', { name: '搜索', exact: true }).click()
  await reader.getByRole('button', { name: new RegExp(name) }).click()
  await reader.locator('.cad-community-preview canvas').first().waitFor({ timeout: 60000 }); await shot(reader, '03-other-account-search-preview.png')
  const pendingDownload = reader.waitForEvent('download'); await reader.getByRole('button', { name: '下载 STEP', exact: true }).click()
  const download = await pendingDownload; await download.saveAs(resolve(output, 'published.step')); assert.equal(await download.failure(), null)
  report.checks.browserDownload = true
  const opened = await post(reader, `/api/cad/community/${resource.id}/open`, () => reader.getByRole('button', { name: '作为独立副本编辑', exact: true }).click(), 201)
  assert.notEqual(opened.record.id, original.id); assert.equal(opened.record.revision, 1)
  report.copy = { id: opened.record.id, revision: 1, name: opened.record.name, plan: opened.record.plan }
  await reader.getByText('已保存 r1', { exact: true }).waitFor({ timeout: 60000 }); await shot(reader, '04-independent-copy.png')
  await reader.getByRole('button', { name: /方程式 \(/ }).click()
  const parameters = Object.entries(opened.record.plan.parameters), x = parameters.find(([key]) => key.endsWith('_x'))
  assert.ok(x, 'import translation remains editable')
  await reader.getByLabel(x[0] + ' 数值或表达式', { exact: true }).fill('10')
  const changed = await finish(reader)
  assert.equal(changed.id, opened.record.id); assert.equal(changed.revision, 2); assert.equal(changed.plan.parameters[x[0]].value, 10)
  report.modified = { id: changed.id, revision: changed.revision, inspection: changed.inspection, plan: changed.plan }
  const modifiedDownload = reader.waitForEvent('download'); await reader.getByRole('button', { name: '导出 STP', exact: true }).click()
  const exported = await modifiedDownload; await exported.saveAs(resolve(output, 'modified-copy.step')); assert.equal(await exported.failure(), null)
  await shot(reader, '05-edited-copy.png')
  await author.setViewportSize({ width: 390, height: 844 }); await shot(author, '06-mobile-community.png')
  report.mobile = await author.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }))
  assert.ok(report.mobile.scrollWidth <= report.mobile.width + 1, 'mobile page has no horizontal overflow')
  await author.setViewportSize({ width: 1440, height: 980 })
  await author.getByRole('button', { name: '撤下此资源', exact: true }).click()
  await post(author, `/api/cad/community/${resource.id}/withdraw`, () => author.getByRole('button', { name: '确认撤下', exact: true }).click())
  withdrawn = true; await shot(author, '07-resource-withdrawn.png')
  const unavailable = await reader.request.get('http://127.0.0.1:8016/api/cad/community/' + resource.id + '/artifacts/step')
  assert.equal(unavailable.status(), 404); report.checks.withdrawRejectsNewDownload = true
  await reader.reload(); await reader.getByText('已保存 r2', { exact: true }).waitFor({ timeout: 60000 })
  await shot(reader, '08-independent-copy-survives-withdrawal-refresh.png')
  report.checks.copySurvivesWithdrawal = true; report.checks.crossAccountCopy = true; report.checks.parameterEditCommitted = true
  assert.deepEqual(errors, [])
  report.files = Object.fromEntries(await Promise.all(['published.step', 'modified-copy.step'].map(async name => { const bytes = await readFile(resolve(output, name)); return [name, { sizeBytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }] })))
} catch (error) {
  report.failure = error.message
  for (let i = 0; i < pages.length; i++) await shot(pages[i], `failure-${i}.png`).catch(() => {})
  throw error
} finally {
  // Withdraw only this script's synthetic resource through its actual author UI.
  if (resource && !withdrawn && pages[0]) {
    try {
      const page = pages[0]; await page.setViewportSize({ width: 1440, height: 980 })
      if (await page.getByRole('button', { name: '撤下此资源', exact: true }).count()) await page.getByRole('button', { name: '撤下此资源', exact: true }).click()
      await post(page, `/api/cad/community/${resource.id}/withdraw`, () => page.getByRole('button', { name: '确认撤下', exact: true }).click())
      withdrawn = true
    } catch { report.cleanupRequiresWithdrawal = resource.id }
  }
  report.withdrawn = withdrawn; await writeFile(resolve(output, 'browser-verification.json'), JSON.stringify(report, null, 2))
  await Promise.all(contexts.map(context => context.close())); await browser.close()
}
