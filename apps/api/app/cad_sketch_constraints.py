"""Strict sketch constraints checked independently after expression resolution."""
import math
import re

_FIELDS = {"horizontal": {"edge"}, "vertical": {"edge"}, "length": {"edge", "value"},
           "coincident": {"points"}, "fixed": {"points", "position"},
           **{kind: {"edges"} for kind in ("parallel", "perpendicular", "equal", "tangent", "concentric")},
           "angle": {"edges", "value"}, "radius": {"edge", "value"}}


def _error(message):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code="invalid_sketch_constraint")


def sketch_points(feature):
    points = {"start": feature["start"]}
    for index, segment in enumerate(feature["segments"]):
        points[f"{index}:to"] = segment["to"]
        if segment["type"] == "arc": points[f"{index}:through"] = segment["through"]
        if segment["type"] == "spline":
            points.update({f"{index}:through:{j}": point for j, point in enumerate(segment["through"])})
        if segment["type"] == "ellipse": points[f"{index}:center"] = segment["center"]
    return points


def _contours(feature):
    from .cad_profile_geometry import profile_contours
    return {contour["id"]: contour for contour in profile_contours(feature)}


def _edge_ref(ref, default, contours):
    contour, edge = default, ref
    if isinstance(ref, dict):
        if set(ref) != {"contourId", "edge"}: _error("边引用字段无效。")
        contour, edge = ref["contourId"], ref["edge"]
    if not isinstance(contour, str) or contour not in contours or type(edge) is not int or not 0 <= edge < len(contours[contour]["segments"]): _error("约束必须引用存在的轮廓边。")
    return contour, edge


def _point_ref(ref, default, contours):
    contour, point = default, ref
    if isinstance(ref, dict):
        if set(ref) != {"contourId", "point"}: _error("控制点引用字段无效。")
        contour, point = ref["contourId"], ref["point"]
    if not isinstance(contour, str) or contour not in contours or not isinstance(point, str) or point not in sketch_points(contours[contour]): _error("约束必须引用存在的轮廓控制点。")
    return contour, point


def validate_sketch_constraints(feature, scalar, vector):
    constraints = feature.get("sketchConstraints", [])
    if not isinstance(constraints, list) or len(constraints) > 128: _error("草图约束最多支持 128 项。")
    contours, ids, identities = _contours(feature), set(), set()
    for item in constraints:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item["type"] not in _FIELDS: _error("草图约束字段或类型无效。")
        kind = item["type"]
        if set(item) - {"contourId"} != _FIELDS[kind] | {"id", "type"}: _error("草图约束字段或类型无效。")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item["id"]) or item["id"] in ids: _error("草图约束 ID 必须有效且不重复。")
        ids.add(item["id"])
        default = item.get("contourId", "main")
        if not isinstance(default, str) or default not in contours: _error("约束所属轮廓不存在。")
        if "edge" in item:
            refs = [_edge_ref(item["edge"], default, contours)]
        elif "edges" in item:
            if not isinstance(item["edges"], list) or len(item["edges"]) != 2: _error("此约束需要两条不同的边。")
            refs = [_edge_ref(ref, default, contours) for ref in item["edges"]]
        else:
            if not isinstance(item["points"], list) or len(item["points"]) != (2 if kind == "coincident" else 1): _error("约束控制点数量无效。")
            refs = [_point_ref(ref, default, contours) for ref in item["points"]]
        if len(set(refs)) != len(refs): _error("约束不能重复引用同一边或点。")
        if kind in {"horizontal", "vertical", "length", "parallel", "perpendicular", "angle"} and any(contours[c]["segments"][i]["type"] != "line" for c, i in refs): _error("方向、长度和夹角约束必须引用直线边。")
        identity = (kind, *sorted(refs))
        if identity in identities: _error("不能重复设置同一草图约束。")
        identities.add(identity)
        if "value" in item: scalar(item["value"])
        if "position" in item: vector(item["position"], 2)


def _unit(vector):
    magnitude = math.hypot(*vector)
    if magnitude < 1e-9: _error("约束引用了退化方向。")
    return [v / magnitude for v in vector]


def _circle(a, through, b):
    ax, ay = a; bx, by = through; cx, cy = b
    d = 2 * (ax*(by-cy)+bx*(cy-ay)+cx*(ay-by))
    if abs(d) < 1e-10: _error("圆弧控制点共线，不能定义半径或圆心。")
    aa, bb, cc = ax*ax+ay*ay, bx*bx+by*by, cx*cx+cy*cy
    center = [(aa*(by-cy)+bb*(cy-ay)+cc*(ay-by))/d, (aa*(cx-bx)+bb*(ax-cx)+cc*(bx-ax))/d]
    return center, math.dist(a, center)


