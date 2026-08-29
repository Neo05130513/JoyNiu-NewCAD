from __future__ import annotations

import base64
import hashlib

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.platform_api import build_platform_services, create_platform_router  # noqa: E402


def _client_for(services):
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    return app, TestClient(app)


def _auth_for(services, user) -> dict[str, str]:
    return {"Authorization": f"Bearer {services.auth.issue_token(user).token}"}


def test_platform_openapi_resolves_lazy_request_annotation() -> None:
    """Postponed ``Request`` annotations must not make /openapi.json fail."""

    services = build_platform_services(":memory:", auth_secret="o" * 32)
    app, _client = _client_for(services)
    schema = app.openapi()
    assert "/api/v1/ocr/analyze-bytes" in schema["paths"]
    assert "/api/v1/pdm/projects/{project_id}/members" in schema["paths"]


def test_cam_snapshot_routes_are_admin_only_and_round_trip() -> None:
    services = build_platform_services(":memory:", auth_secret="q" * 32)
    admin = services.auth.create_user(
        "snapshot-admin@example.com", "a-very-long-password", "Admin", roles=["admin"]
    )
    designer = services.auth.create_user(
        "snapshot-designer@example.com", "a-very-long-password", "Designer", roles=["designer"]
    )
    app, client = _client_for(services)
    designer_auth = _auth_for(services, designer)
    admin_auth = _auth_for(services, admin)
    plan = client.post(
        "/api/v1/cam/plans",
        headers=designer_auth,
        json={"geometryHash": "q" * 64, "stock": {"length": 100, "width": 50, "height": 10}},
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert client.post(
        f"/api/v1/cam/plans/{plan_id}/operations",
        headers=designer_auth,
        json={"operationType": "facing", "toolId": "T10", "depth": 1},
    ).status_code == 201
    simulation = client.post(
        f"/api/v1/cam/plans/{plan_id}/simulate", headers=designer_auth, json={}
    )
    assert simulation.status_code == 200, simulation.text
    assert client.get(f"/api/v1/cam/plans/{plan_id}", headers=designer_auth).status_code == 200
    assert client.get(
        f"/api/v1/cam/simulations/{simulation.json()['id']}", headers=designer_auth
    ).status_code == 200
    viewer = services.auth.create_user(
        "snapshot-viewer@example.com", "a-very-long-password", "Viewer", roles=["viewer"]
    )
    viewer_auth = _auth_for(services, viewer)
    assert client.get("/api/v1/cam/snapshot", headers=viewer_auth).status_code == 403
    assert client.get(f"/api/v1/cam/plans/{plan_id}", headers=viewer_auth).status_code == 403
    exported = client.get("/api/v1/cam/snapshot", headers=admin_auth)
    assert exported.status_code == 200, exported.text
    assert any(item["id"] == plan.json()["id"] for item in exported.json()["plans"])
    restored = client.post(
        "/api/v1/cam/snapshot/restore",
        headers=admin_auth,
        json={"snapshot": exported.json(), "replace": True},
    )
    assert restored.status_code == 200, restored.text
    assert any(item["id"] == plan.json()["id"] for item in restored.json()["plans"])


def test_platform_router_auth_pdm_ocr_and_cam_flow() -> None:
    # This request intentionally uses synthetic bytes with a known fixture id;
    # fixture-only tests must opt in to the explicit unverified override.
    services = build_platform_services(
        ":memory:", auth_secret="z" * 32, allow_unverified_fixture=True
    )
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
    assert ocr.json()["engine"] == "deterministic-fixture-unverified"
    assert any("smoke testing" in item for item in ocr.json()["warnings"])

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

    # The payload is a deterministic fixture smoke body rather than the source
    # image, so opt in explicitly; real upload/API instances keep this false.
    services = build_platform_services(
        ":memory:", auth_secret="w" * 32, allow_unverified_fixture=True
    )
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
    # Recognition evidence is copied into durable PDM metadata; the in-memory
    # OCR index is only a request-time hand-off and may be lost on restart.
    source_metadata = payload["pdm"]["sourceDocument"]["metadata"]
    assert source_metadata["recognition"]["sourceSha256"] == payload["recognition"]["sourceSha256"]
    assert source_metadata["recognition"]["id"] == payload["recognition"]["id"]
    assert source_metadata["recognition"]["dimensions"]
    assert source_metadata["recognition"]["status"] == "confirmed"
    manifest = client.get(payload["next"]["manifestPath"], headers=auth)
    assert manifest.status_code == 200, manifest.text
    assert len(manifest.json()["documents"]) == 4


def test_designer_upload_reviewer_confirm_then_resume_by_recognition_id() -> None:
    """A confirmed recognition can be resumed by the modeling actor."""

    services = build_platform_services(":memory:", auth_secret="r2" * 16)
    source = b"reviewable drawing payload"
    source_sha256 = hashlib.sha256(source).hexdigest()
    # Use a non-verified fixture to model a real OCR result with a known part
    # recipe.  It must remain pending until the reviewer endpoint is called.
    services.ocr.fixtures["review_fixture"] = {
        "fixture_id": "review_fixture",
        "source_sha256": source_sha256,
        "part_type": "bracket",
        "verified": False,
        "confidence": 0.86,
        "dimensions": [],
        "features": [],
        "model_recipe": {
            "parameters": {
                "baseLength": 100,
                "baseWidth": 50,
                "baseThickness": 10,
                "upperLength": 70,
                "upperWidth": 30,
                "upperHeight": 30,
                "totalHeight": 40,
                "notchOpening": 40,
                "notchRadius": 15,
                "bossDiameter": 20,
                "bossCenterDistance": 70,
            }
        },
        "unresolved": [],
    }
    designer = services.auth.create_user(
        "resume-designer@example.com", "a-very-long-password", "Designer", roles=["designer"]
    )
    reviewer = services.auth.create_user(
        "resume-reviewer@example.com", "a-very-long-password", "Reviewer", roles=["reviewer"]
    )
    _app, client = _client_for(services)
    designer_auth = _auth_for(services, designer)
    reviewer_auth = _auth_for(services, reviewer)
    encoded = base64.b64encode(source).decode()

    analyzed = client.post(
        "/api/v1/ocr/analyze",
        headers=designer_auth,
        json={"imageBase64": encoded, "filename": "review.png", "fixtureId": "review_fixture"},
    )
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()["status"] == "needs_review"
    recognition_id = analyzed.json()["id"]

    confirmed = client.post(
        f"/api/v1/ocr/{recognition_id}/confirm",
        headers=reviewer_auth,
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    from app.geometry import cadquery_status

    resumed = client.post(
        "/api/v1/workflows/drawing-to-model",
        headers=designer_auth,
        json={
            "recognitionId": recognition_id,
            "imageBase64": encoded,
            "filename": "review.png",
            "formats": ["glb"],
            "requireCadQuery": bool(cadquery_status()["available"]),
        },
    )
    assert resumed.status_code == 201, resumed.text
    payload = resumed.json()
    assert payload["recognition"]["id"] == recognition_id
    assert payload["recognition"]["status"] == "confirmed"
    assert payload["pdm"]["sourceVersion"]["sha256"] == source_sha256

    mismatched = client.post(
        "/api/v1/workflows/drawing-to-model",
        headers=designer_auth,
        json={
            "recognitionId": recognition_id,
            "imageBase64": base64.b64encode(b"different drawing").decode(),
            "filename": "review.png",
            "formats": ["glb"],
        },
    )
    assert mismatched.status_code == 409, mismatched.text


def test_pdm_and_project_linked_cam_are_isolated_between_users() -> None:
    """A globally privileged non-admin still needs project membership."""

    services = build_platform_services(":memory:", auth_secret="i" * 32)
    owner = services.auth.create_user(
        "owner@example.com", "a-very-long-password", "Owner", roles=["designer"]
    )
    outsider = services.auth.create_user(
        "outsider@example.com",
        "a-very-long-password",
        "Outsider",
        # Supply every non-admin workflow permission so each 403 below proves
        # project isolation rather than a generic missing-role check.
        roles=["designer", "reviewer", "manufacturing"],
    )
    app, client = _client_for(services)
    owner_auth = _auth_for(services, owner)
    outsider_auth = _auth_for(services, outsider)

    malformed = client.post(
        "/api/v1/pdm/projects",
        headers=owner_auth,
        json={"name": "Bad metadata", "metadata": ["not", "an", "object"]},
    )
    assert malformed.status_code == 422, malformed.text

    project = client.post(
        "/api/v1/pdm/projects",
        headers=owner_auth,
        json={"name": "Owner project", "metadata": {"purpose": "isolation-test"}},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    document = client.post(
        f"/api/v1/pdm/projects/{project_id}/documents",
        headers=owner_auth,
        json={"name": "private.step", "kind": "part"},
    )
    assert document.status_code == 201, document.text
    document_id = document.json()["id"]
    version = client.post(
        f"/api/v1/pdm/documents/{document_id}/versions",
        headers=owner_auth,
        json={"contentText": "PRIVATE-STEP", "fileName": "private.step"},
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]

    outsider_projects = client.get("/api/v1/pdm/projects", headers=outsider_auth)
    assert outsider_projects.status_code == 200, outsider_projects.text
    assert project_id not in {item["id"] for item in outsider_projects.json()["items"]}

    read_requests = [
        ("get", f"/api/v1/pdm/projects/{project_id}/manifest", None),
        ("get", f"/api/v1/pdm/projects/{project_id}/documents", None),
        ("get", f"/api/v1/pdm/projects/{project_id}/members", None),
        ("get", f"/api/v1/pdm/documents/{document_id}/versions", None),
        ("get", f"/api/v1/pdm/versions/{version_id}/content", None),
    ]
    for method, path, body in read_requests:
        response = client.request(method, path, headers=outsider_auth, json=body)
        assert response.status_code == 403, (path, response.text)

    write_requests = [
        (
            "post",
            f"/api/v1/pdm/projects/{project_id}/documents",
            {"name": "intruder.step", "kind": "part"},
        ),
        ("patch", f"/api/v1/pdm/projects/{project_id}", {"name": "Hijacked"}),
        ("patch", f"/api/v1/pdm/documents/{document_id}", {"name": "stolen.step"}),
        (
            "post",
            f"/api/v1/pdm/documents/{document_id}/versions",
            {"contentText": "INTRUDER"},
        ),
        ("patch", f"/api/v1/pdm/documents/{document_id}/status", {"status": "in_review"}),
        ("delete", f"/api/v1/pdm/documents/{document_id}", None),
        (
            "put",
            f"/api/v1/pdm/projects/{project_id}/members",
            {"members": [outsider.id]},
        ),
    ]
    for method, path, body in write_requests:
        response = client.request(method, path, headers=outsider_auth, json=body)
        assert response.status_code == 403, (path, response.text)

    # Restore resolves the deleted document's project before mutating it.
    deleted = client.delete(f"/api/v1/pdm/documents/{document_id}", headers=owner_auth)
    assert deleted.status_code == 200, deleted.text
    denied_restore = client.post(
        f"/api/v1/pdm/documents/{document_id}/restore", headers=outsider_auth
    )
    assert denied_restore.status_code == 403, denied_restore.text
    assert client.post(
        f"/api/v1/pdm/documents/{document_id}/restore", headers=owner_auth
    ).status_code == 200

    plan = client.post(
        "/api/v1/cam/plans",
        headers=owner_auth,
        json={
            "projectId": project_id,
            "sourceDocumentId": document_id,
            "sourceVersionId": version_id,
            "geometryHash": version.json()["sha256"],
            "stock": {"length": 110, "width": 60, "height": 45},
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    denied_plan = client.post(
        "/api/v1/cam/plans",
        headers=outsider_auth,
        json={
            "projectId": project_id,
            "geometryHash": "x" * 64,
            "stock": {"length": 110, "width": 60, "height": 45},
        },
    )
    assert denied_plan.status_code == 403, denied_plan.text
    assert client.get(
        "/api/v1/cam/plans", headers=outsider_auth, params={"project_id": project_id}
    ).status_code == 403
    all_outsider_plans = client.get("/api/v1/cam/plans", headers=outsider_auth)
    assert all_outsider_plans.status_code == 200, all_outsider_plans.text
    assert plan_id not in {item["id"] for item in all_outsider_plans.json()["items"]}

    cam_requests = [
        (
            "post",
            f"/api/v1/cam/plans/{plan_id}/operations",
            {"operationType": "facing", "toolId": "T10", "depth": 1},
        ),
        ("post", f"/api/v1/cam/plans/{plan_id}/simulate", {}),
        ("get", f"/api/v1/cam/plans/{plan_id}/gate", None),
        ("post", f"/api/v1/cam/plans/{plan_id}/approve", {}),
        ("post", f"/api/v1/cam/plans/{plan_id}/release", {}),
    ]
    for method, path, body in cam_requests:
        response = client.request(method, path, headers=outsider_auth, json=body)
        assert response.status_code == 403, (path, response.text)

    # A read-only member can inspect, but cannot create documents or edit the
    # plan.  Changing the entry to a bare id grants write access.
    member_update = client.put(
        f"/api/v1/pdm/projects/{project_id}/members",
        headers=owner_auth,
        json={"members": [{"userId": outsider.id, "access": "read"}]},
    )
    assert member_update.status_code == 200, member_update.text
    assert client.get(
        f"/api/v1/pdm/projects/{project_id}/manifest", headers=outsider_auth
    ).status_code == 200
    assert client.post(
        f"/api/v1/pdm/projects/{project_id}/documents",
        headers=outsider_auth,
        json={"name": "read-only.step", "kind": "part"},
    ).status_code == 403
    writable_update = client.put(
        f"/api/v1/pdm/projects/{project_id}/members",
        headers=owner_auth,
        json={"members": [outsider.id]},
    )
    assert writable_update.status_code == 200, writable_update.text
    member_document = client.post(
        f"/api/v1/pdm/projects/{project_id}/documents",
        headers=outsider_auth,
        json={"name": "member.step", "kind": "part"},
    )
    assert member_document.status_code == 201, member_document.text


def test_project_read_members_can_review_and_release_cam_without_project_write() -> None:
    """Reviewer/manufacturing roles use read scope plus their CAM permission."""

    services = build_platform_services(":memory:", auth_secret="m" * 32)
    designer = services.auth.create_user(
        "scope-designer@example.com", "a-very-long-password", "Designer", roles=["designer"]
    )
    reviewer = services.auth.create_user(
        "scope-reviewer@example.com", "a-very-long-password", "Reviewer", roles=["reviewer"]
    )
    manufacturer = services.auth.create_user(
        "scope-manufacturer@example.com",
        "a-very-long-password",
        "Manufacturer",
        roles=["manufacturing"],
    )
    _app, client = _client_for(services)
    designer_auth = _auth_for(services, designer)
    reviewer_auth = _auth_for(services, reviewer)
    manufacturer_auth = _auth_for(services, manufacturer)

    project = client.post(
        "/api/v1/pdm/projects",
        headers=designer_auth,
        json={
            "name": "Scoped CAM",
            "metadata": {
                "members": [
                    {"userId": reviewer.id, "access": "read"},
                    {"userId": manufacturer.id, "access": "read"},
                ]
            },
        },
    )
    assert project.status_code == 201, project.text
    plan = client.post(
        "/api/v1/cam/plans",
        headers=designer_auth,
        json={
            "projectId": project.json()["id"],
            "geometryHash": "c" * 64,
            "stock": {"length": 110, "width": 60, "height": 45},
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    assert client.post(
        f"/api/v1/cam/plans/{plan_id}/operations",
        headers=designer_auth,
        json={"operationType": "facing", "toolId": "T10", "depth": 1},
    ).status_code == 201
    simulation = client.post(
        f"/api/v1/cam/plans/{plan_id}/simulate", headers=designer_auth, json={}
    )
    assert simulation.status_code == 200, simulation.text
    approval = client.post(
        f"/api/v1/cam/plans/{plan_id}/approve",
        headers=reviewer_auth,
        json={"simulationId": simulation.json()["id"]},
    )
    assert approval.status_code == 201, approval.text
    release = client.post(
        f"/api/v1/cam/plans/{plan_id}/release",
        headers=manufacturer_auth,
        json={},
    )
    assert release.status_code == 200, release.text

    # Document review/release follows the same project-read ACL.  The
    # reviewer need not be granted the designer's PROJECT_WRITE permission.
    document = client.post(
        f"/api/v1/pdm/projects/{project.json()['id']}/documents",
        headers=designer_auth,
        json={"name": "scoped-model.step", "kind": "part"},
    )
    assert document.status_code == 201, document.text
    document_id = document.json()["id"]
    version = client.post(
        f"/api/v1/pdm/documents/{document_id}/versions",
        headers=designer_auth,
        json={"contentText": "MODEL"},
    )
    assert version.status_code == 201, version.text
    reviewed = client.patch(
        f"/api/v1/pdm/documents/{document_id}/status",
        headers=reviewer_auth,
        json={"status": "in_review"},
    )
    assert reviewed.status_code == 200, reviewed.text
    released = client.patch(
        f"/api/v1/pdm/documents/{document_id}/status",
        headers=reviewer_auth,
        json={"status": "released"},
    )
    assert released.status_code == 200, released.text


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
    reviewer_list = client.get("/api/v1/cam/plans", headers=reviewer_auth)
    assert reviewer_list.status_code == 200, reviewer_list.text
    assert any(item["id"] == plan_id for item in reviewer_list.json()["items"])

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
    assert client.get(f"/api/v1/cam/plans/{plan_id}", headers=reviewer_auth).status_code == 200
    assert client.get(
        f"/api/v1/cam/simulations/{simulation.json()['id']}", headers=reviewer_auth
    ).status_code == 200
    assert client.get(f"/api/v1/cam/plans/{plan_id}/gate", headers=designer_auth).status_code == 200
    assert client.get(f"/api/v1/cam/plans/{plan_id}/gate", headers=viewer_auth).status_code == 403
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
    assert client.get(f"/api/v1/cam/plans/{plan_id}/gate", headers=reviewer_auth).status_code == 200

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
    nc_id = release.json()["id"]
    nc_info = client.get(f"/api/v1/cam/nc/{nc_id}/info", headers=manufacturer_auth)
    assert nc_info.status_code == 200, nc_info.text
    assert nc_info.json()["id"] == nc_id
    assert "text" not in nc_info.json()


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
