"""Authenticated HTTP routes for independent engineering STEP documents."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .cad_design_workspace import CadDesignWorkspace, DesignError, DesignNotFound, MAX_STEP_BYTES
from .cad_design_assembly import CadAssemblyJobs, AssemblyAdmissionError
from .platform import Permission, PlatformError
from .platform_api import _token_user, _domain_http_exception


def create_cad_design_workspace_router(services, root=None, *, billing=None, billing_policy=None, cad_store=None):
    if root is None:
        database = str(services.auth.database)
        base = Path(database).resolve().parent if database != ":memory:" else Path("data").resolve()
        root = os.environ.get("JOYNIU_CAD_DESIGNS_ROOT", str(base / "cad-designs"))
    store = CadDesignWorkspace(root)
    jobs = CadAssemblyJobs(store, billing=billing, billing_policy=billing_policy, cad_store=cad_store)
    router = APIRouter(prefix="/api/cad/designs", tags=["cad-design-workspace"])
    router.design_store = store
    router.assembly_jobs = jobs
    from .cad_import_assets_api import create_cad_import_router
    feature_base = Path(services.auth.database).resolve().parent if str(services.auth.database) != ":memory:" else Path("data").resolve()
    router.include_router(create_cad_import_router(services, feature_base / "cad-feature-workspace" / "imports"))
    if hasattr(services, "pdm"):
        from .cad_editor_pdm_api import create_cad_editor_pdm_router
        router.include_router(create_cad_editor_pdm_router(services, store))
    headers = {"Cache-Control": "private, no-store"}

    def actor_for(authorization, *, write=False):
        actor = _token_user(services, authorization)
        try: services.auth.require(actor, Permission.DOCUMENT_WRITE if write else Permission.DOCUMENT_READ)
        except PlatformError as exc: raise _domain_http_exception(exc) from None
        return actor

    async def execute(fn, *args):
        try:
            result = await run_in_threadpool(fn, *args)
        except DesignNotFound as exc:
            raise HTTPException(404, str(exc), headers=headers) from None
        except AssemblyAdmissionError as exc:
            raise HTTPException(409, str(exc), headers=headers) from None
        except DesignError as exc:
            raise HTTPException(422, str(exc), headers=headers) from None
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None
        return result

    async def body(request):
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 256_000:
                raise HTTPException(413, "请求不能超过 256 KB。")
        try:
            result = json.loads(chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise HTTPException(422, "请求 JSON 格式无效。") from None
        if not isinstance(result, dict):
            raise HTTPException(422, "请求须为 JSON 对象。")
        return result

    @router.get("")
    async def list_designs(fileId: str | None = None, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse({"items": store.list(actor.id, fileId)}, headers=headers)

    @router.post("/step")
    async def import_step(file: UploadFile = File(...), fileId: str = Form(...), name: str | None = Form(None),
                          authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        payload = await file.read(MAX_STEP_BYTES + 1)
        if len(payload) > MAX_STEP_BYTES:
            raise HTTPException(413, "STEP 文件不能超过 20 MB。")
        result = await execute(store.import_step, actor.id, file.filename, payload, fileId, name)
        return JSONResponse(result, status_code=201, headers=headers)

    @router.post("/assemblies")
    async def assemble(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        return JSONResponse(await execute(store.assemble, actor.id, await body(request)), status_code=201, headers=headers)

    @router.get("/assembly-jobs")
    async def list_assembly_jobs(fileId: str | None = None, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse({"items": await execute(jobs.list, actor.id, fileId)}, headers=headers)

    @router.post("/ai-assemblies")
    async def ai_assembly(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        return JSONResponse(await execute(jobs.submit, actor.id, await body(request)), status_code=202, headers=headers)

    @router.post("/assembly-plans")
    async def compile_assembly_plan(request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        data = await body(request)
        return JSONResponse(await execute(lambda: jobs.submit(actor.id, data, from_ai=False)), status_code=202, headers=headers)

    @router.get("/assembly-jobs/{job_id}")
    async def get_assembly_job(job_id: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse(await execute(jobs.get, actor.id, job_id), headers=headers)

    @router.get("/{design_id}")
    async def get_design(design_id: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse(await execute(store.get, actor.id, design_id), headers=headers)

    @router.post("/{design_id}/measure")
    async def measure(design_id: str, request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        return JSONResponse(await execute(store.measure, actor.id, design_id, await body(request)), headers=headers)

    @router.post("/{design_id}/drawing")
    async def drawing(design_id: str, request: Request, authorization: str | None = Header(None)):
        actor = actor_for(authorization, write=True)
        return JSONResponse(await execute(store.drawing, actor.id, design_id, await body(request)), headers=headers)

    @router.get("/{design_id}/artifacts/{key}")
    async def artifact(design_id: str, key: str, authorization: str | None = Header(None)):
        actor = actor_for(authorization)
        path, item = await execute(store.artifact, actor.id, design_id, key)
        return FileResponse(path, filename=item["filename"], media_type=item["mimeType"], headers=headers)

    return router
