// These names describe parameter identifiers, not any particular drawing. Keep
// them in the presentation layer so saved formulas and source bindings stay valid.
const labels = {
  length: '长度', width: '宽度', height: '高度', depth: '深度', thickness: '厚度',
  radius: '半径', diameter: '直径', angle: '角度', count: '数量',
  totallength: '总长', totalwidth: '总宽', totalheight: '总高', totaldepth: '总深度',
  lefttransitiontotal: '左侧过渡段总长', righttransitiontotal: '右侧过渡段总长', leftbosstotal: '左侧凸台段总长',
  xtotal: '总长终点 X 坐标',
  holedepthfromtop: '顶部至孔中心距离', holeheight: '孔中心高度', holecenterheight: '孔中心高度',
  holediameter: '孔径', holedepth: '孔深', holepitch: '孔中心距', halfwidth: '半宽',
  platethickness: '板厚', frontthickness: '前端壁厚', verticalthickness: '竖臂厚度',
  armwidth: '臂宽', armthickness: '臂厚', upperradius: '上部圆弧半径', lowerradius: '下部圆弧半径',
  innerradius: '内圆弧半径', outerradius: '外圆弧半径', openingradius: '开口半径',
  frontholediameter: '前端孔径', upperholediameter: '上部孔径',
  shoulderheight: '肩部高度', shoulderdistancesquared: '肩部距离的平方',
  tangentoffsety: '切点 Y 向偏移', tangentoffsetz: '切点 Z 向偏移',
  mountthickness: '安装耳厚度', mountradius: '安装耳圆弧半径', mountholediameter: '安装孔径',
  mountpitch: '安装孔中心距', mountpitchx: '安装孔 X 向中心距', mountpitchy: '安装孔 Y 向中心距',
  mountrearoffset: '后排安装孔距后端距离', mountfirsty: '第一排安装孔 Y 坐标',
  mountsecondy: '第二排安装孔 Y 坐标', mountrearx: '后排安装孔 X 坐标', mountfrontx: '前排安装孔 X 坐标',
  mountcentery: '安装孔中心 Y 坐标', mountrootx: '安装耳根部 X 坐标',
  slotdiameter: '槽直径', slotradius: '槽端圆弧半径', slotpitch: '槽两端圆心距', slotangle: '槽倾角',
  slotaxisy: '槽轴线 Y 坐标', slotaxisz: '槽轴线 Z 坐标', slottoollength: '开槽辅助工具长度',
  earradius: '耳板圆弧半径', earthickness: '耳板厚度', eargap: '耳板间距',
  earbottom: '耳板下缘高度', earcenterheight: '耳孔中心高度', earholediameter: '耳板孔径',
  eartotalwidth: '耳板总宽',
  frontearthickness: '前耳板厚度', rearearthickness: '后耳板厚度',
  earoffset: '耳板偏移距离', earrootx: '耳板根部 X 坐标', earx: '耳板 X 坐标',
  mouthwidth: '开口宽度', mouthdrop: '开口下沉深度', lowersetback: '下部退让距离',
  sloperun: '斜面水平跨度', slopedrop: '斜面高差', frontrun: '前斜面水平跨度', rearrun: '后斜面水平跨度',
  frontrise: '前斜面高差', frontslopelength: '前斜面长度', frontridgey: '前斜面上缘 Y 坐标',
  holemouthoffset: '孔口偏移距离', holemouthy: '孔口中心 Y 坐标', holemouthz: '孔口中心 Z 坐标',
  loweraxisdrop: '下部轴线下降距离', upperaxisdistance: '上部轴线距离',
  upperheadrise: '上部圆头抬高量', upperheadheight: '上部圆头高度',
  lowerarcjoinz: '下部圆弧连接点 Z 坐标', outerarcmidx: '外圆弧中点 X 坐标', outerarcmidz: '外圆弧中点 Z 坐标',
  ribwidth: '加强筋宽度', ribrise: '加强筋抬高量', ribcenterrun: '加强筋中心跨度',
  ribendx: '加强筋端点 X 坐标', ribendz: '加强筋端点 Z 坐标',
  tangentsine: '切线角正弦值', tangentcosine: '切线角余弦值',
}

