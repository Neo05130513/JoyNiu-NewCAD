export function photoModelingPrompt({points,referenceLength,width,height,knownDimensions,description,plane='正视参考平面',filename}){
  if(!Array.isArray(points)||points.length!==2||points.some(p=>!Array.isArray(p)||p.length!==2||p.some(v=>!Number.isFinite(v)||v<0||v>1)))throw new Error('请在主照片上标记两个尺度参考点')
  if(!Number.isFinite(width)||!Number.isFinite(height)||width<1||height<1)throw new Error('请等待主照片加载完成')
  const pixels=Math.hypot((points[1][0]-points[0][0])*width,(points[1][1]-points[0][1])*height)
  if(!Number.isFinite(referenceLength)||referenceLength<=0||referenceLength>1e6||pixels<3)throw new Error('参考尺寸须大于零，两个参考点须分开至少 3 像素')
  if(!description?.trim())throw new Error('请描述零件用途、外形和需要保留的结构')
  return `请根据所附零件外形照片建立可编辑参数化 CAD 模型。来源类型为外形照片，不是带完整尺寸的工程图。\n主照片：${filename}；分辨率 ${width}×${height}。\n用户指定尺度参考平面：${plane}。主照片的参考点（从左上角计，坐标除以图像宽高归一化）为 ${JSON.stringify(points)}，两点间实际距离由用户给定为 ${referenceLength} mm；原图参考段为 ${pixels.toFixed(3)} 像素。该尺度仅用于同一平面内的初步比例估算，不保证透视、镜头畸变或深度方向正确。\n已知尺寸与结构：${knownDimensions?.trim()||'除参考段外尚未提供其他精确尺寸。'}\n建模要求：${description.trim()}\n先识别可见轮廓、轴线、孔槽和不同照片之间的对应关系。保留用户给出的精确尺寸为 user 来源；按比例推算的尺寸明确记录估算依据，不将它们标为照片原生标注。无法从照片确定的背面、孔深、壁厚等形成具体问题，不静默补上默认几何。完成真实实体后逐视图核对，保留待确认事项，并允许继续对话修正。`
}

export function addPhotoFiles(current, chosen) {
  const next = [...current]
  for (const file of chosen) {
    if (!/^image\/(png|jpeg|webp|gif|bmp)$/.test(file.type || '') && !(!file.type && /\.(png|jpe?g|webp|gif|bmp)$/i.test(file.name))) throw new Error('请选择 PNG、JPEG、WebP、GIF 或 BMP 图片')
    if (!file.size || file.size > 20 * 1024 * 1024) throw new Error('照片不能为空，每张不能超过 20 MB')
    if (!next.some(item => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) next.push(file)
  }
  if (next.length > 4) throw new Error('最多添加 4 张照片，请先移除不需要的照片')
  return next
}
export function setPhotoPixelCoordinate(points, index, coordinate, text, {width, height}) {
  if (![0, 1].includes(index) || ![0, 1].includes(coordinate)) throw new Error('参考点坐标无效')
  const maximum = coordinate ? height : width, value = String(text).trim() === '' ? null : Number(text)
  if (!Number.isFinite(maximum) || maximum <= 0 || (value !== null && (!Number.isFinite(value) || value < 0 || value > maximum))) throw new Error(`坐标须在 0 到 ${maximum} 像素之间`)
  const result = [points[0] ? [...points[0]] : [null, null], points[1] ? [...points[1]] : [null, null]]
  result[index][coordinate] = value === null ? null : value / maximum
  return result
}
