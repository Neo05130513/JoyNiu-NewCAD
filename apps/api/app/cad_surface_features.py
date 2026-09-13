"""Exact OCCT sheet, face-edit and individual body operations."""
from __future__ import annotations

import math

SURFACE_OP_FIELDS = {
    "surface_offset": ({"input", "faces", "distance"}, {"keepOriginal"}),
    "move_face": ({"input", "faces", "distance"}, set()),
    "surface_boundary": ({"input", "edges"}, {"keepOriginal", "continuity"}),
    "surface_style": ({"points"}, {"tolerance"}),
    "surface_join": ({"inputs"}, {"tolerance", "makeSolid"}),
    "surface_thicken": ({"input", "faces", "thickness"}, {"keepOriginal"}),
    "body_edit": ({"input", "bodies", "action"}, {"vector", "axisStart", "axisEnd", "angle"}),
    "mesh_body": ({"vertices", "triangles"}, set()),
}
SHEET_OPS = {"surface_offset", "surface_boundary", "surface_style", "surface_join", "body_edit", "compound"}


def _error(message, code="invalid_plan"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def validate_surface_feature(feature, scalar, vector, seen):
    from .cad_topology import validate_topology_selector
    op = feature["op"]
    if "input" in feature and feature["input"] not in seen: _error("曲面或实体操作必须引用前置特征。")
    if "inputs" in feature:
        values = feature["inputs"]
        if not isinstance(values, list) or not 2 <= len(values) <= 32 or len(set(values)) != len(values) or any(v not in seen for v in values): _error("曲面连接需要至少两个不同的前置曲面。")
    for key in ("distance", "thickness", "angle", "tolerance"):
        if key in feature: scalar(feature[key])
    for key in ("vector", "axisStart", "axisEnd"):
        if key in feature: vector(feature[key], 3)
    for key in ("keepOriginal", "makeSolid"):
        if key in feature and type(feature[key]) is not bool: _error("曲面选项格式无效。")
    for key, kind in (("faces", "face"), ("edges", "edge")):
        if key not in feature: continue
        values = feature[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 64: _error("请选择 1 至 64 个有效的面或边。")
        indices = set()
        for value in values:
            validate_topology_selector(value, kind, seen)
            if value["sourceFeatureId"] != feature["input"] or value["index"] in indices: _error("曲面选择须来自同一输入且不能重复。")
            indices.add(value["index"])
    if op == "surface_boundary" and feature.get("continuity", "position") not in {"position", "tangent", "curvature"}: _error("曲面连续性无效。")
    if op == "surface_style":
        points = feature["points"]
        if not isinstance(points, list) or not 2 <= len(points) <= 16 or not isinstance(points[0], list) or not 2 <= len(points[0]) <= 16: _error("样式曲面需要 2×2 至 16×16 的点网格。")
        for row in points:
            if not isinstance(row, list) or len(row) != len(points[0]): _error("点网格各行长度必须一致。")
            for point in row: vector(point, 3)
    if op == "body_edit":
        bodies = feature["bodies"]
        if not isinstance(bodies, list) or not 1 <= len(bodies) <= 256: _error("请选择要操作的实体。")
        for body in bodies:
            if not isinstance(body, dict) or set(body) != {"index", "signature"} or type(body["index"]) is not int or not 0 <= body["index"] < 256:
                _error("实体选择必须包含序号和几何签名。")
            import re
            if not isinstance(body["signature"], str) or not re.fullmatch(r"[a-f0-9]{64}", body["signature"]): _error("实体几何签名无效。")
        if len({b["index"] for b in bodies}) != len(bodies): _error("实体选择不能重复。")
        if feature["action"] not in {"delete", "keep", "translate", "copy", "rotate"}: _error("实体操作无效。")
        if feature["action"] in {"translate", "copy"} and "vector" not in feature: _error("移动或复制实体需要位移。")
        if feature["action"] == "rotate" and not {"axisStart", "axisEnd", "angle"}.issubset(feature): _error("旋转实体需要轴及角度。")
    if op == "mesh_body":
        vertices, triangles = feature["vertices"], feature["triangles"]
        if not isinstance(vertices, list) or not 4 <= len(vertices) <= 10000 or not isinstance(triangles, list) or not 4 <= len(triangles) <= 10000: _error("网格转换支持 4 至 10000 个顶点及三角面。", "resource_limit")
        for vertex in vertices: vector(vertex, 3)
        for triangle in triangles:
            if not isinstance(triangle, list) or len(triangle) != 3 or len(set(triangle)) != 3 or any(type(i) is not int or not 0 <= i < len(vertices) for i in triangle): _error("网格三角面索引无效。")


def body_signature(body):
    from .cad_topology import digest, face_signature
    return digest({"faces": sorted(face_signature(f) for f in body.Faces()), "volume": round(body.Volume(), 8)})


def topology_bodies(shape):
    from .cad_topology import _bounds
    result = []
    value = shape.val() if hasattr(shape, "val") else shape
    all_faces, all_edges = value.Faces(), value.Edges()
    for index, body in enumerate(value.Solids()):
        faces, edges = body.Faces(), body.Edges()
        result.append({"id": f"body:{index}", "index": index, "signature": body_signature(body), "bounds": _bounds(body),
                       "volumeMm3": float(body.Volume()), "faceIds": [f"face:{i}" for i, f in enumerate(all_faces) if any(f.isSame(b) for b in faces)],
                       "edgeIds": [f"edge:{i}" for i, e in enumerate(all_edges) if any(e.isSame(b) for b in edges)]})
    return result


def topology_surface_bodies(shape):
    """Independent Face/Shell members, excluding faces owned by solids."""
    from .cad_topology import _bounds, digest, face_signature
    value = shape.val() if hasattr(shape, "val") else shape
    all_faces, all_edges = value.Faces(), value.Edges()
    pending, result = [value], []
    while pending:
        member = pending.pop(0)
        if member.ShapeType() in {"Compound", "CompSolid"}: pending[0:0] = list(member)
        elif member.ShapeType() in {"Face", "Shell"}:
            faces, edges = member.Faces(), member.Edges()
            index = len(result)
            result.append({"id":f"surface:{index}","index":index,"shapeType":member.ShapeType(),
                           "signature":digest({"kind":member.ShapeType(),"faces":sorted(face_signature(face) for face in faces)}),
                           "areaMm2":float(member.Area()),"bounds":_bounds(member),
                           "faceIds":[f"face:{i}" for i, face in enumerate(all_faces) if any(face.isSame(candidate) for candidate in faces)],
                           "edgeIds":[f"edge:{i}" for i, edge in enumerate(all_edges) if any(edge.isSame(candidate) for candidate in edges)]})
    return result


def _collection(cq, shapes):
    if not shapes: _error("操作不能删除最后一个实体。", "empty_geometry")
    return cq.Workplane("XY").newObject([shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)])


def build_surface_feature(cq, feature, shapes, number, vector, plan, history=None):
    from .cad_topology import resolve_topology_selection
    op = feature["op"]
    base = shapes.get(feature.get("input"))
    def selected(kind):
        key = "faces" if kind == "face" else "edges"
        if history:
            return history.resolve(plan, feature, feature["input"], base, feature[key], kind, key)[0]
        return resolve_topology_selection(plan, feature["input"], base, feature[key], kind, max_faces=64)
    def output(values, keep=False):
        return _collection(cq, ([base.val()] if keep and base else []) + values)
    if op in {"surface_offset", "move_face", "surface_thicken"}:
        faces = selected("face")
        distance = number(feature.get("distance", feature.get("thickness")))
        if abs(distance) < 1e-7: _error("偏移距离或厚度不能为零。")
        if op == "move_face":
            from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Skin
            from OCP.GeomAbs import GeomAbs_Intersection
            builder = BRepOffset_MakeOffset()
            builder.Initialize(base.val().wrapped, 0.0, 1e-6, BRepOffset_Skin, False, False, GeomAbs_Intersection)
            for face in faces: builder.SetOffsetOnFace(face.wrapped, distance)
            builder.MakeOffsetShape()
            if not builder.IsDone(): _error("选定面无法按此距离移动，请调整距离或选择。", "feature_failed")
            return output([cq.Shape.cast(builder.Shape())])
        if op == "surface_thicken":
            return output([face.thicken(distance) for face in faces], feature.get("keepOriginal", False))
        from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape
        result = []
        for face in faces:
            builder = BRepOffsetAPI_MakeOffsetShape(); builder.PerformByJoin(face.wrapped, distance, 1e-6)
            if not builder.IsDone(): _error("所选曲面无法生成偏移面。", "feature_failed")
            result.append(cq.Shape.cast(builder.Shape()))
        return output(result, feature.get("keepOriginal", True))
    if op == "surface_boundary":
        edges = selected("edge")
        try:
            boundary = cq.Wire.assembleEdges(edges)
            if not boundary.IsClosed() or not boundary.isValid(): _error("边界曲面需要一条完整闭合、相连的边界。", "invalid_boundary")
        except Exception as exc:
            from .cad_plan import PlanValidationError
            if isinstance(exc, PlanValidationError): raise
            _error("选择的边不构成完整闭合边界。", "invalid_boundary")
        # Higher continuity needs the supporting source faces; use OCCT filling
        # constraints instead of relabelling a positional patch as G1 or G2.
        from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeFilling
        from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1, GeomAbs_C1
        # OCCT BRepFill passes this enum directly to the integer GeomPlate
        # derivative order (0/1/2). GeomAbs_G2 has value 3 and is rejected in
        # the shipped kernel; enum value 2 requests actual curvature matching.
        continuity = {"position": GeomAbs_C0, "tangent": GeomAbs_G1, "curvature": GeomAbs_C1}[feature.get("continuity", "position")]
        builder = BRepOffsetAPI_MakeFilling()
        supports = []
        for edge in edges:
            if continuity == GeomAbs_C0: builder.Add(edge.wrapped, continuity, True)
            else:
                support = [f for f in base.val().Faces() if any(e.isSame(edge) for e in f.Edges())]
                if len(support) != 1: _error("相切或曲率连续需选择仅属于一个支撑面的边界边。")
                supports.append(support[0])
                builder.Add(edge.wrapped, support[0].wrapped, continuity, True)
        if supports and all(face.isSame(supports[0]) for face in supports): builder.LoadInitSurface(supports[0].wrapped)
        builder.Build()
        if not builder.IsDone(): _error("所选边界不能形成有效混合曲面。", "feature_failed")
        if builder.G0Error() > 1e-4 or (feature.get("continuity") in {"tangent","curvature"} and builder.G1Error() > .01) or (feature.get("continuity") == "curvature" and builder.G2Error() > .1):
            _error("曲面未达到请求的边界连续性公差，请调整边界。", "surface_continuity_failed")
        return output([cq.Shape.cast(builder.Shape())], feature.get("keepOriginal", True))
    if op == "surface_style":
        tolerance = number(feature.get("tolerance", 0.01))
        if not 1e-7 <= tolerance <= 1: _error("样式曲面拟合公差应在 0.0000001 至 1 mm。")
        face = cq.Face.makeSplineApprox([[cq.Vector(*vector(p)) for p in row] for row in feature["points"]], tol=tolerance)
        return output([face])
    if op == "surface_join":
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
        tolerance = number(feature.get("tolerance", 1e-5))
        if not 1e-7 <= tolerance <= 1: _error("缝合公差应在 0.0000001 至 1 mm。")
        sewing = BRepBuilderAPI_Sewing(tolerance)
        for ref in feature["inputs"]:
            for face in shapes[ref].val().Faces(): sewing.Add(face.wrapped)
        sewing.Perform()
        result = cq.Shape.cast(sewing.SewedShape())
        shells = result.Shells()
        if len(shells) != 1: _error("曲面边界未能连接为一个壳，请检查间隙和公差。", "feature_failed")
        if feature.get("makeSolid", False):
            if not shells[0].Closed(): _error("曲面未封闭，无法形成实体。", "feature_failed")
            result = cq.Solid.makeSolid(shells[0])
        return output([result])
    if op == "body_edit":
        bodies = base.val().Solids(); indices = {b["index"] for b in feature["bodies"]}
        # Only bodies were selected. Preserve independent sheets, wires and
        # vertices instead of flattening the input through Solids() alone.
        unselected_geometry = []
        def preserve_non_solids(value):
            if value.ShapeType() in {"Compound", "CompSolid"}:
                for child in value: preserve_non_solids(child)
            elif value.ShapeType() != "Solid": unselected_geometry.append(value)
        preserve_non_solids(base.val())
        for ref in feature["bodies"]:
            if ref["index"] >= len(bodies) or body_signature(bodies[ref["index"]]) != ref["signature"]:
                _error("实体形状已变化，请重新选择实体。", "stale_body_selection")
        action = feature["action"]
        if action in {"keep", "delete"}: return output(unselected_geometry + [b for i,b in enumerate(bodies) if (i in indices) == (action == "keep")])
        changed = list(unselected_geometry)
        for i, body in enumerate(bodies):
            if i not in indices: changed.append(body); continue
            if action == "rotate":
                start, end = vector(feature["axisStart"]), vector(feature["axisEnd"])
                if math.dist(start, end) < 1e-7: _error("旋转轴的两个点不能重合。")
                transformed = body.rotate(start, end, number(feature["angle"]))
            else: transformed = body.translate(vector(feature["vector"]))
            changed += [body, transformed] if action == "copy" else [transformed]
        return output(changed)
    if op == "mesh_body":
        from collections import Counter
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
        points = [vector(p) for p in feature["vertices"]]
        edges = Counter(tuple(sorted((a,b))) for tri in feature["triangles"] for a,b in zip(tri, tri[1:]+tri[:1]))
        if any(count != 2 for count in edges.values()): _error("网格不是闭合流形，需先修复孔洞或重复面。", "open_mesh")
        sewing = BRepBuilderAPI_Sewing(1e-6)
        for tri in feature["triangles"]:
            wire = cq.Wire.makePolygon([cq.Vector(*points[i]) for i in tri], close=True)
            face = cq.Face.makeFromWires(wire)
            if face.Area() < 1e-10: _error("网格包含退化三角面。", "invalid_geometry")
            sewing.Add(face.wrapped)
        sewing.Perform(); sewed = cq.Shape.cast(sewing.SewedShape())
        solids = []
        for shell in sewed.Shells():
            if not shell.Closed(): _error("网格缝合后仍未闭合。", "open_mesh")
            solid = cq.Solid.makeSolid(shell)
            if solid.Volume() < 0: solid = cq.Shape.cast(solid.wrapped.Reversed())
            solids.append(solid)
        return output(solids)
    _error("未知的曲面或实体编辑操作。")


def surface_schema_properties(scalar, vec, selector):
    return {
        "faces": {"type": "array", "minItems": 1, "maxItems": 64, "items": selector("face")},
        "edges": {"type": "array", "minItems": 1, "maxItems": 64, "items": selector("edge")},
        "points": {"type": "array", "minItems": 2, "maxItems": 16, "items": {"type": "array", "minItems": 2, "maxItems": 16, "items": vec(3)}},
        "tolerance": scalar, "makeSolid": {"type": "boolean"}, "continuity": {"enum": ["position", "tangent", "curvature"]},
        "action": {"enum": ["keep", "delete", "translate", "copy", "rotate"]},
        "bodies": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "object", "properties": {"index": {"type": "integer", "minimum": 0, "maximum": 255}, "signature": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}, "required": ["index", "signature"], "additionalProperties": False}},
        "vertices": {"type": "array", "minItems": 4, "maxItems": 10000, "items": vec(3)},
        "triangles": {"type": "array", "minItems": 4, "maxItems": 10000, "items": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "integer", "minimum": 0}}},
    }
