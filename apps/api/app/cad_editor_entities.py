"""Persisted construction geometry, independent sketches and model annotations.

These records are part of the versioned CAD plan. They contain bounded data,
never code, filesystem paths, or external URLs. Construction references are
resolved in dependency order and copied into a feature only for construction.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

EDITOR_PLAN_FIELDS = {"references", "sketches", "annotations", "bodyStates"}
SKETCH_FIELDS = {"plane", "origin", "frame", "start", "segments", "contours", "sketchConstraints", "planeReference", "planeSource", "planeAttachment"}
BASE_FRAMES = {
    "XY": {"origin": [0, 0, 0], "xDir": [1, 0, 0], "normal": [0, 0, 1]},
    "XZ": {"origin": [0, 0, 0], "xDir": [1, 0, 0], "normal": [0, -1, 0]},
    "YZ": {"origin": [0, 0, 0], "xDir": [0, 1, 0], "normal": [1, 0, 0]},
}
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
PMI_KINDS = {"distance", "angle", "radius", "diameter", "note", "datum", "tolerance", "surface_finish"}


def error(message, code="invalid_editor_entity"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def _text(value, limit=200):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        error("名称或标注文字格式无效。")


def _records(plan, key, limit):
    records = plan.get(key, [])
    if not isinstance(records, list) or len(records) > limit:
        error(f"{key} 超出允许的记录数量。")
    seen = set()
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]) or item["id"] in seen:
            error("编辑记录须有唯一有效 ID。")
        seen.add(item["id"])
        if "label" in item:
            _text(item["label"])
    return records


def validate_editor_entities(plan, scalar, vector, profile_validator=None):
    references = _records(plan, "references", 128)
    planes = set(BASE_FRAMES)
    for ref in references:
        kind, mode = ref.get("kind"), ref.get("mode")
        fields = {"id", "kind", "mode", "label"}
        if kind == "plane":
            if mode == "frame":
                fields.add("frame")
                frame = ref.get("frame")
                if not isinstance(frame, dict) or set(frame) != {"origin", "xDir", "normal"}:
                    error("平面需要原点、X 方向和法线。")
                for value in frame.values(): vector(value, 3)
            elif mode in {"offset", "angle"}:
                fields |= {"parent", "offset", "angle", "axis"}
                if ref.get("parent") not in planes: error("基准平面必须引用已有平面。")
                scalar(ref.get("offset", 0)); scalar(ref.get("angle", 0))
                if ref.get("axis", "x") not in {"x", "y"}: error("旋转轴需为平面 X 或 Y 轴。")
            elif mode == "three_points":
                fields.add("points")
                if not isinstance(ref.get("points"), list) or len(ref["points"]) != 3: error("三点平面需要三个不同且不共线的点。")
                for point in ref["points"]: vector(point, 3)
            else: error("请选择平面构造方法。")
            planes.add(ref["id"])
        elif kind == "axis" and mode == "two_points":
            fields |= {"start", "end"}
            vector(ref.get("start"), 3); vector(ref.get("end"), 3)
        elif kind == "helix":
            fields |= {"radius", "pitch", "turns", "frame", "lefthand"}
            for key in ("radius", "pitch", "turns"): scalar(ref.get(key))
            if type(ref.get("lefthand", False)) is not bool: error("螺旋线旋向无效。")
            if not isinstance(ref.get("frame"), dict) or set(ref["frame"]) != {"origin", "xDir", "normal"}: error("螺旋线需要完整坐标系。")
            for value in ref["frame"].values(): vector(value, 3)
        elif kind == "point" and mode == "coordinates":
            fields.add("point"); vector(ref.get("point"), 3)
        else: error("参考几何类型或构造方法无效。")
        if set(ref) - fields: error("参考几何包含未知字段。")
    sketches = _records(plan, "sketches", 128)
    for sketch in sketches:
        if set(sketch) - SKETCH_FIELDS - {"id", "label", "construction"}: error("独立草图包含未知字段。")
        if "construction" in sketch and type(sketch["construction"]) is not bool: error("草图辅助线设置无效。")
        if sketch.get("planeReference") and sketch["planeReference"] not in planes: error("草图引用的平面不存在。")
        if profile_validator: profile_validator(sketch)
    sketch_ids = {s["id"] for s in sketches}
    for feature in plan.get("features", []):
        if feature.get("sketchId") and feature["sketchId"] not in sketch_ids: error("特征引用的独立草图不存在。")
        if feature.get("planeReference") and feature["planeReference"] not in planes: error("特征引用的基准平面不存在。")
    for note in _records(plan, "annotations", 256):
        if set(note) - {"id", "kind", "label", "text", "points", "position", "value", "upper", "lower", "datum", "symbol", "references", "hidden", "anchors"}: error("PMI 标注包含未知字段。")
        if note.get("kind") not in PMI_KINDS: error("PMI 标注类型无效。")
        _text(note.get("text", ""), 2000)
        for key in ("datum", "symbol"): _text(note.get(key, ""), 100)
        points = note.get("points", [])
        if not isinstance(points, list) or not len(points) <= 4: error("PMI 拾取点超出限制。")
        for point in points: vector(point, 3)
        vector(note.get("position"), 3)
        for key in ("value", "upper", "lower"):
            if key in note: scalar(note[key])
        required = {"distance": 2, "angle": 3, "radius": 3, "diameter": 3}.get(note["kind"], 0)
        if required and len(points) != required: error("PMI 标注还未选齐测量点。")
        if type(note.get("hidden", False)) is not bool: error("标注可见性无效。")
        refs = note.get("references", [])
        if not isinstance(refs, list) or len(refs) > 4: error("PMI 几何引用超出限制。")
        from .cad_topology import validate_topology_selector
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("kind") not in {"face", "edge"}: error("PMI 必须引用真实边或面。")
            validate_topology_selector(ref, ref["kind"], {f["id"] for f in plan.get("features", [])})
        from .cad_pmi import validate_pmi_anchors
        validate_pmi_anchors(note)
    for body in _records(plan, "bodyStates", 256):
        if set(body) - {"id", "label", "signature", "hidden", "color", "binding"}: error("实体显示设置包含未知字段。")
        if not isinstance(body.get("signature"), str) or not re.fullmatch(r"[a-f0-9]{64}", body["signature"]): error("实体设置需要几何签名。")
        if type(body.get("hidden", False)) is not bool: error("实体可见性无效。")
        if "color" in body and (not isinstance(body["color"], str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", body["color"])): error("实体颜色格式无效。")
        from .cad_body_state import validate_body_binding
        validate_body_binding(body)


def _unit(value):
    length = math.sqrt(sum(v*v for v in value))
    if length < 1e-9: error("参考方向不能为零，三点不能共线。")
    return [v/length for v in value]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def resolved_references(plan, number):
    frames = deepcopy(BASE_FRAMES)
    resolved = []
    def vec(p): return [number(v) for v in p]
    for ref in plan.get("references", []):
        if ref["kind"] == "plane":
            if ref["mode"] == "frame": frame = {key: vec(value) for key, value in ref["frame"].items()}
            elif ref["mode"] == "three_points":
                a, b, c = map(vec, ref["points"])
                x = _unit([b[i]-a[i] for i in range(3)])
                frame = {"origin": a, "xDir": x, "normal": _unit(_cross(x, [c[i]-a[i] for i in range(3)]))}
            else:
                frame = deepcopy(frames[ref["parent"]])
                frame["origin"] = [frame["origin"][i]+number(ref.get("offset", 0))*frame["normal"][i] for i in range(3)]
                a = math.radians(number(ref.get("angle", 0)))
                axis = frame["xDir"] if ref.get("axis", "x") == "x" else _unit(_cross(frame["normal"], frame["xDir"]))
                def rotate(v):
                    cross = _cross(axis, v); dot = sum(axis[i]*v[i] for i in range(3))
                    return [v[i]*math.cos(a)+cross[i]*math.sin(a)+axis[i]*dot*(1-math.cos(a)) for i in range(3)]
                frame["normal"] = rotate(frame["normal"]); frame["xDir"] = rotate(frame["xDir"])
            frame["normal"] = _unit(frame["normal"])
            # Orthogonalize X as OCCT does, while rejecting a parallel X axis.
            frame["xDir"] = _unit(_cross(_cross(frame["normal"], frame["xDir"]), frame["normal"]))
            frames[ref["id"]] = frame
            resolved.append({**deepcopy(ref), "frame": frame})
        elif ref["kind"] == "axis":
            start, end = vec(ref["start"]), vec(ref["end"])
            _unit([end[i]-start[i] for i in range(3)])
            resolved.append({**deepcopy(ref), "start": start, "end": end})
        elif ref["kind"] == "helix":
            radius, pitch, turns = (number(ref[key]) for key in ("radius", "pitch", "turns"))
            if radius <= 1e-7 or pitch <= 1e-7 or not 0 < turns <= 100 or pitch*turns > 1e6: error("螺旋线半径与螺距必须为正，圈数不超过 100。")
            frame = {key: vec(value) for key, value in ref["frame"].items()}
            frame["normal"] = _unit(frame["normal"]); frame["xDir"] = _unit(_cross(_cross(frame["normal"],frame["xDir"]),frame["normal"]))
            resolved.append({**deepcopy(ref), "radius":radius,"pitch":pitch,"turns":turns,"frame":frame})
        else: resolved.append({**deepcopy(ref), "point": vec(ref["point"])})
    return frames, resolved


def resolve_feature_entities(feature, plan, number, sketch_frame_resolver=None):
    result = deepcopy(feature)
    if result.get("sketchId"):
        sketch = next((s for s in plan.get("sketches", []) if s["id"] == result["sketchId"]), None)
        if sketch is None: error("独立草图已不存在。")
        for key in SKETCH_FIELDS: result.pop(key, None)
        result.update({key: deepcopy(value) for key, value in sketch.items() if key in SKETCH_FIELDS})
    if result.get("planeReference"):
        frames, _ = resolved_references(plan, number)
        result["frame"] = deepcopy(frames[result["planeReference"]]); result["plane"] = "custom"
        for key in ("origin", "planeSource", "planeAttachment"): result.pop(key, None)
    def source_sketch(key):
        sketch = next((s for s in plan.get("sketches", []) if s["id"] == key), None)
        if sketch is None: error("路径或截面引用的独立草图不存在。")
        return sketch
    def source_frame(sketch):
        if sketch_frame_resolver:
            return sketch_frame_resolver(sketch)
        if sketch.get("planeReference"):
            return resolved_references(plan, number)[0][sketch["planeReference"]]
        if sketch.get("frame"):
            return {key: [number(v) for v in value] for key, value in sketch["frame"].items()}
        return {**deepcopy(BASE_FRAMES[sketch.get("plane", "XY")]), "origin": [number(v) for v in sketch.get("origin", [0,0,0])]}
    if result.get("op") == "profile_loft":
        for section in result.get("sections", []):
            if not section.get("sketchId"): continue
            sketch = source_sketch(section["sketchId"])
            for key in ("start", "segments", "contours", "sketchConstraints"):
                section.pop(key, None)
                if key in sketch: section[key] = deepcopy(sketch[key])
            section["frame"] = source_frame(sketch)
    if result.get("op") == "profile_sweep" and result.get("path", {}).get("sketchId"):
        path = result["path"]; sketch = source_sketch(path["sketchId"]); frame = source_frame(sketch)
        x, normal = _unit(frame["xDir"]), _unit(frame["normal"]); y = _unit(_cross(normal, x))
        def world(point):
            u, v = map(number, point)
            return [frame["origin"][i]+x[i]*u+y[i]*v for i in range(3)]
        path["start"] = world(sketch["start"]); path["segments"] = []
        for segment in sketch["segments"]:

            item = {**deepcopy(segment), "to": world(segment["to"])}
            if segment["type"] == "ellipse": item.update(center=world(segment["center"]), xDir=x, normal=normal)
            if "through" in segment: item["through"] = [world(p) for p in segment["through"]] if segment["type"] == "spline" else world(segment["through"])
            path["segments"].append(item)
    if result.get("axisReference"):
        _, references = resolved_references(plan, number)
        axis = next((ref for ref in references if ref["id"] == result["axisReference"] and ref["kind"] == "axis"), None)
        if axis is None: error("引用的参考轴不存在。")
        if result.get("op") == "profile_revolve":
            source = source_sketch(result["sketchId"]) if result.get("sketchId") else result
            frame = source_frame(source)
            x, normal = _unit(frame["xDir"]), _unit(frame["normal"]); y = _unit(_cross(normal, x))
            def local(point):
                delta = [point[i]-frame["origin"][i] for i in range(3)]
                if abs(sum(delta[i]*normal[i] for i in range(3))) > 1e-6: error("旋转参考轴必须位于草图平面内。")
                return [sum(delta[i]*direction[i] for i in range(3)) for direction in (x,y)]
            result["axisStart"], result["axisEnd"] = local(axis["start"]), local(axis["end"])
        else:
            result["axisStart"], result["axisEnd"] = axis["start"], axis["end"]
    return result


def annotation_measurement(note, number):
    points = [[number(v) for v in p] for p in note.get("points", [])]
    kind = note["kind"]
    if kind == "distance": return math.dist(*points)
    if kind == "angle":
        a, origin, b = points
        va = _unit([a[i]-origin[i] for i in range(3)]); vb = _unit([b[i]-origin[i] for i in range(3)])
        return math.degrees(math.acos(max(-1, min(1, sum(va[i]*vb[i] for i in range(3))))))
    if kind in {"radius", "diameter"}:
        a, b, c = points
        cross = _cross([b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)])
        twice_area = math.sqrt(sum(v*v for v in cross))
        if twice_area < 1e-10: error("半径标注的三点不能共线。")
        radius = math.dist(a,b)*math.dist(b,c)*math.dist(c,a)/(2*twice_area)
        return radius*(2 if kind == "diameter" else 1)
    return number(note["value"]) if "value" in note else None


def editor_entities_schema():
    # The strict semantic validator above is authoritative. JSON schema keeps
    # bounded collections discoverable without allowing arbitrary top-level data.
    return {key: {"type": "array", "maxItems": 256 if key in {"annotations", "bodyStates"} else 128, "items": {"type": "object"}}
            for key in sorted(EDITOR_PLAN_FIELDS)}
