// Stable source-processing failures also occur in saved runs from older servers.
// Keep these separate from model transport, authentication and usage failures.
const failures = {
  invalid_dwg_input: ['DWG 文件格式无法识别', '无法识别这份 DWG 文件的格式。请用 CAD 软件确认文件可以打开，重新保存 DWG 后上传，或导出清晰的 PDF/PNG。'],
  dwg_input_too_large: ['DWG 文件过大', 'DWG 文件超过解析大小限制。请只导出需要建模的零件图，或上传对应的 PDF/PNG。'],
  dwg_converter_unavailable: ['DWG 转换服务暂不可用', '服务器的 DWG 转换组件暂不可用。请联系管理员处理，或先上传这张图纸导出的 PDF/PNG。'],
  dwg_conversion_timeout: ['DWG 转换超时', '这份 DWG 未能在限定时间内完成转换。请只导出需要建模的零件图，或上传对应的 PDF/PNG；如仍失败，请联系管理员。'],
  dwg_conversion_failed: ['DWG 转换失败', '服务器未能转换这份 DWG。请用 CAD 软件重新保存 DWG 后上传，或导出清晰的 PDF/PNG；也可联系管理员检查文件兼容性。'],
  dxf_parser_unavailable: ['DWG 图纸解析服务暂不可用', '服务器的 DWG 图纸解析组件暂不可用。请联系管理员处理，或先上传这张图纸导出的 PDF/PNG。'],
  dxf_parse_failed: ['DWG 图纸数据解析失败', 'DWG 转换后的图纸数据无法读取，本轮建模尚未开始。请用 CAD 软件重新保存 DWG 后上传，或导出清晰的 PDF/PNG；也可联系管理员检查文件兼容性。'],
  dxf_render_failed: ['DWG 图纸预览生成失败', 'DWG 已转换，但无法生成供识图使用的图纸预览。请导出清晰的 PDF/PNG 后上传，或联系管理员检查文件兼容性。'],
  dwg_resource_limit_exceeded: ['DWG 图纸内容超出处理上限', '这份 DWG 的图元数量或转换结果超出处理上限。请只导出需要建模的零件图，或上传对应的 PDF/PNG。'],
  dwg_preprocessor_unavailable: ['DWG 处理服务暂不可用', '服务器的 DWG 处理组件暂不可用。请联系管理员处理，或先上传这张图纸导出的 PDF/PNG。'],
  dwg_preprocess_failed: ['DWG 图纸处理失败', '这份 DWG 暂时无法处理。请联系管理员检查，或上传由原图导出的清晰 PDF/PNG。'],
}

export function drawingPreprocessFailure(code) {
  if (!Object.hasOwn(failures, code)) return null
  const [label, message] = failures[code]
  return { label, message }
}
