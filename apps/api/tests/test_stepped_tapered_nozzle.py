from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.geometry import cadquery_status
from app.main import app
from app.model_recipes import RECIPE_PART_TYPES, parse_model_parameters
from app.schemas import SteppedTaperedNozzleParameters
from app.stepped_tapered_nozzle import (
    audit_stepped_tapered_nozzle,
    build_stepped_tapered_nozzle_components,
    build_stepped_tapered_nozzle_fallback_meshes,
    build_stepped_tapered_nozzle_shape,
    generate_stepped_tapered_nozzle_artifacts,
    validate_stepped_tapered_nozzle,
)


CLIENT = TestClient(app)
STANDARD = {
    "mainLength": 98,
    "headLength": 50,
    "neckLength": 20,
    "headLeftDiameter": 54.25449350717895,
    "headRightDiameter": 56,
    "neckDiameter": 30,
    "tipDiameter": 25,
    "counterboreDiameter": 40,
    "counterboreDepth": 40,
    "axialBoreDiameter": 13,
    "outletDiameter": 17,
    "outletTaperHalfAngle": 15,
    "insertOuterDiameter": 39.4,
    "insertLength": 40,
    "insertThreadDesignation": "M12",
    "insertAxialOffset": 0,
    "material": "45# 钢",
    "units": "mm",
}


def _request(**updates: object) -> dict[str, object]:
    return {
        "partType": "stepped_tapered_nozzle",
        "recipeId": "stepped_tapered_nozzle_with_insert_v1",
        "parameters": {**STANDARD, **updates},
        "formats": ["step", "glb"],
    }


def _glb_json(data: bytes) -> dict[str, object]:
    assert data[:4] == b"glTF"
    _magic, version, total_length = struct.unpack("<4sII", data[:12])
    assert version == 2
    assert total_length == len(data)
    json_length, chunk_type = struct.unpack("<I4s", data[12:20])
    assert chunk_type == b"JSON"
    return json.loads(data[20 : 20 + json_length].decode("utf-8"))


def test_schema_defaults_and_derived_dimensions_match_dwg_geometry() -> None:
    parameters = SteppedTaperedNozzleParameters.model_validate(STANDARD)
    assert parameters.tip_length == pytest.approx(28)
    assert parameters.outlet_taper_length == pytest.approx(7.464101615137755)
    assert parameters.outlet_taper_start_x == pytest.approx(90.53589838486225)
    assert parameters.radial_clearance == pytest.approx(0.3)
    assert parameters.insert_thread_nominal_diameter == pytest.approx(12)
    assert parameters.insert_thread_designation == "M12"
    assert RECIPE_PART_TYPES["stepped_tapered_nozzle_with_insert_v1"] == "stepped_tapered_nozzle"


def test_metric_thread_designation_is_normalized_but_not_invented() -> None:
    assert SteppedTaperedNozzleParameters(insertThreadDesignation="m12×1.75").insert_thread_designation == "M12X1.75"
    with pytest.raises(ValueError, match="metric syntax"):
        SteppedTaperedNozzleParameters(insertThreadDesignation="M12; run code")


def test_standard_nozzle_validation_exposes_derived_audit_and_thread_warning() -> None:
    report = validate_stepped_tapered_nozzle(
        SteppedTaperedNozzleParameters.model_validate(STANDARD),
        engine="faceted-fallback",
    )
    assert report["valid"] is True
    assert report["productionReady"] is False
    metrics = report["metrics"]
    assert metrics["solidCount"] == 2
    assert metrics["componentMode"] == "two_solid_assembly_candidate"
    assert metrics["tipLength"] == pytest.approx(28)
    assert metrics["outletTaperLength"] == pytest.approx(7.464101615137755)
    assert metrics["radialClearance"] == pytest.approx(0.3)
    assert metrics["insertNominalHoleDiameterMeasured"] == 12
    assert metrics["realThreadGeometryPresent"] is False
    thread_issue = next(item for item in report["issues"] if item["ruleId"] == "thread.m12_simplified")
    assert thread_issue["severity"] == "warning"
    assert "no pitch" in thread_issue["message"]


