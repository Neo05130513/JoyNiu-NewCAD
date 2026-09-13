import asyncio
import fcntl
import json
import os

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.operations_gate import OperationsGateMiddleware


def application(directory, handler=None):
    async def success(request):
        return JSONResponse({"processed": True})
    app = Starlette(routes=[Route("/work", handler or success, methods=["GET", "POST"])])
    app.add_middleware(OperationsGateMiddleware, directory=directory)
    return app


def test_gate_accepts_business_and_reports_readiness_without_sensitive_values(tmp_path):
    with TestClient(application(tmp_path)) as client:
        response = client.post("/work")
        assert response.status_code == 200
        assert response.headers["x-joyniu-operations-gate"] == "v1"
        assert client.get("/api/v1/operations/readiness").json() == {"gateVersion": "v1", "maintenance": False}


def test_owned_maintenance_blocks_reads_and_writes_but_readiness_stays_observable(tmp_path):
    (tmp_path / "maintenance.json").write_text(json.dumps({"token": "PRIVATE-OPERATION"}))
    with TestClient(application(tmp_path)) as client:
        for method in (client.get, client.post):
            response = method("/work")
            assert response.status_code == 503 and response.headers["retry-after"] == "60"
            assert "PRIVATE-OPERATION" not in response.text
        assert client.get("/api/v1/operations/readiness").json()["maintenance"] is True


