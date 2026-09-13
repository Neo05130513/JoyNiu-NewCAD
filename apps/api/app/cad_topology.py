"""Version-bound OCCT topology, never coordinate-nearest or index-only selection."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import re

from .cad_plan import PlanValidationError, expression_names

MAX_PICK_FACES = 2000
MAX_PICK_EDGES = 6000
MAX_PICK_TRIANGLES = 200000
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_topology_selector(value, kind, seen=None):
    required = {"kind", "sourceFeatureId", "geometryVersion", "index", "signature"}
    if (not isinstance(value, dict) or not required.issubset(value) or set(value) - required - {"binding"}
            or value.get("kind") != kind or not isinstance(value.get("sourceFeatureId"), str)
            or not _ID.fullmatch(value["sourceFeatureId"]) or type(value.get("index")) is not int
            or not 0 <= value["index"] < (MAX_PICK_EDGES if kind == "edge" else MAX_PICK_FACES)
            or any(not isinstance(value.get(key), str) or not _HASH.fullmatch(value[key]) for key in ("geometryVersion", "signature"))):
        raise PlanValidationError("拓扑选择必须包含完整的实体版本、来源特征和几何签名。", code="invalid_topology_selector")
    if "binding" in value:
        binding = value["binding"]
        if (not isinstance(binding, dict) or set(binding) != {"version", "key"} or type(binding.get("version")) is not int
                or binding["version"] != 1 or not isinstance(binding.get("key"), str) or not _HASH.fullmatch(binding["key"])):
            raise PlanValidationError("持久拓扑绑定格式无效。", code="invalid_topology_selector")
    if seen is not None and value["sourceFeatureId"] not in seen:
        raise PlanValidationError("拓扑来源必须是当前计划中的前置特征。", code="invalid_topology_selector")


def feature_prefix(plan, feature_id):
    result = deepcopy(plan)
    if feature_id != plan.get("result"): result.pop("bodyStates", None)
    index = next((i for i, item in enumerate(result["features"]) if item["id"] == feature_id), None)
    if index is None:
        raise PlanValidationError("预览特征不存在或已被抑制。", code="invalid_preview_feature")
    result["features"] = result["features"][:index + 1]
    result["result"] = feature_id
    available = {feature["id"] for feature in result["features"]}
    used_sketches = set()
    used_planes = set()
    for feature in result["features"]:
        links = [feature]
        if feature.get("op") == "profile_sweep": links.append(feature.get("path", {}))
        if feature.get("op") == "profile_loft": links.extend(feature.get("sections", []))
        for link in links:
            if link.get("sketchId"): used_sketches.add(link["sketchId"])
            if link.get("planeReference"): used_planes.add(link["planeReference"])
            if link.get("axisReference"): used_planes.add(link["axisReference"])
            if link.get("curveReference"): used_planes.add(link["curveReference"])
    sketches = {item["id"]: item for item in result.get("sketches", [])}
    if used_sketches - sketches.keys(): raise PlanValidationError("预览使用的独立草图不存在。", code="invalid_preview_feature")
    for key in used_sketches:
        sketch = sketches[key]
        if sketch.get("planeSource", {}).get("sourceFeatureId", feature_id) not in available:
            raise PlanValidationError("预览草图引用的源特征不存在或位于当前历史之后。", code="invalid_preview_feature")
        if sketch.get("planeReference"): used_planes.add(sketch["planeReference"])
    if "sketches" in result: result["sketches"] = [item for item in result["sketches"] if item["id"] in used_sketches]
    references = {item["id"]: item for item in result.get("references", [])}
    pending = list(used_planes)
    while pending:
        key = pending.pop()
        if key in {"XY", "XZ", "YZ"}: continue
        if key not in references: raise PlanValidationError("预览使用的参考平面不存在。", code="invalid_preview_feature")
        parent = references[key].get("parent")
        if parent and parent not in used_planes: used_planes.add(parent); pending.append(parent)
    if "references" in result: result["references"] = [item for item in result["references"] if item["id"] in used_planes]
    if "annotations" in result:
        # A later feature's PMI does not belong to an earlier history preview.
        # Drop that whole display record instead of pretending its broken
        # references became an unattached, accepted measurement.
        result["annotations"] = [note for note in result["annotations"] if all(ref.get("sourceFeatureId") in available for ref in note.get("references", []))]
    names = set(result.get("parameters", {}))
    needed = set()
    non_dimensions = {"id", "op", "label", "input", "inputs", "plane", "planeSource", "type", "kind", "sourceFeatureId", "geometryVersion", "signature", "edges", "faces"}
    def scan(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in non_dimensions: scan(child)
        elif isinstance(value, list):
            for child in value: scan(child)
        elif isinstance(value, str):
            try: needed.update(expression_names(value) & names)
            except PlanValidationError: pass
    scan(result["features"])
    scan(result.get("references", []))
    scan(result.get("sketches", []))
    scan(result.get("annotations", []))
    while True:
        before = set(needed)
        for name in before: scan(result["parameters"][name].get("expression"))
        if needed == before: break
    result["parameters"] = {key: item for key, item in result.get("parameters", {}).items() if key in needed}
    return result


def _numbers(values):
    return [round(float(value), 8) + 0.0 for value in values]


def _bounds(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
    values = box.Get()
    return {"min": _numbers(values[:3]), "max": _numbers(values[3:])}


def edge_signature(edge):
    points = [_numbers(point.toTuple()) for point in edge.sample(17)[0]] if edge.Length() > 1e-9 else [_numbers(edge.Center().toTuple())]
    return digest({"type": edge.geomType(), "length": round(edge.Length(), 8), "bounds": _bounds(edge),
                   "points": min(points, list(reversed(points)))})


def face_signature(face):
    return digest({"type": face.geomType(), "area": round(face.Area(), 8), "center": _numbers(face.Center().toTuple()),
                   "bounds": _bounds(face), "edges": sorted(edge_signature(edge) for edge in face.Edges())})


def topology_identity(plan, feature_id, shape):
    import cadquery as cq
    from .cad_history import strip_history_metadata
    value = shape.val() if hasattr(shape, "val") else shape
    edges, faces = value.Edges(), value.Faces()
    if len(edges) > MAX_PICK_EDGES or len(faces) > MAX_PICK_FACES:
        raise PlanValidationError("实体拓扑过于复杂，直接拾取最多支持 2000 个面和 6000 条边。", code="resource_limit")
    edge_signatures, face_signatures = [edge_signature(edge) for edge in edges], [face_signature(face) for face in faces]
    prefix = feature_prefix(strip_history_metadata(plan), feature_id)
    version = digest({"kernel": cq.__version__, "features": prefix["features"], "parameters": prefix["parameters"],
                      "result": feature_id, "faces": sorted(face_signatures), "edges": sorted(edge_signatures)})
    return {"version": version, "edges": edges, "faces": faces, "edgeSignatures": edge_signatures, "faceSignatures": face_signatures}


def resolve_topology_selection(plan, feature_id, shape, selectors, kind, *, max_faces=5):
    if type(max_faces) is not int or not 1 <= max_faces <= 64: raise PlanValidationError("面选择数量限制无效。", code="invalid_topology_selector")
    limit = 64 if kind == "edge" else max_faces
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= limit:
        raise PlanValidationError(f"请选择 1 至 {limit} 个有效的{('边' if kind == 'edge' else '面')}。", code="invalid_topology_selector")
    identity = topology_identity(plan, feature_id, shape)
    signatures = identity[kind + "Signatures"]
    counts, picked, seen = Counter(signatures), [], set()
    for selector in selectors:
        validate_topology_selector(selector, kind)
        if selector["sourceFeatureId"] != feature_id or selector["geometryVersion"] != identity["version"]:
            raise PlanValidationError("拾取的实体版本已变化，请刷新模型后重新选择边或面。", code="stale_topology")
        index = selector["index"]
        if index >= len(signatures) or signatures[index] != selector["signature"]:
            raise PlanValidationError("拾取的拓扑索引与几何签名不符，请重新选择。", code="stale_topology")
        if counts[selector["signature"]] != 1:
            raise PlanValidationError("该位置存在几何完全重合的拓扑，无法唯一确认选择，请先消除重合实体。", code="ambiguous_topology")
        if index in seen:
            raise PlanValidationError("不能重复选择同一条边或同一个面。", code="invalid_topology_selector")
        seen.add(index); picked.append(identity["edges" if kind == "edge" else "faces"][index])
    return picked


def planar_face_frame(face):
    if face.geomType() != "PLANE":
        return None
    normal = face.normalAt().toTuple()
    direction = face._geomAdaptor().Position().XDirection()
    return {"origin": list(face.Center().toTuple()), "xDir": [direction.X(), direction.Y(), direction.Z()], "normal": list(normal)}


def custom_workplane(cq, feature, vector, plan, shapes, history=None):
    frame = {key: vector(value) for key, value in feature["frame"].items()}
    normal, xdir = cq.Vector(*frame["normal"]), cq.Vector(*frame["xDir"])
    if normal.Length < 1e-8 or xdir.Length < 1e-8 or abs(normal.normalized().dot(xdir.normalized())) > 1e-7:
        raise PlanValidationError("草图坐标系的法向和 X 方向须非零且垂直。", code="invalid_sketch_frame")
    source = feature.get("planeSource")
    if source:
        reference = source["sourceFeatureId"]
        face = (history.resolve(plan, feature, reference, shapes[reference], [source], "face", "planeSource")[0][0]
                if history else resolve_topology_selection(plan, reference, shapes[reference], [source], "face")[0])
        if history:
            frame = history.attached_frame(feature, reference, face, frame)
            normal, xdir = cq.Vector(*frame["normal"]), cq.Vector(*frame["xDir"])
        actual = planar_face_frame(face)
        if actual is None:
            raise PlanValidationError("只能在平面面上建立平面草图。", code="invalid_sketch_frame")
        source_normal = cq.Vector(*actual["normal"]).normalized()
        offset = cq.Vector(*frame["origin"]) - cq.Vector(*actual["origin"])
        if abs(abs(source_normal.dot(normal.normalized())) - 1) > 1e-7 or abs(source_normal.dot(offset)) > 1e-5:
            raise PlanValidationError("草图坐标系不在拾取的源平面上，请重新选面。", code="invalid_sketch_frame")
    return cq.Workplane(cq.Plane(origin=frame["origin"], xDir=xdir, normal=normal))


def export_topology_preview(plan, shape, output_dir):
    from .geometry import Mesh, mesh_to_glb
    identity = topology_identity(plan, plan["result"], shape)
    mesh, face_ids, faces, edges = Mesh(), [], [], []
    def selector(kind, index):
        return {"kind": kind, "sourceFeatureId": plan["result"], "geometryVersion": identity["version"],
                "index": index, "signature": identity[kind + "Signatures"][index]}
    for index, face in enumerate(identity["faces"]):
        face_id = f"face:{index}"
        vertices, triangles = face.tessellate(0.05, 0.1)
        face_triangle_start = len(face_ids)
        if len(face_ids) + len(triangles) > MAX_PICK_TRIANGLES:
            raise PlanValidationError("实体网格超过 200000 个三角面，无法生成直接编辑预览。", code="resource_limit")
        # Keep the kernel's indexed topology instead of repeating six floats
        # per vertex for every adjacent triangle. Vertices are shared only
        # within this face, so sharp face boundaries remain sharp and each
        # triangle still has its exact OCCT face identity.
        offset = len(mesh.positions) // 3
        normals = [[0.0, 0.0, 0.0] for _ in vertices]
        for vertex in vertices: mesh.positions.extend(float(v) for v in vertex.toTuple())
        for triangle in triangles:
            a, b, c = (vertices[i] for i in triangle)
            cross = (b-a).cross(c-a)
            if cross.Length < 1e-12: continue
            mesh.indices.extend(offset+i for i in triangle)
            for i in triangle:
                for axis, value in enumerate(cross.toTuple()): normals[i][axis] += value
            face_ids.append(face_id)
        for normal in normals:
            length = math.sqrt(sum(v*v for v in normal))
            mesh.normals.extend(v/length if length else 0.0 for v in normal)
        if len(face_ids) == face_triangle_start and face.Area() > 1e-9:
            raise PlanValidationError("内核未能为某个实体面生成有效网格，请保留原模型并检查该特征。", code="invalid_geometry")
        frame = planar_face_frame(face)
        faces.append({"id": face_id, "surfaceType": face.geomType(), "planar": bool(frame), "selector": selector("face", index),
                      **({"origin": frame["origin"], "normal": frame["normal"], "xAxis": frame["xDir"]} if frame else {})})
    total_points = 0
    for index, edge in enumerate(identity["edges"]):
        if edge.Length() <= 1e-9: continue
        points = edge.sample(2 if edge.geomType() == "LINE" else 0.05)[0]
        if edge.IsClosed() and points: points.append(points[0])
        total_points += len(points)
        if len(points) > 4096 or total_points > 300000:
            raise PlanValidationError("边曲线超过预览采样上限。", code="resource_limit")
        edges.append({"id": f"edge:{index}", "points": [float(value) for point in points for value in point.toTuple()], "selector": selector("edge", index)})
    if not mesh.indices:
        raise PlanValidationError("内核无法生成非空的面网格。", code="invalid_geometry")
    from .cad_surface_features import topology_bodies, topology_surface_bodies
    payload = {"geometryVersion": identity["version"], "sourceFeatureId": plan["result"],
               "mesh": {"positions": mesh.positions, "normals": mesh.normals, "indices": mesh.indices, "faceIds": face_ids},
               "faces": faces, "edges": edges, "bodies": topology_bodies(shape), "surfaceBodies": topology_surface_bodies(shape), "bounds": _bounds(shape.val()),
               "counts": {"faces": len(faces), "edges": len(edges), "triangles": len(face_ids)}}
    if len(json.dumps(payload)) > 32 * 1024 * 1024:
        raise PlanValidationError("直接编辑预览超过 32 MiB 上限。", code="resource_limit")
    path = output_dir / "preview.glb"
    path.write_bytes(mesh_to_glb(mesh, name="Manual transaction preview"))
    return payload, path
