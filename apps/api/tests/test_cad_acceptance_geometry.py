"""Acceptance checks against freshly built and re-imported real solids.

The source ledger is fixed independently of each intentionally faulty model.
No expected measurement is copied from a plan or an inspection result.
"""

from copy import deepcopy
import json

import pytest

from app.cad_acceptance import acceptance_ray_probes, evaluate_cad_acceptance
from app.cad_agent_store import CadRunStore
from app.cad_executor import build_plan_shape, execute_cad_plan
from app.cad_inspector import inspect_shape, inspect_step
from app.geometry import get_cadquery

pytestmark = pytest.mark.skipif(get_cadquery() is None, reason="CadQuery is unavailable")


def source_ledger():
    source = {"type": "drawing", "fileIndex": 0, "view": "orthographic test drawing", "text": "40 x 30 x 12 plate; single through bore diameter 8 at X14 Y16"}
    return [
        {"id": "length", "kind": "bbox_size", "axis": 0, "expected": 40, "source": source},
        {"id": "width", "kind": "bbox_size", "axis": 1, "expected": 30, "source": source},
        {"id": "thickness", "kind": "bbox_size", "axis": 2, "expected": 12, "source": source},
        {"id": "bodyCount", "kind": "solid_count", "expected": 1, "source": source},
        {"id": "boreSizeAndLocation", "kind": "cylinder", "expected": {"diameter": 8, "axis": [0, 0, 1], "center": [14, 16, 0], "count": 1}, "source": source},
        {"id": "borePassage", "kind": "ray_intervals", "expected": [],
         "probe": {"origin": [14, 16, 0], "direction": [0, 0, 1], "start": -1, "end": 13}, "source": source},
        {"id": "plateMaterial", "kind": "ray_intervals", "expected": [[0, 12]],
         "probe": {"origin": [25, 16, 0], "direction": [0, 0, 1], "start": -1, "end": 13}, "source": source},
    ]


def plate_plan(*, length=40, diameter=8, hole_x=14, floor=None, remove_hole=False, side_hole=False):
    features = [{"id": "plate", "op": "box", "size": [length, 30, 12]}]
    if not remove_hole:
        cylinder = {"id": "boreTool", "op": "cylinder", "radius": diameter / 2, "height": 13 if floor is None else 12 - floor + 1,
                    "origin": [hole_x, 16, -.5 if floor is None else floor]}
        if side_hole:
            cylinder.update({"origin": [hole_x, -1, 6], "direction": [0, 1, 0], "height": 32})
        features += [cylinder, {"id": "boredPlate", "op": "cut", "inputs": ["plate", "boreTool"]}]
    return {"version": "cad-plan-v1", "parameters": {}, "features": features, "result": features[-1]["id"]}


def inspect_and_compare(plan, ledger=None):
    ledger = ledger or source_ledger()
    shape, _, _ = build_plan_shape(plan)
    inspection = inspect_shape(shape, ray_probes=acceptance_ray_probes(ledger))
    return inspection, evaluate_cad_acceptance(inspection, ledger)


def failed_ids(comparison):
    return {item["id"] for item in comparison["checks"] if item["passed"] is False}


def test_real_single_through_hole_passes_independent_size_location_and_material_checks():
    inspection, result = inspect_and_compare(plate_plan())
    assert inspection["kernelBacked"] and inspection["valid"]
    assert result["status"] == "passed"
    assert inspection["raySections"]["borePassage"]["intervals"] == []
    assert inspection["raySections"]["plateMaterial"]["intervals"] == [[0, 12]]


@pytest.mark.parametrize("changes,failed", [
    ({"length": 43}, {"length"}),
    ({"diameter": 6}, {"boreSizeAndLocation"}),
    ({"hole_x": 15}, {"boreSizeAndLocation"}),
    ({"floor": 3}, {"borePassage"}),
    ({"remove_hole": True}, {"boreSizeAndLocation", "borePassage"}),
    ({"side_hole": True}, {"boreSizeAndLocation", "borePassage"}),
])
def test_valid_but_wrong_solids_fail_the_specific_independent_observation(changes, failed):
    inspection, result = inspect_and_compare(plate_plan(**changes))
    assert inspection["valid"] and inspection["solidCount"] == 1
    assert result["status"] == "failed"
    assert failed_ids(result) == failed


