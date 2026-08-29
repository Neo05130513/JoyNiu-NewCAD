from __future__ import annotations

import base64

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.platform_api import build_platform_services, create_platform_router  # noqa: E402


def test_platform_router_auth_pdm_ocr_and_cam_flow() -> None:
    services = build_platform_services(":memory:", auth_secret="z" * 32)
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)

    created = client.post(
        "/api/v1/auth/users",
        json={
            "email": "designer@example.com",
            "password": "a-very-long-password",
            "displayName": "Designer",
            "roles": ["designer"],
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "designer@example.com", "password": "a-very-long-password"},
    )
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    project = client.post("/api/v1/pdm/projects", headers=auth, json={"name": "Demo"})
    assert project.status_code == 201, project.text
    document = client.post(
        f"/api/v1/pdm/projects/{project.json()['id']}/documents",
        headers=auth,
        json={"name": "model.json", "kind": "model"},
    )
    assert document.status_code == 201, document.text
    version = client.post(
        f"/api/v1/pdm/documents/{document.json()['id']}/versions",
        headers=auth,
        json={"contentText": "{}", "fileName": "model.json", "contentType": "application/json"},
    )
    assert version.status_code == 201, version.text
    assert version.json()["revision"] == 1

    # Designer is allowed to run OCR/CAM planning, but not release NC.
    ocr = client.post(
        "/api/v1/ocr/analyze",
        headers=auth,
        json={"imageBase64": base64.b64encode(b"unknown").decode(), "fixtureId": "bracket_support_v1"},
    )
    assert ocr.status_code == 200, ocr.text
    assert ocr.json()["engine"] == "deterministic-fixture"

    plan = client.post(
        "/api/v1/cam/plans",
        headers=auth,
        json={"geometryHash": "a" * 64, "stock": {"length": 100, "width": 50, "height": 10}},
    )
    assert plan.status_code == 201, plan.text
    operation = client.post(
        f"/api/v1/cam/plans/{plan.json()['id']}/operations",
        headers=auth,
        json={"operationType": "facing", "toolId": "T10", "depth": 1, "pathLength": 100},
    )
    assert operation.status_code == 201, operation.text
    simulation = client.post(f"/api/v1/cam/plans/{plan.json()['id']}/simulate", headers=auth, json={})
    assert simulation.status_code == 200, simulation.text
    assert simulation.json()["passed"] is True


def test_drawing_to_model_workflow_persists_source_parameters_and_artifacts() -> None:
    """The customer-facing one-call flow must leave an auditable PDM trail."""

    services = build_platform_services(":memory:", auth_secret="w" * 32)
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)
    created = client.post(
        "/api/v1/auth/users",
        json={
            "email": "workflow@example.com",
            "password": "a-very-long-password",
            "displayName": "Workflow Designer",
            "roles": ["designer"],
        },
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "workflow@example.com", "password": "a-very-long-password"},
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    # CadQuery/OCCT is an optional extra.  Exercise the same auditable
    # workflow on minimal CI hosts by allowing the documented faceted
    # fallback; when the kernel is installed the request still verifies the
    # strict B-Rep path.
    from app.geometry import cadquery_status

    require_cadquery = bool(cadquery_status()["available"])
    response = client.post(
        "/api/v1/workflows/drawing-to-model",
        headers=auth,
        json={
            "imageBase64": base64.b64encode(b"fixture payload").decode(),
            "filename": "support.jpg",
            "fixtureId": "bracket_support_v1",
            "formats": ["step", "glb"],
            "requireCadQuery": require_cadquery,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["recognition"]["status"] == "confirmed"
    assert payload["validation"]["valid"] is True
    assert len(payload["pdm"]["artifacts"]) == 2
    manifest = client.get(payload["next"]["manifestPath"], headers=auth)
    assert manifest.status_code == 200, manifest.text
    assert len(manifest.json()["documents"]) == 4
