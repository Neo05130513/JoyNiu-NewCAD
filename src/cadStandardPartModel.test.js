import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { cadStandardParts, insertCadStandardPart } from './cadStandardPartModel.js'

const base = () => ({ version: 'cad-plan-v1', units: 'mm', parameters: { width: { value: 20 } }, features: [{ id: 'body', op: 'box', size: ['width', 16, 10] }], result: 'body' })

test('standard insertion preserves old geometry, binds editable dimensions and makes a separate body', () => {
  const original = base(), before = structuredClone(original)
  const first = insertCadStandardPart(original, 'washer-m8', { position: [40, 8, 2], axis: 'X', instanceId: 'one' })
  assert.deepEqual(original, before)
  assert.deepEqual(first.plan.features[0], original.features[0])
  assert.deepEqual(first.plan.parameters.width, original.parameters.width)
  assert.deepEqual(first.plan.features.at(-1).inputs, ['body', 'sp_one_position'])
  assert.equal(first.plan.features.at(-1).op, 'compound')
  assert.ok(first.plan.features.find(feature => feature.id === 'sp_one_geometry').label.endsWith('· 实体'))
  assert.ok(first.plan.features.find(feature => feature.id === 'sp_one_axis').label.endsWith('· 轴向'))
  assert.ok(first.plan.features.find(feature => feature.id === 'sp_one_position').label.endsWith('· 定位'))
  assert.ok(first.plan.features.at(-1).label.endsWith('· 组合'))
  assert.equal(first.plan.parameters.sp_one_x.value, 40)
  assert.equal(first.plan.features.find(feature => feature.id === 'sp_one_geometry').dimensions.outerDiameter, 'sp_one_outerDiameter')
  const second = insertCadStandardPart(first.plan, 'pin-6-24', { instanceId: 'two' })
  assert.deepEqual(second.plan.features.at(-1).inputs, [first.plan.result, 'sp_two_position'])
  assert.ok(second.plan.parameters.sp_one_length && second.plan.parameters.sp_two_length)
  assert.throws(() => insertCadStandardPart(first.plan, 'pin-6-24', { instanceId: 'one' }), /冲突/)
})

test('blank CAD documents accept a first real standard part and invalid positions never modify the plan', () => {
  const plan = { version: 'cad-plan-v1', units: 'mm', parameters: {}, features: [], result: null }
  const result = insertCadStandardPart(plan, 'pin-6-24', { instanceId: 'first' })
  assert.equal(result.plan.result, 'sp_first_position')
  assert.equal(result.plan.features.some(feature => feature.op === 'compound'), false)
  assert.throws(() => insertCadStandardPart(plan, 'pin-6-24', { position: [NaN, 0, 0] }), /位置/)
  assert.throws(() => insertCadStandardPart(plan, 'missing'), /有效/)
  assert.deepEqual(plan.features, [])
  assert.equal(cadStandardParts.length, 7)
})

test('ring, screw and pin become valid separate STEP solids and retain editable placement and length', () => {
  const folder = mkdtempSync(join(tmpdir(), 'cad-standard-step-'))
  const cases = [['bearing-6204', 'X'], ['socket-m8-30', 'Y'], ['pin-6-24', 'Z']].map(([catalogId, axis], index) => {
    const inserted = insertCadStandardPart(base(), catalogId, { position: [60, 30, 20], axis, instanceId: `real${index}` })
    const edited = structuredClone(inserted.plan)
    edited.parameters[`sp_real${index}_length`].value += 2
    edited.parameters[`sp_real${index}_x`].value += 10
    return { plan: inserted.plan, edited }
  })
  const script = `import json,sys
from pathlib import Path
from app.cad_executor import execute_cad_plan
from app.geometry import get_cadquery
data=json.load(sys.stdin); reports=[]
for i,case in enumerate(data['cases']):
 shapes=[]
 for key in ('plan','edited'):
  result=execute_cad_plan(case[key],Path(data['folder'])/(str(i)+'-'+key),include_projections=False)
  assert result['status']=='succeeded',result.get('errors')
  shape=get_cadquery().importers.importStep(result['artifacts']['step']['path']).val()
  solids=sorted(shape.Solids(),key=lambda solid:solid.BoundingBox().xmin)
  assert shape.isValid() and len(solids)==(12 if i==0 else 2)
  assert abs(solids[0].Volume()-3200)<1e-5
  parts=solids[1:]; total=sum(part.Volume() for part in parts)
  shapes.append({'volume':total,'centerX':(min(part.BoundingBox().xmin for part in parts)+max(part.BoundingBox().xmax for part in parts))/2})
 reports.append(shapes)
print(json.dumps(reports))`
  try {
    const result = spawnSync(resolve('apps/api/.venv/bin/python'), ['-c', script], { input: JSON.stringify({ folder, cases }), encoding: 'utf8', env: { ...process.env, PYTHONPATH: resolve('apps/api') }, timeout: 120000, maxBuffer: 1024 * 1024 })
    assert.equal(result.status, 0, result.stderr || result.stdout)
    const reports = JSON.parse(result.stdout.trim().split('\n').at(-1))
    const envelopes = [Math.PI * ((47 / 2) ** 2 - 10 ** 2) * 14, Math.PI * (4 ** 2 * 30 + (13 / 2) ** 2 * 8), Math.PI * 3 ** 2 * 24]
    reports.forEach(([original, edited], i) => {
      assert.ok(original.volume > envelopes[i] * .5 && original.volume < envelopes[i], 'internal geometry must remove real volume')
      assert.ok(edited.volume > original.volume)
      assert.ok(Math.abs(edited.centerX - original.centerX - (i === 0 ? 11 : 10)) < 1e-5)
    })
  } finally { rmSync(folder, { recursive: true, force: true }) }
})
