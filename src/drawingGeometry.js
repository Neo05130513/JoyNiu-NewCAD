import { canonicalModelKind, validateModelParameters } from './modelValidation.js'

export const drawingLayers = [
  { id: 'OBJECT', label: '可见轮廓', color: '#bfd0e3', dxfColor: 7 },
  { id: 'HIDDEN', label: '内部与遮挡特征', color: '#7a93ae', dxfColor: 8, dash: '3 2' },
  { id: 'CENTER', label: '中心线', color: '#62b6d7', dxfColor: 4, dash: '7 2 1 2' },
  { id: 'DIM', label: '尺寸与引线', color: '#e8b66c', dxfColor: 2 },
  { id: 'SECTION', label: '剖面线', color: '#8495aa', dxfColor: 8 },
  { id: 'INSERT', label: '独立镶件', color: '#ad9de4', dxfColor: 6 },
  { id: 'TEXT', label: '视图标题', color: '#b4c5d8', dxfColor: 7 },
]
const round = (value) => Math.round(Number(value) * 1e6) / 1e6
export const formatDimension = (value) => Number(Number(value).toFixed(3)).toString()
export function drawingScaleFactor(scale) {
  const match = String(scale || '1:1').match(/^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/)
  return match && Number(match[1]) > 0 && Number(match[2]) > 0 ? Number(match[1]) / Number(match[2]) : 1
}

export function isDrawingKernelReady(generation, pending = false) {
  return generation?.validation?.valid === true && generation.validation.productionReady === true
    && !generation.stale && !generation.pendingDrawing && !pending
    && !['unavailable', 'recovering'].includes(generation.artifactStatus)
}

function makeView(id, title) {
  const entities = [], dimensions = []
  const add = (type, payload, layer = 'OBJECT', feature = '') => {
    const entity = { id: `${id}-${entities.length}`, view: id, type, layer, feature, ...payload }
    entities.push(entity)
    return entity
  }
  const line = (x1, y1, x2, y2, layer, feature) => add('line', { x1, y1, x2, y2 }, layer, feature)
  const circle = (cx, cy, r, layer, feature) => add('circle', { cx, cy, r }, layer, feature)
  const arc = (cx, cy, r, start, end, layer, feature) => add('arc', { cx, cy, r, start, end }, layer, feature)
  const poly = (points, closed = false, layer, feature) => add('polyline', { points, closed }, layer, feature)
  const rect = (x, y, w, h, layer, feature) => poly([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], true, layer, feature)
  const text = (x, y, value, layer = 'TEXT', height = 3) => add('text', { x, y, text: String(value), height }, layer)
  const center = (x1, y1, x2, y2) => line(x1, y1, x2, y2, 'CENTER', 'axis')
  const hDim = (x1, x2, y, fromY, value, field) => {
    line(x1, fromY, x1, y + 2, 'DIM'); line(x2, fromY, x2, y + 2, 'DIM'); line(x1, y, x2, y, 'DIM')
    for (const [x, sign] of [[x1, 1], [x2, -1]]) { line(x, y, x + sign * 2.5, y + 1, 'DIM'); line(x, y, x + sign * 2.5, y - 1, 'DIM') }
    text((x1 + x2) / 2, y + 2, value, 'DIM', 3)
    dimensions.push({ field, label: String(value), orientation: 'horizontal', value: round(Math.abs(x2 - x1)) })
  }
  const vDim = (y1, y2, x, fromX, value, field) => {
    line(fromX, y1, x + 2, y1, 'DIM'); line(fromX, y2, x + 2, y2, 'DIM'); line(x, y1, x, y2, 'DIM')
    for (const [y, sign] of [[y1, 1], [y2, -1]]) { line(x, y, x + 1, y + sign * 2.5, 'DIM'); line(x, y, x - 1, y + sign * 2.5, 'DIM') }
    text(x + 9, (y1 + y2) / 2, value, 'DIM', 3)
    dimensions.push({ field, label: String(value), orientation: 'vertical', value: round(Math.abs(y2 - y1)) })
  }
  const leader = (x, y, tx, ty, value, field) => {
    line(x, y, tx, ty, 'DIM'); line(tx, ty, tx + 10, ty, 'DIM'); text(tx + 10, ty + 2, value, 'DIM')
    dimensions.push({ field, label: String(value), orientation: 'leader' })
  }
  const hatch = (points, feature = '', layer = 'SECTION') => {
    // Clip 45-degree section lines against each material polygon.
    const sums = points.map(([x, y]) => y - x)
    for (let c = Math.floor(Math.min(...sums) / 4) * 4; c <= Math.max(...sums); c += 4) {
      const hits = []
      for (let i = 0; i < points.length; i += 1) {
        const a = points[i], b = points[(i + 1) % points.length]
        const da = a[1] - a[0] - c, db = b[1] - b[0] - c
        if ((da < 0 && db >= 0) || (db < 0 && da >= 0)) {
          const t = da / (da - db)
          hits.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])])
        }
      }
      hits.sort((a, b) => a[0] - b[0])
      for (let i = 0; i + 1 < hits.length; i += 2) line(...hits[i], ...hits[i + 1], layer, feature)
    }
  }
  const sectionPoly = (points, feature = '', layer = 'OBJECT') => { poly(points, true, layer, feature); hatch(points, feature, layer === 'INSERT' ? 'INSERT' : 'SECTION') }
  return { id, title, entities, dimensions, line, circle, arc, poly, rect, text, center, hDim, vDim, leader, hatch, sectionPoly }
}

