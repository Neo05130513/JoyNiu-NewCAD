import { API_BASE } from './api.js'
const base = new URL('/api/cad/drawings', API_BASE).href
export async function nativeDrawingRequest(path='', {token,method='GET',body,signal,blob=false,timeoutMs=120000}={}) {
  if(!token)throw new Error('请先登录后使用原生二维工作台')
  const headers={Authorization:`Bearer ${typeof token==='function'?token():token}`}
  if(body && !(body instanceof FormData))headers['Content-Type']='application/json'
  const controller=new AbortController(),abort=()=>controller.abort(),timer=setTimeout(abort,timeoutMs)
  if(signal?.aborted)abort()
  signal?.addEventListener('abort',abort,{once:true})
  try {
    const response=await fetch(`${base}${path}`,{method,headers,body:body instanceof FormData?body:body?JSON.stringify(body):undefined,signal:controller.signal})
    if(!response.ok){const data=await response.json().catch(()=>null);const error=new Error(typeof data?.detail==='string'?data.detail:data?.detail?.message||`请求失败（${response.status}）`);error.status=response.status;error.retryable=response.status>=500;throw error}
    return await (blob?response.blob():response.json())
  } catch(error) {
    if(error.status)throw error
    const failure=new Error(controller.signal.aborted?'请求已超时或取消，请检查连接后重试':'连接中断，未能确认请求结果。请检查网络后重试')
    failure.retryable=true
    failure.cause=error
    throw failure
  } finally {clearTimeout(timer);signal?.removeEventListener('abort',abort)}
}
export function saveNativeBlob(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
