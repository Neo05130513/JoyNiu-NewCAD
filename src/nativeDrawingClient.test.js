import test from 'node:test'
import assert from 'node:assert/strict'
import {nativeDrawingRequest} from './nativeDrawingClient.js'
import {nativePolylineText,parseNativePolylineText,nativePropertyDirty} from './nativeDrawingDraft.js'

test('dropped response is retryable while validation conflicts remain explicit',async t=>{
  t.mock.method(globalThis,'fetch',async()=>{throw new TypeError('Failed to fetch')})
  await assert.rejects(nativeDrawingRequest('/id/operations',{token:'test',method:'POST',body:{requestId:'stable'}}),e=>e.retryable===true&&/连接中断/.test(e.message))
  globalThis.fetch=async()=>new Response(JSON.stringify({detail:{message:'版本冲突'}}),{status:409})
  await assert.rejects(nativeDrawingRequest('/id/operations',{token:'test',method:'POST'}),e=>e.status===409&&!e.retryable&&e.message==='版本冲突')
})
test('stalled requests expire and preserve a recoverable outcome',async t=>{
  t.mock.method(globalThis,'fetch',(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')))))
  await assert.rejects(nativeDrawingRequest('/id',{token:'test',timeoutMs:5}),e=>e.retryable===true&&/超时/.test(e.message))
})
test('polyline editing preserves incomplete draft text until explicit validation',()=>{
  assert.deepEqual(parseNativePolylineText('-2.5,0\n10,20,.5'),[[-2.5,0,0],[10,20,.5]])
  assert.throws(()=>parseNativePolylineText('0,0\n10,'),/不能留空/)
  assert.throws(()=>parseNativePolylineText('0,0\n10,20,not-number'),/不能留空/)
  const entity={id:'1',type:'LWPOLYLINE',points:[[0,0,0],[10,20,0]]},document={entities:[entity]}
  assert.equal(nativePropertyDirty(document,structuredClone(entity),nativePolylineText(entity.points)),false)
  assert.equal(nativePropertyDirty(document,structuredClone(entity),'0,0\n10,'),true)
  assert.deepEqual(entity.points,[[0,0,0],[10,20,0]])
})