function shaftViews(p) {
  const [L, R, r, w, depth, K] = [p.length, p.outerDiameter / 2, p.holeDiameter / 2, p.keywayWidth, p.keywayDepth, p.keywayLength]
  const floor = R - depth, edge = Math.sqrt(R * R - w * w / 4)
  const front = makeView('front', '主视图 · XZ'), top = makeView('top', '俯视图 · XY'), end = makeView('end', '左端视图 · YZ'), section = makeView('section', 'A–A 中心纵剖面')
  front.poly([[0, -R], [L, -R], [L, R], [K, R], [K, edge], [0, edge]], true, 'OBJECT', 'body')
  for (const y of [-r, r]) front.line(0, y, L, y, 'HIDDEN', 'bore')
  front.line(0, floor, K, floor, 'HIDDEN', 'keyway'); front.center(-5, 0, L + 5, 0)
  front.hDim(0, L, R + 12, R, `总长 ${formatDimension(L)}`, 'length')
  front.vDim(-R, R, L + 12, L, `Ø${formatDimension(2 * R)}`, 'outerDiameter')
  top.rect(0, -R, L, 2 * R, 'OBJECT', 'body'); top.rect(0, -w / 2, K, w, 'OBJECT', 'keyway')
  for (const y of [-r, r]) top.line(0, y, L, y, 'HIDDEN', 'bore')
  top.center(-5, 0, L + 5, 0); top.hDim(0, K, R + 12, w / 2, `键槽长 ${formatDimension(K)}`, 'keywayLength')
  top.vDim(-w / 2, w / 2, L + 12, K, `槽宽 ${formatDimension(w)}`, 'keywayWidth')
  const angle = Math.asin(w / (2 * R)) * 180 / Math.PI
  end.arc(0, 0, R, 90 + angle, 450 - angle, 'OBJECT', 'body')
  end.poly([[-w / 2, edge], [-w / 2, floor], [w / 2, floor], [w / 2, edge]], false, 'OBJECT', 'keyway')
  end.circle(0, 0, r, 'OBJECT', 'bore'); end.center(-R - 5, 0, R + 5, 0); end.center(0, -R - 5, 0, R + 5)
  end.leader(r / Math.sqrt(2), -r / Math.sqrt(2), R + 9, -R - 8, `通孔 Ø${formatDimension(2 * r)}`, 'holeDiameter')
  end.vDim(floor, R, -R - 13, -w / 2, `槽深 ${formatDimension(depth)}`, 'keywayDepth')
  section.sectionPoly([[0, r], [L, r], [L, R], [K, R], [K, floor], [0, floor]], 'keyway')
  section.sectionPoly([[0, -R], [L, -R], [L, -r], [0, -r]], 'body')
  section.center(-5, 0, L + 5, 0)
  section.leader(L * .65, r, L * .65, R + 14, `孔 Ø${formatDimension(2 * r)} 贯通`, 'holeDiameter')
  return [front, end, top, section]
}

