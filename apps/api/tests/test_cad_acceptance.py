import pytest

from app.cad_acceptance import acceptance_ray_probes, evaluate_cad_acceptance, validate_observations


def evidence(kind, expected, **kwargs):
    return {"id": "size", "kind": kind, "expected": expected, "source": {"type": "drawing", "fileIndex": 0, "view": "front", "text": "visible dimension"}, **kwargs}


def test_real_measurement_mismatch_cannot_pass_from_plan_value():
    result = evaluate_cad_acceptance({"bbox": {"size": [50, 30, 6]}, "parameters": {"thickness": 9}}, [evidence("bbox_size", 9, axis=2)])
    assert result["status"] == "failed"
    assert result["checks"][0]["actual"] == 6


def test_missing_expected_is_unknown_and_never_filled():
    assert evaluate_cad_acceptance({"bbox": {"size": [50, 30, 6]}}, [evidence("bbox_size", None, axis=2)])["status"] == "needs_input"
    assert evaluate_cad_acceptance({}, [evidence("solid_count", 1)])["status"] == "failed"
    assert evaluate_cad_acceptance({}, [])["status"] == "not_checked"


def test_cylinders_are_counted_by_axis_not_tessellation_or_split_face():
    cylinders = [{"diameter": 13, "axis": [0, 0, 1], "origin": [x, 0, z]} for x, z in ((-40, 0), (-40, 5), (40, 0))]
    result = evaluate_cad_acceptance({"cylinders": cylinders}, [evidence("cylinder", {"diameter": 13, "axis": [0, 0, -1], "count": 2})])
    assert result["status"] == "passed"
    assert result["checks"][0]["actual"]["count"] == 2
    assert evaluate_cad_acceptance({"cylinders": cylinders}, [evidence("cylinder", {"diameter": 30, "axis": [0, 0, 1]})])["status"] == "failed"


def test_opening_needs_material_probe_not_only_a_matching_cylinder():
    observation = evidence("ray_intervals", [], probe={"origin": [0, 0, 0], "direction": [0, 1, 0], "start": -50, "end": 50})
    assert acceptance_ray_probes([observation])[0]["id"] == "size"
    assert evaluate_cad_acceptance({"raySections": {"size": {"intervals": [[-25, 25]]}}}, [observation])["status"] == "failed"
    incomplete = evaluate_cad_acceptance({"raySections": {"size": {"intervals": []}}}, [observation])
    assert incomplete["status"] == "failed"
    assert incomplete["checks"][0]["actual"] is None
    assert "重新执行" in incomplete["checks"][0]["message"]


@pytest.mark.parametrize("change", [{"expected": "thickness"}, {"source": {}}, {"tolerance": 99}, {"axis": True}])
def test_rejects_circular_expectations_and_missing_evidence(change):
    item = evidence("bbox_size", 9, axis=2)
    item.update(change)
    with pytest.raises(ValueError):
        validate_observations([item])


def test_local_dimensions_cannot_all_be_mapped_to_the_global_envelope():
    whole = evidence("bbox_size", 60, axis=2)
    thickness = {**evidence("bbox_size", 7, axis=2), "id": "thickness"}
    with pytest.raises(ValueError, match="whole-solid"):
        validate_observations([whole, thickness])


def test_named_axis_aliases_preserve_exact_measurement_and_conflict_detection():
    requested = evidence("bbox_size", 9, axis=" z ")
    normalized = validate_observations([requested])
    assert requested["axis"] == " z " and normalized[0]["axis"] == 2
    assert evaluate_cad_acceptance({"bbox": {"size": [50, 30, 6]}}, normalized)["status"] == "failed"
    assert evaluate_cad_acceptance({"bbox": {"size": [50, 30, 9]}}, normalized)["status"] == "passed"
    with pytest.raises(ValueError, match="whole-solid"):
        validate_observations([requested, {**evidence("bbox_size", 60, axis=2), "id": "total"}])
