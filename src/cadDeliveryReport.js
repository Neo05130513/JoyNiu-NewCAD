import { cadExplicitMaterial, cadGenerationIsCurrent, cadParameterRows } from './cadAgentState.js'

const array = value => Array.isArray(value) ? value : []
const finite = value => typeof value === 'number' && Number.isFinite(value)
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]))
const dateText = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录'
const viewNames = { front: '主视图', top: '俯视图', right: '右视图', left: '左视图', bottom: '仰视图', rear: '后视图', isometric: '轴测图' }
const checkNames = { bbox_size: '包络尺寸', solid_count: '实体数量', cylinder: '圆柱特征', cylinder_group: '圆柱特征组', ray_intervals: '实体截面材料区间' }
const detailNames = { count: '数量', diameter: '直径', radius: '半径', axis: '轴向', center: '中心', origin: '原点', cylinders: '圆柱记录', size: '尺寸', min: '下界', max: '上界', length: '长度', width: '宽度', height: '高度' }

export function cadDeliveryReportAvailability({ model, generation, drawingJob } = {}) {
  const run = model?.agentRun
  const blocked = !model || model.kind !== 'feature_model' || !run?.runId ? '当前文件没有可导出的 CAD 任务结果。'
    : drawingJob?.cadTask ? '本轮任务仍有未处理的提交记录，请先取得后台结果。'
      : run.status !== 'ready' ? '当前模型尚未确认，请核对并确认后导出交付报告。'
        : run.dirty || run.stale || model.stale || generation?.stale ? '当前参数已修改或实体已过期，请重新检查并确认。'
          : run.deliveryBlockedReason || generation?.deliveryBlockedReason ? '当前模型交付被阻止，请先处理核对问题。'
            : !cadGenerationIsCurrent(model, generation) || generation.previewOnly || generation.validation?.productionReady !== true ? '当前交付实体与模型版本不一致，请重新检查并确认。'
              : !array(generation.artifacts).some(item => String(item.format).toLowerCase() === 'step' && item.productionReady !== false) ? '当前版本没有已确认的 STEP 交付记录。' : ''
  return { allowed: !blocked, reason: blocked }
}

function sourceText(source) {
  if (typeof source === 'string') return source
  if (!source || typeof source !== 'object') return '未记录来源'
  const parts = []
  if (['manual', 'user'].includes(source.type)) parts.push('人工输入或修改')
  if (source.drawingSource) parts.push(`原图依据：${sourceText(source.drawingSource)}`)
  if (source.filename || source.fileName) parts.push(source.filename || source.fileName)
  if (source.view) parts.push(viewNames[source.view] || source.view)
  if (Number.isInteger(source.pageNumber)) parts.push(`第 ${source.pageNumber} 页`)
  else if (Number.isInteger(source.pageIndex)) parts.push(`第 ${source.pageIndex + 1} 页`)
  for (const key of ['text', 'description', 'evidence', 'location', 'endpointsOrDatum']) if (typeof source[key] === 'string' && source[key].trim()) parts.push(source[key])
  return [...new Set(parts)].join(' · ') || '未记录文字来源'
}

function readableValue(value, depth = 0) {
  if (value === null || value === undefined) return '未记录'
  if (finite(value)) return String(value)
  if (typeof value === 'string' || typeof value === 'boolean') return String(value)
  if (depth > 4) return '详见原始检查记录'
  if (Array.isArray(value)) return value.map(item => readableValue(item, depth + 1)).join('；')
  if (typeof value === 'object') return Object.entries(value).filter(([key]) => Object.hasOwn(detailNames, key)).map(([key, item]) => `${detailNames[key]}：${readableValue(item, depth + 1)}`).join('，') || '存在结构化记录，需回工作台核对'
  return '未记录'
}

function hasMeasurement(value) {
  if (finite(value)) return true
  if (Array.isArray(value)) return value.length > 0 && value.some(hasMeasurement)
  if (value && typeof value === 'object') return Object.entries(value).some(([key, item]) => Object.hasOwn(detailNames, key) && hasMeasurement(item))
  return false
}

