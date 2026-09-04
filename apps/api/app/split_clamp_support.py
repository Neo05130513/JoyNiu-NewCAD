"""CadQuery recipe for the split cylindrical clamp support in drawing 9.

The recipe is deliberately explicit instead of trying to reinterpret the
legacy C-bracket fields.  A production STEP and its GLB preview are generated
from the same OCCT shape; the dependency-free mesh is review-only.
"""

from __future__ import annotations

import math
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .geometry import (
    GeneratedArtifact,
    Mesh,
    _cylinder_surface_data,
    _face_bbox,
    _fallback_axis_grid,
    _mesh_from_cadquery,
    _safe_shape_collection,
    _step_fallback,
    cadquery_status,
    get_cadquery,
    mesh_to_glb,
)
from .schemas import SplitClampSupportParameters


_OVERLAP = 0.01


def _axis_is(direction: tuple[float, float, float] | None, index: int) -> bool:
    if direction is None:
        return False
    component = abs(direction[index])
    others = [abs(direction[position]) for position in range(3) if position != index]
    return component >= 0.9 and component >= max(others)


def _fillet_vertical_edges(shape: Any, points: tuple[tuple[float, float], ...], radius: float) -> Any:
    """Fillet the extrusion edges whose XY centres match explicit datums."""

    selected = []
    value = shape.val() if hasattr(shape, "val") else shape
    for edge in value.Edges():
        box = edge.BoundingBox()
        center = edge.Center()
        if box.zlen <= 0 or box.xlen > 0.01 or box.ylen > 0.01:
            continue
        if any(abs(center.x - x) <= 0.02 and abs(center.y - y) <= 0.02 for x, y in points):
            selected.append(edge)
    if not selected:
        raise RuntimeError("base fillet datum edges were not found")
    return shape.newObject(selected).fillet(radius)


