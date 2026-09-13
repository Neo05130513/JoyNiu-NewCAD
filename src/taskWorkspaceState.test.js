import test from 'node:test'
import assert from 'node:assert/strict'
import { cancellationResult, createTaskPoller, filterTaskPage, taskPageOffset } from './taskWorkspaceState.js'
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
function timerHarness() {
  let sequence = 0
  const tasks = new Map()
  return { tasks, schedule: (fn, delay) => { const id = ++sequence; tasks.set(id, { fn, delay }); return id }, unschedule: id => tasks.delete(id) }
}

test('task pagination clamps vanished pages without inventing a page of records', () => {
  assert.equal(taskPageOffset(0, 40), 0)
  assert.equal(taskPageOffset(20, 20), 0)
  assert.equal(taskPageOffset(21, 60), 20)
  assert.equal(taskPageOffset(45, 20), 20)
  const jobs = [{ status: 'ready' }, { status: 'cancel_requested' }, { status: 'review_required' }]
  assert.deepEqual(filterTaskPage(jobs, 'active'), [jobs[1]])
  assert.deepEqual(filterTaskPage(jobs, 'attention'), [jobs[2]])
})

test('a successful cancel HTTP response cannot fabricate cancelled status', () => {
  const running = cancellationResult({ runId: 'run1', status: 'running' }, 'run1')
  assert.equal(running.patch.status, 'running')
  assert.match(running.notice, /尚未确认停止/)
  assert.equal(cancellationResult({ runId: 'run1', status: 'cancel_requested' }, 'run1').patch.status, 'cancel_requested')
  assert.equal(cancellationResult({ runId: 'run1', status: 'cancelled' }, 'run1').notice, '任务已确认取消。')
  assert.doesNotMatch(cancellationResult({ runId: 'run1', status: 'ready' }, 'run1').notice, /已确认取消/)
  assert.throws(() => cancellationResult({ runId: 'other', status: 'cancelled' }, 'run1'))
  assert.throws(() => cancellationResult({}, 'run1'))
  assert.throws(() => cancellationResult({ runId: 'run1', status: 'unknown' }, 'run1'))
  assert.deepEqual(cancellationResult({ runId: 'run1', status: 'cancel_requested' }, 'run1').patch, { status: 'cancel_requested' })
})

test('hidden task pages make no requests and resume when visible', async () => {
  const timers = timerHarness()
  let visible = false, calls = 0
  const poller = createTaskPoller({ ...timers, isVisible: () => visible, load: async () => { calls++; return {} }, onValue: () => {}, onError: () => {} })
  await poller.start()
  assert.equal(calls, 0); assert.equal(timers.tasks.size, 0)
  visible = true; await poller.visibilityChanged()
  assert.equal(calls, 1); assert.equal([...timers.tasks.values()][0].delay, 5000)
  visible = false; poller.visibilityChanged()
  assert.equal(timers.tasks.size, 0)
  poller.stop()
})

test('manual refresh during a read suppresses the old response and schedules one new read', async () => {
  const timers = timerHarness(), first = deferred(), received = []
  let calls = 0
  const poller = createTaskPoller({ ...timers, load: () => { calls++; return calls === 1 ? first.promise : Promise.resolve('new') }, onValue: value => received.push(value), onError: () => {} })
  const initial = poller.start()
  await poller.refresh(); await poller.refresh()
  assert.equal(calls, 1)
  first.resolve('old'); await initial
  assert.deepEqual(received, [])
  assert.equal(timers.tasks.size, 1)
  assert.equal([...timers.tasks.values()][0].delay, 0)
  await [...timers.tasks.values()][0].fn()
  assert.deepEqual(received, ['new']); assert.equal(calls, 2)
  poller.stop()
})

test('failed task reads stay errors and retry without fake empty success', async () => {
  const timers = timerHarness(), values = [], errors = []
  const poller = createTaskPoller({ ...timers, load: async () => { throw new Error('offline') }, onValue: value => values.push(value), onError: error => errors.push(error.message) })
  await poller.start()
  assert.deepEqual(values, []); assert.deepEqual(errors, ['offline'])
  assert.equal([...timers.tasks.values()][0].delay, 15000)
  poller.stop(); assert.equal(timers.tasks.size, 0)
})

test('closing an account ignores a delayed task response and leaves no timers', async () => {
  const timers = timerHarness(), wait = deferred(), values = []
  const poller = createTaskPoller({ ...timers, load: () => wait.promise, onValue: value => values.push(value), onError: () => {} })
  const running = poller.start(); poller.stop(); wait.resolve('old account')
  await running
  assert.deepEqual(values, []); assert.equal(timers.tasks.size, 0)
})