function bracketViews(p) {
  const [L, W, T, U, V, H, R, O, sw, sl, depth, d, pitch] = [p.baseLength, p.baseWidth, p.baseThickness, p.upperLength, p.upperWidth, p.totalHeight, p.notchRadius, p.notchOpening, p.slotWidth, p.slotLength, p.pocketDepth, p.bossDiameter, p.bossCenterDistance]
  const front = makeView('front', '主视图 · XZ'), top = makeView('top', '俯视图 · XY'), end = makeView('end', '右视图 · YZ'), section = makeView('section', 'A–A 中心剖面 · Y = 0')
  front.poly([[-L / 2, 0], [L / 2, 0], [L / 2, T], [U / 2, T], [U / 2, H], [R, H]], false, 'OBJECT', 'body')
  front.arc(0, H, R, 180, 360, 'OBJECT', 'saddle')
  front.poly([[-R, H], [-U / 2, H], [-U / 2, T], [-L / 2, T], [-L / 2, 0]], false, 'OBJECT', 'body')
  for (const c of [-pitch / 2, pitch / 2]) for (const x of [c - d / 2, c + d / 2]) front.line(x, 0, x, Math.abs(x) <= U / 2 ? H : T, 'HIDDEN', 'through-hole')
  for (const sign of [-1, 1]) front.rect(sign < 0 ? -O / 2 : O / 2 - sw, H - depth, sw, depth, 'HIDDEN', 'pocket')
  front.hDim(-L / 2, L / 2, -12, 0, `底板 ${formatDimension(L)}`, 'baseLength'); front.vDim(0, H, L / 2 + 12, U / 2, `总高 ${formatDimension(H)}`, 'totalHeight')
  front.leader(0, H - R, -U / 2, H + 12, `鞍槽 R${formatDimension(R)}`, 'notchRadius')
  top.rect(-L / 2, -W / 2, L, W, 'OBJECT', 'base'); top.rect(-U / 2, -V / 2, U, V, 'OBJECT', 'upper')
  for (const c of [-pitch / 2, pitch / 2]) { top.circle(c, 0, d / 2, 'OBJECT', 'through-hole'); top.center(c, -W / 2 - 4, c, W / 2 + 4) }
  for (const x of [-R, R]) top.line(x, -V / 2, x, V / 2, 'OBJECT', 'saddle')
  for (const x of [-O / 2, O / 2 - sw]) top.rect(x, -sl / 2, sw, sl, 'OBJECT', 'pocket')
  top.center(-L / 2 - 4, 0, L / 2 + 4, 0)
  top.hDim(-pitch / 2, pitch / 2, W / 2 + 12, 0, `孔距 ${formatDimension(pitch)} · 2×Ø${formatDimension(d)}`, 'bossCenterDistance')
  top.vDim(-W / 2, W / 2, L / 2 + 12, L / 2, `宽 ${formatDimension(W)}`, 'baseWidth')
  end.rect(-W / 2, 0, W, T, 'OBJECT', 'base'); end.rect(-V / 2, T, V, H - T, 'OBJECT', 'upper')
  end.line(-V / 2, H - R, V / 2, H - R, 'HIDDEN', 'saddle'); end.rect(-sl / 2, H - depth, sl, depth, 'HIDDEN', 'pocket')
  end.hDim(-V / 2, V / 2, H + 12, H, `上宽 ${formatDimension(V)}`, 'upperWidth'); end.vDim(0, T, W / 2 + 12, W / 2, `板厚 ${formatDimension(T)}`, 'baseThickness')
  const holeIntervals = [-pitch / 2, pitch / 2].map((c) => [c - d / 2, c + d / 2])
  const breaks = [...new Set([-L / 2, L / 2, -U / 2, U / 2, ...holeIntervals.flat()])].sort((a, b) => a - b)
  const height = (x) => {
    if (Math.abs(x) > U / 2 + 1e-7) return T
    const saddleCut = Math.abs(x) < R ? Math.sqrt(Math.max(0, R * R - x * x)) : 0
    const pocketCut = Math.abs(x) >= O / 2 - sw && Math.abs(x) <= O / 2 ? depth : 0
    return H - Math.max(saddleCut, pocketCut)
  }
  for (let i = 0; i < breaks.length - 1; i += 1) {
    const a = breaks[i], b = breaks[i + 1], mid = (a + b) / 2
    if (holeIntervals.some(([lo, hi]) => mid > lo && mid < hi)) continue
    const points = [[a, 0], [b, 0]]
    const samples = Math.max(2, Math.ceil((b - a) * 2))
    for (let j = samples; j >= 0; j -= 1) { const x = a + (b - a) * j / samples; points.push([x, height(Math.max(a + 1e-6, Math.min(b - 1e-6, x)))]) }
    section.sectionPoly(points, 'saddle-and-holes')
  }
  section.hDim(-U / 2, U / 2, H + 12, H, `上长 ${formatDimension(U)}`, 'upperLength')
  section.leader(O / 2 - sw / 2, H - depth, L / 2 + 8, H + 3, `浅槽 ${formatDimension(sw)}×${formatDimension(sl)}×${formatDimension(depth)}`, 'pocketDepth')
  return [front, end, top, section]
}

