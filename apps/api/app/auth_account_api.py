"""Account lifecycle endpoints with rotating, server-revocable browser sessions."""
from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .platform import AuthenticationError, AuthorizationError, ConflictError, PlatformError, RateLimitError, Role, ValidationError

COOKIE_NAME = "joyniu_refresh"
COOKIE_PATH = "/api/v1/auth"
CSRF_HEADER = "x-joyniu-csrf"


def _registration_enabled() -> bool:
    return os.getenv("JOYNIU_PUBLIC_REGISTRATION", "false").strip().casefold() in {"1", "true", "yes"}


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _request_origin(request: Request) -> tuple[str, str, int] | None:
    return _origin(f"{request.url.scheme}://{request.url.netloc}")


def _check_request(request: Request, *, cookie_action: bool = False) -> None:
    if request.headers.get("content-type", "").partition(";")[0].strip().casefold() != "application/json":
        raise HTTPException(415, "请使用 JSON 格式提交。")
    supplied = request.headers.get("origin")
    if supplied is not None:
        source = _origin(supplied)
        allowed = {_request_origin(request)}
        for item in os.getenv("JOYNIU_AUTH_ALLOWED_ORIGINS", "").split(","):
            if item.strip():
                allowed.add(_origin(item.strip()))
        allowed.discard(None)
        local_development = (
            os.getenv("JOYNIU_ENV", "").strip().casefold() not in {"prod", "production"}
            and source is not None and _loopback(source[1]) and _loopback(request.url.hostname))
        if source is None or (source not in allowed and not local_development):
            raise HTTPException(403, "请求来源不受允许，请从本站操作。")
    # A non-simple header cannot be sent by a cross-site form. Browser Origin
    # checks above also defend against overly broad CORS settings elsewhere.
    if cookie_action and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "会话请求缺少安全校验，请刷新页面后重试。")
    if supplied is None and request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "请求来源不受允许，请从本站操作。")


def _limits(services, request: Request, purpose: str, email: str = "") -> None:
    # ASGI client is authoritative. Production must configure its trusted
    # reverse proxy to forward client addresses; never trust arbitrary XFF.
    address = request.client.host if request.client else "unknown"
    maximum, seconds = {"login": (30, 300), "register": (5, 600), "refresh": (120, 60),
                        "password": (10, 600), "forgot": (5, 600)}[purpose]
    limits = [(f"{purpose}:ip", address, maximum, seconds)]
    if purpose == "login" and email:
        limits.append(("login:account", email.strip().casefold()[:254], 10, 300))
    try:
        services.auth.consume_auth_limits(limits)
    except RateLimitError as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from None


def _error(exc: PlatformError) -> HTTPException:
    if isinstance(exc, AuthenticationError):
        return HTTPException(401, "账号或密码不正确，或登录已过期，请重新登录。")
    if isinstance(exc, AuthorizationError):
        return HTTPException(403, "此操作需要管理员权限。")
    if isinstance(exc, ConflictError):
        return HTTPException(409, "该邮箱无法注册，请登录或联系管理员。")
    if isinstance(exc, ValidationError):
        return HTTPException(422, str(exc))
    return HTTPException(400, "账号操作未完成，请检查输入后重试。")


def _text(payload: dict[str, Any], key: str, *, maximum: int = 256) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise HTTPException(422, "输入格式或长度不正确。")
    return value


def _persistent_allowed(request: Request) -> bool:
    return request.url.scheme == "https" or _loopback(request.url.hostname)


def _cookie_response(request: Request, token, raw: str | None = None, expires_at: int | None = None, *, now: int = 0, status: int = 200):
    payload = {**token.to_dict(), "persistent_session": raw is not None}
    if raw is None:
        payload["session_notice"] = "自动保持登录需要 HTTPS；当前仅登录到本次浏览器会话。"
    response = JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    if raw is not None:
        response.set_cookie(COOKIE_NAME, raw, max_age=max(1, int(expires_at) - now), httponly=True,
                            secure=request.url.scheme == "https" or not _loopback(request.url.hostname),
                            samesite="strict", path=COOKIE_PATH)
    return response


def _login_response(services, request: Request, credentials, *, status: int = 200):
    # Even a nonpersistent HTTP login needs a durable session identity so its
    # bearer can be revoked without signing out the customer's other devices.
    token, raw, expiry = services.auth.create_session(credentials.user, credential_token=credentials.token)
    if not _persistent_allowed(request):
        return _cookie_response(request, token, status=status)
    return _cookie_response(request, token, raw, expiry, now=int(services.auth._clock()), status=status)


