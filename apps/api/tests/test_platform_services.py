from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from app.cam import CAMGateRejected, CAMService, StockDefinition
from app.ocr import OCRService
from app.platform_api import build_platform_services
from app.platform import (
    AuthService,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    PDMRepository,
    Permission,
    ValidationError,
)


def test_pdm_versions_are_immutable_and_optimistic() -> None:
    pdm = PDMRepository()
    with pytest.raises(ValidationError):
        # Metadata is an object contract, not an arbitrary JSON array.
        pdm.create_project("Invalid metadata", "usr-owner", metadata=["bad"])  # type: ignore[arg-type]
    project = pdm.create_project("Fixture", "usr-owner")
    document = pdm.create_document(project.id, "bracket.step", "part", "usr-owner")
    first = pdm.create_version(document.id, b"STEP-V1", "usr-owner", file_name="bracket.step")
    assert first.revision == 1
    assert first.sha256 == hashlib.sha256(b"STEP-V1").hexdigest()
    assert pdm.get_version_content(first.id) == b"STEP-V1"
    with pytest.raises(ConflictError):
        pdm.create_version(
            document.id,
            b"stale",
            "usr-owner",
            expected_current_revision=0,
        )
    second = pdm.create_version(
        document.id,
        {"parameters": {"baseLength": 100}},
        "usr-owner",
        expected_current_revision=1,
    )
    assert second.revision == 2
    assert [item.revision for item in pdm.list_versions(document.id)] == [2, 1]


def test_pdm_status_transition_requires_a_version() -> None:
    pdm = PDMRepository()
    project = pdm.create_project("Status", "u")
    document = pdm.create_document(project.id, "model", "model", "u")
    with pytest.raises(ConflictError):
        pdm.change_document_status(document.id, "released", actor_id="reviewer")
    pdm.create_version(document.id, b"model", "u")
    assert pdm.change_document_status(document.id, "in_review", actor_id="reviewer").status == "in_review"
    assert pdm.change_document_status(document.id, "released", actor_id="reviewer").status == "released"


def test_rbac_token_and_deactivation() -> None:
    auth = AuthService(token_secret="s" * 32)
    user = auth.create_user("Designer@Example.com", "a-very-long-password", "Designer", roles=["designer"])
    token = auth.authenticate("designer@example.com", "a-very-long-password")
    assert auth.verify_token(token.token).id == user.id
    assert auth.has_permission(user, Permission.CAM_SIMULATE)
    with pytest.raises(AuthorizationError):
        auth.require(user, Permission.CAM_RELEASE)
    auth.set_active(user.id, False, actor_id="admin")
    with pytest.raises(AuthenticationError):
        auth.verify_token(token.token)


def test_ocr_acceptance_fixture_is_deterministic() -> None:
    image_path = Path(
        "/var/folders/7l/7gm7qtgj14d4gyp7f34q1yl40000gn/T/"
        "codex-clipboard-6532c96b-cdb4-4481-8ffc-b1ad27ad26ac.jpg"
    )
    payload = image_path.read_bytes() if image_path.exists() else b"fixture-bytes"
    result = OCRService().analyze(payload, filename="acceptance.jpg", fixture_id="bracket_support_v1")
    fields = {item.field: item.value for item in result.dimensions}
    assert result.status == "confirmed"
    assert result.fixture_id == "bracket_support_v1"
    assert fields["base_length"] == 100
    assert fields["notch_radius"] == 15
    assert fields["boss_diameter"] == 20
    request = result.to_geometry_request()
    assert request["parameters"]["baseLength"] == 100
    assert request["parameters"]["notchOpening"] == 40


def test_unknown_ocr_never_claims_confirmation() -> None:
    result = OCRService().analyze(b"not-an-image", filename="unknown.bin")
    assert result.status == "needs_review"
    assert result.engine == "none"
    assert result.unresolved
    # A reviewer cannot turn an unknown topology into a bracket merely by
    # clicking confirm; a known part recipe is required first.
    with pytest.raises(ValidationError):
        OCRService().confirm(result, reviewer_id="reviewer-1")


