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
from typing import Any, Mapping
from uuid import uuid4

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from . import __version__
from .download_headers import attachment_content_disposition
from .geometry import (
    GeneratedArtifact,
    MEDIA_TYPES,
    cadquery_status,
    generate_artifacts,
    validate_bracket,
)
from .recognition import recognize_drawing_bytes, parse_hints_json
from .model_recipes import (
    generate_model_recipe_artifacts,
    parse_model_parameters,
    validate_model_recipe,
)
from .schemas import (
    ArtifactDescriptor,
    BracketParameters,
    DrawingRecognition,
    DrawingResultSubmission,
    GeometryRequest,
    GeometryResponse,
    ModelGeometryRequest,
    ModelGeometryResponse,
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
_model_requests: dict[str, ModelGeometryResponse] = {}

# ``recognition.py`` is intentionally backwards-compatible: when no OCR
# dimension can be recovered it returns a complete canonical bracket profile
# so older clients can still render a preview.  Those values are a visual
# scaffold, not evidence from the uploaded sheet.  Keep the distinction at
# the HTTP confirmation boundary too; otherwise a direct legacy ``/accept``
# call could turn the scaffold into a production source without a human ever
# supplying the missing dimensions.
_LEGACY_FALLBACK_ENGINES = frozenset(
    {"heuristic-review", "tesseract-compatible", "compatibility-recognizer"}
)
_REQUIRED_DRAWING_FIELDS = frozenset(
    {
        "baseLength", "baseWidth", "baseThickness", "upperLength", "upperWidth",
        "upperHeight", "totalHeight", "notchOpening", "notchRadius", "slotLength",
        "slotWidth", "pocketDepth", "bossDiameter", "bossCenterDistance",
    }
)
_DRAWING_FIELD_ALIASES = {
    "base_length": "baseLength", "base_width": "baseWidth", "base_thickness": "baseThickness",
    "upper_length": "upperLength", "upper_width": "upperWidth", "upper_height": "upperHeight",
    "total_height": "totalHeight", "notch_opening": "notchOpening", "notch_radius": "notchRadius",
    "slot_length": "slotLength", "slot_width": "slotWidth", "pocket_depth": "pocketDepth",
    "saddle_depth": "saddleDepth", "hole_depth": "holeDepth", "hole_through": "holeThrough",
    "boss_diameter": "bossDiameter", "boss_center_distance": "bossCenterDistance", "boss_height": "bossHeight",
}


def _normalise_drawing_field(value: Any) -> str:
    raw = str(value or "")
    return _DRAWING_FIELD_ALIASES.get(raw, raw)


def _explicit_drawing_candidate_fields(
    result: DrawingRecognition,
    overrides: Mapping[str, Any] | None = None,
) -> set[str]:
    """Return fields backed by real candidate evidence, not fallback defaults.

    For the legacy compatibility recognizer, only explicit AI/OCR/hint
    dimensions and request overrides count.  The canonical ``parameters``
    object is deliberately ignored for fallback engines because it is filled
    even when the image contains no recoverable dimensions.  Non-fallback
    external submissions are already explicit structured candidates and keep
    their historical behaviour.
    """

    fields: set[str] = set()

    def add_mapping(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if item is not None and item != "":
                    normalized = _normalise_drawing_field(key)
                    if normalized in _REQUIRED_DRAWING_FIELDS:
                        fields.add(normalized)

    add_mapping(overrides)
    add_mapping(getattr(result, "candidate_parameters", {}))
    fallback_engine = str(getattr(result, "engine", "")) in _LEGACY_FALLBACK_ENGINES
    if not fallback_engine:
        parameters = getattr(result, "parameters", None)
        add_mapping(parameters.model_dump(by_alias=True) if hasattr(parameters, "model_dump") else parameters)
        add_mapping(getattr(result, "model_recipe", {}).get("parameters", {}))
        return fields

    # ``dimensions`` carries source labels.  Exclude the synthetic canonical
    # rows while retaining OCR-labelled values and client-hint rows.
    for dimension in getattr(result, "dimensions", ()) or ():
        if isinstance(dimension, Mapping):
            field = dimension.get("field")
            value = dimension.get("value")
            source = dimension.get("sourceText", dimension.get("source", ""))
        else:
            field = getattr(dimension, "field", "")
            value = getattr(dimension, "value", None)
            source = getattr(dimension, "source_text", getattr(dimension, "source", ""))
        if value is None or value == "":
            continue
        if str(source).casefold().startswith("canonical-bracket-fallback"):
            continue
        normalized = _normalise_drawing_field(field)
        if normalized in _REQUIRED_DRAWING_FIELDS:
            fields.add(normalized)
    return fields


def _legacy_candidate_missing(
    result: DrawingRecognition,
    overrides: Mapping[str, Any] | None = None,
) -> list[str]:
    return sorted(_REQUIRED_DRAWING_FIELDS - _explicit_drawing_candidate_fields(result, overrides))


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


def _reconcile_model_validation_with_artifacts(
    report: dict[str, Any],
    generated: list[GeneratedArtifact],
) -> tuple[dict[str, Any], str]:
    """Generic-recipe counterpart of the legacy bracket delivery gate."""

    step = next((item for item in generated if item.format == "step"), None)
    primary = step or (generated[0] if generated else None)
    step_production = bool(
        step and step.production_ready and step.engine == "cadquery-occt"
    )
    production_ready = bool(report.get("productionReady") and step_production)
    try:
        from .geometry_acceptance import validate_generated_geometry
        raw_params = report.get("parameters") or {}
        if hasattr(raw_params, "model_dump"):
            raw_params = raw_params.model_dump(mode="json", by_alias=True)
        acceptance = validate_generated_geometry(str(report.get("recipeId") or ""), raw_params, report.get("metrics") or {})
    except Exception:
        acceptance = None
    if acceptance is not None:
        production_ready = production_ready and bool(acceptance["productionReady"])
    if production_ready:
        engine = "cadquery-occt"
        reason = "OCCT STEP artifact passed the production delivery gate"
    elif primary is not None:
        engine = primary.engine
        reason = (
            "no STEP artifact was requested; emitted artifacts are preview-only"
            if step is None
            else "STEP artifact is preview-only; production B-Rep is unavailable"
        )
    else:
        engine = str(report.get("engine") or "no-artifact")
        reason = "no geometry artifact was emitted"
    reconciled = dict(report)
    metrics = dict(reconciled.get("metrics") or {})
    if acceptance is not None:
        metrics["dimensionalAcceptance"] = acceptance
    metrics.update({
        "artifactProductionReady": production_ready,
        "stepArtifactProductionReady": step_production,
        "artifactEngine": engine,
        "productionReadyReason": reason,
        "previewOnly": not production_ready,
    })
    reconciled["productionReady"] = production_ready
    reconciled["engine"] = engine
    reconciled["metrics"] = metrics
    return reconciled, engine


@app.get("/health", tags=["system"])
@app.get("/api/health", tags=["system"])
@app.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, Any]:
    cq = cadquery_status()
    try:
        from .dwg_preprocessor import dwg_preprocessor_status

        dwg = dwg_preprocessor_status()
    except Exception as exc:  # pragma: no cover - defensive health isolation
        dwg = {
            "available": False,
            "engine": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "rawDwgSentToAI": False,
        }
    try:
        from .pdf_preprocessor import pdf_preprocessor_status
        pdf = pdf_preprocessor_status()
    except Exception as exc:
        pdf = {"available": False, "engine": "unavailable", "error": f"{type(exc).__name__}: {exc}", "rawPdfSentToAI": False}
    return {
        "status": "ok" if cq["available"] else "degraded",
        "service": "joyniu-cad-api",
        "version": __version__,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "geometry": cq,
        "dwg": dwg,
        "pdf": pdf,
        "capabilities": {
            "drawingRecognition": True,
            "drawingResultSubmission": True,
            "bracketValidation": True,
            "stepExport": True,
            "glbExport": True,
            "cadqueryBRep": bool(cq["available"]),
            "dwgVectorParsing": bool(dwg.get("available")),
            "pdfVectorParsing": bool(pdf.get("available")),
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

@app.post("/api/pdf/inspect", tags=["drawings"])
@app.post("/api/v1/pdf/inspect", tags=["drawings"], include_in_schema=False)
async def inspect_pdf(file: UploadFile = File(..., description="PDF drawing")) -> dict[str, Any]:
    from .pdf_preprocessor import PDFInputError, PDFInputTooLargeError, PDFPasswordError, PDFPreprocessError, preprocess_pdf
    data = await file.read()
    try:
        result = preprocess_pdf(data, file.filename or "drawing.pdf")
    except PDFInputTooLargeError as exc: raise HTTPException(status_code=413, detail=exc.to_dict()) from exc
    except PDFPasswordError as exc: raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except PDFInputError as exc: raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except PDFPreprocessError as exc: raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    return {"status":"parsed", "source":result.original_metadata, "pageCount":result.summary["pageCount"], "renderedPageCount":result.page_count_rendered, "omittedPageCount":result.page_count_omitted, "vectorSummary":result.summary, "derived":{"previewSizeBytes":len(result.png_bytes),"previewSha256":hashlib.sha256(result.png_bytes).hexdigest(),"previewContentType":"image/png"}, "rawPdfSentToAI":False}


@app.post("/api/dwg/inspect", tags=["drawings"])
@app.post("/api/v1/dwg/inspect", tags=["drawings"], include_in_schema=False)
async def inspect_dwg(file: UploadFile = File(..., description="Binary DWG drawing")) -> dict[str, Any]:
    """Decode one DWG locally and return bounded native vector evidence.

    The original binary never leaves this process.  The same preprocessor is
    used by the AI conversation path before it sends the derived PNG and safe
    numeric summary to the configured model provider.
    """

    from .dwg_preprocessor import (
        DWGConversionTimeoutError,
        DWGConverterUnavailableError,
        DWGInputError,
        DWGInputTooLargeError,
        DWGParserUnavailableError,
        DWGPreprocessError,
        preprocess_dwg,
    )

    data = await file.read(MAX_DRAWING_BYTES + 1)
    if len(data) > MAX_DRAWING_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "errorType": "dwg_input_too_large",
                "message": f"DWG exceeds {MAX_DRAWING_BYTES} byte upload limit",
            },
        )
    try:
        result = preprocess_dwg(data, file.filename or "drawing.dwg")
    except DWGInputTooLargeError as exc:
        raise HTTPException(status_code=413, detail=exc.to_dict()) from exc
    except DWGInputError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except (DWGConverterUnavailableError, DWGParserUnavailableError) as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except DWGConversionTimeoutError as exc:
        raise HTTPException(status_code=504, detail=exc.to_dict()) from exc
    except DWGPreprocessError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    metadata = result.original_metadata.to_dict()
    return {
        "status": "parsed",
        "source": metadata,
        "converter": result.converter,
        "units": result.summary.get("units"),
        "sourceEntityCount": result.summary.get("sourceEntityCount", 0),
        "entityCount": result.summary.get("entityCount", 0),
        "dimensionCount": len(result.summary.get("dimensions") or []),
        "vectorSummary": result.summary,
        "derived": {
            "dxfSizeBytes": len(result.dxf_bytes),
            "dxfSha256": hashlib.sha256(result.dxf_bytes).hexdigest(),
            "previewSizeBytes": len(result.png_bytes),
            "previewSha256": hashlib.sha256(result.png_bytes).hexdigest(),
            "previewContentType": "image/png",
        },
        "rawDwgSentToAI": False,
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
        # This compatibility endpoint has no authenticated customer/designer
        # context.
        # Never trust a client-supplied ``confirmed`` bit: otherwise an
        # unauthenticated caller could inject arbitrary parameters and attach
        # them to a production geometry request.  Confirmation is performed by
        # an explicit customer/designer acceptance route instead.
        status="needs_review",
        partType="bracket",
        sourceFilename=submission.source_filename,
        sourceSha256=submission.source_sha256,
        confidence=submission.confidence,
        parameters=submission.parameters,
        evidence=submission.evidence,
        ocrText=submission.ocr_text,
        warnings=[
            "External recognition is untrusted; explicit customer/designer confirmation (reviewer confirmation in formal release workflows) is required before geometry generation."
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
    """Confirm a compatibility recognition through an authenticated actor.

    The public upload/result endpoints intentionally do not trust a client
    ``confirmed`` flag.  This route is the bridge for clients that use the
    lightweight ``/drawings/recognize`` API. The legacy ``/confirm`` route
    remains formal-review protected; the customer-facing ``/accept`` route
    records a local customer/designer acknowledgement. Optional parameter
    overrides are validated before the result can be used as ``sourceDrawingId``
    for geometry generation.
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
    if result.status != "confirmed":
        missing = _legacy_candidate_missing(result, raw_overrides)
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "drawing candidate is incomplete; provide explicit parameterOverrides",
                    "missingFields": missing,
                    "hint": "Supply every required bracket field, then confirm again.",
                },
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
    "/api/drawings/{drawing_id}/accept",
    response_model=DrawingRecognition,
    tags=["drawings"],
)
@app.post(
    "/api/v1/drawings/{drawing_id}/accept",
    response_model=DrawingRecognition,
    include_in_schema=False,
)
async def accept_drawing_result(
    drawing_id: str,
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    authorization: str | None = Header(default=None),
) -> DrawingRecognition:
    """Customer/designer acceptance bridge for legacy drawing IDs.

    Token users need either OCR execution (designer) or document review
    permission.  Tokenless acceptance is restricted to loopback development
    environments and is recorded as a customer decision.
    """

    actor_id = "anonymous"
    confirmation_type = "customer"
    if authorization:
        if platform_services is None:
            raise HTTPException(status_code=503, detail="platform authentication service is unavailable")
        try:
            from .platform import Permission, PlatformError
            from .platform_api import _token_user

            actor = _token_user(platform_services, authorization)
            if platform_services.auth.has_permission(actor, Permission.DOCUMENT_REVIEW):
                confirmation_type = "reviewer"
            elif platform_services.auth.has_permission(actor, Permission.OCR_RUN):
                confirmation_type = "designer"
            else:
                raise HTTPException(status_code=403, detail="ocr:run or document:review permission is required")
            actor_id = actor.id
        except PlatformError as exc:
            from .platform_api import _domain_http_exception

            raise _domain_http_exception(exc)
    else:
        from .platform_api import _anonymous_drawing_accept_allowed

        if not _anonymous_drawing_accept_allowed(request):
            raise HTTPException(status_code=401, detail="bearer token is required")

    result = _drawings.get(drawing_id)
    # The platform router is mounted after this legacy route, so bridge
    # platform OCR ids here as well when the app exposes both route sets.
    if result is None and platform_services is not None:
        platform_result = getattr(platform_services, "recognitions", {}).get(drawing_id)
        if platform_result is not None:
            data = body if isinstance(body, dict) else {}
            raw_overrides = data.get("parameterOverrides", data.get("parameter_overrides", {}))
            if raw_overrides is None:
                raw_overrides = {}
            if not isinstance(raw_overrides, dict):
                raise HTTPException(status_code=422, detail="parameterOverrides must be an object")
            try:
                accepted = platform_services.ocr.confirm(
                    platform_result,
                    reviewer_id=actor_id,
                    parameter_overrides=raw_overrides,
                    confirmation_type=confirmation_type,
                )
            except Exception as exc:
                from .platform_api import _domain_http_exception

                raise _domain_http_exception(exc)
            platform_services.recognitions[drawing_id] = accepted
            return JSONResponse(content=accepted.to_dict())
    if result is None:
        raise HTTPException(status_code=404, detail="drawing recognition not found")
    if result.status == "rejected":
        raise HTTPException(status_code=409, detail="rejected drawing cannot be accepted")
    data = body if isinstance(body, dict) else {}
    raw_overrides = data.get("parameterOverrides", data.get("parameter_overrides", {}))
    if raw_overrides is None:
        raw_overrides = {}
    if not isinstance(raw_overrides, dict):
        raise HTTPException(status_code=422, detail="parameterOverrides must be an object")
    allowed = set(BracketParameters.model_fields)
    allowed.update(field.alias for field in BracketParameters.model_fields.values() if getattr(field, "alias", None))
    unknown = sorted(str(key) for key in raw_overrides if str(key) not in allowed)
    if unknown:
        raise HTTPException(status_code=422, detail="unknown parameter override(s): " + ", ".join(unknown))
    if result.status != "confirmed":
        missing = _legacy_candidate_missing(result, raw_overrides)
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "drawing candidate is incomplete; provide explicit parameterOverrides",
                    "missingFields": missing,
                    "hint": "Supply every required bracket field, then accept again.",
                },
            )
    parameters = result.parameters
    if raw_overrides:
        candidate = parameters.model_dump(by_alias=True)
        candidate.update(raw_overrides)
        try:
            parameters = BracketParameters.model_validate(candidate)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
    report = validate_bracket(parameters)
    if not report.valid:
        raise _validation_error(report)
    confirmed_at = datetime.now(timezone.utc)
    recipe = dict(result.model_recipe or {})
    recipe["parameters"] = parameters.model_dump(by_alias=True)
    recipe["confirmationType"] = confirmation_type
    recipe["confirmedBy"] = actor_id
    recipe["confirmedAt"] = confirmed_at.isoformat()
    warnings = list(result.warnings or [])
    warning = "Customer confirmation recorded; geometry generation may proceed."
    if warning not in warnings:
        warnings.append(warning)
    confirmed = result.model_copy(
        update={
            "status": "confirmed",
            "parameters": parameters,
            "validation": report,
            "model_recipe": recipe,
            "review_required": False,
            "warnings": warnings,
            "confirmation_type": confirmation_type,
            "confirmed_by": actor_id,
            "confirmed_at": confirmed_at,
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
            platform_registry = getattr(platform_services, "recognitions", {})
            platform_drawing = platform_registry.get(request.source_drawing_id)
            if platform_drawing is not None:
                drawing = platform_drawing
        if drawing is None:
            raise HTTPException(status_code=404, detail="source drawing recognition not found")
        # ``confirmed`` is an informational client hint, not an authority
        # boundary. A caller must use an explicit customer/designer acceptance
        # (or the formal reviewer route) before a needs_review drawing can reach
        # geometry generation.
        if drawing.status != "confirmed":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "drawing evidence requires explicit human confirmation before generation",
                    "drawingId": drawing.id,
                    "status": drawing.status,
                    "hint": "POST /api/v1/drawings/{drawingId}/accept with edited parameterOverrides",
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


@app.post("/api/models/validate", tags=["geometry"])
@app.post("/api/v1/models/validate", tags=["geometry"], include_in_schema=False)
async def validate_model_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Validate a server-owned recipe without executing model-supplied CAD."""

    try:
        request = ModelGeometryRequest.model_validate(body)
        parameters = parse_model_parameters(request.recipe_id, request.parameters)
        report = validate_model_recipe(request.recipe_id, parameters)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "partType": request.part_type,
        "recipeId": request.recipe_id,
        **report,
    }


@app.post(
    "/api/models/generate",
    response_model=ModelGeometryResponse,
    tags=["geometry"],
)
@app.post(
    "/api/v1/models/generate",
    response_model=ModelGeometryResponse,
    include_in_schema=False,
)
async def generate_model(body: dict[str, Any] = Body(...)) -> ModelGeometryResponse:
    """Generate STEP/GLB by dispatching a validated, allow-listed recipe."""

    try:
        request = ModelGeometryRequest.model_validate(body)
        parameters = parse_model_parameters(request.recipe_id, request.parameters)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if request.source_drawing_id:
        drawing = _drawings.get(request.source_drawing_id)
        if drawing is None and platform_services is not None:
            drawing = getattr(platform_services, "recognitions", {}).get(request.source_drawing_id)
        if drawing is None:
            raise HTTPException(status_code=404, detail="source drawing recognition not found")
        if getattr(drawing, "status", None) != "confirmed":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "drawing evidence requires explicit human confirmation before generation",
                    "drawingId": request.source_drawing_id,
                    "status": getattr(drawing, "status", "unknown"),
                },
            )
        recipe = getattr(drawing, "model_recipe", {}) or {}
        recorded_recipe_id = recipe.get("recipeId", recipe.get("recipe_id")) if isinstance(recipe, Mapping) else None
        if recorded_recipe_id and recorded_recipe_id != request.recipe_id:
            raise HTTPException(status_code=422, detail="confirmed drawing recipeId does not match request")
        recorded_parameters = recipe.get("parameters") if isinstance(recipe, Mapping) else None
        if isinstance(recorded_parameters, Mapping) and recorded_parameters:
            try:
                confirmed = parse_model_parameters(request.recipe_id, recorded_parameters)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="confirmed drawing recipe is invalid") from exc
            current_payload = parameters.model_dump(mode="json", by_alias=True)
            confirmed_payload = confirmed.model_dump(mode="json", by_alias=True)
            if current_payload != confirmed_payload:
                raise HTTPException(
                    status_code=409,
                    detail="requested parameters differ from the confirmed drawing recipe",
                )

    report = validate_model_recipe(request.recipe_id, parameters)
    if not report.get("valid"):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Geometry validation failed; no artifact was generated.",
                "validation": report,
            },
        )
    try:
        generated = generate_model_recipe_artifacts(
            request.recipe_id,
            parameters,
            request.formats,
            require_cadquery=request.require_cadquery,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    report["recipeId"] = request.recipe_id
    report, response_engine = _reconcile_model_validation_with_artifacts(report, generated)

    request_id = f"geo_{uuid4().hex[:16]}"
    descriptors: list[ArtifactDescriptor] = []
    for artifact in generated:
        artifact_id = f"art_{uuid4().hex[:16]}"
        filename = f"joyniu-{request.recipe_id}-{request_id}.{artifact.format}"
        digest = hashlib.sha256(artifact.data).hexdigest()
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
            "sha256": digest,
            "part_type": request.part_type,
            "recipe_id": request.recipe_id,
        }
        descriptors.append(ArtifactDescriptor(
            id=artifact_id,
            format=artifact.format,
            filename=filename,
            mediaType=MEDIA_TYPES[artifact.format],
            sizeBytes=len(artifact.data),
            sha256=digest,
            downloadUrl=f"/api/artifacts/{artifact_id}.{artifact.format}",
            engine=artifact.engine,
            productionReady=artifact.production_ready,
            warnings=list(artifact.warnings),
        ))
    response = ModelGeometryResponse(
        requestId=request_id,
        status="completed",
        partType=request.part_type,
        recipeId=request.recipe_id,
        engine=response_engine,
        parameters=parameters.model_dump(mode="json", by_alias=True),
        validation=report,
        artifacts=descriptors,
        sourceDrawingId=request.source_drawing_id,
    )
    _model_requests[request_id] = response
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
        "Content-Disposition": attachment_content_disposition(descriptor.filename),
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
        "Content-Disposition": attachment_content_disposition(record["filename"]),
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
