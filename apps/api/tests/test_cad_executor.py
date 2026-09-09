"""Held-out primitive combinations; drawing-specific numbers live only here."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.cad_executor import build_plan_shape, execute_cad_plan, inspect_cad_draft
from app.cad_inspector import inspect_shape, inspect_step
from app.cad_plan import PlanValidationError
from app.geometry import get_cadquery

pytestmark = pytest.mark.skipif(get_cadquery() is None, reason="CadQuery is unavailable")


def clevis_drawing_plan():
    """10.jpg, assembled entirely from generic operations, never recipe dispatch."""
    values = {"archR": 28, "innerR": 16, "width": 50, "baseT": 9, "earR": 15,
              "earHoleD": 13, "earZ": 40, "earT": 10, "gap": 30,
              "footR": 15, "mountHoleD": 13, "pitch": 80}
    parameters = {key: {"value": value, "source": {"type": "fixture_drawing_evidence", "drawing": "10.jpg"}} for key, value in values.items()}
    parameters["bridgeZ"] = {"expression": "sqrt(archR**2-earR**2)"}
    features = [
        {"id": "outer", "op": "cylinder", "radius": "archR", "height": "width", "origin": [0, "-width/2", 0], "direction": [0, 1, 0]},
        {"id": "clip", "op": "box", "size": ["2*archR", "width", "bridgeZ"], "origin": ["-archR", "-width/2", 0]},
        {"id": "bridge", "op": "intersect", "inputs": ["outer", "clip"]},
        {"id": "frontEar", "op": "profile_extrude", "plane": "XZ", "origin": [0, "width/2", 0],
         "start": ["-earR", "bridgeZ-0.01"], "segments": [
             {"type": "line", "to": ["earR", "bridgeZ-0.01"]},
             {"type": "line", "to": ["earR", "earZ"]},
             {"type": "arc", "through": [0, "earZ+earR"], "to": ["-earR", "earZ"]}], "distance": "earT"},
        {"id": "backEar", "op": "translate", "input": "frontEar", "vector": [0, "-gap-earT", 0]},
        {"id": "earBridge", "op": "union", "inputs": ["bridge", "frontEar", "backEar"]},
        {"id": "footStrip", "op": "box", "size": ["pitch", "2*footR", "baseT"], "origin": ["-pitch/2", "-footR", 0]},
        {"id": "leftFoot", "op": "cylinder", "radius": "footR", "height": "baseT", "origin": ["-pitch/2", 0, 0]},
        {"id": "rightFoot", "op": "translate", "input": "leftFoot", "vector": ["pitch", 0, 0]},
        {"id": "blank", "op": "union", "inputs": ["earBridge", "footStrip", "leftFoot", "rightFoot"]},
        {"id": "underArch", "op": "cylinder", "radius": "innerR", "height": "width+0.02", "origin": [0, "-width/2-0.01", 0], "direction": [0, 1, 0]},
        {"id": "earHoles", "op": "cylinder", "radius": "earHoleD/2", "height": "width+0.02", "origin": [0, "-width/2-0.01", "earZ"], "direction": [0, 1, 0]},
        {"id": "leftHole", "op": "cylinder", "radius": "mountHoleD/2", "height": "baseT+0.02", "origin": ["-pitch/2", 0, -0.01]},
        {"id": "rightHole", "op": "translate", "input": "leftHole", "vector": ["pitch", 0, 0]},
        {"id": "finished", "op": "cut", "inputs": ["blank", "underArch", "earHoles", "leftHole", "rightHole"]},
    ]
    return {"version": "cad-plan-v1", "name": "Evidence fixture 10", "units": "mm", "parameters": parameters,
            "features": features, "result": "finished"}


def clevis_probes():
    return [{"id": "clevisHole", "origin": [0, 0, 40], "direction": [0, 1, 0], "start": -30, "end": 30},
            {"id": "clevisWalls", "origin": [11, 0, 40], "direction": [0, 1, 0], "start": -30, "end": 30},
            {"id": "foot", "origin": [-40, 10, 0], "direction": [0, 0, 1], "start": -1, "end": 20},
            {"id": "mountHole", "origin": [-40, 0, 0], "direction": [0, 0, 1], "start": -1, "end": 60},
            {"id": "bridge", "origin": [0, 0, 0], "direction": [0, 0, 1], "start": -1, "end": 60}]


def test_drawing_10_uses_generic_features_and_real_step_reimport(tmp_path):
    plan = clevis_drawing_plan()
    result = execute_cad_plan(plan, tmp_path, ray_probes=clevis_probes())
    assert result["status"] == "succeeded", result
    assert result["execution"]["isolatedProcess"]
    assert result["drawingAgreement"] == "not_checked"
    assert not result["productionReady"]
    observed = inspect_step(result["artifacts"]["step"]["path"], ray_probes=clevis_probes())
    assert observed["valid"] and observed["solidCount"] == 1
    assert observed["bbox"]["size"] == pytest.approx([110, 50, 55], abs=1e-6)
    rays = observed["raySections"]
    assert rays["clevisHole"]["intervals"] == []
    assert rays["mountHole"]["intervals"] == []
    assert rays["clevisWalls"]["intervals"] == [[-25, -15], [15, 25]]
    assert rays["foot"]["intervals"] == [[0, 9]]
    assert rays["bridge"]["intervals"][0] == pytest.approx([16, 559**0.5])
    assert json.loads((tmp_path / "plan.json").read_text())["features"] == plan["features"]
    for name, view in result["artifacts"]["views"].items():
        import xml.etree.ElementTree as ET
        from app.cad_inspector import _PATH_POINT
        svg = Path(view["path"]).read_text()
        assert svg.lstrip().startswith("<?xml")
        points = [(float(x), float(y)) for path in ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}path")
                  for _, x, y in _PATH_POINT.findall(path.attrib["d"])]
        projected_size = [max(p[i] for p in points) - min(p[i] for p in points) for i in (0, 1)]
        # HLR SVG edges are sampled polylines; exact dimensions above come
        # from re-imported B-Rep surfaces, not these display coordinates.
        assert projected_size == pytest.approx({"front": [110, 55], "top": [110, 50], "right": [50, 55]}[name], abs=.01)
        assert Path(view["pngPath"]).read_bytes().startswith(b"\x89PNG")
    assert Path(result["artifacts"]["glb"]["path"]).read_bytes()[:4] == b"glTF"


def test_parameter_revision_changes_actual_hole_and_survives_json_roundtrip():
    plan = json.loads(json.dumps(clevis_drawing_plan()))
    original, _, _ = build_plan_shape(plan)
    plan["parameters"]["earHoleD"]["value"] = 17
    modified, _, _ = build_plan_shape(json.loads(json.dumps(plan)))
    observed = inspect_shape(modified)
    assert observed["bbox"]["size"] == pytest.approx([110, 50, 55])
    assert any(abs(face["diameter"] - 17) < 1e-6 and abs(face["axis"][1]) > .999 for face in observed["cylinders"])
    assert observed["volumeMm3"] < inspect_shape(original)["volumeMm3"]


def test_heldout_l_profile_extrusion_and_hole_are_not_a_registered_recipe():
    plan = {"version": "cad-plan-v1", "parameters": {"wall": {"value": 4}, "depth": {"value": 17}}, "features": [
        {"id": "angle", "op": "profile_extrude", "plane": "XY", "start": [0, 0], "segments": [
            {"type": "line", "to": [31, 0]}, {"type": "line", "to": [31, "wall"]},
            {"type": "line", "to": ["wall", "wall"]}, {"type": "line", "to": ["wall", 23]},
            {"type": "line", "to": [0, 23]}], "distance": "depth"},
        {"id": "hole", "op": "cylinder", "radius": 1.25, "height": 19, "origin": [19, 2, -1]},
        {"id": "finished", "op": "cut", "inputs": ["angle", "hole"]}], "result": "finished"}
    shape, _, _ = build_plan_shape(plan)
    observed = inspect_shape(shape)
    assert observed["bbox"]["size"] == pytest.approx([31, 23, 17])
    import math
    assert observed["volumeMm3"] == pytest.approx((31*4+19*4-math.pi*1.25**2)*17)


def test_heldout_revolved_sleeve_and_fillet():
    plan = {"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "sleeve", "op": "profile_revolve", "plane": "XZ", "start": [3, 0], "segments": [
            {"type": "line", "to": [8, 0]}, {"type": "line", "to": [8, 12]}, {"type": "line", "to": [3, 12]}],
         "axisStart": [0, 0], "axisEnd": [0, 1]},
        {"id": "rounded", "op": "fillet", "input": "sleeve", "radius": 0.5, "edges": "all"}], "result": "rounded"}
    shape, _, trace = build_plan_shape(plan)
    observed = inspect_shape(shape)
    assert observed["valid"] and observed["solidCount"] == 1
    assert observed["bbox"]["size"] == pytest.approx([16, 16, 12])
    assert trace[-1]["featureId"] == "rounded"


def test_unknown_dimension_stops_before_process_or_artifacts(tmp_path):
    plan = clevis_drawing_plan()
    plan["parameters"]["baseT"] = {"value": None, "question": "Is this thickness 6 or 9?"}
    result = execute_cad_plan(plan, tmp_path)
    assert result["status"] == "needs_input"
    assert result["missingParameters"] == ["baseT"]
    assert result["artifacts"] == {}
    assert not list(tmp_path.iterdir())


def test_worker_timeout_is_bounded_and_returns_no_deliverable(tmp_path):
    result = execute_cad_plan(clevis_drawing_plan(), tmp_path, timeout_seconds=.05)
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "worker_timeout"
    assert result["execution"]["durationMs"] < 5000
    assert result["artifacts"] == {}


def test_failed_feature_is_identified_for_targeted_repair(tmp_path):
    plan = {"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "box", "op": "box", "size": [10, 10, 10]},
        {"id": "tooLargeFillet", "op": "fillet", "input": "box", "radius": 100, "edges": "all"}], "result": "tooLargeFillet"}
    result = execute_cad_plan(plan, tmp_path)
    assert result["status"] == "failed"
    assert result["errors"][0]["featureId"] == "tooLargeFillet"
    assert result["artifacts"] == {}


def test_invalid_probe_cannot_force_unsafe_geometry_query():
    shape, _, _ = build_plan_shape({"version": "cad-plan-v1", "parameters": {}, "features": [
        {"id": "box", "op": "box", "size": [10, 10, 10]}], "result": "box"})
    with pytest.raises(PlanValidationError):
        inspect_shape(shape, ray_probes=[{"id": "ray", "origin": [0, 0, 0], "direction": [0, 0, 0], "start": -1, "end": 1}])


def partial_draft():
    return {"version": "cad-plan-v1", "parameters": {"width": {"value": 23}, "thickness": {"value": 7}},
            "features": [{"id": "base", "op": "box", "size": [13, "width", "thickness"]},
                         {"id": "future_hole_tool", "op": "cylinder", "radius": 2, "height": 9}],
            "result": "base"}


def test_draft_inspection_checks_real_partial_geometry_without_any_deliverable(tmp_path):
    result = inspect_cad_draft(partial_draft(), tmp_path)
    assert result["status"] == "succeeded" and result["valid"]
    assert result["scope"] == "draft_construction" and result["drawingAgreement"] == "not_checked"
    assert result["execution"]["isolatedProcess"] and result["execution"]["timeoutSeconds"] == 15
    assert result["resolvedParameters"] == {"width": 23, "thickness": 7}
    inspection = result["inspection"]
    assert inspection["kernelBacked"] and inspection["engine"] == "cadquery-occt"
    assert inspection["bbox"]["size"] == pytest.approx([13, 23, 7])
    assert inspection["solidCount"] == 1 and inspection["valid"]
    assert [step["featureId"] for step in result["featureTrace"]] == ["base", "future_hole_tool"]
    assert "acceptance" not in inspection and "productionReady" not in result
    assert result["artifacts"] == {} and not list(tmp_path.rglob("*"))


def test_draft_inspection_can_return_three_real_views_without_delivery_artifacts(tmp_path):
    from PIL import Image

    result = inspect_cad_draft(partial_draft(), tmp_path, include_projections=True)
    assert result["status"] == "succeeded" and result["valid"]
    assert result["scope"] == "draft_construction" and result["drawingAgreement"] == "not_checked"
    assert result["execution"]["isolatedProcess"] and result["execution"]["timeoutSeconds"] == 15
    assert result["artifacts"] == {} and "productionReady" not in result
    assert "acceptance" not in result["inspection"]
    assert set(result["draftViews"]) == {"front", "top", "right"}
    expected_aspects = {"front": 13 / 7, "top": 13 / 23, "right": 23 / 7}
    for view, descriptor in result["draftViews"].items():
        svg, png = Path(descriptor["path"]), Path(descriptor["pngPath"])
        assert svg.parent == png.parent == tmp_path.resolve() / "draft-views"
        assert descriptor["mimeType"] == "image/svg+xml" and descriptor["pngMimeType"] == "image/png"
        assert "<path" in svg.read_text()
        with Image.open(png) as image:
            assert image.size == (960, 720) and image.format == "PNG"
            # Exclude the title: each actual projected box outline must exist
            # and match its independent front/top/right dimension ratio.
            ink = image.crop((0, 50, 960, 720)).convert("L").point(lambda pixel: 255 if pixel < 128 else 0)
            bounds = ink.getbbox()
            assert bounds is not None and ink.histogram()[255] > 500
            assert (bounds[2] - bounds[0]) / (bounds[3] - bounds[1]) == pytest.approx(expected_aspects[view], abs=.02)
    assert not list(tmp_path.rglob("*.step")) and not list(tmp_path.rglob("*.glb"))
    assert {path.suffix for path in tmp_path.rglob("*") if path.is_file()} == {".svg", ".png"}


@pytest.mark.parametrize("include_projections", [False, True])
def test_draft_inspection_rejects_self_crossing_profile_and_keeps_prior_feature_trace(tmp_path, include_projections):
    plan = partial_draft()
    plan["features"].append({"id": "crossing_profile", "op": "profile_extrude", "plane": "XY", "start": [0, 0],
                             "segments": [{"type": "line", "to": [12, 12]}, {"type": "line", "to": [0, 12]},
                                          {"type": "line", "to": [12, 0]}], "distance": 4})
    plan["result"] = "crossing_profile"
    result = inspect_cad_draft(plan, tmp_path, include_projections=include_projections)
    assert result["status"] == "failed" and not result["valid"]
    assert result["errors"][0]["featureId"] == "crossing_profile"
    assert result["errors"][0]["code"] == "invalid_geometry"
    assert [step["featureId"] for step in result["featureTrace"]] == ["base", "future_hole_tool"]
    assert result["scope"] == "draft_construction" and result["drawingAgreement"] == "not_checked"
    assert result["artifacts"] == {} and "draftViews" not in result and not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("include_projections", [False, True])
def test_draft_inspection_with_unknown_parameter_stops_before_geometry(tmp_path, include_projections):
    plan = partial_draft()
    plan["parameters"]["thickness"] = {"value": None, "question": "What is the required thickness?"}
    result = inspect_cad_draft(plan, tmp_path, include_projections=include_projections)
    assert result["status"] == "needs_input" and result["missingParameters"] == ["thickness"]
    assert result["scope"] == "draft_construction" and result["drawingAgreement"] == "not_checked"
    assert result["artifacts"] == {} and result["featureTrace"] == []
    assert "draftViews" not in result and not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("include_projections", [False, True])
def test_draft_inspection_uses_the_same_enforced_worker_deadline(tmp_path, include_projections):
    result = inspect_cad_draft(partial_draft(), tmp_path, timeout_seconds=.05, include_projections=include_projections)
    assert result["status"] == "failed" and result["errors"][0]["code"] == "worker_timeout"
    assert result["execution"]["isolatedProcess"] and result["execution"]["durationMs"] < 5000
    assert result["scope"] == "draft_construction" and result["drawingAgreement"] == "not_checked"
    assert result["artifacts"] == {} and "draftViews" not in result and not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("option", ["../outside", 1, {"outputDir": "/tmp"}])
def test_draft_projections_cannot_use_an_option_as_an_output_path(tmp_path, option):
    result = inspect_cad_draft(partial_draft(), tmp_path, include_projections=option)
    assert result["status"] == "failed" and result["artifacts"] == {}
    assert "boolean" in result["errors"][0]["message"]
    assert "draftViews" not in result and not list(tmp_path.rglob("*"))
