export function platformScope({ token = '', projectId = '', fileId = '', generation = null }) {
  return { token, projectId, fileId, generationId: generation?.requestId || '', generationStale: Boolean(generation?.stale) }
}

export function platformScopeIsCurrent(expected, current, accountOnly = false) {
  if (!expected || !current || !expected.token || expected.token !== current.token) return false
  return accountOnly || ['projectId', 'fileId', 'generationId', 'generationStale'].every((key) => expected[key] === current[key])
}

export function pdmDocumentName(entry) {
  return entry.versions?.[0]?.metadata?.displayName || entry.document?.metadata?.displayName || entry.document?.name || entry.name || '未命名文档'
}

export function pdmDocumentPage(documents, query = '', page = 0, pageSize = 10) {
  const text = query.trim().toLowerCase()
  const filtered = documents.filter((entry) => !text || [pdmDocumentName(entry), entry.document?.kind,
    ...(entry.versions || []).map((version) => version.file_name || version.fileName || '')].join(' ').toLowerCase().includes(text))
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize))
  const currentPage = Math.max(0, Math.min(page, pageCount - 1))
  return { items: filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize), total: filtered.length, page: currentPage, pageCount }
}
