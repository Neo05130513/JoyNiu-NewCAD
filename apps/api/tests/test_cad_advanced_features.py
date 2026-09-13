"""Real OCCT regressions for bounded advanced operations and STEP exchange."""

import math

import pytest

from app.cad_executor import build_plan_shape, execute_cad_plan
from app.cad_plan import PlanValidationError, cad_plan_schema, validate_plan
from app.geometry import get_cadquery

cq = get_cadquery()
requires_kernel = pytest.mark.skipif(cq is None, reason="CadQuery is unavailable")


def plan(*features, parameters=None):
    return {"version": "cad-plan-v1", "units": "mm", "parameters": parameters or {},
            "features": list(features), "result": features[-1]["id"]}


BOX = {"id": "base", "op": "box", "size": [10, 10, 10]}
GEAR = {"id": "gear", "op": "gear", "module": 2, "teeth": 24, "pressureAngle": 20,
        "width": 8, "boreDiameter": 8}
SPRING = {"id": "spring", "op": "spring", "meanDiameter": 20, "wireDiameter": 2, "pitch": 5, "turns": 3}


def exchanged(tmp_path, value):
    shape, _, trace = build_plan_shape(value)
    path = tmp_path / "model.step"
    cq.exporters.export(shape.val(), str(path), exportType="STEP")
    original, restored = shape.val(), cq.importers.importStep(str(path)).val()
    assert restored.isValid() and restored.Solids()
    assert len(restored.Solids()) == len(original.Solids())
    assert restored.Volume() == pytest.approx(original.Volume(), rel=2e-5)
    assert trace[-1]["state"] == "succeeded"
    return restored


@requires_kernel
def test_involute_gear_standard_dimensions_tooth_thickness_and_bore_survive_step(tmp_path):
    solid = exchanged(tmp_path, plan(GEAR))
    bounds = solid.BoundingBox()
    assert [bounds.xlen, bounds.ylen, bounds.zlen] == pytest.approx([52, 52, 8], abs=2e-5)
    assert not solid.isInside(cq.Vector(0, 0, 4))
    # At the pitch circle, standard tooth thickness is pi*m/2 and the
    # half-angle is pi/(2*z). Test both sides of all 24 independent teeth.
    for tooth in range(24):
        center = tooth * math.tau / 24
        for sign in (-1, 1):
            for fraction, inside in ((.95, True), (1.05, False)):
                angle = center + sign * fraction * math.pi / 48
                assert solid.isInside(cq.Vector(24 * math.cos(angle), 24 * math.sin(angle), 4)) is inside
    assert 0 < solid.Volume() < math.pi * (26 ** 2 - 4 ** 2) * 8


@requires_kernel
def test_gear_expression_revision_changes_real_diameter_and_profile_shift(tmp_path):
    feature = {**GEAR, "module": "m", "width": "w", "profileShift": .3}
    value = plan(feature, parameters={"m": {"value": 1.5}, "w": {"value": 6}})
    solid = exchanged(tmp_path, value)
    assert solid.BoundingBox().xlen == pytest.approx(1.5 * (24 + 2 + .6), abs=2e-5)
    value["parameters"]["m"]["value"] = 2.5
    larger = exchanged(tmp_path, value)
    assert larger.BoundingBox().xlen == pytest.approx(2.5 * 26.6, abs=2e-5)
    assert larger.Volume() > solid.Volume()


@requires_kernel
@pytest.mark.parametrize("lefthand", [False, True])
def test_helical_spring_has_real_open_coils_and_expected_wire_volume(tmp_path, lefthand):
    solid = exchanged(tmp_path, plan({**SPRING, "lefthand": lefthand}))
    centerline_length = 3 * math.sqrt((20 * math.pi) ** 2 + 5 ** 2)
    assert solid.Volume() == pytest.approx(math.pi * centerline_length, rel=.002)
    assert not solid.isInside(cq.Vector(0, 0, 7.5))
    # The centerline at the start of turn 2 is material, halfway between
    # turns at the same azimuth remains an actual air gap.
    assert solid.isInside(cq.Vector(10, 0, 5))
    assert not solid.isInside(cq.Vector(10, 0, 7.5))
    assert len(solid.Solids()) == 1