const words = {
  length: '长度', width: '宽度', height: '高度', depth: '深度', thickness: '厚度',
  radius: '半径', diameter: '直径', angle: '角度', inclination: '倾角', rotation: '旋转角',
  distance: '距离', offset: '偏移', pitch: '中心距', gap: '间距', clearance: '间隙',
  base: '底座', body: '主体', plate: '板', wall: '壁', arm: '臂', ear: '耳板', mount: '安装部',
  boss: '凸台', bore: '内孔', hole: '孔', slot: '槽', groove: '槽', rib: '加强筋', clamp: '夹口',
  pad: '垫台', platform: '平台', shoulder: '肩部', shaft: '轴', flange: '法兰', head: '头部',
  recess: '凹槽', pocket: '凹腔', opening: '开口', fillet: '圆角', chamfer: '倒角',
  left: '左侧', right: '右侧', front: '前端', rear: '后端', top: '顶部', bottom: '底部',
  upper: '上部', lower: '下部', inner: '内侧', outer: '外侧', central: '中央', mid: '中间',
  vertical: '竖向', horizontal: '横向', total: '总计', half: '半',
  large: '大径段', narrow: '窄段', small: '小径段', transition: '过渡段',
  center: '中心', centre: '中心', axis: '轴线', root: '根部', start: '起点', end: '端部',
  first: '第一', second: '第二', third: '第三', radial: '径向', axial: '轴向',
  projection: '凸出量', setback: '退让距离', run: '水平跨度', drop: '下降量', rise: '抬高量',
  arc: '圆弧', join: '连接点', tangent: '切点', squared: '平方', sine: '正弦值', cosine: '余弦值',
  tool: '辅助工具', count: '数量', number: '数量', ratio: '比例', scale: '缩放比例',
  tolerance: '公差', deg: '（度）', degree: '（度）', degrees: '（度）', rad: '（弧度）', radians: '（弧度）',
}

function tokens(value) {
  return String(value ?? '').trim()
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/([a-z\d])([A-Z])/g, '$1 $2')
    .split(/[\s_.-]+/).filter(Boolean).map((word) => word.toLowerCase())
}

function translatedName(value) {
  const parts = tokens(value)
  if (!parts.length) return ''
  const label = Object.hasOwn(labels, parts.join('')) ? labels[parts.join('')] : ''
  if (label) return label

  // Axis/radius prefixes are common in generated construction coordinates.
  const axis = ['x', 'y', 'z'].includes(parts.at(-1)) ? parts.pop()
    : ['x', 'y', 'z'].includes(parts[0]) ? parts.shift() : null
  const radius = parts[0] === 'r' && parts.length > 1
  if (radius) parts.shift()
  if (!parts.length || parts.some((part) => !Object.hasOwn(words, part) && !/^\d+$/.test(part))) return ''
  const phrase = parts.map((part) => words[part] || part).join('')
  if (axis && ['length', 'width', 'height', 'distance', 'offset', 'pitch'].includes(parts.at(-1))) return `${axis.toUpperCase()} 向${phrase}`
  return `${phrase}${radius ? '半径' : ''}${axis ? ` ${axis.toUpperCase()} 坐标` : ''}`
}

export function cadParameterLabel(key, row = {}, index = 0) {
  row ||= {}
  const candidates = [row.label, row.name, key].filter((value) => typeof value === 'string' && value.trim())
  // Human labels win even when another metadata field still holds an English ID.
  const chinese = candidates.find((value) => /\p{Script=Han}/u.test(value))
  if (chinese) return chinese
  for (const candidate of candidates) {
    const label = translatedName(candidate)
    if (label) return label
  }
  const number = Number.isInteger(index) && index >= 0 ? index + 1 : 1
  return `参数 ${number}`
}

export function cadParameterUnit(key, row = {}, defaultUnit = 'mm') {
  row ||= {}
  // Empty units are intentional for dimensionless parameters.
  const explicit = row.unit ?? row.units
  if (typeof explicit === 'string') return explicit
  const parts = tokens(key)
  const expression = typeof row.expression === 'string' ? row.expression.trim() : ''
  if (parts.some((part) => ['sine', 'cosine', 'ratio', 'scale', 'count', 'number'].includes(part))) return ''
  if (parts.some((part) => ['rad', 'radian', 'radians'].includes(part)) || /^radians\([^()]*\)$/.test(expression)) return 'rad'
  if (parts.some((part) => ['angle', 'inclination', 'rotation', 'deg', 'degree', 'degrees'].includes(part))
    || [key, row.label, row.name].some((value) => typeof value === 'string' && /(角度|夹角|倾角|旋转角)$/.test(value))) return '°'
  // A source paragraph can mention angles while describing a length. Only a
  // standalone degree measurement is safe to use without a semantic angle key.
  const sourceText = typeof row.source === 'string' ? row.source : row.source?.text
  if (!expression && typeof sourceText === 'string' && /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*°$/.test(sourceText.trim())) return '°'
  if (parts.includes('squared') && parts.some((part) => ['distance', 'length', 'width', 'height', 'radius', 'diameter'].includes(part))) return defaultUnit ? `${defaultUnit}²` : ''
  return defaultUnit
}