export function cadDeliveryReportData(options = {}) {
  const { model, generation, drawingJob, candidate = false } = options
  const availability = cadDeliveryReportAvailability(options)
  if (!availability.allowed && !candidate) throw new Error(availability.reason)
  if (!model?.agentRun?.runId) throw new Error('当前文件没有 CAD 任务记录，无法生成核对报告。')
  const run = model.agentRun
  const parameters = cadParameterRows(model.cadPlan).map(row => {
    const derived = Boolean(row.expression)
    return { key: row.key, label: row.label, unit: row.unit || '无量纲', kind: derived ? '派生值' : '名义值',
      value: derived ? (!run.dirty && !run.stale && !model.stale && !generation?.stale && (!generation || cadGenerationIsCurrent(model, generation)) && finite(run.resolvedParameters?.[row.key]) ? run.resolvedParameters[row.key] : null) : finite(row.value) ? row.value : null,
      expression: derived ? row.expression : '', source: sourceText(row.source), question: row.question || '' }
  })
  const checks = array(run.inspection?.acceptance?.checks).map((check, index) => {
    const measured = hasMeasurement(check.actual)
    return { id: check.id || `check-${index + 1}`, label: check.label || checkNames[check.kind] || `检查项 ${index + 1}`,
      kind: checkNames[check.kind] || check.kind || '记录的要求', measured,
      status: !measured ? '未取得实测值' : check.passed === true ? '已测，符合记录依据' : check.passed === false ? '已测，与记录依据不符' : '已测，待判读',
      expected: readableValue(check.expected), actual: readableValue(check.actual), source: sourceText(check.source), message: check.message || '' }
  })
  const sources = array(run.sourceFiles).map(file => ({ filename: file.filename || file.name || '未命名原图', sha256: typeof file.sha256 === 'string' ? file.sha256 : '', contentType: file.contentType || '' }))
  if (!sources.length && drawingJob?.fileMeta?.name) sources.push({ filename: drawingJob.fileMeta.name, sha256: '', contentType: drawingJob.fileMeta.type || '', localMetadataOnly: true })
  const questions = [...new Set([...array(run.questions), ...parameters.map(row => row.question), ...array(run.drawingReview?.differences)].filter(value => typeof value === 'string' && value.trim()))]
  const confirmedBy = options.confirmedBy ?? run.confirmedBy
  const confirmationName = typeof confirmedBy === 'string' ? confirmedBy : confirmedBy?.displayName || confirmedBy?.display_name || confirmedBy?.name || confirmedBy?.id || '未记录'
  return {
    title: model.name || model.cadPlan?.name || 'CAD 模型', candidate: candidate || !availability.allowed, current: availability.allowed, blockingReason: availability.reason,
    runId: run.runId, revision: run.revision, status: run.status, material: cadExplicitMaterial(model) || '未明确指定', units: model.cadPlan?.units || model.units || '未记录',
    confirmedBy: confirmationName, confirmedAt: options.confirmedAt ?? run.confirmedAt ?? run.drawingReview?.humanConfirmedAt ?? null,
    exportedAt: options.exportedAt || new Date().toISOString(), parameters, sources, questions, checks,
    geometry: { recorded: Boolean(run.inspection), kernelBacked: run.inspection?.kernelBacked === true, valid: run.inspection?.valid === true, engine: run.inspection?.engine || '',
      solidCount: run.inspection?.solidCount, size: run.inspection?.bbox?.size, volume: run.inspection?.volumeMm3 },
    projections: array((run.projectionComparison || run.drawingReview?.projectionComparison)?.views).map(view => ({ name: viewNames[view.view] || view.view || '未命名视图',
      status: view.status === 'supported' ? '所测轮廓得到支持' : view.status === 'mismatch' ? '发现轮廓差异' : '核对范围不足', reliability: view.reliability === 'high' ? '高' : view.reliability === 'low' ? '低' : '未明确' })),
    artifactFormats: [...new Set(array(generation?.artifacts).map(item => String(item.format || '').toUpperCase()).filter(Boolean))],
  }
}

