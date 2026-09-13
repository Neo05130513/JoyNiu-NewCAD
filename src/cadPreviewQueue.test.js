import test from 'node:test'
import assert from 'node:assert/strict'
import { createCadPreviewQueue } from './cadPreviewQueue.js'
import { createDirectFeatureClient } from './directFeatureClient.js'

const turn=()=>new Promise(resolve=>setImmediate(resolve))
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return{promise,resolve,reject}}

test('one sent preview survives UI cancellation; commit goes before queued previews only after real settlement',async()=>{
  const queue=createCadPreviewQueue(),sent=deferred(),commit=deferred(),abort=new AbortController(),events=[]
  const first=queue.preview(()=>{events.push('first');return sent.promise},abort.signal)
  const queued=queue.preview(()=>{events.push('second');return 'second'})
  await turn();abort.abort()
  const save=queue.commit(()=>{events.push('commit');return commit.promise})
  await turn();assert.deepEqual(events,['first'])
  sent.resolve('obsolete');await first;await turn();assert.deepEqual(events,['first','commit'])
  commit.resolve('saved');assert.equal(await save,'saved');assert.equal(await queued,'second')
  assert.deepEqual(events,['first','commit','second'])
})

test('cancelled queued previews and completion intents never execute and do not stall later work',async()=>{
  const queue=createCadPreviewQueue(),sent=deferred(),old=new AbortController(),saveAbort=new AbortController(),events=[]
  const first=queue.preview(()=>sent.promise)
  const stale=queue.preview(()=>assert.fail('obsolete preview must not send'),old.signal).catch(error=>error.name)
  const save=queue.commit(()=>assert.fail('cancelled completion must not send'),saveAbort.signal).catch(error=>error.name)
  old.abort();saveAbort.abort();await turn()
  assert.equal(await stale,'AbortError');assert.equal(await save,'AbortError')
  const next=queue.preview(()=>events.push('new-document'))
  assert.deepEqual(events,[]);sent.resolve();await first;await next;assert.deepEqual(events,['new-document'])
})

test('a real HTTP preview error releases the queue and does not permanently disable final validation',async()=>{
  const queue=createCadPreviewQueue(),sent=deferred(),events=[]
  const preview=queue.preview(()=>sent.promise).catch(error=>error.message)
  const commit=queue.commit(()=>events.push('final-validation'))
  sent.reject(Object.assign(new Error('圆角半径过大'),{status:422}))
  assert.equal(await preview,'圆角半径过大');await commit;assert.deepEqual(events,['final-validation'])
})

test('the existing finite preview HTTP timeout bounds a stalled network and reports failure without fabricating success',async()=>{
  const originalSet=globalThis.setTimeout,originalClear=globalThis.clearTimeout,timers=[]
  globalThis.setTimeout=(callback,ms)=>{const timer={callback,ms};timers.push(timer);return timer}
  globalThis.clearTimeout=()=>{}
  try{
    const client=createDirectFeatureClient({fetchImpl:(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('network aborted','AbortError')),{once:true}))})
    const queue=createCadPreviewQueue(),request=queue.preview(()=>client.preview('test-token',{plan:{}})).catch(error=>error.message)
    await Promise.resolve();assert.equal(timers[0].ms,120000);assert.ok(timers[0].ms>45000)
    timers[0].callback();assert.match(await request,/超时/)
    // A subsequent explicit action can proceed; no operation was synthesized
    // automatically from the network failure.
    assert.equal(await queue.commit(()=> 'explicit retry'),'explicit retry')
  }finally{globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear}
})