def test_fixture_id_cannot_spoof_another_drawing_sha() -> None:
    """Known fixture ids are ignored unless the uploaded SHA matches."""

    result = OCRService().analyze(
        b"synthetic bytes that are not the acceptance drawing",
        filename="spoof.jpg",
        fixture_id="bracket_support_v1",
    )
    assert result.status == "needs_review"
    assert result.fixture_id is None
    assert result.engine != "deterministic-fixture"

    # Fixture-only test harnesses can make the override explicit, and the
    # response remains visibly labelled as unverified.
    smoke = OCRService(allow_unverified_fixture=True).analyze(
        b"synthetic bytes that are not the acceptance drawing",
        filename="spoof.jpg",
        fixture_id="bracket_support_v1",
    )
    assert smoke.engine == "deterministic-fixture-unverified"
    assert any("smoke testing" in warning for warning in smoke.warnings)


def _cam_happy_path() -> tuple[CAMService, str, str]:
    cam = CAMService()
    plan = cam.create_plan(
        actor_id="designer",
        geometry_hash="a" * 64,
        stock=StockDefinition(110, 60, 45),
    )
    cam.add_operation(
        plan.id,
        actor_id="designer",
        operation_type="facing",
        tool_id="T10",
        depth=1,
        path_length=100,
    )
    simulation = cam.simulate(plan.id, actor_id="designer")
    return cam, plan.id, simulation.id


def test_cam_release_requires_matching_reviewer_and_simulation() -> None:
    cam, plan_id, simulation_id = _cam_happy_path()
    with pytest.raises(CAMGateRejected):
        cam.release_nc(
            plan_id,
            actor_id="manufacturer",
            actor_permissions=[Permission.CAM_RELEASE.value],
        )
    cam.approve(plan_id, actor_id="reviewer", simulation_id=simulation_id)
    nc = cam.release_nc(
        plan_id,
        actor_id="manufacturer",
        actor_permissions=[Permission.CAM_RELEASE.value],
    )
    assert nc.status == "released"
    assert nc.text.startswith("%\n")
    assert "SIMULATION" in nc.text


def test_cam_collision_blocks_release() -> None:
    cam = CAMService()
    plan = cam.create_plan(
        actor_id="designer",
        geometry_hash="b" * 64,
        stock={"length": 100, "width": 50, "height": 10},
    )
    cam.add_operation(
        plan.id,
        actor_id="designer",
        operation_type="profile",
        tool_id="T06",
        depth=2,
        parameters={"collision": True},
    )
    simulation = cam.simulate(plan.id, actor_id="designer")
    assert not simulation.passed
    decision = cam.gate_status(plan.id)
    assert not decision.passed
    assert any("simulation" in reason for reason in decision.reasons)


