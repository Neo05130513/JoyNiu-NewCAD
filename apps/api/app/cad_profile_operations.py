"""Exact arbitrary-section sweep and loft, with explicit placement and holes."""
import math
from .cad_profile_geometry import validate_profile, validate_segments, profile_faces, contour_wire, segment_schema, contours_schema

PROFILE_OP_FIELDS = {
    "profile_sweep": ({"plane", "start", "segments", "path"}, {"origin", "frame", "planeSource", "planeAttachment", "sketchConstraints", "contours", "sketchId", "planeReference", "isFrenet", "transition"}),
    "profile_loft": ({"sections"}, {"ruled"}),
}


def _error(message, code="invalid_profile"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def validate_profile_operation(feature, scalar, vector):
    from .cad_sketch_constraints import validate_sketch_constraints
    if feature["op"] == "profile_sweep":
        path = feature["path"]
        if not isinstance(path, dict): _error("扫掠路径须明确起点及三维图元，或选择参考曲线。")
        if "curveReference" in path:
            from .cad_profile_geometry import CONTOUR_ID
            if set(path) != {"curveReference"} or not isinstance(path["curveReference"], str) or not CONTOUR_ID.fullmatch(path["curveReference"]): _error("参考曲线路径不能混入离散路径或草图快照。")
        else:
            if set(path) - {"sketchId"} != {"start", "segments"}: _error("扫掠路径须明确起点及三维图元。")
            validate_segments(path["start"], path["segments"], scalar, vector, dimensions=3)
        if "isFrenet" in feature and type(feature["isFrenet"]) is not bool: _error("扫掠坐标系选项无效。")
        if feature.get("transition", "transformed") not in {"transformed", "right", "round"}: _error("扫掠转角处理方式无效。")
    else:
        sections = feature["sections"]
        if not isinstance(sections, list) or not 2 <= len(sections) <= 12: _error("放样须明确提供 2 至 12 个截面。")
        ids = set()
        for section in sections:
            from .cad_profile_geometry import CONTOUR_ID
            if not isinstance(section, dict) or not {"id", "frame", "start", "segments"}.issubset(section) or set(section) - {"id", "frame", "start", "segments", "contours", "sketchConstraints", "sketchId"}: _error("放样截面字段无效。")
            if not isinstance(section["id"], str) or not CONTOUR_ID.fullmatch(section["id"]) or section["id"] in ids: _error("截面 ID 须唯一且有效。")
            ids.add(section["id"])
            frame = section["frame"]
            if not isinstance(frame, dict) or set(frame) != {"origin", "xDir", "normal"}: _error("放样截面需要完整坐标系。")
            for value in frame.values(): vector(value, 3)
            validate_profile(section, scalar, vector); validate_sketch_constraints(section, scalar, vector)
        if "ruled" in feature and type(feature["ruled"]) is not bool: _error("放样直纹选项无效。")
        if sum(len(section["segments"])+sum(len(c["segments"]) for c in section.get("contours", [])) for section in sections) > 1024: _error("放样截面总图元不能超过 1024。", "resource_limit")


def profile_operation_properties(scalar, vec):
    from .cad_sketch_constraints import sketch_constraint_schema
    frame = {"type":"object","properties":{key:vec(3) for key in ("origin","xDir","normal")},"required":["origin","xDir","normal"],"additionalProperties":False}
    reference = {"type":"string","pattern":"^[A-Za-z][A-Za-z0-9_]{0,63}$"}
    section = {"id":{"type":"string","maxLength":64},"sketchId":reference,"frame":frame,"start":vec(2),"segments":segment_schema(scalar,vec),"contours":contours_schema(scalar,vec),"sketchConstraints":sketch_constraint_schema(scalar,vec)}
    return {"path":{"type":"object","oneOf":[{"type":"object","properties":{"sketchId":reference,"start":vec(3),"segments":segment_schema(scalar,vec,3)},"required":["start","segments"],"additionalProperties":False}, {"type":"object","properties":{"curveReference":reference},"required":["curveReference"],"additionalProperties":False}]},
            "isFrenet":{"type":"boolean"},"transition":{"enum":["transformed","right","round"]},"ruled":{"type":"boolean"},
            "sections":{"type":"array","minItems":2,"maxItems":12,"items":{"type":"object","properties":section,"required":["id","frame","start","segments"],"additionalProperties":False}}}


def _generated(cq, builder, roles):
    mapping = {}
    for key, edge in roles:
        candidates = list(builder.Generated(edge.wrapped))
        mapping[key] = [face for raw in candidates if not raw.IsNull() for face in cq.Shape.cast(raw).Faces()]
    for key, method in (("start", "FirstShape"), ("end", "LastShape")):
        raw = getattr(builder, method)()
        mapping[key] = [] if raw.IsNull() else cq.Shape.cast(raw).Faces()
    return mapping


def _cut_holes(cq, outer, holes, mappings):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    mapping = dict(mappings[0])
    value = outer
    for hole, names in zip(holes, mappings[1:]):
        builder = BRepAlgoAPI_Cut(value.wrapped, hole.wrapped); builder.Build()
        if not builder.IsDone(): _error("孔洞沿路径或截面无法生成，请检查相交与过渡。", "feature_failed")
        combined = {**mapping, **names}; mapped = {}
        for key, faces in combined.items():
            mapped[key] = []
            for face in faces:
                changed = list(builder.Modified(face.wrapped))
                if changed: mapped[key].extend(f for raw in changed for f in cq.Shape.cast(raw).Faces())
                elif not builder.IsDeleted(face.wrapped): mapped[key].append(face)
        value, mapping = cq.Shape.cast(builder.Shape()), mapped
    return value, mapping


def reference_curve_wire(cq, key, canonical, number):
    """A real OCCT helix, in the saved reference frame; no sampled spine."""
    from .cad_editor_entities import resolved_references
    _, references = resolved_references(canonical, number)
    ref = next((item for item in references if item["id"] == key and item["kind"] == "helix"), None)
    if ref is None: _error("扫掠引用的螺旋线不存在。", "invalid_editor_entity")
    frame = ref["frame"]
    placement = cq.Plane(origin=frame["origin"], xDir=frame["xDir"], normal=frame["normal"])
    wire = cq.Wire.makeHelix(ref["pitch"], ref["pitch"]*ref["turns"], ref["radius"], lefthand=ref.get("lefthand",False))
    return wire.moved(cq.Location(placement))


def build_profile_operation(cq, feature, number, vector, plane=None, canonical=None):
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell, BRepOffsetAPI_ThruSections
    from .cad_sketch_constraints import check_sketch_constraints
    result, semantic = [], {}
    if feature["op"] == "profile_sweep":
        check_sketch_constraints(feature, number)
        if "curveReference" in feature["path"]:
            path = reference_curve_wire(cq, feature["path"]["curveReference"], canonical or {}, number)
            start = path.Edges()[0].startPoint()
            tangent = path.Edges()[0].tangentAt(0)
        else:
            path, path_roles = contour_wire(cq, feature["path"], number, close=False)
            # contour_wire retains exact Hermite span boundaries for OCCT.
            spine_edges = [edge for _, edge in path_roles]
            if len(spine_edges) > 512: _error("扫掠路径最多支持 512 个连续曲线段。", "resource_limit")
            path = cq.Wire.assembleEdges(spine_edges)
            start = cq.Vector(*vector(feature["path"]["start"]))
            tangent = path_roles[0][1].tangentAt(0)
        if abs((start-plane.plane.origin).dot(plane.plane.zDir)) > 1e-5 or abs(abs(tangent.normalized().dot(plane.plane.zDir))-1) > 1e-6: _error("扫掠截面必须位于路径起点平面，并垂直于路径起始方向；请调整截面坐标系。")
        sections = [profile_faces(cq, feature, number, plane.plane)]
    else:
        sections = []
        for section in feature["sections"]:
            check_sketch_constraints(section, number)
            from .cad_topology import custom_workplane
            section_plane = custom_workplane(cq, {"plane":"custom","frame":section["frame"]}, vector, {}, {})
            sections.append(profile_faces(cq, section, number, section_plane.plane))
        identities = [{r["id"]:tuple(sorted(r["holeIds"])) for r in section} for section in sections]
        if any(identity != identities[0] for identity in identities[1:]): _error("各放样截面的外轮廓和孔洞 ID 必须一一对应。")
    for first in sections[0]:
        matched = [next(r for r in section if r["id"] == first["id"]) for section in sections]
        solids, mappings = [], []
        for hole_id in [None, *first["holeIds"]]:
            wires = [r["outer"] if hole_id is None else r["holes"][r["holeIds"].index(hole_id)] for r in matched]
            prefix = (first["id"], "outer" if hole_id is None else f"hole:{hole_id}")
            roles = [(f"{prefix[0]}/{prefix[1]}/edge:{role}", edge) for role, edge in first["roles"] if (hole_id is None and not (isinstance(role,tuple) and role[0] == "hole")) or (isinstance(role,tuple) and role[0] == "hole" and role[1] == hole_id)]
            # Wire assembly makes new edge wrappers; feed the builder's actual
            # input topology to Generated(), retaining only exact unique roles.
            from .cad_topology import edge_signature
            originals = [(role, edge_signature(edge)) for role, edge in roles]
            roles = [(role, edge) for edge in wires[0].Edges() for role, signature in originals if edge_signature(edge) == signature]
            if feature["op"] == "profile_sweep":
                builder = BRepOffsetAPI_MakePipeShell(path.wrapped)
                builder.SetMode(feature.get("isFrenet", False)); builder.SetTransitionMode(cq.Solid._transModeDict[feature.get("transition", "transformed")])
                builder.SetForceApproxC1(True)
                builder.Add(wires[0].wrapped, False, False); builder.Build()
                if not builder.IsDone() or not builder.MakeSolid(): _error("显式截面和路径无法扫掠成实体。", "feature_failed")
            else:
                builder = BRepOffsetAPI_ThruSections(True, feature.get("ruled",False))
                # Equal edge counts retain the user's explicit correspondence;
                # differing section types require OCCT's actual wire matching.
                builder.CheckCompatibility(len({len(wire.Edges()) for wire in wires}) > 1)
                for wire in wires: builder.AddWire(wire.wrapped)
                builder.Build()
                if not builder.IsDone(): _error("给定截面不能生成有效放样实体。", "feature_failed")
            value = cq.Shape.cast(builder.Shape())
            if not value.isValid() or not value.Solids(): _error("扫掠或放样生成了无效实体，请检查自交。", "invalid_geometry")
            mapping = _generated(cq,builder,roles)
            mapping = {f"{prefix[0]}/{prefix[1]}/{key}" if key in {"start","end"} else key:values for key,values in mapping.items()}
            solids.append(value); mappings.append(mapping)
        value, mapping = _cut_holes(cq,solids[0],solids[1:],mappings)
        result.append(value)
        semantic.update(mapping)
    value = result[0] if len(result)==1 else cq.Compound.makeCompound(result)
    return cq.Workplane("XY").newObject([value]), semantic
