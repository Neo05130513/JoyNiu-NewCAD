"""Bracket geometry, validation and portable artifact exporters.

CadQuery/OCCT is used whenever it can be imported. The service intentionally
keeps a small, dependency-free tessellation path as well: CI machines and
developer laptops often cannot install OCCT wheels, but they should still be
able to inspect an auditable GLB and a clearly-labelled faceted STEP preview.
The fallback is deterministic and uses the same coordinate recipe as the
CadQuery builder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import Any, Iterable, Sequence

from .schemas import (
    BracketParameters,
    Severity,
    ValidationIssue,
    ValidationReport,
)


# ---------------------------------------------------------------------------
# CadQuery discovery

_CADQUERY_ATTEMPTED = False
_CADQUERY: Any = None
_CADQUERY_ERROR: str | None = None


def get_cadquery() -> Any | None:
    """Return the imported CadQuery module, or None with a reason.

    Import is lazy so the health endpoint and fallback mode work without OCCT.
    Setting JOYNIU_DISABLE_CADQUERY=1 is useful for deterministic local
    development and for testing the fallback path.
    """

    global _CADQUERY_ATTEMPTED, _CADQUERY, _CADQUERY_ERROR
    if _CADQUERY_ATTEMPTED:
        return _CADQUERY
    _CADQUERY_ATTEMPTED = True
    if os.getenv("JOYNIU_DISABLE_CADQUERY", "").lower() in {"1", "true", "yes"}:
        _CADQUERY_ERROR = "disabled by JOYNIU_DISABLE_CADQUERY"
        return None
    try:
        import cadquery  # type: ignore

        _CADQUERY = cadquery
    except Exception as exc:  # pragma: no cover - depends on host wheels
        _CADQUERY_ERROR = f"{type(exc).__name__}: {exc}"
        _CADQUERY = None
    return _CADQUERY


def cadquery_status() -> dict[str, Any]:
    module = get_cadquery()
    raw_version = getattr(module, "__version__", None) if module else None
    return {
        "available": module is not None,
        # CadQuery currently exposes a ``packaging.version.Version`` object on
        # some releases; convert it to a plain string so FastAPI/JSON clients
        # can always serialize the health payload.
        "version": str(raw_version) if raw_version is not None else None,
        "error": None if module else _CADQUERY_ERROR,
        "engine": "cadquery-occt" if module else "faceted-fallback",
    }


# ---------------------------------------------------------------------------
# Validation


_DIMENSION_FIELDS = (
    "base_length",
    "base_width",
    "base_thickness",
    "upper_length",
    "upper_width",
    "upper_height",
    "total_height",
    "notch_opening",
    "notch_radius",
    "boss_diameter",
    "boss_center_distance",
)

# The rectangular lead-in of the saddle cutter needs a microscopic overlap
# below the top plane for OCCT's boolean regularisation to retain the nominal
# 40 mm opening.  Keep the value in one place so the analytic volume estimate
# and the B-Rep recipe describe the same geometry.
_SLOT_OVERLAP_MM = 0.01


def _value(p: BracketParameters, name: str) -> float:
    value = getattr(p, name)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _circle_rectangle_intersection_area(
    cx: float,
    cy: float,
    radius: float,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
) -> float:
    """Estimate a disk/rectangle overlap area with deterministic Simpson rule.

    Bosses can be partly inside the upper box (the acceptance drawing places
    their axes on the box's side planes).  Subtracting that overlap avoids the
    several-percent overestimate produced by simply adding two full cylinder
    volumes.  The bounded numerical integral is deterministic, dependency-free
    and more than sufficient for an audit estimate in millimetres.
    """

    if radius <= 0 or x1 <= x0 or y1 <= y0:
        return 0.0
    left = max(x0, cx - radius)
    right = min(x1, cx + radius)
    if right <= left:
        return 0.0

    # Simpson integration handles the smooth clipped-circle profile well.  A
    # fixed even subdivision keeps the result reproducible across hosts.
    subdivisions = 1024
    step = (right - left) / subdivisions

    def section_length(x: float) -> float:
        distance = x - cx
        half_height = math.sqrt(max(0.0, radius * radius - distance * distance))
        lower = max(y0, cy - half_height)
        upper = min(y1, cy + half_height)
        return max(0.0, upper - lower)

    total = section_length(left) + section_length(right)
    for index in range(1, subdivisions):
        weight = 4.0 if index % 2 else 2.0
        total += weight * section_length(left + index * step)
    return max(0.0, total * step / 3.0)


def _circle_circle_intersection_area(radius: float, distance: float) -> float:
    """Return the overlap area of two equal-radius circles."""

    if radius <= 0:
        return 0.0
    distance = abs(distance)
    if distance >= 2.0 * radius:
        return 0.0
    if distance <= 1e-12:
        return math.pi * radius * radius
    ratio = max(-1.0, min(1.0, distance / (2.0 * radius)))
    return max(
        0.0,
        2.0 * radius * radius * math.acos(ratio)
        - 0.5 * distance * math.sqrt(max(0.0, 4.0 * radius * radius - distance * distance)),
    )


def _estimate_bracket_volume(parameters: BracketParameters) -> float:
    """Estimate the fused solid volume using the canonical feature recipe."""

    p = parameters
    values = [_value(p, field_name) for field_name in _DIMENSION_FIELDS]
    boss_height = (
        _value(p, "boss_height")
        if p.boss_height is not None
        else _value(p, "upper_height")
    )
    if not all(math.isfinite(value) and value > 0 for value in (*values, boss_height)):
        return 0.0

    base_volume = p.base_length * p.base_width * p.base_thickness
    upper_volume = p.upper_length * p.upper_width * p.upper_height
    boss_radius = p.boss_diameter / 2.0
    boss_volume = 2.0 * math.pi * boss_radius * boss_radius * boss_height

    # Cylinders start on the base top.  Only the portion inside the upper box
    # is already present in ``upper_volume`` and must be subtracted from the
    # added boss volume.
    overlap_height = min(boss_height, p.upper_height)
    upper_x0, upper_x1 = -p.upper_length / 2.0, p.upper_length / 2.0
    upper_y0, upper_y1 = -p.upper_width / 2.0, p.upper_width / 2.0
    boss_overlap_area = 0.0
    for center_x in (-p.boss_center_distance / 2.0, p.boss_center_distance / 2.0):
        boss_overlap_area += _circle_rectangle_intersection_area(
            center_x,
            0.0,
            boss_radius,
            upper_x0,
            upper_x1,
            upper_y0,
            upper_y1,
        )
    boss_volume -= boss_overlap_area * overlap_height
    # Avoid double-counting if a caller deliberately places the two bosses
    # closer than one diameter (normal acceptance dimensions are separated).
    boss_volume -= _circle_circle_intersection_area(
        boss_radius, p.boss_center_distance
    ) * boss_height

    # The horizontal R-notch removes the lower half of a cylinder through the
    # upper width.  The tiny rectangular lead-in contributes two side strips
    # of width (opening/2 - radius) and depth _SLOT_OVERLAP_MM.
    straight = max(0.0, p.notch_opening / 2.0 - p.notch_radius)
    notch_area = (
        0.5 * math.pi * p.notch_radius * p.notch_radius
        + 2.0 * straight * _SLOT_OVERLAP_MM
    )
    notch_volume = notch_area * p.upper_width
    estimate = base_volume + upper_volume + boss_volume - notch_volume
    return max(0.0, estimate) if math.isfinite(estimate) else 0.0


def validate_bracket(
    parameters: BracketParameters,
    *,
    engine: str | None = None,
) -> ValidationReport:
    """Run manufacturing-oriented geometric checks and return every issue.

    The report is intentionally non-throwing. This lets a UI show all
    conflicting dimensions in one pass and preserve the previous valid model.
    valid means no error-level issue; warnings are allowed for a preview.
    """

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    issues: list[ValidationIssue] = []

    def check(
        rule_id: str,
        condition: bool,
        message: str,
        *,
        severity: Severity = Severity.error,
        actual: Any = None,
        expected: Any = None,
    ) -> None:
        issues.append(
            ValidationIssue(
                ruleId=rule_id,
                severity=severity,
                passed=bool(condition),
                message=message,
                actual=actual,
                expected=expected,
            )
        )

    finite_positive = True
    for name in _DIMENSION_FIELDS:
        value = _value(p, name)
        ok = math.isfinite(value) and value > 0
        finite_positive = finite_positive and ok
        check(
            f"dimension.{name}",
            ok,
            f"{name} must be a finite number greater than 0",
            actual=value,
            expected="> 0",
        )

    boss_height = float(p.resolved_boss_height)
    boss_height_ok = math.isfinite(boss_height) and boss_height > 0
    check(
        "dimension.boss_height",
        boss_height_ok,
        "bossHeight must be a finite number greater than 0",
        actual=boss_height,
        expected="> 0",
    )

    if finite_positive:
        base_l, base_w, base_t = p.base_length, p.base_width, p.base_thickness
        upper_l, upper_w, upper_h = p.upper_length, p.upper_width, p.upper_height
        total_h = p.total_height
        opening, radius = p.notch_opening, p.notch_radius
        diameter, center_dist = p.boss_diameter, p.boss_center_distance

        height_ok = math.isclose(total_h, base_t + upper_h, abs_tol=0.01)
        check(
            "stack.total_height",
            height_ok,
            "totalHeight must equal baseThickness + upperHeight",
            actual=total_h,
            expected=round(base_t + upper_h, 4),
        )
        check(
            "envelope.upper_length",
            upper_l <= base_l + 1e-6,
            "upperLength must fit inside baseLength",
            actual=upper_l,
            expected=f"<= {base_l:g}",
        )
        check(
            "envelope.upper_width",
            upper_w <= base_w + 1e-6,
            "upperWidth must fit inside baseWidth",
            actual=upper_w,
            expected=f"<= {base_w:g}",
        )
        check(
            "notch.opening",
            opening < upper_l - 1e-6,
            "notchOpening must leave side walls in the upper body",
            actual=opening,
            expected=f"< {upper_l:g}",
        )
        check(
            "notch.radius",
            radius <= opening / 2 + 1e-6,
            "notchRadius must not exceed half of notchOpening",
            actual=radius,
            expected=f"<= {opening / 2:g}",
        )
        arc_center_z = total_h
        arc_bottom_z = arc_center_z - radius
        check(
            "notch.arc_center",
            math.isclose(arc_center_z, total_h, abs_tol=0.01),
            "U-slot arc centre is constrained to the total-height top plane",
            actual=arc_center_z,
            expected=total_h,
        )
        check(
            "notch.bottom",
            arc_bottom_z > base_t + 1e-6,
            "U-slot bottom must remain above the base top",
            actual=arc_bottom_z,
            expected=f"> {base_t:g}",
        )
        check(
            "notch.depth",
            radius <= upper_h + 1e-6,
            "notchRadius must fit within upperHeight",
            severity=Severity.warning,
            actual=radius,
            expected=f"<= {upper_h:g}",
        )
        bosses_fit_length = center_dist + diameter <= base_l + 1e-6
        check(
            "bosses.base_length_clearance",
            bosses_fit_length,
            "boss centres and radii must fit inside the base length",
            actual=center_dist + diameter,
            expected=f"<= {base_l:g}",
        )
        bosses_fit_width = diameter <= base_w + 1e-6
        check(
            "bosses.base_width_clearance",
            bosses_fit_width,
            "boss diameter must fit inside the base width",
            actual=diameter,
            expected=f"<= {base_w:g}",
        )
        boss_height_ok = boss_height <= total_h - base_t + 1e-6
        check(
            "bosses.height",
            boss_height_ok,
            "bossHeight must not exceed the upper stack height",
            severity=Severity.warning,
            actual=boss_height,
            expected=f"<= {total_h - base_t:g}",
        )
        side_wall = (upper_l - opening) / 2
        check(
            "manufacturing.side_wall",
            side_wall >= 2.0,
            "remaining U-slot side wall is at least 2 mm",
            severity=Severity.warning,
            actual=round(side_wall, 4),
            expected=">= 2",
        )
        boss_edge_clearance = (base_l - (center_dist + diameter)) / 2
        check(
            "manufacturing.boss_edge_clearance",
            boss_edge_clearance >= 1.0,
            "bosses retain at least 1 mm edge clearance",
            severity=Severity.warning,
            actual=round(boss_edge_clearance, 4),
            expected=">= 1",
        )

    metrics: dict[str, float | int | str | bool] = {}
    # Add a kernel/topology audit when OCCT is available. In fallback mode the
    # same semantic checks are evaluated analytically and explicitly marked as
    # non-kernel evidence.
    audit = audit_geometry(
        p,
        engine=selected_engine,
        try_kernel=selected_engine == "cadquery-occt",
    )
    for key, value in audit.items():
        if isinstance(value, (bool, int, float, str)):
            if key not in {"engine"}:
                metrics[key] = value
    audit_passed = bool(audit.get("topologyAuditPassed", False))
    check(
        "topology.audit",
        audit_passed,
        "topology, bounding box, volume and key-feature audit",
        severity=Severity.error if selected_engine == "cadquery-occt" else Severity.info,
        actual={
            "solidCount": audit.get("solidCount"),
            "faceCount": audit.get("faceCount"),
            "volumeMm3": audit.get("volumeMm3"),
            "notchArcPresent": audit.get("notchArcPresent"),
            "bossPairPresent": audit.get("bossPairPresent"),
            "notchArcCenterZ": audit.get("notchArcCenterZ"),
            "notchBottomZ": audit.get("notchBottomZ"),
            "bossCenterDistanceMeasured": audit.get("bossCenterDistanceMeasured"),
            "bossHeightMeasured": audit.get("bossHeightMeasured"),
        },
        expected="one valid solid with R15 arc and two boss cylinders",
    )

    errors = [i for i in issues if i.severity == Severity.error and not i.passed]
    valid = not errors
    # A fallback artifact is useful for review but must never be represented as
    # production-ready CAD, even when all dimensions pass.
    production_ready = (
        valid
        and selected_engine == "cadquery-occt"
        and bool(audit.get("kernelBacked"))
        and audit_passed
    )
    # Keep measured kernel values as the source of truth.  The legacy
    # ``bounding*`` aliases are retained for existing clients, but must not
    # overwrite a topology audit with the requested parameters (otherwise a
    # shifted/trimmed shape could look correct in the UI).  In analytic
    # fallback mode the audit values are deterministic estimates.
    metrics.update({
        "boundingLength": float(metrics.get("bboxLength", p.base_length)),
        "boundingWidth": float(metrics.get("bboxWidth", p.base_width)),
        "boundingHeight": float(metrics.get("bboxHeight", p.total_height)),
        "upperSideWall": round(max(0.0, (p.upper_length - p.notch_opening) / 2), 4),
        "bossEdgeClearance": round(
            max(0.0, (p.base_length - p.boss_center_distance - p.boss_diameter) / 2),
            4,
        ),
        "notchArcCenterZ": float(metrics.get("notchArcCenterZ", p.total_height)),
        "notchBottomZ": float(
            metrics.get(
                "notchBottomZ",
                float(p.total_height) - float(p.notch_radius),
            )
        ),
    })
    metrics["previewOnly"] = not production_ready
    if finite_positive:
        metrics["estimatedVolumeMm3"] = round(
            _estimate_bracket_volume(p), 3
        )

    return ValidationReport(
        valid=valid,
        productionReady=production_ready,
        engine=selected_engine,
        parameters=p,
        issues=issues,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Portable mesh builder


@dataclass
class Mesh:
    positions: list[float] = field(default_factory=list)
    normals: list[float] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)

    def add_triangle(
        self,
        a: Sequence[float],
        b: Sequence[float],
        c: Sequence[float],
    ) -> None:
        ax, ay, az = a
        bx, by, bz = b
        cx, cy, cz = c
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length < 1e-12:
            return
        normal = (nx / length, ny / length, nz / length)
        start = len(self.positions) // 3
        for point in (a, b, c):
            self.positions.extend(float(v) for v in point)
            self.normals.extend(normal)
        self.indices.extend((start, start + 1, start + 2))

    def add_quad(
        self,
        a: Sequence[float],
        b: Sequence[float],
        c: Sequence[float],
        d: Sequence[float],
    ) -> None:
        self.add_triangle(a, b, c)
        self.add_triangle(a, c, d)


def _box(
    mesh: Mesh,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    z0: float,
    z1: float,
) -> None:
    if x1 <= x0 or y1 <= y0 or z1 <= z0:
        return
    v000 = (x0, y0, z0)
    v100 = (x1, y0, z0)
    v110 = (x1, y1, z0)
    v010 = (x0, y1, z0)
    v001 = (x0, y0, z1)
    v101 = (x1, y0, z1)
    v111 = (x1, y1, z1)
    v011 = (x0, y1, z1)
    mesh.add_quad(v000, v010, v110, v100)
    mesh.add_quad(v001, v101, v111, v011)
    mesh.add_quad(v000, v100, v101, v001)
    mesh.add_quad(v010, v011, v111, v110)
    mesh.add_quad(v000, v001, v011, v010)
    mesh.add_quad(v100, v110, v111, v101)


def _wedge_segment(
    mesh: Mesh,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    z_base: float,
    z_top0: float,
    z_top1: float,
) -> None:
    if x1 <= x0 or z_top0 <= z_base or z_top1 <= z_base:
        return
    a0 = (x0, y0, z_base)
    a1 = (x1, y0, z_base)
    b1 = (x1, y0, z_top1)
    b0 = (x0, y0, z_top0)
    c0 = (x0, y1, z_base)
    c1 = (x1, y1, z_base)
    d1 = (x1, y1, z_top1)
    d0 = (x0, y1, z_top0)
    mesh.add_quad(a0, a1, b1, b0)
    mesh.add_quad(c0, d0, d1, c1)
    mesh.add_quad(a0, c0, c1, a1)
    mesh.add_quad(b0, b1, d1, d0)
    mesh.add_quad(a0, b0, d0, c0)
    mesh.add_quad(a1, c1, d1, b1)


def _cylinder(
    mesh: Mesh,
    cx: float,
    cy: float,
    z0: float,
    height: float,
    radius: float,
    segments: int = 48,
) -> None:
    if radius <= 0 or height <= 0:
        return
    segments = max(12, min(128, int(segments)))
    z1 = z0 + height
    center_bottom = (cx, cy, z0)
    center_top = (cx, cy, z1)
    ring0 = []
    ring1 = []
    for i in range(segments):
        angle = 2 * math.pi * i / segments
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        ring0.append((x, y, z0))
        ring1.append((x, y, z1))
    for i in range(segments):
        j = (i + 1) % segments
        mesh.add_triangle(center_bottom, ring0[j], ring0[i])
        mesh.add_triangle(center_top, ring1[i], ring1[j])
        mesh.add_quad(ring0[i], ring0[j], ring1[j], ring1[i])


def build_fallback_mesh(parameters: BracketParameters) -> Mesh:
    """Build a deterministic faceted preview using the documented recipe."""

    p = parameters
    mesh = Mesh()
    base_l = max(0.001, float(p.base_length))
    base_w = max(0.001, float(p.base_width))
    base_t = max(0.001, float(p.base_thickness))
    upper_l = max(0.001, min(float(p.upper_length), base_l))
    upper_w = max(0.001, min(float(p.upper_width), base_w))
    upper_h = max(0.001, float(p.upper_height))
    opening = max(0.001, min(float(p.notch_opening), upper_l - 0.002))
    radius = max(0.001, min(float(p.notch_radius), opening / 2))
    top = base_t + upper_h
    _box(mesh, -base_l / 2, base_l / 2, -base_w / 2, base_w / 2, 0, base_t)

    x_outer = upper_l / 2
    x_notch = opening / 2
    y0, y1 = -upper_w / 2, upper_w / 2
    _box(mesh, -x_outer, -x_notch, y0, y1, base_t, top)
    _box(mesh, x_notch, x_outer, y0, y1, base_t, top)

    # The drawing calls out R15 with its arc centre on the Z=totalHeight top
    # plane, hence the lowest point is Z=25 for the 40 mm high acceptance part.
    # The rectangular lead-in is only a one-millimetre over-cut above the top;
    # shoulders between the 40 mm opening and the 30 mm diameter arc remain.
    center_z = top
    straight = max(0.0, x_notch - radius)
    xs: list[float] = [-x_notch]
    if straight > 1e-8:
        xs.append(-radius)
    arc_segments = 20
    if radius > 1e-8:
        xs.extend(
            -radius + (2 * radius) * i / arc_segments
            for i in range(1, arc_segments)
        )
    if straight > 1e-8:
        xs.append(radius)
    xs.append(x_notch)
    xs = sorted(set(round(x, 9) for x in xs))

    def boundary(x: float) -> float:
        if abs(x) <= radius + 1e-8:
            return center_z - math.sqrt(max(0.0, radius * radius - x * x))
        return center_z

    for left, right in zip(xs, xs[1:]):
        _wedge_segment(
            mesh,
            left,
            right,
            y0,
            y1,
            base_t,
            boundary(left),
            boundary(right),
        )

    boss_r = max(0.001, float(p.boss_diameter) / 2)
    boss_h = max(0.001, float(p.resolved_boss_height))
    for x in (-float(p.boss_center_distance) / 2, float(p.boss_center_distance) / 2):
        _cylinder(mesh, x, 0.0, base_t, boss_h, boss_r)
    return mesh


def _mesh_from_cadquery(shape: Any) -> Mesh | None:
    """Tessellate an OCCT shape through CadQuery's public API."""

    try:
        value = shape.val() if hasattr(shape, "val") else shape
        vertices, triangles = value.tessellate(0.05)
        mesh = Mesh()
        for tri in triangles:
            points = []
            for index in tri:
                point = vertices[index]
                if hasattr(point, "toTuple"):
                    point = point.toTuple()
                points.append(tuple(float(v) for v in point))
            if len(points) == 3:
                mesh.add_triangle(points[0], points[1], points[2])
        return mesh if mesh.indices else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CadQuery shape and exporters


def build_cadquery_shape(parameters: BracketParameters) -> Any:
    """Create the bracket as a fused/cut CadQuery solid."""

    cq = get_cadquery()
    if cq is None:
        raise RuntimeError(f"CadQuery unavailable: {_CADQUERY_ERROR or 'unknown error'}")
    p = parameters
    base = cq.Workplane("XY").box(
        p.base_length, p.base_width, p.base_thickness, centered=(True, True, False)
    )
    upper = (
        cq.Workplane("XY")
        .box(p.upper_length, p.upper_width, p.upper_height, centered=(True, True, False))
        .translate((0, 0, p.base_thickness))
    )
    shape = base.union(upper)
    boss_h = p.resolved_boss_height
    for x in (-p.boss_center_distance / 2, p.boss_center_distance / 2):
        boss = (
            cq.Workplane("XY")
            .center(x, 0)
            .circle(p.boss_diameter / 2)
            .extrude(boss_h)
            .translate((0, 0, p.base_thickness))
        )
        shape = shape.union(boss)

    opening = p.notch_opening
    radius = p.notch_radius
    top = p.base_thickness + p.upper_height
    center_z = top
    # Start the rectangular lead-in a tiny distance below the top plane.  A
    # cutter whose lower face is exactly coplanar with the upper body's top
    # face can be discarded by OCCT's boolean regularisation, leaving only
    # the R15 (30 mm) circular span.  The 0.01 mm overlap is below drawing
    # tolerance, but preserves the specified 40 mm top opening in the B-Rep.
    slot_overlap = _SLOT_OVERLAP_MM
    slot_height = 1.0 + slot_overlap
    slot = (
        cq.Workplane("XY")
        .box(opening, p.upper_width + 4.0, slot_height, centered=(True, True, False))
        .translate((0, 0, center_z - slot_overlap))
    )
    depth = p.upper_width + 4.0
    cutter_solid = cq.Solid.makeCylinder(
        radius,
        depth,
        cq.Vector(0, -depth / 2, center_z),
        cq.Vector(0, 1, 0),
    )
    cylinder_cutter = cq.Workplane("XY").newObject([cutter_solid])
    shape = shape.cut(slot.union(cylinder_cutter))
    try:
        return shape.clean()
    except Exception:
        return shape


def _safe_shape_collection(value: Any, method_name: str) -> list[Any] | None:
    """Call a CadQuery collection method without coupling to one CQ release."""

    try:
        method = getattr(value, method_name, None)
        if not callable(method):
            return None
        result = method()
        return list(result)
    except Exception:
        return None


def _safe_member(value: Any, name: str) -> Any | None:
    """Read an OCCT/CadQuery property across wrapper versions.

    CadQuery has exposed geometry through both methods (``Radius()``) and
    properties in different releases.  Keeping that compatibility logic in a
    tiny helper makes the audit code below readable and, importantly, keeps
    failures local to one feature rather than aborting the whole report.
    """

    try:
        member = getattr(value, name, None)
        if callable(member):
            return member()
        return member
    except Exception:
        return None


def _xyz(value: Any) -> tuple[float, float, float] | None:
    """Extract finite XYZ coordinates from an OCCT point/vector wrapper."""

    if value is None:
        return None
    coordinates: list[float] = []
    for name in ("X", "Y", "Z"):
        component = _safe_member(value, name)
        try:
            number = float(component)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        coordinates.append(number)
    return tuple(coordinates)  # type: ignore[return-value]


def _face_bbox(face: Any) -> tuple[float, float, float, float, float, float] | None:
    """Return a face bounding box as ``xmin,xmax,ymin,ymax,zmin,zmax``."""

    box = _safe_member(face, "BoundingBox")
    if box is None:
        return None
    if isinstance(box, (tuple, list)) and len(box) >= 6:
        try:
            values = tuple(float(component) for component in box[:6])
            if all(math.isfinite(component) for component in values):
                return values  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    values: list[float] = []
    for name in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"):
        component = _safe_member(box, name)
        try:
            number = float(component)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        values.append(number)
    return tuple(values)  # type: ignore[return-value]


def _cylinder_surface_data(
    face: Any,
) -> tuple[float, tuple[float, float, float] | None, tuple[float, float, float] | None]:
    """Return ``(radius, axis_origin, axis_direction)`` for a cylindrical face.

    ``Face._geomAdaptor()`` often returns a trimmed surface.  We unwrap its
    ``BasisSurface`` until a cylindrical adaptor (or a native cylinder) is
    found.  All access is defensive because OCCT Python bindings differ a bit
    between CadQuery wheels.
    """

    radius = float("nan")
    cylinder: Any | None = None
    # A few CadQuery versions expose the radius directly on Face rather than
    # through the adaptor.  Probe that public spelling first, then continue
    # with the OCCT surface unwrapping below.
    for name in ("radius", "Radius"):
        candidate = _safe_member(face, name)
        try:
            candidate_radius = float(candidate)
            if math.isfinite(candidate_radius):
                radius = candidate_radius
                break
        except (TypeError, ValueError):
            continue
    try:
        geometry = face._geomAdaptor()
    except Exception:
        # Keep any direct Face.radius() evidence and try Face.Axis() below.
        axis = _safe_member(face, "Axis")
        return radius, _xyz(_safe_member(axis, "Location")), _xyz(
            _safe_member(axis, "Direction")
        )
    for _ in range(6):
        candidate = _safe_member(geometry, "Radius")
        try:
            candidate_radius = float(candidate)
            if math.isfinite(candidate_radius):
                radius = candidate_radius
        except (TypeError, ValueError):
            pass
        if math.isfinite(radius):
            break
        candidate_cylinder = _safe_member(geometry, "Cylinder")
        if candidate_cylinder is not None:
            cylinder = candidate_cylinder
            candidate = _safe_member(candidate_cylinder, "Radius")
            try:
                candidate_radius = float(candidate)
                if math.isfinite(candidate_radius):
                    radius = candidate_radius
            except (TypeError, ValueError):
                pass
            if math.isfinite(radius):
                break
        basis = _safe_member(geometry, "BasisSurface")
        if basis is None or basis is geometry:
            break
        geometry = basis

    axis_source = cylinder if cylinder is not None else geometry
    axis = _safe_member(axis_source, "Axis")
    if axis is None and axis_source is not face:
        axis = _safe_member(face, "Axis")
    if axis is None:
        return radius, None, None
    origin = _xyz(_safe_member(axis, "Location"))
    direction = _xyz(_safe_member(axis, "Direction"))
    return radius, origin, direction


def audit_geometry(
    parameters: BracketParameters,
    shape: Any | None = None,
    *,
    engine: str | None = None,
    try_kernel: bool = True,
) -> dict[str, Any]:
    """Return topology and feature evidence for a generated bracket.

    The result consists of JSON-safe scalar values so it can be copied directly
    into ValidationReport.metrics and persisted with a PDM version. When a
    CadQuery shape is supplied (or can be built), OCCT topology is inspected.
    Otherwise the deterministic analytic recipe is audited and marked
    kernelBacked=false.
    """

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    expected_length = float(p.base_length)
    expected_width = float(p.base_width)
    expected_height = float(p.total_height)
    expected_volume = _estimate_bracket_volume(p)

    kernel_shape = shape
    kernel_error: str | None = None
    if kernel_shape is None and try_kernel and selected_engine == "cadquery-occt":
        try:
            kernel_shape = build_cadquery_shape(p)
        except Exception as exc:  # pragma: no cover - OCCT/platform dependent
            kernel_error = f"{type(exc).__name__}: {exc}"

    if kernel_shape is not None:
        value = kernel_shape.val() if hasattr(kernel_shape, "val") else kernel_shape
        solids = _safe_shape_collection(value, "Solids")
        faces = _safe_shape_collection(value, "Faces")
        edges = _safe_shape_collection(value, "Edges")
        vertices = _safe_shape_collection(value, "Vertices")
        solid_count = len(solids) if solids is not None else None
        face_count = len(faces) if faces is not None else None
        edge_count = len(edges) if edges is not None else None
        vertex_count = len(vertices) if vertices is not None else None

        bbox = None
        try:
            bbox = value.BoundingBox()
        except Exception:
            try:
                bbox = kernel_shape.BoundingBox()
            except Exception:
                bbox = None
        def _bbox_dimension(name: str) -> float:
            if bbox is None:
                return float("nan")
            candidate = _safe_member(bbox, name)
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                return float("nan")
            return value if math.isfinite(value) else float("nan")

        bbox_length = _bbox_dimension("xlen")
        bbox_width = _bbox_dimension("ylen")
        bbox_height = _bbox_dimension("zlen")
        bbox_matches = all(
            math.isfinite(actual)
            and math.isclose(actual, expected, abs_tol=0.05)
            for actual, expected in (
                (bbox_length, expected_length),
                (bbox_width, expected_width),
                (bbox_height, expected_height),
            )
        )
        try:
            volume = float(value.Volume())
        except Exception:
            try:
                volume = float(kernel_shape.Volume())
            except Exception:
                volume = float("nan")
        try:
            validity_method = getattr(value, "isValid", None)
            kernel_valid = bool(validity_method()) if callable(validity_method) else False
        except Exception:
            kernel_valid = False

        notch_faces = 0
        boss_faces = 0
        cylindrical_faces = 0
        notch_axis_records: list[tuple[float, float, float]] = []
        boss_origin_records: list[tuple[float, float, float]] = []
        boss_direction_records: list[tuple[float, float, float]] = []
        notch_bottom_candidates: list[float] = []
        notch_center_candidates: list[float] = []
        boss_height_candidates: list[float] = []
        if faces is not None:
            for face in faces:
                try:
                    geom_type = str(face.geomType()).upper()
                except Exception:
                    geom_type = ""
                if "CYL" not in geom_type:
                    continue
                cylindrical_faces += 1
                # Face.radius() is not consistently exposed, so use the
                # adaptor helper for radius and axis evidence.
                face_radius, axis_origin, axis_direction = _cylinder_surface_data(face)
                face_box = _face_bbox(face)
                axis_y = bool(
                    axis_direction is not None
                    and abs(axis_direction[1]) >= 0.9
                    and abs(axis_direction[1])
                    >= max(abs(axis_direction[0]), abs(axis_direction[2]))
                )
                axis_z = bool(
                    axis_direction is not None
                    and abs(axis_direction[2]) >= 0.9
                    and abs(axis_direction[2])
                    >= max(abs(axis_direction[0]), abs(axis_direction[1]))
                )
                notch_radius_match = math.isfinite(face_radius) and math.isclose(
                    face_radius, float(p.notch_radius), abs_tol=0.05
                )
                boss_radius_match = math.isfinite(face_radius) and math.isclose(
                    face_radius, float(p.boss_diameter) / 2, abs_tol=0.05
                )

                # When an axis is available, use it to disambiguate a notch
                # cylinder (axis Y) from vertical boss cylinders (axis Z).
                # Radius-only matches are still surfaced in the counts for
                # diagnostics, but cannot pass the production audit without
                # the corresponding axis evidence.
                notch_match = notch_radius_match and (axis_direction is None or axis_y)
                boss_match = boss_radius_match and (axis_direction is None or axis_z)
                if notch_match:
                    notch_faces += 1
                    if axis_direction is not None:
                        notch_axis_records.append(axis_direction)
                    if axis_origin is not None:
                        notch_center_candidates.append(axis_origin[2])
                    if face_box is not None:
                        notch_bottom_candidates.append(face_box[4])
                        # A trimmed lower semicylinder can have zmax just
                        # below the mathematical centre because the 40 mm
                        # lead-in overlaps it by a numerical tolerance.
                        if axis_origin is None:
                            notch_center_candidates.append(face_box[5])
                if boss_match:
                    boss_faces += 1
                    if axis_direction is not None:
                        boss_direction_records.append(axis_direction)
                    if axis_origin is not None:
                        boss_origin_records.append(axis_origin)
                    if face_box is not None:
                        boss_height_candidates.append(max(0.0, face_box[5] - face_box[4]))

        notch_present = notch_faces >= 1
        boss_pair_present = boss_faces >= 2
        # A kernel shape should provide axis evidence.  Radius-only counting
        # remains accepted when the binding omits Axis(), but the additional
        # metrics make that limitation explicit to callers.
        notch_axis_aligned = bool(notch_axis_records) and all(
            abs(direction[1]) >= 0.9
            and abs(direction[1]) >= max(abs(direction[0]), abs(direction[2]))
            for direction in notch_axis_records
        )
        boss_axes_aligned = bool(boss_direction_records) and all(
            abs(direction[2]) >= 0.9
            and abs(direction[2]) >= max(abs(direction[0]), abs(direction[1]))
            for direction in boss_direction_records
        )
        if boss_origin_records:
            expected_distance = abs(float(p.boss_center_distance))
            boss_center_distance_measured = 0.0
            boss_centers_match = False
            # Look for any pair at the requested ±distance/2 positions.  The
            # combination search tolerates extra same-radius cylindrical
            # faces introduced by a future fillet or split operation.
            for first_index, first in enumerate(boss_origin_records):
                for second in boss_origin_records[first_index + 1 :]:
                    distance = abs(first[0] - second[0])
                    if distance > boss_center_distance_measured:
                        boss_center_distance_measured = distance
                    if (
                        math.isclose(distance, expected_distance, abs_tol=0.05)
                        and math.isclose((first[0] + second[0]) / 2, 0.0, abs_tol=0.05)
                        and math.isclose(first[1], 0.0, abs_tol=0.05)
                        and math.isclose(second[1], 0.0, abs_tol=0.05)
                        and math.isclose(first[2], float(p.base_thickness), abs_tol=0.05)
                        and math.isclose(second[2], float(p.base_thickness), abs_tol=0.05)
                    ):
                        boss_centers_match = True
            boss_center_distance_value: float | None = boss_center_distance_measured
        else:
            # No axis API: retain a deterministic parameter value for display,
            # but mark the measured-match flag false so production gating can
            # distinguish inferred evidence from OCCT evidence.
            boss_center_distance_value = None
            boss_centers_match = False

        if boss_height_candidates:
            boss_height_value = max(boss_height_candidates)
            boss_height_matches = math.isclose(
                boss_height_value, float(p.resolved_boss_height), abs_tol=0.05
            )
        else:
            boss_height_value = None
            boss_height_matches = False

        if notch_center_candidates:
            notch_center_value = sum(notch_center_candidates) / len(notch_center_candidates)
        else:
            notch_center_value = float(p.total_height)
        if notch_bottom_candidates:
            notch_bottom_value = min(notch_bottom_candidates)
        else:
            notch_bottom_value = notch_center_value - float(p.notch_radius)
        notch_center_matches = bool(notch_center_candidates) and math.isclose(
            notch_center_value, float(p.total_height), abs_tol=0.05
        )
        notch_bottom_matches = bool(notch_bottom_candidates) and math.isclose(
            notch_bottom_value,
            float(p.total_height) - float(p.notch_radius),
            abs_tol=0.05,
        )

        # Radius/axis/position evidence is required when the corresponding
        # feature is present.  This prevents a shifted or disconnected solid
        # from being accepted merely because it happens to contain two
        # cylinders with the right radii.  A production OCCT binding is
        # expected to expose all four topology collections and both cylinder
        # axis directions; unknown values are therefore a failed audit rather
        # than an implicit pass.
        topology_collections_complete = all(
            collection is not None
            for collection in (solids, faces, edges, vertices)
        )
        axis_evidence_complete = bool(
            notch_axis_records and boss_direction_records and boss_origin_records
        )
        feature_geometry_passed = bool(
            notch_present
            and boss_pair_present
            and notch_center_matches
            and notch_bottom_matches
            and boss_centers_match
            and boss_height_matches
            and notch_axis_aligned
            and boss_axes_aligned
            and axis_evidence_complete
        )
        topology_passed = bool(
            kernel_valid
            and topology_collections_complete
            and solid_count == 1
            and face_count is not None
            and edge_count is not None
            and vertex_count is not None
            and bbox_matches
            and math.isfinite(volume)
            and volume > 0
            and feature_geometry_passed
        )
        if kernel_error:
            topology_passed = False
        volume_matches_estimate = bool(
            expected_volume > 0
            and math.isfinite(volume)
            and abs(volume - expected_volume)
            <= max(1.0, expected_volume * 0.02)
        )
        volume_delta = volume - expected_volume if math.isfinite(volume) else 0.0
        volume_relative_error = (
            abs(volume_delta) / expected_volume if expected_volume > 0 else 0.0
        )
        if not volume_matches_estimate:
            topology_passed = False
        return {
            "topologyAuditEngine": "cadquery-occt",
            "kernelBacked": True,
            "topologyAuditPassed": topology_passed,
            "kernelShapeValid": kernel_valid,
            "topologyCollectionsComplete": topology_collections_complete,
            "axisEvidenceComplete": axis_evidence_complete,
            "solidCount": solid_count if solid_count is not None else 0,
            "faceCount": face_count if face_count is not None else 0,
            "edgeCount": edge_count if edge_count is not None else 0,
            "vertexCount": vertex_count if vertex_count is not None else 0,
            "bboxLength": round(bbox_length, 6) if math.isfinite(bbox_length) else 0.0,
            "bboxWidth": round(bbox_width, 6) if math.isfinite(bbox_width) else 0.0,
            "bboxHeight": round(bbox_height, 6) if math.isfinite(bbox_height) else 0.0,
            "bboxMatchesParameters": bbox_matches,
            "volumeMm3": round(volume, 6) if math.isfinite(volume) else 0.0,
            "estimatedVolumeMm3": round(expected_volume, 6),
            "volumeDeltaMm3": round(volume_delta, 6),
            "volumeRelativeError": round(volume_relative_error, 8),
            "volumeMatchesEstimate": volume_matches_estimate,
            "notchArcFaceCount": notch_faces,
            "bossCylindricalFaceCount": boss_faces,
            "cylindricalFaceCount": cylindrical_faces,
            "notchArcPresent": notch_present,
            "bossPairPresent": boss_pair_present,
            "notchArcCenterZ": round(notch_center_value, 6),
            "notchBottomZ": round(notch_bottom_value, 6),
            "notchArcCenterMatchesParameters": notch_center_matches,
            "notchBottomMatchesParameters": notch_bottom_matches,
            "notchAxisAligned": notch_axis_aligned,
            "bossAxesAligned": boss_axes_aligned,
            "bossCenterDistanceMeasured": (
                round(boss_center_distance_value, 6)
                if boss_center_distance_value is not None
                else 0.0
            ),
            "bossCenterDistanceMatchesParameters": boss_centers_match,
            "bossHeightMeasured": (
                round(boss_height_value, 6) if boss_height_value is not None else 0.0
            ),
            "bossHeightMatchesParameters": boss_height_matches,
            "kernelAuditError": kernel_error or "",
        }

    mesh = build_fallback_mesh(p)
    vertex_count = len(mesh.positions) // 3
    face_count = len(mesh.indices) // 3
    return {
        "topologyAuditEngine": "analytic-fallback",
        "kernelBacked": False,
        "topologyAuditPassed": bool(face_count > 0 and expected_volume > 0),
        "kernelShapeValid": False,
        "solidCount": 1 if face_count > 0 else 0,
        "faceCount": face_count,
        "edgeCount": round(face_count * 3 / 2),
        "vertexCount": vertex_count,
        "bboxLength": expected_length,
        "bboxWidth": expected_width,
        "bboxHeight": expected_height,
        "bboxMatchesParameters": True,
        "volumeMm3": round(expected_volume, 6),
        "estimatedVolumeMm3": round(expected_volume, 6),
        "volumeDeltaMm3": 0.0,
        "volumeRelativeError": 0.0,
        "volumeMatchesEstimate": True,
        "notchArcFaceCount": 1,
        "bossCylindricalFaceCount": 2,
        "cylindricalFaceCount": 2,
        "notchArcPresent": True,
        "bossPairPresent": True,
        "notchArcCenterZ": float(p.total_height),
        "notchBottomZ": round(float(p.total_height) - float(p.notch_radius), 6),
        "notchArcCenterMatchesParameters": True,
        "notchBottomMatchesParameters": True,
        "notchAxisAligned": True,
        "bossAxesAligned": True,
        "bossCenterDistanceMeasured": float(p.boss_center_distance),
        "bossCenterDistanceMatchesParameters": True,
        "bossHeightMeasured": float(p.resolved_boss_height),
        "bossHeightMatchesParameters": True,
        "kernelAuditError": kernel_error or "",
    }


def _step_fallback(mesh: Mesh, parameters: BracketParameters) -> bytes:
    """Write an AP242 tessellated preview STEP document."""

    points = [
        tuple(mesh.positions[i : i + 3])
        for i in range(0, len(mesh.positions), 3)
    ]
    triangles = [
        tuple(mesh.indices[i : i + 3])
        for i in range(0, len(mesh.indices), 3)
    ]
    point_text = ",".join(
        f"({x:.6f},{y:.6f},{z:.6f})" for x, y, z in points
    )
    tri_text = ",".join(
        f"({a + 1},{b + 1},{c + 1})" for a, b, c in triangles
    )
    name = "JOYNIU_BRACKET_FACETED_PREVIEW"
    lines = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('JoyNiu NewCAD faceted fallback preview'),'2;1');",
        "FILE_NAME('joyniu_bracket.step','2026-08-29T00:00:00',('JoyNiu'),('JoyNiu'),'JoyNiu NewCAD','JoyNiu API','');",
        "FILE_SCHEMA(('AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF { 1 0 10303 442 1 1 4 }'));",
        "ENDSEC;",
        "DATA;",
        "/* CadQuery/OCCT unavailable: this tessellated artifact is for audit and review. */",
        "#1=APPLICATION_CONTEXT('managed model based 3d engineering');",
        "#2=APPLICATION_PROTOCOL_DEFINITION('international standard','ap242_managed_model_based_3d_engineering_mim_lf',2020,#1);",
        "#3=PRODUCT_CONTEXT('',#1,'mechanical');",
        f"#4=PRODUCT('{name}','{name}','',(#3));",
        "#5=PRODUCT_DEFINITION_FORMATION_WITH_SPECIFIED_SOURCE('design','',#4,.MADE.);",
        "#6=PRODUCT_DEFINITION('design','',#5,#7);",
        "#7=PRODUCT_DEFINITION_CONTEXT('part definition',#1,'design');",
        "#8=CARTESIAN_POINT_LIST_3D('',(" + point_text + "));",
        "#9=TRIANGULATED_FACE_SET('',#8,$,.T.,(" + tri_text + "));",
        "#10=(GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#11)) GLOBAL_UNIT_ASSIGNED_CONTEXT((#12,#13,#14)) REPRESENTATION_CONTEXT('','3D'));",
        "#11=UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(0.01),#12,'distance_accuracy_value','');",
        "#12=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));",
        "#13=(NAMED_UNIT(*) SI_UNIT($,.STERADIAN.));",
        "#14=(NAMED_UNIT(*) SI_UNIT($,.RADIAN.));",
        "#15=SHAPE_REPRESENTATION('Bracket faceted preview',(#9),#10);",
        "#16=SHAPE_DEFINITION_REPRESENTATION(#6,#15);",
        "ENDSEC;",
        "END-ISO-10303-21;",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def mesh_to_glb(mesh: Mesh, *, name: str = "JoyNiu Bracket") -> bytes:
    """Encode a flat-shaded mesh as a standards-compliant GLB 2.0."""

    if not mesh.indices:
        raise ValueError("cannot encode an empty mesh")
    pos = struct.pack(f"<{len(mesh.positions)}f", *mesh.positions)
    normals = struct.pack(f"<{len(mesh.normals)}f", *mesh.normals)
    indices = struct.pack(f"<{len(mesh.indices)}I", *mesh.indices)
    position_offset = 0
    normal_offset = (len(pos) + 3) & ~3
    index_offset = (normal_offset + len(normals) + 3) & ~3
    binary = bytearray(index_offset + len(indices))
    binary[position_offset : position_offset + len(pos)] = pos
    binary[normal_offset : normal_offset + len(normals)] = normals
    binary[index_offset : index_offset + len(indices)] = indices

    position_values = [
        mesh.positions[i : i + 3]
        for i in range(0, len(mesh.positions), 3)
    ]
    mins = [min(v[i] for v in position_values) for i in range(3)]
    maxs = [max(v[i] for v in position_values) for i in range(3)]
    gltf = {
        "asset": {"version": "2.0", "generator": "JoyNiu NewCAD API"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": name, "mesh": 0}],
        "meshes": [{
            "name": name,
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1},
                "indices": 2,
                "mode": 4,
                "material": 0,
            }],
        }],
        "materials": [{
            "name": "45# steel preview",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.38, 0.48, 0.62, 1.0],
                "metallicFactor": 0.72,
                "roughnessFactor": 0.3,
            },
            "doubleSided": True,
        }],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": position_offset, "byteLength": len(pos), "target": 34962},
            {"buffer": 0, "byteOffset": normal_offset, "byteLength": len(normals), "target": 34962},
            {"buffer": 0, "byteOffset": index_offset, "byteLength": len(indices), "target": 34963},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(mesh.positions) // 3,
                "type": "VEC3",
                "min": mins,
                "max": maxs,
            },
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": len(mesh.normals) // 3,
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5125,
                "count": len(mesh.indices),
                "type": "SCALAR",
                "min": [0],
                "max": [len(mesh.positions) // 3 - 1],
            },
        ],
    }
    json_chunk = json.dumps(
        gltf, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    bin_chunk = bytes(binary) + b"\x00" * ((4 - len(binary) % 4) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    return b"".join(
        (
            struct.pack("<4sII", b"glTF", 2, total),
            struct.pack("<I4s", len(json_chunk), b"JSON"),
            json_chunk,
            struct.pack("<I4s", len(bin_chunk), b"BIN\x00"),
            bin_chunk,
        )
    )


@dataclass
class GeneratedArtifact:
    format: str
    data: bytes
    engine: str
    production_ready: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def generate_artifacts(
    parameters: BracketParameters,
    formats: Iterable[str] = ("step", "glb"),
    *,
    require_cadquery: bool = False,
) -> list[GeneratedArtifact]:
    """Generate requested artifacts, selecting OCCT or auditable fallback."""

    requested = list(dict.fromkeys(str(f).lower() for f in formats))
    unsupported = [f for f in requested if f not in {"step", "glb"}]
    if unsupported:
        raise ValueError(f"unsupported output format(s): {', '.join(unsupported)}")

    cq_shape = None
    cq_error: str | None = None
    if get_cadquery() is not None:
        try:
            cq_shape = build_cadquery_shape(parameters)
        except Exception as exc:  # pragma: no cover - host-specific OCCT errors
            cq_error = f"{type(exc).__name__}: {exc}"
            if require_cadquery:
                raise RuntimeError(
                    f"CadQuery shape build failed: {cq_error}"
                ) from exc
    elif require_cadquery:
        raise RuntimeError(
            f"CadQuery is required but unavailable: {_CADQUERY_ERROR}"
        )

    # ``get_cadquery`` can report an importable module while a platform-specific
    # OCCT construction fails.  Do not silently downgrade a strict request to
    # a faceted mesh in that case.
    if require_cadquery and cq_shape is None:
        raise RuntimeError(
            f"CadQuery shape is required but unavailable: {cq_error or 'unknown error'}"
        )

    # Keep the first tessellation result: calling OCCT's tessellator a second
    # time merely to detect success is surprisingly expensive for larger
    # drawings and can produce subtly different triangulations.
    mesh = _mesh_from_cadquery(cq_shape) if cq_shape is not None else None
    cq_mesh = mesh is not None and cq_shape is not None
    if require_cadquery and mesh is None and "glb" in requested:
        raise RuntimeError(
            "CadQuery tessellation failed; strict geometry output cannot use a mesh fallback"
        )
    if mesh is None:
        mesh = build_fallback_mesh(parameters)

    artifacts: list[GeneratedArtifact] = []
    for fmt in requested:
        if fmt == "glb":
            if cq_mesh:
                artifacts.append(
                    GeneratedArtifact(
                        format="glb",
                        data=mesh_to_glb(mesh),
                        engine="cadquery-tessellation",
                        production_ready=False,
                        warnings=[
                            "GLB is a tessellated visualization export; retain STEP for B-Rep editing."
                        ],
                    )
                )
            else:
                warnings = [
                    "CadQuery/OCCT unavailable; GLB generated by deterministic mesh fallback."
                ]
                if cq_error:
                    warnings.append(f"CadQuery build failed: {cq_error}")
                artifacts.append(
                    GeneratedArtifact(
                        format="glb",
                        data=mesh_to_glb(mesh),
                        engine="mesh-fallback",
                        production_ready=False,
                        warnings=warnings,
                    )
                )
        elif fmt == "step":
            if cq_shape is not None:
                try:
                    from cadquery import exporters  # type: ignore

                    with tempfile.NamedTemporaryFile(
                        suffix=".step", delete=False
                    ) as handle:
                        path = Path(handle.name)
                    try:
                        exporters.export(cq_shape, str(path), exportType="STEP")
                        data = path.read_bytes()
                    finally:
                        path.unlink(missing_ok=True)
                    artifacts.append(
                        GeneratedArtifact(
                            format="step",
                            data=data,
                            engine="cadquery-occt",
                            production_ready=True,
                        )
                    )
                    continue
                except Exception as exc:  # pragma: no cover
                    cq_error = f"STEP exporter failed: {type(exc).__name__}: {exc}"
                    if require_cadquery:
                        raise RuntimeError(
                            f"CadQuery STEP export failed: {cq_error}"
                        ) from exc
            warnings = [
                "CadQuery/OCCT unavailable; STEP is an AP242 tessellated preview and is not production B-Rep."
            ]
            if cq_error:
                warnings.append(cq_error)
            artifacts.append(
                GeneratedArtifact(
                    format="step",
                    data=_step_fallback(mesh, parameters),
                    engine="faceted-step-fallback",
                    production_ready=False,
                    warnings=warnings,
                )
            )
    return artifacts


def artifact_data_url(data: bytes, media_type: str) -> str:
    """Return a data URL for small local integrations (not used by downloads)."""

    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


MEDIA_TYPES = {
    "step": "application/step",
    "glb": "model/gltf-binary",
}
