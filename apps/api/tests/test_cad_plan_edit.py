from copy import deepcopy
import math

import pytest

from app.cad_plan import MAX_FEATURES, PlanValidationError, UnknownParametersError, resolve_parameters
from app.cad_plan_edit import PlanEditError, apply_plan_edit


def base():
    return {"version": "cad-plan-v1", "units": "mm", "name": "User plate",
            "parameters": {"width": {"value": 20, "source": {"type": "user", "text": "width 20"}},
                           "thickness": {"value": 8}},
            "features": [{"id": "body", "op": "box", "size": ["width", 12, "thickness"]}], "result": "body"}


def test_initial_edit_requires_explicit_feature_and_result_without_template():
    for edit in ({}, {"name": "Blank"}, {"parameters": {"width": {"value": 20}}},
                 {"features": base()["features"]}, {"features": [], "result": "body"}):
        with pytest.raises(PlanEditError, match="first edit"):
            apply_plan_edit(None, edit)
    submitted = {key: value for key, value in base().items() if key not in {"version", "units"}}
    original = deepcopy(submitted)
    result = apply_plan_edit(None, submitted)
    assert result == base() and submitted == original
    assert set(result) == {"version", "units", "name", "parameters", "features", "result"}


def test_upserts_replace_whole_entries_keep_positions_and_append_new_features():
    current = base()
    current["features"].append({"id": "hole", "op": "cylinder", "radius": 2, "height": 8, "origin": [10, 6, 0]})
    edit = {"parameters": {"width": {"value": 24}},
            "features": [{"id": "hole", "op": "box", "size": [3, 3, 8], "origin": [9, 5, 0]},
                         {"id": "finished", "op": "cut", "inputs": ["body", "hole"]}], "result": "finished"}
    before, original_edit = deepcopy(current), deepcopy(edit)
    result = apply_plan_edit(current, edit)
    assert current == before and edit == original_edit
    assert list(result["parameters"]) == ["width", "thickness"]
    assert result["parameters"]["width"] == {"value": 24}
    assert [item["id"] for item in result["features"]] == ["body", "hole", "finished"]
    assert result["features"][1] == edit["features"][0]
    assert "radius" not in result["features"][1]
    result["features"][0]["size"][1] = 99
    result["parameters"]["thickness"]["value"] = 99
    assert current == before  # result shares no mutable descendants with caller


def test_explicit_removal_can_update_dependents_and_result_atomically():
    current = base()
    current["features"].extend([{"id": "tool", "op": "cylinder", "radius": 2, "height": 8},
                                {"id": "old_cut", "op": "cut", "inputs": ["body", "tool"]}])
    current["result"] = "old_cut"
    result = apply_plan_edit(current, {"removeFeatures": ["old_cut", "tool"], "result": "body"})
    assert [item["id"] for item in result["features"]] == ["body"]
    assert len(current["features"]) == 3 and current["result"] == "old_cut"


@pytest.mark.parametrize("edit", [
    {"removeFeatures": ["missing"]},
    {"removeFeatures": ["body", "body"]},
    {"features": [base()["features"][0], base()["features"][0]]},
    {"features": [base()["features"][0]], "removeFeatures": ["body"]},
    {"removeFeatures": ["body"]},
    {"features": [{"id": "body", "op": "translate", "input": "future", "vector": [1, 0, 0]},
                  {"id": "future", "op": "box", "size": [1, 1, 1]}]},
    {"features": [{"id": "new", "op": "cut", "inputs": ["body", "missing"]}], "result": "new"},
    {"features": [{"id": "body", "op": "box", "size": ["unrecorded", 12, 8]}]},
    {"parameters": {"width": {"expression": "unrecorded + 1"}}},
    {"parameters": {"width": {"value": 1, "expression": "2"}}},
    {"features": [{"id": "body", "op": "box"}]},
    {"result": "missing"},
    {"units": "inches"},
    {"version": "cad-plan-v1"},
    {"outputPath": "/tmp/unsolicited.step"},
    {"parameters": {"width": {"value": 2, "code": "print(1)"}}},
    {"features": [{"id": "body", "op": "python", "code": "print(1)"}]},
    {"features": {}}, {"parameters": []}, {"removeFeatures": None},
    {"notes": "not an array"},
    {"parameters": {"width": {"value": math.nan}}},
])
def test_invalid_edit_never_mutates_existing_plan(edit):
    current, before = base(), base()
    with pytest.raises(PlanValidationError):
        apply_plan_edit(current, edit)
    assert current == before


