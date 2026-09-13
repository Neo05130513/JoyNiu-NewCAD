import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let server, Content
before(async () => {
  server = await createServer({ configFile: false, server: { middlewareMode: true, hmr: false, watch: null }, appType: 'custom' })
  Content = (await server.ssrLoadModule('/src/AdminOperationsWorkspace.jsx')).AdminOperationsContent
})
after(async () => { await server?.close() })
const render = data => renderToStaticMarkup(React.createElement(Content, { section: 'system', data, params: {}, user: { roles: ['admin'], permissions: ['*'], active: true } }))

test('system distinguishes host monitoring local backup and unavailable external delivery', () => {
  const html = render({ monitoring: { available: true, configured: true, generatedAt: '2026-09-12T10:00:00Z', status: 'warning', message: '本地监控已接入' },
    backup: { available: true, automaticEnabled: true, managedSets: 3, lastBackupAt: '2026-09-12T09:00:00Z', message: '已读取本机备份校验报告；异地副本尚未接入。' },
    hostOperations: { dataDisk: { freeBytes: 10 * 1024 ** 3, usedPercent: 83 }, jobs: { providerFailuresLastHour: 3 } },
    externalNotifications: { available: false, message: '尚未配置外部告警通知' }, offsiteBackup: { available: false, message: '尚未接入并验证异地备份目标' } })
  for (const text of ['宿主持续监控', '本机备份校验', '异地备份', '外部告警通知', '83%', '10.00 GB', '空闲后执行', '有预警', '尚未接入并验证异地备份目标']) assert.ok(html.includes(text), text)
  assert.doesNotMatch(html, /备份成功|监控正常|NaN/)
})

test('expired host report keeps its age warning next to historical values', () => {
  const html = render({ monitoring: { available: false, configured: true, stale: true, status: 'stale' }, backup: { available: false, stale: true }, hostOperations: { dataDisk: { usedPercent: 83 } } })
  assert.match(html, /报告过期/)
  assert.match(html, /以下数值不能作为实时状态/)
  assert.match(html, /83%/)
})
