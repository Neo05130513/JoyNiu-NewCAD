from __future__ import annotations

import json
import math
import struct
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.geometry import cadquery_status, get_cadquery
from app.main import app
from app.model_recipes import parse_model_parameters
from app.schemas import ArchedClevisSupportParameters, BracketParameters
from app.arched_clevis_support import (
    audit_arched_clevis_support,
    build_arched_clevis_support_shape,
    generate_arched_clevis_support_artifacts,
    solid_ray_intervals,
    validate_arched_clevis_support,
)


STANDARD = {
    "archOuterRadius": 28, "archInnerRadius": 16, "baseWidth": 50, "baseThickness": 9,
    "earRadius": 15, "earHoleDiameter": 13, "earCenterHeight": 40, "earThickness": 10,
    "earGap": 30, "mountEarRadius": 15, "mountHoleDiameter": 13, "mountHoleCenterDistance": 80,
    "material": "45# 钢", "units": "mm",
}


def request(**updates):
    return {"partType": "arched_clevis_support", "recipeId": "arched_clevis_support_v1",
            "parameters": {**STANDARD, **updates}, "formats": ["step", "glb"], "requireCadQuery": True}


@pytest.fixture(scope="module")
def solid():
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    return build_arched_clevis_support_shape(ArchedClevisSupportParameters(**STANDARD))


def test_contract_derives_envelope_and_bridge_without_reinterpreting_old_bracket():
    p = parse_model_parameters("arched_clevis_support_v1", STANDARD)
    assert p.base_length == 110
    assert p.total_height == 55
    assert p.bridge_height == pytest.approx(math.sqrt(559))
    assert p.model_dump(by_alias=True) == STANDARD
    assert isinstance(parse_model_parameters("bracket_support_v1", {}), BracketParameters)
    for duplicate in ("baseLength", "totalHeight", "bridgeHeight", "bossDiameter", "notchRadius"):
        with pytest.raises(ValueError, match="unknown parameter"):
            parse_model_parameters("arched_clevis_support_v1", {**STANDARD, duplicate: 1})


@pytest.mark.parametrize("field", [key for key in STANDARD if key not in {"material", "units"}])
def test_non_finite_dimensions_are_rejected_before_geometry(field):
    with pytest.raises(ValueError, match="finite"):
        parse_model_parameters("arched_clevis_support_v1", {**STANDARD, field: float("inf")})


@pytest.mark.parametrize(("updates", "rule"), [
    ({"archInnerRadius": 28}, "arch.wall"),
    ({"archInnerRadius": 24}, "bridge.wall"),
    ({"earRadius": 28}, "ear.arch_connection"),
    ({"earHoleDiameter": 30}, "ear.hole_wall"),
    ({"earGap": 31}, "ear.width_stack"),
    ({"earThickness": 0}, "dimension.earThickness"),
    ({"earCenterHeight": 29}, "ear.hole_height"),
    ({"baseThickness": 24}, "base.height"),
    ({"mountEarRadius": 26}, "mount.ear_width"),
    ({"mountHoleDiameter": 30}, "mount.hole_wall"),
    ({"mountHoleCenterDistance": 65}, "mount.arch_clearance"),
])
def test_invalid_relations_do_not_build_or_claim_topology(updates, rule):
    p = ArchedClevisSupportParameters(**{**STANDARD, **updates})
    report = validate_arched_clevis_support(p, engine="faceted-fallback")
    assert report["valid"] is False
    assert report["productionReady"] is False
    assert {issue["ruleId"]: issue["passed"] for issue in report["issues"]}[rule] is False
    assert report["metrics"]["kernelBacked"] is False
    with pytest.raises(ValueError):
        build_arched_clevis_support_shape(p)


def test_no_kernel_means_no_shape_substitution_or_unearned_audit(monkeypatch):
    p = ArchedClevisSupportParameters(**STANDARD)
    report = validate_arched_clevis_support(p, engine="faceted-fallback")
    assert report["valid"] is True
    assert report["productionReady"] is False
    assert report["metrics"]["topologyAuditPassed"] is False
    monkeypatch.setattr("app.arched_clevis_support.get_cadquery", lambda: None)
    with pytest.raises(RuntimeError, match="no substitute shape"):
        generate_arched_clevis_support_artifacts(p, require_cadquery=False)


