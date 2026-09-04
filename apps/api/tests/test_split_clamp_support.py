from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai_proxy import AIConversationResult, parameter_patch_schema
from app.geometry import cadquery_status
from app.main import app
from app.ocr import _REQUIRED_SPLIT_CLAMP_CANDIDATE_FIELDS
from app.platform_api import build_platform_services, create_platform_router
from app.schemas import SplitClampSupportParameters
from app.split_clamp_support import (
    audit_split_clamp_support,
    build_split_clamp_support_shape,
    generate_split_clamp_support_artifacts,
    validate_split_clamp_support,
)


CLIENT = TestClient(app)
STANDARD = {
    "baseLength": 125,
    "baseWidth": 95,
    "baseThickness": 15,
    "baseMainDepth": 80,
    "frontTongueWidth": 80,
    "rearBridgeWidth": 86,
    "totalHeight": 75,
    "pedestalOuterRadius": 33,
    "pedestalCenterFromRear": 35,
    "pedestalHeight": 40,
    "rearClampRise": 20,
    "boreDiameter": 36,
    "boreFloorZ": 40,
    "splitWidth": 12,
    "mountHoleCount": 2,
    "mountHoleDiameter": 12,
    "mountHoleCenterDistance": 96,
    "mountHoleCenterFromRear": 40,
    "crossHoleDiameter": 12,
    "crossHoleCenterZ": 55,
    "ribHeight": 20,
    "ribThickness": 10,
    "outerCornerRadius": 8,
    "neckConcaveRadius": 5,
    "neckConvexRadius": 8,
    "material": "45# 钢",
    "units": "mm",
}


def _request(**updates):
    return {
        "partType": "split_clamp_support",
        "recipeId": "split_clamp_support_v1",
        "parameters": {**STANDARD, **updates},
        "formats": ["step", "glb"],
    }


def test_mount_hole_rear_datum_is_exposed_to_ai_and_required_for_confirmation() -> None:
    schema = parameter_patch_schema()
    assert schema["properties"]["mountHoleCenterFromRear"] == {
        "anyOf": [{"type": "number"}, {"type": "null"}],
    }
    assert "mountHoleCenterFromRear" in schema["required"]
    assert "mountHoleCenterFromRear" in _REQUIRED_SPLIT_CLAMP_CANDIDATE_FIELDS


def test_standard_split_clamp_dimensions_validate() -> None:
    parameters = SplitClampSupportParameters.model_validate(STANDARD)
    assert parameters.pedestal_center_y == pytest.approx(12.5)
    assert parameters.mount_hole_center_y == pytest.approx(7.5)
    assert parameters.lower_clamp_top_z == pytest.approx(55)
    report = validate_split_clamp_support(
        parameters,
        engine="faceted-fallback",
    )
    assert report["valid"] is True
    assert report["productionReady"] is False
    assert report["metrics"]["bboxLength"] == 125
    assert report["metrics"]["bboxWidth"] == 95
    assert report["metrics"]["bboxHeight"] == 75
    assert report["metrics"]["mountHoleCenterFromRearMeasured"] == 40
    assert report["metrics"]["crossHoleCenterZMeasured"] == 55
    assert report["metrics"]["dProfilePresent"] is True
    assert report["metrics"]["rectangularRearWallPresent"] is True
    assert report["metrics"]["ribsInRearBand"] is True
    assert report["metrics"]["rearBaseCornersSquare"] is True
    assert report["metrics"]["boreFloorZMeasured"] == 40


@pytest.mark.parametrize(
    ("updates", "rule_id"),
    [
        ({"totalHeight": 70}, "stack.total_height"),
        ({"boreDiameter": 68}, "bore.wall"),
        ({"boreFloorZ": 10}, "bore.floor"),
        ({"mountHoleCenterDistance": 120}, "mount_holes.length_clearance"),
        ({"mountHoleCenterFromRear": 94}, "mount_holes.depth_clearance"),
        ({"crossHoleCenterZ": 65}, "cross_hole.step_intersection"),
        ({"crossHoleCenterZ": 73}, "cross_hole.vertical_clearance"),
    ],
)
def test_split_clamp_relationship_errors_are_auditable(updates, rule_id) -> None:
    report = validate_split_clamp_support(
        SplitClampSupportParameters.model_validate({**STANDARD, **updates}),
        engine="faceted-fallback",
    )
    assert report["valid"] is False
    rules = {item["ruleId"]: item for item in report["issues"]}
    assert rules[rule_id]["passed"] is False


