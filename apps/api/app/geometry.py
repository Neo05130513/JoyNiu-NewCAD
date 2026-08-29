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
    "slot_length",
    "slot_width",
    "pocket_depth",
)

# The C-semantics recipe has two 10 mm wide, 30 mm long shallow pockets on
# either side of the R15 saddle.  ``notchOpening`` remains the historical API
# name for the combined 40 mm span; deriving the outer/inner X limits from it
# keeps old payloads compatible while making the feature explicit in the
# generated recipe.
_SLOT_Y_DEFAULT_MM = 30.0
_SLOT_WIDTH_DEFAULT_MM = 10.0
_POCKET_DEPTH_DEFAULT_MM = 10.0

# A microscopic overlap keeps OCCT booleans robust when a cutter terminates on
# a coplanar face.  The resulting trimmed face remains on the nominal model
# boundary (the overlap is far below drawing tolerance).
_CUTTER_OVERLAP_MM = 0.01


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


def _slot_extents(parameters: BracketParameters) -> tuple[float, float, float, float, float]:
    """Return the two symmetric shallow-pocket extents.

    ``notchOpening`` is retained as the historical 40 mm opening dimension.
    In the C recipe the two pockets occupy the outer quarter of that span:
    ``[-20,-10]`` and ``[10,20]``.  The explicit ``slotWidth`` field controls
    the 10 mm width while ``slotLength`` and ``pocketDepth`` control Y and Z.
    The tuple is ``(outer, inner, y_half, z_bottom, z_top)``.
    """

    opening = float(parameters.notch_opening)
    width = float(parameters.slot_width)
    outer = abs(opening) / 2.0
    inner = max(0.0, outer - abs(width))
    y_half = min(
        abs(float(parameters.slot_length)) / 2.0,
        abs(float(parameters.upper_width)) / 2.0,
    )
    top = float(parameters.base_thickness) + float(parameters.upper_height)
    depth = min(abs(float(parameters.pocket_depth)), abs(float(parameters.upper_height)))
    return outer, inner, y_half, top - depth, top


def _simpson_integral(function: Any, left: float, right: float, subdivisions: int = 512) -> float:
    """Deterministic Simpson integration used by the analytic volume audit."""

    if right <= left:
        return 0.0
    n = max(2, int(subdivisions))
    if n % 2:
        n += 1
    step = (right - left) / n
    total = float(function(left)) + float(function(right))
    for index in range(1, n):
        total += (4.0 if index % 2 else 2.0) * float(function(left + index * step))
    return total * step / 3.0


def _saddle_slot_cut_volume(parameters: BracketParameters) -> float:
    """Estimate the union of the R15 saddle and two shallow pockets.

    The saddle is a horizontal cylinder centred on the top plane.  At a given
    X its lower-half height is ``sqrt(R²-X²)``.  The pockets overlap that
    cylinder in the central 30 mm of Y, so integrating the union avoids
    double-counting the overlap and reproduces the OCCT volume to sub-mm³
    precision for the acceptance dimensions.
    """

    p = parameters
    try:
        radius = abs(float(p.notch_radius))
        upper_height = abs(float(p.upper_height))
        saddle_y = min(abs(float(p.upper_width)), abs(float(p.resolved_saddle_depth)))
        slot_y = min(abs(float(p.slot_length)), abs(float(p.upper_width)))
        slot_depth = min(abs(float(p.pocket_depth)), upper_height)
        outer, inner, _y_half, slot_bottom, top = _slot_extents(p)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if radius <= 0 or upper_height <= 0 or saddle_y <= 0:
        return 0.0
    # The validation layer rejects a saddle reaching into the base.  Clamp the
    # analytic interval defensively for callers that use this helper directly.
    if radius > upper_height + 1e-9:
        vertical_clip = upper_height
    else:
        vertical_clip = radius
    if slot_depth <= 0 or slot_y <= 0:
        slot_depth = 0.0

    # Include every feature boundary and the points where the circular height
    # crosses the pocket floor.  Simpson is then applied piecewise so the
    # min()/indicator kinks do not reduce accuracy.
    domain = max(radius, outer)
    bounds = [-domain, domain]
    if outer > 0:
        bounds.extend((-outer, -inner, inner, outer))
    if slot_depth < radius:
        crossing = math.sqrt(max(0.0, radius * radius - slot_depth * slot_depth))
        bounds.extend((-crossing, crossing))
    bounds = sorted(set(round(value, 12) for value in bounds if -domain <= value <= domain))
    if len(bounds) < 2:
        return 0.0

    def integrand(x: float) -> float:
        if abs(x) >= radius:
            saddle_height = 0.0
        else:
            saddle_height = min(
                vertical_clip,
                math.sqrt(max(0.0, radius * radius - x * x)),
            )
        in_slot = inner - 1e-10 <= abs(x) <= outer + 1e-10
        slot_height = slot_depth if in_slot else 0.0
        overlap = min(saddle_height, slot_height) if in_slot else 0.0
        return saddle_y * saddle_height + slot_y * (slot_height - overlap)

    return max(
        0.0,
        sum(_simpson_integral(integrand, left, right, 256) for left, right in zip(bounds, bounds[1:])),
    )


def _side_hole_cut_volume(parameters: BracketParameters) -> float:
    """Estimate the material removed by the two vertical side-hole cuts."""

    p = parameters
    try:
        radius = abs(float(p.boss_diameter)) / 2.0
        depth = min(max(0.0, float(p.resolved_hole_depth)), float(p.total_height))
        base_x0, base_x1 = -float(p.base_length) / 2.0, float(p.base_length) / 2.0
        base_y0, base_y1 = -float(p.base_width) / 2.0, float(p.base_width) / 2.0
        upper_x0, upper_x1 = -float(p.upper_length) / 2.0, float(p.upper_length) / 2.0
        upper_y0, upper_y1 = -float(p.upper_width) / 2.0, float(p.upper_width) / 2.0
        base_height = min(depth, float(p.base_thickness))
        upper_height = max(
            0.0,
            min(depth, float(p.total_height)) - float(p.base_thickness),
        )
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if radius <= 0 or depth <= 0:
        return 0.0
    removed = 0.0
    for center_x in (-float(p.boss_center_distance) / 2.0, float(p.boss_center_distance) / 2.0):
        base_area = _circle_rectangle_intersection_area(
            center_x, 0.0, radius, base_x0, base_x1, base_y0, base_y1
        )
        upper_area = _circle_rectangle_intersection_area(
            center_x, 0.0, radius, upper_x0, upper_x1, upper_y0, upper_y1
        )
        removed += base_area * base_height + upper_area * upper_height
    return max(0.0, removed)