const table = (headings, rows, empty) => rows.length ? `<div class="table-scroll"><table><thead><tr>${headings.map(value => `<th>${escapeHtml(value)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${escapeHtml(value)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : `<p class="muted">${escapeHtml(empty)}</p>`
const items = values => `<ul>${values.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul>`

export function createCadDeliveryReport(options) {
  const report = cadDeliveryReportData(options)
  const kind = report.candidate ? '候选核对报告 · 尚未确认交付' : '已确认模型核对报告'
  const nominal = report.parameters.filter(row => row.kind === '名义值')
  const derived = report.parameters.filter(row => row.kind === '派生值')
  const measured = report.checks.filter(check => check.measured)
  const unmeasured = report.checks.filter(check => !check.measured)
  const geometryText = report.geometry.recorded ? `${report.geometry.kernelBacked ? '使用几何内核' : '未记录几何内核支持'}；${report.geometry.valid ? '几何有效性检查通过' : '几何有效性未通过或未记录'}；实体数量：${readableValue(report.geometry.solidCount)}` : '未记录实体几何检查。'
  const sections = [
    `<section><h2>版本与确认记录</h2>${table(['项目', '记录'], [['任务编号', report.runId], ['模型版本', report.revision], ['报告类别', kind], ['确认人', report.confirmedBy], ['确认时间', dateText(report.confirmedAt)], ['报告导出时间', dateText(report.exportedAt)], ['材料', report.material], ['模型单位', report.units], ['已记录的实体与视图格式', report.artifactFormats.join('、') || '无']], '')}<p class="muted">确认人与确认时间来自已保存的确认记录；缺失信息显示为“未记录”。报告导出时间不代表模型确认时间。</p></section>`,
    `<section><h2>名义参数</h2><p>这些值为当前建模输入，不等同于实体上每一处尺寸都已实测。</p>${table(['参数名称', '参数标识', '名义值', '单位', '来源 / 未决说明'], nominal.map(row => [row.label, row.key, row.value ?? '未确定', row.unit, [row.source, row.question].filter(Boolean).join('；')]), '未记录名义参数。')}</section>`,
    `<section><h2>派生尺寸</h2><p>派生值取自当前版本服务端保存的计算结果，报告不在浏览器重新求值。缺少结果或参数已修改时显示待计算。</p>${table(['参数名称', '参数标识', '计算结果', '单位', '尺寸关系', '来源'], derived.map(row => [row.label, row.key, row.value ?? '待计算 / 未记录', row.unit, row.expression, row.source]), '未记录派生尺寸。')}</section>`,
    `<section><h2>原图与输入来源</h2>${table(['原图文件', '类型', '文件 SHA-256', '保存记录'], report.sources.map(source => [source.filename, source.contentType || '未记录', source.sha256 || '未记录', source.localMetadataOnly ? '仅有本地文件名，不能据此确认原图已上传' : '服务端原图记录']), '当前结果没有已记录的原图；请按文字要求或参数来源核对。')}<p class="muted">此 HTML 保存来源文字和文件指纹，不嵌入原图、实体文件、登录凭据或带权限的下载链接。原图细节请回工作台核对。</p></section>`,
    `<section><h2>几何检查与实际测量范围</h2><p>${escapeHtml(geometryText)}</p><p>包络尺寸：${escapeHtml(readableValue(report.geometry.size))} ${escapeHtml(report.units)}；体积：${escapeHtml(readableValue(report.geometry.volume))} mm³。上述数据属于几何记录，不能替代图纸逐项核对。</p><p class="muted">检查项中的长度采用模型单位 ${escapeHtml(report.units)}；数量和轴向分量为无量纲值。</p><h3>已取得实测值的要求（${measured.length} 项）</h3>${table(['检查项', '方法', '记录的要求', '实体实测', '结果', '来源 / 说明'], measured.map(check => [check.label, check.kind, check.expected, check.actual, check.status, [check.source, check.message].filter(Boolean).join('；')]), '没有已取得实测值的要求记录。')}<h3>尚未取得实测值（${unmeasured.length} 项）</h3>${table(['检查项', '记录的要求', '状态', '来源 / 说明'], unmeasured.map(check => [check.label, check.expected, check.status, [check.source, check.message].filter(Boolean).join('；')]), '记录的检查清单中没有标记为缺少实测值的项目；这不代表图纸所有特征都已检查。')}<p class="scope">实测结论仅覆盖表内记录的要求与测量方法。未列出的尺寸、未覆盖的截面、局部特征、公差配合、表面要求和材料工艺仍需按原图与工程要求核对。</p></section>`,
    `<section><h2>投影核对</h2>${table(['视图', '当前范围内结论', '记录可靠性'], report.projections.map(view => [view.name, view.status, view.reliability]), '未记录投影核对结果。')}<p class="muted">轮廓支持只说明已测轮廓范围，不表示整张图纸全部尺寸与特征一致。</p></section>`,
    `<section><h2>未决问题</h2>${report.questions.length ? items(report.questions) : '<p>当前结果未列出未决问题。未列出问题不代表完成了未记录的检查。</p>'}</section>`,
    `<section><h2>使用范围</h2><p>本报告是模型版本、来源及核对记录的离线副本，不是制造合格证明或加工放行文件。投入制造前，请由相应责任人员核对原图、尺寸、公差、材料、工艺与生产适用性。</p><p class="muted">如模型参数、原图或实体版本已变更，请重新检查、确认并导出新的报告。</p></section>`,
  ]
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>${escapeHtml(report.title)} · ${kind}</title><style>html{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f2f5fa;color:#233d59}body{max-width:1120px;margin:28px auto;padding:0 22px;font-size:13px;line-height:1.85}header{padding:26px 30px;background:#173e73;color:#fff;border-radius:12px}header p{margin:4px 0;opacity:.85}h1{font-size:27px;line-height:1.4;margin:9px 0}h2{font-size:19px;margin:0 0 12px;color:#244d7d}h3{font-size:14px;margin:24px 0 10px}section{background:#fff;padding:25px 28px;margin:18px 0;border:1px solid #dce5ef;border-radius:10px;break-inside:avoid}.eyebrow{font-size:10px;letter-spacing:2px}.badge{display:inline-block;font-weight:650;font-size:12px;background:#ffffff20;padding:3px 10px;border-radius:5px}.candidate{background:#7b4930}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;border:1px solid #dfe7f1;padding:10px 12px;vertical-align:top;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#f0f5fb;color:#4f6d90;font-weight:600}td{min-width:70px}.muted{color:#75869c;font-size:11px}.scope{border-left:3px solid #dcaa55;padding:10px 14px;background:#fff8eb;color:#76562c}footer{color:#8494aa;text-align:center;font-size:11px;margin:24px}li{padding:3px 0}@media(max-width:650px){body{margin:14px auto;padding:0 12px}header,section{padding:20px 16px}h1{font-size:22px}table{min-width:550px}}@media print{html{background:white}body{max-width:none;margin:0;padding:0;font-size:10pt}header{color:#233d59;background:white!important;border:1px solid #cbd8e7}section{padding:16px 0;border:0;border-top:1px solid #ccd8e8;border-radius:0;break-inside:auto}h2,h3{break-after:avoid}.table-scroll{overflow:visible}table{font-size:8pt;min-width:0!important}tr{break-inside:avoid}thead{display:table-header-group}th,td{padding:6px}footer{margin:12px}}@page{size:A4;margin:15mm}</style></head><body><header class="${report.candidate ? 'candidate' : ''}"><span class="eyebrow">JOYNIU CAD · MODEL REVIEW RECORD</span><h1>${escapeHtml(report.title)}</h1><span class="badge">${kind}</span><p>第 ${escapeHtml(report.revision ?? '未记录')} 版 · ${escapeHtml(report.runId)}</p>${report.blockingReason ? `<p>${escapeHtml(report.blockingReason)}</p>` : ''}</header><main>${sections.join('')}</main><footer>JoyNiu CAD · 本报告可离线查看或使用浏览器打印保存为 PDF。</footer></body></html>`
  const safeName = String(report.title).replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 80) || 'CAD模型'
  return { filename: `${safeName}-第${Number.isSafeInteger(report.revision) ? report.revision : '未知'}版-${report.candidate ? '候选' : '已确认'}核对报告.html`, html }
}

export function downloadCadDeliveryReport(options) {
  const report = createCadDeliveryReport(options)
  const url = URL.createObjectURL(new Blob([report.html], { type: 'text/html;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = report.filename
  document.body.appendChild(anchor)
  try { anchor.click() } finally { anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
  return report
}
