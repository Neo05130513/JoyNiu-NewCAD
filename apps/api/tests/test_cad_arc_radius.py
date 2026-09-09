"""Synthetic radius constraints independent of any drawing or recipe."""

from copy import deepcopy
import math

import pytest

from app.cad_executor import build_plan_shape, execute_cad_plan, inspect_cad_draft
from app.cad_inspector import inspect_shape, inspect_step
from app.cad_plan import PlanValidationError, cad_plan_schema, validate_plan
from app.geometry import get_cadquery


def arc_plan():
    return {"version": "cad-plan-v1", "parameters": {"diameter": {"value": 12.5}},
            "features": [{"id": "curved_body", "op": "profile_extrude", "plane": "XY",
                          "start": ["-diameter/2", -2], "segments": [
                              {"type": "line", "to": ["-diameter/2", 0]},
                              {"type": "arc", "through": [0, "diameter/2"], "to": ["diameter/2", 0],
                               "radius": "diameter/2"},
                              {"type": "line", "to": ["diameter/2", -2]}], "distance": 2.75}],
            "result": "curved_body"}


def test_profile_schema_and_validation_allow_an_optional_radius_expression_only_on_arcs():
    original = arc_plan()
    assert validate_plan(original)["features"][0]["segments"][1]["radius"] == "diameter/2"
    unchanged = deepcopy(original)
    schema = cad_plan_schema()
    for feature in schema["properties"]["features"]["items"]["oneOf"]:
        if feature["properties"]["op"]["const"] in {"profile_extrude", "profile_revolve"}:
            segments = feature["properties"]["segments"]["items"]["oneOf"]
            arc = next(segment for segment in segments if segment["properties"]["type"]["const"] == "arc")
            line = next(segment for segment in segments if segment["properties"]["type"]["const"] == "line")
            assert "radius" in arc["properties"] and "radius" not in arc["required"]
            assert "radius" not in line["properties"]
    assert original == unchanged
    original["features"][0]["segments"][0]["radius"] = 6.25
    with pytest.raises(PlanValidationError, match="segment fields"):
        validate_plan(original)


@pytest.mark.parametrize("radius", [None, True, {}, "missing_radius", "diameter.__class__"])
def test_arc_radius_constraints_use_the_existing_bounded_dimension_language(radius):
    plan = arc_plan()
    plan["features"][0]["segments"][1]["radius"] = radius
    with pytest.raises(PlanValidationError) as raised:
        validate_plan(plan)
    assert raised.value.feature_id == "curved_body"


@pytest.fixture
def cad_kernel():
    if get_cadquery() is None:
        pytest.skip("CadQuery is unavailable")


@pytest.mark.parametrize("plane", ["XY", "XZ", "YZ"])
def test_constrained_arc_uses_the_previous_segment_endpoint_and_matches_real_cylindrical_geometry(cad_kernel, plane):
    plan = arc_plan()
    plan["features"][0].update({"plane": plane, "origin": [113, -27, 4.5]})
    shape, parameters, _ = build_plan_shape(plan)
    observed = inspect_shape(shape)
    radius = parameters["diameter"] / 2
    assert observed["valid"] and observed["solidCount"] == 1
    assert any(face["radius"] == pytest.approx(radius) for face in observed["cylinders"])
    assert observed["volumeMm3"] == pytest.approx((math.pi * radius**2 / 2 + 4 * radius) * 2.75)


def test_consecutive_constrained_arcs_also_support_revolved_profiles(cad_kernel):
    plan = {"version": "cad-plan-v1", "parameters": {"major": {"value": 11.25}, "minor": {"value": 2.75}},
            "features": [{"id": "ring", "op": "profile_revolve", "plane": "XZ", "start": ["major", "-minor"],
                          "segments": [{"type": "arc", "through": ["major+minor", 0], "to": ["major", "minor"], "radius": "minor"},
                                       {"type": "arc", "through": ["major-minor", 0], "to": ["major", "-minor"], "radius": "minor"}],
                          "axisStart": [0, 0], "axisEnd": [0, 1]}], "result": "ring"}
    shape, _, _ = build_plan_shape(plan)
    observed = inspect_shape(shape)
    assert observed["valid"] and observed["solidCount"] == 1
    assert observed["volumeMm3"] == pytest.approx(2 * math.pi**2 * 11.25 * 2.75**2)


