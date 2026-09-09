"""A parametric arched clevis support, built and audited as one OCCT solid.

X is the mounting-hole pitch, Y is the clevis-hole axis, and Z starts at the
feet's bottom face. The two concentric arch circles have their centre at Z=0.
The central bridge meets the R-arch at X=+/-earRadius: its height is derived,
not an extra pocket/slot guessed from the drawing. The old bracket is untouched.
"""

from __future__ import annotations

import math
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .geometry import (
    GeneratedArtifact,
    _cylinder_surface_data,
    _mesh_from_cadquery,
    cadquery_status,
    get_cadquery,
    mesh_to_glb,
)
from .schemas import ArchedClevisSupportParameters


def _dimension_issues(p: ArchedClevisSupportParameters) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def check(rule: str, field: str, passed: bool, message: str, actual: Any, expected: Any) -> None:
        issues.append({"ruleId": rule, "field": field, "severity": "error", "passed": bool(passed),
                       "message": message, "actual": actual, "expected": expected})

    for key, value in p.model_dump(by_alias=True).items():
        if isinstance(value, (int, float)):
            check(f"dimension.{key}", key, math.isfinite(value) and value > 0,
                  f"{key} must be a finite positive dimension", value, "> 0")
    check("arch.wall", "archInnerRadius", p.arch_inner_radius < p.arch_outer_radius,
          "The inner arch must leave an outer wall", p.arch_inner_radius, f"< {p.arch_outer_radius:g}")
    check("ear.arch_connection", "earRadius", p.ear_radius < p.arch_outer_radius,
          "The ear must fit within the outer arch", p.ear_radius, f"< {p.arch_outer_radius:g}")
    check("ear.hole_wall", "earHoleDiameter", p.ear_hole_diameter < 2 * p.ear_radius,
          "The ear hole must leave a radial wall", p.ear_hole_diameter, f"< {2*p.ear_radius:g}")
    check("ear.width_stack", "earGap", math.isclose(p.base_width, 2*p.ear_thickness+p.ear_gap, rel_tol=0, abs_tol=1e-6),
          "baseWidth must equal two earThickness values plus earGap", p.base_width, 2*p.ear_thickness+p.ear_gap)
    check("mount.ear_width", "mountEarRadius", 2*p.mount_ear_radius <= p.base_width,
          "The mounting ears must fit within baseWidth", 2*p.mount_ear_radius, f"<= {p.base_width:g}")
    check("mount.hole_wall", "mountHoleDiameter", p.mount_hole_diameter < 2*p.mount_ear_radius,
          "The mounting holes must leave a radial wall", p.mount_hole_diameter, f"< {2*p.mount_ear_radius:g}")
    check("mount.arch_clearance", "mountHoleCenterDistance", p.mount_hole_center_distance/2-p.mount_hole_diameter/2 > p.arch_outer_radius,
          "Mounting holes must lie outside the arch and only penetrate the feet",
          p.mount_hole_center_distance/2-p.mount_hole_diameter/2, f"> {p.arch_outer_radius:g}")
    check("bridge.wall", "earRadius", p.bridge_height > p.arch_inner_radius,
          "The flat bridge must remain above the inner arch", p.bridge_height, f"> {p.arch_inner_radius:g}")
    check("ear.hole_height", "earCenterHeight", p.ear_center_height-p.ear_hole_diameter/2 > p.bridge_height,
          "The clevis holes must be wholly above the bridge", p.ear_center_height-p.ear_hole_diameter/2, f"> {p.bridge_height:g}")
    check("base.height", "baseThickness", p.base_thickness < p.bridge_height,
          "The mounting feet must be lower than the bridge", p.base_thickness, f"< {p.bridge_height:g}")
    return issues


