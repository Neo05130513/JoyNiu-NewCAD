"""Administrator policy versions; users can only read their own settlement history."""
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response

from .billing_policy import BillingPolicyService
from .platform import PlatformError
from .platform_api import _domain_http_exception, _token_user


def create_billing_policy_router(services, *, billing, policy=None):
    service = policy or BillingPolicyService(billing)
    router = APIRouter(prefix="/billing", tags=["billing-policy"])
    router.policy_service = service

    def identity(authorization: str | None = Header(default=None)):
        return _token_user(services, authorization)

    def no_cache(response: Response):
        response.headers["Cache-Control"] = "no-store"

    router.dependencies.append(Depends(no_cache))

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None

    def administrator(actor=Depends(identity)):
        invoke(billing._admin, actor.id)
        return actor

    def reader(actor=Depends(identity)):
        invoke(billing._admin, actor.id, "billing:read")
        return actor

    @router.get("/pricing")
    def pricing():
        return invoke(service.pricing)

    @router.get("/admin/policy")
    def overview(actor=Depends(administrator)):
        return invoke(service.overview, actor.id)

    @router.post("/admin/policy/versions", status_code=201)
    def save_version(payload: dict = Body(...), actor=Depends(administrator)):
        return invoke(service.save_version, actor.id, payload)

    @router.patch("/admin/policy")
    def set_enabled(payload: dict = Body(...), actor=Depends(administrator)):
        if set(payload) != {"revision", "versionId", "enabled", "reason"}:
            raise HTTPException(422, "须提供状态版本、规则版本、开启状态和变更原因")
        return invoke(service.set_enabled, actor.id, revision=payload["revision"], version_id=payload["versionId"],
                      enabled=payload["enabled"], reason=payload["reason"])

    @router.get("/settlements")
    def settlements(actor=Depends(identity), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.settlements, owner_id=actor.id, limit=limit, offset=offset)

    @router.get("/admin/settlements")
    def admin_settlements(actor=Depends(reader), q: str = Query('',max_length=128), status: str = Query('',max_length=64), ownerId: str = Query('',max_length=160), dateFrom: str = Query('',max_length=10), dateTo: str = Query('',max_length=10), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.settlements, actor_id=actor.id, limit=limit, offset=offset, q=q, status=status, filter_owner_id=ownerId, date_from=dateFrom, date_to=dateTo)

    # No HTTP route accepts token counts, calls settle_attempt, or changes a
    # sealed receipt. Only the trusted worker can supply the actual-call list.
    return router
