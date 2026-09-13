"""Formal document publishing and explicit acceptance, never inferred consent."""
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response

from .commercial_terms import CommercialTermsService, CommercialTermsUnavailable
from .platform import PlatformError
from .platform_api import _domain_http_exception, _token_user


def create_commercial_terms_router(services, terms=None):
    service = terms if terms is not None else CommercialTermsService(services.auth)
    router = APIRouter(prefix="/commercial", tags=["commercial-terms"])
    router.terms_service = service

    def no_cache(response: Response):
        response.headers["Cache-Control"] = "no-store"

    router.dependencies.append(Depends(no_cache))

    def identity(authorization: str | None = Header(default=None)):
        return _token_user(services, authorization)

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CommercialTermsUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None

    @router.get("/terms")
    def terms():
        return invoke(service.terms)

    @router.post("/admin/terms", status_code=201)
    def publish(payload: dict = Body(...), actor=Depends(identity)):
        return invoke(service.publish, actor.id, payload)

    @router.get("/admin/terms/history")
    def history(actor=Depends(identity), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.history, actor.id, limit=limit, offset=offset)

    @router.get("/acceptance")
    def acceptance(actor=Depends(identity)):
        return invoke(service.acceptance, actor.id)

    @router.post("/acceptance")
    def accept(payload: dict = Body(...), actor=Depends(identity)):
        if set(payload) != {"documentIds", "idempotencyKey"}:
            raise HTTPException(422, "须明确提交当前文档列表和本次接受请求标识。")
        return invoke(service.accept, actor.id, document_ids=payload["documentIds"], idempotency_key=payload["idempotencyKey"])

    return router