def test_removing_a_referenced_feature_is_rejected_and_keeps_original():
    current = base()
    current["features"].append({"id": "moved", "op": "translate", "input": "body", "vector": [1, 0, 0]})
    current["result"] = "moved"
    before = deepcopy(current)
    with pytest.raises(PlanValidationError) as error:
        apply_plan_edit(current, {"removeFeatures": ["body"]})
    assert error.value.feature_id == "moved"
    assert current == before


def test_batch_and_total_feature_and_parameter_limits_remain_enforced():
    features = [{"id": f"body{i}", "op": "box", "size": [1, 1, 1]} for i in range(13)]
    assert len(apply_plan_edit(None, {"features": features[:12], "result": "body11"})["features"]) == 12
    with pytest.raises(PlanEditError, match="12"):
        apply_plan_edit(None, {"features": features, "result": "body12"})
    full = base()
    full["features"] = [{"id": f"body{i}", "op": "box", "size": [1, 1, 1]} for i in range(MAX_FEATURES)]
    full["result"] = "body0"
    with pytest.raises(PlanValidationError):
        apply_plan_edit(full, {"features": [{"id": "extra", "op": "box", "size": [1, 1, 1]}]})
    parameters = {f"p{i}": {"value": i} for i in range(101)}
    with pytest.raises(PlanEditError, match="100"):
        apply_plan_edit(base(), {"parameters": parameters})
    full = apply_plan_edit(None, {"parameters": dict(list(parameters.items())[:100]), "features": features[:1], "result": "body0"})
    assert len(full["parameters"]) == 100
    with pytest.raises(PlanValidationError):
        apply_plan_edit(full, {"parameters": {"one_more": {"value": 1}}})


def test_unknown_dimensions_remain_unknown_and_cannot_execute():
    result = apply_plan_edit(base(), {"parameters": {"width": {"value": None, "question": "What is the width?"}}})
    assert result["parameters"]["width"]["value"] is None
    with pytest.raises(UnknownParametersError) as error:
        resolve_parameters(result)
    assert error.value.missing == ["width"]


def test_malformed_existing_plan_is_not_implicitly_repaired():
    with pytest.raises(PlanValidationError):
        apply_plan_edit({}, {"features": base()["features"], "result": "body"})


def test_incremental_prefix_builds_real_geometry_only_when_executor_is_called(tmp_path):
    from app.geometry import cadquery_status
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery unavailable")
    from app.cad_executor import execute_cad_plan
    first = apply_plan_edit(None, {key: value for key, value in base().items() if key not in {"version", "units"}})
    second = apply_plan_edit(first, {"parameters": {"boreRadius": {"value": 2}},
                                    "features": [{"id": "tool", "op": "cylinder", "radius": "boreRadius", "height": "thickness", "origin": [10, 6, 0]}]})
    final = apply_plan_edit(second, {"features": [{"id": "part", "op": "cut", "inputs": ["body", "tool"]}], "result": "part"})
    assert set(final).isdisjoint({"inspection", "artifacts", "valid", "status"})
    result = execute_cad_plan(final, tmp_path, timeout_seconds=30,
                              ray_probes=[{"id": "bore", "origin": [10, 6, 0], "direction": [0, 0, 1], "start": -1, "end": 9}])
    assert result["status"] == "succeeded", result.get("errors")
    assert result["inspection"]["bbox"]["size"] == pytest.approx([20, 12, 8])
    assert result["inspection"]["raySections"]["bore"]["intervals"] == []
    assert result["inspection"]["volumeMm3"] == pytest.approx(20*12*8 - math.pi*2**2*8)
