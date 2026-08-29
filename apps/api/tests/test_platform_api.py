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


def test_cam_routes_enforce_role_separation_and_release_gate() -> None:
    """CAM mutations, approval and NC release must honor RBAC boundaries."""

    services = build_platform_services(":memory:", auth_secret="r" * 32)
    # Seed accounts directly so this test focuses on CAM authorization rather
    # than the bootstrap-account flow.
    users = {
        "viewer": services.auth.create_user(
            "viewer@example.com", "a-very-long-password", "Viewer", roles=["viewer"]
        ),
        "designer": services.auth.create_user(
            "designer@example.com", "a-very-long-password", "Designer", roles=["designer"]
        ),
        "reviewer": services.auth.create_user(
            "reviewer@example.com", "a-very-long-password", "Reviewer", roles=["reviewer"]
        ),
        "manufacturer": services.auth.create_user(
            "manufacturer@example.com",
            "a-very-long-password",
            "Manufacturer",
            roles=["manufacturing"],
        ),
    }

    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)

    def auth_for(name: str) -> dict[str, str]:
        user = users[name]
        token = services.auth.issue_token(user).token
        return {"Authorization": f"Bearer {token}"}

    viewer_auth = auth_for("viewer")
    designer_auth = auth_for("designer")
    reviewer_auth = auth_for("reviewer")
    manufacturer_auth = auth_for("manufacturer")

    # Viewers cannot create plans, mutate operations, or run simulation.
    denied_plan = client.post(
        "/api/v1/cam/plans",
        headers=viewer_auth,
        json={"geometryHash": "v" * 64, "stock": {"length": 110, "width": 60, "height": 45}},
    )
    assert denied_plan.status_code == 403, denied_plan.text

    created_plan = client.post(
        "/api/v1/cam/plans",
        headers=designer_auth,
        json={"geometryHash": "d" * 64, "stock": {"length": 110, "width": 60, "height": 45}},
    )
    assert created_plan.status_code == 201, created_plan.text
    plan_id = created_plan.json()["id"]

    denied_operation = client.post(
        f"/api/v1/cam/plans/{plan_id}/operations",
        headers=viewer_auth,
        json={"operationType": "facing", "toolId": "T10", "depth": 1},
    )
    assert denied_operation.status_code == 403, denied_operation.text
    added = client.post(
        f"/api/v1/cam/plans/{plan_id}/operations",
        headers=designer_auth,
        json={"operationType": "facing", "toolId": "T10", "depth": 1},
    )
    assert added.status_code == 201, added.text

    denied_simulation = client.post(
        f"/api/v1/cam/plans/{plan_id}/simulate", headers=viewer_auth, json={}
    )
    assert denied_simulation.status_code == 403, denied_simulation.text
    simulation = client.post(
        f"/api/v1/cam/plans/{plan_id}/simulate", headers=designer_auth, json={}
    )
    assert simulation.status_code == 200, simulation.text
    simulation_id = simulation.json()["id"]

    # Only a reviewer/admin can approve.  A designer and manufacturing account
    # must not be able to self-approve the plan they may later release.
    designer_approval = client.post(
        f"/api/v1/cam/plans/{plan_id}/approve",
        headers=designer_auth,
        json={"simulationId": simulation_id},
    )
    assert designer_approval.status_code == 403, designer_approval.text
    manufacturer_approval = client.post(
        f"/api/v1/cam/plans/{plan_id}/approve",
        headers=manufacturer_auth,
        json={"simulationId": simulation_id},
    )
    assert manufacturer_approval.status_code == 403, manufacturer_approval.text
    approval = client.post(
        f"/api/v1/cam/plans/{plan_id}/approve",
        headers=reviewer_auth,
        json={"simulationId": simulation_id, "role": "reviewer"},
    )
    assert approval.status_code == 201, approval.text

    # Reviewer cannot release, and the manufacturing release succeeds only
    # after the independent reviewer approval above.
    reviewer_release = client.post(
        f"/api/v1/cam/plans/{plan_id}/release",
        headers=reviewer_auth,
        json={},
    )
    assert reviewer_release.status_code == 403, reviewer_release.text
    release = client.post(
        f"/api/v1/cam/plans/{plan_id}/release",
        headers=manufacturer_auth,
        json={"includeText": True},
    )
    assert release.status_code == 200, release.text
    assert release.json()["status"] == "released"
    assert release.json()["text"].startswith("%\n")


def test_cam_http_gate_rejects_same_account_approval_and_release() -> None:
    """Even a dual-role account must use a distinct reviewer identity."""

    services = build_platform_services(":memory:", auth_secret="s" * 32)
    dual = services.auth.create_user(
        "dual@example.com",
        "a-very-long-password",
        "Dual role",
        roles=["reviewer", "manufacturing"],
    )
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {services.auth.issue_token(dual).token}"}
    plan = client.post(
        "/api/v1/cam/plans",
        headers=auth,
        json={"geometryHash": "s" * 64, "stock": {"length": 110, "width": 60, "height": 45}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert client.post(
        f"/api/v1/cam/plans/{plan_id}/operations",
        headers=auth,
        json={"operationType": "facing", "toolId": "T10", "depth": 1},
    ).status_code == 201
    simulation = client.post(
        f"/api/v1/cam/plans/{plan_id}/simulate", headers=auth, json={}
    )
    assert simulation.status_code == 200, simulation.text
    approval = client.post(
        f"/api/v1/cam/plans/{plan_id}/approve",
        headers=auth,
        json={"simulationId": simulation.json()["id"]},
    )
    assert approval.status_code == 201, approval.text
    release = client.post(f"/api/v1/cam/plans/{plan_id}/release", headers=auth, json={})
    assert release.status_code == 409, release.text
    assert "separate_reviewer" in str(release.json())
