export const activeTaskStatuses = ['queued', 'running', 'cancel_requested']
export const taskStatusNames = { queued: '排队中', running: '处理中', cancel_requested: '正在取消', cancelled: '已取消', interrupted: '处理已中断', failed: '本轮未完成', needs_input: '等待你补充信息', review_required: '等待核对模型', ready: '已确认，可交付' }
export const taskStatusLabel = status => taskStatusNames[status] || '状态待更新'
export const taskPageOffset = (total, offset, limit = 20) => Math.min(Math.max(0, offset), Math.max(0, Math.ceil(total / limit) - 1) * limit)
export const filterTaskPage = (items, filter) => items.filter(item => filter === 'all' || (filter === 'active' ? activeTaskStatuses.includes(item.status) : ['needs_input', 'review_required', 'failed', 'interrupted'].includes(item.status)))

export function cancellationResult(result, runId) {
  if (!result || result.runId !== runId || !Object.hasOwn(taskStatusNames, result.status)) throw new Error('取消接口返回了无效任务状态，请刷新任务后重试。')
  return {
    patch: Object.fromEntries(['status', 'message', 'progress', 'revision', 'updatedAt'].filter(key => result[key] !== undefined).map(key => [key, result[key]])),
    notice: result.status === 'cancelled' ? '任务已确认取消。' : activeTaskStatuses.includes(result.status)
      ? '尚未确认停止，页面将继续查询任务状态。' : `任务当前状态：${taskStatusLabel(result.status)}。`,
  }
}

/** One read at a time; hidden tabs pause and recover immediately when visible. */
export function createTaskPoller({ load, onValue, onError, isVisible = () => true, schedule = setTimeout, unschedule = clearTimeout }) {
  let stopped = false, running = false, timer = null, refreshRequested = false, epoch = 0
  const clear = () => { if (timer !== null) unschedule(timer); timer = null }
  async function tick() {
    clear()
    if (stopped || !isVisible()) return
    if (running) { refreshRequested = true; return }
    running = true
    const reading = epoch
    let delay = 5000
    try {
      const value = await load()
      if (!stopped && reading === epoch) onValue(value)
    } catch (error) {
      delay = 15000
      if (!stopped && reading === epoch) onError(error)
    } finally {
      running = false
      if (!stopped && isVisible()) timer = schedule(tick, refreshRequested ? 0 : delay)
      refreshRequested = false
    }
  }
  return { start: tick, refresh: () => { epoch++; return tick() }, visibilityChanged: () => { clear(); if (isVisible()) return tick() }, stop: () => { stopped = true; clear() } }
}
