"""Project-scoped PDM transfer and reopening for the CAD editor."""
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .cad_editor_pdm import CadEditorPdm
from .cad_feature_workspace import CadFeatureWorkspace, FeatureNotFound, FeatureWorkspaceError
from .cad_design_workspace import DesignError
from .cad_plan import PlanValidationError
from .platform import Permission, PlatformError, ValidationError
from .platform_api import _token_user, _domain_http_exception, _require_project_access, _require_version_access, _can_access_project


def create_cad_editor_pdm_router(services, designs, *, features=None):
    features = features or CadFeatureWorkspace(Path(services.auth.database).resolve().parent / "cad-feature-workspace", design_store=designs)
    store = CadEditorPdm(services.pdm, features, designs)
    router = APIRouter(prefix="/pdm")
    router.editor_pdm = store
    headers = {"Cache-Control": "private, no-store"}

    def actor_for(token, *, write=False):
        actor = _token_user(services, token)
        services.auth.require(actor, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        return actor

    async def call(fn, *args):
        try: return await run_in_threadpool(fn, *args)
        except PlatformError as error: raise _domain_http_exception(error) from None
        except FeatureNotFound as error: raise HTTPException(404, str(error), headers=headers) from None
        except (FeatureWorkspaceError, DesignError, PlanValidationError) as error: raise HTTPException(422, str(error), headers=headers) from None

    async def body(request):
        import json
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192: raise HTTPException(413, "PDM 请求不能超过 8 KB。")
        try: value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError): raise HTTPException(422, "请求 JSON 格式无效。") from None
        if not isinstance(value, dict): raise HTTPException(422, "请求须为对象。")
        return value

    def projects(token):
        actor = actor_for(token)
        services.auth.require(actor, Permission.PROJECT_READ)
        return {"items": [{"id": project.id, "name": project.name,
                           "canWrite": services.auth.has_permission(actor, Permission.DOCUMENT_WRITE) and _can_access_project(services, actor, project, write=True)}
                          for project in services.pdm.list_projects() if _can_access_project(services, actor, project)]}

    @router.get("/projects")
    async def list_projects(authorization: str | None = Header(None)):
        return JSONResponse(await call(projects, authorization), headers=headers)

    @router.post("/projects")
    async def create_project(request: Request, authorization: str | None = Header(None)):
        data = await body(request)
        def create():
            actor = actor_for(authorization, write=True)
            services.auth.require(actor, Permission.PROJECT_WRITE)
            if set(data) != {"name"}: raise ValidationError("新项目只接受名称。")
            return services.pdm.create_project(data["name"], actor.id).to_dict()
        return JSONResponse(await call(create), status_code=201, headers=headers)

    @router.get("/projects/{project_id}/documents")
    async def documents(project_id: str, authorization: str | None = Header(None)):
        def read():
            actor = actor_for(authorization)
            _require_project_access(services, actor, project_id)
            return {"items": store.documents(project_id)}
        return JSONResponse(await call(read), headers=headers)

    @router.post("/versions")
    async def save(request: Request, authorization: str | None = Header(None)):
        data = await body(request)
        def write():
            actor = actor_for(authorization, write=True)
            _require_project_access(services, actor, data.get("projectId"), write=True)
            return store.save(actor.id, data)
        return JSONResponse(await call(write), status_code=201, headers=headers)

    @router.get("/versions/{version_id}")
    async def version(version_id: str, authorization: str | None = Header(None)):
        def read():
            actor = actor_for(authorization)
            _require_version_access(services, actor, version_id)
            return store._saved(version_id)
        return JSONResponse(await call(read), headers=headers)

    @router.get("/versions/{version_id}/artifacts/{kind}")
    async def artifact(version_id: str, kind: str, authorization: str | None = Header(None)):
        def read():
            actor = actor_for(authorization)
            _require_version_access(services, actor, version_id)
            if kind not in {"step", "glb", "plan"}: raise ValidationError("不支持的 CAD 文件格式。")
            _, parts = store.bundle(version_id)
            return parts[{"step": "model.step", "glb": "model.glb", "plan": "plan.json"}[kind]]
        return Response(await call(read), media_type={"step": "application/step", "glb": "model/gltf-binary", "plan": "application/json"}.get(kind), headers=headers)

    @router.post("/versions/{version_id}/open")
    async def open_version(version_id: str, request: Request, authorization: str | None = Header(None)):
        data = await body(request)
        def write():
            actor = actor_for(authorization, write=True)
            _require_version_access(services, actor, version_id)
            return store.open(actor.id, version_id, data)
        return JSONResponse(await call(write), status_code=201, headers=headers)

    return router