def test_occt_solid_has_correct_envelope_and_functional_sections(solid):
    p = ArchedClevisSupportParameters(**STANDARD)
    audit = audit_arched_clevis_support(p, solid)
    assert audit["topologyAuditPassed"] is True, audit
    assert audit["solidCount"] == 1
    assert (audit["bboxLength"], audit["bboxWidth"], audit["bboxHeight"]) == pytest.approx((110, 50, 55))
    assert audit["bridgeHeightMeasured"] == pytest.approx(math.sqrt(559))
    rays = audit["raySectionsMm"]
    assert rays["bridgeCenterZ"][0] == pytest.approx([16, math.sqrt(559)])
    assert rays["clevisWallY"] == [[-25, -15], [15, 25]]
    assert rays["clevisHoleY"] == []
    for side in ("left", "right"):
        assert rays[f"{side}MountHoleZ"] == []
        assert rays[f"{side}FootZ"] == [[0, 9]]
    # These witness points distinguish this part from a rectangular C-bracket,
    # a full-height vertical bore, a filled central gap, or a closed base.
    value = solid.val()
    assert not value.isInside((0, 0, 8), 1e-6)
    assert value.isInside((0, 0, 20), 1e-6)
    assert not value.isInside((0, 0, 27), 1e-6)
    for y in (-20, 20):
        assert value.isInside((10, y, 40), 1e-6)
        assert not value.isInside((0, y, 40), 1e-6)
        assert value.isInside((0, y, 52), 1e-6)
    assert not value.isInside((24, 0, 20), 1e-6)
    assert not value.isInside((40, 10, 10), 1e-6)


def test_audit_bounds_are_invariant_after_glb_tessellation(solid):
    p = ArchedClevisSupportParameters(**STANDARD)
    before = audit_arched_clevis_support(p, solid)
    solid.val().tessellate(0.1, 0.12)
    after = audit_arched_clevis_support(p, solid)
    # CadQuery's default bounding box grows to ~50.05 x 55.026 after this
    # tessellation. The functional audit must still use the exact B-Rep.
    assert before["topologyAuditPassed"] is after["topologyAuditPassed"] is True
    assert after["bboxMeasurementSource"] == "exact-brep-surfaces-without-triangulation"
    for key in ("bboxLength", "bboxWidth", "bboxHeight", "bridgeHeightMeasured"):
        assert after[key] == pytest.approx(before[key], abs=1e-8)
    assert after["raySectionsMm"] == before["raySectionsMm"]


def test_audit_rejects_filled_gap_blocked_arch_or_wrong_hole_axis(solid):
    cq = get_cadquery()
    p = ArchedClevisSupportParameters(**STANDARD)
    gap_plug = cq.Workplane("XY").box(28, 31, 20, centered=(True, True, False)).translate((0, 0, 25))
    gap_audit = audit_arched_clevis_support(p, solid.union(gap_plug))
    assert gap_audit["clevisGapAndThicknessPresent"] is False
    assert gap_audit["topologyAuditPassed"] is False
    arch_plug = cq.Workplane("XY").box(34, 20, 17, centered=(True, True, False))
    arch_audit = audit_arched_clevis_support(p, solid.union(arch_plug))
    assert arch_audit["innerArchPresent"] is False
    assert arch_audit["topologyAuditPassed"] is False
    wrong_z_bore = cq.Workplane("XY").circle(6.5).extrude(60)
    wrong_audit = audit_arched_clevis_support(p, solid.cut(wrong_z_bore))
    assert wrong_audit["flatBridgePresent"] is False
    assert wrong_audit["topologyAuditPassed"] is False


def test_audit_rejects_a_short_mount_hole_and_extra_bridge_pocket(solid):
    cq = get_cadquery()
    p = ArchedClevisSupportParameters(**STANDARD)
    plug = cq.Workplane("XY").center(40, 0).circle(6.5).extrude(1)
    assert audit_arched_clevis_support(p, solid.union(plug))["mountHolePairPresent"] is False
    pocket = cq.Workplane("XY").box(8, 20, 4, centered=(True, True, False)).translate((0, 0, p.bridge_height-2))
    assert audit_arched_clevis_support(p, solid.cut(pocket))["flatBridgePresent"] is False


