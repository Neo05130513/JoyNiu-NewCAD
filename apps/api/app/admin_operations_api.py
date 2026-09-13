"""RBAC operations API; responses deliberately contain no customer artifacts."""
from contextlib import asynccontextmanager
import json
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from .platform import PlatformError
from .platform_api import _domain_http_exception, _token_user
from .admin_operations import AdminOperationsService


def create_admin_operations_router(services, *, cad_store, billing=None, billing_policy=None, database=None, service=None):
    operations = service or AdminOperationsService(services,cad_store=cad_store,billing=billing,billing_policy=billing_policy,database=database)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if service is None:
                operations.close()

    def no_cache(response: Response):
        response.headers['Cache-Control'] = 'no-store'

    router = APIRouter(prefix='/admin',tags=['admin-operations'],dependencies=[Depends(no_cache)],lifespan=lifespan)
    router.admin_operations_service = operations

    def identity(authorization: str | None = Header(default=None)):
        try:
            return _token_user(services,authorization)
        except HTTPException as failure:
            failure.headers = {**(failure.headers or {}),'Cache-Control':'no-store'}
            raise

    def invoke(function,*args,**kwargs):
        try:
            return function(*args,**kwargs)
        except PlatformError as error:
            failure = _domain_http_exception(error)
            failure.headers = {**(failure.headers or {}),'Cache-Control':'no-store'}
            raise failure from None

    async def body(request, maximum=32768):
        headers = {'Cache-Control': 'no-store'}
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise HTTPException(415, '请使用 JSON 提交', headers=headers)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > maximum:
                raise HTTPException(413, '请求超过大小限制', headers=headers)
            content.extend(chunk)
        try:
            def invalid(_value):
                raise ValueError()
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            values = json.loads(content.decode('utf-8'), parse_constant=invalid, object_pairs_hook=unique)
            if not isinstance(values, dict):
                raise ValueError()
        except (UnicodeError, ValueError, RecursionError):
            raise HTTPException(422, '请求须为有效且无重复字段的 JSON 对象', headers=headers) from None
        return values

    @router.get('/overview')
    def overview(days: int = Query(30), actor=Depends(identity)):
        return invoke(operations.overview,actor.id,days=days)

    @router.get('/customers')
    def customers(q: str = Query('',max_length=128), active: bool | None = None, limit: int = Query(20,ge=1,le=100), offset: int = Query(0,ge=0,le=1_000_000), actor=Depends(identity)):
        return invoke(operations.customers,actor.id,q=q,active=active,limit=limit,offset=offset)

    @router.get('/customers/{owner_id}')
    def customer(owner_id: str,actor=Depends(identity)):
        return invoke(operations.customer,actor.id,owner_id)

    @router.put('/customers/{owner_id}/crm')
    async def update_crm(owner_id: str, request: Request, actor=Depends(identity)):
        invoke(operations.actor, actor.id, 'admin:customer-manage')
        return invoke(operations.update_crm, actor.id, owner_id, await body(request))

    @router.get('/customers/{owner_id}/followups')
    def followups(owner_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
        return invoke(operations.notes, actor.id, 'customer', owner_id, limit=limit, offset=offset)

    @router.post('/customers/{owner_id}/followups')
    async def add_followup(owner_id: str, request: Request, actor=Depends(identity)):
        invoke(operations.actor, actor.id, 'admin:customer-manage')
        return invoke(operations.add_note, actor.id, 'customer', owner_id, await body(request))

    @router.get('/tasks')
    def tasks(q: str = Query('',max_length=128),status: str | None = None,ownerId: str | None = Query(None,max_length=160),handlingStatus: str | None = None,priority: str | None = None,limit: int = Query(20,ge=1,le=100),offset: int = Query(0,ge=0,le=1_000_000),actor=Depends(identity)):
        return invoke(operations.tasks,actor.id,q=q,status=status,owner=ownerId,handling_status=handlingStatus,priority=priority,limit=limit,offset=offset)

    @router.get('/tasks/{run_id}')
    def task(run_id: str,actor=Depends(identity)):
        return invoke(operations.task,actor.id,run_id)

    @router.put('/tasks/{run_id}/handling')
    async def update_handling(run_id: str, request: Request, actor=Depends(identity)):
        invoke(operations.actor, actor.id, 'admin:task-followup')
        return invoke(operations.update_handling, actor.id, run_id, await body(request))

    @router.get('/tasks/{run_id}/notes')
    def notes(run_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(identity)):
        return invoke(operations.notes, actor.id, 'task', run_id, limit=limit, offset=offset)

    @router.post('/tasks/{run_id}/notes')
    async def add_note(run_id: str, request: Request, actor=Depends(identity)):
        invoke(operations.actor, actor.id, 'admin:task-followup')
        return invoke(operations.add_note, actor.id, 'task', run_id, await body(request))

    @router.post('/tasks/{run_id}/cancel')
    async def cancel(run_id: str,request: Request,actor=Depends(identity)):
        # Require capability before spending resources parsing a body.
        invoke(operations.actor,actor.id,'admin:task-manage')
        headers = {'Cache-Control':'no-store'}
        if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
            raise HTTPException(415,'请使用 JSON 提交停止请求',headers=headers)
        content = bytearray()
        async for chunk in request.stream():
            if len(content)+len(chunk)>8192:
                raise HTTPException(413,'停止请求超过大小限制',headers=headers)
            content.extend(chunk)
        try:
            def invalid(_value):
                raise ValueError()
            values = json.loads(content.decode('utf-8'),parse_constant=invalid)
            if not isinstance(values,dict):
                raise ValueError()
        except (UnicodeError,ValueError,RecursionError):
            raise HTTPException(422,'停止请求须为有效 JSON 对象',headers=headers) from None
        return invoke(operations.cancel,actor.id,run_id,values)

    @router.get('/system')
    def system(actor=Depends(identity)):
        return invoke(operations.system,actor.id)

    @router.get('/audit')
    def audit(source: str | None = None,actorId: str | None = Query(None,max_length=128),limit: int = Query(20,ge=1,le=100),offset: int = Query(0,ge=0,le=1_000_000),actor=Depends(identity)):
        return invoke(operations.audit,actor.id,source=source,actor_filter=actorId,limit=limit,offset=offset)

    return router