function roundedOutline(view, points, radii) {
  const vertices = points.map((point, i) => {
    const prev = points[(i + points.length - 1) % points.length], next = points[(i + 1) % points.length], r = radii[i] || 0
    const inLength = Math.hypot(point[0] - prev[0], point[1] - prev[1]), outLength = Math.hypot(next[0] - point[0], next[1] - point[1])
    const incoming = [(point[0] - prev[0]) / inLength, (point[1] - prev[1]) / inLength], outgoing = [(next[0] - point[0]) / outLength, (next[1] - point[1]) / outLength]
    const sign = Math.sign(incoming[0] * outgoing[1] - incoming[1] * outgoing[0])
    const a = [point[0] - incoming[0] * r, point[1] - incoming[1] * r], b = [point[0] + outgoing[0] * r, point[1] + outgoing[1] * r]
    const center = [a[0] - incoming[1] * sign * r, a[1] + incoming[0] * sign * r]
    if (r) {
      let start = Math.atan2(a[1] - center[1], a[0] - center[0]) * 180 / Math.PI, end = Math.atan2(b[1] - center[1], b[0] - center[0]) * 180 / Math.PI
      if (sign < 0) [start, end] = [end, start]
      while (end <= start) end += 360
      view.arc(...center, r, start, end, 'OBJECT', 'base-fillet')
    }
    return { a, b }
  })
  vertices.forEach((v, i) => view.line(...v.b, ...vertices[(i + 1) % vertices.length].a, 'OBJECT', 'base'))
}