def test_dimension_updates_change_geometry_instead_of_using_drawing_constants(solid):
    p = ArchedClevisSupportParameters(**{**STANDARD,
        "archOuterRadius": 32, "archInnerRadius": 18, "baseWidth": 60, "baseThickness": 11,
        "earRadius": 17, "earHoleDiameter": 14, "earCenterHeight": 44, "earThickness": 12,
        "earGap": 36, "mountEarRadius": 18, "mountHoleDiameter": 14, "mountHoleCenterDistance": 96})
    changed = build_arched_clevis_support_shape(p)
    audit = audit_arched_clevis_support(p, changed)
    assert audit["topologyAuditPassed"] is True, audit
    assert (audit["bboxLength"], audit["bboxWidth"], audit["bboxHeight"]) == pytest.approx((132, 60, 61))
    assert audit["raySectionsMm"]["clevisWallY"] == [[-30, -18], [18, 30]]
    assert audit["raySectionsMm"]["bridgeCenterZ"][0] == pytest.approx([18, math.sqrt(735)])


def test_api_validates_identity_unknown_fields_and_relations():
    client = TestClient(app)
    for body in ({**request(), "partType": "bracket"}, request(bossDiameter=13), request(totalHeight=55)):
        assert client.post("/api/v1/models/validate", json=body).status_code == 422
    invalid = client.post("/api/v1/models/generate", json=request(earGap=40))
    assert invalid.status_code == 422
    assert "no artifact" in invalid.text


@pytest.mark.parametrize(("candidate_missing", "request_missing"), [(True, False), (False, True), (True, True)])
def test_source_drawing_cannot_be_completed_with_template_defaults(monkeypatch, candidate_missing, request_missing):
    import importlib
    main = importlib.import_module("app.main")
    source_parameters = dict(STANDARD)
    body = {**request(), "sourceDrawingId": "arched-explicit-evidence"}
    if candidate_missing:
        del source_parameters["baseThickness"]
    if request_missing:
        del body["parameters"]["baseThickness"]
    monkeypatch.setitem(main._drawings, body["sourceDrawingId"], SimpleNamespace(
        status="confirmed", model_recipe={"recipeId": "arched_clevis_support_v1", "parameters": source_parameters}))
    response = TestClient(app).post("/api/v1/models/generate", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["missingFields"] == ["baseThickness"]


def test_generic_api_exports_same_audited_shape_and_step_round_trips(solid, tmp_path):
    client = TestClient(app)
    response = client.post("/api/v1/models/generate", json=request())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["partType"] == "arched_clevis_support"
    assert payload["recipeId"] == "arched_clevis_support_v1"
    assert payload["parameters"] == STANDARD
    assert payload["validation"]["productionReady"] is True
    assert payload["validation"]["metrics"]["topologyAuditPassed"] is True
    descriptors = {item["format"]: item for item in payload["artifacts"]}
    assert descriptors["step"]["engine"] == "cadquery-occt"
    assert descriptors["step"]["productionReady"] is True
    assert descriptors["glb"]["engine"] == "cadquery-tessellation"
    step = client.get(descriptors["step"]["downloadUrl"])
    glb = client.get(descriptors["glb"]["downloadUrl"])
    assert step.status_code == glb.status_code == 200
    assert step.content.startswith(b"ISO-10303-21;")
    magic, version, size = struct.unpack("<4sII", glb.content[:12])
    assert (magic, version, size) == (b"glTF", 2, len(glb.content))
    json_size = struct.unpack("<I", glb.content[12:16])[0]
    manifest = json.loads(glb.content[20:20+json_size])
    assert manifest["meshes"][0]["name"] == "JoyNiu Arched Clevis Support"
    path = tmp_path / "round-trip.step"
    path.write_bytes(step.content)
    imported = get_cadquery().importers.importStep(str(path))
    audit = audit_arched_clevis_support(ArchedClevisSupportParameters(**STANDARD), imported)
    assert audit["topologyAuditPassed"] is True, audit
    assert solid_ray_intervals(imported, (0, 0, 0), (0, 0, 1), -1, 60)[0] == pytest.approx([16, math.sqrt(559)])