def test_one_unrelated_parameter_change_does_not_fix_an_existing_bore_error():
    _, previous = inspect_and_compare(plate_plan(diameter=6))
    _, modified = inspect_and_compare(plate_plan(diameter=6, length=41))
    assert failed_ids(previous) == {"boreSizeAndLocation"}
    assert failed_ids(modified) == {"boreSizeAndLocation", "length"}


def test_cylinder_surface_alone_cannot_distinguish_a_boss_from_a_hole():
    plan = {"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "plate", "op": "box", "size": [40, 30, 4]},
        {"id": "boss", "op": "cylinder", "radius": 4, "height": 8, "origin": [14, 16, 4]},
        {"id": "part", "op": "union", "inputs": ["plate", "boss"]}], "result": "part"}
    inspection, result = inspect_and_compare(plan)
    cylinder = next(item for item in result["checks"] if item["id"] == "boreSizeAndLocation")
    assert cylinder["passed"] is True
    assert inspection["raySections"]["borePassage"]["intervals"] == [[0, 12]]
    assert "borePassage" in failed_ids(result)


def test_missing_source_dimension_remains_unknown_despite_valid_real_geometry():
    ledger = deepcopy(source_ledger())
    ledger[2]["expected"] = None
    _, comparison = inspect_and_compare(plate_plan(), ledger)
    assert comparison["status"] == "needs_input"
    assert comparison["checks"][2]["passed"] is None


def test_ray_sections_survive_export_reimport_and_durable_revision_storage(tmp_path):
    ledger = source_ledger()
    run_id = "cad_" + "e" * 32
    store = CadRunStore(tmp_path / "runs")
    result = execute_cad_plan(plate_plan(), store.directory(run_id) / "build", ray_probes=acceptance_ray_probes(ledger))
    assert result["status"] == "succeeded", result
    reread = inspect_step(result["artifacts"]["step"]["path"], ray_probes=acceptance_ray_probes(ledger))
    reread["acceptance"] = evaluate_cad_acceptance(reread, ledger)
    assert reread["acceptance"]["status"] == "passed"
    record = {"runId": run_id, "revision": 1, "owner": "fixture", "plan": result["plan"], "inspection": reread, "observations": ledger,
              "state": {"cadPlan": result["plan"], "observations": ledger}}
    store.save(record)
    reloaded = CadRunStore(store.root).load(run_id)
    assert reloaded["inspection"]["raySections"] == reread["raySections"]
    assert acceptance_ray_probes(reloaded["observations"]) == acceptance_ray_probes(ledger)
    assert evaluate_cad_acceptance(reloaded["inspection"], reloaded["observations"])["status"] == "passed"
    assert json.loads(json.dumps(reloaded))["inspection"]["raySections"]["borePassage"]["intervals"] == []
    assert reloaded["inspection"]["raySections"]["borePassage"]["coverage"]["complete"] is True
    assert reloaded["inspection"]["raySections"]["borePassage"]["fullMaterialIntervals"] == []


def section_ledger(identifier, origin, direction, start, end, expected):
    return [{"id": identifier, "kind": "ray_intervals", "expected": expected,
             "probe": {"origin": origin, "direction": direction, "start": start, "end": end},
             "source": {"type": "user", "text": "Independent complete material-section requirement"}}]


