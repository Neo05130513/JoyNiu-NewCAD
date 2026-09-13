import test from 'node:test'
import assert from 'node:assert/strict'
import { createSketchHistory, recordSketchHistory, sketchHistoryValue, syncSketchHistory, travelSketchHistory, SKETCH_HISTORY_LIMIT } from './featureSketchHistory.js'
const feature={id:'sketch',op:'profile_extrude',start:[0,0],segments:[{type:'line',to:['width',0]}],sketchConstraints:[{id:'h1',type:'horizontal',edge:0}]}

test('feature expressions, constraints and parameter provenance travel atomically without mutable checkpoint aliases',()=>{
  const parameters={width:{value:20,source:{type:'user',text:'confirmed'}}},original=createSketchHistory(feature,parameters)
  const circle={...feature,segments:[{type:'arc',to:[0,2],through:[2,0]}],sketchConstraints:[]}
  const changed=recordSketchHistory(original,circle,{width:{...parameters.width,value:30}})
  circle.segments[0].through[0]=999;parameters.width.value=999
  const undone=travelSketchHistory(changed),snapshot=sketchHistoryValue(undone)
  assert.deepEqual(snapshot.feature,feature);assert.equal(snapshot.parameters.width.value,20)
  snapshot.feature.sketchConstraints.length=0;snapshot.parameters.width.source.text='mutated'
  const redone=travelSketchHistory(undone,true)
  assert.equal(redone.present.feature.segments[0].through[0],2)
  assert.equal(redone.present.parameters.width.value,30)
  assert.equal(travelSketchHistory(redone).present.parameters.width.source.text,'confirmed')
})

test('identical edits keep redo, a new branch clears redo, and external replacement resets bounded local history',()=>{
  let history=createSketchHistory(feature)
  for(let index=1;index<=80;index++)history=recordSketchHistory(history,{...feature,distance:index})
  assert.equal(history.past.length,SKETCH_HISTORY_LIMIT)
  const undo=travelSketchHistory(history)
  assert.equal(recordSketchHistory(undo,undo.present.feature,undo.present.parameters),undo)
  assert.equal(syncSketchHistory(undo,structuredClone(undo.present.feature),{}),undo)
  assert.equal(recordSketchHistory(undo,{...feature,distance:91}).future.length,0)
  const external=syncSketchHistory(undo,{...feature,id:'different-file-sketch'})
  assert.equal(external.past.length,0);assert.equal(external.future.length,0)
  assert.equal(travelSketchHistory(external),external)
})
