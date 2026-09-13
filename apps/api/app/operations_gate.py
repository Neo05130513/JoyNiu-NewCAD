"""Opt-in host-coordinated admission gate for quiescent deployment backups.

Ordinary HTTP requests hold a shared OS lock until their ASGI call (including
background response work) exits. The host must own the exclusive lock before
checking the job journal a second time and stopping this single API service.
Readiness and narrowly authenticated loopback activation probes are exceptions.
No model, customer record, or credential is changed by the gate.
"""
from __future__ import annotations

import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import stat

from starlette.responses import JSONResponse


_ACTIVATION_PROBE_PATHS = frozenset({
    "/api/v1/health", "/api/v1/cad-agent/capabilities",
    "/api/cad/drawings", "/api/cad/features", "/api/cad/designs", "/api/cad/deliveries",
})


def _activation_probe(scope, directory):
    """A deployment-only read probe; application authentication still runs."""
    client = scope.get("client")
    if (scope.get("method") != "GET" or scope.get("path") not in _ACTIVATION_PROBE_PATHS
            or not isinstance(client, (tuple, list)) or len(client) != 2
            or client[0] not in {"127.0.0.1", "::1"}):
        return False
    headers = scope.get("headers", ())
    # Uvicorn may already have interpreted proxy headers when constructing
    # scope.client. Their presence disqualifies the probe, even if that peer
    # now looks like loopback. Probes are anonymous reads only.
    if any(name.lower() in {b"authorization", b"cookie", b"forwarded"}
           or name.lower().startswith(b"x-forwarded-") for name, _value in headers):
        return False
    tokens = [value for name, value in headers if name.lower() == b"x-joyniu-maintenance-probe"]
    if len(tokens) != 1 or not re.fullmatch(rb"[a-fA-F0-9]{32}", tokens[0]):
        return False
    try:
        descriptor = os.open(directory / "maintenance.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as marker:
            info = os.fstat(marker.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 8192:
                return False
            data = marker.read(8193)
        if len(data) > 8192:
            return False
        value = json.loads(data)
        if not isinstance(value, dict) or value.get("format") != "joyniu-currentcad-activation-v1":
            return False
        token = value.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", token):
            return False
        return hmac.compare_digest(tokens[0], token.encode("ascii"))
    except (OSError, ValueError, TypeError, RecursionError):
        return False


class OperationsGateMiddleware:
    def __init__(self, app, directory=None):
        self.app = app
        configured = directory if directory is not None else os.getenv("JOYNIU_OPERATIONS_DIR")
        self.directory = Path(configured) if configured else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.directory is None:
            return await self.app(scope, receive, send)
        if scope.get("method") == "GET" and scope.get("path") == "/api/v1/operations/readiness":
            response = JSONResponse({"gateVersion": "v1", "maintenance": (self.directory / "maintenance.json").exists()})
            return await response(scope, receive, send)

        async def with_header(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-joyniu-operations-gate", b"v1")]}
            await send(message)

        # Activation keeps the host's exclusive admission lock and marker in
        # place. Only a loopback read probe with its one-time marker token may
        # reach the original router while that exclusive lock is held.
        if _activation_probe(scope, self.directory):
            return await self.app(scope, receive, with_header)
        descriptor = None
        try:
            self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
            descriptor = os.open(self.directory / "admission.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o640)
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            # Check under the shared lock: a request racing the host's marker
            # creation either exits here or prevents exclusive acquisition.
            if (self.directory / "maintenance.json").exists():
                raise BlockingIOError("Maintenance in progress")
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            response = JSONResponse(
                {"detail": "系统正在进行维护备份，请稍后重试；本次请求尚未开始处理。", "code": "operations_maintenance"},
                status_code=503, headers={"Retry-After": "60", "X-Joyniu-Operations-Gate": "v1"},
            )
            return await response(scope, receive, send)

        try:
            return await self.app(scope, receive, with_header)
        finally:
            os.close(descriptor)
