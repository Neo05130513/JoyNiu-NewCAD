"""Authenticated manual feature design routes; never updates AI run state."""
from __future__ import annotations

import json
import os
import asyncio
import contextlib
import threading
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError, FeatureNotFound, FeatureConflict, public_feature_record
from .cad_plan import PlanValidationError
from .cad_design_workspace import DesignError
from .platform import Permission, PlatformError
from .platform_api import _domain_http_exception, _token_user


def create_cad_feature_workspace_router(services, source_store=None, root=None, design_store=None):
    base = Path(services.auth.database).resolve().parent if str(services.auth.database) != ":memory:" else Path("data").resolve()
    if design_store is None:
        from .cad_design_workspace import CadDesignWorkspace
        design_store = CadDesignWorkspace(os.environ.get("JOYNIU_CAD_DESIGNS_ROOT", str(base / "cad-designs")))
    store = CadFeatureWorkspace(root or base / "cad-feature-workspace", source_store=source_store, design_store=design_store)
    router = APIRouter(prefix="/api/cad/features", tags=["cad-feature-workspace"])
    router.feature_store = store
    headers = {"Cache-Control": "private, no-store"}

    def actor_for(authorization, *, write=False):
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None
        return actor

    async def body(request):
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 300_000:
                raise HTTPException(413, "请求不能超过 300 KB。", headers=headers)
        try:
            value = json.loads(chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(422, "请求 JSON 格式无效。", headers=headers) from None
        if not isinstance(value, dict):
            raise HTTPException(422, "请求须为对象。", headers=headers)
        return value

    async def call(fn, *args):
        try:
            return await run_in_threadpool(fn, *args)
        except FeatureNotFound as exc:
            raise HTTPException(404, str(exc), headers=headers) from None
        except FeatureConflict as exc:
            raise HTTPException(409, str(exc), headers=headers) from None
        except (FeatureWorkspaceError, PlanValidationError, DesignError) as exc:
            code = getattr(exc, "code", None)
            raise HTTPException(409 if code in {"stale_topology", "unresolved_topology_binding"} else 422, {"message": str(exc), "featureId": getattr(exc, "feature_id", None), "code": code}, headers=headers) from None

    async def cancellable(request, fn, *args):
        cancel = threading.Event()
        async def watch():
            while not cancel.is_set():
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.1)
        watcher = asyncio.create_task(watch())
        try:
            return await call(fn, *args, cancel)
        finally:
            cancel.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError): await watcher

    @router.get("")
    async def list_designs(authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse({"items": await call(store.list, actor.id)}, headers=headers)

    @router.post("")
    async def create_design(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        return JSONResponse(public_feature_record(await call(store.save, actor.id, await body(request))), status_code=201, headers=headers)

    @router.post("/preview")
    async def preview(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        data = await body(request)
        return JSONResponse(await cancellable(request, store.preview, actor.id, data), headers=headers)

    @router.post("/commit")
    async def commit(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        data = await body(request)
        return JSONResponse(public_feature_record(await cancellable(request, store.commit, actor.id, data)), headers=headers)

    @router.get("/previews/{preview_id}/artifacts/{format}")
    async def preview_artifact(preview_id: str, format: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        path, mime = await call(store.preview_artifact, actor.id, preview_id, format)
        return FileResponse(path, media_type=mime, filename=path.name, headers=headers)

    @router.get("/{design_id}")
    async def get_design(design_id: str, revision: int | None = None, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse(public_feature_record(await call(store.get, actor.id, design_id, revision)), headers=headers)

    @router.put("/{design_id}")
    async def save_design(design_id: str, request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        return JSONResponse(public_feature_record(await call(store.save, actor.id, await body(request), design_id)), headers=headers)

    @router.get("/{design_id}/versions")
    async def versions(design_id: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse({"items": await call(store.versions, actor.id, design_id)}, headers=headers)

    @router.post("/{design_id}/build")
    async def build(design_id: str, request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        data = await body(request)
        return JSONResponse(public_feature_record(await call(store.build, actor.id, design_id, data.get("expectedRevision"))), headers=headers)

    @router.get("/{design_id}/versions/{revision}/artifacts/{format}")
    async def artifact(design_id: str, revision: int, format: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        path, mime = await call(store.artifact, actor.id, design_id, revision, format)
        return FileResponse(path, media_type=mime, filename=path.name, headers=headers)

    return router
