"""Multipart import into immutable owner-scoped CAD body assets."""
import asyncio
import contextlib
import threading
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .cad_import_assets import CadImportAssets, MAX_IMPORT_BYTES
from .cad_feature_workspace import FeatureNotFound
from .cad_plan import PlanValidationError
from .platform import Permission, PlatformError
from .platform_api import _token_user, _domain_http_exception


def create_cad_import_router(services, root):
    store=CadImportAssets(root);router=APIRouter(prefix="/imports")
    router.import_assets=store
    def actor_for(authorization,write=False):
        actor=_token_user(services,authorization)
        try: services.auth.require(actor, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        except PlatformError as error: raise _domain_http_exception(error) from None
        return actor
    headers={"Cache-Control":"private, no-store"}
    @router.post("")
    async def upload(request:Request,file:UploadFile=File(...),name:str=Form(...),units:str=Form("mm"),requestId:str=Form(...),authorization:str|None=Header(None)):
        actor=actor_for(authorization,True)
        payload=await file.read(MAX_IMPORT_BYTES+1)
        if len(payload)>MAX_IMPORT_BYTES: raise HTTPException(413,"导入文件不能超过 20 MB。")
        cancel=threading.Event()
        async def watch():
            while not cancel.is_set():
                if await request.is_disconnected(): cancel.set();return
                await asyncio.sleep(.1)
        watcher=asyncio.create_task(watch())
        try:
            value=await run_in_threadpool(store.upload,actor.id,file.filename,payload,name,units,requestId,cancel)
            return JSONResponse(value,status_code=201,headers=headers)
        except FeatureNotFound as error: raise HTTPException(404,str(error),headers=headers) from None
        except PlanValidationError as error: raise HTTPException(409 if error.code=="import_request_conflict" else 422,{"message":str(error),"code":error.code},headers=headers) from None
        finally:
            cancel.set();watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError): await watcher
    @router.get("/{asset_id}")
    async def get(asset_id:str,authorization:str|None=Header(None)):
        actor=actor_for(authorization)
        try:
            value=await run_in_threadpool(store.get,actor.id,asset_id)
            await run_in_threadpool(store.path,actor.id,asset_id,value["sha256"])
            return JSONResponse(store.public(value),headers=headers)
        except FeatureNotFound as error: raise HTTPException(404,str(error),headers=headers) from None
        except PlanValidationError as error: raise HTTPException(422,str(error),headers=headers) from None
    return router