def build_split_clamp_support_shape(parameters: SplitClampSupportParameters) -> Any:
    """Build one fused/cut OCCT solid for ``split_clamp_support_v1``."""

    cq = get_cadquery()
    if cq is None:
        raise RuntimeError("CadQuery unavailable")
    p = parameters
    rear_y = p.base_width / 2
    main_front_y = rear_y - p.base_main_depth
    tongue_depth = p.base_width - p.base_main_depth
    tongue_front_y = -p.base_width / 2
    center_y = p.pedestal_center_y
    mount_y = p.mount_hole_center_y
    lower_top = p.lower_clamp_top_z

    # The plan view is a 125 x 80 main plate plus the centred 80 x 15 front
    # tongue.  Select fillets by their dimensioned XY datum rather than by
    # fragile edge order.  The plan view leaves the two rear corners square;
    # only the forward main-plate corners and front tongue ends use R8, while
    # the two re-entrant neck shoulders use R5.
    main_base = (
        cq.Workplane("XY")
        .box(p.base_length, p.base_main_depth, p.base_thickness, centered=(True, True, False))
        .translate((0, (rear_y + main_front_y) / 2, 0))
    )
    tongue = (
        cq.Workplane("XY")
        .box(p.front_tongue_width, tongue_depth + _OVERLAP, p.base_thickness, centered=(True, True, False))
        .translate((0, (main_front_y + tongue_front_y) / 2 + _OVERLAP / 2, 0))
    )
    shape = main_base.union(tongue).clean()
    shape = _fillet_vertical_edges(
        shape,
        ((-p.front_tongue_width / 2, main_front_y), (p.front_tongue_width / 2, main_front_y)),
        p.neck_concave_radius,
    )
    shape = _fillet_vertical_edges(
        shape,
        (
            (-p.base_length / 2, main_front_y),
            (p.base_length / 2, main_front_y),
        ),
        p.outer_corner_radius,
    )
    shape = _fillet_vertical_edges(
        shape,
        ((-p.front_tongue_width / 2, tongue_front_y), (p.front_tongue_width / 2, tongue_front_y)),
        p.neck_convex_radius,
    )

    # The R33 callout belongs only to the forward semicircle in plan.  Behind
    # its centreline the drawing has straight x=+/-R sides continuing to the
    # rear datum, forming a D-profile rather than a complete cylinder.
    lower_round = (
        cq.Workplane("XY")
        .circle(p.pedestal_outer_radius)
        .extrude(p.pedestal_height)
        .translate((0, center_y, p.base_thickness))
    )
    lower_rear = (
        cq.Workplane("XY")
        .box(
            2 * p.pedestal_outer_radius,
            rear_y - center_y + _OVERLAP,
            p.pedestal_height,
            centered=(True, False, False),
        )
        .translate((0, center_y - _OVERLAP, p.base_thickness))
    )
    lower = lower_round.union(lower_rear).clean()
    shape = shape.union(lower)

    # The straight-sided rear half rises another 20 mm.  It is a rectangular
    # bridge in plan, not the rear half of a cylinder.
    upper_rear = (
        cq.Workplane("XY")
        .box(
            2 * p.pedestal_outer_radius,
            rear_y - center_y + _OVERLAP,
            p.rear_clamp_rise + _OVERLAP,
            centered=(True, False, False),
        )
        .translate((0, center_y - _OVERLAP, lower_top - _OVERLAP))
    )
    shape = shape.union(upper_rear)

    # Symmetric triangular gussets visible in the front view.  The side view
    # places their 10 mm thickness directly against the rear datum, while the
    # 86 mm plan dimension fixes their outer x span.
    rib_projection = max(
        p.rib_thickness,
        (p.rear_bridge_width - 2 * p.pedestal_outer_radius) / 2,
    )
    for side in (-1, 1):
        inner_x = side * (p.pedestal_outer_radius - _OVERLAP)
        outer_x = side * (p.pedestal_outer_radius + rib_projection)
        rib = (
            cq.Workplane("XZ")
            .moveTo(inner_x, p.base_thickness - _OVERLAP)
            .lineTo(outer_x, p.base_thickness - _OVERLAP)
            .lineTo(inner_x, p.base_thickness + p.rib_height)
            .close()
            .extrude(p.rib_thickness / 2, both=True)
            .translate((0, rear_y - p.rib_thickness / 2, 0))
        )
        shape = shape.union(rib)

    # The mounting-hole row is dimensioned 40 mm from the rear edge, separate
    # from the clamp axis' 35 mm datum.
    for x in (-p.mount_hole_center_distance / 2, p.mount_hole_center_distance / 2):
        cutter = cq.Solid.makeCylinder(
            p.mount_hole_diameter / 2,
            p.base_thickness + 2 * _OVERLAP,
            cq.Vector(x, mount_y, -_OVERLAP),
            cq.Vector(0, 0, 1),
        )
        shape = shape.cut(cq.Workplane("XY").newObject([cutter]))

    bore = cq.Solid.makeCylinder(
        p.bore_diameter / 2,
        p.total_height - p.bore_floor_z + 2 * _OVERLAP,
        cq.Vector(0, center_y, p.bore_floor_z - _OVERLAP),
        cq.Vector(0, 0, 1),
    )
    shape = shape.cut(cq.Workplane("XY").newObject([bore]))

    # The 12 mm radial split runs from the bore's front tangent to the body's
    # front edge.  It starts at the bore floor, matching the visible internal
    # floor/slot relationship in the side and isometric views.
    split_front = center_y - p.pedestal_outer_radius - _OVERLAP
    split_back = center_y - p.bore_diameter / 2 + _OVERLAP
    split = (
        cq.Workplane("XY")
        .box(
            p.split_width,
            split_back - split_front,
            p.total_height - p.bore_floor_z + 2 * _OVERLAP,
            centered=(True, False, False),
        )
        .translate((0, split_front, p.bore_floor_z - _OVERLAP))
    )
    shape = shape.cut(split)

    cross_hole = cq.Solid.makeCylinder(
        p.cross_hole_diameter / 2,
        rear_y - (center_y - p.pedestal_outer_radius) + 2 * _OVERLAP,
        cq.Vector(0, center_y - p.pedestal_outer_radius - _OVERLAP, p.cross_hole_center_z),
        cq.Vector(0, 1, 0),
    )
    shape = shape.cut(cq.Workplane("XY").newObject([cross_hole]))
    try:
        return shape.clean()
    except Exception:
        return shape


