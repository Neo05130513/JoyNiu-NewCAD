import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { cadAgent, cadArtifactUrl } from './cadAgentClient.js'
import { localizedSourceAnnotations, normalizedDimensionBox } from './sourceDrawingState.js'
import { downloadSourceFile, latestSourceFileRun, refreshSourceFileRun } from './sourceFileAccess.js'
import { downloadBlob } from './workspaceFeedback.js'

const imageFile = (file) => /^image\/(png|jpeg|webp|gif|bmp)$/i.test(file.type) || /\.(png|jpe?g|webp|gif|bmp)$/i.test(file.name)

export default function useSourceDrawing(model, drawingJob, token) {
  const run = model?.agentRun, runId = run?.runId, revision = run?.revision
  const identity = `${runId || ''}:${revision ?? ''}`
  const [loaded, setLoaded] = useState(null)
  const [fileRefresh, setFileRefresh] = useState(null)
  const fileRequests = useRef(new Set())
  const contextRef = useRef({ identity, token })
  contextRef.current = { identity, token }
  useEffect(() => () => { fileRequests.current.forEach(controller => controller.abort()); fileRequests.current.clear() }, [identity, token])
  const accessFiles = useCallback(async (documentId) => {
    if (!runId) return
    const controller = new AbortController()
    fileRequests.current.add(controller)
    const current = () => !controller.signal.aborted && contextRef.current.identity === identity && contextRef.current.token === token
    try {
      const options = { runId, revision, token, signal: controller.signal }
      const result = documentId ? await downloadSourceFile({ ...options, documentId }) : { run: await refreshSourceFileRun(options) }
      if (current()) {
        setFileRefresh({ identity, token, run: result.run })
        if (result.blob) downloadBlob(result.blob, result.filename)
      }
      return result.run
    } finally { fileRequests.current.delete(controller) }
  }, [identity, runId, revision, token])
  const [attempt, setAttempt] = useState(0)
  const [locationRequest, setLocationRequest] = useState({ identity: null, attempt: 0 })
  const locationAttempt = locationRequest.identity === identity ? locationRequest.attempt : 0
  const [localization, setLocalization] = useState(null)
  const lastLocationRequest = useRef(null)
  const [localDocuments, setLocalDocuments] = useState({ files: null, documents: [] })
  const files = useMemo(() => (drawingJob?.files || [drawingJob?.file]).filter((file) => typeof File !== 'undefined' && file instanceof File), [drawingJob?.files, drawingJob?.file])
  useEffect(() => {
    const urls = []
    const documents = files.map((file, index) => {
      const url = URL.createObjectURL(file); urls.push(url)
      return { id: `local-${index}`, filename: file.name, downloadUrl: url, pages: imageFile(file) ? [{ id: `local-${index}-1`, page: 1, url }] : [],
        error: imageFile(file) ? '' : '图纸处理完成后，可在这里查看各页和尺寸位置。' }
    })
    setLocalDocuments({ files, documents })
    return () => urls.forEach((url) => URL.revokeObjectURL(url))
  }, [files])
  useEffect(() => {
    if (!runId) return
    const controller = new AbortController()
    setLoaded({ identity, loading: true })
    cadAgent.getRun({ runId, revision, token, signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setLoaded({ identity, run: result, loading: false })
    }).catch((error) => {
      if (!controller.signal.aborted) setLoaded({ identity, error: `暂时无法读取已保存的原图：${error.message}`, loading: false })
    })
    return () => controller.abort()
  }, [runId, revision, identity, token, attempt])
  const current = loaded?.identity === identity ? loaded : null
  const savedRun = current?.run || run
  const currentLocalization = localization?.identity === identity ? localization : null
  const sourceRun = useMemo(() => currentLocalization?.result
    ? { ...savedRun, sourceDimensionLocations: currentLocalization.result } : savedRun, [savedRun, currentLocalization?.result])
  const canLocate = Boolean(runId && savedRun?.sourceTranscription?.annotations?.length)
  useEffect(() => {
    // Fetch the durable run first, so cached positions survive page reloads and
    // a page opened from old browser storage does not repeat provider work.
    if (!current?.run || !canLocate) {
      if (current?.error) setLocalization((previous) => previous?.identity === identity ? { ...previous, loading: false } : previous)
      return
    }
    const annotations = localizedSourceAnnotations(current.run)
    const needsLocations = annotations.some((annotation) => annotation.text && !normalizedDimensionBox(annotation.bbox))
    const requestKey = `${identity}:${locationAttempt}`
    const force = locationAttempt > 0 && lastLocationRequest.current !== requestKey
    const activeProgress = current.run.sourceDimensionLocationProgress
    const resuming = activeProgress?.status === 'running' && activeProgress.runId === runId && activeProgress.revision === current.run.revision
    if (!force && !resuming && (current.run.sourceDimensionLocations || !needsLocations)) {
      setLocalization({ identity, result: current.run.sourceDimensionLocations, loading: false, error: '' })
      return
    }
    lastLocationRequest.current = requestKey
    const controller = new AbortController()
    setLocalization((previous) => ({ ...(previous?.identity === identity ? previous : {}), identity, loading: true, error: '' }))
    cadAgent.waitForSourceDimensions({ runId, revision: current.run.revision, token, force, signal: controller.signal, resume: activeProgress,
      onProgress: (progress) => {
        if (!controller.signal.aborted) setLocalization((previous) => ({ ...(previous?.identity === identity ? previous : {}),
          identity, loading: true, error: '', progress,
          ...(progress.annotations?.length ? { result: progress } : {}),
        }))
      },
    })
      .then((result) => {
        if (!controller.signal.aborted) setLocalization({ identity, result, loading: false,
          error: result.status === 'unavailable' ? '暂时无法识别尺寸位置，可以重试定位。' : '' })
      }).catch((error) => {
        if (!controller.signal.aborted) setLocalization((previous) => ({ ...(previous?.identity === identity ? previous : {}),
          identity, loading: false, error: `尺寸定位未完成：${error.message}` }))
      })
    return () => controller.abort()
  }, [current?.run, current?.error, canLocate, identity, runId, token, locationAttempt])
  const fileRun = latestSourceFileRun(run, current?.run, fileRefresh?.identity === identity && fileRefresh.token === token ? fileRefresh.run : null)
  const documents = useMemo(() => (fileRun?.sourceDocuments || []).map((document) => ({ ...document,
    downloadUrl: cadArtifactUrl({ url: document.downloadUrl }),
    pages: (document.pages || []).map((page) => ({ ...page, url: cadArtifactUrl(page) })),
  })), [fileRun?.sourceDocuments])
  return { documents: documents.length ? documents : localDocuments.files === files ? localDocuments.documents : [], sourceRun,
    locating: Boolean(currentLocalization?.loading), locationError: currentLocalization?.error
      || (sourceRun?.sourceDimensionLocations?.status === 'unavailable' ? '暂时无法识别尺寸位置，可以重试定位。' : ''),
    canLocate, locate: () => setLocationRequest((value) => ({ identity, attempt: (value.identity === identity ? value.attempt : 0) + 1 })),
    locationProgress: currentLocalization?.progress,
    loading: Boolean(current?.loading), error: current?.error || (documents.some((document) => document.error) ? '部分原图预览暂时不可用，请重新加载或查看原文件。' : ''), retry: () => setAttempt((value) => value + 1),
    refreshFiles: runId ? () => accessFiles() : null, downloadSource: runId ? documentId => accessFiles(documentId) : null,
    hasSource: Boolean(files.length || run?.sourceFiles?.length || sourceRun?.sourceDocuments?.length || drawingJob?.fileMeta?.name) }
}
