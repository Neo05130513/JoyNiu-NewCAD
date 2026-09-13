"""Public resource reads and authenticated, explicit sharing operations."""
import json
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .cad_community import CadCommunity
from .cad_feature_workspace import CadFeatureWorkspace, FeatureNotFound, FeatureWorkspaceError
from .cad_plan import PlanValidationError
from .platform import Permission, PlatformError, ValidationError
from .platform_api import _token_user, _domain_http_exception
from .download_headers import attachment_content_disposition


def create_cad_community_router(services, *, root=None, features=None):
    base = Path(services.auth.database).resolve().parent
    features = features or CadFeatureWorkspace(base / "cad-feature-workspace")
    store = CadCommunity(root or base / "cad-community", features)
    router = APIRouter(prefix="/api/cad/community", tags=["cad-community"])
    router.community_store = store
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}

    def actor(token, write=False, optional=False):
        if optional and not token: return None
        user = _token_user(services, token)
        services.auth.require(user, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        return user.id

    async def call(fn):
        try: return await run_in_threadpool(fn)
        except PlatformError as error: raise _domain_http_exception(error) from None
        except FeatureNotFound as error: raise HTTPException(404, str(error), headers=headers) from None
        except (FeatureWorkspaceError, PlanValidationError) as error: raise HTTPException(422, str(error), headers=headers) from None

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16384: raise HTTPException(413, "社区请求不能超过 16 KB。", headers=headers)
        try: value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError): raise HTTPException(422, "请求 JSON 格式无效。", headers=headers) from None
        if not isinstance(value, dict): raise HTTPException(422, "请求须为对象。", headers=headers)
        return value

    @router.get("")
    async def listing(q: str = "", mine: bool = False, authorization: str | None = Header(None)):
        return JSONResponse(await call(lambda: store.list(actor(authorization, optional=not mine), q, mine)), headers=headers)

    @router.post("")
    async def publish(request: Request, authorization: str | None = Header(None)):
        data = await body(request)
        return JSONResponse(await call(lambda: store.publish(actor(authorization, write=True), data)), status_code=201, headers=headers)

    @router.get("/{resource_id}")
    async def resource(resource_id: str, authorization: str | None = Header(None)):
        return JSONResponse(await call(lambda: store.get(resource_id, actor(authorization, optional=True))), headers=headers)

    @router.get("/{resource_id}/artifacts/{kind}")
    async def artifact(resource_id: str, kind: str):
        payload, mime = await call(lambda: store.artifact(resource_id, kind))
        return Response(payload, media_type=mime, headers={**headers, "Content-Disposition": attachment_content_disposition(f"{resource_id}.{kind}")})

    @router.post("/{resource_id}/open")
    async def open_resource(resource_id: str, request: Request, authorization: str | None = Header(None)):
        data = await body(request)
        return JSONResponse(await call(lambda: store.open(actor(authorization, write=True), resource_id, data)), status_code=201, headers=headers)

    @router.post("/{resource_id}/withdraw")
    async def withdraw(resource_id: str, request: Request, authorization: str | None = Header(None)):
        data = await body(request)
        def execute():
            owner = actor(authorization, write=True)
            if data: raise ValidationError("撤下不接受额外字段。")
            return store.withdraw(owner, resource_id)
        return JSONResponse(await call(execute), headers=headers)

    return router
