/** Refresh capability URLs without replacing edited geometry or historical facts. */
export function refreshCadResourceLinks(model, generation, result) {
  if (!model?.agentRun?.runId || result?.runId !== model.agentRun.runId || result.revision !== model.agentRun.revision) return null
  const resources = Object.fromEntries(['artifacts', 'sourceFiles', 'sourceDocuments', 'fileLinksExpiresAt', 'fileLinksEpoch'].filter(key => result[key] !== undefined).map(key => [key, result[key]]))
  const updated = { ...model, agentRun: { ...model.agentRun, ...resources } }
  const refreshedGeneration = generation?.runId === result.runId && generation.revision === result.revision
    ? { ...generation, artifacts: result.artifacts || [], fileLinksExpiresAt: result.fileLinksExpiresAt } : generation
  return { model: updated, generation: refreshedGeneration }
}

export function cadResourceRefreshDelay(expiresAt, now = Date.now()) {
  const expires = typeof expiresAt === 'number' ? (expiresAt < 1e12 ? expiresAt * 1000 : expiresAt) : Date.parse(expiresAt)
  return Number.isFinite(expires) ? Math.max(15000, Math.min(45 * 60 * 1000, expires - now - 5 * 60 * 1000)) : 45 * 60 * 1000
}
