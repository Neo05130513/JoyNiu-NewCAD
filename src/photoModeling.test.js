import test from 'node:test'
import assert from 'node:assert/strict'
import{photoModelingPrompt,addPhotoFiles,setPhotoPixelCoordinate}from'./photoModeling.js'
import{MECHANICAL_CASES}from'./mechanicalCases.js'
test('photo calibration preserves actual pixels and explicit dimensional authority',()=>{const prompt=photoModelingPrompt({points:[[.1,.2],[.6,.2]],width:1000,height:600,referenceLength:50,description:'支架',knownDimensions:'厚度 8 mm',filename:'part.png'});assert.match(prompt,/500.000 像素/);assert.match(prompt,/实际距离由用户给定为 50 mm/);assert.match(prompt,/厚度 8 mm/);assert.match(prompt,/不保证透视/);assert.match(prompt,/不静默补上默认几何/)})
test('photo calibration rejects degenerate scales and lacks no required description',()=>{const args={points:[[.2,.2],[.2,.2]],width:100,height:100,referenceLength:50,description:'零件'};assert.throws(()=>photoModelingPrompt(args));assert.throws(()=>photoModelingPrompt({...args,points:[[0,0],[1,1]],referenceLength:0}));assert.throws(()=>photoModelingPrompt({...args,points:[[0,0],[1,1]],description:''}))})
test('case library has stable independent editable plans and all operation families',()=>{assert.equal(new Set(MECHANICAL_CASES.map(c=>c.id)).size,MECHANICAL_CASES.length);const ops=new Set(MECHANICAL_CASES.flatMap(c=>c.plan.features.map(f=>f.op)));for(const op of ['gear','spring','shell','chamfer','sweep','loft','linear_pattern','circular_pattern','profile_extrude','profile_revolve'])assert.ok(ops.has(op));for(const c of MECHANICAL_CASES){assert.ok(c.plan.features.some(f=>f.id===c.plan.result));assert.ok(Object.keys(c.plan.parameters).length>0)}})

test('photo calibration requires a loaded image and normalized point coordinates',()=>{const args={points:[[.1,.2],[.6,.2]],width:1000,height:600,referenceLength:50,description:'支架'};for(const update of [{width:0},{height:NaN},{points:[[NaN,0],[1,1]]},{points:[[-.1,0],[1,1]]},{points:[[0,0,1],[1,1]]},{points:[null,[1,1]]}])assert.throws(()=>photoModelingPrompt({...args,...update}))})

test('entering one pixel coordinate never invents the other three reference coordinates', () => {
  const size = {width: 960, height: 720}
  let points = setPhotoPixelCoordinate([], 0, 0, '100', size)
  assert.deepEqual(points, [[100 / 960, null], [null, null]])
  assert.throws(() => photoModelingPrompt({points, ...size, referenceLength: 20, description: '齿轮'}), /参考点/)
  for (const [i, axis, value] of [[0, 1, '100'], [1, 0, '300'], [1, 1, '100']]) points = setPhotoPixelCoordinate(points, i, axis, value, size)
  assert.match(photoModelingPrompt({points, ...size, referenceLength: 20, description: '齿轮'}), /200.000 像素/)
  const cleared = setPhotoPixelCoordinate(points, 0, 1, '', size)
  assert.equal(cleared[0][1], null)
  assert.equal(points[0][1], 100 / 720)
  assert.throws(() => setPhotoPixelCoordinate(points, 1, 0, '961', size), /0 到 960/)
  assert.throws(() => setPhotoPixelCoordinate(points, 1, 0, '-1', size), /坐标/)
  assert.equal(setPhotoPixelCoordinate(points, 0, 0, '0.25', size)[0][0], .25 / 960)
})

test('adding supplemental photos preserves main photo and rejects invalid or excessive selections atomically', () => {
  const file = (name, size = 100, type = 'image/png') => ({name, size, type, lastModified: 42})
  const first = file('main.png'), side = file('side.png')
  const existing = [first]
  assert.deepEqual(addPhotoFiles(existing, []), existing)
  assert.deepEqual(addPhotoFiles(existing, [first, side]), [first, side])
  assert.equal(existing.length, 1)
  assert.throws(() => addPhotoFiles(existing, [file('empty.png', 0)]), /不能为空/)
  assert.throws(() => addPhotoFiles(existing, [file('large.png', 21 * 1024 * 1024)]), /20 MB/)
  assert.throws(() => addPhotoFiles(existing, [file('document.pdf', 100, 'application/pdf')]), /图片/)
  assert.throws(() => addPhotoFiles(existing, ['a', 'b', 'c', 'd'].map(name => file(name + '.png'))), /最多添加 4/)
  assert.equal(existing.length, 1)
  assert.deepEqual(addPhotoFiles([], [file('unknown.JPG', 100, '')]).map(item => item.name), ['unknown.JPG'])
})