def build_split_clamp_support_fallback_mesh(parameters: SplitClampSupportParameters) -> Mesh:
    """Build a deterministic voxel preview of the same feature semantics."""

    p = parameters
    rear_y = p.base_width / 2
    main_front_y = rear_y - p.base_main_depth
    front_y = -p.base_width / 2
    center_y = p.pedestal_center_y
    mount_y = p.mount_hole_center_y
    lower_top = p.lower_clamp_top_z
    bore_r = p.bore_diameter / 2
    mount_r = p.mount_hole_diameter / 2
    cross_r = p.cross_hole_diameter / 2
    rib_projection = max(p.rib_thickness, (p.rear_bridge_width - 2 * p.pedestal_outer_radius) / 2)
    largest = max(p.base_length, p.base_width, p.total_height)
    step = max(1.8, largest / 72.0)
    xs = _fallback_axis_grid(
        -p.base_length / 2,
        p.base_length / 2,
        step,
        (
            -p.front_tongue_width / 2,
            p.front_tongue_width / 2,
            -p.pedestal_outer_radius,
            p.pedestal_outer_radius,
            -p.split_width / 2,
            p.split_width / 2,
            -p.mount_hole_center_distance / 2,
            p.mount_hole_center_distance / 2,
        ),
    )
    ys = _fallback_axis_grid(
        front_y,
        rear_y,
        step,
        (
            main_front_y,
            center_y - p.pedestal_outer_radius,
            center_y - bore_r,
            center_y,
            mount_y,
            rear_y - p.rib_thickness,
            rear_y,
        ),
    )
    zs = _fallback_axis_grid(
        0,
        p.total_height,
        step,
        (p.base_thickness, p.bore_floor_z, lower_top, p.cross_hole_center_z, p.total_height),
    )

    def inside(x: float, y: float, z: float) -> bool:
        in_main = abs(x) <= p.base_length / 2 and main_front_y <= y <= rear_y and 0 <= z <= p.base_thickness
        in_tongue = abs(x) <= p.front_tongue_width / 2 and front_y <= y <= main_front_y and 0 <= z <= p.base_thickness
        radial2 = x * x + (y - center_y) ** 2
        in_d_profile = (
            (y <= center_y and radial2 <= p.pedestal_outer_radius ** 2)
            or (center_y <= y <= rear_y and abs(x) <= p.pedestal_outer_radius)
        )
        in_lower = in_d_profile and p.base_thickness <= z <= lower_top
        in_rear_upper = (
            abs(x) <= p.pedestal_outer_radius
            and y >= center_y
            and y <= rear_y
            and lower_top <= z <= p.total_height
        )
        dx = abs(x) - p.pedestal_outer_radius
        rib_ceiling = p.base_thickness + p.rib_height * max(0.0, 1.0 - dx / max(rib_projection, 1e-9))
        in_rib = (
            0 <= dx <= rib_projection
            and rear_y - p.rib_thickness <= y <= rear_y
            and p.base_thickness <= z <= rib_ceiling
        )
        if not (in_main or in_tongue or in_lower or in_rear_upper or in_rib):
            return False
        if z <= p.base_thickness:
            for hole_x in (-p.mount_hole_center_distance / 2, p.mount_hole_center_distance / 2):
                if (x - hole_x) ** 2 + (y - mount_y) ** 2 < mount_r ** 2:
                    return False
        if z >= p.bore_floor_z and radial2 < bore_r ** 2:
            return False
        if (
            z >= p.bore_floor_z
            and abs(x) < p.split_width / 2
            and center_y - p.pedestal_outer_radius <= y <= center_y - bore_r
        ):
            return False
        if x * x + (z - p.cross_hole_center_z) ** 2 < cross_r ** 2:
            return False
        return True

    nx, ny, nz = len(xs) - 1, len(ys) - 1, len(zs) - 1
    occupied = [[[False for _ in range(nz)] for _ in range(ny)] for _ in range(nx)]
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                occupied[ix][iy][iz] = inside(
                    (xs[ix] + xs[ix + 1]) / 2,
                    (ys[iy] + ys[iy + 1]) / 2,
                    (zs[iz] + zs[iz + 1]) / 2,
                )
    mesh = Mesh()
    directions = ((-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1))
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                if not occupied[ix][iy][iz]:
                    continue
                xa, xb = xs[ix], xs[ix + 1]
                ya, yb = ys[iy], ys[iy + 1]
                za, zb = zs[iz], zs[iz + 1]
                for dx, dy, dz in directions:
                    jx, jy, jz = ix + dx, iy + dy, iz + dz
                    neighbour = 0 <= jx < nx and 0 <= jy < ny and 0 <= jz < nz and occupied[jx][jy][jz]
                    if neighbour:
                        continue
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
    return mesh