def test_generic_model_api_rejects_recipe_mismatch_and_unknown_fields() -> None:
    mismatch = CLIENT.post(
        "/api/v1/models/validate",
        json={**_request(), "partType": "bracket"},
    )
    assert mismatch.status_code == 422
    typo = CLIENT.post(
        "/api/v1/models/validate",
        json=_request(pedestalOuterRadiu=33),
    )
    assert typo.status_code == 422
    assert "pedestalOuterRadiu" in typo.text


def test_generic_model_api_generates_downloadable_step_and_glb() -> None:
    response = CLIENT.post("/api/v1/models/generate", json=_request())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["partType"] == "split_clamp_support"
    assert payload["recipeId"] == "split_clamp_support_v1"
    assert payload["parameters"]["baseLength"] == 125
    assert payload["parameters"]["pedestalOuterRadius"] == 33
    assert payload["parameters"]["boreDiameter"] == 36
    assert payload["parameters"]["splitWidth"] == 12
    assert payload["parameters"]["mountHoleCenterDistance"] == 96
    assert payload["parameters"]["mountHoleCenterFromRear"] == 40
    assert payload["validation"]["metrics"]["splitPresent"] is True
    by_format = {item["format"]: item for item in payload["artifacts"]}
    step = CLIENT.get(by_format["step"]["downloadUrl"])
    glb = CLIENT.get(by_format["glb"]["downloadUrl"])
    assert step.status_code == 200
    assert step.content.startswith(b"ISO-10303-21;")
    assert glb.status_code == 200
    assert glb.content[:4] == b"glTF"
    _magic, version, total_length = struct.unpack("<4sII", glb.content[:12])
    assert version == 2
    assert total_length == len(glb.content)


def test_occt_shape_contains_the_dimensioned_functional_features() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    parameters = SplitClampSupportParameters.model_validate(STANDARD)
    shape = build_split_clamp_support_shape(parameters)
    value = shape.val()
    box = value.BoundingBox()
    assert value.isValid()
    assert len(value.Solids()) == 1
    assert box.xlen == pytest.approx(125, abs=0.01)
    assert box.ylen == pytest.approx(95, abs=0.01)
    assert box.zlen == pytest.approx(75, abs=0.01)
    audit = audit_split_clamp_support(parameters, shape, engine="cadquery-occt")
    assert audit["topologyAuditPassed"] is True
    assert audit["outerCylinderPresent"] is True
    assert audit["dProfilePresent"] is True
    assert audit["lowerDProfileRearCornersPresent"] is True
    assert audit["rectangularRearWallPresent"] is True
    assert audit["centralBorePresent"] is True
    assert audit["boreFloorPresent"] is True
    assert audit["boreFloorZMeasured"] == pytest.approx(40, abs=0.02)
    assert audit["mountHolePairPresent"] is True
    assert audit["mountHoleCenterDistanceMeasured"] == pytest.approx(96, abs=0.01)
    assert audit["mountHoleCenterFromRearMeasured"] == pytest.approx(40, abs=0.01)
    assert audit["crossHolePresent"] is True
    assert audit["crossHoleRearExitPresent"] is True
    assert audit["crossHoleCenterZMeasured"] == pytest.approx(55, abs=0.01)
    assert audit["splitPresent"] is True
    assert audit["splitStartsAtBoreFloor"] is True
    assert audit["splitFloorZMeasured"] == pytest.approx(40, abs=0.02)
    assert audit["ribPairPresent"] is True
    assert audit["ribsInRearBand"] is True
    assert audit["baseFilletsPresent"] is True
    assert audit["outerFilletFaceCount"] == 2
    assert audit["rearCornerFilletFaceCount"] == 0
    assert audit["neckConcaveFilletFaceCount"] == 2
    assert audit["neckConvexFilletFaceCount"] == 2
    assert audit["rearBaseCornersSquare"] is True

    # Plan-view witness points distinguish the drawing's D-profile and
    # rear-edge gussets from the previous full-circle/centreline-rib model.
    for side in (-1, 1):
        assert value.isInside((side * 32, 46.5, 45), 1e-6)
        assert value.isInside((side * 32, 46.5, 65), 1e-6)
        assert value.isInside((side * 38, 42.5, 20), 1e-6)
        assert not value.isInside((side * 38, 12.5, 20), 1e-6)