@pytest.mark.parametrize(
    ("updates", "rule_id"),
    [
        ({"headLength": 80, "neckLength": 20}, "stack.tip_length"),
        ({"counterboreDiameter": 55}, "profile.head_envelope"),
        ({"counterboreDepth": 55}, "counterbore.depth"),
        ({"outletDiameter": 26}, "bore.diameters"),
        ({"outletTaperHalfAngle": 2}, "outlet.tip_envelope"),
        ({"insertOuterDiameter": 40}, "insert.radial_clearance"),
        ({"insertLength": 41}, "insert.axial_envelope"),
        ({"insertAxialOffset": -1}, "insert.axial_offset"),
        ({"insertThreadDesignation": "M42"}, "insert.thread_hole"),
    ],
)
def test_invalid_relationships_have_specific_rules(
    updates: dict[str, object],
    rule_id: str,
) -> None:
    parameters = SteppedTaperedNozzleParameters.model_validate({**STANDARD, **updates})
    report = validate_stepped_tapered_nozzle(parameters, engine="faceted-fallback")
    assert report["valid"] is False
    rules = {item["ruleId"]: item for item in report["issues"]}
    assert rules[rule_id]["passed"] is False


def test_registry_and_generic_api_reject_recipe_mismatch_and_unknown_fields() -> None:
    parsed = parse_model_parameters("stepped_tapered_nozzle_with_insert_v1", STANDARD)
    assert isinstance(parsed, SteppedTaperedNozzleParameters)
    mismatch = CLIENT.post(
        "/api/v1/models/validate",
        json={**_request(), "partType": "bracket"},
    )
    assert mismatch.status_code == 422
    typo = CLIENT.post(
        "/api/v1/models/validate",
        json=_request(outletTaperAngel=15),
    )
    assert typo.status_code == 422
    assert "outletTaperAngel" in typo.text


def test_occt_shape_has_two_unfused_solids_and_dimensioned_voids() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    parameters = SteppedTaperedNozzleParameters.model_validate(STANDARD)
    compound = build_stepped_tapered_nozzle_shape(parameters).val()
    solids = sorted(compound.Solids(), key=lambda solid: solid.BoundingBox().xlen, reverse=True)
    assert compound.isValid()
    assert len(solids) == 2
    main, insert = solids
    main_box, insert_box = main.BoundingBox(), insert.BoundingBox()
    assert (main_box.xlen, main_box.ylen, main_box.zlen) == pytest.approx((98, 56, 56), abs=0.01)
    assert (insert_box.xlen, insert_box.ylen, insert_box.zlen) == pytest.approx((40, 39.4, 39.4), abs=0.01)
    assert main.intersect(insert).Volume() == pytest.approx(0, abs=1e-6)

    # x=20 is inside the Ø40 counterbore. Radius 19.85 lies in the 0.3 mm
    # radial assembly gap, while radius 21 belongs only to the main head.
    assert not main.isInside((20, 0, 0), 1e-6)
    assert not insert.isInside((20, 0, 0), 1e-6)
    assert not main.isInside((20, 19.85, 0), 1e-6)
    assert not insert.isInside((20, 19.85, 0), 1e-6)
    assert main.isInside((20, 21, 0), 1e-6)
    assert insert.isInside((20, 10, 0), 1e-6)
    assert not main.isInside((45, 0, 0), 1e-6)
    assert main.isInside((45, 7, 0), 1e-6)

    audit = audit_stepped_tapered_nozzle(parameters, build_stepped_tapered_nozzle_shape(parameters), engine="cadquery-occt")
    assert audit["topologyAuditPassed"] is True
    assert audit["solidCount"] == 2
    assert audit["componentsDoNotOverlap"] is True
    assert audit["headLeftDiameterMeasured"] == pytest.approx(54.25449350717895, abs=0.01)
    assert audit["headLengthMeasured"] == pytest.approx(50, abs=0.01)
    assert audit["neckLengthMeasured"] == pytest.approx(20, abs=0.01)
    assert audit["tipLengthMeasured"] == pytest.approx(28, abs=0.01)
    assert audit["counterboreDiameterMeasured"] == pytest.approx(40, abs=0.01)
    assert audit["counterboreDepthMeasured"] == pytest.approx(40, abs=0.01)
    assert audit["axialBoreDiameterMeasured"] == pytest.approx(13, abs=0.01)
    assert audit["outletDiameterMeasured"] == pytest.approx(17, abs=0.01)
    assert audit["outletTaperLengthMeasured"] == pytest.approx(7.464101615, abs=0.01)
    assert audit["insertOuterDiameterMeasured"] == pytest.approx(39.4, abs=0.01)
    assert audit["insertNominalHoleDiameterMeasured"] == pytest.approx(12, abs=0.01)
    assert audit["radialClearanceMeasured"] == pytest.approx(0.3, abs=0.01)