def build_arched_clevis_support_shape(parameters: ArchedClevisSupportParameters) -> Any:
    """Return a fused B-Rep; subtract the under-arch passage after the feet."""

    p = parameters
    failed = [issue for issue in _dimension_issues(p) if not issue["passed"]]
    if failed:
        raise ValueError("; ".join(issue["message"] for issue in failed))
    cq = get_cadquery()
    if cq is None:
        raise RuntimeError("arched_clevis_support_v1 requires CadQuery/OCCT; no substitute shape is generated")
    overlap = min(0.01, p.ear_thickness/100, p.base_thickness/100, (p.bridge_height-p.arch_inner_radius)/100)

    # Clip the outer cylinder at the bridge plane. No guessed recess is cut
    # into this bridge: the middle 30 mm Y gap ends exactly at this plane.
    outer = cq.Workplane("XZ").circle(p.arch_outer_radius).extrude(p.base_width/2, both=True)
    bridge_box = cq.Workplane("XY").box(2*p.arch_outer_radius, p.base_width, p.bridge_height,
                                        centered=(True, True, False))
    shape = outer.intersect(bridge_box)
    for side in (-1, 1):
        ear = (cq.Workplane("XZ")
               .moveTo(-p.ear_radius, p.bridge_height-overlap)
               .lineTo(p.ear_radius, p.bridge_height-overlap)
               .lineTo(p.ear_radius, p.ear_center_height)
               .threePointArc((0, p.total_height), (-p.ear_radius, p.ear_center_height))
               .close().extrude(p.ear_thickness/2, both=True)
               .translate((0, side*(p.ear_gap+p.ear_thickness)/2, 0)))
        shape = shape.union(ear)

    # The outer R15 foot ends and the connecting strip form a capsule in XY.
    # The strip overlaps the arch, and its middle is removed by the inner
    # R16 cutter below; it must not turn the open arch into a solid base plate.
    feet = cq.Workplane("XY").box(p.mount_hole_center_distance, 2*p.mount_ear_radius,
                                   p.base_thickness, centered=(True, True, False))
    for side in (-1, 1):
        foot = (cq.Workplane("XY").center(side*p.mount_hole_center_distance/2, 0)
                .circle(p.mount_ear_radius).extrude(p.base_thickness))
        feet = feet.union(foot)
    shape = shape.union(feet)
    inner = cq.Solid.makeCylinder(p.arch_inner_radius, p.base_width+2*overlap,
                                 cq.Vector(0, -p.base_width/2-overlap, 0), cq.Vector(0, 1, 0))
    clevis_holes = cq.Solid.makeCylinder(p.ear_hole_diameter/2, p.base_width+2*overlap,
                                        cq.Vector(0, -p.base_width/2-overlap, p.ear_center_height), cq.Vector(0, 1, 0))
    shape = shape.cut(cq.Workplane("XY").newObject([inner, clevis_holes]))
    for side in (-1, 1):
        hole = cq.Solid.makeCylinder(p.mount_hole_diameter/2, p.base_thickness+2*overlap,
                                    cq.Vector(side*p.mount_hole_center_distance/2, 0, -overlap), cq.Vector(0, 0, 1))
        shape = shape.cut(cq.Workplane("XY").newObject([hole]))
    return shape.clean()


def solid_ray_intervals(shape: Any, origin: tuple[float, float, float],
                        direction: tuple[float, float, float], start: float, end: float) -> list[list[float]]:
    """Measure solid intervals on a line in mm using OCCT face intersections.

    Midpoint classification removes tangent contacts and duplicated seams.
    This is geometry evidence, not an echo of the input parameter values.
    """
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

    value = shape.val() if hasattr(shape, "val") else shape
    unit_length = math.sqrt(sum(component*component for component in direction))
    unit = tuple(component/unit_length for component in direction)
    intersection = IntCurvesFace_ShapeIntersector()
    intersection.Load(value.wrapped, 1e-7)
    intersection.Perform(gp_Lin(gp_Pnt(*origin), gp_Dir(*unit)), start, end)
    if not intersection.IsDone():
        raise RuntimeError("OCCT ray intersection did not complete")
    boundaries = sorted([start, end] + [intersection.WParameter(i) for i in range(1, intersection.NbPnt()+1)])
    distinct: list[float] = []
    for boundary in boundaries:
        if not distinct or abs(boundary-distinct[-1]) > 1e-6:
            distinct.append(float(boundary))
    intervals: list[list[float]] = []
    for lo, hi in zip(distinct, distinct[1:]):
        midpoint = tuple(origin[i]+unit[i]*(lo+hi)/2 for i in range(3))
        if value.isInside(midpoint, 1e-7):
            if intervals and math.isclose(intervals[-1][1], lo, abs_tol=1e-6):
                intervals[-1][1] = hi
            else:
                intervals.append([lo, hi])
    return intervals


def _intervals_match(actual: list[list[float]], expected: list[list[float]]) -> bool:
    return len(actual) == len(expected) and all(
        math.isclose(a, b, rel_tol=0, abs_tol=1e-5)
        for got, wanted in zip(actual, expected) for a, b in zip(got, wanted)
    )


