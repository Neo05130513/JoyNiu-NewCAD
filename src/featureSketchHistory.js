// Local draft history is independent of server revisions and parent model undo.
// Every checkpoint contains the whole feature and its parameter map together.
export const SKETCH_HISTORY_LIMIT = 50
const clone = value => structuredClone(value)
const checkpoint = (feature, parameters) => ({ feature: clone(feature), parameters: clone(parameters), key: JSON.stringify([feature, parameters]) })

export function createSketchHistory(feature, parameters = {}) {
  return { past: [], present: checkpoint(feature, parameters), future: [] }
}

export function syncSketchHistory(history, feature, parameters = {}) {
  return history.present.key === JSON.stringify([feature, parameters]) ? history : createSketchHistory(feature, parameters)
}

export function recordSketchHistory(history, feature, parameters = {}) {
  const next = checkpoint(feature, parameters)
  if (next.key === history.present.key) return history
  return { past: [...history.past, history.present].slice(-SKETCH_HISTORY_LIMIT), present: next, future: [] }
}

export function travelSketchHistory(history, redo = false) {
  if (redo) return history.future.length ? { past: [...history.past, history.present].slice(-SKETCH_HISTORY_LIMIT), present: history.future[0], future: history.future.slice(1) } : history
  return history.past.length ? { past: history.past.slice(0, -1), present: history.past.at(-1), future: [history.present, ...history.future] } : history
}

export function sketchHistoryValue(history) {
  return { feature: clone(history.present.feature), parameters: clone(history.present.parameters) }
}
