"""Authenticated, bounded HTTP boundary for native drawing documents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from fastapi import Request
except ImportError:  # pragma: no cover
    Request = Any

from .download_headers import attachment_content_disposition
from .dwg_preprocessor import DWGPreprocessError
from .native_drawing import DrawingError, MAX_UPLOAD, NativeDrawingStore, capabilities
from .platform import Permission, PlatformError
from .platform_api import _domain_http_exception, _token_user


def create_native_drawing_router(services, database=None):
    """Mount directly on the app; routes already include /api/cad/drawings."""
    from fastapi import APIRouter, Header, HTTPException
    from fastapi.responses import JSONResponse, Response
    from starlette.concurrency import run_in_threadpool

    store = NativeDrawingStore(database or services.auth.database)
    router = APIRouter(prefix='/api/cad/drawings', tags=['native-drawing'])
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}

    def actor_for(authorization, *, write=False):
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        except PlatformError as exc:
            raise _domain_http_exception(exc) from exc
        return actor

    async def body(request, maximum):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > maximum:
                raise HTTPException(413, '请求超过大小上限。', headers=headers)
        return bytes(data)

    async def payload(request):
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(415, '请使用 application/json。', headers=headers)
        raw = await body(request, 2 * 1024 * 1024)
        try:
            def invalid(_value):
                raise ValueError()
            value = json.loads(raw, parse_constant=invalid)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(422, '请求必须为有效 JSON 对象。', headers=headers) from None

    async def call(function, *args, **kwargs):
        try:
            return await run_in_threadpool(function, *args, **kwargs)
        except DrawingError as exc:
            detail = {'code': exc.code, 'message': str(exc)}
            if hasattr(exc, 'revision'):
                detail['revision'] = exc.revision
            raise HTTPException(exc.status, detail, headers=headers) from exc
        except DWGPreprocessError as exc:
            raise HTTPException(422, {'code': exc.code, 'message': 'DWG 转换不可用或文件转换失败；请提供 DXF，原文件未修改。'}, headers=headers) from exc

    @router.get('/capabilities')
    async def status(authorization: str | None = Header(default=None)):
        actor = actor_for(authorization)
        result = await call(capabilities)
        result['canWrite'] = services.auth.has_permission(actor, Permission.DOCUMENT_WRITE)
        return JSONResponse(result, headers=headers)

    @router.get('')
    async def listing(authorization: str | None = Header(default=None)):
        actor = actor_for(authorization)
        return JSONResponse(await call(store.list, actor.id), headers=headers)

    @router.post('')
    async def create(request: Request, authorization: str | None = Header(default=None)):
        actor = actor_for(authorization, write=True)
        value = await payload(request)
        return JSONResponse(await call(store.create, actor.id, value.get('name', '未命名图纸'), request_id=value.get('requestId')), status_code=201, headers=headers)

    @router.post('/import')
    async def upload(request: Request, authorization: str | None = Header(default=None)):
        actor = actor_for(authorization, write=True)
        if not request.headers.get('content-type', '').lower().startswith('multipart/form-data;'):
            raise HTTPException(415, '请使用 multipart/form-data 上传 file。', headers=headers)
        # Bound the complete body before the multipart parser touches file data.
        raw = await body(request, MAX_UPLOAD + 65536)
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            sent = True
            return {'type': 'http.request', 'body': raw, 'more_body': False}
        limited = Request(request.scope, receive=receive)
        try:
            async with limited.form(max_files=1, max_fields=2) as form:
                file = form.get('file')
                if file is None or not hasattr(file, 'read') or not getattr(file, 'filename', None):
                    raise HTTPException(422, '请选择一个 file 上传文件。', headers=headers)
                data = await file.read(MAX_UPLOAD + 1)
                if len(data) > MAX_UPLOAD:
                    raise HTTPException(413, '上传文件超过 20 MiB。', headers=headers)
                name = form.get('name') or Path(file.filename).stem
                result = await call(store.create, actor.id, name, data, file.filename, request_id=form.get('requestId'))
        except HTTPException:
            raise
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, '上传表单无效。', headers=headers) from exc
        return JSONResponse(result, status_code=201, headers=headers)

    @router.get('/{identity}')
    async def document(identity: str, authorization: str | None = Header(default=None)):
        actor = actor_for(authorization)
        return JSONResponse(await call(store.get, actor.id, identity), headers=headers)

    @router.post('/{identity}/operations')
    async def edit(identity: str, request: Request, authorization: str | None = Header(default=None)):
        actor = actor_for(authorization, write=True)
        value = await payload(request)
        result = await call(store.operate, actor.id, identity, value.get('expectedRevision'), value.get('operations'), request_id=value.get('requestId'))
        return JSONResponse(result, headers=headers)

    @router.post('/{identity}/undo')
    async def undo(identity: str, request: Request, authorization: str | None = Header(default=None)):
        actor = actor_for(authorization, write=True)
        value = await payload(request)
        return JSONResponse(await call(store.operate, actor.id, identity, value.get('expectedRevision'), history='undo', request_id=value.get('requestId')), headers=headers)

    @router.post('/{identity}/redo')
    async def redo(identity: str, request: Request, authorization: str | None = Header(default=None)):
        actor = actor_for(authorization, write=True)
        value = await payload(request)
        return JSONResponse(await call(store.operate, actor.id, identity, value.get('expectedRevision'), history='redo', request_id=value.get('requestId')), headers=headers)

    @router.get('/{identity}/export')
    async def export(identity: str, format: str = 'dxf', paper: str = 'A4', scale: str = 'fit', color: str = 'original', landscape: bool = False, layout: str = 'Model',
                     authorization: str | None = Header(default=None)):
        actor = actor_for(authorization)
        data, content_type, filename = await call(store.export, actor.id, identity, format, paper=paper, scale=scale, color=color, landscape=landscape, layout=layout)
        return Response(data, media_type=content_type, headers={**headers, 'Content-Disposition': attachment_content_disposition(filename)})

    @router.get('/{identity}/preview.svg')
    async def preview(identity: str, layout: str = 'Model', authorization: str | None = Header(default=None)):
        actor = actor_for(authorization)
        data, content_type, _filename = await call(store.export, actor.id, identity, 'svg', layout=layout)
        return Response(data, media_type=content_type, headers={**headers, 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    return router