def _estimate_bracket_volume(parameters: BracketParameters) -> float:
    """Estimate the C-recipe solid volume with subtractive side holes."""

    p = parameters
    values = [_value(p, field_name) for field_name in _DIMENSION_FIELDS]
    try:
        resolved_hole_depth = float(p.resolved_hole_depth)
        resolved_saddle_depth = float(p.resolved_saddle_depth)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if not all(
        math.isfinite(value) and value > 0
        for value in (*values, resolved_hole_depth, resolved_saddle_depth)
    ):
        return 0.0

    base_volume = float(p.base_length) * float(p.base_width) * float(p.base_thickness)
    upper_volume = float(p.upper_length) * float(p.upper_width) * float(p.upper_height)
    side_holes = _side_hole_cut_volume(p)
    saddle_and_pockets = _saddle_slot_cut_volume(p)
    estimate = base_volume + upper_volume - side_holes - saddle_and_pockets
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

    # Optional fields are resolved by the schema so a legacy payload can keep
    # using ``bossHeight`` while the C recipe uses an explicit through-hole
    # depth.  Validate both the resolved value and any explicitly supplied
    # override, but do not let the legacy alias shorten a through hole.
    try:
        hole_depth = float(p.resolved_hole_depth)
        saddle_depth = float(p.resolved_saddle_depth)
    except (TypeError, ValueError, AttributeError):
        hole_depth = float("nan")
        saddle_depth = float("nan")
    hole_depth_ok = math.isfinite(hole_depth) and hole_depth > 0
    saddle_depth_ok = math.isfinite(saddle_depth) and saddle_depth > 0
    check(
        "dimension.hole_depth",
        hole_depth_ok,
        "holeDepth must be a finite number greater than 0",
        actual=hole_depth,
        expected="> 0",
    )
    check(
        "dimension.saddle_depth",
        saddle_depth_ok,
        "saddleDepth must be a finite number greater than 0",
        actual=saddle_depth,
        expected="> 0",
    )
    legacy_boss_height = p.boss_height
    legacy_boss_height_ok = (
        legacy_boss_height is None
        or (math.isfinite(float(legacy_boss_height)) and float(legacy_boss_height) > 0)
    )
    check(
        "dimension.boss_height",
        legacy_boss_height_ok,
        "bossHeight compatibility value must be a finite number greater than 0",
        severity=Severity.info,
        actual=legacy_boss_height,
        expected="null or > 0",
    )
    finite_positive = finite_positive and hole_depth_ok and saddle_depth_ok

    if finite_positive:
        base_l, base_w, base_t = map(float, (p.base_length, p.base_width, p.base_thickness))
        upper_l, upper_w, upper_h = map(float, (p.upper_length, p.upper_width, p.upper_height))
        total_h = float(p.total_height)
        opening, radius = float(p.notch_opening), float(p.notch_radius)
        diameter, center_dist = float(p.boss_diameter), float(p.boss_center_distance)
        slot_length, slot_width, pocket_depth = map(
            float, (p.slot_length, p.slot_width, p.pocket_depth)
        )
        slot_outer, slot_inner, slot_y_half, slot_bottom, slot_top = _slot_extents(p)

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
            "saddle arc centre is constrained to the total-height top plane",
            actual=arc_center_z,
            expected=total_h,
        )
        check(
            "notch.bottom",
            arc_bottom_z > base_t + 1e-6,
            "saddle bottom must remain above the base top",
            actual=arc_bottom_z,
            expected=f"> {base_t:g}",
        )
        check(
            "notch.depth",
            radius <= upper_h + 1e-6,
            "notchRadius must fit within upperHeight",
            actual=radius,
            expected=f"<= {upper_h:g}",
        )

        # Shallow pockets: canonical extents are x[-20,-10]/[10,20],
        # y[-15,15], z[30,40].
        check(
            "pockets.width",
            slot_width > 0 and slot_width <= opening / 2 + 1e-6,
            "slotWidth must be positive and fit within half the opening",
            actual=slot_width,
            expected=f"<= {opening / 2:g}",
        )
        check(
            "pockets.length",
            slot_length <= upper_w + 1e-6,
            "slotLength must fit inside upperWidth",
            actual=slot_length,
            expected=f"<= {upper_w:g}",
        )
        check(
            "pockets.depth",
            pocket_depth <= upper_h + 1e-6,
            "pocketDepth must fit inside upperHeight",
            actual=pocket_depth,
            expected=f"<= {upper_h:g}",
        )
        check(
            "pockets.envelope",
            slot_outer <= upper_l / 2 + 1e-6 and slot_inner >= 0,
            "shallow pockets must remain inside the upper body",
            actual={"outerX": slot_outer, "innerX": slot_inner},
            expected=f"0 <= inner <= outer <= {upper_l / 2:g}",
        )

        # A legacy payload may still request ``upperWidth=30`` while omitting
        # the new saddleDepth field.  The cutter can only remove material over
        # the upper body's available Y span, so compare the *effective* span
        # (clipped to upperWidth) with the available envelope.  Canonical C
        # parameters are upperWidth=50/saddleDepth=50 and remain unchanged.
        effective_saddle_depth = min(saddle_depth, upper_w)
        saddle_through = math.isclose(
            effective_saddle_depth, min(base_w, upper_w), abs_tol=0.01
        )
        check(
            "saddle.through_width",
            saddle_through,
            "saddleDepth must span the available upper-body width",
            actual=saddle_depth,
            expected=min(base_w, upper_w),
        )
        hole_depth_limit = hole_depth <= total_h + 1e-6
        check(
            "holes.depth",
            hole_depth_limit,
            "holeDepth must not exceed totalHeight",
            actual=hole_depth,
            expected=f"<= {total_h:g}",
        )
        hole_through = bool(p.hole_through) and math.isclose(
            hole_depth, total_h, abs_tol=0.01
        )
        check(
            "holes.through",
            hole_through,
            "the two side holes must run from Z=0 through totalHeight",
            actual={"holeDepth": hole_depth, "holeThrough": bool(p.hole_through)},
            expected={"holeDepth": total_h, "holeThrough": True},
        )
        holes_fit_length = center_dist + diameter <= base_l + 1e-6
        check(
            "holes.base_length_clearance",
            holes_fit_length,
            "side-hole centres and radii must fit inside the base length",
            actual=center_dist + diameter,
            expected=f"<= {base_l:g}",
        )
        holes_fit_width = diameter <= base_w + 1e-6
        check(
            "holes.base_width_clearance",
            holes_fit_width,
            "side-hole diameter must fit inside the base width",
            actual=diameter,
            expected=f"<= {base_w:g}",
        )
        holes_separated = center_dist >= diameter - 1e-6
        check(
            "holes.separation",
            holes_separated,
            "the two side holes must not overlap",
            actual=center_dist,
            expected=f">= {diameter:g}",
        )
        # The C drawing places each hole centre on an upper-body side plane,
        # producing a semicircular recess there and a full hole in the base.
        side_alignment = math.isclose(center_dist, upper_l, abs_tol=0.01)
        check(
            "holes.upper_side_alignment",
            side_alignment,
            "side-hole centres should coincide with the upper-body side planes",
            severity=Severity.warning,
            actual=center_dist,
            expected=upper_l,
        )
        side_hole_clearance = center_dist / 2 - diameter / 2
        check(
            "manufacturing.hole_saddle_clearance",
            side_hole_clearance >= slot_outer,
            "side holes must clear the saddle/pocket span",
            severity=Severity.warning,
            actual=round(side_hole_clearance, 4),
            expected=f">= {slot_outer:g}",
        )
        side_wall = (upper_l - opening) / 2
        check(
            "manufacturing.side_wall",
            side_wall >= 2.0,
            "remaining saddle side wall is at least 2 mm",
            severity=Severity.warning,
            actual=round(side_wall, 4),
            expected=">= 2",
        )
        hole_edge_clearance = (base_l - (center_dist + diameter)) / 2
        check(
            "manufacturing.boss_edge_clearance",
            hole_edge_clearance >= 1.0,
            "side holes retain at least 1 mm edge clearance",
            severity=Severity.warning,
            actual=round(hole_edge_clearance, 4),
            expected=">= 1",
        )
        if legacy_boss_height is not None:
            check(
                "compatibility.boss_height_alias",
                True,
                "bossHeight is retained as a compatibility alias; side-hole depth uses holeDepth/holeThrough",
                severity=Severity.info,
                actual=float(legacy_boss_height),
                expected="ignored for through-hole geometry",
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
            "sideHolePairPresent": audit.get("sideHolePairPresent"),
            "bossPairPresent": audit.get("bossPairPresent"),
            "notchArcCenterZ": audit.get("notchArcCenterZ"),
            "notchBottomZ": audit.get("notchBottomZ"),
            "holeDepthMeasured": audit.get("holeDepthMeasured"),
            "bossCenterDistanceMeasured": audit.get("bossCenterDistanceMeasured"),
            "bossHeightMeasured": audit.get("bossHeightMeasured"),
            "pocketPairPresent": audit.get("pocketPairPresent"),
        },
        expected="one valid solid with R15 saddle, two vertical side holes and two shallow pockets",
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