def test_occt_audit_rejects_missing_rear_wall_or_rear_gusset_material() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    import cadquery as cq  # type: ignore

    parameters = SplitClampSupportParameters.model_validate(STANDARD)
    shape = build_split_clamp_support_shape(parameters)
    damaged_wall = shape.cut(
        cq.Workplane("XY")
        .box(4, 4, parameters.rear_clamp_rise, centered=(True, True, False))
        .translate((32, parameters.base_width / 2 - 2, parameters.lower_clamp_top_z))
    )
    wall_audit = audit_split_clamp_support(
        parameters,
        damaged_wall,
        engine="cadquery-occt",
    )
    assert wall_audit["rectangularRearWallPresent"] is False
    assert wall_audit["topologyAuditPassed"] is False

    damaged_ribs = shape
    for side in (-1, 1):
        damaged_ribs = damaged_ribs.cut(
            cq.Workplane("XY")
            .box(10, 10, parameters.rib_height + 0.02, centered=(True, True, False))
            .translate((
                side * 38,
                parameters.base_width / 2 - parameters.rib_thickness / 2,
                parameters.base_thickness - 0.01,
            ))
        )
    rib_audit = audit_split_clamp_support(
        parameters,
        damaged_ribs,
        engine="cadquery-occt",
    )
    assert rib_audit["ribPairPresent"] is False
    assert rib_audit["ribsInRearBand"] is False
    assert rib_audit["topologyAuditPassed"] is False


def test_occt_audit_rejects_filled_bore_split_or_cross_hole_exit() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    import cadquery as cq  # type: ignore

    parameters = SplitClampSupportParameters.model_validate(STANDARD)
    shape = build_split_clamp_support_shape(parameters)
    bore_plug = (
        cq.Workplane("XY")
        .workplane(offset=parameters.bore_floor_z)
        .center(0, parameters.pedestal_center_y)
        .circle(parameters.bore_diameter / 2)
        .extrude(1)
    )
    bore_audit = audit_split_clamp_support(
        parameters,
        shape.union(bore_plug),
        engine="cadquery-occt",
    )
    assert bore_audit["boreFloorPresent"] is False
    assert bore_audit["topologyAuditPassed"] is False

    split_plug = (
        cq.Workplane("XY")
        .box(
            parameters.split_width,
            parameters.pedestal_outer_radius - parameters.bore_diameter / 2,
            1,
            centered=(True, True, False),
        )
        .translate((
            0,
            parameters.pedestal_center_y
            - (parameters.pedestal_outer_radius + parameters.bore_diameter / 2) / 2,
            parameters.bore_floor_z,
        ))
    )
    split_audit = audit_split_clamp_support(
        parameters,
        shape.union(split_plug),
        engine="cadquery-occt",
    )
    assert split_audit["splitStartsAtBoreFloor"] is False
    assert split_audit["topologyAuditPassed"] is False

    rear_y = parameters.base_width / 2
    cross_plug = cq.Workplane("XY").newObject([
        cq.Solid.makeCylinder(
            parameters.cross_hole_diameter / 2,
            2,
            cq.Vector(0, rear_y - 2, parameters.cross_hole_center_z),
            cq.Vector(0, 1, 0),
        )
    ])
    cross_audit = audit_split_clamp_support(
        parameters,
        shape.union(cross_plug),
        engine="cadquery-occt",
    )
    assert cross_audit["crossHoleRearExitPresent"] is False
    assert cross_audit["topologyAuditPassed"] is False


def test_occt_step_round_trip_preserves_split_clamp_envelope() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    import cadquery as cq  # type: ignore

    parameters = SplitClampSupportParameters.model_validate(STANDARD)
    artifact = generate_split_clamp_support_artifacts(
        parameters,
        ["step"],
        require_cadquery=True,
    )[0]
    assert artifact.engine == "cadquery-occt"
    assert artifact.production_ready is True
    with tempfile.TemporaryDirectory(prefix="joyniu-split-clamp-") as directory:
        path = Path(directory) / "split-clamp.step"
        path.write_bytes(artifact.data)
        reopened = cq.importers.importStep(str(path)).val()
        box = reopened.BoundingBox()
        assert reopened.isValid()
        assert len(reopened.Solids()) == 1
        assert box.xlen == pytest.approx(125, abs=0.01)
        assert box.ylen == pytest.approx(95, abs=0.01)
        assert box.zlen == pytest.approx(75, abs=0.01)
        assert audit_split_clamp_support(
            parameters,
            reopened,
            engine="cadquery-occt",
        )["topologyAuditPassed"] is True


