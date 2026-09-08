import test from 'node:test'
import assert from 'node:assert/strict'
import { buildDrawingScene, dxfForModel, drawingScaleFactor, drawingEntityBounds, isDrawingKernelReady } from './drawingGeometry.js'

const shaft = { name: '动力轴', kind: 'shaft', outerDiameter: 24, length: 70, holeDiameter: 10, keywayWidth: 6, keywayDepth: 3, keywayLength: 40 }
const bracket = { name: '支架', kind: 'bracket', baseLength: 100, baseWidth: 50, baseThickness: 10, upperLength: 70, upperWidth: 50, upperHeight: 30, totalHeight: 40, notchOpening: 40, notchRadius: 15, slotLength: 30, slotWidth: 10, pocketDepth: 10, bossDiameter: 20, bossCenterDistance: 70 }
const clamp = { name: '夹紧座', kind: 'split_clamp_support', baseLength: 125, baseWidth: 95, baseThickness: 15, baseMainDepth: 80, frontTongueWidth: 80, rearBridgeWidth: 86, totalHeight: 75, pedestalOuterRadius: 33, pedestalCenterFromRear: 35, pedestalHeight: 40, rearClampRise: 20, boreDiameter: 36, boreFloorZ: 40, splitWidth: 12, mountHoleCount: 2, mountHoleDiameter: 12, mountHoleCenterDistance: 96, mountHoleCenterFromRear: 40, crossHoleDiameter: 12, crossHoleCenterZ: 55, ribHeight: 20, ribThickness: 10, outerCornerRadius: 8, neckConcaveRadius: 5, neckConvexRadius: 8 }
const nozzle = { name: '锥管嘴', kind: 'stepped_tapered_nozzle', mainLength: 98, headLength: 50, neckLength: 20, headLeftDiameter: 54.2544935, headRightDiameter: 56, neckDiameter: 30, tipDiameter: 25, counterboreDiameter: 40, counterboreDepth: 40, axialBoreDiameter: 13, outletDiameter: 17, outletTaperHalfAngle: 15, insertOuterDiameter: 39.4, insertLength: 40, insertThreadDesignation: 'M12', insertAxialOffset: 0 }

test('every recipe produces its own three views, section and finite exportable geometry', () => {
  for (const model of [shaft, bracket, clamp, nozzle]) {
    const scene = buildDrawingScene(model)
    assert.equal(scene.valid, true, JSON.stringify(scene.errors))
    assert.deepEqual(scene.views.map((view) => view.id), ['front', 'end', 'top', 'section'])
    assert.ok(scene.dimensions.length >= 6)
    assert.ok(scene.entities.some((entity) => entity.layer === 'SECTION'))
    for (const entity of scene.entities) for (const point of drawingEntityBounds(entity)) assert.ok(point.every(Number.isFinite), entity.id)
    assert.ok(!/NaN|Infinity/.test(dxfForModel(model)))
  }
})

test('shaft geometry expresses the through bore and keyway in views and section', () => {
  const scene = buildDrawingScene(shaft)
  assert.ok(scene.entities.some((e) => e.view === 'end' && e.type === 'circle' && e.feature === 'bore' && e.r === 5))
  const slot = scene.entities.find((e) => e.view === 'top' && e.feature === 'keyway' && e.type === 'polyline')
  assert.equal(slot.points[1][0] - slot.points[0][0], 40)
  assert.equal(slot.points[2][1] - slot.points[1][1], 6)
  assert.ok(scene.entities.some((e) => e.view === 'section' && e.feature === 'keyway'))
  const dxf = dxfForModel(shaft)
  for (const text of ['主视图', '俯视图', '左端视图', '中心纵剖面', '通孔 Ø10', '槽深 3', '键槽长 40']) assert.ok(dxf.includes(text), text)
  assert.ok(dxf.includes('\nCIRCLE\n')); assert.ok(dxf.includes('\nLWPOLYLINE\n')); assert.ok(dxf.includes('\nARC\n'))
})

test('edited dimensions change actual line/circle geometry instead of text only', () => {
  const before = buildDrawingScene(shaft), after = buildDrawingScene({ ...shaft, length: 85, outerDiameter: 32, holeDiameter: 12, keywayLength: 50 })
  assert.equal(before.dimensions.find((d) => d.field === 'length').value, 70)
  assert.equal(after.dimensions.find((d) => d.field === 'length').value, 85)
  assert.equal(after.entities.find((e) => e.view === 'end' && e.feature === 'bore').r, 6)
  const body = after.entities.find((e) => e.view === 'front' && e.feature === 'body')
  assert.equal(Math.max(...body.points.map(([x]) => x)) - Math.min(...body.points.map(([x]) => x)), 85)
  assert.notDeepEqual(before.entities, after.entities)
})

test('clamp uses its D profile, fillets, blind bore and cross hole; nozzle keeps its insert separate', () => {
  const clampScene = buildDrawingScene(clamp)
  const arc = clampScene.entities.find((e) => e.feature === 'clamp-D-profile')
  assert.equal(arc.r, 33); assert.equal(arc.end - arc.start, 180)
  assert.equal(clampScene.entities.filter((e) => e.feature === 'base-fillet').length, 6)
  assert.ok(clampScene.entities.some((e) => e.feature === 'blind-bore'))
  assert.ok(clampScene.entities.some((e) => e.feature === 'cross-hole'))
  const nozzleScene = buildDrawingScene(nozzle)
  assert.ok(nozzleScene.entities.some((e) => e.layer === 'INSERT' && e.view === 'section'))
  assert.ok(nozzleScene.notes.some((note) => note.includes('不绘制真实牙型')))
})

test('DXF honors visible layers and retains actual millimeter geometry at every display scale', () => {
  const scene = buildDrawingScene(shaft, { scale: '2:1' })
  const all = dxfForModel(shaft, { scene }), withoutHidden = dxfForModel(shaft, { scene, layers: { HIDDEN: false } })
  const entitySection = (dxf) => dxf.split('\nENTITIES\n')[1]
  assert.ok(entitySection(all).includes('\n8\nHIDDEN\n'))
  assert.ok(!entitySection(withoutHidden).includes('\n8\nHIDDEN\n'))
  assert.equal(all, dxfForModel(shaft, { scale: '1:2' }))
  assert.ok(all.includes('\n$INSUNITS\n70\n4\n'))
  assert.equal(drawingScaleFactor('1:2'), .5); assert.equal(drawingScaleFactor('2:1'), 2)
})

test('unknown or invalid models produce an honest empty scene and cannot be exported', () => {
  assert.equal(buildDrawingScene({ ...shaft, kind: 'gear' }).entities.length, 0)
  assert.throws(() => dxfForModel({ ...shaft, holeDiameter: 50 }), /通孔/)
  assert.throws(() => dxfForModel({ ...nozzle, insertThreadDesignation: '' }), /螺纹/)
})

test('production validation is not displayed for unavailable, stale or pending artifacts', () => {
  const ready = { validation: { valid: true, productionReady: true }, artifactStatus: 'available' }
  assert.equal(isDrawingKernelReady(ready), true)
  for (const patch of [{ artifactStatus: 'unavailable' }, { artifactStatus: 'recovering' }, { stale: true }, { pendingDrawing: true }, { validation: { valid: false, productionReady: true } }, { validation: { valid: true, productionReady: false } }]) {
    assert.equal(isDrawingKernelReady({ ...ready, ...patch }), false, JSON.stringify(patch))
  }
  assert.equal(isDrawingKernelReady(ready, true), false)
  assert.equal(isDrawingKernelReady(null), false)
})