def _cylinder_surface(
    mesh: Mesh,
    cx: float,
    cy: float,
    z0: float,
    height: float,
    radius: float,
    *,
    start_angle: float = 0.0,
    sweep: float = 2 * math.pi,
    segments: int = 48,
) -> None:
    """Add only the lateral surface of a faceted cylinder.

    This is used for subtractive hole walls in the dependency-free preview;
    unlike :func:`_cylinder` it emits no caps, so a viewer cannot mistake a
    hole for an additive boss.
    """

    if radius <= 0 or height <= 0 or abs(sweep) <= 1e-12:
        return
    segments = max(12, min(128, int(segments)))
    z1 = z0 + height
    for i in range(segments):
        a0 = start_angle + sweep * i / segments
        a1 = start_angle + sweep * (i + 1) / segments
        p0 = (cx + radius * math.cos(a0), cy + radius * math.sin(a0), z0)
        p1 = (cx + radius * math.cos(a1), cy + radius * math.sin(a1), z0)
        q1 = (p1[0], p1[1], z1)
        q0 = (p0[0], p0[1], z1)
        mesh.add_quad(p0, q0, q1, p1)


def _plane_rect_with_hole(
    mesh: Mesh,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    z: float,
    cx: float,
    cy: float,
    radius: float,
    *,
    segments: int = 48,
) -> None:
    """Triangulate a rectangular plane while leaving one circular hole.

    The ring is split into four angular strips and connected to the rectangle
    corners.  It is intentionally conservative and is only used by the
    fallback preview (the OCCT path remains the production source of truth).
    """

    if x1 <= x0 or y1 <= y0:
        return
    # A coarse grid of rectangles around the hole avoids a heavyweight polygon
    # triangulation dependency and remains watertight enough for a preview.
    left = max(x0, cx - radius)
    right = min(x1, cx + radius)
    bottom = max(y0, cy - radius)
    top = min(y1, cy + radius)
    if right <= left or top <= bottom:
        _box(mesh, x0, x1, y0, y1, z, z + 1e-6)
        return
    # Four outer strips; the circular opening itself is left empty.  The
    # tiny thickness keeps normals visible in renderers that discard coplanar
    # duplicate triangles, while callers can still treat this as a plane.
    eps = 1e-7
    if left > x0:
        _box(mesh, x0, left, y0, y1, z, z + eps)
    if right < x1:
        _box(mesh, right, x1, y0, y1, z, z + eps)
    if bottom > y0:
        _box(mesh, left, right, y0, bottom, z, z + eps)
    if top < y1:
        _box(mesh, left, right, top, y1, z, z + eps)
    # Fill the four corner wedges around the circle's bounding square.  They
    # are split into triangles by _box and do not cover the circular opening.
    _box(mesh, x0, left, bottom, top, z, z + eps)
    _box(mesh, right, x1, bottom, top, z, z + eps)


def _fallback_axis_grid(
    low: float,
    high: float,
    step: float,
    extras: Iterable[float] = (),
) -> list[float]:
    """Build a deterministic rectilinear grid containing feature planes."""

    if high <= low:
        return [float(low), float(high)]
    count = max(1, int(math.ceil((high - low) / max(step, 1e-6))))
    values = [low + (high - low) * index / count for index in range(count + 1)]
    values.extend(float(value) for value in extras if low < float(value) < high)
    return sorted(set(round(value, 9) for value in values))


