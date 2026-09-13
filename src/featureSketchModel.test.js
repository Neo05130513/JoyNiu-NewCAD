import test from 'node:test'
import assert from 'node:assert/strict'
import { appendSketchSegment, removeSketchSegment, replaceSketchShape, screenToSketch, sketchArc, sketchAxes, sketchGeometry, sketchPoints, sketchViewBox, updateSketchPoint } from './featureSketchModel.js'

const profile = () => ({ id: 'profile1', op: 'profile_revolve', label: '保留的零件', plane: 'XZ', origin: [4, 5, 6], start: [5, 0], segments: [{ type: 'line', to: [10, 0] }, { type: 'line', to: [10, 20] }, { type: 'line', to: [5, 20] }], axisStart: [0, 0], axisEnd: [0, 1], angle: 180 })

test('explicit rectangle and circle replacement use only supported profile fields and preserve other settings', () => {
  const original = profile(), saved = structuredClone(original)
  const rectangle = replaceSketchShape(original, 'rectangle', { x: 1, y: 2, width: 8, height: 4 })
  assert.deepEqual(rectangle.start, [1, 2])
  assert.deepEqual(rectangle.segments, [{ type: 'line', to: [9, 2] }, { type: 'line', to: [9, 6] }, { type: 'line', to: [1, 6] }, { type: 'line', to: [1, 2] }])
  const circle = replaceSketchShape(original, 'circle', { x: 12, y: 8, radius: 3 })
  assert.deepEqual(circle.start, [15, 8])
  assert.deepEqual(circle.segments, [{ type: 'arc', through: [12, 11], to: [9, 8] }, { type: 'arc', through: [12, 5], to: [15, 8] }])
  for (const result of [rectangle, circle]) {
    assert.deepEqual(Object.keys(result).sort(), [...Object.keys(original), 'sketchConstraints'].sort())
    for (const key of ['id', 'op', 'label', 'plane', 'origin', 'axisStart', 'axisEnd', 'angle']) assert.deepEqual(result[key], original[key])
  }
  assert.deepEqual(original, saved)
  assert.throws(() => replaceSketchShape(original, 'circle', { x: 0, y: 0, radius: '' }), /请输入/)
  assert.throws(() => replaceSketchShape(original, 'rectangle', { x: 0, y: 0, width: 4, height: -2 }), /必须大于零/)
})

test('a new rectangle exposes all four real edges and keeps its closing vertex linked without remapping existing point indices', () => {
  const rectangle = replaceSketchShape(profile(), 'rectangle', { x: 1, y: 2, width: 8, height: 4 })
  const geometry = sketchGeometry(rectangle)
  assert.deepEqual(geometry.edges.map(edge => edge.index), [0, 1, 2, 3])
  assert.equal(geometry.closed, true); assert.equal(geometry.closingPath, '')
  assert.deepEqual(geometry.edges[3], { index: 3, start: [1, 6], end: [1, 2], d: 'M 1 6 L 1 2' })
  assert.deepEqual(sketchPoints(rectangle).map(point => point.key), ['start', '0:to', '1:to', '2:to', '3:to'])
  const movedStart = updateSketchPoint(rectangle, 'start', [-2, 3])
  assert.deepEqual(movedStart.segments[3].to, movedStart.start)
  assert.deepEqual(movedStart.segments.slice(0, 3), rectangle.segments.slice(0, 3))
  const movedEnd = updateSketchPoint(rectangle, '3:to', [3, 4])
  assert.deepEqual(movedEnd.start, [3, 4]); assert.deepEqual(movedEnd.segments[3].to, [3, 4])
  assert.deepEqual(rectangle.start, [1, 2])
  const legacy = profile(), legacyBefore = structuredClone(legacy)
  assert.equal(sketchGeometry(legacy).edges.length, 3)
  assert.equal(sketchGeometry(legacy).closingPath, 'M 5 20 L 5 0')
  assert.deepEqual(legacy, legacyBefore)
})

