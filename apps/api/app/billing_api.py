"""Account-scoped billing; offline receipt confirmation requires finance permission."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response

from .billing import BillingService, BillingUnavailable, VerifiedPaymentEvent, VerifiedRefundEvent
from .platform import PlatformError, RateLimitError, ValidationError
from .platform_api import _domain_http_exception, _token_user


def create_billing_router(services, *, billing=None, payment_adapter=None):
    """Mount under /api/v1; ``router.billing_service`` is the worker interface.

    Payment and charging are deliberately disabled by default. Composition may
    inject a BillingService after commercial rules and a real gateway exist.
    No credentials, fabricated checkout, or demo balances are seeded here.
    """
    service = billing or BillingService(services.pdm.database, auth=services.auth, payment_adapter=payment_adapter)
    router = APIRouter(prefix="/billing", tags=["billing"])
    router.billing_service = service

    def identity(authorization: str | None = Header(default=None)):
        return _token_user(services, authorization)

    def no_cache(response: Response):
        response.headers["Cache-Control"] = "no-store"

    router.dependencies.append(Depends(no_cache))

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BillingUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except RateLimitError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from None
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None

    def fields(payload, allowed):
        if set(payload) - set(allowed):
            raise HTTPException(422, "请求包含不支持的字段")
        return payload

    def require_permission(permission):
        def authorized(actor=Depends(identity)):
            invoke(service._admin, actor.id, permission)
            return actor
        return authorized

    reader = require_permission("billing:read")
    manager = require_permission("billing:manage")
    adjuster = require_permission("billing:adjust")
    policy_manager = require_permission("billing:policy")

    @router.get("/status")
    def status():
        return service.status()

    @router.get("/packages")
    def packages():
        return service.packages()

    @router.get("/wallet")
    def wallet(actor=Depends(identity)):
        return invoke(service.wallet, actor.id)

    @router.get("/ledger")
    def ledger(actor=Depends(identity), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "ledger", owner_id=actor.id, limit=limit, offset=offset)

    @router.get("/orders")
    def orders(actor=Depends(identity), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "orders", owner_id=actor.id, limit=limit, offset=offset)

    @router.post("/orders", status_code=201)
    def create_order(payload: dict = Body(...), actor=Depends(identity)):
        fields(payload, ("packageId", "idempotencyKey"))
        return invoke(service.create_order, actor.id, package_id=payload.get("packageId"), idempotency_key=payload.get("idempotencyKey"))

    @router.get("/orders/{order_id}")
    def order(order_id: str, actor=Depends(identity)):
        return invoke(service.order, actor.id, order_id)

    @router.post("/orders/{order_id}/reconcile")
    def reconcile_order(order_id: str, payload: dict = Body(default={}), actor=Depends(identity)):
        fields(payload, ())
        return invoke(service.reconcile_order, actor.id, order_id)

    @router.post("/orders/{order_id}/refund-requests", status_code=201)
    def request_refund(order_id: str, payload: dict = Body(...), actor=Depends(identity)):
        fields(payload, ("reason", "amountFen", "idempotencyKey"))
        return invoke(service.request_service, actor.id, order_id, kind="refund", values=payload, idempotency_key=payload.get("idempotencyKey"))

    @router.post("/orders/{order_id}/invoice-requests", status_code=201)
    def request_invoice(order_id: str, payload: dict = Body(...), actor=Depends(identity)):
        fields(payload, ("title", "taxId", "email", "idempotencyKey"))
        return invoke(service.request_service, actor.id, order_id, kind="invoice", values=payload, idempotency_key=payload.get("idempotencyKey"))

    @router.get("/refund-requests")
    def refund_requests(actor=Depends(identity), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "refund", owner_id=actor.id, limit=limit, offset=offset)

    @router.get("/invoice-requests")
    def invoice_requests(actor=Depends(identity), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "invoice", owner_id=actor.id, limit=limit, offset=offset)

    @router.get("/admin/packages")
    def admin_packages(actor=Depends(reader)):
        return invoke(service.packages, actor_id=actor.id, include_inactive=True)

    @router.post("/admin/packages", status_code=201)
    def create_package(payload: dict = Body(...), actor=Depends(policy_manager)):
        return invoke(service.save_package, actor.id, payload)

    @router.patch("/admin/packages/{package_id}")
    def update_package(package_id: str, payload: dict = Body(...), actor=Depends(policy_manager)):
        return invoke(service.save_package, actor.id, payload, package_id=package_id)

    @router.post("/admin/adjustments", status_code=201)
    def adjustment(payload: dict = Body(...), actor=Depends(adjuster)):
        fields(payload, ("ownerId", "creditUnits", "reason", "idempotencyKey", "category"))
        return invoke(service.adjust, actor.id, owner_id=payload.get("ownerId"), credit_units=payload.get("creditUnits"),
                      reason=payload.get("reason"), idempotency_key=payload.get("idempotencyKey"), category=payload.get("category", "adjustment"))

    @router.post("/admin/offline-recharges", status_code=201)
    def offline_recharge(payload: dict = Body(...), actor=Depends(manager)):
        fields(payload, ("ownerId", "amountFen", "externalReference", "reason", "receiptConfirmed", "idempotencyKey"))
        return invoke(service.offline_recharge, actor.id, owner_id=payload.get("ownerId"), amount_fen=payload.get("amountFen"),
                      external_reference=payload.get("externalReference"), reason=payload.get("reason"),
                      receipt_confirmed=payload.get("receiptConfirmed"), idempotency_key=payload.get("idempotencyKey"))

    @router.post("/admin/offline-recharges/{order_id}/refund", status_code=201)
    def offline_refund(order_id: str, payload: dict = Body(...), actor=Depends(manager)):
        fields(payload, ("externalReference", "reason", "refundConfirmed", "idempotencyKey", "refundRequestId"))
        return invoke(service.offline_refund, actor.id, order_id, external_reference=payload.get("externalReference"),
                      reason=payload.get("reason"), refund_confirmed=payload.get("refundConfirmed"),
                      idempotency_key=payload.get("idempotencyKey"), refund_request_id=payload.get("refundRequestId"))

    def record_filters(q: str = Query('', max_length=128), status: str = Query('', max_length=64),
                       ownerId: str = Query('', max_length=160), dateFrom: str = Query('', max_length=10), dateTo: str = Query('', max_length=10), source: str = Query('', max_length=10)):
        return dict(q=q, status=status, filter_owner_id=ownerId, date_from=dateFrom, date_to=dateTo, source=source)

    @router.get("/admin/ledger")
    def admin_ledger(actor=Depends(reader), filters=Depends(record_filters), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "ledger", actor_id=actor.id, limit=limit, offset=offset, **filters)

    @router.get("/admin/orders")
    def admin_orders(actor=Depends(reader), filters=Depends(record_filters), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "orders", actor_id=actor.id, limit=limit, offset=offset, **filters)

    @router.get("/admin/refund-requests")
    def admin_refunds(actor=Depends(reader), filters=Depends(record_filters), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "refund", actor_id=actor.id, limit=limit, offset=offset, **filters)

    @router.get("/admin/invoice-requests")
    def admin_invoices(actor=Depends(reader), filters=Depends(record_filters), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "invoice", actor_id=actor.id, limit=limit, offset=offset, **filters)

    @router.patch("/admin/requests/{request_id}")
    def review_request(request_id: str, payload: dict = Body(...), actor=Depends(manager)):
        fields(payload, ("status", "note", "externalReference"))
        return invoke(service.review_request, actor.id, request_id, status=payload.get("status"), note=payload.get("note"), external_reference=payload.get("externalReference", ""))

    @router.post("/admin/requests/{request_id}/refund")
    def execute_refund(request_id: str, payload: dict = Body(default={}), actor=Depends(manager)):
        fields(payload, ())
        return invoke(service.execute_refund, actor.id, request_id)

    @router.post("/admin/requests/{request_id}/refund/reconcile")
    def reconcile_refund(request_id: str, payload: dict = Body(default={}), actor=Depends(manager)):
        fields(payload, ())
        return invoke(service.reconcile_refund, actor.id, request_id)

    @router.get("/admin/dashboard")
    def dashboard(actor=Depends(reader)):
        return invoke(service.dashboard, actor.id)

    @router.get("/admin/usage")
    def usage(actor=Depends(reader), filters=Depends(record_filters), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "usage", actor_id=actor.id, limit=limit, offset=offset, **filters)

    @router.get("/admin/audit")
    def audit(actor=Depends(reader), filters=Depends(record_filters), limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        return invoke(service.list_records, "audit", actor_id=actor.id, limit=limit, offset=offset, **filters)

    @router.post("/payment-notifications/{provider}")
    async def payment_notification(provider: str, request: Request):
        adapter = service.payment_adapter
        if not adapter or adapter.configured is not True or adapter.name != provider:
            raise HTTPException(503, "支付渠道尚未配置")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 131072:
                raise HTTPException(413, "支付通知过大")
        try:
            event = adapter.verify_event(bytes(body), dict(request.headers))
            if not isinstance(event, VerifiedPaymentEvent):
                raise ValidationError("未验证的支付事件")
        except Exception:
            raise HTTPException(400, "支付通知验证失败") from None
        invoke(service.accept_payment, event)
        return {"code": "SUCCESS", "message": "成功"}

    @router.post("/refund-notifications/{provider}")
    async def refund_notification(provider: str, request: Request):
        adapter = service.payment_adapter
        # Keep accepting signed results for already-started refunds even when
        # the operator disables creation of new refunds.
        if not adapter or adapter.configured is not True or adapter.name != provider or not callable(getattr(adapter, "verify_refund_event", None)):
            raise HTTPException(503, "退款渠道尚未配置")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 131072:
                raise HTTPException(413, "退款通知过大")
        try:
            event = adapter.verify_refund_event(bytes(body), dict(request.headers))
            if not isinstance(event, VerifiedRefundEvent):
                raise ValidationError("未验证的退款事件")
        except Exception:
            raise HTTPException(400, "退款通知验证失败") from None
        invoke(service.accept_refund, event)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return router