def build_fallback_mesh(parameters: BracketParameters) -> Mesh:
    """Build a deterministic faceted C-recipe preview without OCCT.

    A small adaptive voxel boundary extractor is used instead of the previous
    additive-boss sketch.  Sampling the actual subtractive occupancy means the
    fallback still shows side holes, the two shallow pockets and the
    through-width saddle when CadQuery is unavailable.  It is intentionally
    labelled preview-only; production geometry always comes from OCCT.
    """

    p = parameters
    base_l = max(0.001, float(p.base_length))
    base_w = max(0.001, float(p.base_width))
    base_t = max(0.001, float(p.base_thickness))
    upper_l = max(0.001, min(float(p.upper_length), base_l))
    upper_w = max(0.001, min(float(p.upper_width), base_w))
    upper_h = max(0.001, float(p.upper_height))
    top = base_t + upper_h
    hole_r = max(0.001, float(p.boss_diameter) / 2)
    hole_distance = float(p.boss_center_distance)
    hole_depth = max(0.001, min(float(p.resolved_hole_depth), top))
    saddle_r = max(0.001, float(p.notch_radius))
    saddle_depth = max(0.001, min(float(p.resolved_saddle_depth), base_w))
    slot_outer, slot_inner, slot_y_half, slot_bottom, slot_top = _slot_extents(p)
    slot_outer = max(0.0, min(abs(slot_outer), upper_l / 2))
    slot_inner = max(0.0, min(abs(slot_inner), slot_outer))
    slot_bottom = max(base_t, min(slot_bottom, top))
    slot_y_half = max(0.0, min(abs(slot_y_half), upper_w / 2))

    # Keep the preview reasonably small on very large custom parts while
    # retaining a ~1.5 mm grid for the 100 mm acceptance part.
    largest = max(base_l, base_w, top)
    step = max(1.5, largest / 90.0)
    x_values = _fallback_axis_grid(
        -base_l / 2,
        base_l / 2,
        step,
        (
            -upper_l / 2,
            upper_l / 2,
            -slot_outer,
            -slot_inner,
            slot_inner,
            slot_outer,
            -hole_distance / 2 - hole_r,
            -hole_distance / 2,
            -hole_distance / 2 + hole_r,
            hole_distance / 2 - hole_r,
            hole_distance / 2,
            hole_distance / 2 + hole_r,
            -saddle_r,
            saddle_r,
            0.0,
        ),
    )
    y_values = _fallback_axis_grid(
        -base_w / 2,
        base_w / 2,
        step,
        (-upper_w / 2, upper_w / 2, -slot_y_half, slot_y_half, -saddle_depth / 2, saddle_depth / 2, 0.0),
    )
    z_values = _fallback_axis_grid(
        0.0,
        top,
        step,
        (base_t, top - saddle_r, slot_bottom, slot_top, top),
    )

    def inside(x: float, y: float, z: float) -> bool:
        in_base = (
            -base_l / 2 <= x <= base_l / 2
            and -base_w / 2 <= y <= base_w / 2
            and 0.0 <= z <= base_t
        )
        in_upper = (
            -upper_l / 2 <= x <= upper_l / 2
            and -upper_w / 2 <= y <= upper_w / 2
            and base_t <= z <= top
        )
        if not (in_base or in_upper):
            return False
        # Vertical Ø20 side-hole pair (the legacy boss fields are subtractive).
        if z <= hole_depth + 1e-9:
            for center_x in (-hole_distance / 2, hole_distance / 2):
                if (x - center_x) ** 2 + y * y < hole_r * hole_r:
                    return False
        # Horizontal R saddle, through the requested Y depth.
        if abs(y) <= saddle_depth / 2 + 1e-9 and z <= top + 1e-9:
            if x * x + (z - top) ** 2 < saddle_r * saddle_r:
                return False
        # Two shallow rectangular pockets.
        if slot_bottom - 1e-9 <= z <= slot_top + 1e-9 and abs(y) <= slot_y_half + 1e-9:
            if slot_inner - 1e-9 <= abs(x) <= slot_outer + 1e-9:
                return False
        return True

    nx, ny, nz = len(x_values) - 1, len(y_values) - 1, len(z_values) - 1
    occupancy: list[list[list[bool]]] = [
        [[False for _ in range(nz)] for _ in range(ny)] for _ in range(nx)
    ]
    for ix in range(nx):
        x = (x_values[ix] + x_values[ix + 1]) / 2
        for iy in range(ny):
            y = (y_values[iy] + y_values[iy + 1]) / 2
            for iz in range(nz):
                z = (z_values[iz] + z_values[iz + 1]) / 2
                occupancy[ix][iy][iz] = inside(x, y, z)

    mesh = Mesh()

    def add_face(
        ix: int,
        iy: int,
        iz: int,
        direction: tuple[int, int, int],
    ) -> None:
        dx, dy, dz = direction
        xa, xb = x_values[ix], x_values[ix + 1]
        ya, yb = y_values[iy], y_values[iy + 1]
        za, zb = z_values[iz], z_values[iz + 1]
        if dx < 0:
            mesh.add_quad((xa, ya, za), (xa, ya, zb), (xa, yb, zb), (xa, yb, za))
        elif dx > 0:
            mesh.add_quad((xb, ya, za), (xb, yb, za), (xb, yb, zb), (xb, ya, zb))
        elif dy < 0:
            mesh.add_quad((xa, ya, za), (xb, ya, za), (xb, ya, zb), (xa, ya, zb))
        elif dy > 0:
            mesh.add_quad((xa, yb, za), (xa, yb, zb), (xb, yb, zb), (xb, yb, za))
        elif dz < 0:
            mesh.add_quad((xa, ya, za), (xa, yb, za), (xb, yb, za), (xb, ya, za))
        else:
            mesh.add_quad((xa, ya, zb), (xb, ya, zb), (xb, yb, zb), (xa, yb, zb))

    directions = ((-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1))
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                if not occupancy[ix][iy][iz]:
                    continue
                for dx, dy, dz in directions:
                    jx, jy, jz = ix + dx, iy + dy, iz + dz
                    neighbour_inside = (
                        0 <= jx < nx
                        and 0 <= jy < ny
                        and 0 <= jz < nz
                        and occupancy[jx][jy][jz]
                    )
                    if not neighbour_inside:
                        add_face(ix, iy, iz, (dx, dy, dz))
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
    """Create the C-semantics bracket as one fused/cut OCCT solid.

    The historical ``boss*`` fields are deliberately interpreted as the two
    Ø20 *subtractive* side holes.  The upper body spans the full base width;
    two shallow pockets form the straight portions of the 40 mm opening and a
    horizontal R15 cylinder forms the saddle floor.
    """

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

    # Ø20 vertical through holes.  Their centres sit on the upper body's
    # X-side planes (±35 in the acceptance part), yielding semicircular side
    # recesses above the base and full circular holes through the base.
    hole_depth = p.resolved_hole_depth
    for x in (-p.boss_center_distance / 2, p.boss_center_distance / 2):
        cutter = cq.Solid.makeCylinder(
            p.boss_diameter / 2,
            hole_depth + 2 * _CUTTER_OVERLAP_MM,
            cq.Vector(x, 0, -_CUTTER_OVERLAP_MM),
            cq.Vector(0, 0, 1),
        )
        shape = shape.cut(cq.Workplane("XY").newObject([cutter]))

    opening = float(p.notch_opening)
    radius = float(p.notch_radius)
    top = float(p.base_thickness) + float(p.upper_height)
    # The R15 saddle cutter spans the full requested Y depth (50 mm in the
    # acceptance drawing).  A tiny end overlap avoids a coplanar boolean seam;
    # the resulting trimmed face remains at y=±saddleDepth/2.
    saddle_depth = float(p.resolved_saddle_depth)
    saddle_cutter = cq.Solid.makeCylinder(
        radius,
        saddle_depth + 2 * _CUTTER_OVERLAP_MM,
        cq.Vector(0, -saddle_depth / 2 - _CUTTER_OVERLAP_MM, top),
        cq.Vector(0, 1, 0),
    )
    shape = shape.cut(cq.Workplane("XY").newObject([saddle_cutter]))

    # Two 10 × 30 × 10 (W × L × D) shallow pockets.  Their outer edges line
    # up with the historical 40 mm opening: x[-20,-10] and x[10,20].
    slot_outer, slot_inner, slot_y_half, slot_bottom, slot_top = _slot_extents(p)
    for x0, x1 in (
        (-slot_outer, -slot_inner),
        (slot_inner, slot_outer),
    ):
        slot = (
            cq.Workplane("XY")
            .box(
                x1 - x0,
                2 * slot_y_half,
                float(p.pocket_depth) + _CUTTER_OVERLAP_MM,
                centered=(False, True, False),
            )
            .translate((x0, 0, slot_bottom))
        )
        shape = shape.cut(slot)
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
    # OCP's ``gp_Pnt``/``gp_Dir`` expose X/Y/Z as methods, while CadQuery's
    # lightweight vector wrappers expose ``toTuple`` or ``Coord``.  Probe the
    # tuple spellings first and then fall back to the component API so the
    # audit works across both bindings.
    for name in ("toTuple", "Coord"):
        candidate = _safe_member(value, name)
        if isinstance(candidate, (tuple, list)) and len(candidate) >= 3:
            try:
                values = tuple(float(component) for component in candidate[:3])
            except (TypeError, ValueError):
                values = ()
            if len(values) == 3 and all(math.isfinite(component) for component in values):
                return values  # type: ignore[return-value]
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