function clampViews(p) {
  const [L, W, T, H, R, boreR] = [p.baseLength, p.baseWidth, p.baseThickness, p.totalHeight, p.pedestalOuterRadius, p.boreDiameter / 2]
  const rear = W / 2, frontY = -W / 2, shoulderY = rear - p.baseMainDepth, centerY = rear - p.pedestalCenterFromRear, mountY = rear - p.mountHoleCenterFromRear
  const lower = T + p.pedestalHeight, halfTongue = p.frontTongueWidth / 2, ribX = R + Math.max(p.ribThickness, (p.rearBridgeWidth - 2 * R) / 2)
  const front = makeView('front', '主视图 · XZ'), top = makeView('top', '俯视图 · XY'), end = makeView('end', '右视图 · YZ'), section = makeView('section', 'A–A 中心剖面 · X = 0')
  front.poly([[-L / 2, 0], [L / 2, 0], [L / 2, T], [ribX, T], [R, T + p.ribHeight], [R, H], [-R, H], [-R, T + p.ribHeight], [-ribX, T], [-L / 2, T]], true, 'OBJECT', 'body')
  front.line(-R, lower, R, lower, 'OBJECT', 'low-clamp'); front.circle(0, p.crossHoleCenterZ, p.crossHoleDiameter / 2, 'OBJECT', 'cross-hole')
  front.rect(-p.splitWidth / 2, p.boreFloorZ, p.splitWidth, lower - p.boreFloorZ, 'OBJECT', 'split')
  for (const x of [-boreR, boreR]) front.line(x, p.boreFloorZ, x, H, 'HIDDEN', 'blind-bore')
  for (const c of [-p.mountHoleCenterDistance / 2, p.mountHoleCenterDistance / 2]) for (const x of [c - p.mountHoleDiameter / 2, c + p.mountHoleDiameter / 2]) front.line(x, 0, x, T, 'HIDDEN', 'mount-hole')
  front.hDim(-L / 2, L / 2, -12, 0, `底板 ${formatDimension(L)}`, 'baseLength'); front.vDim(0, H, L / 2 + 12, R, `总高 ${formatDimension(H)}`, 'totalHeight')
  front.leader(0, p.crossHoleCenterZ, R + 12, H + 10, `横孔 Ø${formatDimension(p.crossHoleDiameter)}`, 'crossHoleDiameter')
  roundedOutline(top, [[-L / 2, rear], [L / 2, rear], [L / 2, shoulderY], [halfTongue, shoulderY], [halfTongue, frontY], [-halfTongue, frontY], [-halfTongue, shoulderY], [-L / 2, shoulderY]], [0, 0, p.outerCornerRadius, p.neckConcaveRadius, p.neckConvexRadius, p.neckConvexRadius, p.neckConcaveRadius, p.outerCornerRadius])
  top.arc(0, centerY, R, 180, 360, 'OBJECT', 'clamp-D-profile'); top.poly([[-R, centerY], [-R, rear], [R, rear], [R, centerY]], false, 'OBJECT', 'rear-clamp')
  top.line(-R, centerY, R, centerY, 'OBJECT', 'height-step'); top.circle(0, centerY, boreR, 'OBJECT', 'blind-bore')
  top.rect(-p.splitWidth / 2, centerY - R, p.splitWidth, R - boreR, 'OBJECT', 'split')
  for (const x of [-p.mountHoleCenterDistance / 2, p.mountHoleCenterDistance / 2]) top.circle(x, mountY, p.mountHoleDiameter / 2, 'OBJECT', 'mount-hole')
  for (const side of [-1, 1]) top.rect(side < 0 ? -ribX : R, rear - p.ribThickness, ribX - R, p.ribThickness, 'OBJECT', 'rib')
  top.center(0, frontY - 5, 0, rear + 5); top.center(-L / 2 - 5, mountY, L / 2 + 5, mountY)
  top.hDim(-p.mountHoleCenterDistance / 2, p.mountHoleCenterDistance / 2, rear + 12, mountY, `孔距 ${formatDimension(p.mountHoleCenterDistance)} · 2×Ø${formatDimension(p.mountHoleDiameter)}`, 'mountHoleCenterDistance')
  top.vDim(frontY, rear, L / 2 + 12, L / 2, `总深 ${formatDimension(W)}`, 'baseWidth')
  top.leader(-R * .707, centerY - R * .707, -L / 2 - 15, frontY - 13, `前圆弧 R${formatDimension(R)}`, 'pedestalOuterRadius')
  end.poly([[frontY, 0], [rear, 0], [rear, H], [centerY, H], [centerY, lower], [centerY - R, lower], [centerY - R, T], [frontY, T]], true, 'OBJECT', 'body')
  end.rect(centerY - boreR, p.boreFloorZ, 2 * boreR, H - p.boreFloorZ, 'HIDDEN', 'blind-bore')
  for (const z of [p.crossHoleCenterZ - p.crossHoleDiameter / 2, p.crossHoleCenterZ + p.crossHoleDiameter / 2]) end.line(centerY - R, z, rear, z, 'HIDDEN', 'cross-hole')
  end.hDim(centerY, rear, H + 12, H, `距后缘 ${formatDimension(p.pedestalCenterFromRear)}`, 'pedestalCenterFromRear')
  end.vDim(0, p.boreFloorZ, rear + 12, centerY, `孔底 Z${formatDimension(p.boreFloorZ)}`, 'boreFloorZ')
  // At X=0 all cuts are rectangular in YZ: keep each material band explicit.
  const xs = [...new Set([frontY, centerY - R, centerY - boreR, centerY, centerY + boreR, rear])].sort((a, b) => a - b)
  const crossLo = p.crossHoleCenterZ - p.crossHoleDiameter / 2, crossHi = p.crossHoleCenterZ + p.crossHoleDiameter / 2
  const zs = [...new Set([0, T, p.boreFloorZ, lower, crossLo, crossHi, H])].sort((a, b) => a - b)
  const inside = (x, z) => {
    if (x <= frontY || x >= rear) return false
    const height = x < centerY - R ? T : x < centerY ? lower : H
    if (z <= 0 || z >= height) return false
    if (x >= centerY - R && x <= centerY + boreR && z > p.boreFloorZ) return false
    if (x >= centerY - R && z > crossLo && z < crossHi) return false
    return true
  }
  for (let i = 0; i < xs.length - 1; i += 1) for (let j = 0; j < zs.length - 1; j += 1) {
    const [a, b, lo, hi] = [xs[i], xs[i + 1], zs[j], zs[j + 1]], mx = (a + b) / 2, mz = (lo + hi) / 2
    if (!inside(mx, mz)) continue
    section.hatch([[a, lo], [b, lo], [b, hi], [a, hi]], 'section-material')
    if (!inside(a - 1e-5, mz)) section.line(a, lo, a, hi, 'OBJECT', 'section-boundary')
    if (!inside(b + 1e-5, mz)) section.line(b, lo, b, hi, 'OBJECT', 'section-boundary')
    if (!inside(mx, lo - 1e-5)) section.line(a, lo, b, lo, 'OBJECT', 'section-boundary')
    if (!inside(mx, hi + 1e-5)) section.line(a, hi, b, hi, 'OBJECT', 'section-boundary')
  }
  section.leader(centerY, p.boreFloorZ, rear + 8, H + 10, `盲孔 Ø${formatDimension(2 * boreR)} · 底 Z${formatDimension(p.boreFloorZ)}`, 'boreDiameter')
  return [front, end, top, section]
}