def _geometry(contour, index, number):
    def point(raw): return [number(value) for value in raw]
    segment = contour["segments"][index]; kind = segment["type"]
    a = point(contour["start"] if index == 0 else contour["segments"][index-1]["to"])
    b = point(segment["to"])
    result = {"kind": kind, "a": a, "b": b}
    if kind == "line":
        result.update(length=math.dist(a, b), ta=_unit([b[i]-a[i] for i in range(2)]))
        result["tb"] = result["ta"]
    elif kind == "arc":
        center, radius = _circle(a, point(segment["through"]), b)
        result.update(center=center, radius=radius, ta=_unit([-(a[1]-center[1]), a[0]-center[0]]), tb=_unit([-(b[1]-center[1]), b[0]-center[0]]))
    elif kind == "ellipse":
        center, radii = point(segment["center"]), point(segment["radii"])
        rotation = math.radians(number(segment["rotation"]))
        def tangent(angle):
            t = math.radians(number(angle)); x, y = -radii[0]*math.sin(t), radii[1]*math.cos(t)
            return _unit([x*math.cos(rotation)-y*math.sin(rotation), x*math.sin(rotation)+y*math.cos(rotation)])
        result.update(ta=tangent(segment["startAngle"]), tb=tangent(segment["endAngle"]))
        if abs(radii[0]-radii[1]) <= 1e-7: result.update(center=center, radius=radii[0])
    else:
        points = [a, *[point(p) for p in segment["through"]], b]
        first, last = _unit([points[1][i]-a[i] for i in range(2)]), _unit([b[i]-points[-2][i] for i in range(2)])
        if math.dist(a,b) <= 1e-7:
            h0, hn = math.dist(a,points[1]), math.dist(points[-2],b)
            first = last = _unit([last[i]*h0+first[i]*hn for i in range(2)])
        result.update(ta=first, tb=last)
    return result


def check_sketch_constraints(feature, number):
    contours = _contours(feature)
    for item in feature.get("sketchConstraints", []):
        kind, default = item["type"], item.get("contourId", "main")
        def geometry(ref):
            contour, index = _edge_ref(ref, default, contours)
            return _geometry(contours[contour], index, number)
        def point(ref):
            contour, key = _point_ref(ref, default, contours)
            return [number(value) for value in sketch_points(contours[contour])[key]]
        tolerance = 1e-5
        if kind in {"horizontal", "vertical", "length", "radius"}:
            edge = geometry(item["edge"])
            if kind == "horizontal": residual = abs(edge["a"][1]-edge["b"][1])
            elif kind == "vertical": residual = abs(edge["a"][0]-edge["b"][0])
            else:
                value = number(item["value"])
                if value <= 0: _error("长度或半径约束必须大于零。")
                if kind not in edge: _error("半径约束仅支持圆弧和圆。")
                residual = abs(edge[kind]-value)
        elif kind == "coincident": residual = math.dist(*[point(ref) for ref in item["points"]])
        elif kind == "fixed": residual = math.dist(point(item["points"][0]), [number(value) for value in item["position"]])
        else:
            a, b = [geometry(ref) for ref in item["edges"]]
            if kind in {"parallel", "perpendicular", "angle"}:
                dot = max(-1, min(1, sum(x*y for x,y in zip(a["ta"], b["ta"]))))
                if kind == "parallel": residual = abs(a["ta"][0]*b["ta"][1]-a["ta"][1]*b["ta"][0])
                elif kind == "perpendicular": residual = abs(dot)
                else:
                    angle = number(item["value"])
                    if not 0 <= angle <= 180: _error("夹角约束须在 0 至 180 度之间。")
                    residual = abs(math.acos(dot)-math.radians(angle))
            elif kind == "equal":
                key = "length" if a["kind"] == b["kind"] == "line" else "radius"
                if key not in a or key not in b: _error("相等约束只支持两条直线长度或两圆半径。")
                residual = abs(a[key]-b[key])
            elif kind == "concentric":
                if "center" not in a or "center" not in b: _error("同心约束只支持圆弧和圆。")
                residual = math.dist(a["center"], b["center"])
            else:
                candidates = [(pa,ta,pb,tb) for pa,ta in ((a["a"],a["ta"]),(a["b"],a["tb"])) for pb,tb in ((b["a"],b["ta"]),(b["b"],b["tb"])) if math.dist(pa,pb) <= tolerance]
                if not candidates: _error("相切约束须引用有共同端点的两条曲线。")
                residual = min(abs(ta[0]*tb[1]-ta[1]*tb[0]) for _,ta,_,tb in candidates)
        if not math.isfinite(residual) or residual > tolerance: _error(f"草图约束 {item['id']} 未满足（残差 {residual:.8g}），请先求解或修正冲突约束。")


def sketch_constraint_schema(scalar, vec):
    variants = []
    cid = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}
    edge = {"oneOf": [{"type": "integer", "minimum": 0, "maximum": 127}, {"type": "object", "properties": {"contourId": cid, "edge": {"type": "integer", "minimum": 0, "maximum": 127}}, "required": ["contourId", "edge"], "additionalProperties": False}]}
    point = {"oneOf": [{"type": "string", "maxLength": 128}, {"type": "object", "properties": {"contourId": cid, "point": {"type": "string", "maxLength": 128}}, "required": ["contourId", "point"], "additionalProperties": False}]}
    for kind, required in _FIELDS.items():
        fields = {"id": {"type": "string", "maxLength": 128}, "type": {"const": kind}, "contourId": cid}
        if "edge" in required: fields["edge"] = edge
        if "edges" in required: fields["edges"] = {"type": "array", "minItems": 2, "maxItems": 2, "items": edge, "uniqueItems": True}
        if "points" in required: fields["points"] = {"type": "array", "minItems": 2 if kind == "coincident" else 1, "maxItems": 2 if kind == "coincident" else 1, "items": point, "uniqueItems": True}
        if "value" in required: fields["value"] = scalar
        if "position" in required: fields["position"] = vec(2)
        variants.append({"type": "object", "properties": fields, "required": ["id", "type", *sorted(required)], "additionalProperties": False})
    return {"type": "array", "maxItems": 128, "items": {"oneOf": variants}}