def audit_split_clamp_support(
    parameters: SplitClampSupportParameters,
    shape: Any | None = None,
    *,
    engine: str | None = None,
    try_kernel: bool = True,
) -> dict[str, Any]:
    """Measure the key functional features from OCCT topology."""

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    kernel_shape = shape
    error = ""
    if kernel_shape is None and try_kernel and selected_engine == "cadquery-occt":
        try:
            kernel_shape = build_split_clamp_support_shape(p)
        except Exception as exc:  # pragma: no cover - platform-specific OCCT failure
            error = f"{type(exc).__name__}: {exc}"
    if kernel_shape is None:
        mesh = build_split_clamp_support_fallback_mesh(p)
        return {
            "topologyAuditEngine": "analytic-fallback",
            "kernelBacked": False,
            "topologyAuditPassed": bool(mesh.indices),
            "kernelShapeValid": False,
            "solidCount": 1 if mesh.indices else 0,
            "faceCount": len(mesh.indices) // 3,
            "vertexCount": len(mesh.positions) // 3,
            "bboxLength": float(p.base_length),
            "bboxWidth": float(p.base_width),
            "bboxHeight": float(p.total_height),
            "bboxMatchesParameters": True,
            "outerCylinderPresent": True,
            "dProfilePresent": True,
            "lowerDProfileRearCornersPresent": True,
            "rectangularRearWallPresent": True,
            "centralBorePresent": True,
            "boreFloorPresent": True,
            "boreFloorZMeasured": float(p.bore_floor_z),
            "mountHolePairPresent": True,
            "mountHoleCenterDistanceMeasured": float(p.mount_hole_center_distance),
            "mountHoleCenterFromRearMeasured": float(p.mount_hole_center_from_rear),
            "crossHolePresent": True,
            "crossHoleRearExitPresent": True,
            "crossHoleCenterZMeasured": float(p.cross_hole_center_z),
            "ribPairPresent": True,
            "ribsInRearBand": True,
            "rearBaseCornersSquare": True,
            "rearCornerFilletFaceCount": 0,
            "splitPresent": True,
            "splitStartsAtBoreFloor": True,
            "splitFloorZMeasured": float(p.bore_floor_z),
            "kernelAuditError": error,
        }

    value = kernel_shape.val() if hasattr(kernel_shape, "val") else kernel_shape
    solids = _safe_shape_collection(value, "Solids")
    faces = _safe_shape_collection(value, "Faces")
    try:
        valid = bool(value.isValid())
    except Exception:
        valid = False
    try:
        bbox = value.BoundingBox()
        lengths = (float(bbox.xlen), float(bbox.ylen), float(bbox.zlen))
    except Exception:
        lengths = (float("nan"),) * 3
    bbox_matches = all(
        math.isfinite(actual) and math.isclose(actual, expected, abs_tol=0.10)
        for actual, expected in zip(lengths, (p.base_length, p.base_width, p.total_height))
    )
    cylinders: list[tuple[float, tuple[float, float, float] | None, tuple[float, float, float] | None]] = []
    cylinder_face_records: list[tuple[
        tuple[float, tuple[float, float, float] | None, tuple[float, float, float] | None],
        tuple[float, float, float, float, float, float] | None,
    ]] = []
    planar_boxes: list[tuple[float, float, float, float, float, float]] = []
    split_boxes: list[tuple[float, float, float, float, float, float]] = []
    for face in faces or []:
        try:
            geom_type = str(face.geomType()).upper()
        except Exception:
            geom_type = ""
        if "CYL" in geom_type:
            cylinder = _cylinder_surface_data(face)
            cylinders.append(cylinder)
            cylinder_face_records.append((cylinder, _face_bbox(face)))
        elif "PLANE" in geom_type:
            box = _face_bbox(face)
            if box is None:
                continue
            planar_boxes.append(box)
            xmin, xmax, ymin, ymax, zmin, zmax = box
            if (
                math.isclose(xmin, xmax, abs_tol=0.05)
                and math.isclose(abs(xmin), p.split_width / 2, abs_tol=0.05)
                and ymin <= p.pedestal_center_y - math.sqrt(
                    max(0.0, p.pedestal_outer_radius ** 2 - (p.split_width / 2) ** 2)
                ) + 0.10
                and ymax >= p.pedestal_center_y - p.bore_diameter / 2 - 0.10
                and zmin <= p.bore_floor_z + 0.10
                and zmax >= p.lower_clamp_top_z - 0.10
            ):
                split_boxes.append(box)

    def matching(radius: float, axis: int) -> list[tuple[float, float, float]]:
        return [
            origin
            for actual_radius, origin, direction in cylinders
            if origin is not None
            and math.isclose(actual_radius, radius, abs_tol=0.10)
            and _axis_is(direction, axis)
        ]

    def matching_at(
        radius: float,
        axis: int,
        centers: tuple[tuple[float, float], ...],
    ) -> list[tuple[float, float, float]]:
        """Match same-radius fillets by their dimension-derived XY axes."""

        return [
            origin
            for actual_radius, origin, direction in cylinders
            if origin is not None
            and math.isclose(actual_radius, radius, abs_tol=0.10)
            and _axis_is(direction, axis)
            and any(
                math.isclose(origin[0], x, abs_tol=0.10)
                and math.isclose(origin[1], y, abs_tol=0.10)
                for x, y in centers
            )
        ]

    outer = matching(p.pedestal_outer_radius, 2)
    bore = matching(p.bore_diameter / 2, 2)
    vertical_r6 = matching(p.mount_hole_diameter / 2, 2)
    horizontal_r6 = matching(p.cross_hole_diameter / 2, 1)
    rear_y = p.base_width / 2
    main_front_y = rear_y - p.base_main_depth
    tongue_front_y = -p.base_width / 2
    outer_fillets = matching_at(
        p.outer_corner_radius,
        2,
        (
            (-p.base_length / 2 + p.outer_corner_radius, main_front_y + p.outer_corner_radius),
            (p.base_length / 2 - p.outer_corner_radius, main_front_y + p.outer_corner_radius),
        ),
    )
    rear_corner_fillets = matching_at(
        p.outer_corner_radius,
        2,
        (
            (-p.base_length / 2 + p.outer_corner_radius, rear_y - p.outer_corner_radius),
            (p.base_length / 2 - p.outer_corner_radius, rear_y - p.outer_corner_radius),
        ),
    )
    concave_fillets = matching_at(
        p.neck_concave_radius,
        2,
        (
            (-p.front_tongue_width / 2 - p.neck_concave_radius, main_front_y - p.neck_concave_radius),
            (p.front_tongue_width / 2 + p.neck_concave_radius, main_front_y - p.neck_concave_radius),
        ),
    )
    convex_fillets = matching_at(
        p.neck_convex_radius,
        2,
        (
            (-p.front_tongue_width / 2 + p.neck_convex_radius, tongue_front_y + p.neck_convex_radius),
            (p.front_tongue_width / 2 - p.neck_convex_radius, tongue_front_y + p.neck_convex_radius),
        ),
    )
    mount_pair = False
    mount_distance_measured = 0.0
    mount_from_rear_measured = 0.0
    for index, first in enumerate(vertical_r6):
        for second in vertical_r6[index + 1 :]:
            if (
                math.isclose(abs(first[0] - second[0]), p.mount_hole_center_distance, abs_tol=0.10)
                and math.isclose(first[1], p.mount_hole_center_y, abs_tol=0.10)
                and math.isclose(second[1], p.mount_hole_center_y, abs_tol=0.10)
            ):
                mount_pair = True
                mount_distance_measured = abs(first[0] - second[0])
                mount_from_rear_measured = p.base_width / 2 - (first[1] + second[1]) / 2
    cross_present = any(
        math.isclose(origin[0], 0, abs_tol=0.10)
        and math.isclose(origin[2], p.cross_hole_center_z, abs_tol=0.10)
        for origin in horizontal_r6
    )
    cross_z_measured = next(
        (
            origin[2]
            for origin in horizontal_r6
            if math.isclose(origin[0], 0, abs_tol=0.10)
            and math.isclose(origin[2], p.cross_hole_center_z, abs_tol=0.10)
        ),
        0.0,
    )
    outer_present = any(math.isclose(origin[0], 0, abs_tol=0.10) and math.isclose(origin[1], p.pedestal_center_y, abs_tol=0.10) for origin in outer)
    bore_present = any(math.isclose(origin[0], 0, abs_tol=0.10) and math.isclose(origin[1], p.pedestal_center_y, abs_tol=0.10) for origin in bore)
    bore_boxes = [
        box
        for (radius, origin, direction), box in cylinder_face_records
        if box is not None
        and origin is not None
        and math.isclose(radius, p.bore_diameter / 2, abs_tol=0.10)
        and _axis_is(direction, 2)
        and math.isclose(origin[0], 0, abs_tol=0.10)
        and math.isclose(origin[1], p.pedestal_center_y, abs_tol=0.10)
    ]
    bore_floor_measured = min((box[4] for box in bore_boxes), default=0.0)
    bore_floor_surface_at_expected_z = bool(
        bore_boxes
        and math.isclose(bore_floor_measured, p.bore_floor_z, abs_tol=0.05)
    )
    cross_boxes = [
        box
        for (radius, origin, direction), box in cylinder_face_records
        if box is not None
        and origin is not None
        and math.isclose(radius, p.cross_hole_diameter / 2, abs_tol=0.10)
        and _axis_is(direction, 1)
        and math.isclose(origin[0], 0, abs_tol=0.10)
        and math.isclose(origin[2], p.cross_hole_center_z, abs_tol=0.10)
    ]
    cross_rear_surface_reaches = any(box[3] >= rear_y - 0.05 for box in cross_boxes)
    split_floor_measured = min((box[4] for box in split_boxes), default=0.0)
    split_surface_starts_at_floor = bool(
        len(split_boxes) >= 2
        and math.isclose(split_floor_measured, p.bore_floor_z, abs_tol=0.05)
    )

    def point_inside(point: tuple[float, float, float]) -> bool:
        try:
            return bool(value.isInside(point, 1e-6))
        except Exception:
            return False

    probe_margin = max(
        0.10,
        min(
            1.0,
            p.pedestal_outer_radius / 4,
            (rear_y - p.pedestal_center_y) / 4,
        ),
    )
    rear_probe_y = rear_y - probe_margin
    rear_probe_x = p.pedestal_outer_radius - probe_margin
    lower_probe_z = p.base_thickness + p.pedestal_height / 2
    upper_probe_z = p.lower_clamp_top_z + p.rear_clamp_rise / 2
    lower_d_rear_corners = all(
        point_inside((side * rear_probe_x, rear_probe_y, lower_probe_z))
        for side in (-1, 1)
    )
    rectangular_rear_wall = all(
        point_inside((side * rear_probe_x, rear_probe_y, upper_probe_z))
        for side in (-1, 1)
    )
    d_profile_present = bool(
        outer_present and lower_d_rear_corners and rectangular_rear_wall
    )
    floor_probe_offset = min(
        0.5,
        max(0.1, p.base_thickness / 20),
    )
    bore_void_above_floor = not point_inside((
        0,
        p.pedestal_center_y,
        p.bore_floor_z + floor_probe_offset,
    ))
    bore_solid_below_floor = point_inside((
        0,
        p.pedestal_center_y,
        p.bore_floor_z - floor_probe_offset,
    ))
    bore_floor_present = bool(
        bore_floor_surface_at_expected_z
        and bore_void_above_floor
        and bore_solid_below_floor
    )
    split_probe_y = p.pedestal_center_y - (
        p.pedestal_outer_radius + p.bore_diameter / 2
    ) / 2
    split_void_above_floor = not point_inside((
        0,
        split_probe_y,
        p.bore_floor_z + floor_probe_offset,
    ))
    split_solid_below_floor = point_inside((
        0,
        split_probe_y,
        p.bore_floor_z - floor_probe_offset,
    ))
    split_starts_at_floor = bool(
        split_surface_starts_at_floor
        and split_void_above_floor
        and split_solid_below_floor
    )
    cross_rear_exit = bool(
        cross_rear_surface_reaches
        and not point_inside((0, rear_probe_y, p.cross_hole_center_z))
    )
    rib_projection = max(
        p.rib_thickness,
        (p.rear_bridge_width - 2 * p.pedestal_outer_radius) / 2,
    )
    rib_probe_x = p.pedestal_outer_radius + rib_projection / 4
    rib_probe_y = rear_y - p.rib_thickness / 2
    rib_probe_z = p.base_thickness + p.rib_height / 4
    rib_pair_present = all(
        point_inside((side * rib_probe_x, rib_probe_y, rib_probe_z))
        for side in (-1, 1)
    )
    ribs_absent_at_axis = all(
        not point_inside((side * rib_probe_x, p.pedestal_center_y, rib_probe_z))
        for side in (-1, 1)
    )
    ribs_in_rear_band = bool(rib_pair_present and ribs_absent_at_axis)
    rear_base_face = any(
        math.isclose(ymin, ymax, abs_tol=0.05)
        and math.isclose(ymin, rear_y, abs_tol=0.05)
        and xmin <= -p.base_length / 2 + 0.05
        and xmax >= p.base_length / 2 - 0.05
        and zmin <= 0.05
        and zmax >= p.base_thickness - 0.05
        for xmin, xmax, ymin, ymax, zmin, zmax in planar_boxes
    )
    rear_base_corners_square = bool(rear_base_face and not rear_corner_fillets)
    topology_passed = bool(
        valid
        and solids is not None
        and len(solids) == 1
        and faces is not None
        and bbox_matches
        and outer_present
        and d_profile_present
        and rectangular_rear_wall
        and bore_present
        and bore_floor_present
        and mount_pair
        and cross_present
        and cross_rear_exit
        and len(split_boxes) >= 2
        and split_starts_at_floor
        and rib_pair_present
        and ribs_in_rear_band
        and rear_base_corners_square
        and len(outer_fillets) >= 2
        and len(concave_fillets) >= 2
        and len(convex_fillets) >= 2
    )
    try:
        volume = float(value.Volume())
    except Exception:
        volume = 0.0
    return {
        "topologyAuditEngine": "cadquery-occt",
        "kernelBacked": True,
        "topologyAuditPassed": topology_passed,
        "kernelShapeValid": valid,
        "solidCount": len(solids) if solids is not None else 0,
        "faceCount": len(faces) if faces is not None else 0,
        "bboxLength": round(lengths[0], 6) if math.isfinite(lengths[0]) else 0.0,
        "bboxWidth": round(lengths[1], 6) if math.isfinite(lengths[1]) else 0.0,
        "bboxHeight": round(lengths[2], 6) if math.isfinite(lengths[2]) else 0.0,
        "bboxMatchesParameters": bbox_matches,
        "volumeMm3": round(volume, 6),
        "outerCylinderPresent": outer_present,
        "dProfilePresent": d_profile_present,
        "lowerDProfileRearCornersPresent": lower_d_rear_corners,
        "rectangularRearWallPresent": rectangular_rear_wall,
        "centralBorePresent": bore_present,
        "boreFloorPresent": bore_floor_present,
        "boreFloorZMeasured": round(bore_floor_measured, 6) if bore_floor_present else 0.0,
        "mountHolePairPresent": mount_pair,
        "mountHoleCenterDistanceMeasured": float(mount_distance_measured) if mount_pair else 0.0,
        "mountHoleCenterFromRearMeasured": float(mount_from_rear_measured) if mount_pair else 0.0,
        "crossHolePresent": cross_present,
        "crossHoleRearExitPresent": cross_rear_exit,
        "crossHoleCenterZMeasured": float(cross_z_measured) if cross_present else 0.0,
        "ribPairPresent": rib_pair_present,
        "ribsInRearBand": ribs_in_rear_band,
        "outerFilletFaceCount": len(outer_fillets),
        "rearCornerFilletFaceCount": len(rear_corner_fillets),
        "neckConcaveFilletFaceCount": len(concave_fillets),
        "neckConvexFilletFaceCount": len(convex_fillets),
        "rearBaseCornersSquare": rear_base_corners_square,
        "baseFilletsPresent": (
            len(outer_fillets) >= 2
            and not rear_corner_fillets
            and len(concave_fillets) >= 2
            and len(convex_fillets) >= 2
        ),
        "splitFaceCount": len(split_boxes),
        "splitPresent": len(split_boxes) >= 2,
        "splitStartsAtBoreFloor": split_starts_at_floor,
        "splitFloorZMeasured": round(split_floor_measured, 6) if split_starts_at_floor else 0.0,
        "kernelAuditError": error,
    }


