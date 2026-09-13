"""Authenticated, size-bounded HTTP boundary for account workspaces."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import Request
except ImportError:  # pragma: no cover - domain users do not need FastAPI
    Request = Any  # type: ignore[misc,assignment]

from .account_workspace import (
    MAX_WORKSPACE_BYTES,
    AccountWorkspaceStore,
    WorkspaceConflictError,
    WorkspaceTooLargeError,
    WorkspaceValidationError,
)
from .platform_api import _token_user


def create_account_workspace_router(services, database: str | Path | None = None, *, max_body_bytes: int = MAX_WORKSPACE_BYTES):
    from fastapi import APIRouter, Header, HTTPException
    from fastapi.responses import JSONResponse

    if type(max_body_bytes) is not int or max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be a positive integer")
    store = AccountWorkspaceStore(
        database if database is not None else services.auth.database,
        max_bytes=max_body_bytes,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            store.close()

    router = APIRouter(tags=["account-workspace"], lifespan=lifespan)
    no_store = {"Cache-Control": "no-store"}

    def http_error(status: int, detail: Any):
        return HTTPException(status_code=status, detail=detail, headers=no_store)

    async def read_payload(request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise http_error(415, "请使用 application/json 保存工作区。")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                raise http_error(422, "Content-Length 无效。") from None
            if size < 0:
                raise http_error(422, "Content-Length 无效。")
            if size > max_body_bytes:
                raise http_error(413, "工作区超过允许的保存大小。")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_body_bytes:
                raise http_error(413, "工作区超过允许的保存大小。")
            body.extend(chunk)
        try:
            def invalid_constant(_value):
                raise ValueError("non-finite JSON number")
            payload = json.loads(body.decode("utf-8"), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise http_error(422, "请求体必须是有效的 UTF-8 JSON。") from None
        if not isinstance(payload, dict):
            raise http_error(422, "请求体必须是 JSON 对象。")
        return payload

    @router.get("/account/workspace")
    async def load_workspace(authorization: str | None = Header(default=None)):
        actor = _token_user(services, authorization)
        return JSONResponse(store.load(actor.id), headers=no_store)

    @router.put("/account/workspace")
    async def save_workspace(request: Request, authorization: str | None = Header(default=None)):
        actor = _token_user(services, authorization)
        data = await read_payload(request)
        try:
            result = store.save(actor.id, data.get("snapshot"), expected_revision=data.get("expectedRevision"))
        except WorkspaceConflictError as exc:
            raise http_error(409, {"code": "workspace_revision_conflict", "message": str(exc), "revision": exc.revision}) from exc
        except WorkspaceTooLargeError as exc:
            raise http_error(413, str(exc)) from exc
        except WorkspaceValidationError as exc:
            raise http_error(422, str(exc)) from exc
        return JSONResponse(result, headers=no_store)

    return router