function nozzleViews(p) {
  const L = p.mainLength, h = p.headLength, neckEnd = h + p.neckLength, r0 = p.headLeftDiameter / 2, r1 = p.headRightDiameter / 2, rn = p.neckDiameter / 2, rt = p.tipDiameter / 2
  const bore = p.axialBoreDiameter / 2, cb = p.counterboreDiameter / 2, outlet = p.outletDiameter / 2, taperStart = L - (outlet - bore) / Math.tan(p.outletTaperHalfAngle * (Math.PI / 180))
  const outer = [[0, r0], [h, r1], [h, rn], [neckEnd, rn], [neckEnd, rt], [L, rt]]
  const inner = [[0, cb], [p.counterboreDepth, cb], [p.counterboreDepth, bore], [taperStart, bore], [L, outlet]]
  const front = makeView('front', '主视图 · XZ'), top = makeView('top', '俯视图 · XY'), end = makeView('end', '左端视图 · YZ'), section = makeView('section', 'A–A 纵剖面 · 主件与镶件')
  for (const view of [front, top]) {
    view.poly([...outer, ...outer.map(([x, y]) => [x, -y]).reverse()], true, 'OBJECT', 'main-body')
    for (const sign of [-1, 1]) view.poly(inner.map(([x, y]) => [x, sign * y]), false, 'HIDDEN', 'stepped-bore')
    view.rect(p.insertAxialOffset, -p.insertOuterDiameter / 2, p.insertLength, p.insertOuterDiameter, 'HIDDEN', 'insert')
    view.center(-5, 0, L + 5, 0)
  }
  front.hDim(0, L, Math.max(r0, r1) + 12, r1, `总长 ${formatDimension(L)}`, 'mainLength')
  front.hDim(0, h, -Math.max(r0, r1) - 12, -r1, `前段 ${formatDimension(h)}`, 'headLength')
  top.hDim(h, neckEnd, Math.max(r0, r1) + 12, rn, `颈段 ${formatDimension(p.neckLength)}`, 'neckLength')
  top.vDim(-rt, rt, L + 12, L, `末段 Ø${formatDimension(p.tipDiameter)}`, 'tipDiameter')
  for (const [radius, layer, feature] of [[Math.max(r0, r1), 'OBJECT', 'head-envelope'], [r0, 'OBJECT', 'head-face'], [cb, 'OBJECT', 'counterbore'], [p.insertOuterDiameter / 2, 'INSERT', 'insert']]) end.circle(0, 0, radius, layer, feature)
  const threadDiameter = Number(String(p.insertThreadDesignation).match(/^M(\d+(?:\.\d+)?)/i)[1])
  end.circle(0, 0, threadDiameter / 2, 'INSERT', 'insert-thread-indication'); end.center(-r1 - 5, 0, r1 + 5, 0); end.center(0, -r1 - 5, 0, r1 + 5)
  end.leader(cb, 0, r1 + 10, r1 + 10, `沉孔 Ø${formatDimension(p.counterboreDiameter)}`, 'counterboreDiameter')
  end.leader(0, -p.insertOuterDiameter / 2, r1 + 10, -r1 - 10, `${p.insertThreadDesignation} 镶件`, 'insertThreadDesignation')
  for (const sign of [-1, 1]) {
    section.sectionPoly([...outer, ...inner.slice().reverse()].map(([x, y]) => [x, sign * y]), 'main-body')
    section.sectionPoly([[p.insertAxialOffset, sign * threadDiameter / 2], [p.insertAxialOffset + p.insertLength, sign * threadDiameter / 2], [p.insertAxialOffset + p.insertLength, sign * p.insertOuterDiameter / 2], [p.insertAxialOffset, sign * p.insertOuterDiameter / 2]], 'insert', 'INSERT')
  }
  section.center(-5, 0, L + 5, 0); section.hDim(0, p.counterboreDepth, r1 + 12, cb, `沉孔深 ${formatDimension(p.counterboreDepth)}`, 'counterboreDepth')
  section.leader(L, outlet, L + 10, r1 + 12, `出口 Ø${formatDimension(p.outletDiameter)} · ${formatDimension(p.outletTaperHalfAngle)}°`, 'outletTaperHalfAngle')
  section.leader(p.insertAxialOffset + p.insertLength / 2, -p.insertOuterDiameter / 2, L / 2, -r1 - 12, `镶件 Ø${formatDimension(p.insertOuterDiameter)}×${formatDimension(p.insertLength)}`, 'insertLength')
  return [front, end, top, section]
}