def test_satisfied_radius_constraint_survives_worker_execution_and_step_reimport(cad_kernel, tmp_path):
    result = execute_cad_plan(arc_plan(), tmp_path)
    assert result["status"] == "succeeded", result
    assert result["execution"]["isolatedProcess"]
    observed = inspect_step(result["artifacts"]["step"]["path"])
    assert observed["valid"] and observed["solidCount"] == 1
    assert any(face["radius"] == pytest.approx(6.25) for face in observed["cylinders"])


@pytest.mark.parametrize("execute", [execute_cad_plan, inspect_cad_draft])
def test_mismatched_arc_returns_actual_and_expected_radius_before_creating_artifacts(cad_kernel, tmp_path, execute):
    plan = arc_plan()
    plan["features"][0]["segments"][1]["radius"] = "diameter/2-1.5"
    result = execute(plan, tmp_path)
    assert result["status"] == "failed" and result["valid"] is False
    error = result["errors"][0]
    assert error["code"] == "arc_radius_mismatch"
    assert error["featureId"] == "curved_body" and error["segmentIndex"] == 1
    assert error["actualRadius"] == pytest.approx(6.25)
    assert error["expectedRadius"] == pytest.approx(4.75)
    assert "actual 6.25 mm" in error["message"] and "expected 4.75 mm" in error["message"]
    assert result["artifacts"] == {}
    assert not list(tmp_path.rglob("*.step")) and not list(tmp_path.rglob("*.glb"))


def test_unconstrained_arcs_remain_compatible_and_roundoff_is_not_a_design_mismatch(cad_kernel):
    old = arc_plan()
    del old["features"][0]["segments"][1]["radius"]
    old_shape, _, _ = build_plan_shape(old)
    rounded = arc_plan()
    rounded["features"][0]["segments"][1]["radius"] = 6.250002
    constrained_shape, _, _ = build_plan_shape(rounded)
    assert constrained_shape.val().Volume() == pytest.approx(old_shape.val().Volume())


@pytest.mark.parametrize("radius", [0, -2.75])
def test_radius_constraint_must_resolve_to_a_positive_length(cad_kernel, radius):
    plan = arc_plan()
    plan["features"][0]["segments"][1]["radius"] = radius
    with pytest.raises(PlanValidationError, match="arc radius must be greater") as raised:
        build_plan_shape(plan)
    assert raised.value.feature_id == "curved_body"


@pytest.mark.parametrize("through,end", [([0, 0], [6.25, 0]), ([-6.25, 0], [6.25, 0]), ([0, 6.25], [-6.25, 0])])
def test_degenerate_constrained_arcs_report_geometry_error_instead_of_infinite_radius(cad_kernel, through, end):
    plan = arc_plan()
    plan["features"][0]["segments"][1].update({"through": through, "to": end})
    with pytest.raises(PlanValidationError) as raised:
        build_plan_shape(plan)
    assert raised.value.code == "invalid_arc_geometry"
    assert raised.value.feature_id == "curved_body"
    assert raised.value.details == {"segmentIndex": 1, "expectedRadius": 6.25}


def test_unknown_constrained_radius_stays_unresolved_without_starting_a_worker(tmp_path):
    plan = arc_plan()
    plan["parameters"]["expected"] = {"value": None, "question": "What radius is required?"}
    plan["features"][0]["segments"][1]["radius"] = "expected"
    result = execute_cad_plan(plan, tmp_path)
    assert result["status"] == "needs_input" and result["missingParameters"] == ["expected"]
    assert result["artifacts"] == {} and not list(tmp_path.iterdir())