def test_occt_audit_rejects_a_missing_insert() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    parameters = SteppedTaperedNozzleParameters.model_validate(STANDARD)
    main, _insert = build_stepped_tapered_nozzle_components(parameters)
    audit = audit_stepped_tapered_nozzle(parameters, main, engine="cadquery-occt")
    assert audit["solidCount"] == 1
    assert audit["topologyAuditPassed"] is False


def test_step_round_trip_preserves_two_independent_solids() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    import cadquery as cq  # type: ignore

    parameters = SteppedTaperedNozzleParameters.model_validate(STANDARD)
    artifact = generate_stepped_tapered_nozzle_artifacts(
        parameters,
        ["step"],
        require_cadquery=True,
    )[0]
    assert artifact.production_ready is True
    assert any("two unfused solids" in warning for warning in artifact.warnings)
    with tempfile.TemporaryDirectory(prefix="joyniu-nozzle-") as directory:
        path = Path(directory) / "nozzle.step"
        path.write_bytes(artifact.data)
        reopened = cq.importers.importStep(str(path)).val()
    solids = sorted(reopened.Solids(), key=lambda solid: solid.BoundingBox().xlen, reverse=True)
    assert reopened.isValid()
    assert len(solids) == 2
    assert solids[0].BoundingBox().xlen == pytest.approx(98, abs=0.01)
    assert solids[1].BoundingBox().xlen == pytest.approx(40, abs=0.01)
    assert audit_stepped_tapered_nozzle(parameters, reopened, engine="cadquery-occt")["topologyAuditPassed"] is True


def test_glb_contains_two_named_component_meshes_and_nodes() -> None:
    parameters = SteppedTaperedNozzleParameters.model_validate(STANDARD)
    artifact = generate_stepped_tapered_nozzle_artifacts(parameters, ["glb"])[0]
    gltf = _glb_json(artifact.data)
    assert len(gltf["nodes"]) == 2
    assert len(gltf["meshes"]) == 2
    assert gltf["scenes"][0]["nodes"] == [0, 1]
    assert gltf["extras"]["componentMode"] == "two_solid_assembly_candidate"
    assert "no helical thread" in gltf["extras"]["threadRepresentation"]


def test_generic_model_api_generates_downloadable_two_component_artifacts() -> None:
    response = CLIENT.post("/api/v1/models/generate", json=_request())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["partType"] == "stepped_tapered_nozzle"
    assert payload["recipeId"] == "stepped_tapered_nozzle_with_insert_v1"
    assert payload["parameters"]["mainLength"] == 98
    metrics = payload["validation"]["metrics"]
    assert metrics["solidCount"] == 2
    assert metrics["tipLength"] == pytest.approx(28)
    assert metrics["outletTaperLength"] == pytest.approx(7.464101615137755)
    assert metrics["radialClearance"] == pytest.approx(0.3)
    by_format = {item["format"]: item for item in payload["artifacts"]}
    step = CLIENT.get(by_format["step"]["downloadUrl"])
    glb = CLIENT.get(by_format["glb"]["downloadUrl"])
    assert step.status_code == 200 and step.content.startswith(b"ISO-10303-21;")
    assert glb.status_code == 200 and len(_glb_json(glb.content)["nodes"]) == 2


def test_fallback_meshes_remain_two_disconnected_components() -> None:
    main, insert = build_stepped_tapered_nozzle_fallback_meshes(
        SteppedTaperedNozzleParameters.model_validate(STANDARD)
    )
    assert main.indices and insert.indices
    main_x = main.positions[0::3]
    insert_x = insert.positions[0::3]
    assert min(main_x) == pytest.approx(0)
    assert max(main_x) == pytest.approx(98)
    assert min(insert_x) == pytest.approx(0)
    assert max(insert_x) == pytest.approx(40)


def test_strict_export_never_downgrades_without_cadquery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.stepped_tapered_nozzle as module

    monkeypatch.setattr(module, "get_cadquery", lambda: None)
    with pytest.raises(RuntimeError, match="CadQuery is required"):
        generate_stepped_tapered_nozzle_artifacts(
            SteppedTaperedNozzleParameters.model_validate(STANDARD),
            ["step"],
            require_cadquery=True,
        )