def test_cam_snapshot_round_trip_persists_full_state_across_restart() -> None:
    """Plans, operations, simulations, approvals and NC survive a restart."""

    with tempfile.TemporaryDirectory(prefix="joyniu-cam-") as directory:
        database = str(Path(directory) / "platform.sqlite3")
        first = CAMService(database=database)
        plan = first.create_plan(
            actor_id="designer",
            geometry_hash="persist" * 10 + "aa",
            stock=StockDefinition(110, 60, 45),
            project_id="prj_persist",
        )
        first.add_operation(
            plan.id,
            actor_id="designer",
            operation_type="facing",
            tool_id="T10",
            depth=1,
            path_length=100,
        )
        simulation = first.simulate(plan.id, actor_id="designer")
        approval = first.approve(plan.id, actor_id="reviewer", simulation_id=simulation.id)
        program = first.release_nc(
            plan.id,
            actor_id="manufacturer",
            actor_permissions=[Permission.CAM_RELEASE.value],
        )
        exported = first.snapshot()
        assert exported["plans"][0]["id"] == plan.id
        assert exported["simulations"][0]["id"] == simulation.id
        assert exported["ncPrograms"][0]["id"] == program.id
        first.close()

        restarted = CAMService(database=database)
        restored_plan = restarted.get_plan(plan.id)
        assert restored_plan.status == "released"
        assert len(restored_plan.operations) == 1
        assert restored_plan.approvals[0].id == approval.id
        assert restarted.get_simulation(simulation.id).passed is True
        assert restarted.get_nc(program.id).sha256 == program.sha256

        # A portable snapshot can restore into an ephemeral worker too.
        worker = CAMService()
        worker.restore(exported)
        assert worker.get_plan(plan.id).released_nc_id == program.id
        assert worker.get_nc(program.id).text == program.text

        # Invalid imports are rejected before replacing the live state.
        corrupted = dict(exported)
        corrupted["ncPrograms"] = [dict(exported["ncPrograms"][0], sha256="0" * 64)]
        with pytest.raises(ValidationError):
            worker.restore(corrupted)
        assert worker.get_plan(plan.id).released_nc_id == program.id


def test_build_platform_services_restores_cam_with_shared_database() -> None:
    """The normal service graph wires CAM persistence to the PDM DB path."""

    with tempfile.TemporaryDirectory(prefix="joyniu-platform-") as directory:
        database = str(Path(directory) / "platform.sqlite3")
        first = build_platform_services(database, auth_secret="b" * 32)
        designer = first.auth.create_user(
            "persist-designer@example.com",
            "a-very-long-password",
            "Persist designer",
            roles=["designer"],
        )
        plan = first.cam.create_plan(
            actor_id=designer.id,
            geometry_hash="z" * 64,
            stock=StockDefinition(100, 50, 10),
        )
        first.cam.add_operation(
            plan.id,
            actor_id=designer.id,
            operation_type="facing",
            tool_id="T10",
            depth=1,
        )
        first.cam.close()
        first.pdm.close()
        first.auth.close()

        second = build_platform_services(database, auth_secret="b" * 32)
        assert second.auth.get_user(designer.id).email == designer.email
        restored = second.cam.get_plan(plan.id)
        assert restored.created_by == designer.id
        assert restored.operations[0].tool_id == "T10"
        second.cam.close()
        second.pdm.close()
        second.auth.close()


def test_platform_cam_service_reuses_auth_permissions() -> None:
    """The service graph must protect CAM calls even outside HTTP routes."""

    services = build_platform_services(":memory:", auth_secret="p" * 32)
    viewer = services.auth.create_user(
        "viewer-cam@example.com", "a-very-long-password", "Viewer", roles=["viewer"]
    )
    designer = services.auth.create_user(
        "designer-cam@example.com", "a-very-long-password", "Designer", roles=["designer"]
    )
    manufacturer = services.auth.create_user(
        "manufacturer-cam@example.com",
        "a-very-long-password",
        "Manufacturer",
        roles=["manufacturing"],
    )
    assert not services.auth.has_permission(manufacturer, Permission.CAM_APPROVE)
    with pytest.raises(AuthorizationError):
        services.cam.create_plan(
            actor_id=viewer.id,
            geometry_hash="v" * 64,
            stock=StockDefinition(110, 60, 45),
        )
    plan = services.cam.create_plan(
        actor_id=designer.id,
        geometry_hash="p" * 64,
        stock=StockDefinition(110, 60, 45),
    )
    with pytest.raises(AuthorizationError):
        services.cam.add_operation(
            plan.id,
            actor_id=viewer.id,
            operation_type="facing",
            tool_id="T10",
            depth=1,
        )
    with pytest.raises(AuthorizationError):
        services.cam.simulate(plan.id, actor_id=viewer.id)
    with pytest.raises(AuthorizationError):
        services.cam.approve(plan.id, actor_id=manufacturer.id)
