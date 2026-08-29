"""FastAPI entrypoint for JoyNiu NewCAD geometry workflows.

Run locally with:

    uvicorn app.main:app --reload --port 8010

The geometry endpoints keep short-lived recognition/artifact caches for fast
downloads. The mounted platform router adds SQLite-backed PDM, RBAC, OCR
evidence and CAM/NC release records; use ``JOYNIU_DB`` to choose its database.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import ValidationError

from . import __version__
from .geometry import (
    GeneratedArtifact,
    MEDIA_TYPES,
    cadquery_status,
    generate_artifacts,
    validate_bracket,
)
from .recognition import recognize_drawing_bytes, parse_hints_json
from .schemas import (
    ArtifactDescriptor,
    BracketParameters,
    DrawingRecognition,
    DrawingResultSubmission,
    GeometryRequest,
    GeometryResponse,
    ValidationReport,
)


MAX_DRAWING_BYTES = int(os.getenv("JOYNIU_MAX_DRAWING_BYTES", str(20 * 1024 * 1024)))

app = FastAPI(
    title="JoyNiu NewCAD API",
    version=__version__,
    description=(
        "Auditable drawing-to-bracket geometry service. CadQuery/OCCT is used "
        "when installed; otherwise exports are explicitly marked as fallback previews."
    ),
)

origins = [
    item.strip()
    for item in os.getenv(
        "JOYNIU_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if item.strip()
]
# Local Vite ports are intentionally flexible during development (the API is
# commonly started on 8010 while a second preview runs on 5174/5175).  Keep
# the permissive rule limited to loopback; production deployments should set
# JOYNIU_CORS_ORIGINS explicitly and can disable this regex.
local_origin_regex = os.getenv(
    "JOYNIU_CORS_ORIGIN_REGEX",
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_origin_regex=local_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_drawings: dict[str, DrawingRecognition] = {}
_artifacts: dict[str, dict[str, Any]] = {}
_requests: dict[str, GeometryResponse] = {}


def _json(model: Any) -> Any:
    """Serialize Pydantic models with the browser-facing camelCase aliases."""

    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", by_alias=True)
    return model


def _parameters_from_body(body: Any) -> BracketParameters:
    if body is None:
        return BracketParameters()
    if isinstance(body, BracketParameters):
        return body
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object expected")
    payload = body.get("parameters", body)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="parameters must be an object")
    try:
        return BracketParameters.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _geometry_request(body: Any) -> GeometryRequest:
    if body is None:
        return GeometryRequest()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="JSON object expected")
    payload = dict(body)
    if "parameters" not in payload:
        parameter_keys = set(BracketParameters.model_fields)
        parameter_aliases = {
            field.alias for field in BracketParameters.model_fields.values()
        }
        if parameter_keys.intersection(payload) or parameter_aliases.intersection(payload):
            params = {
                key: value
                for key, value in payload.items()
                if key in parameter_keys or key in parameter_aliases
            }
            payload = {
                "parameters": params,
                "formats": body.get("formats", ["step", "glb"]),
                "sourceDrawingId": body.get("sourceDrawingId"),
                "requireCadQuery": body.get("requireCadQuery", False),
                "confirmed": body.get("confirmed", False),
            }
    try:
        return GeometryRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _validation_error(report: ValidationReport) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "message": "Geometry validation failed; no artifact was generated.",
            "validation": _json(report),
        },
    )


def _reconcile_validation_with_artifacts(
    report: ValidationReport,
    generated: list[GeneratedArtifact],
) -> tuple[ValidationReport, str]:
    """Make the response-level delivery flags agree with emitted artifacts.

    ``validate_bracket`` audits the requested geometry before exporters run.
    An exporter can still fail afterwards (for example, an OCCT STEP writer
    may be missing a platform-specific shared library) and
    :func:`generate_artifacts` will then return an explicitly-labelled
    faceted preview.  Returning the pre-export report unchanged in that case
    would incorrectly tell clients that a production B-Rep was delivered.

    STEP is the production deliverable; GLB is always a visualization export.
    Consequently ``productionReady`` is true only when the validation passed
    *and* a real ``cadquery-occt`` STEP artifact is present.  The primary
    ``engine`` follows the STEP artifact when one was requested, otherwise the
    first emitted artifact, so a fallback can never be represented as
    ``cadquery-occt`` at the response level.
    """

    step = next((item for item in generated if item.format == "step"), None)
    primary = step or (generated[0] if generated else None)
    step_production = bool(
        step
        and step.production_ready
        and step.engine == "cadquery-occt"
    )
    production_ready = bool(report.production_ready and step_production)

    if production_ready:
        response_engine = "cadquery-occt"
        reason = "OCCT STEP artifact passed the production delivery gate"
    elif primary is not None:
        response_engine = primary.engine
        if step is None:
            reason = "no STEP artifact was requested; emitted artifacts are preview-only"
        elif not step.production_ready:
            reason = "STEP exporter returned a preview artifact; production B-Rep is unavailable"
        elif step.engine != "cadquery-occt":
            reason = f"STEP artifact engine {step.engine!r} is not OCCT B-Rep"
        else:
            reason = "geometry validation did not pass the production delivery gate"
    else:  # Defensive: GeometryRequest rejects an empty format list.
        response_engine = report.engine if not report.production_ready else "no-artifact"
        reason = "no geometry artifact was emitted"

    metrics = dict(report.metrics)
    metrics.update(
        {
            "artifactProductionReady": production_ready,
            "stepArtifactProductionReady": step_production,
            "artifactEngine": response_engine,
            "productionReadyReason": reason,
            "previewOnly": not production_ready,
        }
    )
    reconciled = report.model_copy(
        update={
            "production_ready": production_ready,
            "engine": response_engine,
            "metrics": metrics,
        }
    )
    return reconciled, response_engine


@app.get("/health", tags=["system"])
@app.get("/api/health", tags=["system"])
@app.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, Any]:
    cq = cadquery_status()
    return {
        "status": "ok" if cq["available"] else "degraded",
        "service": "joyniu-cad-api",
        "version": __version__,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "geometry": cq,
        "capabilities": {
            "drawingRecognition": True,
            "drawingResultSubmission": True,
            "bracketValidation": True,
            "stepExport": True,
            "glbExport": True,
            "cadqueryBRep": bool(cq["available"]),
            "ocr": _ocr_capability(),
        },
        "notes": (
            []
            if cq["available"]
            else [
                "Install joyniu-cad-api[geometry] for OCCT-backed B-Rep STEP.",
                "Fallback GLB/STEP artifacts remain deterministic and auditable, "
                "but productionReady=false.",
            ]
        ),
    }


def _ocr_capability() -> dict[str, Any]:
    try:
        import PIL  # type: ignore
        import pytesseract  # type: ignore

        version = getattr(pytesseract, "get_tesseract_version", lambda: None)()
        return {
            "available": True,
            "engine": "pytesseract",
            # pytesseract may return a ``packaging.version.Version`` object;
            # health must remain JSON serialisable across library versions.
            "version": str(version) if version is not None else None,
        }
    except Exception:
        return {
            "available": False,
            "engine": "calibration-fallback",
            "note": "Install joyniu-cad-api[ocr] and the tesseract binary for OCR.",
        }


@app.post(
    "/api/drawings/recognize",
    response_model=DrawingRecognition,
    tags=["drawings"],
)
@app.post(
    "/api/v1/drawings/recognize",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
@app.post(
    "/api/drawing/recognize",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
async def recognize_drawing(
    file: UploadFile = File(..., description="PNG/JPEG/GIF drawing or PDF preview"),
    hints: str | None = Form(None, description="Optional JSON dimension hints"),
    hints_json: str | None = Form(None, description="Alias for hints"),
) -> DrawingRecognition:
    data = await file.read()
    if len(data) > MAX_DRAWING_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"drawing exceeds {MAX_DRAWING_BYTES} byte upload limit",
        )
    content_type = (file.content_type or "").lower()
    # PDF/DXF are accepted for the reviewable OCR path; a rasterizer/parser can
    # be enabled independently without changing the upload contract.
    extension = Path(file.filename or "").suffix.casefold()
    allowed = (
        content_type.startswith("image/")
        or content_type in {
            "application/pdf",
            "application/dxf",
            "image/vnd.dxf",
            "application/acad",
            "application/x-dxf",
            "application/octet-stream",
            "",
        }
        or extension in {".pdf", ".dxf", ".dwg"}
    )
    if not allowed:
        raise HTTPException(
            status_code=415,
            detail="upload must be an image (PNG/JPEG/GIF), PDF or DXF/DWG",
        )
    raw_hints = hints_json or hints
    result = recognize_drawing_bytes(
        data,
        filename=file.filename or "drawing",
        hints=parse_hints_json(raw_hints),
    )
    _drawings[result.id] = result
    return result


@app.post(
    "/api/drawings/results",
    response_model=DrawingRecognition,
    tags=["drawings"],
)
@app.post(
    "/api/v1/drawings/results",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
@app.post(
    "/api/drawing/results",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
async def submit_drawing_result(body: dict[str, Any] = Body(...)) -> DrawingRecognition:
    # Accept a wrapper from vision providers: {"result": {...}}.
    payload = body.get("result", body) if isinstance(body, dict) else body
    try:
        submission = DrawingResultSubmission.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    validation = validate_bracket(submission.parameters)
    result = DrawingRecognition(
        id=f"drw_{uuid4().hex[:16]}",
        # This compatibility endpoint has no authenticated reviewer context.
        # Never trust a client-supplied ``confirmed`` bit: otherwise an
        # unauthenticated caller could inject arbitrary parameters and attach
        # them to a production geometry request.  Confirmation is performed by
        # the reviewer-protected platform OCR route instead.
        status="needs_review",
        partType="bracket",
        sourceFilename=submission.source_filename,
        sourceSha256=submission.source_sha256,
        confidence=submission.confidence,
        parameters=submission.parameters,
        evidence=submission.evidence,
        ocrText=submission.ocr_text,
        warnings=[
            "External recognition is untrusted; reviewer confirmation is required before geometry generation."
        ],
        validation=validation,
    )
    _drawings[result.id] = result
    return result


@app.get(
    "/api/drawings/{drawing_id}",
    response_model=DrawingRecognition,
    tags=["drawings"],
)
@app.get(
    "/api/v1/drawings/{drawing_id}",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
async def get_drawing_result(drawing_id: str) -> DrawingRecognition:
    result = _drawings.get(drawing_id)
    if result is None:
        raise HTTPException(status_code=404, detail="drawing recognition not found")
    return result


@app.post(
    "/api/drawings/{drawing_id}/confirm",
    response_model=DrawingRecognition,
    tags=["drawings"],
)
@app.post(
    "/api/v1/drawings/{drawing_id}/confirm",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
async def confirm_drawing_result(
    drawing_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    authorization: str | None = Header(default=None),
) -> DrawingRecognition:
    """Confirm a compatibility recognition through an authenticated reviewer.

    The public upload/result endpoints intentionally do not trust a client
    ``confirmed`` flag.  This route is the bridge for clients that use the
    lightweight ``/drawings/recognize`` API: a reviewer token is required, and
    optional parameter overrides are validated before the result can be used
    as ``sourceDrawingId`` for geometry generation.
    """

    if platform_services is None:
        raise HTTPException(
            status_code=503,
            detail="platform authentication service is unavailable",
        )
    try:
        # Import lazily to keep the geometry module importable without the
        # optional platform/FastAPI service graph.
        from .platform import Permission, PlatformError
        from .platform_api import _token_user

        actor = _token_user(platform_services, authorization)
        platform_services.auth.require(actor, Permission.DOCUMENT_REVIEW)
    except PlatformError as exc:
        # Reuse the platform adapter's stable 401/403 mapping.
        from .platform_api import _domain_http_exception

        raise _domain_http_exception(exc)

    result = _drawings.get(drawing_id)
    if result is None:
        raise HTTPException(status_code=404, detail="drawing recognition not found")
    if result.status == "rejected":
        raise HTTPException(status_code=409, detail="rejected drawing cannot be confirmed")

    data = body if isinstance(body, dict) else {}
    raw_overrides = data.get("parameterOverrides", data.get("parameter_overrides", {}))
    if raw_overrides is None:
        raw_overrides = {}
    if not isinstance(raw_overrides, dict):
        raise HTTPException(status_code=422, detail="parameterOverrides must be an object")
    allowed_override_keys = set(BracketParameters.model_fields)
    allowed_override_keys.update(
        field.alias
        for field in BracketParameters.model_fields.values()
        if getattr(field, "alias", None)
    )
    unknown_overrides = sorted(
        str(key) for key in raw_overrides if str(key) not in allowed_override_keys
    )
    if unknown_overrides:
        raise HTTPException(
            status_code=422,
            detail="unknown parameter override(s): " + ", ".join(unknown_overrides),
        )

    parameters = result.parameters
    if raw_overrides:
        payload = parameters.model_dump(by_alias=True)
        payload.update(raw_overrides)
        try:
            parameters = BracketParameters.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        override_report = validate_bracket(parameters)
        if not override_report.valid:
            raise _validation_error(override_report)
    else:
        override_report = result.validation

    recipe = dict(result.model_recipe or {})
    recipe["parameters"] = parameters.model_dump(by_alias=True)
    recipe["confirmedBy"] = actor.id
    warnings = list(result.warnings or [])
    warning = "Reviewer confirmation recorded; geometry generation may proceed."
    if warning not in warnings:
        warnings.append(warning)
    confirmed = result.model_copy(
        update={
            "status": "confirmed",
            "parameters": parameters,
            "validation": override_report,
            "model_recipe": recipe,
            "review_required": False,
            "warnings": warnings,
        }
    )
    _drawings[drawing_id] = confirmed
    return confirmed


@app.post(
    "/api/brackets/validate",
    response_model=ValidationReport,
    tags=["geometry"],
)
@app.post(
    "/api/v1/brackets/validate",
    response_model=ValidationReport,
    include_in_schema=False,
)
async def validate_bracket_endpoint(body: dict[str, Any] = Body(...)) -> ValidationReport:
    parameters = _parameters_from_body(body)
    return validate_bracket(parameters)


@app.post(
    "/api/brackets/generate",
    response_model=GeometryResponse,
    tags=["geometry"],
)
@app.post(
    "/api/v1/brackets/generate",
    response_model=GeometryResponse,
    include_in_schema=False,
)
async def generate_bracket(body: dict[str, Any] = Body(default_factory=dict)) -> GeometryResponse:
    request = _geometry_request(body)
    if request.source_drawing_id:
        drawing = _drawings.get(request.source_drawing_id)
        # Platform AI uploads are registered in the authenticated service
        # graph rather than the legacy compatibility map above.  Accept that
        # same-account recognition hand-off here, but only when the platform
        # service marked it confirmed by its reviewer workflow.
        if drawing is None and platform_services is not None:
            platform_drawing = platform_services.recognitions.get(request.source_drawing_id)
            if platform_drawing is not None:
                drawing = platform_drawing
        if drawing is None:
            raise HTTPException(status_code=404, detail="source drawing recognition not found")
        # ``confirmed`` is an informational client hint, not an authority
        # boundary.  A caller must use the reviewer-protected platform OCR
        # confirmation route (or submit an explicitly confirmed external
        # result) before a needs_review drawing can reach geometry generation.
        if drawing.status != "confirmed":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "drawing evidence requires reviewer confirmation before generation",
                    "drawingId": drawing.id,
                    "status": drawing.status,
                    "hint": "POST /api/v1/ocr/{recognitionId}/confirm with a reviewer token",
                },
            )
    report = validate_bracket(request.parameters)
    if not report.valid:
        raise _validation_error(report)
    try:
        generated = generate_artifacts(
            request.parameters,
            request.formats,
            require_cadquery=request.require_cadquery,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Exporters run after the OCCT topology audit and may still fail.  Reconcile
    # the final response with what was actually emitted so a faceted fallback
    # can never inherit productionReady=true/cadquery-occt from the pre-export
    # validation report.
    report, response_engine = _reconcile_validation_with_artifacts(report, generated)

    request_id = f"geo_{uuid4().hex[:16]}"
    descriptors: list[ArtifactDescriptor] = []
    for artifact in generated:
        artifact_id = f"art_{uuid4().hex[:16]}"
        extension = artifact.format
        filename = f"joyniu-bracket-{request_id}.{extension}"
        _artifacts[artifact_id] = {
            "id": artifact_id,
            "data": artifact.data,
            "format": artifact.format,
            "filename": filename,
            "media_type": MEDIA_TYPES[artifact.format],
            "engine": artifact.engine,
            "production_ready": artifact.production_ready,
            "warnings": artifact.warnings,
            "request_id": request_id,
            "sha256": hashlib.sha256(artifact.data).hexdigest(),
        }
        descriptors.append(
            ArtifactDescriptor(
                id=artifact_id,
                format=artifact.format,
                filename=filename,
                mediaType=MEDIA_TYPES[artifact.format],
                sizeBytes=len(artifact.data),
                sha256=hashlib.sha256(artifact.data).hexdigest(),
                downloadUrl=f"/api/artifacts/{artifact_id}.{extension}",
                engine=artifact.engine,
                productionReady=artifact.production_ready,
                warnings=list(artifact.warnings),
            )
        )

    response = GeometryResponse(
        requestId=request_id,
        status="completed",
        engine=response_engine,
        parameters=request.parameters,
        validation=report,
        artifacts=descriptors,
        sourceDrawingId=request.source_drawing_id,
    )
    _requests[request_id] = response
    return response


@app.post(
    "/api/brackets/export/{format}",
    tags=["geometry"],
    response_class=Response,
)
@app.post(
    "/api/v1/brackets/export/{format}",
    tags=["geometry"],
    response_class=Response,
    include_in_schema=False,
)
async def export_bracket_format(
    format: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> Response:
    fmt = format.lower().lstrip(".")
    if fmt not in {"step", "glb"}:
        raise HTTPException(status_code=404, detail="format must be step or glb")
    payload = dict(body)
    payload["formats"] = [fmt]
    result = await generate_bracket(payload)
    descriptor = result.artifacts[0]
    record = _artifacts[descriptor.id]
    headers = {
        "Content-Disposition": f'attachment; filename="{descriptor.filename}"',
        "X-JoyNiu-Artifact-Id": descriptor.id,
        "X-JoyNiu-Engine": descriptor.engine,
        "X-JoyNiu-Production-Ready": str(descriptor.production_ready).lower(),
        "X-JoyNiu-SHA256": descriptor.sha256,
    }
    return Response(
        content=record["data"],
        media_type=record["media_type"],
        headers=headers,
    )


@app.get("/api/artifacts/{artifact_id}.{format}", tags=["artifacts"])
@app.get("/api/artifacts/{artifact_id}", tags=["artifacts"], include_in_schema=False)
@app.get(
    "/api/v1/artifacts/{artifact_id}.{format}",
    tags=["artifacts"],
    include_in_schema=False,
)
@app.get(
    "/api/v1/artifacts/{artifact_id}",
    tags=["artifacts"],
    include_in_schema=False,
)
async def download_artifact(artifact_id: str, format: str | None = None) -> Response:
    lookup_id = artifact_id
    # The extensionless route is intentionally registered as a compatibility
    # alias and may receive a dotted URL before the explicit extension route.
    # Normalize it here so both /artifacts/art_x.step and /artifacts/art_x work.
    if lookup_id not in _artifacts and "." in lookup_id:
        candidate, suffix = lookup_id.rsplit(".", 1)
        if candidate in _artifacts:
            lookup_id = candidate
            format = format or suffix
    record = _artifacts.get(lookup_id)
    if record is None:
        raise HTTPException(status_code=404, detail="artifact not found or expired")
    if format and format.lower().lstrip(".") != record["format"]:
        raise HTTPException(status_code=409, detail="artifact format does not match URL")
    headers = {
        "Content-Disposition": f'attachment; filename="{record["filename"]}"',
        "X-JoyNiu-Artifact-Id": lookup_id,
        "X-JoyNiu-Engine": record["engine"],
        "X-JoyNiu-Production-Ready": str(record["production_ready"]).lower(),
        "X-JoyNiu-SHA256": record["sha256"],
    }
    return Response(
        content=record["data"],
        media_type=record["media_type"],
        headers=headers,
    )


@app.get("/api/brackets/requests/{request_id}", response_model=GeometryResponse, tags=["geometry"])
@app.get(
    "/api/v1/brackets/requests/{request_id}",
    response_model=GeometryResponse,
    include_in_schema=False,
)
async def get_geometry_request(request_id: str) -> GeometryResponse:
    result = _requests.get(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail="geometry request not found")
    return result


# Platform services (PDM/RBAC/OCR/CAM) are supplied by an optional module in
# this same package. Dynamic inclusion keeps the geometry service runnable when
# that module is not installed yet. A file-backed SQLite path is used by
# default so local restarts preserve PDM/RBAC state; set JOYNIU_DB=:memory:
# for ephemeral tests.
try:  # pragma: no cover - exercised when platform module is present
    from .platform_api import build_platform_services, create_platform_router

    _default_db = Path(__file__).resolve().parents[1] / "data" / "joyniu.sqlite3"
    _platform_db = os.getenv("JOYNIU_DB", str(_default_db))
    if _platform_db != ":memory:":
        Path(_platform_db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    platform_services = build_platform_services(
        _platform_db,
        auth_secret=os.getenv("JOYNIU_AUTH_SECRET"),
        enable_live_ocr=None,
    )
    # Geometry already owns the public /api[/v1]/health contract. Keep the
    # platform router's /health compatibility endpoints callable, but hide
    # those duplicate routes from OpenAPI to avoid ambiguous operation ids and
    # a documented response shape that differs from the route selected at
    # runtime.
    _platform_compat_router = create_platform_router(platform_services)
    for _route in _platform_compat_router.routes:
        if getattr(_route, "path", None) == "/health":
            _route.include_in_schema = False
    app.include_router(_platform_compat_router, prefix="/api")
    _platform_v1_router = create_platform_router(platform_services)
    for _route in _platform_v1_router.routes:
        if getattr(_route, "path", None) == "/health":
            _route.include_in_schema = False
    app.include_router(
        _platform_v1_router,
        prefix="/api/v1",
    )
except Exception as exc:  # pragma: no cover - optional platform dependency
    platform_services = None
    platform_router = None
    _platform_error = f"{type(exc).__name__}: {exc}"


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8010")), reload=False)


__all__ = ["app", "run"]
