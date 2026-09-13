"""Support routes intentionally provide no CAD artifact or source download access."""
from contextlib import asynccontextmanager
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from .platform import PlatformError
from .platform_api import _domain_http_exception, _token_user
from .support import SupportService


def create_support_router(services, database=None, *, cad_store=None, service=None, max_body_bytes=64 * 1024):
    support = service or SupportService(services.auth, database, cad_store=cad_store)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if service is None:
                support.close()

    def no_cache(response: Response):
        response.headers["Cache-Control"] = "no-store"

    router = APIRouter(prefix="/support", tags=["support"], dependencies=[Depends(no_cache)], lifespan=lifespan)
    router.support_service = support

    def identity(authorization: str | None = Header(default=None)):
        return _token_user(services, authorization)

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformError as error:
            failure = _domain_http_exception(error)
            failure.headers = {**(failure.headers or {}), "Cache-Control": "no-store"}
            raise failure from None

    async def body(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "请使用 JSON 提交工单")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > max_body_bytes:
                raise HTTPException(413, "工单内容超过大小限制")
            content.extend(chunk)
        try:
            def invalid(_value):
                raise ValueError()
            result = json.loads(content.decode("utf-8"), parse_constant=invalid)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (UnicodeError, ValueError, RecursionError):
            raise HTTPException(422, "工单内容须为有效 JSON 对象") from None

    @router.get("/tickets")
    def list_own(status: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
        return invoke(support.list_tickets, actor.id, status=status, limit=limit, offset=offset)

    @router.post("/tickets", status_code=201)
    async def create(request: Request, actor=Depends(identity)):
        return invoke(support.create, actor.id, await body(request))

    # Explicit routes keep an administrator's personal tickets distinct from
    # the support queue. The service repeats role/ownership checks on every call.
    def register_details(prefix, admin):
        @router.get(prefix)
        def detail(ticket_id: str, actor=Depends(identity)):
            return invoke(support.ticket, actor.id, ticket_id, admin=admin)

        @router.get(prefix + "/messages")
        def messages(ticket_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
            return invoke(support.messages, actor.id, ticket_id, admin=admin, limit=limit, offset=offset)

        @router.post(prefix + "/messages", status_code=201)
        async def append(ticket_id: str, request: Request, actor=Depends(identity)):
            return invoke(support.append, actor.id, ticket_id, await body(request), admin=admin)

        @router.patch(prefix)
        async def update(ticket_id: str, request: Request, actor=Depends(identity)):
            return invoke(support.update, actor.id, ticket_id, await body(request), admin=admin)

    register_details("/tickets/{ticket_id}", False)
    register_details("/admin/tickets/{ticket_id}", True)

    @router.get("/admin/tickets")
    def list_admin(status: str | None = None, q: str = Query('', max_length=128), assignedTo: str | None = Query(None, max_length=128), priority: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
        return invoke(support.list_tickets, actor.id, admin=True, status=status, q=q, assigned_to=assignedTo, priority=priority, limit=limit, offset=offset)

    @router.get('/admin/assignees')
    def assignees(actor=Depends(identity)):
        return invoke(support.assignees, actor.id)

    @router.get('/admin/tickets/{ticket_id}/notes')
    def notes(ticket_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
        return invoke(support.notes, actor.id, ticket_id, limit=limit, offset=offset)

    @router.post('/admin/tickets/{ticket_id}/data-receipts', status_code=201)
    async def data_receipt(ticket_id: str, request: Request, actor=Depends(identity)):
        return invoke(support.record_data_receipt, actor.id, ticket_id, await body(request))

    @router.post('/admin/tickets/{ticket_id}/notes', status_code=201)
    async def append_note(ticket_id: str, request: Request, actor=Depends(identity)):
        return invoke(support.append_note, actor.id, ticket_id, await body(request))

    @router.get("/admin/tickets/{ticket_id}/audit")
    def audit(ticket_id: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
        return invoke(support.audit, actor.id, ticket_id, limit=limit, offset=offset)

    return router
