import test from 'node:test'
import assert from 'node:assert/strict'
import {cadCoordinate,cadBoxSelection,cadCornerOperations,cadMirror,cadTrimOperation} from './nativeCadInteraction.js'
const line=(id,start,end)=>({id,type:'LINE',start,end,editable:true,layer:'0'})
test('absolute, relative and polar coordinates remain bounded without evaluating commands',()=>{
  assert.deepEqual(cadCoordinate('@20,-5',[5,10]),[25,5])
  const p=cadCoordinate('@10<90',[2,3]);assert.ok(Math.abs(p[0]-2)<1e-8);assert.equal(p[1],13)
  assert.equal(cadCoordinate('LINE'),null);assert.equal(cadCoordinate('1+eval(),2'),null)
  assert.throws(()=>cadCoordinate('999999999,0'),/超出/)
})
test('window selection contains entities and crossing selection includes intersecting extents',()=>{
  const entities=[line('inside',[2,2],[4,4]),line('cross',[0,3],[12,3]),line('outside',[20,20],[22,22])]
  assert.deepEqual(cadBoxSelection(entities,[1,1],[8,8]),['inside'])
  assert.deepEqual(cadBoxSelection(entities,[8,8],[1,1]),['inside','cross'])
})
test('fillet has correct tangencies and leaves original geometry unchanged',()=>{
  const entities=[line('a',[0,0],[20,0]),line('b',[0,0],[0,20])],before=structuredClone(entities)
  const ops=cadCornerOperations(entities,3),arc=ops.at(-1).entity
  assert.ok(Math.abs(arc.center[0]-3)<1e-8&&Math.abs(arc.center[1]-3)<1e-8)
  assert.equal(arc.radius,3);assert.equal(ops[0].id,'a');assert.deepEqual(entities,before)
  assert.throws(()=>cadCornerOperations(entities,30),/超过/)
  const chamfer=cadCornerOperations(entities,2,true);assert.equal(chamfer.at(-1).entity.type,'LINE')
})
test('mirroring across an arbitrary line and trimming middle segments generate real entity operations',()=>{
  const mirrored=cadMirror(line('a',[1,0],[2,0]),[0,0],[10,10]);assert.ok(Math.abs(mirrored.start[0])<1e-8);assert.ok(Math.abs(mirrored.start[1]-1)<1e-8)
  const original=line('a',[0,0],[10,0]),boundaries=[line('b',[3,-2],[3,2]),line('c',[7,-2],[7,2])]
  const ops=cadTrimOperation(original,boundaries,[5,0]);assert.equal(ops[0].end,.3);assert.deepEqual(ops[1].entity.start,[7,0,0]);assert.deepEqual(original.end,[10,0])
})
