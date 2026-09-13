"""Use the actual account router over loopback HTTP, never a mocked response."""
from contextlib import contextmanager
import socket
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import urlopen

from fastapi import FastAPI
import pytest
import uvicorn

from app.auth_account_api import create_account_router
from app.platform_api import build_platform_services
from .test_commercial_operations import ops


@contextmanager
def actual_account_http():
    services = build_platform_services(":memory:", auth_secret="operations-route-fixture-secret-123456")
    application = FastAPI()
    application.include_router(create_account_router(services), prefix="/api/v1")
    requests = []

    @application.middleware("http")
    async def record_request(request, call_next):
        requests.append(request.url.path)
        return await call_next(request)

    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.bind(("127.0.0.1", 0))
    connection.listen(128)
    base_url = "http://127.0.0.1:" + str(connection.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
    worker = Thread(target=lambda: server.run(sockets=[connection]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, "isolated HTTP server did not start"
        yield base_url, requests
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        connection.close()
        services.close()


def test_recovery_wait_uses_real_account_capabilities_route(monkeypatch):
    with actual_account_http() as (base_url, requests):
        host = ops.Host({"baseUrl": base_url, "restartTimeoutSeconds": 2})
        # Only Docker state is simulated. Host.wait_ready performs its real
        # urllib request against the application's current router declaration.
        monkeypatch.setattr(host, "services", lambda: [{"service": "api", "state": "running", "health": "healthy"}])
        host.wait_ready()
        assert requests == ["/api/v1/auth/account-capabilities"]
        with pytest.raises(HTTPError) as missing:
            urlopen(base_url + "/api/v1/auth/capabilities", timeout=2)
        assert missing.value.code == 404
