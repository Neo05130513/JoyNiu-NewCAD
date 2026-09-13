"""Authenticated boundary for immutable CAD delivery packages."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .cad_design_workspace import CadDesignWorkspace
from .cad_feature_workspace import CadFeatureWorkspace
from .native_drawing import NativeDrawingStore
from .delivery_workspace import DeliveryWorkspace, DeliveryError
from .platform import Permission, PlatformError
from .platform_api import _domain_http_exception, _token_user


def create_delivery_workspace_router(services, cad_store=None, root=None, *, engineering_store=None, feature_store=None, native_store=None):
    base = Path(services.auth.database).resolve().parent if str(services.auth.database) != ":memory:" else Path("data").resolve()
    engineering_store = engineering_store or CadDesignWorkspace(os.environ.get("JOYNIU_CAD_DESIGNS_ROOT", str(base / "cad-designs")))
    feature_store = feature_store or CadFeatureWorkspace(base / "cad-feature-workspace", source_store=cad_store, design_store=engineering_store)
    native_store = native_store or NativeDrawingStore(services.auth.database if str(services.auth.database) != ":memory:" else base / "delivery-native.sqlite3")
    store = DeliveryWorkspace(root or base / "cad-deliveries", engineering_store=engineering_store,
                              feature_store=feature_store, native_store=native_store, cad_store=cad_store)
    router = APIRouter(prefix="/api/cad/deliveries", tags=["cad-deliveries"])
    router.delivery_store = store
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}

    def actor_for(authorization, *, write=False):
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None
        return actor

    async def call(function, *args):
        try:
            return await run_in_threadpool(function, *args)
        except DeliveryError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}, headers=headers) from None

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 64_000:
                raise HTTPException(413, {"code": "delivery_size", "message": "交付请求不能超过 64 KB。"}, headers=headers)
        try:
            return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(422, {"code": "invalid_delivery", "message": "请求须为有效 JSON 对象。"}, headers=headers) from None

    @router.get("/sources")
    async def sources(authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse({"items": await call(store.sources, actor.id)}, headers=headers)

    @router.get("")
    async def listing(authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse({"items": await call(store.list, actor.id)}, headers=headers)

    @router.post("")
    async def create(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        return JSONResponse(await call(store.create, actor.id, await body(request)), status_code=201, headers=headers)

    @router.get("/{delivery_id}")
    async def detail(delivery_id: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse(await call(store.get, actor.id, delivery_id), headers=headers)

    @router.get("/{delivery_id}/archive")
    async def archive(delivery_id: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        path, item = await call(store.archive, actor.id, delivery_id)
        return FileResponse(path, filename=item["name"], media_type=item["mimeType"], headers=headers)

    @router.get("/{delivery_id}/files/{file_id}")
    async def file(delivery_id: str, file_id: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        path, item = await call(store.file, actor.id, delivery_id, file_id)
        return FileResponse(path, filename=item["name"], media_type=item["mimeType"], headers=headers)

    return router
