"""CadQuery recipe for the two-solid stepped tapered nozzle from DWG 1(1).

The drawing contains two independent axial sections: a 98 mm nozzle body and
a 40 mm removable M12 insert.  They share an assembly datum at ``x=0`` but are
never fused.  The M12 designation is represented by a nominal straight bore;
pitch, tolerance class and helical tooth geometry are intentionally outside
this recipe and are called out in every artifact audit.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import tempfile
from typing import Any, Iterable, Sequence

from .geometry import (
    GeneratedArtifact,
    Mesh,
    _cylinder_surface_data,
    _face_bbox,
    _mesh_from_cadquery,
    _safe_shape_collection,
    _step_fallback,
    cadquery_status,
    get_cadquery,
)
from .schemas import SteppedTaperedNozzleParameters


_OVERLAP = 0.01


def _workplane_for(cq: Any, shape: Any) -> Any:
    return cq.Workplane("XY").newObject([shape])


def build_stepped_tapered_nozzle_components(
    parameters: SteppedTaperedNozzleParameters,
) -> tuple[Any, Any]:
    """Return the main body and insert as two independent OCCT solids."""

    cq = get_cadquery()
    if cq is None:
        raise RuntimeError("CadQuery unavailable")
    p = parameters
    if p.tip_length <= 0 or not math.isfinite(p.outlet_taper_length):
        raise ValueError("invalid axial stack or outlet taper")

    head_left_radius = p.head_left_diameter / 2
    head_right_radius = p.head_right_diameter / 2
    neck_radius = p.neck_diameter / 2
    tip_radius = p.tip_diameter / 2
    neck_end = p.head_length + p.neck_length

    # Revolving one connected half-section avoids coincident-solid unions at
    # the Ø56→Ø30 and Ø30→Ø25 shoulders.
    outer_profile = (
        cq.Workplane("XY")
        .moveTo(0, 0)
        .lineTo(0, head_left_radius)
        .lineTo(p.head_length, head_right_radius)
        .lineTo(p.head_length, neck_radius)
        .lineTo(neck_end, neck_radius)
        .lineTo(neck_end, tip_radius)
        .lineTo(p.main_length, tip_radius)
        .lineTo(p.main_length, 0)
        .close()
        .revolve(360, (0, 0), (1, 0))
    )

    counterbore = cq.Solid.makeCylinder(
        p.counterbore_diameter / 2,
        p.counterbore_depth + _OVERLAP,
        cq.Vector(-_OVERLAP, 0, 0),
        cq.Vector(1, 0, 0),
    )
    axial_bore = cq.Solid.makeCylinder(
        p.axial_bore_diameter / 2,
        p.main_length + 2 * _OVERLAP,
        cq.Vector(-_OVERLAP, 0, 0),
        cq.Vector(1, 0, 0),
    )
    outlet_taper = cq.Solid.makeCone(
        p.axial_bore_diameter / 2,
        p.outlet_diameter / 2,
        p.outlet_taper_length,
        cq.Vector(p.outlet_taper_start_x, 0, 0),
        cq.Vector(1, 0, 0),
    )
    main = outer_profile.cut(_workplane_for(cq, counterbore))
    main = main.cut(_workplane_for(cq, axial_bore))
    main = main.cut(_workplane_for(cq, outlet_taper)).clean()

    insert_outer = cq.Solid.makeCylinder(
        p.insert_outer_diameter / 2,
        p.insert_length,
        cq.Vector(p.insert_axial_offset, 0, 0),
        cq.Vector(1, 0, 0),
    )
    # Cosmetic/simplified representation only: M12 pitch and tolerance class
    # are not present in the source drawing, so no helical tooth is invented.
    insert_hole = cq.Solid.makeCylinder(
        p.insert_thread_nominal_diameter / 2,
        p.insert_length + 2 * _OVERLAP,
        cq.Vector(p.insert_axial_offset - _OVERLAP, 0, 0),
        cq.Vector(1, 0, 0),
    )
    insert = _workplane_for(cq, insert_outer).cut(_workplane_for(cq, insert_hole)).clean()
    return main, insert


def build_stepped_tapered_nozzle_shape(parameters: SteppedTaperedNozzleParameters) -> Any:
    """Return one compound containing exactly two unfused solids."""

    cq = get_cadquery()
    if cq is None:
        raise RuntimeError("CadQuery unavailable")
    main, insert = build_stepped_tapered_nozzle_components(parameters)
    compound = cq.Compound.makeCompound([main.val(), insert.val()])
    return _workplane_for(cq, compound)


def _ring(radius: float, x: float, segments: int) -> list[tuple[float, float, float]]:
    return [
        (
            x,
            radius * math.cos(2 * math.pi * index / segments),
            radius * math.sin(2 * math.pi * index / segments),
        )
        for index in range(segments)
    ]


def _add_revolved_surface(
    mesh: Mesh,
    profile: Sequence[tuple[float, float]],
    *,
    inward: bool = False,
    segments: int = 72,
) -> None:
    rings = [_ring(radius, x, segments) for x, radius in profile]
    for first, second in zip(rings, rings[1:]):
        for index in range(segments):
            following = (index + 1) % segments
            if inward:
                mesh.add_quad(first[index], second[index], second[following], first[following])
            else:
                mesh.add_quad(first[index], first[following], second[following], second[index])


def _add_annulus(
    mesh: Mesh,
    x: float,
    inner_radius: float,
    outer_radius: float,
    *,
    reverse: bool,
    segments: int = 72,
) -> None:
    inner = _ring(inner_radius, x, segments)
    outer = _ring(outer_radius, x, segments)
    for index in range(segments):
        following = (index + 1) % segments
        if reverse:
            mesh.add_quad(inner[index], inner[following], outer[following], outer[index])
        else:
            mesh.add_quad(inner[index], outer[index], outer[following], inner[following])


def build_stepped_tapered_nozzle_fallback_meshes(
    parameters: SteppedTaperedNozzleParameters,
) -> tuple[Mesh, Mesh]:
    """Build two disconnected, deterministic lathed preview meshes."""

    p = parameters
    neck_end = p.head_length + p.neck_length
    outer = (
        (0.0, p.head_left_diameter / 2),
        (p.head_length, p.head_right_diameter / 2),
        (p.head_length, p.neck_diameter / 2),
        (neck_end, p.neck_diameter / 2),
        (neck_end, p.tip_diameter / 2),
        (p.main_length, p.tip_diameter / 2),
    )
    inner = (
        (0.0, p.counterbore_diameter / 2),
        (p.counterbore_depth, p.counterbore_diameter / 2),
        (p.counterbore_depth, p.axial_bore_diameter / 2),
        (p.outlet_taper_start_x, p.axial_bore_diameter / 2),
        (p.main_length, p.outlet_diameter / 2),
    )
    main = Mesh()
    _add_revolved_surface(main, outer)
    _add_revolved_surface(main, inner, inward=True)
    _add_annulus(main, 0, p.counterbore_diameter / 2, p.head_left_diameter / 2, reverse=True)
    _add_annulus(main, p.main_length, p.outlet_diameter / 2, p.tip_diameter / 2, reverse=False)

    insert = Mesh()
    insert_outer_radius = p.insert_outer_diameter / 2
    insert_inner_radius = p.insert_thread_nominal_diameter / 2
    start, end = p.insert_axial_offset, p.insert_axial_offset + p.insert_length
    _add_revolved_surface(insert, ((start, insert_outer_radius), (end, insert_outer_radius)))
    _add_revolved_surface(insert, ((start, insert_inner_radius), (end, insert_inner_radius)), inward=True)
    _add_annulus(insert, start, insert_inner_radius, insert_outer_radius, reverse=True)
    _add_annulus(insert, end, insert_inner_radius, insert_outer_radius, reverse=False)
    return main, insert


def _merge_meshes(meshes: Sequence[Mesh]) -> Mesh:
    merged = Mesh()
    for mesh in meshes:
        for offset in range(0, len(mesh.indices), 3):
            points: list[tuple[float, float, float]] = []
            for index in mesh.indices[offset : offset + 3]:
                start = index * 3
                points.append(tuple(mesh.positions[start : start + 3]))
            if len(points) == 3:
                merged.add_triangle(points[0], points[1], points[2])
    return merged


def _pad4(buffer: bytearray) -> None:
    buffer.extend(b"\x00" * ((4 - len(buffer) % 4) % 4))


def _two_component_glb(main: Mesh, insert: Mesh) -> bytes:
    """Encode two named GLB meshes/nodes rather than one fused visual mesh."""

    binary = bytearray()
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    mesh_records: list[dict[str, Any]] = []
    names = ("Nozzle main body", "M12 insert (simplified bore)")
    for component_index, (name, mesh) in enumerate(zip(names, (main, insert))):
        if not mesh.indices:
            raise ValueError("cannot encode an empty nozzle component mesh")
        component_accessors: list[int] = []
        payloads = (
            (struct.pack(f"<{len(mesh.positions)}f", *mesh.positions), 34962),
            (struct.pack(f"<{len(mesh.normals)}f", *mesh.normals), 34962),
            (struct.pack(f"<{len(mesh.indices)}I", *mesh.indices), 34963),
        )
        for payload, target in payloads:
            _pad4(binary)
            offset = len(binary)
            binary.extend(payload)
            buffer_views.append(
                {"buffer": 0, "byteOffset": offset, "byteLength": len(payload), "target": target}
            )
            component_accessors.append(len(accessors))
            if target == 34963:
                accessors.append(
                    {
                        "bufferView": len(buffer_views) - 1,
                        "componentType": 5125,
                        "count": len(mesh.indices),
                        "type": "SCALAR",
                        "min": [0],
                        "max": [len(mesh.positions) // 3 - 1],
                    }
                )
            else:
                positions = [mesh.positions[index : index + 3] for index in range(0, len(mesh.positions), 3)]
                record: dict[str, Any] = {
                    "bufferView": len(buffer_views) - 1,
                    "componentType": 5126,
                    "count": len(mesh.positions) // 3,
                    "type": "VEC3",
                }
                if len(component_accessors) == 1:
                    record["min"] = [min(point[axis] for point in positions) for axis in range(3)]
                    record["max"] = [max(point[axis] for point in positions) for axis in range(3)]
                accessors.append(record)
        mesh_records.append(
            {
                "name": name,
                "primitives": [
                    {
                        "attributes": {
                            "POSITION": component_accessors[0],
                            "NORMAL": component_accessors[1],
                        },
                        "indices": component_accessors[2],
                        "material": component_index,
                        "mode": 4,
                    }
                ],
            }
        )

    gltf = {
        "asset": {"version": "2.0", "generator": "JoyNiu NewCAD API"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [{"name": name, "mesh": index} for index, name in enumerate(names)],
        "meshes": mesh_records,
        "materials": [
            {
                "name": "45# steel main",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.34, 0.44, 0.58, 1.0],
                    "metallicFactor": 0.72,
                    "roughnessFactor": 0.3,
                },
                "doubleSided": True,
            },
            {
                "name": "M12 insert candidate",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.68, 0.50, 0.22, 1.0],
                    "metallicFactor": 0.75,
                    "roughnessFactor": 0.28,
                },
                "doubleSided": True,
            },
        ],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
        "extras": {
            "componentMode": "two_solid_assembly_candidate",
            "threadRepresentation": "M12 nominal straight bore; no helical thread geometry",
        },
    }
    json_chunk = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    _pad4(binary)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return b"".join(
        (
            struct.pack("<4sII", b"glTF", 2, total),
            struct.pack("<I4s", len(json_chunk), b"JSON"),
            json_chunk,
            struct.pack("<I4s", len(binary), b"BIN\x00"),
            bytes(binary),
        )
    )


def _axis_is_x(direction: tuple[float, float, float] | None) -> bool:
    return bool(
        direction is not None
        and abs(direction[0]) >= 0.9
        and abs(direction[0]) >= max(abs(direction[1]), abs(direction[2]))
    )


def _analytic_audit(parameters: SteppedTaperedNozzleParameters) -> dict[str, Any]:
    p = parameters
    return {
        "topologyAuditEngine": "analytic-fallback",
        "kernelBacked": False,
        "topologyAuditPassed": True,
        "kernelShapeValid": False,
        "solidCount": 2,
        "componentsDoNotOverlap": p.radial_clearance > 0,
        "componentMode": "two_solid_assembly_candidate",
        "bboxLength": float(p.main_length),
        "bboxDiameter": float(max(p.head_left_diameter, p.head_right_diameter)),
        "bboxMatchesParameters": True,
        "mainLengthMeasured": float(p.main_length),
        "headLengthMeasured": float(p.head_length),
        "neckLengthMeasured": float(p.neck_length),
        "tipLength": float(p.tip_length),
        "tipLengthMeasured": float(p.tip_length),
        "counterboreDiameterMeasured": float(p.counterbore_diameter),
        "counterboreDepthMeasured": float(p.counterbore_depth),
        "axialBoreDiameterMeasured": float(p.axial_bore_diameter),
        "outletDiameterMeasured": float(p.outlet_diameter),
        "outletTaperLength": float(p.outlet_taper_length),
        "outletTaperLengthMeasured": float(p.outlet_taper_length),
        "insertOuterDiameterMeasured": float(p.insert_outer_diameter),
        "insertLengthMeasured": float(p.insert_length),
        "insertNominalHoleDiameterMeasured": float(p.insert_thread_nominal_diameter),
        "insertAxialOffsetMeasured": float(p.insert_axial_offset),
        "radialClearance": float(p.radial_clearance),
        "radialClearanceMeasured": float(p.radial_clearance),
        "m12SimplifiedStraightHole": True,
        "realThreadGeometryPresent": False,
        "kernelAuditError": "",
    }


def audit_stepped_tapered_nozzle(
    parameters: SteppedTaperedNozzleParameters,
    shape: Any | None = None,
    *,
    engine: str | None = None,
    try_kernel: bool = True,
) -> dict[str, Any]:
    """Measure the two components, axial features, bores and assembly gap."""

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    kernel_shape = shape
    error = ""
    if kernel_shape is None and try_kernel and selected_engine == "cadquery-occt":
        try:
            kernel_shape = build_stepped_tapered_nozzle_shape(p)
        except Exception as exc:  # pragma: no cover - platform-specific OCCT failure
            error = f"{type(exc).__name__}: {exc}"
    if kernel_shape is None:
        result = _analytic_audit(p)
        result["kernelAuditError"] = error
        return result

    value = kernel_shape.val() if hasattr(kernel_shape, "val") else kernel_shape
    solids = _safe_shape_collection(value, "Solids") or []
    faces = _safe_shape_collection(value, "Faces") or []
    try:
        valid = bool(value.isValid())
    except Exception:
        valid = False
    try:
        overall_box = value.BoundingBox()
        bbox_length = float(overall_box.xlen)
        bbox_diameter = max(float(overall_box.ylen), float(overall_box.zlen))
    except Exception:
        bbox_length = bbox_diameter = 0.0

    solid_records: list[tuple[Any, Any]] = []
    for solid in solids:
        try:
            solid_records.append((solid, solid.BoundingBox()))
        except Exception:
            continue
    solid_records.sort(key=lambda item: float(item[1].xlen), reverse=True)
    main_solid = solid_records[0][0] if solid_records else None
    insert_solid = solid_records[1][0] if len(solid_records) > 1 else None
    main_box = solid_records[0][1] if solid_records else None
    insert_box = solid_records[1][1] if len(solid_records) > 1 else None

    cylinders: list[tuple[float, tuple[float, float, float] | None, tuple[float, float, float] | None, tuple[float, float, float, float, float, float] | None]] = []
    cones: list[tuple[float, float, tuple[float, float, float, float, float, float]]] = []
    planar: list[tuple[float, float, float, float, float, float]] = []
    for face in faces:
        box = _face_bbox(face)
        if box is None:
            continue
        try:
            geometry_type = str(face.geomType()).upper()
        except Exception:
            geometry_type = ""
        if "CYL" in geometry_type:
            radius, origin, direction = _cylinder_surface_data(face)
            cylinders.append((radius, origin, direction, box))
        elif "CONE" in geometry_type:
            cones.append((box[1] - box[0], max(box[3] - box[2], box[5] - box[4]), box))
        elif "PLANE" in geometry_type:
            planar.append(box)

    def cylinder(radius: float, start: float, end: float) -> bool:
        return any(
            math.isfinite(actual)
            and math.isclose(actual, radius, abs_tol=0.05)
            and _axis_is_x(direction)
            and box is not None
            and math.isclose(box[0], start, abs_tol=0.05)
            and math.isclose(box[1], end, abs_tol=0.05)
            for actual, _origin, direction, box in cylinders
        )

    head_cone = any(
        math.isclose(length, p.head_length, abs_tol=0.05)
        and math.isclose(diameter, p.head_right_diameter, abs_tol=0.05)
        for length, diameter, _box in cones
    )
    outlet_cone = any(
        math.isclose(length, p.outlet_taper_length, abs_tol=0.05)
        and math.isclose(diameter, p.outlet_diameter, abs_tol=0.05)
        for length, diameter, _box in cones
    )
    left_diameter = max(
        (
            max(box[3] - box[2], box[5] - box[4])
            for box in planar
            if math.isclose(box[0], 0, abs_tol=0.05)
            and math.isclose(box[1], 0, abs_tol=0.05)
        ),
        default=0.0,
    )
    no_overlap = False
    overlap_volume = 0.0
    if main_solid is not None and insert_solid is not None:
        try:
            overlap_volume = float(main_solid.intersect(insert_solid).Volume())
            no_overlap = overlap_volume <= 1e-5
        except Exception:
            no_overlap = False

    main_length_measured = float(main_box.xlen) if main_box is not None else 0.0
    insert_length_measured = float(insert_box.xlen) if insert_box is not None else 0.0
    insert_offset_measured = float(insert_box.xmin) if insert_box is not None else 0.0
    bbox_matches = bool(
        math.isclose(bbox_length, p.main_length, abs_tol=0.05)
        and math.isclose(
            bbox_diameter,
            max(p.head_left_diameter, p.head_right_diameter),
            abs_tol=0.05,
        )
    )
    neck_end = p.head_length + p.neck_length
    counterbore_present = cylinder(p.counterbore_diameter / 2, 0, p.counterbore_depth)
    axial_bore_present = cylinder(
        p.axial_bore_diameter / 2,
        p.counterbore_depth,
        p.outlet_taper_start_x,
    )
    neck_present = cylinder(p.neck_diameter / 2, p.head_length, neck_end)
    tip_present = cylinder(p.tip_diameter / 2, neck_end, p.main_length)
    insert_outer_present = cylinder(
        p.insert_outer_diameter / 2,
        p.insert_axial_offset,
        p.insert_axial_offset + p.insert_length,
    )
    insert_hole_present = cylinder(
        p.insert_thread_nominal_diameter / 2,
        p.insert_axial_offset,
        p.insert_axial_offset + p.insert_length,
    )
    clearance_measured = (
        p.counterbore_diameter / 2 - p.insert_outer_diameter / 2
        if counterbore_present and insert_outer_present
        else 0.0
    )
    topology_passed = bool(
        valid
        and len(solids) == 2
        and main_solid is not None
        and insert_solid is not None
        and no_overlap
        and bbox_matches
        and math.isclose(main_length_measured, p.main_length, abs_tol=0.05)
        and math.isclose(insert_length_measured, p.insert_length, abs_tol=0.05)
        and math.isclose(insert_offset_measured, p.insert_axial_offset, abs_tol=0.05)
        and math.isclose(left_diameter, p.head_left_diameter, abs_tol=0.05)
        and head_cone
        and neck_present
        and tip_present
        and counterbore_present
        and axial_bore_present
        and outlet_cone
        and insert_outer_present
        and insert_hole_present
        and math.isclose(clearance_measured, p.radial_clearance, abs_tol=0.01)
    )
    return {
        "topologyAuditEngine": "cadquery-occt",
        "kernelBacked": True,
        "topologyAuditPassed": topology_passed,
        "kernelShapeValid": valid,
        "solidCount": len(solids),
        "componentsDoNotOverlap": no_overlap,
        "componentOverlapVolumeMm3": overlap_volume,
        "componentMode": "two_solid_assembly_candidate",
        "bboxLength": bbox_length,
        "bboxDiameter": bbox_diameter,
        "bboxMatchesParameters": bbox_matches,
        "mainLengthMeasured": main_length_measured,
        "headLeftDiameterMeasured": left_diameter,
        "headLengthMeasured": float(p.head_length) if head_cone else 0.0,
        "neckLengthMeasured": float(p.neck_length) if neck_present else 0.0,
        "tipLength": float(p.tip_length),
        "tipLengthMeasured": float(p.tip_length) if tip_present else 0.0,
        "counterboreDiameterMeasured": float(p.counterbore_diameter) if counterbore_present else 0.0,
        "counterboreDepthMeasured": float(p.counterbore_depth) if counterbore_present else 0.0,
        "axialBoreDiameterMeasured": float(p.axial_bore_diameter) if axial_bore_present else 0.0,
        "outletDiameterMeasured": float(p.outlet_diameter) if outlet_cone else 0.0,
        "outletTaperLength": float(p.outlet_taper_length),
        "outletTaperLengthMeasured": float(p.outlet_taper_length) if outlet_cone else 0.0,
        "insertOuterDiameterMeasured": float(p.insert_outer_diameter) if insert_outer_present else 0.0,
        "insertLengthMeasured": insert_length_measured,
        "insertNominalHoleDiameterMeasured": float(p.insert_thread_nominal_diameter) if insert_hole_present else 0.0,
        "insertAxialOffsetMeasured": insert_offset_measured,
        "radialClearance": float(p.radial_clearance),
        "radialClearanceMeasured": clearance_measured,
        "m12SimplifiedStraightHole": p.insert_thread_designation == "M12",
        "realThreadGeometryPresent": False,
        "kernelAuditError": error,
    }


def validate_stepped_tapered_nozzle(
    parameters: SteppedTaperedNozzleParameters,
    *,
    engine: str | None = None,
) -> dict[str, Any]:
    """Validate dimensional relationships and the two-solid OCCT topology."""

    p = parameters
    selected_engine = engine or cadquery_status()["engine"]
    issues: list[dict[str, Any]] = []

    def check(rule: str, condition: bool, message: str, actual: Any, expected: Any) -> None:
        issues.append(
            {
                "ruleId": rule,
                "severity": "error",
                "passed": bool(condition),
                "message": message,
                "actual": actual,
                "expected": expected,
            }
        )

    positive_fields = {
        name: value
        for name, value in p.model_dump().items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and name not in {"insert_axial_offset", "insertAxialOffset"}
    }
    for name, value in positive_fields.items():
        check(
            f"dimension.{name}",
            math.isfinite(float(value)) and value > 0,
            f"{name} must be greater than zero",
            value,
            "> 0",
        )
    check(
        "stack.tip_length",
        p.tip_length > 0,
        "headLength and neckLength must leave a positive tipLength",
        p.tip_length,
        "> 0",
    )
    check(
        "profile.head_envelope",
        p.counterbore_diameter < min(p.head_left_diameter, p.head_right_diameter),
        "counterbore must leave material through the tapered head",
        p.counterbore_diameter,
        f"< {min(p.head_left_diameter, p.head_right_diameter):g}",
    )
    check(
        "profile.step_order",
        p.tip_diameter <= p.neck_diameter < min(p.head_left_diameter, p.head_right_diameter),
        "outer diameters must step down from head to neck to tip",
        [p.head_left_diameter, p.head_right_diameter, p.neck_diameter, p.tip_diameter],
        "head > neck >= tip",
    )
    check(
        "counterbore.depth",
        0 < p.counterbore_depth <= p.head_length,
        "counterboreDepth must terminate inside the head segment",
        p.counterbore_depth,
        f"0 .. {p.head_length:g}",
    )
    check(
        "bore.diameters",
        0 < p.axial_bore_diameter < p.outlet_diameter < p.tip_diameter,
        "axial bore and expanded outlet must remain inside the tip",
        [p.axial_bore_diameter, p.outlet_diameter, p.tip_diameter],
        "axialBoreDiameter < outletDiameter < tipDiameter",
    )
    check(
        "outlet.angle",
        0 < p.outlet_taper_half_angle < 90,
        "outletTaperHalfAngle must define a finite forward taper",
        p.outlet_taper_half_angle,
        "0 .. 90 degrees",
    )
    check(
        "outlet.tip_envelope",
        math.isfinite(p.outlet_taper_length) and 0 < p.outlet_taper_length <= p.tip_length,
        "derived outlet taper must fit inside the tip segment",
        p.outlet_taper_length,
        f"0 .. {p.tip_length:g}",
    )
    check(
        "insert.radial_clearance",
        p.radial_clearance > 0,
        "insert must have positive radial clearance inside the counterbore",
        p.radial_clearance,
        "> 0",
    )
    check(
        "insert.axial_offset",
        p.insert_axial_offset >= 0,
        "insertAxialOffset cannot precede the assembly datum",
        p.insert_axial_offset,
        ">= 0",
    )
    check(
        "insert.axial_envelope",
        p.insert_axial_offset + p.insert_length <= p.counterbore_depth,
        "insert must remain inside the counterbore depth",
        p.insert_axial_offset + p.insert_length,
        f"<= {p.counterbore_depth:g}",
    )
    check(
        "insert.thread_hole",
        p.insert_thread_nominal_diameter < p.insert_outer_diameter,
        "simplified nominal thread hole must leave an insert wall",
        p.insert_thread_nominal_diameter,
        f"< {p.insert_outer_diameter:g}",
    )
    dimensional_valid = not any(not issue["passed"] for issue in issues)
    audit = (
        audit_stepped_tapered_nozzle(
            p,
            engine=selected_engine,
            try_kernel=dimensional_valid and selected_engine == "cadquery-occt",
        )
        if dimensional_valid
        else {"topologyAuditPassed": False, "kernelBacked": False}
    )
    issues.append(
        {
            "ruleId": "topology.audit",
            "severity": "error" if selected_engine == "cadquery-occt" else "info",
            "passed": bool(audit.get("topologyAuditPassed", False)),
            "message": "two-solid nozzle/insert envelope and feature topology audit",
            "actual": audit,
            "expected": "two valid, unfused coaxial solids with the dimensioned outer steps, bores, taper and radial clearance",
        }
    )
    issues.append(
        {
            "ruleId": "thread.m12_simplified",
            "severity": "warning",
            "passed": True,
            "message": "M12 is represented as a nominal straight bore; no pitch, tolerance class or helical tooth was inferred",
            "actual": p.insert_thread_designation,
            "expected": "confirm pitch/tolerance before thread manufacturing",
        }
    )
    valid = dimensional_valid and (
        bool(audit.get("topologyAuditPassed")) if selected_engine == "cadquery-occt" else True
    )
    production_ready = bool(valid and selected_engine == "cadquery-occt" and audit.get("kernelBacked"))
    metrics = dict(audit)
    metrics.update(
        {
            "tipLength": float(p.tip_length),
            "outletTaperLength": float(p.outlet_taper_length),
            "radialClearance": float(p.radial_clearance),
            "previewOnly": not production_ready,
            "threadRepresentation": "nominal straight bore; non-helical",
        }
    )
    return {
        "valid": valid,
        "productionReady": production_ready,
        "engine": selected_engine,
        "parameters": p.model_dump(mode="json", by_alias=True),
        "issues": issues,
        "metrics": metrics,
    }


def generate_stepped_tapered_nozzle_artifacts(
    parameters: SteppedTaperedNozzleParameters,
    formats: Iterable[str] = ("step", "glb"),
    *,
    require_cadquery: bool = False,
) -> list[GeneratedArtifact]:
    """Export two-component STEP and GLB without fusing either component."""

    requested = list(dict.fromkeys(str(item).lower() for item in formats))
    unsupported = [item for item in requested if item not in {"step", "glb"}]
    if unsupported:
        raise ValueError(f"unsupported output format(s): {', '.join(unsupported)}")
    shape = None
    component_meshes: tuple[Mesh, Mesh] | None = None
    build_error = ""
    if get_cadquery() is not None:
        try:
            main, insert = build_stepped_tapered_nozzle_components(parameters)
            cq = get_cadquery()
            assert cq is not None
            shape = _workplane_for(cq, cq.Compound.makeCompound([main.val(), insert.val()]))
            main_mesh = _mesh_from_cadquery(main)
            insert_mesh = _mesh_from_cadquery(insert)
            if main_mesh is not None and insert_mesh is not None:
                component_meshes = (main_mesh, insert_mesh)
        except Exception as exc:  # pragma: no cover - host-specific OCCT failure
            build_error = f"{type(exc).__name__}: {exc}"
            if require_cadquery:
                raise RuntimeError(f"CadQuery shape build failed: {build_error}") from exc
    elif require_cadquery:
        raise RuntimeError("CadQuery is required but unavailable")
    if require_cadquery and shape is None:
        raise RuntimeError(f"CadQuery shape is required but unavailable: {build_error or 'unknown error'}")
    if component_meshes is None:
        if require_cadquery and "glb" in requested:
            raise RuntimeError("CadQuery tessellation failed; strict output cannot use a mesh fallback")
        component_meshes = build_stepped_tapered_nozzle_fallback_meshes(parameters)

    artifacts: list[GeneratedArtifact] = []
    for format_name in requested:
        if format_name == "glb":
            artifacts.append(
                GeneratedArtifact(
                    format="glb",
                    data=_two_component_glb(*component_meshes),
                    engine="cadquery-tessellation" if shape is not None else "mesh-fallback",
                    production_ready=False,
                    warnings=[
                        "GLB contains two independent component meshes; retain STEP for editable B-Rep solids.",
                        "M12 is a simplified nominal straight bore, not a helical thread form.",
                    ],
                )
            )
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
                artifacts.append(
                    GeneratedArtifact(
                        format="step",
                        data=data,
                        engine="cadquery-occt",
                        production_ready=True,
                        warnings=[
                            "STEP contains two unfused solids.",
                            "M12 is a simplified nominal straight bore; pitch and tolerance require confirmation.",
                        ],
                    )
                )
                continue
            except Exception as exc:  # pragma: no cover - exporter/platform dependent
                build_error = f"STEP exporter failed: {type(exc).__name__}: {exc}"
                if require_cadquery:
                    raise RuntimeError(build_error) from exc
        fallback = _step_fallback(_merge_meshes(component_meshes), parameters)
        artifacts.append(
            GeneratedArtifact(
                format="step",
                data=fallback,
                engine="faceted-step-fallback",
                production_ready=False,
                warnings=[
                    "CadQuery/OCCT unavailable; STEP is a faceted two-component preview, not production B-Rep.",
                    "M12 is a simplified nominal straight bore, not a helical thread form.",
                ]
                + ([build_error] if build_error else []),
            )
        )
    return artifacts


__all__ = [
    "audit_stepped_tapered_nozzle",
    "build_stepped_tapered_nozzle_components",
    "build_stepped_tapered_nozzle_fallback_meshes",
    "build_stepped_tapered_nozzle_shape",
    "generate_stepped_tapered_nozzle_artifacts",
    "validate_stepped_tapered_nozzle",
]