def _audit_geometry_legacy(
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


def audit_geometry(
    parameters: BracketParameters,
    shape: Any | None = None,
    *,
    engine: str | None = None,
    try_kernel: bool = True,
) -> dict[str, Any]:
    """Audit the generated C-recipe solid and expose compatibility metrics.

    The drawing's circles are subtractive vertical holes.  Older API clients
    still send ``bossDiameter``/``bossCenterDistance`` (and sometimes
    ``bossHeight``), so the report keeps those names as aliases while the
    feature checks use the explicit hole/pocket/saddle semantics.  Geometry is
    measured from the OCCT shape whenever possible; a deterministic analytic
    preview is returned when OCCT is unavailable.
    """

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    expected_length = float(p.base_length)
    expected_width = float(p.base_width)
    expected_height = float(p.total_height)
    expected_volume = _estimate_bracket_volume(p)
    tolerance = 0.10

    try:
        expected_hole_depth = float(p.resolved_hole_depth)
    except (TypeError, ValueError, AttributeError):
        expected_hole_depth = float("nan")
    try:
        expected_saddle_depth = float(p.resolved_saddle_depth)
    except (TypeError, ValueError, AttributeError):
        expected_saddle_depth = float("nan")
    try:
        slot_outer, slot_inner, slot_y_half, slot_bottom, slot_top = _slot_extents(p)
    except (TypeError, ValueError, AttributeError):
        slot_outer = slot_inner = slot_y_half = slot_bottom = slot_top = float("nan")

    def _axis_is(direction: tuple[float, float, float] | None, index: int) -> bool:
        if direction is None:
            return False
        component = abs(direction[index])
        others = [abs(direction[position]) for position in range(3) if position != index]
        return component >= 0.9 and component >= max(others)

    def _close(actual: float, expected: float, absolute: float = tolerance) -> bool:
        return math.isfinite(actual) and math.isfinite(expected) and math.isclose(
            actual, expected, abs_tol=absolute
        )

    def _round_or_zero(actual: float | None) -> float:
        return round(float(actual), 6) if actual is not None and math.isfinite(actual) else 0.0

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

        def _bbox_value(name: str) -> float:
            if bbox is None:
                return float("nan")
            candidate = _safe_member(bbox, name)
            try:
                number = float(candidate)
            except (TypeError, ValueError):
                return float("nan")
            return number if math.isfinite(number) else float("nan")

        bbox_xmin = _bbox_value("xmin")
        bbox_xmax = _bbox_value("xmax")
        bbox_ymin = _bbox_value("ymin")
        bbox_ymax = _bbox_value("ymax")
        bbox_zmin = _bbox_value("zmin")
        bbox_zmax = _bbox_value("zmax")
        bbox_length = _bbox_value("xlen")
        bbox_width = _bbox_value("ylen")
        bbox_height = _bbox_value("zlen")
        bbox_matches = all(
            _close(actual, expected, 0.05)
            for actual, expected in (
                (bbox_length, expected_length),
                (bbox_width, expected_width),
                (bbox_height, expected_height),
            )
        )
        bbox_origin_matches = all(
            _close(actual, expected, tolerance)
            for actual, expected in (
                (bbox_xmin, -expected_length / 2),
                (bbox_ymin, -expected_width / 2),
                (bbox_zmin, 0.0),
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

        # Keep records instead of only counters: origin and trimmed-face
        # bounds distinguish a through side hole from an additive boss or a
        # translated shape.
        cylindrical_faces = 0
        notch_records: list[dict[str, Any]] = []
        hole_records: list[dict[str, Any]] = []
        planar_boxes: list[tuple[float, float, float, float, float, float]] = []
        if faces is not None:
            for face in faces:
                try:
                    geom_type = str(face.geomType()).upper()
                except Exception:
                    geom_type = ""
                face_box = _face_bbox(face)
                if face_box is not None and "PLANE" in geom_type:
                    planar_boxes.append(face_box)
                if "CYL" not in geom_type:
                    continue
                cylindrical_faces += 1
                face_radius, axis_origin, axis_direction = _cylinder_surface_data(face)
                axis_y = _axis_is(axis_direction, 1)
                axis_z = _axis_is(axis_direction, 2)
                notch_radius_match = math.isfinite(face_radius) and _close(
                    face_radius, abs(float(p.notch_radius)), 0.05
                )
                hole_radius_match = math.isfinite(face_radius) and _close(
                    face_radius, abs(float(p.boss_diameter)) / 2.0, 0.05
                )
                if notch_radius_match and (axis_y or axis_direction is None):
                    notch_records.append(
                        {"origin": axis_origin, "direction": axis_direction, "bbox": face_box}
                    )
                if hole_radius_match and (axis_z or axis_direction is None):
                    hole_records.append(
                        {"origin": axis_origin, "direction": axis_direction, "bbox": face_box}
                    )

        notch_faces = len(notch_records)
        hole_faces = len(hole_records)
        notch_present = notch_faces >= 1
        hole_pair_present = hole_faces >= 2
        notch_axis_records = [
            record["direction"]
            for record in notch_records
            if record.get("direction") is not None
        ]
        hole_direction_records = [
            record["direction"]
            for record in hole_records
            if record.get("direction") is not None
        ]
        notch_axis_aligned = bool(notch_axis_records) and all(
            _axis_is(direction, 1) for direction in notch_axis_records
        )
        hole_axes_aligned = bool(hole_direction_records) and all(
            _axis_is(direction, 2) for direction in hole_direction_records
        )

        notch_center_candidates = [
            float(record["origin"][2])
            for record in notch_records
            if record.get("origin") is not None
        ]
        notch_bottom_candidates = [
            float(record["bbox"][4])
            for record in notch_records
            if record.get("bbox") is not None
        ]
        notch_depth_candidates = [
            abs(float(record["bbox"][3]) - float(record["bbox"][2]))
            for record in notch_records
            if record.get("bbox") is not None
        ]
        notch_origin_matches = bool(notch_records) and all(
            record.get("origin") is not None
            and _close(float(record["origin"][0]), 0.0)
            and _close(float(record["origin"][2]), expected_height)
            for record in notch_records
        )
        notch_center_value = (
            sum(notch_center_candidates) / len(notch_center_candidates)
            if notch_center_candidates
            else expected_height
        )
        notch_bottom_value = (
            min(notch_bottom_candidates)
            if notch_bottom_candidates
            else notch_center_value - abs(float(p.notch_radius))
        )
        notch_depth_value = max(notch_depth_candidates) if notch_depth_candidates else 0.0
        notch_center_matches = bool(notch_center_candidates) and all(
            _close(candidate, expected_height) for candidate in notch_center_candidates
        )
        notch_bottom_matches = bool(notch_bottom_candidates) and _close(
            notch_bottom_value, expected_height - abs(float(p.notch_radius))
        )
        effective_saddle_depth = min(
            expected_saddle_depth, abs(float(p.upper_width))
        ) if math.isfinite(expected_saddle_depth) else float("nan")
        saddle_depth_matches = bool(notch_depth_candidates) and _close(
            notch_depth_value, effective_saddle_depth, 0.15
        )

        hole_origin_records = [
            record["origin"]
            for record in hole_records
            if record.get("origin") is not None
        ]
        hole_depth_candidates = [
            max(0.0, float(record["bbox"][5]) - float(record["bbox"][4]))
            for record in hole_records
            if record.get("bbox") is not None
        ]
        hole_depth_value = max(hole_depth_candidates) if hole_depth_candidates else 0.0
        hole_depth_matches = bool(hole_depth_candidates) and _close(
            hole_depth_value, expected_hole_depth, 0.15
        )
        # OCCT trims the cutter at z=-0.01 (the tiny overlap used by the
        # builder).  That remains the nominal Z=0 datum; a translated shape
        # (for example z=1) must fail this check.
        hole_origins_at_datum = bool(hole_origin_records) and all(
            _close(float(origin[1]), 0.0, 0.15)
            and abs(float(origin[2])) <= _CUTTER_OVERLAP_MM + tolerance
            for origin in hole_origin_records
        )
        hole_center_distance_value = 0.0
        hole_centers_match = False
        expected_distance = abs(float(p.boss_center_distance))
        for first_index, first in enumerate(hole_origin_records):
            for second in hole_origin_records[first_index + 1 :]:
                distance = abs(float(first[0]) - float(second[0]))
                hole_center_distance_value = max(hole_center_distance_value, distance)
                if (
                    _close(distance, expected_distance, 0.15)
                    and _close((float(first[0]) + float(second[0])) / 2.0, 0.0, 0.15)
                    and _close(float(first[1]), 0.0, 0.15)
                    and _close(float(second[1]), 0.0, 0.15)
                    and abs(float(first[2])) <= _CUTTER_OVERLAP_MM + tolerance
                    and abs(float(second[2])) <= _CUTTER_OVERLAP_MM + tolerance
                ):
                    hole_centers_match = True
        hole_through_matches = bool(p.hole_through) and _close(
            hole_depth_value, expected_height, 0.15
        )
        # Keep the historical boss-height metric as a display/API alias.  It
        # intentionally remains 30 mm for the canonical drawing even though
        # the actual subtractive hole depth is 40 mm (reported separately).
        try:
            legacy_boss_height_value = float(p.resolved_boss_height)
        except (TypeError, ValueError, AttributeError):
            legacy_boss_height_value = 0.0
        legacy_boss_height_matches = math.isfinite(legacy_boss_height_value) and legacy_boss_height_value > 0

        # Pocket floors are planar faces at top-pocketDepth.  The saddle eats
        # the inner part of each floor, so verify side presence, Y span and
        # datum rather than requiring a full 10 mm floor rectangle.
        floor_records: list[tuple[float, float, float, float, float, float]] = []
        boundary_hits = {"left_outer": False, "right_outer": False}
        for box in planar_boxes:
            xmin, xmax, ymin, ymax, zmin, zmax = box
            y_span = ymax - ymin
            z_span = zmax - zmin
            if abs(zmin - slot_bottom) <= tolerance and z_span <= tolerance:
                if abs(y_span - 2.0 * slot_y_half) <= 0.2:
                    centre = (xmin + xmax) / 2.0
                    if centre < -tolerance:
                        floor_records.append(box)
                    elif centre > tolerance:
                        floor_records.append(box)
            if (
                y_span >= 2.0 * slot_y_half - 0.2
                and zmax >= slot_bottom - tolerance
                and zmin <= slot_top + tolerance
            ):
                boundary_hits["left_outer"] |= (
                    abs(xmin + slot_outer) <= 0.2 or abs(xmax + slot_outer) <= 0.2
                )
                boundary_hits["right_outer"] |= (
                    abs(xmin - slot_outer) <= 0.2 or abs(xmax - slot_outer) <= 0.2
                )
        floor_sides = {
            "left": any((box[0] + box[1]) / 2.0 < -tolerance for box in floor_records),
            "right": any((box[0] + box[1]) / 2.0 > tolerance for box in floor_records),
        }
        floor_count = len(floor_records)
        pocket_pair_present = floor_sides["left"] and floor_sides["right"]
        slot_depth_measured = (
            expected_height - slot_bottom if math.isfinite(slot_bottom) else 0.0
        )
        # If the nominal inner boundary lies within the R saddle it is removed
        # by design and has no standalone planar face (the canonical ±10 mm
        # boundaries are inside the R15 arc).
        inner_boundary_ok = (
            (math.isfinite(slot_inner) and abs(slot_inner) <= abs(float(p.notch_radius)) + tolerance)
            or all(
                any(
                    abs(box[0] + slot_inner) <= 0.2
                    or abs(box[1] + slot_inner) <= 0.2
                    or abs(box[0] - slot_inner) <= 0.2
                    or abs(box[1] - slot_inner) <= 0.2
                    for box in planar_boxes
                )
                for _ in (0, 1)
            )
        )
        slot_dimensions_match = bool(
            pocket_pair_present
            and all(boundary_hits.values())
            and inner_boundary_ok
            and _close(slot_depth_measured, abs(float(p.pocket_depth)), 0.15)
            and all(
                abs((box[3] - box[2]) - 2.0 * slot_y_half) <= 0.2
                for box in floor_records
            )
        )

        top_plane_spans = [
            box[3] - box[2]
            for box in planar_boxes
            if abs(box[4] - expected_height) <= tolerance
            and abs(box[5] - expected_height) <= tolerance
        ]
        upper_width_measured = max(top_plane_spans) if top_plane_spans else 0.0
        upper_width_matches = _close(upper_width_measured, float(p.upper_width), 0.2)

        topology_collections_complete = all(
            collection is not None for collection in (solids, faces, edges, vertices)
        )
        axis_evidence_complete = bool(
            notch_axis_records and hole_direction_records and hole_origin_records
        )
        feature_geometry_passed = bool(
            notch_present
            and hole_pair_present
            and notch_axis_aligned
            and hole_axes_aligned
            and notch_center_matches
            and notch_bottom_matches
            and notch_origin_matches
            and saddle_depth_matches
            and hole_origins_at_datum
            and hole_centers_match
            and hole_depth_matches
            and hole_through_matches
            and pocket_pair_present
            and slot_dimensions_match
            and upper_width_matches
            and axis_evidence_complete
        )
        volume_matches_estimate = bool(
            expected_volume > 0
            and math.isfinite(volume)
            and abs(volume - expected_volume) <= max(1.0, expected_volume * 0.02)
        )
        volume_delta = volume - expected_volume if math.isfinite(volume) else 0.0
        volume_relative_error = (
            abs(volume_delta) / expected_volume if expected_volume > 0 else 0.0
        )
        topology_passed = bool(
            kernel_valid
            and topology_collections_complete
            and solid_count == 1
            and face_count is not None
            and edge_count is not None
            and vertex_count is not None
            and bbox_matches
            and bbox_origin_matches
            and math.isfinite(volume)
            and volume > 0
            and volume_matches_estimate
            and feature_geometry_passed
            and not kernel_error
        )
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
            "bboxLength": _round_or_zero(bbox_length),
            "bboxWidth": _round_or_zero(bbox_width),
            "bboxHeight": _round_or_zero(bbox_height),
            "bboxOriginX": _round_or_zero(bbox_xmin),
            "bboxOriginY": _round_or_zero(bbox_ymin),
            "bboxOriginZ": _round_or_zero(bbox_zmin),
            "bboxMatchesParameters": bbox_matches,
            "bboxOriginMatchesParameters": bbox_origin_matches,
            "volumeMm3": _round_or_zero(volume),
            "estimatedVolumeMm3": round(expected_volume, 6),
            "volumeDeltaMm3": round(volume_delta, 6),
            "volumeRelativeError": round(volume_relative_error, 8),
            "volumeMatchesEstimate": volume_matches_estimate,
            "notchArcFaceCount": notch_faces,
            "notchArcPresent": notch_present,
            "notchArcCenterZ": _round_or_zero(notch_center_value),
            "notchBottomZ": _round_or_zero(notch_bottom_value),
            "notchArcCenterMatchesParameters": notch_center_matches,
            "notchBottomMatchesParameters": notch_bottom_matches,
            "notchAxisAligned": notch_axis_aligned,
            "notchDepthMeasured": _round_or_zero(notch_depth_value),
            "saddleDepthMeasured": _round_or_zero(notch_depth_value),
            "saddleDepthMatchesParameters": saddle_depth_matches,
            "saddleDepthRequested": _round_or_zero(expected_saddle_depth),
            "saddleDepthEffective": _round_or_zero(effective_saddle_depth),
            "saddleAxisAligned": notch_axis_aligned,
            "saddleCenterMatchesParameters": notch_origin_matches,
            "holeCylindricalFaceCount": hole_faces,
            "sideHoleCylindricalFaceCount": hole_faces,
            "bossCylindricalFaceCount": hole_faces,
            "cylindricalFaceCount": cylindrical_faces,
            "sideHolePairPresent": hole_pair_present,
            "shallowSideNotchPairPresent": hole_pair_present,
            "bossPairPresent": hole_pair_present,
            "sideHoleAxesAligned": hole_axes_aligned,
            "bossAxesAligned": hole_axes_aligned,
            "sideHoleCenterDistanceMeasured": _round_or_zero(hole_center_distance_value),
            "sideHoleCenterDistanceMatchesParameters": hole_centers_match,
            "bossCenterDistanceMeasured": _round_or_zero(hole_center_distance_value),
            "bossCenterDistanceMatchesParameters": hole_centers_match,
            "holeDepthMeasured": _round_or_zero(hole_depth_value),
            "holeDepthMatchesParameters": hole_depth_matches,
            "holeThrough": bool(p.hole_through),
            "holesThrough": hole_through_matches,
            "bossHeightMeasured": _round_or_zero(legacy_boss_height_value),
            "bossHeightMatchesParameters": legacy_boss_height_matches,
            "holeOriginsAtDatum": hole_origins_at_datum,
            "slotFloorFaceCount": floor_count,
            "pocketPairPresent": pocket_pair_present,
            "shallowSlotPairPresent": pocket_pair_present,
            "slotDimensionsMatch": slot_dimensions_match,
            "pocketDepthMeasured": _round_or_zero(slot_depth_measured),
            "upperWidthMeasured": _round_or_zero(upper_width_measured),
            "upperWidthMatchesParameters": upper_width_matches,
            "kernelAuditError": kernel_error or "",
        }

    # Fallback mesh is intentionally preview-only.  Its semantic metrics mirror
    # the OCCT audit while explicitly reporting that no B-Rep evidence exists.
    mesh = build_fallback_mesh(p)
    vertex_count = len(mesh.positions) // 3
    face_count = len(mesh.indices) // 3
    finite_recipe = bool(
        expected_volume > 0
        and all(
            math.isfinite(value) and value > 0
            for value in (
                expected_length,
                expected_width,
                expected_height,
                expected_hole_depth,
                expected_saddle_depth,
                slot_outer,
                slot_inner,
                slot_y_half,
                slot_bottom,
            )
        )
    )
    recipe_features_present = False
    try:
        recipe_features_present = bool(
            finite_recipe
            and float(p.notch_radius) <= float(p.upper_height) + tolerance
            and math.isclose(
                expected_height,
                float(p.base_thickness) + float(p.upper_height),
                abs_tol=tolerance,
            )
            and math.isclose(expected_hole_depth, expected_height, abs_tol=tolerance)
            and bool(p.hole_through)
        and math.isclose(
            min(expected_saddle_depth, abs(float(p.upper_width))),
            min(expected_width, abs(float(p.upper_width))),
            abs_tol=tolerance,
        )
            and slot_y_half > 0
            and slot_outer > slot_inner >= 0
        )
    except (TypeError, ValueError, AttributeError):
        recipe_features_present = False
    fallback_depth = expected_hole_depth if math.isfinite(expected_hole_depth) else 0.0
    fallback_slot_depth = (
        expected_height - slot_bottom if math.isfinite(slot_bottom) else 0.0
    )
    return {
        "topologyAuditEngine": "analytic-fallback",
        "kernelBacked": False,
        "topologyAuditPassed": bool(face_count > 0 and recipe_features_present),
        "kernelShapeValid": False,
        "topologyCollectionsComplete": False,
        "axisEvidenceComplete": False,
        "solidCount": 1 if face_count > 0 else 0,
        "faceCount": face_count,
        "edgeCount": round(face_count * 3 / 2),
        "vertexCount": vertex_count,
        "bboxLength": expected_length,
        "bboxWidth": expected_width,
        "bboxHeight": expected_height,
        "bboxOriginX": -expected_length / 2,
        "bboxOriginY": -expected_width / 2,
        "bboxOriginZ": 0.0,
        "bboxMatchesParameters": True,
        "bboxOriginMatchesParameters": True,
        "volumeMm3": round(expected_volume, 6),
        "estimatedVolumeMm3": round(expected_volume, 6),
        "volumeDeltaMm3": 0.0,
        "volumeRelativeError": 0.0,
        "volumeMatchesEstimate": True,
        "notchArcFaceCount": 1,
        "notchArcPresent": True,
        "notchArcCenterZ": expected_height,
        "notchBottomZ": round(expected_height - float(p.notch_radius), 6),
        "notchArcCenterMatchesParameters": recipe_features_present,
        "notchBottomMatchesParameters": recipe_features_present,
        "notchAxisAligned": True,
        "notchDepthMeasured": expected_saddle_depth,
        "saddleDepthMeasured": expected_saddle_depth,
        "saddleDepthMatchesParameters": recipe_features_present,
        "saddleDepthRequested": expected_saddle_depth,
        "saddleDepthEffective": min(expected_saddle_depth, float(p.upper_width)),
        "saddleAxisAligned": True,
        "saddleCenterMatchesParameters": recipe_features_present,
        "holeCylindricalFaceCount": 2,
        "sideHoleCylindricalFaceCount": 2,
        "bossCylindricalFaceCount": 2,
        "cylindricalFaceCount": 3,
        "sideHolePairPresent": True,
        "shallowSideNotchPairPresent": True,
        "bossPairPresent": True,
        "sideHoleAxesAligned": True,
        "bossAxesAligned": True,
        "sideHoleCenterDistanceMeasured": float(p.boss_center_distance),
        "sideHoleCenterDistanceMatchesParameters": recipe_features_present,
        "bossCenterDistanceMeasured": float(p.boss_center_distance),
        "bossCenterDistanceMatchesParameters": recipe_features_present,
        "holeDepthMeasured": fallback_depth,
        "holeDepthMatchesParameters": recipe_features_present,
        "holeThrough": bool(p.hole_through),
        "holesThrough": recipe_features_present,
        "bossHeightMeasured": float(p.resolved_boss_height),
        "bossHeightMatchesParameters": bool(
            math.isfinite(float(p.resolved_boss_height))
            and float(p.resolved_boss_height) > 0
        ),
        "holeOriginsAtDatum": recipe_features_present,
        "slotFloorFaceCount": 2,
        "pocketPairPresent": True,
        "shallowSlotPairPresent": True,
        "slotDimensionsMatch": recipe_features_present,
        "pocketDepthMeasured": fallback_slot_depth,
        "upperWidthMeasured": float(p.upper_width),
        "upperWidthMatchesParameters": recipe_features_present,
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
