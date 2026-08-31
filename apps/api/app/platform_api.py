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
import asyncio
import hashlib
import ipaddress
import json
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

# FastAPI evaluates postponed annotations against the defining module's
# globals.  ``Request`` used to be imported only inside ``create_platform_router``
# which made ``app.openapi()`` fail with an unresolved ``ForwardRef('Request')``
# on Pydantic v2.  Keep the dependency optional for domain-only consumers while
# exposing a module-level symbol whenever FastAPI is installed.
try:  # pragma: no cover - the fallback is exercised on dependency-free hosts
    from fastapi import Request as _FastAPIRequest, UploadFile as _FastAPIUploadFile
except ImportError:  # pragma: no cover
    _FastAPIRequest = Any  # type: ignore[assignment]
    _FastAPIUploadFile = Any  # type: ignore[assignment]
Request = _FastAPIRequest
UploadFile = _FastAPIUploadFile

from .cam import (
    CAMConflictError,
    CAMGateRejected,
    CAMNotFoundError,
    CAMService,
    StockDefinition,
)
from .ai_proxy import AIFile, AIProxy, AIProxyError, MAX_FILE_BYTES, PARAMETER_FIELDS
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


# The compatibility upload recognizer intentionally returns a canonical
# bracket recipe so older browser clients can keep rendering a preview.  In
# the platform conversation path that recipe is only a visual scaffold: it
# must never be copied into the durable AI candidate or used to satisfy the
# confirmation gate.  Keep the marker list local to this adapter so the
# platform OCR record remains evidence-first without changing the legacy
# response contract.
_COMPATIBILITY_FALLBACK_ENGINES = frozenset(
    {"heuristic-review", "tesseract-compatible", "compatibility-recognizer"}
)


def _is_compatibility_fallback(recognition: Any) -> bool:
    engine = str(getattr(recognition, "engine", "") or "").casefold()
    recipe = getattr(recognition, "model_recipe", {})
    source = recipe.get("source", "") if isinstance(recipe, Mapping) else ""
    return engine in _COMPATIBILITY_FALLBACK_ENGINES or str(source).casefold() == "compatibility-recognizer"


@dataclass(slots=True)
class PlatformServices:
    """Dependency container shared by HTTP handlers and background workers."""

    pdm: PDMRepository
    auth: AuthService
    ocr: OCRService
    cam: CAMService
    ai: AIProxy
    recognitions: dict[str, DrawingRecognition] = field(default_factory=dict)

    def close(self) -> None:
        """Close all persistent components owned by this service graph."""

        self.cam.close()
        self.pdm.close()
        self.auth.close()


