"""Measurements and engineering/diagnostic views of actual OCCT solids.

These observations do not claim that a solid agrees with a source drawing.
The agent must compare them with independently recorded drawing evidence.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

from .cad_plan import PlanValidationError, evaluate_expression

_NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"
_PATH_POINT = re.compile(rf"([ML])\s*({_NUMBER})\s*,\s*({_NUMBER})")


def solid_ray_intervals(shape: Any, origin: tuple[float, float, float],
                        direction: tuple[float, float, float], start: float, end: float) -> list[list[float]]:
    """Intersect exact B-Rep faces and classify the resulting line intervals."""
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

    value = shape.val() if hasattr(shape, "val") else shape
    magnitude = math.sqrt(sum(component * component for component in direction))
    if magnitude < 1e-12 or end <= start:
        raise PlanValidationError("Ray requires a nonzero direction and end > start")
    unit = tuple(component / magnitude for component in direction)
    intersector = IntCurvesFace_ShapeIntersector()
    intersector.Load(value.wrapped, 1e-7)
    intersector.Perform(gp_Lin(gp_Pnt(*origin), gp_Dir(*unit)), start, end)
    if not intersector.IsDone():
        raise RuntimeError("OCCT ray intersection failed")
    points = sorted([start, end] + [intersector.WParameter(i) for i in range(1, intersector.NbPnt() + 1)])
    distinct: list[float] = []
    for point in points:
        if not distinct or abs(point - distinct[-1]) > 1e-6:
            distinct.append(float(point))
    intervals: list[list[float]] = []
    for low, high in zip(distinct, distinct[1:]):
        midpoint = tuple(origin[i] + unit[i] * (low + high) / 2 for i in range(3))
        if any(solid.isInside(midpoint, 1e-7) for solid in value.Solids()):
            if intervals and abs(intervals[-1][1] - low) < 1e-6:
                intervals[-1][1] = high
            else:
                intervals.append([low, high])
    return intervals


def inspect_shape(shape: Any, *, ray_probes: list[dict[str, Any]] | None = None,
                  parameters: dict[str, float] | None = None) -> dict[str, Any]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from .geometry import _cylinder_surface_data

    value = shape.val() if hasattr(shape, "val") else shape
    if not value.Solids():
        raise RuntimeError("CAD result contains no solids")
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(value.wrapped, box, False, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    cylinders = []
    for index, face in enumerate(value.Faces()):
        if face.geomType() == "CYLINDER":
            radius, origin, direction = _cylinder_surface_data(face)
            if math.isfinite(radius) and origin is not None and direction is not None:
                cylinders.append({"faceIndex": index, "radius": float(radius), "diameter": 2 * float(radius),
                                  "origin": list(origin), "axis": list(direction), "area": float(face.Area())})
    probes = ray_probes or []
    if not isinstance(probes, list) or len(probes) > 32:
        raise PlanValidationError("At most 32 ray probes are permitted")
    rays = {}
    for probe in probes:
        if not isinstance(probe, dict) or set(probe) != {"id", "origin", "direction", "start", "end"}:
            raise PlanValidationError("Ray probe requires id, origin, direction, start and end")
        if not isinstance(probe["id"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", probe["id"]) or probe["id"] in rays:
            raise PlanValidationError("Ray probe IDs must be unique identifiers")
        def vector(key: str) -> tuple[float, float, float]:
            raw = probe[key]
            if not isinstance(raw, list) or len(raw) != 3:
                raise PlanValidationError("Ray origins and directions require three dimensions")
            return tuple(evaluate_expression(item, parameters or {}) for item in raw)
        origin, direction = vector("origin"), vector("direction")
        start = evaluate_expression(probe["start"], parameters or {})
        end = evaluate_expression(probe["end"], parameters or {})
        magnitude = math.sqrt(sum(component * component for component in direction))
        if magnitude < 1e-12 or end <= start:
            raise PlanValidationError("Ray requires a nonzero direction and end > start")
        unit = tuple(component / magnitude for component in direction)
        lower, upper = (xmin, ymin, zmin), (xmax, ymax, zmax)
        projected_min = sum(((lower[i] if unit[i] >= 0 else upper[i])-origin[i])*unit[i] for i in range(3))
        projected_max = sum(((upper[i] if unit[i] >= 0 else lower[i])-origin[i])*unit[i] for i in range(3))
        margin = max(1e-4, math.dist(lower, upper) * 1e-6)
        full_start, full_end = projected_min-margin, projected_max+margin
        # The submitted window is diagnostic only. A thickness or through-hole
        # requirement must include every solid interval on this entire line,
        # even when material continues outside the model's requested window.
        full_intervals = solid_ray_intervals(value, origin, direction, full_start, full_end)
        window_intervals = [[max(start, low), min(end, high)] for low, high in full_intervals
                            if min(end, high)-max(start, low) > 1e-7]
        rays[probe["id"]] = {"origin": list(origin), "direction": list(direction),
                              "requestedRange": [start, end], "intervals": window_intervals,
                              "fullMaterialIntervals": full_intervals,
                              "coverage": {"complete": True, "basis": "actual_solid_bbox", "range": [full_start, full_end],
                                           "bboxProjection": [projected_min, projected_max],
                                           "requestedRangeCoversBody": start <= projected_min and end >= projected_max,
                                           "materialOutsideRequestedRange": any(low < start-1e-7 or high > end+1e-7 for low, high in full_intervals)}}
    bbox = {"min": [xmin, ymin, zmin], "max": [xmax, ymax, zmax],
            "size": [xmax - xmin, ymax - ymin, zmax - zmin]}
    return {"engine": "cadquery-occt", "kernelBacked": True, "valid": bool(value.isValid()),
            "solidCount": len(value.Solids()), "faceCount": len(value.Faces()), "edgeCount": len(value.Edges()),
            "volumeMm3": float(value.Volume()), "surfaceAreaMm2": float(value.Area()),
            "bbox": bbox, "bboxLength": bbox["size"][0], "bboxWidth": bbox["size"][1], "bboxHeight": bbox["size"][2],
            "bboxMeasurementSource": "exact-brep-surfaces-without-triangulation",
            "cylinders": cylinders, "raySections": rays,
            "drawingAgreement": "not_checked", "productionReady": False,
            "scope": "Measured from actual OCCT solid. Geometry validity is not drawing agreement or manufacturing certification."}


def _normalize_projection_axes(svg_text: str, direction: tuple[float, float, float],
                               horizontal: tuple[float, float, float], vertical: tuple[float, float, float]) -> str:
    """Replace OCCT's arbitrary camera roll with engineering drawing axes."""
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    camera = gp_Ax2(gp_Pnt(), gp_Dir(*direction))
    camera_x, camera_y = camera.XDirection().Coord(), camera.YDirection().Coord()
    matrix = [[sum(desired[i] * actual[i] for i in range(3)) for actual in (camera_x, camera_y)]
              for desired in (horizontal, vertical)]
    root = ET.fromstring(svg_text)
    all_points = []
    for path in root.iter("{http://www.w3.org/2000/svg}path"):
        transformed = []
        for command, raw_x, raw_y in _PATH_POINT.findall(path.attrib.get("d", "")):
            x, y = float(raw_x), float(raw_y)
            new_x, new_y = matrix[0][0] * x + matrix[0][1] * y, matrix[1][0] * x + matrix[1][1] * y
            transformed.append(f"{command}{new_x:.12g},{new_y:.12g}")
            all_points.append((new_x, new_y))
        path.set("d", " ".join(transformed))
    if not all_points:
        raise RuntimeError("OCCT projection contains no projected edges")
    xmin, xmax = min(p[0] for p in all_points), max(p[0] for p in all_points)
    ymin, ymax = min(p[1] for p in all_points), max(p[1] for p in all_points)
    width, height = float(root.attrib["width"]), float(root.attrib["height"])
    scale = min((width - 160) / max(xmax - xmin, 1e-9), (height - 120) / max(ymax - ymin, 1e-9))
    translate_x = -xmin + (width / scale - (xmax - xmin)) / 2
    translate_y = -ymax - (height / scale - (ymax - ymin)) / 2
    group = root.find("{http://www.w3.org/2000/svg}g")
    group.set("transform", f"scale({scale}, {-scale}) translate({translate_x}, {translate_y})")
    group.set("stroke-width", str(1.2 / scale))
    for child in group.iter():
        if "stroke-dasharray" in child.attrib:
            child.set("stroke-dasharray", f"{4/scale},{4/scale}")
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def _projection_png(svg_text: str, path: Path, title: str, *,
                    projection_label: str = "OCCT orthographic projection | mm") -> None:
    """Rasterize CadQuery's projected M/L polylines, preserving hidden lines.

    CadQuery's HLR SVG exporter discretizes exact projected edges into M/L
    paths. Reading those paths avoids a second, inconsistent mesh projection
    and keeps the renderer dependency limited to the existing Pillow package.
    """
    from PIL import Image, ImageDraw

    root = ET.fromstring(svg_text)
    paths: list[tuple[list[tuple[float, float]], bool]] = []

    def walk(element: Any, hidden: bool = False) -> None:
        hidden = hidden or "stroke-dasharray" in element.attrib
        if element.tag.endswith("path"):
            data = element.attrib.get("d", "")
            # Exporter-generated CAD paths only: never accept arbitrary SVG
            # resources, fonts, scripts, links or embedded images.
            points = [(float(x), float(y)) for _, x, y in _PATH_POINT.findall(data)]
            if points:
                paths.append((points, hidden))
        for child in element:
            walk(child, hidden)

    walk(root)
    points = [point for line, _ in paths for point in line]
    if not points:
        raise RuntimeError("OCCT projection contains no projected edges")
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    width, height, margin = 960, 720, 48
    scale = min((width - 2 * margin) / max(xmax - xmin, 1e-9), (height - 2 * margin - 28) / max(ymax - ymin, 1e-9))
    offset_x = (width - (xmax - xmin) * scale) / 2
    offset_y = (height - (ymax - ymin) * scale) / 2 + 14
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 16), f"{title} | {projection_label}", fill="#333333")
    for line, hidden in sorted(paths, key=lambda item: not item[1]):
        mapped = [(offset_x + (x - xmin) * scale, offset_y + (ymax - y) * scale) for x, y in line]
        if not hidden:
            draw.line(mapped, fill="#1c3149", width=2)
            continue
        phase = 0.0
        for p0, p1 in zip(mapped, mapped[1:]):
            distance = math.dist(p0, p1)
            cursor = 0.0
            while cursor < distance:
                run = min(distance - cursor, 8 - phase % 8)
                if phase % 16 < 8:
                    start = tuple(p0[i] + (p1[i] - p0[i]) * cursor / distance for i in (0, 1))
                    end = tuple(p0[i] + (p1[i] - p0[i]) * (cursor + run) / distance for i in (0, 1))
                    draw.line([start, end], fill="#a3a9af", width=1)
                cursor += run
                phase += run
    image.save(path)