def account_login(services, request: Request, payload: dict[str, Any]):
    """Shared implementation for the existing /auth/login contract."""
    _check_request(request)
    email = _text(payload, "email", maximum=254)
    password = _text(payload, "password")
    _limits(services, request, "login", email)
    try:
        token = services.auth.authenticate(email, password)
        # The verified credentials grant a new independent session. An older
        # cookie is revoked so account switching cannot leave it reusable.
        if request.cookies.get(COOKIE_NAME):
            services.auth.revoke_refresh_session(request.cookies[COOKIE_NAME])
        return _login_response(services, request, token)
    except PlatformError as exc:
        raise _error(exc) from None


def create_account_router(services):
    router = APIRouter(tags=["Accounts"])

    def actor(authorization: str | None):
        from .platform_api import _token_user
        return _token_user(services, authorization)

    @router.get("/auth/account-capabilities")
    async def capabilities(request: Request):
        return JSONResponse({"registrationEnabled": _registration_enabled(), "passwordResetEmailAvailable": False,
                             "passwordResetMode": "administrator", "emailVerificationAvailable": False,
                             "persistentSessionAvailable": _persistent_allowed(request), "refreshCsrfHeader": "X-JoyNiu-CSRF"},
                            headers={"Cache-Control": "no-store"})

    @router.post("/auth/register")
    async def register(request: Request, payload: dict[str, Any] = Body(...)):
        _check_request(request)
        if not _registration_enabled():
            raise HTTPException(403, "暂未开放自助注册，请联系管理员开通账号。")
        _limits(services, request, "register")
        if set(payload) - {"email", "password", "displayName", "display_name"}:
            raise HTTPException(422, "注册仅接受邮箱、密码和姓名。")
        email, password = _text(payload, "email", maximum=254), _text(payload, "password")
        name = _text(payload, "displayName" if "displayName" in payload else "display_name", maximum=120)
        try:
            user = services.auth.create_user(email, password, name, roles=(Role.DESIGNER,), actor_id="self-registration")
            if request.cookies.get(COOKIE_NAME):
                services.auth.revoke_refresh_session(request.cookies[COOKIE_NAME])
            return _login_response(services, request, services.auth.authenticate(user.email, password), status=201)
        except PlatformError as exc:
            raise _error(exc) from None

    @router.post("/auth/refresh")
    async def refresh(request: Request, payload: dict[str, Any] = Body(...)):
        _check_request(request, cookie_action=True)
        _limits(services, request, "refresh")
        if not _persistent_allowed(request):
            raise HTTPException(403, "自动保持登录需要 HTTPS。")
        try:
            token, raw, expiry = services.auth.refresh_session(request.cookies.get(COOKIE_NAME, ""))
            return _cookie_response(request, token, raw, expiry, now=int(services.auth._clock()))
        except PlatformError as exc:
            response = JSONResponse({"detail": _error(exc).detail}, status_code=401, headers={"Cache-Control": "no-store"})
            response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True, samesite="strict")
            return response

    @router.post("/auth/logout")
    async def logout(request: Request, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)):
        _check_request(request, cookie_action=True)
        if request.cookies.get(COOKIE_NAME):
            services.auth.revoke_refresh_session(request.cookies[COOKIE_NAME])
        if authorization and authorization.lower().startswith("bearer "):
            try:
                services.auth.revoke_access_session(authorization.split(" ", 1)[1].strip())
            except AuthenticationError:
                pass  # Logout is idempotent after rotation, expiry or revocation.
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True, samesite="strict")
        return response

    @router.post("/auth/password/change")
    async def change_password(request: Request, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)):
        _check_request(request, cookie_action=True)
        user = actor(authorization)
        _limits(services, request, "password")
        try:
            updated = services.auth.change_password(user.id, _text(payload, "currentPassword"), _text(payload, "newPassword"))
            return _login_response(services, request, services.auth.authenticate(updated.email, _text(payload, "newPassword")))
        except PlatformError as exc:
            raise _error(exc) from None

    @router.post("/auth/users/{user_id}/password/reset")
    async def reset_password(user_id: str, request: Request, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)):
        _check_request(request)
        user = actor(authorization)
        _limits(services, request, "password")
        try:
            updated = services.auth.reset_password(user_id, _text(payload, "newPassword"), actor_id=user.id)
            return JSONResponse({"ok": True, "user": updated.to_dict()}, headers={"Cache-Control": "no-store"})
        except PlatformError as exc:
            raise _error(exc) from None

    @router.post("/auth/password/forgot")
    async def forgot_password(request: Request, payload: dict[str, Any] = Body(...)):
        _check_request(request)
        _limits(services, request, "forgot")
        # Do not inspect whether the supplied email exists or claim to send mail.
        raise HTTPException(503, {"code": "password_reset_email_unavailable", "message": "暂未配置邮件找回服务，请联系管理员重置密码。"})

    return router