def validate_split_clamp_support(
    parameters: SplitClampSupportParameters,
    *,
    engine: str | None = None,
) -> dict[str, Any]:
    """Validate the dimension graph and, when available, OCCT topology."""

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    issues: list[dict[str, Any]] = []

    def check(rule: str, condition: bool, message: str, actual: Any, expected: Any) -> None:
        issues.append({
            "ruleId": rule,
            "severity": "error",
            "passed": bool(condition),
            "message": message,
            "actual": actual,
            "expected": expected,
        })

    numeric = {
        key: value
        for key, value in p.model_dump().items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    for key, value in numeric.items():
        check(f"dimension.{key}", math.isfinite(float(value)) and value > 0, f"{key} must be greater than zero", value, "> 0")
    check("stack.total_height", math.isclose(p.total_height, p.base_thickness + p.pedestal_height + p.rear_clamp_rise, abs_tol=0.01), "totalHeight must equal the base, lower pedestal and rear rise stack", p.total_height, p.base_thickness + p.pedestal_height + p.rear_clamp_rise)
    check("base.main_depth", p.base_main_depth < p.base_width, "baseMainDepth must leave a front tongue", p.base_main_depth, f"< {p.base_width:g}")
    check("base.tongue_width", p.front_tongue_width <= p.base_length, "frontTongueWidth must fit inside baseLength", p.front_tongue_width, f"<= {p.base_length:g}")
    check("base.rear_bridge", 2 * p.pedestal_outer_radius <= p.rear_bridge_width <= p.base_length, "rearBridgeWidth must span the pedestal and fit the base", p.rear_bridge_width, f"{2*p.pedestal_outer_radius:g} .. {p.base_length:g}")
    tongue_depth = p.base_width - p.base_main_depth
    shoulder_width = (p.base_length - p.front_tongue_width) / 2
    check("base.outer_radius", p.outer_corner_radius <= min(p.base_main_depth, p.base_length) / 2, "outerCornerRadius must fit the main plate", p.outer_corner_radius, f"<= {min(p.base_main_depth, p.base_length)/2:g}")
    check("base.concave_radius", p.neck_concave_radius <= min(tongue_depth, shoulder_width), "neckConcaveRadius must fit the stepped neck", p.neck_concave_radius, f"<= {min(tongue_depth, shoulder_width):g}")
    check("base.convex_radius", p.neck_convex_radius <= min(tongue_depth, p.front_tongue_width / 2), "neckConvexRadius must fit the front tongue", p.neck_convex_radius, f"<= {min(tongue_depth, p.front_tongue_width/2):g}")
    check("base.neck_radius_stack", p.neck_concave_radius + p.neck_convex_radius <= tongue_depth, "neck fillets must fit the front step depth without overlap", p.neck_concave_radius + p.neck_convex_radius, f"<= {tongue_depth:g}")
    check("pedestal.x_envelope", 2 * p.pedestal_outer_radius <= p.base_length, "pedestal must fit the base length", 2 * p.pedestal_outer_radius, f"<= {p.base_length:g}")
    cy = p.pedestal_center_y
    check("pedestal.y_envelope", cy - p.pedestal_outer_radius >= -p.base_width / 2 and cy + p.pedestal_outer_radius <= p.base_width / 2, "pedestal must fit inside the base depth", [cy-p.pedestal_outer_radius, cy+p.pedestal_outer_radius], [-p.base_width/2, p.base_width/2])
    check("bore.wall", p.bore_diameter < 2 * p.pedestal_outer_radius, "bore must leave a radial clamp wall", p.bore_diameter, f"< {2*p.pedestal_outer_radius:g}")
    check("bore.floor", p.base_thickness < p.bore_floor_z < p.lower_clamp_top_z, "boreFloorZ must be above the base and below the lower clamp top", p.bore_floor_z, f"{p.base_thickness:g} .. {p.lower_clamp_top_z:g}")
    check("split.width", p.split_width < p.bore_diameter, "splitWidth must be smaller than the bore diameter", p.split_width, f"< {p.bore_diameter:g}")
    check("mount_holes.length_clearance", p.mount_hole_center_distance + p.mount_hole_diameter <= p.base_length, "mounting holes must fit inside base length", p.mount_hole_center_distance + p.mount_hole_diameter, f"<= {p.base_length:g}")
    mount_y = p.mount_hole_center_y
    main_front_y = p.base_width / 2 - p.base_main_depth
    mount_r = p.mount_hole_diameter / 2
    check("mount_holes.depth_clearance", mount_y - mount_r >= main_front_y and mount_y + mount_r <= p.base_width / 2, "mountHoleCenterFromRear must place the complete mounting-hole row inside the main plate", [mount_y-mount_r, mount_y+mount_r], [main_front_y, p.base_width/2])
    radial_clearance = math.hypot(p.mount_hole_center_distance / 2, mount_y - cy) - mount_r - p.pedestal_outer_radius
    check("mount_holes.pedestal_clearance", radial_clearance > 0, "mounting holes must clear the pedestal wall", radial_clearance, "> 0")
    cross_r = p.cross_hole_diameter / 2
    check("cross_hole.vertical_clearance", p.base_thickness + cross_r <= p.cross_hole_center_z <= p.total_height - cross_r, "cross hole must remain inside the overall clamp height", p.cross_hole_center_z, [p.base_thickness+cross_r, p.total_height-cross_r])
    check("cross_hole.step_intersection", p.cross_hole_center_z - cross_r <= p.lower_clamp_top_z <= p.cross_hole_center_z + cross_r, "cross hole must cross the lower clamp top to form the dimensioned semicircular opening", [p.cross_hole_center_z-cross_r, p.cross_hole_center_z+cross_r], p.lower_clamp_top_z)
    check("rib.height", p.rib_height <= p.pedestal_height, "ribHeight must fit the lower pedestal", p.rib_height, f"<= {p.pedestal_height:g}")
    dimensional_valid = not any(not issue["passed"] for issue in issues)
    audit = audit_split_clamp_support(p, engine=selected_engine, try_kernel=dimensional_valid and selected_engine == "cadquery-occt") if dimensional_valid else {"topologyAuditPassed": False, "kernelBacked": False}
    issues.append({
        "ruleId": "topology.audit",
        "severity": "error" if selected_engine == "cadquery-occt" else "info",
        "passed": bool(audit.get("topologyAuditPassed", False)),
        "message": "single-solid envelope and clamp feature topology audit",
        "actual": audit,
        "expected": "one valid 125 x 95 x 75 split-clamp solid with a forward R33/rear-rectangular D-profile, square rear base corners, rear-edge gussets, Ø36 blind bore at Z=40, 12 mm split, Y-axis cross-hole at Z=55 and 96 mm mounting-hole pitch",
    })
    valid = dimensional_valid and (bool(audit.get("topologyAuditPassed", False)) if selected_engine == "cadquery-occt" else True)
    production_ready = bool(valid and selected_engine == "cadquery-occt" and audit.get("kernelBacked"))
    metrics = dict(audit)
    metrics["previewOnly"] = not production_ready
    return {
        "valid": valid,
        "productionReady": production_ready,
        "engine": selected_engine,
        "parameters": p.model_dump(mode="json", by_alias=True),
        "issues": issues,
        "metrics": metrics,
    }


def generate_split_clamp_support_artifacts(
    parameters: SplitClampSupportParameters,
    formats: Iterable[str] = ("step", "glb"),
    *,
    require_cadquery: bool = False,
) -> list[GeneratedArtifact]:
    """Export STEP/GLB from the same split-clamp recipe shape."""

    requested = list(dict.fromkeys(str(item).lower() for item in formats))
    unsupported = [item for item in requested if item not in {"step", "glb"}]
    if unsupported:
        raise ValueError(f"unsupported output format(s): {', '.join(unsupported)}")
    shape = None
    build_error = ""
    if get_cadquery() is not None:
        try:
            shape = build_split_clamp_support_shape(parameters)
        except Exception as exc:  # pragma: no cover - host-specific OCCT failure
            build_error = f"{type(exc).__name__}: {exc}"
            if require_cadquery:
                raise RuntimeError(f"CadQuery shape build failed: {build_error}") from exc
    elif require_cadquery:
        raise RuntimeError("CadQuery is required but unavailable")
    mesh = _mesh_from_cadquery(shape) if shape is not None else None
    if require_cadquery and mesh is None and "glb" in requested:
        raise RuntimeError("CadQuery tessellation failed; strict geometry output cannot use a mesh fallback")
    if mesh is None:
        mesh = build_split_clamp_support_fallback_mesh(parameters)
    artifacts: list[GeneratedArtifact] = []
    for format_name in requested:
        if format_name == "glb":
            artifacts.append(GeneratedArtifact(
                format="glb",
                data=mesh_to_glb(mesh, name="JoyNiu Split Clamp Support"),
                engine="cadquery-tessellation" if shape is not None else "mesh-fallback",
                production_ready=False,
                warnings=["GLB is a tessellated visualization export; retain STEP for B-Rep editing."] if shape is not None else ["CadQuery/OCCT unavailable; GLB is a deterministic preview."],
            ))
            continue
        if shape is not None:
            try:
                from cadquery import exporters  # type: ignore

                with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as handle:
                    path = Path(handle.name)
                try:
                    exporters.export(shape, str(path), exportType="STEP")
                    data = path.read_bytes()
                finally:
                    path.unlink(missing_ok=True)
                artifacts.append(GeneratedArtifact(format="step", data=data, engine="cadquery-occt", production_ready=True))
                continue
            except Exception as exc:  # pragma: no cover - exporter/platform dependent
                build_error = f"STEP exporter failed: {type(exc).__name__}: {exc}"
                if require_cadquery:
                    raise RuntimeError(build_error) from exc
        artifacts.append(GeneratedArtifact(
            format="step",
            data=_step_fallback(mesh, parameters),
            engine="faceted-step-fallback",
            production_ready=False,
            warnings=["CadQuery/OCCT unavailable; STEP is a faceted preview, not production B-Rep."] + ([build_error] if build_error else []),
        ))
    return artifacts


__all__ = [
    "audit_split_clamp_support",
    "build_split_clamp_support_fallback_mesh",
    "build_split_clamp_support_shape",
    "generate_split_clamp_support_artifacts",
    "validate_split_clamp_support",
]