def export_projections(shape: Any, output_dir: Path) -> dict[str, dict[str, str]]:
    from cadquery.occ_impl.exporters.svg import getSVG

    value = shape.val() if hasattr(shape, "val") else shape
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    # Front is XZ viewed from -Y; right is YZ viewed from +X.
    view_axes = {"front": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
                 "top": ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
                 "right": ((1, 0, 0), (0, 1, 0), (0, 0, 1))}
    for name, (direction, horizontal, vertical) in view_axes.items():
        svg = getSVG(value, {"width": 960, "height": 720, "marginLeft": 80, "marginTop": 60,
                             "projectionDir": direction, "showAxes": False, "showHidden": True})
        svg = _normalize_projection_axes(svg, direction, horizontal, vertical)
        path, png = output_dir / f"{name}.svg", output_dir / f"{name}.png"
        path.write_text(svg, encoding="utf-8")
        _projection_png(svg, png, name.title())
        result[name] = {"path": str(path), "mimeType": "image/svg+xml", "pngPath": str(png), "pngMimeType": "image/png"}
    return result


def export_spatial_views(shape: Any, output_dir: Path) -> dict[str, dict[str, Any]]:
    """Export one fixed OCCT isometric view for spatial diagnosis only.

    This axonometric view is distinct from the front/top/right engineering
    projections. Its foreshortened axes must not enter 2D pixel registration.
    Both formats use the same exact-solid HLR edges, never substitute meshes.
    """
    from cadquery.occ_impl.exporters.svg import getSVG

    value = shape.val() if hasattr(shape, "val") else shape
    output_dir.mkdir(parents=True, exist_ok=True)
    direction = (1, -1, 1)  # Look at the front, right and top; keep world Z up.
    horizontal = (1 / math.sqrt(2), 1 / math.sqrt(2), 0)
    vertical = (-1 / math.sqrt(6), 1 / math.sqrt(6), 2 / math.sqrt(6))
    svg = getSVG(value, {"width": 960, "height": 720, "marginLeft": 80, "marginTop": 60,
                         "projectionDir": direction, "showAxes": False, "showHidden": False})
    svg = _normalize_projection_axes(svg, direction, horizontal, vertical)
    caption = "Actual OCCT spatial diagnostic | not an orthographic engineering view"
    root = ET.fromstring(svg)
    root.set("data-view-type", "isometric")
    root.set("data-pixel-registration-eligible", "false")
    label = ET.SubElement(root, "{http://www.w3.org/2000/svg}text", {
        "x": "24", "y": "24", "fill": "#333333", "font-size": "12", "font-family": "sans-serif"})
    label.text = f"Isometric | {caption}"
    svg = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    path, png = output_dir / "isometric.svg", output_dir / "isometric.png"
    path.write_text(svg, encoding="utf-8")
    _projection_png(svg, png, "Isometric", projection_label=caption)
    return {"isometric": {"path": str(path), "mimeType": "image/svg+xml", "pngPath": str(png), "pngMimeType": "image/png",
                           "viewType": "isometric", "scope": "spatial_diagnostic", "cameraDirection": list(direction),
                           "orthographicEngineeringView": False, "pixelRegistrationEligible": False,
                           "productionReady": False}}


def inspect_step(path: str | Path, *, ray_probes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Re-import a delivered STEP for a separate geometry observation."""
    from cadquery import importers
    return inspect_shape(importers.importStep(str(path)), ray_probes=ray_probes)