def build_platform_services(
    database: str | Path = ":memory:",
    *,
    auth_secret: str | bytes | None = None,
    token_ttl_seconds: int = 3600,
    enable_live_ocr: bool | None = None,
    allow_unverified_fixture: bool | None = False,
) -> PlatformServices:
    """Build a local service graph.

    ``database`` is shared by PDM and accounts when it is a filesystem path.
    For tests, pass ``":memory:"`` (the default).  A CAM plan and OCR result
    CAM plans, simulations and NC programs use the same SQLite path through a
    transactional CAM snapshot row, so they survive process restarts.  Passing
    ``database=":memory:"`` keeps that snapshot ephemeral for tests.
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
        authorizer=lambda actor_id, permission: auth.has_permission(actor_id, permission),
        # CAM uses the same SQLite file as PDM/RBAC.  Its snapshot row is
        # independent of the PDM schema and is restored automatically on a
        # process restart, keeping plans/simulations/NC auditable.
        database=db_value,
    )
    return PlatformServices(
        pdm=PDMRepository(db_value),
        auth=auth,
        ocr=OCRService(
            enable_live_ocr=enable_live_ocr,
            # This is deliberately false for API/service-graph defaults.  A
            # fixture id may only select a result when its source SHA matches;
            # fixture-only smoke tests can opt in explicitly.
            allow_unverified_fixture=allow_unverified_fixture,
        ),
        cam=cam,
        ai=AIProxy(),
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
    if isinstance(exc, RuntimeError):
        # Optional kernel/exporter failures are an unavailable capability, not
        # an application crash.  Keep the detail concise so clients can offer
        # the documented fallback/installation guidance.
        return HTTPException(status_code=503, detail=str(exc))
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


def _require_any_permission(
    services: PlatformServices,
    actor: Any,
    *permissions: Permission,
) -> Any:
    """Authorize read-only views that are shared by several CAM roles."""

    if not any(services.auth.has_permission(actor, permission) for permission in permissions):
        values = ", ".join(permission.value for permission in permissions)
        raise AuthorizationError(f"one of these permissions is required: {values}")
    return actor


def _actor_roles(actor: Any) -> set[str]:
    """Return normalized role names for both domain enums and JSON values."""

    values: set[str] = set()
    for raw in getattr(actor, "roles", ()) or ():
        value = getattr(raw, "value", raw)
        values.add(str(value).strip().casefold())
    return values


def _is_admin_actor(services: PlatformServices, actor: Any) -> bool:
    """Whether an actor has global administration rights."""

    if Role.ADMIN.value in _actor_roles(actor):
        return True
    try:
        return services.auth.has_permission(actor, Permission.USER_MANAGE)
    except PlatformError:
        return False


def _anonymous_ai_request_allowed(request: Any) -> bool:
    """Allow guest AI only for loopback or an explicit development env."""

    host = str(getattr(getattr(request, "client", None), "host", "") or "").strip().casefold()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in {"localhost", "testclient"}
    environment = os.environ.get("JOYNIU_ENV", "").strip().casefold()
    return loopback or environment in {"development", "dev", "local", "test"}


def _anonymous_drawing_accept_allowed(request: Any) -> bool:
    """Allow customer acceptance without a token only on local dev hosts."""

    host = str(getattr(getattr(request, "client", None), "host", "") or "").strip().casefold()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in {"localhost", "testclient"}
    environment = os.environ.get("JOYNIU_ENV", "").strip().casefold()
    return loopback and environment in {"development", "dev", "local"}


def _member_identity(entry: Any) -> tuple[str | None, str]:
    """Extract an id/email and access level from a metadata member entry.

    Supported forms are intentionally permissive for migration compatibility:
    ``"usr_123"``, ``{"userId": "usr_123", "access": "read"}``, and
    ``{"email": "person@example.com", "role": "designer"}``.  A bare
    string or mapping without an access hint grants read/write project access;
    explicit ``read``/``viewer``/``readonly`` values are read-only.
    """

    if isinstance(entry, str):
        return entry.strip() or None, "write"
    if not isinstance(entry, Mapping):
        return None, "read"
    # A member mapping can use either camelCase or snake_case keys.  Keep the
    # first non-empty identity so callers may include both id and email.
    identity: str | None = None
    for key in (
        "userId",
        "userID",
        "user_id",
        "memberId",
        "member_id",
        "actorId",
        "actor_id",
        "id",
        "email",
        "user",
        "member",
        "principal",
    ):
        raw = entry.get(key)
        if key in {"user", "member", "principal"} and isinstance(raw, Mapping):
            raw = next(
                (
                    raw.get(candidate)
                    for candidate in ("id", "userId", "user_id", "email")
                    if raw.get(candidate) is not None
                ),
                None,
            )
        if raw is not None and str(raw).strip():
            identity = str(raw).strip()
            break
    access_raw: Any = entry.get("access", entry.get("accessLevel", entry.get("access_level")))
    if access_raw is None:
        access_raw = entry.get(
            "permission",
            entry.get("permissions", entry.get("role", "write")),
        )
    if isinstance(access_raw, (list, tuple, set, frozenset)):
        access_values = {str(item).strip().casefold() for item in access_raw}
    else:
        access_values = {str(access_raw).strip().casefold()}
    read_only_values = {
        "read",
        "reader",
        "view",
        "viewer",
        "readonly",
        "read-only",
        "read_only",
        "ro",
    }
    level = "read" if access_values and access_values.issubset(read_only_values) else "write"
    return identity, level


def _iter_project_members(project: Any) -> Iterable[Any]:
    metadata = getattr(project, "metadata", {})
    if not isinstance(metadata, Mapping):
        return ()
    members = metadata.get("members", ())
    if isinstance(members, Mapping):
        # Also accept a compact ``{"usr_a": "read", "usr_b": "write"}``
        # representation and normalize it to the canonical list shape for
        # access checks.
        return tuple(
            {"userId": identity, "access": access}
            for identity, access in members.items()
        )
    if isinstance(members, str):
        return (members,)
    if isinstance(members, (list, tuple, set, frozenset)):
        return tuple(members)
    return ()


def _project_member_matches(project: Any, actor: Any, *, write: bool = False) -> bool:
    actor_id = str(getattr(actor, "id", "")).strip()
    actor_email = str(getattr(actor, "email", "")).strip().casefold()
    for entry in _iter_project_members(project):
        identity, level = _member_identity(entry)
        if not identity:
            continue
        normalized = identity.casefold()
        if normalized not in {actor_id.casefold(), actor_email}:
            continue
        if not write or level == "write":
            return True
    return False


def _can_access_project(
    services: PlatformServices,
    actor: Any,
    project: Any,
    *,
    write: bool = False,
) -> bool:
    """Evaluate project scope independently of global RBAC permissions."""

    if _is_admin_actor(services, actor):
        return True
    owner_identity = str(project.owner_id).strip().casefold()
    if owner_identity in {
        str(getattr(actor, "id", "")).strip().casefold(),
        str(getattr(actor, "email", "")).strip().casefold(),
    }:
        return True
    return _project_member_matches(project, actor, write=write)


def _require_project_access(
    services: PlatformServices,
    actor: Any,
    project_id: str,
    *,
    write: bool = False,
) -> Any:
    """Require owner/admin/member access and return the project object."""

    project = services.pdm.get_project(str(project_id))
    if not _can_access_project(services, actor, project, write=write):
        mode = "write" if write else "read"
        raise AuthorizationError(f"actor is not authorized for {mode} access to project: {project.id}")
    return project


def _require_project_owner_or_admin(
    services: PlatformServices,
    actor: Any,
    project: Any,
) -> Any:
    owner_identity = str(project.owner_id).strip().casefold()
    actor_id = str(getattr(actor, "id", "")).strip().casefold()
    actor_email = str(getattr(actor, "email", "")).strip().casefold()
    if _is_admin_actor(services, actor) or owner_identity in {actor_id, actor_email}:
        return project
    raise AuthorizationError("only the project owner or an admin may manage members")


def _resolve_document_project(
    services: PlatformServices,
    document_id: str,
    *,
    include_deleted: bool = False,
) -> tuple[Any, Any]:
    document = services.pdm.get_document(str(document_id), include_deleted=include_deleted)
    project = services.pdm.get_project(document.project_id)
    return document, project


def _require_document_access(
    services: PlatformServices,
    actor: Any,
    document_id: str,
    *,
    write: bool = False,
    include_deleted: bool = False,
) -> tuple[Any, Any]:
    document, project = _resolve_document_project(
        services, document_id, include_deleted=include_deleted
    )
    _require_project_access(services, actor, project.id, write=write)
    return document, project


def _require_version_access(
    services: PlatformServices,
    actor: Any,
    version_id: str,
    *,
    write: bool = False,
) -> tuple[Any, Any, Any]:
    version = services.pdm.get_version(str(version_id))
    document, project = _require_document_access(
        services,
        actor,
        version.document_id,
        write=write,
        include_deleted=True,
    )
    return version, document, project


def _require_cam_plan_access(
    services: PlatformServices,
    actor: Any,
    plan_id: str,
    *,
    write: bool = False,
) -> Any:
    """Apply project scope to a CAM plan before invoking CAMService."""

    plan = services.cam.get_plan(str(plan_id))
    if plan.project_id:
        _require_project_access(services, actor, plan.project_id, write=write)
        return plan
    # Legacy/unscoped plans remain inspectable by CAM participants so existing
    # local workflows continue to work.  Mutations, however, stay with the
    # creator (or an admin) because there is no project membership to consult.
    if write and not (
        _is_admin_actor(services, actor)
        or str(plan.created_by) == str(getattr(actor, "id", ""))
    ):
        raise AuthorizationError("only the CAM plan creator or an admin may modify an unscoped plan")
    return plan


def _validate_members_payload(raw: Any) -> list[Any]:
    """Validate and copy a project ``metadata.members`` value."""

    if isinstance(raw, Mapping):
        raw = [{"userId": key, "access": value} for key, value in raw.items()]
    elif isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValidationError("members must be an array or identity-to-access object")
    result: list[Any] = []
    seen: set[str] = set()
    for entry in raw:
        identity, _level = _member_identity(entry)
        if not identity:
            raise ValidationError("each project member needs a userId or email")
        key = identity.casefold()
        if key in seen:
            raise ValidationError(f"duplicate project member: {identity}")
        seen.add(key)
        if isinstance(entry, Mapping):
            # JSON round-tripping through dict() strips custom Mapping classes
            # while retaining any caller-defined metadata fields.
            result.append(dict(entry))
        else:
            result.append(identity)
    return result


def create_platform_router(services: PlatformServices, *, prefix: str = ""):
    """Return an ``APIRouter`` with auth, PDM, OCR and CAM endpoints.

    The import is lazy so service users do not need FastAPI installed.  Routes
    use untyped dictionaries intentionally: the domain dataclasses are the
    stable contract, while this keeps the adapter compatible with both Pydantic
    v1 and v2 environments used by deployments.
    """

    try:
        from fastapi import APIRouter, Body, File, Form, Header, HTTPException, UploadFile
        from fastapi.responses import JSONResponse, PlainTextResponse
    except ImportError as exc:  # pragma: no cover - exercised in dependency-free CI
        raise RuntimeError(
            "FastAPI is optional for the domain services; install apps/api requirements to create routes"
        ) from exc

    router = APIRouter(prefix=prefix, tags=["platform"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "joyniu-platform", "features": ["pdm", "rbac", "ocr", "cam", "ai-chat"]}

    @router.get("/ai/status")
    async def ai_status() -> dict[str, Any]:
        """Return non-secret provider capability information for the UI."""

        return services.ai.status()

    @router.post("/ai/conversation")
    async def ai_conversation(
        request: Request,
        message: str = Form(default=""),
        previous_response_id: str | None = Form(default=None),
        previous_response_id_alias: str | None = Form(default=None, alias="previousResponseId"),
        model_state_json: str | None = Form(default=None),
        model_state_alias: str | None = Form(default=None, alias="modelState"),
        file: UploadFile | None = File(default=None),
        files: list[UploadFile] | None = File(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Proxy a parameter-editing conversation without exposing the key.

        Attachments are passed to the provider as data URLs only after the
        caller has authenticated and been granted ``ai:chat`` (or when the
        explicit local anonymous flag is enabled). The result is reduced to a
        validated parameter patch plus non-secret provider/attachment status;
        raw provider payloads and credentials never cross this boundary.
        """

        try:
            # Production deployments require an authenticated designer/admin.
            # A deliberately explicit local flag lets a customer try the
            # workbench before creating an account; it is never enabled by
            # default and does not weaken any PDM/CAM route.
            if authorization:
                actor = _token_user(services, authorization)
                services.auth.require(actor, Permission.AI_CHAT)
            elif not services.ai.allow_anonymous or not _anonymous_ai_request_allowed(request):
                raise AuthenticationError("bearer token is required")
            model_state: Mapping[str, Any] | None = None
            effective_previous_response_id = previous_response_id or previous_response_id_alias
            effective_model_state = model_state_json or model_state_alias
            if effective_model_state:
                try:
                    parsed_state = json.loads(effective_model_state)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("modelState must be valid JSON") from exc
                if not isinstance(parsed_state, Mapping):
                    raise ValidationError("modelState must be a JSON object")
                model_state = dict(parsed_state)
            attachments: list[AIFile] = []
            incoming_files = list(files or [])
            if file is not None:
                incoming_files.insert(0, file)
            if len(incoming_files) > 4:
                raise ValidationError("too many drawing files (maximum 4)")
            for upload in incoming_files:
                filename = Path(upload.filename or "drawing").name
                suffix = Path(filename).suffix.casefold()
                content_type = (upload.content_type or "application/octet-stream").casefold()
                allowed_suffixes = {".pdf", ".dxf", ".dwg"}
                image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                if not content_type.startswith("image/") and suffix not in allowed_suffixes | image_suffixes:
                    raise ValidationError("supported AI drawing files are images, PDF, DXF, and DWG")
                data = await upload.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValidationError("drawing file is too large")
                if not data:
                    raise ValidationError("drawing file is empty")
                attachments.append(AIFile(filename=filename, content_type=content_type, data=data))
            if not message.strip() and not attachments:
                raise ValidationError("message or drawing file is required")
            result = await asyncio.to_thread(
                services.ai.converse,
                message,
                previous_response_id=effective_previous_response_id,
                model_state=model_state,
                files=attachments,
            )
            # The AI adapter may expose a richer compatibility recognition,
            # but geometry generation and explicit human confirmation must use the
            # platform OCR service's own DrawingRecognition object. Register
            # one per uploaded file in the same service graph and return that
            # canonical id so ``sourceDrawingId`` can be resumed safely.
            registered_drawing = None
            canonical_attachments: list[dict[str, Any]] = []
            for attachment in attachments:
                canonical_attachments.append(
                    {
                        "filename": attachment.filename,
                        "contentType": attachment.content_type,
                        "sizeBytes": len(attachment.data),
                        "sha256": hashlib.sha256(attachment.data).hexdigest(),
                    }
                )
                try:
                    candidate = await asyncio.to_thread(
                        services.ocr.analyze,
                        attachment.data,
                        filename=attachment.filename,
                    )
                except Exception:
                    # OCR is an enrichment step after the AI request. A
                    # missing rasterizer/parser must not discard an otherwise
                    # valid conversation response; the attachment metadata
                    # and any review flag from the AI proxy remain available.
                    # If the multimodal model supplied a structured candidate,
                    # register an explicit AI-only recognition so the customer
                    # can still accept/edit it through the normal endpoint.
                    # Empty patches keep the legacy no-recognition response
                    # for compatibility and correctly ask the user to retry.
                    ai_patch = getattr(result, "parameter_patch", {})
                    filtered_patch = {
                        str(key): value
                        for key, value in ai_patch.items()
                        if isinstance(ai_patch, Mapping)
                        and str(key) in PARAMETER_FIELDS
                        and value is not None
                    } if isinstance(ai_patch, Mapping) else {}
                    if not filtered_patch:
                        continue
                    candidate = DrawingRecognition(
                        id=f"drw_ai_{uuid.uuid4().hex[:16]}",
                        status="needs_review",
                        part_type="unknown",
                        source_filename=attachment.filename,
                        source_sha256=hashlib.sha256(attachment.data).hexdigest(),
                        image_width=None,
                        image_height=None,
                        confidence=0.0,
                        dimensions=(),
                        features=(),
                        model_recipe={"parameters": {}, "source": "ai-candidate"},
                        assumptions=("候选字段来自多模态 AI，尚未由 OCR/几何配方确认",),
                        warnings=("OCR 识别不可用；以下为 AI 候选数据，需人工确认",),
                        unresolved=("feature_topology",),
                        ocr_text="",
                        engine="ai-candidate",
                        candidate_parameters=filtered_patch,
                    )
                # Merge the AI's validated parameterPatch into the canonical
                # platform recognition candidate.  Keep the result pending;
                # a patch is a proposal, never an implicit confirmation.  For
                # a wholly unknown drawing, preserve the durable recipe's
                # historical empty ``parameters`` alias but expose the AI
                # proposal as ``candidateParameters`` so the customer can see
                # and edit every value before accepting it.
                patch = getattr(result, "parameter_patch", {})
                candidate_patch: dict[str, Any] = {}
                if isinstance(patch, Mapping):
                    candidate_patch.update(
                        {
                            str(key): value
                            for key, value in patch.items()
                            if str(key) in PARAMETER_FIELDS and value is not None
                        }
                    )
                # A live OCR adapter may have dimensions even when the AI
                # provider returned no parameter patch. Promote those numeric
                # fields to the same editable candidate envelope.
                aliases = {
                    "base_length": "baseLength", "base_width": "baseWidth", "base_thickness": "baseThickness",
                    "upper_length": "upperLength", "upper_width": "upperWidth", "upper_height": "upperHeight",
                    "total_height": "totalHeight", "notch_opening": "notchOpening", "notch_radius": "notchRadius",
                    "slot_length": "slotLength", "slot_width": "slotWidth", "pocket_depth": "pocketDepth",
                    "saddle_depth": "saddleDepth", "hole_depth": "holeDepth", "hole_through": "holeThrough",
                    "boss_diameter": "bossDiameter", "boss_center_distance": "bossCenterDistance", "boss_height": "bossHeight",
                }
                if not candidate_patch:
                    for dimension in candidate.dimensions:
                        raw_field = str(dimension.field)
                        field_name = aliases.get(raw_field, raw_field)
                        # The legacy compatibility recognizer emits a full
                        # canonical profile with ``sourceText`` set to this
                        # marker when OCR found no labelled value.  Those
                        # numbers are a preview scaffold, not AI/OCR
                        # evidence, so do not promote them to a candidate.
                        source_text = str(getattr(dimension, "source_text", "") or "")
                        if source_text.casefold().startswith("canonical-bracket-fallback"):
                            continue
                        if field_name in PARAMETER_FIELDS and dimension.value is not None:
                            candidate_patch[field_name] = dimension.value
                existing_recipe = candidate.model_recipe.get("parameters", {})
                compatibility_fallback = _is_compatibility_fallback(candidate)
                if compatibility_fallback:
                    # Keep recipe metadata for audit/debugging, but strip the
                    # synthetic canonical dimensions from the durable
                    # candidate.  Marking the part unknown forces the strict
                    # all-required-fields check in OCRService.confirm even
                    # when a few OCR labels or AI fields are present.
                    recipe = dict(candidate.model_recipe)
                    recipe["parameters"] = dict(candidate_patch)
                    candidate = replace(
                        candidate,
                        part_type="unknown",
                        model_recipe=recipe,
                        candidate_parameters=dict(candidate_patch),
                    )
                elif candidate.part_type != "unknown" or bool(existing_recipe):
                    recipe = dict(candidate.model_recipe)
                    merged = dict(existing_recipe) if isinstance(existing_recipe, Mapping) else {}
                    merged.update(candidate_patch)
                    recipe["parameters"] = merged
                    candidate = replace(candidate, model_recipe=recipe, candidate_parameters=dict(merged))
                elif candidate_patch:
                    candidate = replace(candidate, candidate_parameters=candidate_patch)
                services.recognitions[candidate.id] = candidate
                if registered_drawing is None:
                    registered_drawing = candidate
            if registered_drawing is not None:
                result = replace(result, drawing=registered_drawing.to_dict())
            elif attachments and getattr(result, "drawing", None) is not None:
                # Never return a compatibility recognition id that is absent
                # from this platform service graph.  Such an id would later
                # produce a misleading 404 when used as sourceDrawingId.
                result = replace(result, drawing=None, needs_review=True)
            # Keep the response metadata authoritative even when an injected
            # provider/test double does not populate its own attachment list.
            if canonical_attachments:
                result = replace(result, attachments=tuple(canonical_attachments))
            return result.to_dict()
        except (PlatformError, AIProxyError) as exc:
            raise _domain_http_exception(exc)

    @router.post("/ai/chat")
    async def ai_chat_alias(
        request: Request,
        message: str = Form(default=""),
        previous_response_id: str | None = Form(default=None),
        previous_response_id_alias: str | None = Form(default=None, alias="previousResponseId"),
        model_state_json: str | None = Form(default=None),
        model_state_alias: str | None = Form(default=None, alias="modelState"),
        file: UploadFile | None = File(default=None),
        files: list[UploadFile] | None = File(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Short alias used by newer clients; accept both file conventions."""
        return await ai_conversation(
            request=request,
            message=message,
            previous_response_id=previous_response_id,
            previous_response_id_alias=previous_response_id_alias,
            model_state_json=model_state_json,
            model_state_alias=model_state_alias,
            file=file,
            files=files,
            authorization=authorization,
        )

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
            if owner_id.casefold() not in {
                str(actor.id).casefold(),
                str(getattr(actor, "email", "")).casefold(),
            }:
                services.auth.require(actor, Permission.USER_MANAGE)
            metadata = data.get("metadata")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, Mapping):
                raise ValidationError("metadata must be an object")
            metadata = dict(metadata)
            if "members" in metadata:
                metadata["members"] = _validate_members_payload(metadata["members"])
            return services.pdm.create_project(
                str(data.get("name", "")),
                owner_id,
                description=str(data.get("description", "")),
                metadata=metadata,
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
            # Query all projects once, then apply the same owner/member policy
            # used by every project-scoped endpoint.  Filtering here prevents a
            # non-member from learning another user's project ids via list.
            projects = services.pdm.list_projects(owner_id=None, include_archived=include_archived)
            if not _is_admin_actor(services, actor):
                projects = [
                    project
                    for project in projects
                    if _can_access_project(services, actor, project, write=False)
                ]
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
            _require_project_access(services, actor, project_id, write=True)
            metadata = data.get("metadata")
            if metadata is not None and not isinstance(metadata, Mapping):
                raise ValidationError("metadata must be an object")
            return services.pdm.create_document(
                project_id,
                str(data.get("name", "")),
                str(data.get("kind", "model")),
                actor.id,
                metadata=metadata if isinstance(metadata, Mapping) else {},
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
            project = _require_project_access(services, actor, project_id, write=True)
            metadata: Mapping[str, Any] | None = None
            if "metadata" in data:
                raw_metadata = data.get("metadata")
                if not isinstance(raw_metadata, Mapping):
                    raise ValidationError("metadata must be an object")
                # PATCH semantics preserve existing project metadata keys while
                # allowing callers to update just ``members`` or one auxiliary
                # field.
                metadata = dict(project.metadata) if isinstance(project.metadata, Mapping) else {}
                metadata.update(dict(raw_metadata))
                if "members" in metadata:
                    # A regular project member may rename/describe a project,
                    # but changing the ACL is reserved for owner/admin.
                    _require_project_owner_or_admin(services, actor, project)
                    metadata["members"] = _validate_members_payload(metadata["members"])
            return services.pdm.rename_project(
                project_id,
                str(data["name"]) if "name" in data else project.name,
                description=(str(data["description"]) if "description" in data else None),
                actor_id=actor.id,
                metadata=metadata,
            ).to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/projects/{project_id}/members")
    async def list_project_members(
        project_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """List the project ACL after applying the normal read-scope check."""

        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.PROJECT_READ)
            project = _require_project_access(services, actor, project_id, write=False)
            metadata = project.metadata if isinstance(project.metadata, Mapping) else {}
            members = metadata.get("members", [])
            if isinstance(members, Mapping):
                members = [
                    {"userId": str(identity), "access": access}
                    for identity, access in members.items()
                ]
            elif isinstance(members, str):
                members = [members]
            elif not isinstance(members, (list, tuple, set, frozenset)):
                members = []
            return {
                "projectId": project.id,
                "ownerId": project.owner_id,
                "members": list(members),
            }
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.put("/pdm/projects/{project_id}/members")
    @router.patch("/pdm/projects/{project_id}/members")
    async def update_project_members(
        project_id: str,
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Replace ``metadata.members`` (owner/admin only).

        Keeping members in project metadata makes this additive to existing
        SQLite databases.  Other metadata keys are preserved by this endpoint.
        """

        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            project = services.pdm.get_project(project_id)
            services.auth.require(actor, Permission.PROJECT_WRITE)
            _require_project_owner_or_admin(services, actor, project)
            raw_members = data.get("members")
            if raw_members is None and isinstance(data.get("metadata"), Mapping):
                raw_members = data["metadata"].get("members", [])
            members = _validate_members_payload(raw_members if raw_members is not None else [])
            metadata = dict(project.metadata) if isinstance(project.metadata, Mapping) else {}
            metadata["members"] = members
            updated = services.pdm.update_project_metadata(
                project.id,
                metadata,
                actor_id=actor.id,
            )
            return {
                "project": updated.to_dict(),
                "projectId": updated.id,
                "ownerId": updated.owner_id,
                "members": members,
            }
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
            _require_project_access(services, actor, project_id, write=False)
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
            _require_document_access(services, actor, document_id, write=True)
            metadata = data.get("metadata")
            if metadata is not None and not isinstance(metadata, Mapping):
                raise ValidationError("metadata must be an object")
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
            _require_project_access(services, actor, project_id, write=False)
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
            _require_document_access(services, actor, document_id, write=True)
            if "contentBase64" in data or "content_base64" in data:
                content: Any = _decode_base64(data.get("contentBase64", data.get("content_base64")))
            elif "contentText" in data or "content_text" in data:
                content = str(data.get("contentText", data.get("content_text")))
            else:
                content = data.get("content", data.get("payload"))
            raw_metadata = data.get("metadata")
            if raw_metadata is not None and not isinstance(raw_metadata, Mapping):
                raise ValidationError("metadata must be an object")
            version = services.pdm.create_version(
                document_id,
                content,
                actor.id,
                file_name=data.get("fileName", data.get("file_name")),
                content_type=str(data.get("contentType", data.get("content_type", "application/octet-stream"))),
                note=str(data.get("note", "")),
                metadata=raw_metadata if isinstance(raw_metadata, Mapping) else {},
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
            _require_document_access(services, actor, document_id, write=False, include_deleted=True)
            return {"items": [item.to_dict() for item in services.pdm.list_versions(document_id)]}
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/pdm/versions/{version_id}/content")
    async def get_version_content(version_id: str, authorization: str | None = Header(default=None)):
        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.DOCUMENT_READ)
            version, _document, _project = _require_version_access(services, actor, version_id, write=False)
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
            normalized_status = requested_status.strip().casefold()
            permission = Permission.DOCUMENT_RELEASE if normalized_status == "released" else Permission.DOCUMENT_REVIEW
            services.auth.require(actor, permission)
            # Review/release is governed by the dedicated document
            # permission; project read membership is sufficient so a reviewer
            # role does not need the designer's PROJECT_WRITE permission.
            _require_document_access(services, actor, document_id, write=False)
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
            _require_document_access(services, actor, document_id, write=True)
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
            _require_document_access(
                services,
                actor,
                document_id,
                write=True,
                include_deleted=True,
            )
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
                confirmation_type="reviewer",
            )
            services.recognitions[recognition_id] = result
            return result.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/drawings/{drawing_id}/accept")
    async def accept_drawing(
        drawing_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Accept an OCR/AI candidate after an explicit human decision.

        Designers with ``ocr:run`` and reviewers with ``document:review`` may
        accept using a bearer token.  Tokenless acceptance is deliberately
        limited to loopback requests in an explicit development environment;
        it is recorded as a customer confirmation and never grants any other
        platform permission.
        """

        actor_id = "anonymous"
        confirmation_type = "customer"
        if authorization:
            actor = _token_user(services, authorization)
            if services.auth.has_permission(actor, Permission.DOCUMENT_REVIEW):
                confirmation_type = "reviewer"
            elif services.auth.has_permission(actor, Permission.OCR_RUN):
                confirmation_type = "designer"
            else:
                raise _domain_http_exception(
                    AuthorizationError("ocr:run or document:review permission is required")
                )
            actor_id = actor.id
        elif not _anonymous_drawing_accept_allowed(request):
            raise _domain_http_exception(AuthenticationError("bearer token is required"))

        try:
            recognition = services.recognitions.get(str(drawing_id))
            if recognition is None:
                raise NotFoundError(f"drawing recognition not found: {drawing_id}")
            data = _require_dict(payload)
            overrides = data.get("parameterOverrides", data.get("parameter_overrides", {}))
            if overrides is None:
                overrides = {}
            if not isinstance(overrides, Mapping):
                raise ValidationError("parameterOverrides must be an object")
            result = services.ocr.confirm(
                recognition,
                reviewer_id=actor_id,
                parameter_overrides=overrides,
                confirmation_type=confirmation_type,
            )
            services.recognitions[recognition.id] = result
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
        formal reviewer can approve a recognition in the same request; an
        unconfirmed drawing returns HTTP 409 with its complete AI candidate and
        the ``/drawings/{id}/accept`` continuation path, and creates no model
        versions yet. Every accepted blob is stored as an immutable PDM
        version, so the returned manifest can be inspected after confirmation.
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
            recognition_id_raw = data.get("recognitionId", data.get("recognition_id"))
            recognition_id = str(recognition_id_raw).strip() if recognition_id_raw else ""
            if recognition_id:
                # A reviewer confirms the recognition in a separate request;
                # the designer can then resume this workflow with the same
                # source image.  Requiring the bytes again keeps the durable
                # PDM source version anchored to the exact reviewed hash and
                # prevents reusing a confirmed result for another drawing.
                recognition = services.recognitions.get(recognition_id)
                if recognition is None:
                    raise NotFoundError(f"drawing recognition not found: {recognition_id}")
                source_sha256 = hashlib.sha256(image_bytes).hexdigest()
                if source_sha256 != recognition.source_sha256:
                    raise ConflictError(
                        "imageBase64 does not match the source image used for this recognition"
                    )
            else:
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
                            "message": "drawing evidence has been analyzed and is waiting for explicit human confirmation",
                            "next": {
                                "action": "acceptDrawing",
                                "path": f"/drawings/{recognition.id}/accept",
                            },
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
                # Existing projects are shared through the same owner/member
                # policy as every PDM endpoint; a member with write access may
                # run the workflow without global user-management rights.
                project = _require_project_access(services, actor, str(project_id), write=True)
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
            # Keep the complete recognition/evidence envelope in document and
            # version metadata.  The in-memory ``recognitions`` index is only
            # a fast hand-off cache; this metadata is the durable audit record
            # that survives a service restart and still ties every generated
            # artifact back to the exact source hash and reviewer decision.
            recognition_audit = recognition.to_dict()
            source_document = services.pdm.create_document(
                project.id,
                filename,
                "drawing",
                actor.id,
                metadata={
                    "sha256": recognition.source_sha256,
                    "recognitionId": recognition.id,
                    "recognition": recognition_audit,
                },
            )
            source_version = services.pdm.create_version(
                source_document.id,
                image_bytes,
                actor.id,
                file_name=filename,
                content_type=str(data.get("contentType", "application/octet-stream")),
                metadata={
                    "recognitionId": recognition.id,
                    "sourceSha256": recognition.source_sha256,
                    "recognition": recognition_audit,
                },
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
            # Validation audits the requested shape before the exporters run.
            # Reconcile the workflow-level report with the actual STEP
            # artifact as well, otherwise an exporter fallback could leave a
            # durable PDM manifest claiming that a preview is production CAD.
            step_artifact = next(
                (item for item in generated if item.format == "step"), None
            )
            primary_artifact = step_artifact or (generated[0] if generated else None)
            step_production = bool(
                step_artifact
                and step_artifact.production_ready
                and step_artifact.engine == "cadquery-occt"
            )
            workflow_production = bool(report.production_ready and step_production)
            workflow_engine = (
                "cadquery-occt"
                if workflow_production
                else primary_artifact.engine
                if primary_artifact is not None
                else report.engine
            )
            workflow_reason = (
                "OCCT STEP artifact passed the production delivery gate"
                if workflow_production
                else "STEP artifact is preview-only; production B-Rep is unavailable"
                if step_artifact is not None
                else "no STEP artifact was requested; emitted artifacts are preview-only"
            )
            report_metrics = dict(report.metrics)
            report_metrics.update(
                {
                    "artifactProductionReady": workflow_production,
                    "stepArtifactProductionReady": step_production,
                    "artifactEngine": workflow_engine,
                    "productionReadyReason": workflow_reason,
                    "previewOnly": not workflow_production,
                }
            )
            report = report.model_copy(
                update={
                    "production_ready": workflow_production,
                    "engine": workflow_engine,
                    "metrics": report_metrics,
                }
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
        except (PlatformError, RuntimeError) as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/snapshot")
    async def export_cam_snapshot(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Export the complete CAM/NC state for backup or migration.

        Snapshots contain NC text and are therefore restricted to global
        administrators rather than ordinary CAM participants.
        """

        actor = _token_user(services, authorization)
        try:
            services.auth.require(actor, Permission.USER_MANAGE)
            return services.cam.snapshot()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.post("/cam/snapshot/restore")
    async def restore_cam_snapshot(
        payload: dict[str, Any] = Body(...),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Atomically restore a previously exported CAM snapshot (admin only)."""

        actor = _token_user(services, authorization)
        data = _require_dict(payload)
        try:
            services.auth.require(actor, Permission.USER_MANAGE)
            # Accept both a raw snapshot body and ``{"snapshot": {...}}`` so
            # clients can add migration metadata without changing the core
            # service contract.
            snapshot = data.get("snapshot", data)
            if not isinstance(snapshot, Mapping):
                raise ValidationError("snapshot must be an object")
            replace = bool(data.get("replace", True)) if "snapshot" in data else True
            return services.cam.restore(snapshot, replace=replace)
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
            project_id_raw = data.get("projectId", data.get("project_id"))
            project_id = str(project_id_raw).strip() if project_id_raw else None
            source_document_id_raw = data.get("sourceDocumentId", data.get("source_document_id"))
            source_document_id = str(source_document_id_raw).strip() if source_document_id_raw else None
            source_version_id_raw = data.get("sourceVersionId", data.get("source_version_id"))
            source_version_id = str(source_version_id_raw).strip() if source_version_id_raw else None
            if project_id:
                # A project-linked plan is a project write.  Validate source
                # references as well so a plan cannot smuggle another
                # project's document/version into this project's audit trail.
                _require_project_access(services, actor, project_id, write=True)
                if source_document_id:
                    _document, source_project = _resolve_document_project(
                        services, source_document_id, include_deleted=True
                    )
                    if source_project.id != project_id:
                        raise ValidationError("sourceDocumentId does not belong to projectId")
                if source_version_id:
                    version = services.pdm.get_version(source_version_id)
                    _document, source_project = _resolve_document_project(
                        services, version.document_id, include_deleted=True
                    )
                    if source_project.id != project_id:
                        raise ValidationError("sourceVersionId does not belong to projectId")
                    if source_document_id and version.document_id != source_document_id:
                        raise ValidationError("sourceVersionId does not belong to sourceDocumentId")
            else:
                # Unscoped plans are retained for legacy local CAM jobs, but a
                # source reference still needs read access to its PDM project.
                if source_document_id:
                    _require_document_access(
                        services, actor, source_document_id, write=False, include_deleted=True
                    )
                if source_version_id:
                    version = services.pdm.get_version(source_version_id)
                    _require_document_access(
                        services, actor, version.document_id, write=False, include_deleted=True
                    )
                    if source_document_id and version.document_id != source_document_id:
                        raise ValidationError("sourceVersionId does not belong to sourceDocumentId")
            plan = services.cam.create_plan(
                actor_id=actor.id,
                geometry_hash=str(data.get("geometryHash", data.get("geometry_hash", ""))),
                stock=data.get("stock", {}),
                machine=str(data.get("machine", "3-axis-mill")),
                units=str(data.get("units", "mm")),
                project_id=project_id,
                source_document_id=source_document_id,
                source_version_id=source_version_id,
            )
            return plan.to_dict()
        except (PlatformError, KeyError, TypeError, ValueError) as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/plans/{plan_id}")
    async def get_cam_plan(
        plan_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Return one plan after applying its project ACL."""

        actor = _token_user(services, authorization)
        try:
            _require_any_permission(
                services,
                actor,
                Permission.CAM_PLAN,
                Permission.CAM_APPROVE,
                Permission.CAM_RELEASE,
                Permission.NC_DOWNLOAD,
            )
            plan = _require_cam_plan_access(services, actor, plan_id, write=False)
            return plan.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/simulations/{simulation_id}")
    async def get_cam_simulation(
        simulation_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Return a simulation only when its parent plan is visible."""

        actor = _token_user(services, authorization)
        try:
            _require_any_permission(
                services,
                actor,
                Permission.CAM_PLAN,
                Permission.CAM_APPROVE,
                Permission.CAM_RELEASE,
                Permission.NC_DOWNLOAD,
            )
            simulation = services.cam.get_simulation(simulation_id)
            _require_cam_plan_access(services, actor, simulation.plan_id, write=False)
            return simulation.to_dict()
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/nc/{program_id}/info")
    async def get_nc_info(
        program_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Return NC metadata (without program text) under project ACL."""

        actor = _token_user(services, authorization)
        try:
            _require_any_permission(
                services,
                actor,
                Permission.CAM_PLAN,
                Permission.CAM_APPROVE,
                Permission.CAM_RELEASE,
                Permission.NC_DOWNLOAD,
            )
            program = services.cam.get_nc(program_id)
            _require_cam_plan_access(services, actor, program.plan_id, write=False)
            info = program.to_dict()
            info.pop("text", None)
            info["downloadPath"] = f"/api/v1/cam/nc/{program.id}"
            return info
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    @router.get("/cam/plans")
    async def list_cam_plans(
        authorization: str | None = Header(default=None),
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor = _token_user(services, authorization)
        try:
            # Designers/manufacturing can manage plans; reviewers/admins need
            # read access to inspect a plan before approving it.
            _require_any_permission(
                services,
                actor,
                Permission.CAM_PLAN,
                Permission.CAM_APPROVE,
                Permission.CAM_RELEASE,
            )
            if project_id:
                _require_project_access(services, actor, project_id, write=False)
                plans = services.cam.list_plans(project_id=project_id)
            else:
                plans = []
                for candidate in services.cam.list_plans():
                    try:
                        _require_cam_plan_access(services, actor, candidate.id, write=False)
                    except AuthorizationError:
                        continue
                    plans.append(candidate)
            return {"items": [plan.to_dict() for plan in plans]}
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
            _require_cam_plan_access(
                services,
                actor,
                plan_id,
                write=True,
            )
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
            _require_cam_plan_access(
                services,
                actor,
                plan_id,
                write=True,
            )
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
            # Gate status is a review/read operation, not the release itself.
            # Expose it to designers and reviewers so they can diagnose a
            # blocked plan, while keeping viewers unauthorised.
            _require_any_permission(
                services,
                actor,
                Permission.CAM_PLAN,
                Permission.CAM_APPROVE,
                Permission.CAM_RELEASE,
            )
            _require_cam_plan_access(services, actor, plan_id, write=False)
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
            # Approval is a review action.  Project read membership plus the
            # dedicated CAM_APPROVE permission is sufficient; reviewers do not
            # receive PROJECT_WRITE by design.
            _require_cam_plan_access(services, actor, plan_id, write=False)
            # A manufacturing account may release an already approved plan,
            # but may not supply the approval itself.  Admins are the explicit
            # emergency/administrative exception and are still subject to the
            # separate release-actor gate.
            actor_roles = {
                (item.value if isinstance(item, Role) else str(item)).casefold()
                for item in actor.roles
            }
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
            # Release follows the same scope rule: manufacturing needs project
            # read membership and CAM_RELEASE, while plan editing remains a
            # project-write action.
            _require_cam_plan_access(services, actor, plan_id, write=False)
            actor_roles = {
                (item.value if isinstance(item, Role) else str(item)).casefold()
                for item in actor.roles
            }
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
            _require_cam_plan_access(services, actor, program.plan_id, write=False)
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