def audit_arched_clevis_support(parameters: ArchedClevisSupportParameters, shape: Any) -> dict[str, Any]:
    """Audit actual B-Rep surfaces and ray sections at functional datums."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    p = parameters
    value = shape.val() if hasattr(shape, "val") else shape
    # CadQuery's default BoundingBox uses cached triangulation when present.
    # Tessellating the exact same solid can therefore enlarge its reported
    # bounds by mesh deflection. Always measure the underlying B-Rep curves
    # and surfaces, independently of whether a GLB was generated beforehand.
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(value.wrapped, box, False, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    lengths = (xmax-xmin, ymax-ymin, zmax-zmin)
    cylinders = [_cylinder_surface_data(face) for face in value.Faces() if face.geomType() == "CYLINDER"]

    def cylinder_at(radius: float, axis: int, center: tuple[float, float, float]) -> bool:
        return any(origin is not None and direction is not None
                   and math.isclose(actual_radius, radius, abs_tol=1e-5)
                   and abs(direction[axis]) > 0.999
                   and all(math.isclose(origin[i], center[i], abs_tol=1e-5) for i in range(3) if i != axis)
                   for actual_radius, origin, direction in cylinders)

    span = max(p.base_length, p.base_width, p.total_height) + 1
    ear_x = (p.ear_radius+p.ear_hole_diameter/2)/2
    foot_y = (p.mount_ear_radius+p.mount_hole_diameter/2)/2
    rays = {
        "bridgeCenterZ": solid_ray_intervals(shape, (0, 0, 0), (0, 0, 1), -1, span),
        "bridgeQuarterZ": solid_ray_intervals(shape, (p.ear_radius/2, 0, 0), (0, 0, 1), -1, span),
        "clevisWallY": solid_ray_intervals(shape, (ear_x, 0, p.ear_center_height), (0, 1, 0), -span, span),
        "clevisHoleY": solid_ray_intervals(shape, (0, 0, p.ear_center_height), (0, 1, 0), -span, span),
    }
    for side, name in ((-1, "left"), (1, "right")):
        x = side*p.mount_hole_center_distance/2
        rays[f"{name}MountHoleZ"] = solid_ray_intervals(shape, (x, 0, 0), (0, 0, 1), -1, span)
        rays[f"{name}FootZ"] = solid_ray_intervals(shape, (x, foot_y, 0), (0, 0, 1), -1, span)
    bridge_center = _intervals_match(rays["bridgeCenterZ"], [[p.arch_inner_radius, p.bridge_height]])
    inner_quarter = math.sqrt(max(0.0, p.arch_inner_radius**2-(p.ear_radius/2)**2))
    bridge_quarter = _intervals_match(rays["bridgeQuarterZ"], [[inner_quarter, p.bridge_height]])
    checks = {
        "kernelShapeValid": bool(value.isValid()),
        "singleSolid": len(value.Solids()) == 1,
        "bboxMatchesParameters": all(math.isclose(a, b, abs_tol=1e-5) for a, b in zip(
            lengths, (p.base_length, p.base_width, p.total_height))),
        "outerArchPresent": cylinder_at(p.arch_outer_radius, 1, (0, 0, 0)),
        "innerArchPresent": cylinder_at(p.arch_inner_radius, 1, (0, 0, 0)) and bridge_center,
        "flatBridgePresent": bridge_center and bridge_quarter,
        "clevisGapAndThicknessPresent": _intervals_match(rays["clevisWallY"], [[-p.base_width/2, -p.ear_gap/2], [p.ear_gap/2, p.base_width/2]]),
        "clevisHolePairPresent": cylinder_at(p.ear_hole_diameter/2, 1, (0, 0, p.ear_center_height)) and not rays["clevisHoleY"],
        "clevisOuterRadiusPresent": cylinder_at(p.ear_radius, 1, (0, 0, p.ear_center_height)),
        "mountHolePairPresent": all(cylinder_at(p.mount_hole_diameter/2, 2, (side*p.mount_hole_center_distance/2, 0, 0)) for side in (-1, 1))
                                 and not rays["leftMountHoleZ"] and not rays["rightMountHoleZ"],
        "mountEarPairPresent": all(cylinder_at(p.mount_ear_radius, 2, (side*p.mount_hole_center_distance/2, 0, 0)) for side in (-1, 1)),
        "mountHolesOnlyThroughFeet": all(_intervals_match(rays[f"{name}FootZ"], [[0, p.base_thickness]]) for name in ("left", "right")),
    }
    return {
        **checks, "topologyAuditEngine": "cadquery-occt", "kernelBacked": True,
        "topologyAuditPassed": all(checks.values()), "solidCount": len(value.Solids()),
        "faceCount": len(value.Faces()), "volumeMm3": float(value.Volume()),
        "bboxLength": float(lengths[0]), "bboxWidth": float(lengths[1]), "bboxHeight": float(lengths[2]),
        "bboxMeasurementSource": "exact-brep-surfaces-without-triangulation",
        "bridgeHeightMeasured": rays["bridgeCenterZ"][0][1] if len(rays["bridgeCenterZ"]) == 1 else None,
        "raySectionsMm": rays,
        "auditScope": "OCCT solid validity, envelope, cylindrical surfaces, and eight functional ray sections; not manufacturing/tolerance certification",
    }


def validate_arched_clevis_support(parameters: ArchedClevisSupportParameters, *, engine: str | None = None) -> dict[str, Any]:
    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    issues = _dimension_issues(p)
    dimensions_valid = all(issue["passed"] for issue in issues)
    audit: dict[str, Any] = {"kernelBacked": False, "topologyAuditPassed": False, "topologyAuditEngine": "not-run"}
    if dimensions_valid and selected_engine == "cadquery-occt":
        try:
            audit = audit_arched_clevis_support(p, build_arched_clevis_support_shape(p))
        except Exception as exc:
            audit["kernelAuditError"] = f"{type(exc).__name__}: {exc}"
    issues.append({"ruleId": "topology.audit", "severity": "error" if selected_engine == "cadquery-occt" else "info",
                   "passed": bool(audit["topologyAuditPassed"]), "actual": audit,
                   "expected": "one valid arched clevis solid with open arch, flat bridge, two ears and four correctly oriented holes",
                   "message": "OCCT envelope, surface and section audit" if audit["kernelBacked"] else "OCCT topology was not verified"})
    valid = dimensions_valid and (bool(audit["topologyAuditPassed"]) if selected_engine == "cadquery-occt" else True)
    ready = bool(valid and audit["kernelBacked"])
    return {"valid": valid, "productionReady": ready, "engine": selected_engine,
            "parameters": p.model_dump(mode="json", by_alias=True), "issues": issues,
            "metrics": {**audit, "previewOnly": not ready,
                        "derivedDimensions": {"baseLength": p.base_length, "totalHeight": p.total_height, "bridgeHeight": p.bridge_height}}}


def generate_arched_clevis_support_artifacts(parameters: ArchedClevisSupportParameters,
                                             formats: Iterable[str] = ("step", "glb"), *,
                                             require_cadquery: bool = False) -> list[GeneratedArtifact]:
    """Export the same audited OCCT solid to STEP and GLB; never substitute a bracket."""
    requested = list(dict.fromkeys(str(item).lower() for item in formats))
    if not requested or any(item not in {"step", "glb"} for item in requested):
        raise ValueError("supported output formats are step and glb")
    # Even permissive clients must receive this topology or an honest error.
    # The new recipe has no approximation fallback claiming to be this part.
    shape = build_arched_clevis_support_shape(parameters)
    audit = audit_arched_clevis_support(parameters, shape)
    if not audit["topologyAuditPassed"]:
        raise RuntimeError("Arched clevis OCCT shape failed its functional topology audit")
    mesh = _mesh_from_cadquery(shape) if "glb" in requested else None
    if "glb" in requested and mesh is None:
        raise RuntimeError("Arched clevis OCCT tessellation failed; no substitute preview is generated")
    artifacts = []
    for format_name in requested:
        if format_name == "glb":
            artifacts.append(GeneratedArtifact("glb", mesh_to_glb(mesh, name="JoyNiu Arched Clevis Support"),
                                               "cadquery-tessellation", False,
                                               ["GLB is a tessellated visualization; retain STEP for B-Rep editing."]))
            continue
        from cadquery import exporters
        with tempfile.TemporaryDirectory(prefix="joyniu-arched-clevis-") as directory:
            path = Path(directory) / "arched-clevis.step"
            try:
                exporters.export(shape, str(path), exportType="STEP")
                data = path.read_bytes()
            except Exception as exc:
                raise RuntimeError(f"Arched clevis STEP export failed: {type(exc).__name__}: {exc}") from exc
        artifacts.append(GeneratedArtifact("step", data, "cadquery-occt", True))
    return artifacts


__all__ = ["build_arched_clevis_support_shape", "audit_arched_clevis_support", "solid_ray_intervals",
           "validate_arched_clevis_support", "generate_arched_clevis_support_artifacts"]