@pytest.mark.parametrize("bottom,top,passed", [(0, 9, True), (0, 10, False), (-1, 9, False)])
def test_complete_plate_thickness_cannot_pass_from_a_clipped_probe(bottom, top, passed):
    ledger = section_ledger("base_plate_thickness", [50, 0, 0], [0, 0, 1], 0, 9, [[0, 9]])
    submitted = deepcopy(ledger)
    plan = {"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "plate", "op": "box", "size": [20, 20, top-bottom], "origin": [40, -10, bottom]}], "result": "plate"}
    inspection, comparison = inspect_and_compare(plan, ledger)
    section = inspection["raySections"]["base_plate_thickness"]
    assert section["requestedRange"] == [0, 9] and section["intervals"] == [[0, 9]]
    assert section["fullMaterialIntervals"] == [[bottom, top]]
    assert section["coverage"]["complete"] is True
    assert section["coverage"]["range"][0] < bottom and section["coverage"]["range"][1] > top
    assert comparison["checks"][0]["passed"] is passed
    assert comparison["checks"][0]["actual"] == [[bottom, top]]
    assert comparison["checks"][0]["measurement"]["windowIntervals"] == [[0, 9]]
    assert ledger == submitted


@pytest.mark.parametrize("extension,passed", [(0, True), (1, False)])
def test_complete_ear_section_detects_material_outside_both_probe_endpoints(extension, passed):
    ledger = section_ledger("upper_ear", [10, -25, 40], [0, 1, 0], 0, 50, [[0, 10], [40, 50]])
    plan = {"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "left_ear", "op": "box", "size": [4, 10+extension, 4], "origin": [8, -25-extension, 38]},
        {"id": "right_ear", "op": "box", "size": [4, 10+extension, 4], "origin": [8, 15, 38]},
        {"id": "ears", "op": "union", "inputs": ["left_ear", "right_ear"]}], "result": "ears"}
    inspection, comparison = inspect_and_compare(plan, ledger)
    section = inspection["raySections"]["upper_ear"]
    assert section["intervals"] == [[0, 10], [40, 50]]
    assert section["fullMaterialIntervals"] == [[-extension, 10], [40, 50+extension]]
    assert comparison["checks"][0]["passed"] is passed


@pytest.mark.parametrize("floor,passed", [(None, True), (3, False)])
def test_short_void_probe_cannot_hide_the_floor_of_a_blind_hole(floor, passed):
    ledger = section_ledger("through_bore", [14, 16, 12], [0, 0, -1], 0, 5, [])
    inspection, comparison = inspect_and_compare(plate_plan(floor=floor), ledger)
    section = inspection["raySections"]["through_bore"]
    assert section["intervals"] == []
    assert section["coverage"]["requestedRangeCoversBody"] is False
    assert section["fullMaterialIntervals"] == ([] if floor is None else [[9, 12]])
    assert comparison["checks"][0]["passed"] is passed


@pytest.mark.parametrize("missing", ["coverage", "fullMaterialIntervals", "requestedRange", "complete", "extent", "position"])
def test_missing_stale_or_incomplete_full_section_evidence_cannot_pass(missing):
    inspection, _ = inspect_and_compare(plate_plan())
    section = inspection["raySections"]["borePassage"]
    if missing == "complete":
        section["coverage"]["complete"] = False
    elif missing == "extent":
        section["coverage"]["range"] = [1, 11]
    elif missing == "position":
        section["origin"][0] += 1
    else:
        del section[missing]
    comparison = evaluate_cad_acceptance(inspection, source_ledger())
    assert comparison["status"] == "failed" and failed_ids(comparison) == {"borePassage"}
    failed = next(item for item in comparison["checks"] if item["id"] == "borePassage")
    assert failed["actual"] is None and "重新执行" in failed["message"]


def test_full_section_coverage_handles_non_axis_aligned_nonunit_directions():
    root3 = 3**.5
    ledger = section_ledger("diagonal", [1, 1, 1], [-2, -2, -2], -.1, .1, [[-root3, root3]])
    plan = {"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "cube", "op": "box", "size": [2, 2, 2]}], "result": "cube"}
    inspection, comparison = inspect_and_compare(plan, ledger)
    assert inspection["raySections"]["diagonal"]["intervals"] == [[-.1, .1]]
    assert comparison["status"] == "passed"
    assert comparison["checks"][0]["actual"][0] == pytest.approx([-root3, root3])