@requires_kernel
def test_rotate_moves_shape_about_declared_world_axis_without_scaling(tmp_path):
    solid = exchanged(tmp_path, plan({"id": "bar", "op": "box", "size": [10, 2, 3], "origin": [5, 0, 0]},
        {"id": "turned", "op": "rotate", "input": "bar", "axisStart": [0, 0, 0], "axisEnd": [0, 0, 1], "angle": 90}))
    bounds = solid.BoundingBox()
    assert [bounds.xmin, bounds.xmax, bounds.ymin, bounds.ymax] == pytest.approx([-2, 0, 5, 15])
    assert solid.Volume() == pytest.approx(60)


@requires_kernel
def test_linear_pattern_retains_separate_copies_and_can_fuse_overlaps(tmp_path):
    source = {"id": "block", "op": "box", "size": [2, 3, 4]}
    solid = exchanged(tmp_path, plan(source, {"id": "row", "op": "linear_pattern", "input": "block", "count": 4, "vector": [5, 0, 0]}))
    assert len(solid.Solids()) == 4
    assert solid.Volume() == pytest.approx(96)
    assert solid.BoundingBox().xlen == pytest.approx(17)
    joined = exchanged(tmp_path, plan(source, {"id": "row", "op": "linear_pattern", "input": "block", "count": 4, "vector": [1, 0, 0], "fuse": True}))
    assert len(joined.Solids()) == 1
    assert joined.Volume() == pytest.approx(5 * 3 * 4)


@requires_kernel
def test_circular_pattern_full_turn_has_no_duplicate_endpoint(tmp_path):
    solid = exchanged(tmp_path, plan({"id": "pin", "op": "cylinder", "radius": 1, "height": 3, "origin": [5, 0, 0]},
        {"id": "pins", "op": "circular_pattern", "input": "pin", "count": 4, "axisStart": [0, 0, 0], "axisEnd": [0, 0, 1], "angle": 360}))
    assert len(solid.Solids()) == 4
    assert solid.Volume() == pytest.approx(12 * math.pi)
    for point in [(5, 0, 1), (0, 5, 1), (-5, 0, 1), (0, -5, 1)]:
        assert solid.isInside(cq.Vector(*point))


@requires_kernel
def test_chamfer_removes_real_corner_material(tmp_path):
    solid = exchanged(tmp_path, plan(BOX, {"id": "bevel", "op": "chamfer", "input": "base", "length": 1, "edges": "all"}))
    assert 900 < solid.Volume() < 980
    assert solid.isInside(cq.Vector(5, 5, 5))
    assert not solid.isInside(cq.Vector(.1, .1, 5))
    assert solid.BoundingBox().xlen == pytest.approx(10)


@requires_kernel
def test_shell_removes_selected_face_but_keeps_exact_wall_and_floor(tmp_path):
    solid = exchanged(tmp_path, plan(BOX, {"id": "cup", "op": "shell", "input": "base", "thickness": -1, "faces": ["maxZ"]}))
    assert solid.Volume() == pytest.approx(1000 - 8 * 8 * 9)
    assert solid.isInside(cq.Vector(5, 5, .5))
    assert solid.isInside(cq.Vector(.5, 5, 5))
    assert not solid.isInside(cq.Vector(5, 5, 5))


@requires_kernel
def test_sweep_round_profile_straight_and_bent_path(tmp_path):
    straight = exchanged(tmp_path, plan({"id": "pipe", "op": "sweep", "radius": 2, "path": [[0, 0, 0], [0, 0, 20]]}))
    assert straight.Volume() == pytest.approx(math.pi * 4 * 20)
    elbow = exchanged(tmp_path, plan({"id": "elbow", "op": "sweep", "radius": 2, "path": [[0, 0, 0], [0, 0, 20], [15, 0, 20]]}))
    assert elbow.isInside(cq.Vector(0, 0, 10)) and elbow.isInside(cq.Vector(10, 0, 20))
    assert not elbow.isInside(cq.Vector(8, 0, 8))


@requires_kernel
def test_loft_circle_and_rectangle_sections_have_expected_frustum_volumes(tmp_path):
    round_part = exchanged(tmp_path, plan({"id": "transition", "op": "loft", "sections": [
        {"origin": [0, 0, 0], "radius": 8}, {"origin": [0, 0, 20], "radius": 4}]}))
    assert round_part.Volume() == pytest.approx(math.pi * 20 / 3 * (64 + 32 + 16), rel=1e-5)
    rect_part = exchanged(tmp_path, plan({"id": "transition", "op": "loft", "ruled": True, "sections": [
        {"origin": [0, 0, 0], "width": 8, "height": 8}, {"origin": [0, 0, 20], "width": 4, "height": 4}]}))
    assert rect_part.Volume() == pytest.approx(20 / 3 * (64 + 32 + 16), rel=1e-5)