function entityPoints(entity) {
  if (entity.type === 'line') return [[entity.x1, entity.y1], [entity.x2, entity.y2]]
  if (entity.type === 'polyline') return entity.points
  if (entity.type === 'circle' || entity.type === 'arc') return [[entity.cx - entity.r, entity.cy - entity.r], [entity.cx + entity.r, entity.cy + entity.r]]
  const textHalfWidth = entity.text.length * entity.height * .35
  return [[entity.x - textHalfWidth, entity.y], [entity.x + textHalfWidth, entity.y + entity.height]]
}
function translateEntity(entity, dx, dy) {
  if (entity.type === 'line') return { ...entity, x1: entity.x1 + dx, y1: entity.y1 + dy, x2: entity.x2 + dx, y2: entity.y2 + dy }
  if (entity.type === 'polyline') return { ...entity, points: entity.points.map(([x, y]) => [x + dx, y + dy]) }
  if (entity.type === 'arc' || entity.type === 'circle') return { ...entity, cx: entity.cx + dx, cy: entity.cy + dy }
  return { ...entity, x: entity.x + dx, y: entity.y + dy }
}

export function buildDrawingScene(model, options = {}) {
  const validation = validateModelParameters(model)
  if (!validation.valid) return { ...validation, entities: [], views: [], dimensions: [], layers: drawingLayers, notes: [], width: 420, height: 297 }
  const p = Object.fromEntries(Object.entries(model).map(([key, value]) => [key, typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value)) ? Number(value) : value]))
  const factories = { shaft: shaftViews, bracket: bracketViews, split_clamp_support: clampViews, stepped_tapered_nozzle: nozzleViews }
  const rawViews = factories[validation.kind](p)
  const bounds = rawViews.map((view) => {
    const points = view.entities.flatMap(entityPoints), xs = points.map(([x]) => x), ys = points.map(([, y]) => y)
    return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) }
  })
  const widths = [Math.max(bounds[0].maxX - bounds[0].minX, bounds[2].maxX - bounds[2].minX), Math.max(bounds[1].maxX - bounds[1].minX, bounds[3].maxX - bounds[3].minX)]
  const heights = [Math.max(bounds[0].maxY - bounds[0].minY, bounds[1].maxY - bounds[1].minY), Math.max(bounds[2].maxY - bounds[2].minY, bounds[3].maxY - bounds[3].minY)]
  const width = Math.max(300, widths[0] + widths[1] + 55), height = heights[0] + heights[1] + 90
  const entities = [], views = []
  rawViews.forEach((view, i) => {
    const col = i % 2, row = i < 2 ? 1 : 0, b = bounds[i]
    const x = 15 + (col ? widths[0] + 25 : 0), y = 28 + (row ? heights[1] + 35 : 0)
    const dx = x - b.minX, dy = y - b.minY
    const translated = view.entities.map((entity) => translateEntity(entity, dx, dy))
    translated.push({ id: `${view.id}-title`, view: view.id, type: 'text', layer: 'TEXT', x: x + (b.maxX - b.minX) / 2, y: y - 10, height: 3.6, text: view.title })
    entities.push(...translated)
    views.push({ id: view.id, title: view.title, bounds: { x, y, width: b.maxX - b.minX, height: b.maxY - b.minY }, entityCount: translated.length })
  })
  entities.push({ id: 'drawing-title', view: 'sheet', type: 'text', layer: 'TEXT', x: width / 2, y: height - 10, height: 4, text: `${model.name || validation.kind} · 参数工程图` })
  entities.push({ id: 'drawing-units', view: 'sheet', type: 'text', layer: 'TEXT', x: width / 2, y: 8, height: 3, text: '单位 mm · DXF 模型空间 1:1 · 尺寸值为实际值' })
  const notes = ['视图按当前配方参数计算；虚线表示内部特征。图纸尺寸检查不替代 OCCT 实体拓扑校验。']
  if (validation.kind === 'bracket' || validation.kind === 'split_clamp_support') notes.push('三视图保留各切除特征的投影边界，复杂相交处未执行 OCCT 自动消隐；中心剖面用于核对内部结构。')
  if (validation.kind === 'stepped_tapered_nozzle') notes.push('主件与镶件分层显示；M 螺纹为公称标注和简化孔，不绘制真实牙型。')
  return { ...validation, modelName: model.name || validation.kind, entities, views, dimensions: rawViews.flatMap((view) => view.dimensions.map((dimension) => ({ ...dimension, view: view.id }))), layers: drawingLayers, notes, width: round(width), height: round(height), scale: options.scale || '1:1', parameterSnapshot: { ...model } }
}

