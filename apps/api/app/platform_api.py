"""Thin FastAPI adapter for the platform services.

Importing this module does not require FastAPI.  Call
``create_platform_router`` from an application that has the optional API
dependencies installed, then mount the returned router alongside the geometry
router::

    services = build_platform_services(database="joyniu.sqlite3")
    app.include_router(create_platform_router(services), prefix="/api/v1")

All domain invariants remain in ``platform.py``, ``ocr.py`` and ``cam.py``;
this file only handles authentication headers, JSON coercion and HTTP status
codes.
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .cam import (
    CAMConflictError,
    CAMGateRejected,
    CAMNotFoundError,
    CAMService,
    StockDefinition,
)
from .ocr import DrawingRecognition, OCRService
from .platform import (
    AccessToken,
    AuthService,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PDMRepository,
    Permission,
    PlatformError,
    Role,
    ValidationError,
)


@dataclass(slots=True)
class PlatformServices:
    """Dependency container shared by HTTP handlers and background workers."""

    pdm: PDMRepository
    auth: AuthService
    ocr: OCRService
    cam: CAMService
    recognitions: dict[str, DrawingRecognition] = field(default_factory=dict)


def build_platform_services(
    database: str | Path = ":memory:",
    *,
    auth_secret: str | bytes | None = None,
    token_ttl_seconds: int = 3600,
    enable_live_ocr: bool | None = None,
) -> PlatformServices:
    """Build a local service graph.

    ``database`` is shared by PDM and accounts when it is a filesystem path.
    For tests, pass ``":memory:"`` (the default).  A CAM plan and OCR result
    are kept in memory until a caller snapshots them to PDM, which is deliberate
    so simulation artifacts cannot be silently edited in place.
    """

    db_value = str(database)
    # Keep a single AuthService object in the service graph and let CAM
    # delegate its operation-level checks to it.  The HTTP handlers perform
    # the same checks for clear status codes, while this callback protects
    # callers that invoke CAMService directly (workers/CLI jobs).
    auth = AuthService(
        db_value,
        token_secret=auth_secret,
        token_ttl_seconds=token_ttl_seconds,
    )
    cam = CAMService(
        authorizer=lambda actor_id, permission: auth.has_permission(actor_id, permission)
    )
    return PlatformServices(
        pdm=PDMRepository(db_value),
        auth=auth,
        ocr=OCRService(enable_live_ocr=enable_live_ocr),
        cam=cam,
    )


def _decode_base64(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValidationError("imageBase64/contentBase64 must be a non-empty string")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValidationError("invalid base64 payload") from exc


def _require_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("JSON object body is required")
    return payload


def _domain_http_exception(exc: Exception):
    """Convert a service exception to an HTTPException without importing early."""

    from fastapi import HTTPException

    if isinstance(exc, (AuthenticationError,)):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (NotFoundError, CAMNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (ConflictError, CAMConflictError, CAMGateRejected)):
        detail: Any = str(exc)
        if isinstance(exc, CAMGateRejected):
            detail = {"message": str(exc), "gate": exc.decision.to_dict()}
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, (ValidationError, KeyError, TypeError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="internal platform error")


def _token_user(services: PlatformServices, authorization: str | None):
    if not authorization:
        raise _domain_http_exception(AuthenticationError("bearer token is required"))
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise _domain_http_exception(AuthenticationError("bearer token is required"))
    try:
        return services.auth.verify_token(token.strip())
    except PlatformError as exc:
        raise _domain_http_exception(exc)


def create_platform_router(services: PlatformServices, *, prefix: str = ""):
    """Return an ``APIRouter`` with auth, PDM, OCR and CAM endpoints.

    The import is lazy so service users do not need FastAPI installed.  Routes
    use untyped dictionaries intentionally: the domain dataclasses are the
    stable contract, while this keeps the adapter compatible with both Pydantic
    v1 and v2 environments used by deployments.
    """

    try:
        from fastapi import APIRouter, Body, Header, HTTPException, Request
        from fastapi.responses import JSONResponse, PlainTextResponse
    except ImportError as exc:  # pragma: no cover - exercised in dependency-free CI
        raise RuntimeError(
            "FastAPI is optional for the domain services; install apps/api requirements to create routes"
        ) from exc

    router = APIRouter(prefix=prefix, tags=["platform"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "joyniu-platform", "features": ["pdm", "rbac", "ocr", "cam"]}

    @router.post("/auth/users", status_code=201)
    async def create_user(
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        data = _require_dict(payload)
        # First account is a deliberately explicit local bootstrap.  Once an
        # account exists, only an admin may create another account.
        if services.auth.count_users() > 0:
            actor = _token_user(services, authorization)
            try:
                services.auth.require(actor, Permission.USER_MANAGE)
            except PlatformError as exc:
                raise _domain_http_exception(exc)
            actor_id = actor.id
        else:
            actor_id = "bootstrap"
        try:
            roles = data.get("roles", [Role.VIEWER.value])
            user = services.auth.create_user(
                str(data.get("email", "")),
                str(data.get("password", "")),
                str(data.get("displayName", data.get("display_name", ""))),
                roles=roles,
                actor_id=actor_id,
            )
            return user.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/auth/login")
    async def login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        data = _require_dict(payload)
        try:
            token = services.auth.authenticate(str(data.get("email", "")), str(data.get("password", "")))
            return token.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/auth/me")
    async def me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        return _token_user(services, authorization).to_dict()

    @router.get("/auth/users")
    async def list_users(
        authorization: str | None = Header(default=None),
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.USER_MANAGE)
            return {"items": [item.to_dict() for item in services.auth.list_users(include_inactive=include_inactive)]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.patch("/auth/users/{user_id}/roles")
    async def assign_roles(
        user_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.USER_MANAGE)
            return services.auth.assign_roles(user_id, _require_dict(payload).get("roles", []), actor_id=actor.id).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.patch("/auth/users/{user_id}/active")
    async def set_user_active(
        user_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.USER_MANAGE)
            return services.auth.set_active(user_id, bool(_require_dict(payload).get("active", True)), actor_id=actor.id).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/pdm/projects", status_code=201)
    async def create_project(
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.PROJECT_WRITE)
            owner_id = str(data.get("ownerId", data.get("owner_id", actor.id)))
            if owner_id != actor.id:
                services.auth.require(actor, Permission.USER_MANAGE)
            return services.pdm.create_project(
                str(data.get("name", "")),
                owner_id,
                description=str(data.get("description", "")),
                metadata=data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {},
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/projects")
    async def list_projects(
        authorization: str | None = Header(default=None),
        include_archived: bool = False,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.PROJECT_READ)
            projects = services.pdm.list_projects(
                owner_id=None if services.auth.has_permission(actor, Permission.USER_MANAGE) else actor.id,
                include_archived=include_archived,
            )
            return {"items": [project.to_dict() for project in projects]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/pdm/projects/{project_id}/documents", status_code=201)
    async def create_document(
        project_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE)
            return services.pdm.create_document(
                project_id,
                str(data.get("name", "")),
                str(data.get("kind", "model")),
                actor.id,
                metadata=data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {},
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.patch("/pdm/projects/{project_id}")
    async def rename_project(
        project_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.PROJECT_WRITE)
            return services.pdm.rename_project(
                project_id,
                str(data.get("name", "")),
                description=(str(data["description"]) if "description" in data else None),
                actor_id=actor.id,
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/projects/{project_id}/documents")
    async def list_documents(
        project_id: str,
        authorization: str | None = Header(default=None),
        kind: str | None = None,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_READ)
            return {"items": [item.to_dict() for item in services.pdm.list_documents(project_id, kind=kind)]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.patch("/pdm/documents/{document_id}")
    async def rename_document(
        document_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE)
            metadata = data.get("metadata")
            return services.pdm.rename_document(
                document_id,
                str(data.get("name", "")),
                actor_id=actor.id,
                metadata=metadata if isinstance(metadata, Mapping) else None,
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/projects/{project_id}/manifest")
    async def project_manifest(
        project_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.PROJECT_READ)
            return services.pdm.get_project_manifest(project_id)
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/pdm/documents/{document_id}/versions", status_code=201)
    async def create_version(
        document_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.VERSION_CREATE)
            if "contentBase64" in data or "content_base64" in data:
                content: Any = _decode_base64(data.get("contentBase64", data.get("content_base64")))
            elif "contentText" in data or "content_text" in data:
                content = str(data.get("contentText", data.get("content_text")))
            else:
                content = data.get("content", data.get("payload"))
            version = services.pdm.create_version(
                document_id,
                content,
                actor.id,
                file_name=data.get("fileName", data.get("file_name")),
                content_type=str(data.get("contentType", data.get("content_type", "application/octet-stream"))),
                note=str(data.get("note", "")),
                metadata=data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {},
                expected_current_revision=(
                    int(data["expectedRevision"])
                    if data.get("expectedRevision") is not None
                    else (
                        int(data["expected_current_revision"])
                        if data.get("expected_current_revision") is not None
                        else None
                    )
                ),
            )
            return version.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/documents/{document_id}/versions")
    async def list_versions(
        document_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_READ)
            return {"items": [item.to_dict() for item in services.pdm.list_versions(document_id)]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/versions/{version_id}/content")
    async def get_version_content(version_id: str, authorization: str | None = Header(default=None)):
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_READ)
            version = services.pdm.get_version(version_id)
            from fastapi.responses import Response

            return Response(
                content=services.pdm.get_version_content(version_id),
                media_type=version.content_type,
                headers={"Content-Disposition": f'attachment; filename="{version.file_name}"'},
            )
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.patch("/pdm/documents/{document_id}/status")
    async def change_document_status(
        document_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            requested_status = str(data.get("status", ""))
            permission = Permission.DOCUMENT_RELEASE if requested_status == "released" else Permission.DOCUMENT_REVIEW
            services.auth.require(actor, permission)
            return services.pdm.change_document_status(
                document_id,
                requested_status,
                actor_id=actor.id,
                expected_current_revision=data.get("expectedRevision"),
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.delete("/pdm/documents/{document_id}")
    async def delete_document(
        document_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE)
            return services.pdm.soft_delete_document(document_id, actor_id=actor.id).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/audit")
    async def list_audit(
        authorization: str | None = Header(default=None),
        resource_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.AUDIT_READ)
            return {"items": [item.to_dict() for item in services.pdm.list_audit_events(resource_id=resource_id, limit=limit)]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/pdm/documents/{document_id}/restore")
    async def restore_document(
        document_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_WRITE)
            return services.pdm.restore_document(document_id, actor_id=actor.id).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/ocr/fixtures")
    async def list_ocr_fixtures() -> dict[str, Any]:
        return {"items": services.ocr.list_fixtures()}

    @router.post("/ocr/analyze")
    async def analyze_drawing(
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.OCR_RUN)
            encoded = data.get("imageBase64", data.get("image_base64", data.get("contentBase64")))
            image_bytes = _decode_base64(encoded)
            result = services.ocr.analyze(
                image_bytes,
                filename=str(data.get("filename", data.get("sourceFilename", "drawing.png"))),
                fixture_id=data.get("fixtureId", data.get("fixture_id")),
            )
            services.recognitions[result.id] = result
            return result.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/ocr/analyze-bytes")
    async def analyze_drawing_bytes(
        request: Request,
        authorization: str | None = Header(default=None),
        filename: str = "drawing.png",
        fixture_id: str | None = None,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.OCR_RUN)
            result = services.ocr.analyze(await request.body(), filename=filename, fixture_id=fixture_id)
            services.recognitions[result.id] = result
            return result.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/ocr/{recognition_id}/confirm")
    async def confirm_drawing(
        recognition_id: str,
        payload: dict[str, Any] = Body(default={}),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_REVIEW)
            recognition = services.recognitions.get(recognition_id)
            if recognition is None:
                raise NotFoundError(f"drawing recognition not found: {recognition_id}")
            data = _require_dict(payload)
            result = services.ocr.confirm(
                recognition,
                reviewer_id=actor.id,
                parameter_overrides=data.get("parameterOverrides", data.get("parameter_overrides", {})),
            )
            services.recognitions[recognition_id] = result
            return result.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/workflows/drawing-to-model", status_code=201)
    async def drawing_to_model_workflow(
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> Any:
        """Run the auditable upload → OCR → OCCT → PDM workflow in one call.

        The endpoint is intentionally explicit about ``confirmed``.  A
        reviewer can approve a recognition in the same request; an unreviewed
        drawing returns HTTP 409 with its evidence and creates no model
        versions.  Every accepted blob is stored as an immutable PDM version,
        so the returned manifest can be inspected after the request finishes.
        """

        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.OCR_RUN)
            services.auth.require(actor, Permission.PROJECT_WRITE)
            services.auth.require(actor, Permission.DOCUMENT_WRITE)
            services.auth.require(actor, Permission.VERSION_CREATE)
            encoded = data.get("imageBase64", data.get("image_base64", data.get("contentBase64")))
            image_bytes = _decode_base64(encoded)
            filename = str(data.get("filename", data.get("sourceFilename", "drawing.png")))
            recognition = services.ocr.analyze(
                image_bytes,
                filename=filename,
                fixture_id=data.get("fixtureId", data.get("fixture_id")),
            )
            services.recognitions[recognition.id] = recognition
            if recognition.status != "confirmed":
                if not bool(data.get("confirmed", False)):
                    return JSONResponse(
                        status_code=409,
                        content={
                            "message": "drawing evidence requires reviewer confirmation",
                            "recognition": recognition.to_dict(),
                        },
                    )
                services.auth.require(actor, Permission.DOCUMENT_REVIEW)
                recognition = services.ocr.confirm(
                    recognition,
                    reviewer_id=actor.id,
                    parameter_overrides=data.get("parameterOverrides", data.get("parameter_overrides", {})),
                )
                services.recognitions[recognition.id] = recognition

            project_id = data.get("projectId", data.get("project_id"))
            if project_id:
                project = services.pdm.get_project(str(project_id))
                if project.owner_id != actor.id:
                    services.auth.require(actor, Permission.USER_MANAGE)
            else:
                project = services.pdm.create_project(
                    str(data.get("projectName", data.get("project_name", "图纸转三维验收项目"))),
                    actor.id,
                    description="由 drawing-to-model workflow 自动创建",
                    metadata={"workflow": "drawing-to-model", "recognitionId": recognition.id},
                )

            # Store the source before geometry generation; this is the durable
            # audit anchor even when a kernel or validation step rejects the
            # requested model.
            source_document = services.pdm.create_document(
                project.id,
                filename,
                "drawing",
                actor.id,
                metadata={"sha256": recognition.source_sha256, "recognitionId": recognition.id},
            )
            source_version = services.pdm.create_version(
                source_document.id,
                image_bytes,
                actor.id,
                file_name=filename,
                content_type=str(data.get("contentType", "application/octet-stream")),
                metadata={"recognitionId": recognition.id, "sourceSha256": recognition.source_sha256},
            )

            from .geometry import generate_artifacts, validate_bracket
            from .schemas import BracketParameters

            parameters = BracketParameters.model_validate(
                recognition.model_recipe.get("parameters", {})
            )
            report = validate_bracket(parameters)
            if not report.valid:
                raise ValidationError("recognized parameters failed geometry validation")
            formats = data.get("formats", ["step", "glb"])
            if not isinstance(formats, (list, tuple)):
                raise ValidationError("formats must be an array")
            generated = generate_artifacts(
                parameters,
                formats,
                require_cadquery=bool(data.get("requireCadQuery", data.get("require_cadquery", True))),
            )
            run_id = uuid.uuid4().hex[:10]
            parameter_document = services.pdm.create_document(
                project.id,
                f"{Path(filename).stem}-parameters-{run_id}.json",
                "model",
                actor.id,
                metadata={"recognitionId": recognition.id, "sourceVersionId": source_version.id},
            )
            parameter_version = services.pdm.create_version(
                parameter_document.id,
                parameters.model_dump(by_alias=True),
                actor.id,
                file_name=parameter_document.name,
                content_type="application/json",
                metadata={"recognitionId": recognition.id, "sourceVersionId": source_version.id, "validation": report.metrics},
            )
            artifact_records: list[dict[str, Any]] = []
            for artifact in generated:
                artifact_document = services.pdm.create_document(
                    project.id,
                    f"{Path(filename).stem}-{run_id}.{artifact.format}",
                    "model",
                    actor.id,
                    metadata={
                        "recognitionId": recognition.id,
                        "parameterVersionId": parameter_version.id,
                        "engine": artifact.engine,
                        "productionReady": artifact.production_ready,
                    },
                )
                artifact_version = services.pdm.create_version(
                    artifact_document.id,
                    artifact.data,
                    actor.id,
                    file_name=artifact_document.name,
                    content_type={"step": "application/step", "glb": "model/gltf-binary"}.get(artifact.format, "application/octet-stream"),
                    metadata={
                        "recognitionId": recognition.id,
                        "parameterVersionId": parameter_version.id,
                        "engine": artifact.engine,
                        "productionReady": artifact.production_ready,
                        "sha256": artifact.sha256,
                    },
                )
                artifact_records.append({"format": artifact.format, "engine": artifact.engine, "productionReady": artifact.production_ready, "version": artifact_version.to_dict()})

            return {
                "workflow": "drawing-to-model",
                "project": project.to_dict(),
                "recognition": recognition.to_dict(),
                "validation": report.model_dump(mode="json", by_alias=True),
                "pdm": {
                    "sourceDocument": source_document.to_dict(),
                    "sourceVersion": source_version.to_dict(),
                    "parameterDocument": parameter_document.to_dict(),
                    "parameterVersion": parameter_version.to_dict(),
                    "artifacts": artifact_records,
                },
                "next": {
                    "cam": "Create a CAM plan using the parameter/model version hash; release remains reviewer-gated.",
                    # The router is mounted under both /api and /api/v1 by the
                    # default app; return the versioned path so a browser can
                    # follow it directly without guessing the mount prefix.
                    "manifestPath": f"/api/v1/pdm/projects/{project.id}/manifest",
                },
            }
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/cam/plans", status_code=201)
    async def create_cam_plan(
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.CAM_PLAN)
            plan = services.cam.create_plan(
                actor_id=actor.id,
                geometry_hash=str(data.get("geometryHash", data.get("geometry_hash", ""))),
                stock=data.get("stock", {}),
                machine=str(data.get("machine", "3-axis-mill")),
                units=str(data.get("units", "mm")),
                project_id=data.get("projectId", data.get("project_id")),
                source_document_id=data.get("sourceDocumentId", data.get("source_document_id")),
                source_version_id=data.get("sourceVersionId", data.get("source_version_id")),
            )
            return plan.to_dict()
        except (PlatformError, KeyError, TypeError, ValueError) as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/plans")
    async def list_cam_plans(
        authorization: str | None = Header(default=None),
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.CAM_PLAN)
            return {"items": [plan.to_dict() for plan in services.cam.list_plans(project_id=project_id)]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/cam/plans/{plan_id}/operations", status_code=201)
    async def add_cam_operation(
        plan_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            # Editing a plan is a design action.  Keep this check at the
            # boundary as well as in CAMService so a viewer cannot mutate a
            # plan by calling the router with a valid bearer token.
            services.auth.require(actor, Permission.CAM_PLAN)
            operation = services.cam.add_operation(
                plan_id,
                actor_id=actor.id,
                operation_type=str(data.get("operationType", data.get("operation_type", "profile"))),
                tool_id=str(data.get("toolId", data.get("tool_id", "T10"))),
                depth=float(data.get("depth", 1)),
                feed_rate=float(data.get("feedRate", data.get("feed_rate", 600))),
                spindle_rpm=int(data.get("spindleRpm", data.get("spindle_rpm", 6000))),
                retract_height=float(data.get("retractHeight", data.get("retract_height", 5))),
                path_length=float(data.get("pathLength", data.get("path_length", 0))),
                parameters=data.get("parameters") if isinstance(data.get("parameters"), Mapping) else {},
                enabled=bool(data.get("enabled", True)),
                expected_revision=data.get("expectedRevision", data.get("expected_revision")),
            )
            return operation.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/cam/plans/{plan_id}/simulate")
    async def simulate_cam_plan(
        plan_id: str,
        payload: dict[str, Any] = Body(default={}),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            # Simulation is available only to CAM planners/designers (and
            # manufacturing roles); requiring both permissions prevents a
            # future role from accidentally gaining simulation through a
            # partial permission grant.
            services.auth.require(actor, Permission.CAM_PLAN, Permission.CAM_SIMULATE)
            return services.cam.simulate(
                plan_id,
                actor_id=actor.id,
                expected_revision=data.get("expectedRevision", data.get("expected_revision")),
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/plans/{plan_id}/gate")
    async def cam_gate(
        plan_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.CAM_RELEASE)
            permissions = actor.permissions
            return services.cam.gate_status(plan_id, actor_id=actor.id, actor_permissions=permissions).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/cam/plans/{plan_id}/approve", status_code=201)
    async def approve_cam_plan(
        plan_id: str,
        payload: dict[str, Any] = Body(default={}),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.CAM_APPROVE)
            # A manufacturing account may release an already approved plan,
            # but may not supply the approval itself.  Admins are the explicit
            # emergency/administrative exception and are still subject to the
            # separate release-actor gate.
            actor_roles = {str(item).casefold() for item in actor.roles}
            if Role.REVIEWER.value not in actor_roles and Role.ADMIN.value not in actor_roles:
                raise AuthorizationError("reviewer role is required to approve a CAM plan")
            requested_role = str(data.get("role", Role.REVIEWER.value)).strip().casefold()
            if requested_role not in {Role.REVIEWER.value, Role.ADMIN.value}:
                raise AuthorizationError("CAM approval role must be reviewer or admin")
            if requested_role == Role.ADMIN.value and Role.ADMIN.value not in actor_roles:
                raise AuthorizationError("only an admin may record an admin CAM approval")
            return services.cam.approve(
                plan_id,
                actor_id=actor.id,
                role=requested_role,
                comment=str(data.get("comment", "")),
                simulation_id=data.get("simulationId", data.get("simulation_id")),
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/cam/plans/{plan_id}/release")
    async def release_cam_plan(
        plan_id: str,
        payload: dict[str, Any] = Body(default={}),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.CAM_RELEASE)
            actor_roles = {str(item).casefold() for item in actor.roles}
            if Role.MANUFACTURING.value not in actor_roles and Role.ADMIN.value not in actor_roles:
                raise AuthorizationError("manufacturing or admin role is required to release NC")
            program = services.cam.release_nc(
                plan_id,
                actor_id=actor.id,
                postprocessor=str(data.get("postprocessor", "generic-3axis")),
                actor_permissions=actor.permissions,
            )
            result = program.to_dict()
            # Return text only when explicitly requested; this avoids leaking NC
            # into ordinary list views while retaining a one-call demo flow.
            if data.get("includeText"):
                result["text"] = program.text
            return result
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/nc/{program_id}")
    async def download_nc(
        program_id: str,
        authorization: str | None = Header(default=None),
    ):
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.NC_DOWNLOAD)
            program = services.cam.get_nc(program_id)
            return PlainTextResponse(
                program.text,
                media_type="text/plain",
                headers={"Content-Disposition": f'attachment; filename="{program.id}.nc"'},
            )
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    return router


# A default router keeps the geometry entrypoint's optional dynamic inclusion
# backwards-compatible.  Hosts that need explicit lifecycle/database control
# should call ``build_platform_services`` and ``create_platform_router``
# themselves.  FastAPI remains optional for direct service usage.
try:  # pragma: no cover - exercised by the FastAPI application
    _default_database = os.getenv("JOYNIU_DB", ":memory:")
    default_services = build_platform_services(
        _default_database,
        auth_secret=os.getenv("JOYNIU_AUTH_SECRET"),
        enable_live_ocr=None,
    )
    router = create_platform_router(default_services)
except RuntimeError:  # FastAPI not installed; domain imports still work.
    default_services = None
    router = None


__all__ = [
    "PlatformServices",
    "build_platform_services",
    "create_platform_router",
    "default_services",
    "router",
]
