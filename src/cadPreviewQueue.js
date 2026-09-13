// Shared by mounted editors and document scopes. A sent preview occupies its
// slot until real HTTP settlement, even when its UI result is obsolete.
export function createCadPreviewQueue() {
  const pending=[]
  let running=null
  const aborted=()=>new DOMException('已取消等待预览', 'AbortError')
  const pump=()=>{
    if(running||!pending.length)return
    const preferred=pending.findIndex(item=>item.kind==='commit')
    const item=pending.splice(preferred<0?0:preferred,1)[0]
    if(item.signal?.aborted){item.cleanup();item.reject(aborted());pump();return}
    running=item
    Promise.resolve().then(()=>item.signal?.aborted?Promise.reject(aborted()):item.run()).then(value=>finish(item,null,value),error=>finish(item,error))
  }
  const finish=(item,error,value)=>{
    item.cleanup();running=null
    if(error)item.reject(error);else item.resolve(value)
    pump()
  }
  const enqueue=(kind,run,signal)=>new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(aborted());return}
    const item={kind,run,signal,resolve,reject,cleanup:()=>signal?.removeEventListener('abort',cancel)}
    const cancel=()=>{
      // Aborting a sent fetch would reject before the server releases its slot.
      // Keep waiting instead; the hook discards that obsolete response.
      const index=pending.indexOf(item)
      if(index>=0){pending.splice(index,1);item.cleanup();reject(aborted())}
    }
    signal?.addEventListener('abort',cancel,{once:true})
    pending.push(item);pump()
  })
  return {preview:(run,signal)=>enqueue('preview',run,signal),commit:(run,signal)=>enqueue('commit',run,signal)}
}
export const cadPreviewQueue=createCadPreviewQueue()