export function dxfForModel(model, options = {}) {
  const scene = options.scene || (model?.entities && model?.views ? model : buildDrawingScene(model, options))
  if (!scene.valid) throw new Error(scene.errors?.map((error) => error.message).join('；') || '图纸参数无效，不能导出 DXF。')
  const isVisible = (layer) => options.layers?.[layer] !== false
  const out = [], pair = (code, value) => { out.push(String(code), typeof value === 'number' ? String(round(value)) : String(value).replace(/[\r\n]/g, ' ')) }
  const section = (name) => { pair(0, 'SECTION'); pair(2, name) }
  section('HEADER'); pair(9, '$ACADVER'); pair(1, 'AC1021'); pair(9, '$INSUNITS'); pair(70, 4); pair(9, '$MEASUREMENT'); pair(70, 1); pair(0, 'ENDSEC')
  section('TABLES'); pair(0, 'TABLE'); pair(2, 'LTYPE'); pair(5, 'F001'); pair(100, 'AcDbSymbolTable'); pair(70, 3)
  for (const [name, segments] of [['CONTINUOUS', []], ['DASHED', [3, -2]], ['CENTER', [7, -2, 1, -2]]]) {
    pair(0, 'LTYPE'); pair(2, name); pair(70, 0); pair(3, name); pair(72, 65); pair(73, segments.length); pair(40, segments.reduce((sum, part) => sum + Math.abs(part), 0)); segments.forEach((part) => pair(49, part))
  }
  pair(0, 'ENDTAB'); pair(0, 'TABLE'); pair(2, 'LAYER'); pair(5, 'F002'); pair(100, 'AcDbSymbolTable'); pair(70, scene.layers.length)
  for (const layer of scene.layers) { pair(0, 'LAYER'); pair(2, layer.id); pair(70, 0); pair(62, isVisible(layer.id) ? layer.dxfColor : -layer.dxfColor); pair(6, layer.id === 'HIDDEN' ? 'DASHED' : layer.id === 'CENTER' ? 'CENTER' : 'CONTINUOUS') }
  pair(0, 'ENDTAB'); pair(0, 'ENDSEC'); section('ENTITIES')
  for (const e of scene.entities) {
    if (!isVisible(e.layer)) continue
    const type = { line: 'LINE', circle: 'CIRCLE', arc: 'ARC', polyline: 'LWPOLYLINE', text: 'TEXT' }[e.type]
    pair(0, type); pair(100, 'AcDbEntity'); pair(8, e.layer)
    if (e.type === 'line') { pair(100, 'AcDbLine'); pair(10, e.x1); pair(20, e.y1); pair(30, 0); pair(11, e.x2); pair(21, e.y2); pair(31, 0) }
    if (e.type === 'circle' || e.type === 'arc') { pair(100, 'AcDbCircle'); pair(10, e.cx); pair(20, e.cy); pair(30, 0); pair(40, e.r); if (e.type === 'arc') { pair(100, 'AcDbArc'); pair(50, ((e.start % 360) + 360) % 360); pair(51, ((e.end % 360) + 360) % 360) } }
    if (e.type === 'polyline') { pair(100, 'AcDbPolyline'); pair(90, e.points.length); pair(70, e.closed ? 1 : 0); for (const [x, y] of e.points) { pair(10, x); pair(20, y) } }
    if (e.type === 'text') { pair(100, 'AcDbText'); pair(10, e.x); pair(20, e.y); pair(30, 0); pair(40, e.height); pair(1, e.text); pair(72, 1); pair(11, e.x); pair(21, e.y); pair(31, 0); pair(100, 'AcDbText'); pair(73, 0) }
  }
  pair(0, 'ENDSEC'); pair(0, 'EOF')
  return `${out.join('\n')}\n`
}

export function drawingEntityBounds(entity) { return entityPoints(entity) }
