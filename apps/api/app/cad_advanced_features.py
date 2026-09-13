"""Bounded OCCT construction helpers for explicit, serializable CAD operations.

No selectors, Python, paths or callbacks from a model are evaluated. Gear flanks
are spline interpolants of the involute, with circular addendum/root arcs and
radial root connectors; this is not a generated hob/trochoid root profile.
"""

from __future__ import annotations

import math

ADVANCED_OP_FIELDS = {
    "gear": ({"module", "teeth", "pressureAngle", "width"}, {"boreDiameter", "profileShift", "backlash", "origin"}),
    "spring": ({"meanDiameter", "wireDiameter", "pitch", "turns"}, {"lefthand", "origin"}),
    "rotate": ({"input", "axisStart", "axisEnd", "angle"}, set()),
    "linear_pattern": ({"input", "count", "vector"}, {"fuse"}),
    "circular_pattern": ({"input", "count", "axisStart", "axisEnd", "angle"}, {"fuse"}),
    "chamfer": ({"input", "length", "edges"}, {"length2"}),
    "shell": ({"input", "thickness", "faces"}, set()),
    "sweep": ({"path", "radius"}, set()),
    "loft": ({"sections"}, {"ruled"}),
}
REFERENCE_OPS = {"rotate", "linear_pattern", "circular_pattern", "chamfer", "shell"}
EDGE_SELECTORS = {"all": None, "parallelX": "|X", "parallelY": "|Y", "parallelZ": "|Z"}
FACE_SELECTORS = {"maxX": ">X", "minX": "<X", "maxY": ">Y", "minY": "<Y", "maxZ": ">Z", "minZ": "<Z"}


