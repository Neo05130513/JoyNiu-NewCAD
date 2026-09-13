"""Real solids distinguish smooth surface joins from sharp and hidden edges."""
import math
import re
import xml.etree.ElementTree as ET

import pytest

from app.cad_inspector import export_projections, solid_ray_intervals
from app.geometry import get_cadquery


pytestmark = pytest.mark.skipif(get_cadquery() is None, reason="CadQuery is unavailable")


def stepped_d_shape():
    cq = get_cadquery()
    rear = cq.Workplane("XY").box(66, 35, 75, centered=False).translate((-33, 0, 0))
    front = (cq.Workplane("XY").moveTo(-33, 35)
             .threePointArc((0, 68), (33, 35)).close().extrude(55))
    return rear.union(front).clean()


def svg_paths(path):
    """Return raw engineering coordinates, with inherited hidden-line styling."""
    paths = {"visible": [], "hidden": []}

    def walk(element, hidden=False):
        hidden = hidden or "stroke-dasharray" in element.attrib
        if element.tag.endswith("path"):
            points = [(float(x), float(y)) for x, y in
                      re.findall(r"[ML]\s*([^,\s]+),([^\sML]+)", element.attrib.get("d", ""))]
            assert len(points) >= 2
            paths["hidden" if hidden else "visible"].append(points)
        for child in element:
            walk(child, hidden)

    walk(ET.parse(path).getroot())
    assert paths["visible"]
    return paths


def line_spans(paths, fixed_axis, coordinate):
    """Merge collinear path segments, independent of edge splitting/direction."""
    spans = []
    for points in paths:
        for start, end in zip(points, points[1:]):
            if all(abs(point[fixed_axis] - coordinate) < 1e-7 for point in (start, end)):
                low, high = sorted((start[1 - fixed_axis], end[1 - fixed_axis]))
                if high - low > 1e-7:
                    spans.append((low, high))
    merged = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1] + 1e-7:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def test_right_view_omits_tangent_join_but_retains_collinear_sharp_step_and_silhouette(tmp_path):
    shape = stepped_d_shape().val()
    assert shape.isValid() and len(shape.Solids()) == 1
    assert shape.Volume() == pytest.approx(66 * 35 * 75 + math.pi * 33**2 * 55 / 2)

    # Both joins project to Y=35. Verify their actual adjacent face normals:
    # the lower plane/cylinder join is G1; the upper plane/plane join is sharp.
    joins = sorted((edge for edge in shape.Edges() if edge.geomType() == "LINE"
                    and all(abs(vertex.X - 33) < 1e-7 and abs(vertex.Y - 35) < 1e-7
                            for vertex in edge.Vertices())), key=lambda edge: edge.Center().z)
    assert len(joins) == 2
    for edge, expected_z, face_types, normal_dot in zip(
            joins, [(0, 55), (55, 75)], [("CYLINDER", "PLANE"), ("PLANE", "PLANE")], [1, 0]):
        assert sorted(vertex.Z for vertex in edge.Vertices()) == pytest.approx(expected_z)
        faces = [face for face in shape.Faces() if any(candidate.isSame(edge) for candidate in face.Edges())]
        assert tuple(sorted(face.geomType() for face in faces)) == face_types
        normals = [face.normalAt(edge.Center()) for face in faces]
        assert normals[0].dot(normals[1]) == pytest.approx(normal_dot, abs=1e-7)

    views = export_projections(shape, tmp_path)
    assert set(views) == {"front", "top", "right"}
    visible = svg_paths(views["right"]["path"])["visible"]
    assert line_spans(visible, 0, 35) == [(55, 75)]
    # The cylinder silhouette has no source B-Rep edge at Y=68, but must render.
    assert line_spans(visible, 0, 68) == [(0, 55)]
    assert line_spans(visible, 0, 0) == [(0, 75)]
    assert line_spans(visible, 1, 0) == [(0, 68)]
    assert line_spans(visible, 1, 55) == [(35, 68)]
    assert line_spans(visible, 1, 75) == [(0, 35)]


def test_through_bore_keeps_visible_rim_and_dashed_inner_wall_outlines(tmp_path):
    cq = get_cadquery()
    base = stepped_d_shape()
    bore = cq.Workplane("XY").center(0, 35).circle(18).extrude(80)
    shape = base.cut(bore).clean().val()
    assert shape.isValid() and len(shape.Solids()) == 1
    # Half of the bore passes through the 75 mm step, half through 55 mm.
    assert base.val().Volume() - shape.Volume() == pytest.approx(math.pi * 18**2 * 65)
    assert solid_ray_intervals(shape, (0, 35, 0), (0, 0, 1), -1, 76) == []
    full_bore = cq.Workplane("XY", origin=(0, 35, -5)).circle(18).extrude(85).val()
    assert shape.intersect(full_bore).Volume() == pytest.approx(0, abs=1e-7)

    views = export_projections(shape, tmp_path)
    right = svg_paths(views["right"]["path"])
    assert line_spans(right["visible"], 0, 35) == [(55, 75)]
    for y, height in [(17, 75), (53, 55)]:
        assert line_spans(right["hidden"], 0, y) == [(0, height)]
        assert line_spans(right["visible"], 0, y) == []

    top = svg_paths(views["top"]["path"])
    rim = [points for points in top["visible"] if len(points) > 2
           and all(abs(math.hypot(x, y - 35) - 18) < 1e-7 for x, y in points)]
    assert rim
    # The two different-height semicircular rims form the complete top-view hole.
    length = sum(math.dist(start, end) for points in rim for start, end in zip(points, points[1:]))
    assert length == pytest.approx(2 * math.pi * 18, rel=1e-3)
    points = [point for arc in rim for point in arc]
    assert [min(x for x, _ in points), max(x for x, _ in points),
            min(y for _, y in points), max(y for _, y in points)] == pytest.approx([-18, 18, 17, 53], abs=.02)
    assert line_spans(top["visible"], 1, 35) == [(-33, -18), (18, 33)]