test('numeric closure vertices stay connected while expression points require explicit coordinate replacement', () => {
  const circle = replaceSketchShape(profile(), 'circle', { x: 0, y: 0, radius: 5 })
  circle.segments[0].radius = 'expectedRadius'
  const moved = updateSketchPoint(circle, 'start', [6, 1])
  assert.deepEqual(moved.start, [6, 1])
  assert.deepEqual(moved.segments[1].to, [6, 1])
  assert.equal(moved.segments[0].radius, 'expectedRadius')
  assert.deepEqual(circle.start, [5, 0])
  const expressed = { ...circle, start: ['width / 2', 0] }
  assert.throws(() => updateSketchPoint(expressed, 'start', [4, 0]), /表达式坐标不能拖动/)
  assert.deepEqual(updateSketchPoint(expressed, 'start', [4, 0], { explicit: true }).start, [4, 0])
  assert.equal(expressed.start[0], 'width / 2')
  assert.throws(() => updateSketchPoint({ ...circle, start: ['5', 0] }, 'start', [4, 0]), /表达式坐标不能拖动/)
})

test('appending and deleting respect profile bounds and reject degenerate arcs', () => {
  const original = profile()
  const added = appendSketchSegment(original, { type: 'arc', through: [2, 15], to: [5, 10] })
  assert.equal(added.segments.length, 4)
  assert.equal(original.segments.length, 3)
  assert.deepEqual(removeSketchSegment(added, 3), original)
  assert.throws(() => appendSketchSegment(original, { type: 'line', to: [5, 20] }), /不能.*重合/)
  assert.throws(() => appendSketchSegment(original, { type: 'arc', through: [5, 15], to: [5, 10] }), /共线/)
  assert.throws(() => removeSketchSegment({ ...original, segments: original.segments.slice(0, 2) }, 1), /至少需要 2/)
  assert.throws(() => appendSketchSegment({ ...original, segments: Array.from({ length: 128 }, () => ({ type: 'line', to: [1, 2] })) }, { type: 'line', to: [5, 6] }), /最多支持 128/)
})

test('arc preview uses actual circle geometry, including major arc direction and bounds', () => {
  const small = sketchArc([1, 0], [Math.SQRT1_2, Math.SQRT1_2], [0, 1])
  assert.ok(Math.abs(small.radius - 1) < 1e-8)
  assert.equal(small.sweep, 1); assert.equal(small.largeArc, 0)
  const major = sketchArc([1, 0], [-1, 0], [0, 1])
  assert.equal(major.sweep, 0); assert.equal(major.largeArc, 1)
  assert.equal(sketchArc([0, 0], [1, 1], [2, 2]), null)
  const circle = replaceSketchShape(profile(), 'circle', { x: 0, y: 0, radius: 5 })
  const view = sketchViewBox(circle)
  assert.ok(view[0] < -5 && view[0] + view[2] > 5)
  assert.ok(view[1] < -5 && view[1] + view[3] > 5)
  const geometry = sketchGeometry(circle)
  assert.equal(geometry.closed, true); assert.equal(geometry.closingPath, '')
  assert.equal(geometry.edges.filter(edge => edge.d.includes(' A ')).length, 2)
})

test('open endpoints show the automatic closing edge and unresolved expressions never invent geometry', () => {
  const original = profile(), geometry = sketchGeometry(original)
  assert.equal(geometry.closed, false)
  assert.equal(geometry.closingPath, 'M 5 20 L 5 0')
  const expressed = { ...original, start: ['r', 0] }
  const partial = sketchGeometry(expressed)
  assert.equal(partial.edges.length, 2)
  assert.equal(partial.closingPath, '')
  assert.equal(partial.closureKnown, false)
  assert.match(partial.warnings[0], /表达式/)
  assert.deepEqual(sketchAxes('XZ'), ['X', 'Z'])
  assert.deepEqual(sketchAxes('YZ'), ['Y', 'Z'])
})

test('screen mapping respects SVG letterboxing, flipped vertical axis and hidden zero-size canvases', () => {
  const bounds = { left: 10, top: 20, width: 400, height: 200 }, view = [-10, -10, 20, 20]
  assert.deepEqual(screenToSketch(210, 120, bounds, view), [0, 0])
  assert.deepEqual(screenToSketch(260, 70, bounds, view), [5, 5])
  assert.equal(screenToSketch(0, 0, { ...bounds, width: 0 }, view), null)
  assert.equal(screenToSketch(NaN, 0, bounds, view), null)
})