def _error(message, *, code="invalid_plan"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def validate_advanced_feature(feature, scalar, vector):
    """Validate shapes and expressions without importing the CAD kernel."""
    op = feature["op"]
    for key in ("module", "teeth", "pressureAngle", "width", "boreDiameter", "profileShift", "backlash",
                "meanDiameter", "wireDiameter", "pitch", "turns", "count", "length", "length2", "thickness"):
        if key in feature:
            scalar(feature[key])
    for key in ("lefthand", "fuse", "ruled"):
        if key in feature and type(feature[key]) is not bool:
            _error(f"{key} must be a boolean")
    if op in {"rotate", "circular_pattern"}:
        vector(feature["axisStart"], 3)
        vector(feature["axisEnd"], 3)
    if op == "chamfer" and (not isinstance(feature["edges"], str) or feature["edges"] not in EDGE_SELECTORS):
        _error("Chamfer edges must be all, parallelX, parallelY or parallelZ")
    if op == "shell":
        faces = feature["faces"]
        if (not isinstance(faces, list) or not 1 <= len(faces) <= 5
                or any(not isinstance(face, str) or face not in FACE_SELECTORS for face in faces)
                or len(set(faces)) != len(faces)):
            _error("Shell faces must be 1 to 5 unique maxX/minX/maxY/minY/maxZ/minZ selectors")
    if op == "sweep":
        path = feature["path"]
        if not isinstance(path, list) or not 2 <= len(path) <= 32:
            _error("Sweep path must contain 2 to 32 points")
        for point in path:
            vector(point, 3)
    if op == "loft":
        sections = feature["sections"]
        if not isinstance(sections, list) or not 2 <= len(sections) <= 12:
            _error("Loft requires 2 to 12 parallel XY sections")
        for section in sections:
            if not isinstance(section, dict) or set(section) not in ({"origin", "radius"}, {"origin", "width", "height"}):
                _error("A loft section requires origin and radius, or origin and width/height")
            vector(section["origin"], 3)
            for key in set(section) - {"origin"}:
                scalar(section[key])


def advanced_schema_properties(scalar, vec):
    circle = {"type": "object", "properties": {"origin": vec(3), "radius": scalar},
              "required": ["origin", "radius"], "additionalProperties": False}
    rectangle = {"type": "object", "properties": {"origin": vec(3), "width": scalar, "height": scalar},
                 "required": ["origin", "width", "height"], "additionalProperties": False}
    properties = {key: {**scalar} for key in ("module", "teeth", "pressureAngle", "width", "boreDiameter", "profileShift", "backlash",
                  "meanDiameter", "wireDiameter", "pitch", "turns", "count", "length", "length2", "thickness")}
    properties.update({key: {"type": "boolean"} for key in ("lefthand", "fuse", "ruled")})
    properties.update({"faces": {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True,
                                  "items": {"enum": list(FACE_SELECTORS)}},
                       "path": {"type": "array", "minItems": 2, "maxItems": 32, "items": vec(3)},
                       "sections": {"type": "array", "minItems": 2, "maxItems": 12, "items": {"oneOf": [circle, rectangle]}}})
    properties["teeth"]["description"] = "External spur gear integer tooth count 6..120; undercut combinations are rejected."
    # Copy scalar mappings before annotating; callers reuse the same scalar
    # descriptor in other unrelated fields.
    descriptions = {
        "pressureAngle": "Gear pressure angle in degrees, 14.5..30.",
        "profileShift": "Dimensionless profile shift -0.5..0.5; default 0.",
        "backlash": "Circular backlash in mm at pitch circle, 0..0.5 module; default 0.",
        "turns": "Open-ended constant-pitch spring turns, 0.25..40.",
        "count": "Pattern includes source instance; integer count 2..64. Full 360-degree pattern has no duplicate endpoint.",
        "thickness": "Nonzero shell thickness; negative offsets inward and removes selected extreme faces.",
    }
    for key, description in descriptions.items():
        properties[key] = {**scalar, "description": description}
    return properties


def _positive(number, value, name):
    result = number(value)
    if result <= 1e-6:
        _error(f"{name} must be greater than 0.000001 mm")
    return result


def _integer(number, value, name, low, high):
    result = number(value)
    if result != math.floor(result) or not low <= result <= high:
        _error(f"{name} must be an integer between {low} and {high}", code="resource_limit")
    return int(result)


def _axis(feature, vector):
    start, end = vector(feature["axisStart"]), vector(feature["axisEnd"])
    if math.dist(start, end) < 1e-7:
        _error("Rotation axis requires distinct points")
    return start, end


def gear_dimensions(feature, number):
    """Resolve standard external spur gear dimensions and reject undercut."""
    module = _positive(number, feature["module"], "Gear module")
    teeth = _integer(number, feature["teeth"], "Gear teeth", 6, 120)
    pressure = number(feature["pressureAngle"])
    shift = number(feature.get("profileShift", 0))
    if not 14.5 <= pressure <= 30 or not -.5 <= shift <= .5:
        _error("Gear pressure angle must be 14.5..30 degrees and profile shift -0.5..0.5")
    alpha = math.radians(pressure)
    # Avoid pretending a simple involute/root connector models hob undercut.
    if teeth * math.sin(alpha) ** 2 < 2 * (1 - shift) - 1e-8:
        _error("Gear parameters require an undercut/trochoid tooth root, which this spur gear operation does not support")
    pitch = module * teeth / 2
    base, root, tip = pitch * math.cos(alpha), pitch - module * (1.25 - shift), pitch + module * (1 + shift)
    width = _positive(number, feature["width"], "Gear width")
    bore = number(feature.get("boreDiameter", 0))
    backlash = number(feature.get("backlash", 0))
    if root <= 0 or not 0 <= bore < 2 * root - 1e-5 or not 0 <= backlash <= module * .5:
        _error("Gear bore must stay inside the root circle; backlash must be 0..0.5 module")
    half_pitch = math.pi / (2 * teeth) + 2 * shift * math.tan(alpha) / teeth - backlash / (2 * pitch)
    inv_alpha = math.tan(alpha) - alpha

    def half_angle(radius):
        t = math.sqrt(max(0.0, (radius / base) ** 2 - 1))
        return half_pitch + inv_alpha - (t - math.atan(t))

    start = max(root, base)
    if tip <= start or half_angle(tip) <= 1e-5 or half_angle(start) >= math.pi / teeth:
        _error("Gear parameters create a pointed or overlapping tooth")
    return {"module": module, "teeth": teeth, "pitch": pitch, "base": base, "root": root,
            "tip": tip, "width": width, "bore": bore, "start": start, "halfAngle": half_angle}


def _gear(cq, feature, number, vector):
    d = gear_dimensions(feature, number)
    edges = []
    half = d["halfAngle"]
    step = math.tau / d["teeth"]

    def point(radius, angle):
        return cq.Vector(radius * math.cos(angle), radius * math.sin(angle), 0)

    def line(a, b):
        if (a - b).Length > 1e-8:
            edges.append(cq.Edge.makeLine(a, b))

    def arc(radius, begin, end):
        edges.append(cq.Edge.makeThreePointArc(point(radius, begin), point(radius, (begin + end) / 2), point(radius, end)))

    # Sampling uniformly in involute parameter keeps the base-circle cusp
    # resolved; the interpolation tolerance is well below the display mesh.
    t0 = math.sqrt(max(0, (d["start"] / d["base"]) ** 2 - 1))
    t1 = math.sqrt((d["tip"] / d["base"]) ** 2 - 1)
    radii = [d["base"] * math.sqrt(1 + (t0 + (t1 - t0) * i / 24) ** 2) for i in range(25)]
    for index in range(d["teeth"]):
        center = index * step
        left = [point(radius, center - half(radius)) for radius in radii]
        right = [point(radius, center + half(radius)) for radius in reversed(radii)]
        line(point(d["root"], center - half(d["start"])), left[0])
        edges.append(cq.Edge.makeSpline(left, tol=1e-7))
        arc(d["tip"], center - half(d["tip"]), center + half(d["tip"]))
        edges.append(cq.Edge.makeSpline(right, tol=1e-7))
        line(right[-1], point(d["root"], center + half(d["start"])))
        arc(d["root"], center + half(d["start"]), center + step - half(d["start"]))
    wire = cq.Wire.assembleEdges(edges)
    shape = cq.Workplane("XY").newObject([wire]).toPending().extrude(d["width"])
    if d["bore"] > 0:
        shape = shape.cut(cq.Workplane("XY").circle(d["bore"] / 2).extrude(d["width"]))
    return shape.translate(vector(feature.get("origin", [0, 0, 0])))


def build_advanced_feature(cq, feature, shapes, number, vector):
    """Construct one feature; the caller performs final topology checks."""
    op = feature["op"]
    if op == "gear":
        return _gear(cq, feature, number, vector)
    if op == "spring":
        diameter = _positive(number, feature["meanDiameter"], "Spring mean diameter")
        wire = _positive(number, feature["wireDiameter"], "Spring wire diameter")
        pitch = _positive(number, feature["pitch"], "Spring pitch")
        turns = number(feature["turns"])
        if not .25 <= turns <= 40 or diameter <= 2 * wire or pitch <= wire * 1.05:
            _error("Spring requires 0.25..40 turns, mean diameter > 2 wire diameters and pitch > 1.05 wire diameters")
        helix = cq.Wire.makeHelix(pitch, pitch * turns, diameter / 2, lefthand=feature.get("lefthand", False))
        plane = cq.Plane(origin=helix.startPoint(), normal=helix.tangentAt(0))
        return cq.Workplane(plane).circle(wire / 2).sweep(helix, isFrenet=True).translate(vector(feature.get("origin", [0, 0, 0])))
    if op == "rotate":
        start, end = _axis(feature, vector)
        angle = number(feature["angle"])
        if abs(angle) > 360:
            _error("Rotation angle must be within +/-360 degrees")
        return shapes[feature["input"]].rotate(start, end, angle)
    if op in {"linear_pattern", "circular_pattern"}:
        count = _integer(number, feature["count"], "Pattern count", 2, 64)
        original = shapes[feature["input"]].val()
        if len(original.Faces()) * count > 10_000 or len(original.Solids()) * count > 256:
            _error("Pattern exceeds the face/solid complexity limit", code="resource_limit")
        copies = []
        if op == "linear_pattern":
            delta = vector(feature["vector"])
            if math.dist(delta, (0, 0, 0)) < 1e-7:
                _error("Pattern step vector must be nonzero")
            copies = [original.translate(tuple(component * i for component in delta)) for i in range(count)]
        else:
            start, end = _axis(feature, vector)
            angle = number(feature["angle"])
            if not 1e-6 < abs(angle) <= 360:
                _error("Circular pattern coverage must be nonzero and within +/-360 degrees")
            divisor = count if abs(abs(angle) - 360) < 1e-7 else count - 1
            copies = [original.rotate(start, end, angle * i / divisor) for i in range(count)]
        combined = copies[0].fuse(*copies[1:]) if feature.get("fuse", False) else cq.Compound.makeCompound(copies)
        return cq.Workplane("XY").newObject([combined])
    if op == "chamfer":
        length = _positive(number, feature["length"], "Chamfer length")
        length2 = _positive(number, feature["length2"], "Chamfer second length") if "length2" in feature else None
        return shapes[feature["input"]].edges(EDGE_SELECTORS[feature["edges"]]).chamfer(length, length2)
    if op == "shell":
        thickness = number(feature["thickness"])
        if abs(thickness) <= 1e-6:
            _error("Shell thickness must be nonzero; negative hollows inward")
        selector = " or ".join(FACE_SELECTORS[face] for face in feature["faces"])
        return shapes[feature["input"]].faces(selector).shell(thickness, kind="intersection")
    if op == "sweep":
        points = [cq.Vector(*vector(point)) for point in feature["path"]]
        if any((b - a).Length <= 1e-6 for a, b in zip(points, points[1:])):
            _error("Sweep path must not contain consecutive coincident points")
        radius = _positive(number, feature["radius"], "Sweep radius")
        directions = [(b - a).normalized() for a, b in zip(points, points[1:])]
        if any(first.dot(second) <= -1 + 1e-7 for first, second in zip(directions, directions[1:])):
            _error("Sweep path must not reverse through a 180-degree corner")
        path_edges = [cq.Edge.makeLine(a, b) for a, b in zip(points, points[1:])]
        # BRepCheck can report a swept solid as valid even when its tube
        # intersects itself. Reject intersecting/nearby non-adjacent path
        # segments before making the pipe; intended junctions need booleans.
        for index, edge in enumerate(path_edges):
            for later in path_edges[index + 2:]:
                if edge.distance(later) <= 2 * radius + 1e-6:
                    _error("Sweep path crosses itself or non-adjacent segments are within two profile radii")
        path = cq.Wire.makePolygon(points, close=False)
        plane = cq.Plane(origin=points[0], normal=points[1] - points[0])
        # OCCT's round transition can appear valid before STEP exchange while
        # changing volume on readback. A miter transition preserves the actual
        # swept material at the supported polyline corners.
        return cq.Workplane(plane).circle(radius).sweep(path, transition="right")
    if op == "loft":
        wires = []
        previous_z = None
        for section in feature["sections"]:
            origin = vector(section["origin"])
            if previous_z is not None and origin[2] <= previous_z + 1e-6:
                _error("Loft XY sections must have strictly increasing Z coordinates")
            previous_z = origin[2]
            profile = cq.Workplane("XY", origin=origin)
            if "radius" in section:
                profile = profile.circle(_positive(number, section["radius"], "Loft radius"))
            else:
                profile = profile.rect(_positive(number, section["width"], "Loft width"), _positive(number, section["height"], "Loft height"))
            wires.append(profile.val())
        return cq.Workplane("XY").newObject(wires).toPending().loft(ruled=feature.get("ruled", False))
    _error(f"Unsupported advanced CAD operation: {op}")