def test_exclusive_host_lock_blocks_requests_even_without_marker(tmp_path):
    with open(tmp_path / "admission.lock", "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with TestClient(application(tmp_path)) as client:
            assert client.post("/work").status_code == 503


def test_active_http_call_holds_lock_until_asgi_finishes(tmp_path):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        async def endpoint(scope, receive, send):
            started.set()
            await finish.wait()
        gate = OperationsGateMiddleware(endpoint, directory=tmp_path)
        task = asyncio.create_task(gate({"type": "http", "method": "POST", "path": "/work"}, None, None))
        await started.wait()
        with open(tmp_path / "admission.lock", "r+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("live request did not hold its shared lock")
            except BlockingIOError:
                pass
            finish.set()
            await task
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(scenario())


def test_failed_http_call_releases_lock(tmp_path):
    async def fail(request):
        raise ValueError("fixture failure")
    with TestClient(application(tmp_path, fail), raise_server_exceptions=False) as client:
        assert client.post("/work").status_code == 500
    with open(tmp_path / "admission.lock", "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_gate_fails_closed_for_symlink_lock(tmp_path):
    target = tmp_path / "target"
    target.write_text("private fixture")
    (tmp_path / "admission.lock").symlink_to(target)
    with TestClient(application(tmp_path)) as client:
        assert client.post("/work").status_code == 503
    assert target.read_text() == "private fixture"


def test_default_does_not_enable_maintenance_gate(monkeypatch):
    monkeypatch.delenv("JOYNIU_OPERATIONS_DIR", raising=False)
    with TestClient(application(None)) as client:
        assert client.post("/work").status_code == 200
        assert client.get("/api/v1/operations/readiness").status_code == 404


def probe_application(directory):
    async def readonly(request):
        return JSONResponse({"processed": True, "path": request.url.path})
    paths = ["/api/v1/health", "/api/v1/cad-agent/capabilities", "/api/cad/drawings",
             "/api/cad/features", "/api/cad/designs", "/api/cad/deliveries", "/not-a-probe"]
    app = Starlette(routes=[Route(path, readonly, methods=["GET", "POST", "DELETE"]) for path in paths])
    app.add_middleware(OperationsGateMiddleware, directory=directory)
    return app


def activation_marker(directory, **overrides):
    marker = {"format": "joyniu-currentcad-activation-v1", "token": "a1" * 16, **overrides}
    (directory / "maintenance.json").write_text(json.dumps(marker))
    return {"X-Joyniu-Maintenance-Probe": marker["token"]}


def test_activation_probe_reaches_only_allowlisted_gets_under_exclusive_lock(tmp_path):
    header = activation_marker(tmp_path)
    with open(tmp_path / "admission.lock", "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for host in ("127.0.0.1", "::1"):
            with TestClient(probe_application(tmp_path), client=(host, 50001)) as client:
                for path in ("/api/v1/health", "/api/v1/cad-agent/capabilities", "/api/cad/drawings",
                             "/api/cad/features", "/api/cad/designs", "/api/cad/deliveries"):
                    response = client.get(path, headers=header)
                    assert response.status_code == 200
                    assert response.json()["processed"] is True
                    assert response.headers["x-joyniu-operations-gate"] == "v1"
                    assert header["X-Joyniu-Maintenance-Probe"] not in response.text
                assert client.get("/api/v1/operations/readiness").json() == {"gateVersion": "v1", "maintenance": True}
                assert client.get("/api/cad/designs").status_code == 503
                assert client.post("/api/cad/designs", headers=header).status_code == 503
                assert client.delete("/api/cad/designs", headers=header).status_code == 503
                assert client.head("/api/v1/health", headers=header).status_code == 503
                assert client.get("/not-a-probe", headers=header).status_code == 503
                assert client.get("/api/cad/designs/", headers=header).status_code == 503
                assert client.get("/api/cad/designs/design_example", headers=header).status_code == 503


def test_non_loopback_cannot_use_probe_even_with_forwarded_loopback_header(tmp_path):
    header = {**activation_marker(tmp_path), "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "::1"}
    with open(tmp_path / "admission.lock", "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for host in ("192.0.2.40", "localhost", "127.0.0.2", "::ffff:127.0.0.1", "testclient"):
            with TestClient(probe_application(tmp_path), client=(host, 50001)) as client:
                response = client.get("/api/v1/health", headers=header)
                assert response.status_code == 503
                assert "processed" not in response.text
                assert client.get("/api/v1/operations/readiness").json()["maintenance"] is True


def test_probe_wrong_missing_duplicate_token_and_legacy_marker_fail_closed(tmp_path):
    header = activation_marker(tmp_path)
    with open(tmp_path / "admission.lock", "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with TestClient(probe_application(tmp_path), client=("127.0.0.1", 50001)) as client:
            for token in (None, "b2" * 16, "a1" * 15, "a1" * 17, "z" * 32):
                headers = {} if token is None else {"X-Joyniu-Maintenance-Probe": token}
                assert client.get("/api/v1/health", headers=headers).status_code == 503
            duplicate = [("X-Joyniu-Maintenance-Probe", "a1" * 16)] * 2
            assert client.get("/api/v1/health", headers=duplicate).status_code == 503
            for marker in ({"format": "joyniu-commercial-maintenance-v1", "token": "a1" * 16},
                           {"format": "joyniu-commercial-offline-v1", "token": "a1" * 16},
                           {"token": "a1" * 16}, {"format": "joyniu-currentcad-activation-v1", "token": 1}):
                (tmp_path / "maintenance.json").write_text(json.dumps(marker))
                assert client.get("/api/v1/health", headers=header).status_code == 503
            (tmp_path / "maintenance.json").unlink()
            assert client.get("/api/v1/health", headers=header).status_code == 503


def test_loopback_probe_refuses_auth_cookies_and_all_proxy_headers(tmp_path):
    header = activation_marker(tmp_path)
    with open(tmp_path / "admission.lock", "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with TestClient(probe_application(tmp_path), client=("127.0.0.1", 50001)) as client:
            for forbidden, value in (("Authorization", "Bearer valid-but-not-for-probes"), ("Authorization", ""),
                                     ("Cookie", "session=private"), ("Cookie", ""),
                                     ("Forwarded", "for=127.0.0.1"), ("Forwarded", ""),
                                     ("X-Forwarded-For", "127.0.0.1"), ("x-forwarded-proto", "http"),
                                     ("X-Forwarded-Host", "localhost"), ("X-Forwarded-Custom", "")):
                supplied = {**header, forbidden: value}
                for path in ("/api/v1/health", "/api/cad/features"):
                    assert client.get(path, headers=supplied).status_code == 503
                assert client.get("/api/v1/operations/readiness", headers=supplied).json()["maintenance"] is True
            assert client.get("/api/v1/health", headers=header).status_code == 200


def test_probe_marker_partial_oversized_or_symlink_is_not_authority(tmp_path):
    header = activation_marker(tmp_path)
    with TestClient(probe_application(tmp_path), client=("127.0.0.1", 50001)) as client:
        for data in (b'{"format":', b'{"token":"' + b'a' * 8193 + b'"}', b'null', b'[]', b'\xff'):
            (tmp_path / "maintenance.json").write_bytes(data)
            assert client.get("/api/v1/health", headers=header).status_code == 503
        (tmp_path / "maintenance.json").unlink()
        target = tmp_path / "different-file.json"
        target.write_text(json.dumps({"format": "joyniu-currentcad-activation-v1", "token": "a1" * 16}))
        (tmp_path / "maintenance.json").symlink_to(target)
        assert client.get("/api/v1/health", headers=header).status_code == 503


def test_probe_preserves_real_workbench_account_authentication_under_exclusive_lock(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.platform import AuthService
    from app.native_drawing_api import create_native_drawing_router
    from app.cad_design_workspace_api import create_cad_design_workspace_router
    from app.cad_feature_workspace_api import create_cad_feature_workspace_router
    from app.delivery_workspace_api import create_delivery_workspace_router

    auth = AuthService(tmp_path / "accounts.sqlite3", token_secret="maintenance-probe-real-router-test" * 2)
    services = SimpleNamespace(auth=auth)
    app = FastAPI()
    designs = create_cad_design_workspace_router(services, root=tmp_path / "designs")
    features = create_cad_feature_workspace_router(services, root=tmp_path / "features", design_store=designs.design_store)
    app.include_router(designs)
    app.include_router(features)
    app.include_router(create_native_drawing_router(services))
    app.include_router(create_delivery_workspace_router(services, root=tmp_path / "deliveries",
        engineering_store=designs.design_store, feature_store=features.feature_store))
    operation_dir = tmp_path / "operations"
    operation_dir.mkdir()
    app.add_middleware(OperationsGateMiddleware, directory=operation_dir)
    header = activation_marker(operation_dir)
    try:
        with open(operation_dir / "admission.lock", "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with TestClient(app, client=("127.0.0.1", 51001)) as client:
                for path in ("/api/cad/drawings", "/api/cad/features", "/api/cad/designs", "/api/cad/deliveries"):
                    assert client.get(path).status_code == 503
                    reached = client.get(path, headers=header)
                    assert reached.status_code == 401
                    assert reached.headers["x-joyniu-operations-gate"] == "v1"
                    assert header["X-Joyniu-Maintenance-Probe"] not in reached.text
                    assert client.post(path, headers=header, json={}).status_code == 503
            with TestClient(app, client=("198.51.100.17", 51001)) as client:
                assert client.get("/api/cad/features", headers=header).status_code == 503
    finally:
        auth.close()
