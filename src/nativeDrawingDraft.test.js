import test from 'node:test'
import assert from 'node:assert/strict'
import {nativeDimensionDraft, nativeLayouts, validNativeLayout, visibleNativeEntities} from './nativeDrawingDraft.js'

const draft = (points, extra = {}) => nativeDimensionDraft({kind: 'aligned', layer: '0', points, ...extra})
test('aligned dimensions place slanted and vertical dimension lines on the clicked side', () => {
  assert.equal(draft([[0, 0], [3, 4], [-4, 3]]).distance, 5)
  assert.equal(draft([[0, 0], [3, 4], [4, -3]]).distance, -5)
  assert.equal(draft([[10, 20], [10, 40], [4, 80]]).distance, 6)
  assert.equal(draft([[10, 40], [10, 20], [4, 80]]).distance, -6)
  assert.equal(draft([[0, 0], [3, 4], [-1, 7]]).distance, 5)
})
test('dimension drafts reject coincident definitions before submitting native geometry', () => {
  assert.throws(() => draft([[1, 2], [1, 2], [3, 4]]), /不能重合/)
  assert.throws(() => draft([[0, 0], [1, 0], [0, 0]], {kind: 'angular'}), /零长度/)
})
test('associated dimensions bind only backend supported source types and prefer selected entities', () => {
  const entities = [{id: 'unsupported', type: 'SPLINE', start: [0, 0]}, {id: 'a', type: 'LINE', start: [0, 0], end: [5, 5]}, {id: 'b', type: 'LINE', start: [0, 0], end: [3, 4]}]
  const dim = draft([[0, 0], [3, 4], [-4, 3]], {entities, preferredIds: ['b']})
  assert.deepEqual(dim.sourceRefs, {p1: {entityId: 'b', point: 'start'}, p2: {entityId: 'b', point: 'end'}})
  assert.equal(entities[0].id, 'unsupported')
})
test('circle dimensions bind to the actual matching radius and angular dimensions retain each ray', () => {
  const entities = [{id: 'a', type: 'CIRCLE', center: [0, 0], radius: 5}, {id: 'b', type: 'LINE', start: [0, 0], end: [0, 5]}]
  const circle = draft([[0, 0], [3, 4], [0, -5]], {kind: 'diameter', entities})
  assert.deepEqual(circle.sourceRefs, {circle: {entityId: 'a'}})
  assert.equal(circle.angle, -90)
  const angular = draft([[0, 0], [3, 4], [0, 5]], {kind: 'angular', entities})
  assert.deepEqual(angular.p1, [3, 4])
  assert.deepEqual(angular.sourceRefs.p2, {entityId: 'b', point: 'end'})
})
test('switching drawings keeps a valid layout or falls back to a layout in the new drawing', () => {
  assert.equal(validNativeLayout(['Model', '图框 A'], '图框 A'), '图框 A')
  assert.equal(validNativeLayout(['Model', '图框 B'], '图框 A'), 'Model')
  assert.equal(validNativeLayout(['图框 B'], '图框 A'), '图框 B')
  assert.equal(validNativeLayout([], '图框 A'), 'Model')
  assert.deepEqual(nativeLayouts(['Model', 'Model', null]), ['Model'])
})
test('hidden source entities cannot silently acquire a dimension association or snap', () => {
  const entities = visibleNativeEntities({entities: [{id: 'hidden', type: 'LINE', layer: '隐藏', start: [0, 0], end: [3, 4]}, {id: 'visible', type: 'LINE', layer: '0', start: [8, 8], end: [9, 9]}], layers: [{name: '隐藏', visible: false}]})
  assert.deepEqual(entities.map(entity => entity.id), ['visible'])
  assert.equal(draft([[0, 0], [3, 4], [-4, 3]], {entities}).sourceRefs, undefined)
})

test('frozen layers never participate in snapping or source association',()=>{
  assert.deepEqual(visibleNativeEntities({entities:[{id:'hidden',layer:'ICE'}],layers:[{name:'ICE',visible:true,frozen:true}]}),[])
})

test('hidden INSERT and DIMENSION parents hide their flattened display entities',async()=>{
  const {nativeVisibleRenderEntities}=await import('./nativeDrawingDraft.js')
  const document={entities:[{id:'block',type:'INSERT',layer:'HIDE',color:3},{id:'dimension',type:'DIMENSION',layer:'SHOW',color:2}],renderEntities:[{id:'block:1',parentId:'block',type:'LINE',layer:'0',color:0},{id:'dimension:1',parentId:'dimension',type:'LINE',layer:'0',color:0}],layers:[{name:'HIDE',visible:false},{name:'SHOW',visible:true},{name:'0',visible:false}]}
  assert.deepEqual(nativeVisibleRenderEntities(document),[{id:'dimension:1',parentId:'dimension',type:'LINE',layer:'SHOW',color:2}])
})