@pytest.mark.parametrize("feature", [
    {"id": "bad", "op": "rotate", "input": "future", "axisStart": [0, 0, 0], "axisEnd": [0, 0, 1], "angle": 30},
    {"id": "bad", "op": "sweep", "radius": 1, "path": [[0, 0, 0]]},
    {"id": "bad", "op": "loft", "sections": [{"origin": [0, 0, 0], "radius": 1, "script": "x"}, {"origin": [0, 0, 1], "radius": 1}]},
    {"id": "bad", "op": "shell", "input": "base", "thickness": 1, "faces": ["__import__('os')"]},
    {**GEAR, "module": "unknown + 1"},
    {**SPRING, "lefthand": "false"},
])
def test_advanced_structure_rejects_untrusted_or_undefined_inputs(feature):
    with pytest.raises(PlanValidationError):
        validate_plan(plan(BOX, feature))


@requires_kernel
@pytest.mark.parametrize("feature", [
    {**GEAR, "teeth": 12},  # unsupported undercut
    {**GEAR, "teeth": 121},
    {**GEAR, "teeth": 24.5},
    {**GEAR, "boreDiameter": 50},
    {**GEAR, "module": -1},
    {**SPRING, "pitch": 2},  # intersecting coils
    {**SPRING, "turns": 41},
    {"id": "bad", "op": "rotate", "input": "base", "axisStart": [0, 0, 0], "axisEnd": [0, 0, 0], "angle": 30},
    {"id": "bad", "op": "linear_pattern", "input": "base", "count": 1000, "vector": [20, 0, 0]},
    {"id": "bad", "op": "linear_pattern", "input": "base", "count": 4, "vector": [0, 0, 0]},
    {"id": "bad", "op": "shell", "input": "base", "thickness": 0, "faces": ["maxZ"]},
    {"id": "bad", "op": "sweep", "radius": 2, "path": [[0, 0, 0], [20, 0, 20], [20, 0, 0], [0, 0, 20]]},
    {"id": "bad", "op": "sweep", "radius": 2, "path": [[0, 0, 0], [20, 0, 0], [10, 0, 0]]},
    {"id": "bad", "op": "loft", "sections": [{"origin": [0, 0, 1], "radius": 1}, {"origin": [0, 0, 0], "radius": 2}]},
])
def test_invalid_geometry_parameters_are_rejected_before_unsafe_construction(feature):
    with pytest.raises(PlanValidationError) as error:
        build_plan_shape(plan(BOX, feature))
    assert error.value.feature_id == feature["id"]


@requires_kernel
def test_nested_patterns_are_bounded_before_large_copy_allocation():
    value = plan(BOX,
        {"id": "first", "op": "linear_pattern", "input": "base", "count": 64, "vector": [20, 0, 0]},
        {"id": "second", "op": "linear_pattern", "input": "first", "count": 64, "vector": [0, 20, 0]})
    with pytest.raises(PlanValidationError, match="complexity"):
        build_plan_shape(value)


@requires_kernel
def test_advanced_operation_uses_original_isolated_worker_and_exports_step(tmp_path):
    result = execute_cad_plan(plan(BOX, {"id": "cup", "op": "shell", "input": "base", "thickness": -1, "faces": ["maxZ"]}), tmp_path)
    assert result["status"] == "succeeded", result
    assert result["execution"]["isolatedProcess"] and not result["execution"]["arbitraryCodeAllowed"]
    assert result["productionReady"] is False
    shape = cq.importers.importStep(result["artifacts"]["step"]["path"]).val()
    assert shape.isValid() and shape.Volume() == pytest.approx(424)


def test_schema_distinguishes_3d_rotation_axes_from_2d_revolve_axes():
    schema = cad_plan_schema()
    operations = {entry["properties"]["op"]["const"]: entry for entry in schema["properties"]["features"]["items"]["oneOf"]}
    assert operations["rotate"]["properties"]["axisStart"]["minItems"] == 3
    assert operations["profile_revolve"]["properties"]["axisStart"]["minItems"] == 2
    assert set(operations) >= {"gear", "spring", "rotate", "linear_pattern", "circular_pattern", "shell", "chamfer", "sweep", "loft"}