def test_strict_split_clamp_export_never_downgrades(monkeypatch) -> None:
    import app.split_clamp_support as module

    monkeypatch.setattr(module, "get_cadquery", lambda: None)
    with pytest.raises(RuntimeError, match="CadQuery is required"):
        generate_split_clamp_support_artifacts(
            SplitClampSupportParameters.model_validate(STANDARD),
            ["step"],
            require_cadquery=True,
        )


def test_ai_split_clamp_candidate_can_be_accepted_and_generated(monkeypatch) -> None:
    """Exercise the real conversation → customer acceptance → OCCT bridge."""

    from app import main as geometry_main

    services = build_platform_services(":memory:", auth_secret="split-clamp-confirm" * 3)
    designer = services.auth.create_user(
        "split-clamp-designer@example.com",
        "a-very-long-password",
        "Split clamp designer",
        roles=["designer"],
    )

    class FakeAI:
        def converse(self, message, *, previous_response_id, model_state, files):
            assert message == "分析并创建夹座"
            assert files[0].filename == "9.jpg"
            return AIConversationResult(
                response_id="resp_split_clamp",
                message="已识别开缝圆筒夹座候选",
                parameter_patch=dict(STANDARD),
                needs_review=True,
                questions=(),
                part_type="split_clamp_support",
                recipe_id="split_clamp_support_v1",
                parameter_evidence={
                    "pedestalOuterRadius": {
                        "value": 33,
                        "sourceView": "top",
                        "sourceText": "R33",
                        "confidence": 0.99,
                    }
                },
            )

    services.ai = FakeAI()
    platform_app = FastAPI()
    platform_app.include_router(create_platform_router(services), prefix="/api/v1")
    platform_client = TestClient(platform_app)
    token = services.auth.issue_token(designer).token
    headers = {"Authorization": f"Bearer {token}"}
    conversation = platform_client.post(
        "/api/v1/ai/conversation",
        headers=headers,
        data={"message": "分析并创建夹座"},
        files={"file": ("9.jpg", b"drawing-9", "image/jpeg")},
    )
    assert conversation.status_code == 200, conversation.text
    candidate = conversation.json()["drawingRecognition"]
    assert candidate["partType"] == "split_clamp_support"
    assert candidate["modelRecipe"]["recipeId"] == "split_clamp_support_v1"
    assert candidate["parameters"] == {}
    assert candidate["candidateParameters"]["pedestalOuterRadius"] == 33
    assert candidate["modelRecipe"]["parameterEvidence"]["pedestalOuterRadius"]["sourceText"] == "R33"

    accepted = platform_client.post(
        f"/api/v1/drawings/{candidate['id']}/accept",
        headers=headers,
        json={},
    )
    assert accepted.status_code == 200, accepted.text
    confirmed = accepted.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["partType"] == "split_clamp_support"
    assert confirmed["modelRecipe"]["recipeId"] == "split_clamp_support_v1"
    assert confirmed["parameters"]["baseLength"] == 125
    assert confirmed["parameters"]["pedestalOuterRadius"] == 33
    assert confirmed["parameters"]["boreDiameter"] == 36
    assert confirmed["parameters"]["splitWidth"] == 12
    assert confirmed["parameters"]["mountHoleCenterDistance"] == 96
    assert confirmed["parameters"]["mountHoleCenterFromRear"] == 40

    monkeypatch.setattr(geometry_main, "platform_services", services)
    generated = TestClient(geometry_main.app).post(
        "/api/v1/models/generate",
        json={
            **_request(),
            "sourceDrawingId": candidate["id"],
            "requireCadQuery": bool(cadquery_status()["available"]),
        },
    )
    assert generated.status_code == 200, generated.text
    payload = generated.json()
    assert payload["sourceDrawingId"] == candidate["id"]
    assert payload["recipeId"] == "split_clamp_support_v1"
    assert payload["validation"]["metrics"]["topologyAuditPassed"] is True
