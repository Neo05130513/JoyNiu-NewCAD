"""Real OCCT spatial diagnostics stay separate from engineering projections."""
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest
from PIL import Image

from app.cad_executor import build_plan_shape, execute_cad_plan, inspect_cad_draft
from app.cad_inspector import export_projections, export_spatial_views, inspect_shape, inspect_step
from app.geometry import get_cadquery


pytestmark = pytest.mark.skipif(get_cadquery() is None, reason="CadQuery is unavailable")


def block_plan():
    return {"version": "cad-plan-v1", "units": "mm", "name": "Spatial diagnostic fixture",
            "parameters": {"height": {"value": 7}},
            "features": [{"id": "body", "op": "box", "size": [13, 23, "height"], "origin": [41, -19, 5]}],
            "result": "body"}


def check_spatial_view(descriptor, directory):
    assert descriptor["viewType"] == "isometric" and descriptor["scope"] == "spatial_diagnostic"
    assert descriptor["orthographicEngineeringView"] is False
    assert descriptor["pixelRegistrationEligible"] is False and descriptor["productionReady"] is False
    assert descriptor["cameraDirection"] == [1, -1, 1]
    svg, png = Path(descriptor["path"]), Path(descriptor["pngPath"])
    assert svg.parent == png.parent == directory.resolve()
    assert descriptor["mimeType"] == "image/svg+xml" and descriptor["pngMimeType"] == "image/png"
    root = ET.parse(svg).getroot()
    assert root.attrib["data-view-type"] == "isometric"
    assert root.attrib["data-pixel-registration-eligible"] == "false"
    assert "not an orthographic engineering view" in "".join(root.itertext())
    lines = [[(float(x), float(y)) for x, y in re.findall(r"[ML]\s*([^,\s]+),([^\sML]+)", path.attrib.get("d", ""))]
             for path in root.iter("{http://www.w3.org/2000/svg}path")]
    points = [point for line in lines for point in line]
    assert points
    extent = [max(point[axis] for point in points) - min(point[axis] for point in points) for axis in (0, 1)]
    # Independent projection of the rectangular solid's eight corners. Both
    # in-plane axes must be foreshortened; this cannot be a renamed front view.
    expected = [(13 + 23) / math.sqrt(2), (13 + 23 + 2 * 7) / math.sqrt(6)]
    assert extent == pytest.approx(expected, abs=1e-7)
    edges = [(end[0] - start[0], end[1] - start[1]) for line in lines for start, end in zip(line, line[1:])]
    assert any(abs(dx) < 1e-7 and abs(dy) > 1 for dx, dy in edges)
    for slope in (-1 / math.sqrt(3), 1 / math.sqrt(3)):
        assert any(abs(dx) > 1 and math.isclose(dy / dx, slope, abs_tol=1e-7) for dx, dy in edges)
    with Image.open(png) as image:
        assert image.format == "PNG" and image.size == (960, 720)
        ink = image.crop((0, 50, 960, 720)).convert("L").point(lambda value: 255 if value < 128 else 0)
        bounds = ink.getbbox()
        assert bounds and ink.histogram()[255] > 1000
        assert (bounds[2] - bounds[0]) / (bounds[3] - bounds[1]) == pytest.approx(expected[0] / expected[1], abs=.02)


def test_inspector_keeps_three_engineering_views_and_exports_a_separate_fixed_spatial_view(tmp_path):
    shape, _, _ = build_plan_shape(block_plan())
    before = inspect_shape(shape)
    engineering = export_projections(shape, tmp_path / "engineering")
    assert set(engineering) == {"front", "top", "right"}
    spatial = export_spatial_views(shape, tmp_path / "diagnostics")
    assert set(spatial) == {"isometric"}
    check_spatial_view(spatial["isometric"], tmp_path / "diagnostics")
    after = inspect_shape(shape)
    assert after["bbox"]["size"] == pytest.approx(before["bbox"]["size"])
    assert after["volumeMm3"] == pytest.approx(before["volumeMm3"])
    assert after["solidCount"] == before["solidCount"] == 1


@pytest.mark.parametrize("worker", [execute_cad_plan, inspect_cad_draft])
def test_workers_return_optional_spatial_views_without_mixing_them_into_three_views(tmp_path, worker):
    options = {"include_projections": True} if worker is inspect_cad_draft else {}
    result = worker(block_plan(), tmp_path, include_isometric=True, **options)
    assert result["status"] == "succeeded" and result["valid"]
    assert result["execution"]["isolatedProcess"]
    assert result["drawingAgreement"] == "not_checked"
    assert set(result["spatialViews"]) == {"isometric"}
    check_spatial_view(result["spatialViews"]["isometric"], tmp_path / "spatial-views")
    if worker is inspect_cad_draft:
        assert result["execution"]["timeoutSeconds"] == 15
        assert result["scope"] == "draft_construction" and result["artifacts"] == {}
        assert set(result["draftViews"]) == {"front", "top", "right"}
        assert "productionReady" not in result
        assert not list(tmp_path.rglob("*.step")) and not list(tmp_path.rglob("*.glb"))
    else:
        assert set(result["artifacts"]["views"]) == {"front", "top", "right"}
        assert result["productionReady"] is False
        measured = inspect_step(result["artifacts"]["step"]["path"])
        assert measured["bbox"]["size"] == pytest.approx([13, 23, 7])
        assert measured["volumeMm3"] == pytest.approx(13 * 23 * 7)


@pytest.mark.parametrize("worker", [execute_cad_plan, inspect_cad_draft])
def test_spatial_diagnostics_are_opt_in_and_leave_default_results_unchanged(tmp_path, worker):
    result = worker(block_plan(), tmp_path)
    assert result["status"] == "succeeded"
    assert "spatialViews" not in result and not list(tmp_path.rglob("isometric.*"))


def test_isometric_only_draft_does_not_implicitly_generate_engineering_or_delivery_files(tmp_path):
    result = inspect_cad_draft(block_plan(), tmp_path, include_isometric=True)
    assert result["status"] == "succeeded" and result["artifacts"] == {}
    assert "draftViews" not in result
    assert {path.name for path in tmp_path.rglob("*") if path.is_file()} == {"isometric.svg", "isometric.png"}


@pytest.mark.parametrize("worker", [execute_cad_plan, inspect_cad_draft])
@pytest.mark.parametrize("state", ["unknown", "invalid_geometry", "timeout", "invalid_option"])
def test_failed_or_unfinished_workers_never_return_spatial_views(tmp_path, worker, state):
    plan = block_plan()
    options = {"include_isometric": True}
    if state == "unknown":
        plan["parameters"]["height"]["value"] = None
    elif state == "invalid_geometry":
        plan["parameters"]["height"]["value"] = -1
    elif state == "timeout":
        options["timeout_seconds"] = .05
    else:
        options["include_isometric"] = "../outside"
    result = worker(plan, tmp_path, **options)
    assert result["status"] == ("needs_input" if state == "unknown" else "failed")
    assert result["artifacts"] == {} and "spatialViews" not in result
    assert not list(tmp_path.rglob("isometric.*"))
    if state == "timeout":
        assert result["errors"][0]["code"] == "worker_timeout"
    elif state == "invalid_option":
        assert "boolean" in result["errors"][0]["message"]
