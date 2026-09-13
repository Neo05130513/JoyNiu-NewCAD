import { useEffect, useRef, useState } from 'react'
import { directFeatureClient } from './directFeatureClient.js'
import { featureErrorFeedback } from './directFeatureModel.js'
import { cadPreviewQueue } from './cadPreviewQueue.js'

// A cancelled or superseded response cannot become the visible transaction.
// Cache belongs to one mounted document and is cleared on account/doc changes.
export default function useCadPreview({ draft, featureId, token, record, scope, active = true, enabled = true, paused = false, delay = 450, queue = cadPreviewQueue }) {
  const [state, setState] = useState({ geometry: null, loading: false, error: '', key: '' })
  const [retry, setRetry] = useState(0)
  const cache = useRef(new Map()), credentials = useRef(token)
  credentials.current = token
  const key = JSON.stringify([scope, draft?.plan, draft?.suppressed, featureId, record?.id, record?.revision])
  useEffect(() => { cache.current.clear(); setState({ geometry:null,loading:false,error:'',key:'' }) }, [scope])
  useEffect(() => {
    if (!active || !enabled || paused || !draft?.plan?.features?.length || !token) return
    const abort = new AbortController(), cached = cache.current.get(key)
    if (cached) { setState({ geometry:cached,loading:false,error:'',key }); return }
    setState(previous => ({...previous,loading:true,error:'',key}))
    const timer = setTimeout(async () => {
      try {
        const geometry = await queue.preview(() => directFeatureClient.preview(() => credentials.current, {
          plan:draft.plan, suppressed:draft.suppressed || [], ...(featureId ? {featureId} : {}),
          ...(record ? {designId:record.id,expectedRevision:record.revision} : {}),
        }), abort.signal)
        if (abort.signal.aborted) return
        cache.current.set(key,geometry)
        while(cache.current.size>4)cache.current.delete(cache.current.keys().next().value)
        setState({geometry,loading:false,error:'',key})
      } catch(error) { if(!abort.signal.aborted)setState(previous=>({...previous,loading:false,error:featureErrorFeedback(error).message,key})) }
    }, delay)
    return () => { clearTimeout(timer); abort.abort() }
  }, [key, active, enabled, paused, Boolean(token), delay, retry, queue])
  // Old triangles must never be offered as selectable geometry for a new
  // plan/input, or after disabling this account/document's preview.
  const current = Boolean(active && enabled && token && state.geometry && state.key === key && !state.loading && !state.error)
  return { ...state, error:active&&enabled&&state.key===key?state.error:'', loading:Boolean(active&&enabled&&!paused&&state.key===key&&state.loading), geometry:current ? state.geometry : null, current,
    retry:()=>{cache.current.delete(key);setRetry(value=>value+1)} }
}
