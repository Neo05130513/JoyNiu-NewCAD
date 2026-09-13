import test from 'node:test'
import assert from 'node:assert/strict'
import { cadParameterLabel, cadParameterUnit } from './cadParameterLabels.js'

test('Chinese labels make existing camel case and snake case parameters readable', () => {
  const expected = {
    length: '长度', width: '宽度', plateThickness: '板厚', frontThickness: '前端壁厚',
    upperRadius: '上部圆弧半径', holeDepthFromTop: '顶部至孔中心距离', lowerRadius: '下部圆弧半径',
    frontHoleDiameter: '前端孔径', halfWidth: '半宽', shoulderHeight: '肩部高度',
    shoulderDistanceSquared: '肩部距离的平方', tangentOffsetY: '切点 Y 向偏移', tangentOffsetZ: '切点 Z 向偏移',
    mountHoleDiameter: '安装孔径', mountPitchX: '安装孔 X 向中心距', mountRearOffset: '后排安装孔距后端距离',
    mountFirstY: '第一排安装孔 Y 坐标', mountPitchY: '安装孔 Y 向中心距', mountRearX: '后排安装孔 X 坐标',
    mountFrontX: '前排安装孔 X 坐标', mountSecondY: '第二排安装孔 Y 坐标', slotDiameter: '槽直径',
    slotAngle: '槽倾角', slotAxisY: '槽轴线 Y 坐标', slotAxisZ: '槽轴线 Z 坐标', slotToolLength: '开槽辅助工具长度',
  }
  for (const [key, label] of Object.entries(expected)) {
    assert.equal(cadParameterLabel(key), label)
    assert.equal(cadParameterLabel(key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`)), label)
  }
  assert.equal(cadParameterLabel('hole_depth'), '孔深')
  assert.equal(cadParameterLabel('arm_width'), '臂宽')
})

test('existing human Chinese labels and names are preserved exactly', () => {
  assert.equal(cadParameterLabel('width', { label: ' 零件总宽 W ', name: '另一个名称' }), ' 零件总宽 W ')
  assert.equal(cadParameterLabel('width', { label: 'width', name: '板材宽度' }), '板材宽度')
  assert.equal(cadParameterLabel('盲孔深度'), '盲孔深度')
  assert.equal(cadParameterLabel('p1', { label: 'Hole diameter' }), '孔径')
  assert.equal(cadParameterLabel('plateThickness', { label: 'unknown' }), '板厚')
})

test('generic compositions support new identifiers without guessing unknown terms', () => {
  assert.equal(cadParameterLabel('rear_boss_diameter'), '后端凸台直径')
  assert.equal(cadParameterLabel('r_left_boss'), '左侧凸台半径')
  assert.equal(cadParameterLabel('x_left_boss_end'), '左侧凸台端部 X 坐标')
  assert.equal(cadParameterLabel('mysteryHole', {}, 0), '参数 1')
  assert.equal(cadParameterLabel('customThing', {}, 1), '参数 2')
  assert.equal(cadParameterLabel('α', {}, 2), '参数 3')
  assert.equal(cadParameterLabel('constructor', {}, 3), '参数 4')
  assert.equal(cadParameterLabel('toString', {}, 4), '参数 5')
  assert.equal(cadParameterLabel(''), '参数 1')
  assert.equal(cadParameterLabel(null, null, -1), '参数 1')
})

test('units respect explicit units and distinguish angles, radians and derived values', () => {
  assert.equal(cadParameterUnit('slotAngle'), '°')
  assert.equal(cadParameterUnit('rear_angle'), '°')
  assert.equal(cadParameterUnit('theta', { label: '斜槽倾角' }), '°')
  assert.equal(cadParameterUnit('slotAngle', { unit: 'rad' }), 'rad')
  assert.equal(cadParameterUnit('slotAngle', { unit: 'mm' }), 'mm')
  assert.equal(cadParameterUnit('slotAngle', { unit: '', units: 'rad' }), '')
  assert.equal(cadParameterUnit('slotAngle', { units: 'rad' }), 'rad')
  assert.equal(cadParameterUnit('slotAngleRadians'), 'rad')
  assert.equal(cadParameterUnit('slotAngle', { expression: 'radians(23)' }), 'rad')
  assert.equal(cadParameterUnit('slotAngleCosine'), '')
  assert.equal(cadParameterUnit('tangent_sine'), '')
  assert.equal(cadParameterUnit('holeCount'), '')
  assert.equal(cadParameterUnit('shoulderDistanceSquared'), 'mm²')
  assert.equal(cadParameterUnit('shoulder_distance_squared', {}, 'cm'), 'cm²')
  assert.equal(cadParameterUnit('mystery'), 'mm')
  assert.equal(cadParameterUnit('mystery', {}, 'in'), 'in')
})

test('incidental degree text cannot turn a derived length into an angle', () => {
  assert.equal(cadParameterUnit('p1', { source: { text: '23°' } }), '°')
  assert.equal(cadParameterUnit('p1', { source: '-12.5°' }), '°')
  assert.equal(cadParameterUnit('slotToolLength', { source: { text: '根据长度和 23° 倾角计算工具长度' }, expression: '2*length/cos(radians(slotAngle))' }), 'mm')
  assert.equal(cadParameterUnit('p1', { source: { text: '23°' }, expression: 'width*cos(radians(23))' }), 'mm')
  assert.equal(cadParameterUnit('height', { source: { text: '侧面有 45° 斜边，高度为 23 mm。' } }), 'mm')
})

test('presentation leaves parameter values, sources and formulas untouched', () => {
  const row = { value: 23, expression: 'other_angle+2', source: { type: 'drawing', text: '23°' }, label: 'slotAngle' }
  const original = structuredClone(row)
  cadParameterLabel('slotAngle', row)
  cadParameterUnit('slotAngle', row)
  assert.deepEqual(row, original)
})
