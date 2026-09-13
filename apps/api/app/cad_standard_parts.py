"""Bounded real standard-part geometry, generated from editable nominal dimensions.

Manufacturer boundary/ball dimensions are stored with sources in the shared
catalogue. Raceway conformity, running clearance and cage sheet thickness are
explicit design parameters, not claimed proprietary manufacturer internals.
"""
from __future__ import annotations
import json
import math
from pathlib import Path

CATALOG = {item["catalogId"]: item for item in json.loads(Path(__file__).with_name("cad_standard_part_catalog.json").read_text())}
STANDARD_PART_OP_FIELDS = {"standard_part": ({"catalogId", "dimensions"}, set())}


def _error(message, code="invalid_standard_part"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def standard_part_schema_properties(scalar, vec=None):
    keys = sorted({key for part in CATALOG.values() for key in part["dimensions"]})
    return {"catalogId": {"enum": list(CATALOG)}, "dimensions": {"type": "object", "properties": {key: scalar for key in keys}, "additionalProperties": False, "maxProperties": len(keys)}}


def validate_standard_part(feature, scalar, vector=None):
    part = CATALOG.get(feature.get("catalogId")) if isinstance(feature.get("catalogId"), str) else None
    if part is None: _error("请选择库中支持的标准件规格。")
    dimensions = feature.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(part["dimensions"]): _error("标准件尺寸字段不完整或含不支持的字段。")
    for value in dimensions.values(): scalar(value)


def _dimensions(feature, number):
    validate_standard_part(feature, lambda value: None)
    d = {key: number(value) for key, value in feature["dimensions"].items()}
    if any(not math.isfinite(value) or value <= 0 or value > 2000 for value in d.values()): _error("标准件尺寸须为 0 至 2000 mm 范围内的正数。")
    kind = CATALOG[feature["catalogId"]]["kind"]
    r, length = d["outerDiameter"] / 2, d["length"]
    if kind == "socket_screw":
        if not (0.2 <= d["pitch"] <= 4 and 1 <= d["threadLength"] / d["pitch"] <= 80): _error("螺纹须为 0.2–4 mm 螺距且 1–80 圈。", "resource_limit")
        if d["threadLength"] > length or d["pitch"] >= r or d["headDiameter"] <= 2*r or d["socketAcrossFlats"] / math.sqrt(3) >= d["headDiameter"]/2-d["headChamfer"] or d["socketDepth"] >= d["headLength"] or d["tipChamfer"] >= r or d["headChamfer"] >= d["headLength"]/2: _error("螺钉的螺纹、头部、倒角或内六角尺寸互相冲突。")
    elif kind == "bearing":
        ball, pitch = d["ballDiameter"], d["pitchDiameter"]
        if d["ballCount"] != int(d["ballCount"]) or not 3 <= d["ballCount"] <= 24: _error("滚珠数须为 3 至 24 的整数。")
        if not (d["innerDiameter"] < d["innerRingDiameter"] < pitch < d["outerRingDiameter"] < d["outerDiameter"]): _error("轴承内外圈与节圆尺寸次序无效。")
        if not (0.501 <= d["grooveRatio"] <= 0.57 and d["radialClearance"] < ball * .03 and d["cageThickness"] < ball * .2): _error("轴承滚道曲率、游隙或保持架厚度无效。")
        if length <= ball*1.15 or pitch*math.sin(math.pi/d["ballCount"]) <= ball+d["cageThickness"]: _error("滚珠互相干涉或轴承宽度不足。")
        if pitch-ball-d["radialClearance"] <= d["innerDiameter"]+2*d["edgeRadius"] or pitch+ball+d["radialClearance"] >= d["outerDiameter"]-2*d["edgeRadius"]: _error("滚道将穿透内外圈壁厚。")
        if min(d["innerRingDiameter"]-d["innerDiameter"],d["outerDiameter"]-d["outerRingDiameter"],length) <= 2*d["edgeRadius"]: _error("轴承倒角大于圈壁厚。")
    elif kind == "washer" and d["innerDiameter"] >= 2*r: _error("垫圈内径须小于外径。")
    elif kind == "pin" and d["endChamfer"] >= min(r,length/2): _error("圆柱销端部尺寸大于半径或半长。")
    return kind, d


def _annulus(cq, inner, outer, height, z=0):
    return cq.Workplane("XY", origin=(0,0,z)).circle(outer).circle(inner).extrude(height).val()


def _thread_cutter(cq, radius, pitch, height):
    # ISO basic external 60 degree profile: P/8 crest, rounded H/6 root.
    # This is one continuous OCCT helix, never stacked rings or a mesh.
    h = math.sqrt(3)*pitch/2; extra = pitch*.1
    points = [(radius+extra,0,-7*pitch/16-extra/math.sqrt(3)), (radius-5*h/8,0,-pitch/8),
              (radius-5*h/8,0,pitch/8), (radius+extra,0,7*pitch/16+extra/math.sqrt(3))]
    edges = [cq.Edge.makeLine(cq.Vector(*points[0]),cq.Vector(*points[1])),
             cq.Edge.makeThreePointArc(cq.Vector(*points[1]),cq.Vector(radius-17*h/24,0,0),cq.Vector(*points[2])),
             cq.Edge.makeLine(cq.Vector(*points[2]),cq.Vector(*points[3])),cq.Edge.makeLine(cq.Vector(*points[3]),cq.Vector(*points[0]))]
    profile = cq.Wire.assembleEdges(edges).translate((0,0,-pitch))
    path = cq.Wire.makeHelix(pitch,height+pitch,radius,center=(0,0,-pitch))
    cutter = cq.Solid.sweep(profile,[],path,isFrenet=True)
    # Exact end stop also produces the visible partial run-out at the shoulder.
    return cutter.intersect(cq.Solid.makeCylinder(radius+pitch,height))


def _screw(cq, d):
    r, length = d["outerDiameter"]/2,d["length"]
    shaft = cq.Workplane("XY").circle(r).extrude(length).faces("<Z").edges().chamfer(d["tipChamfer"]).val()
    shaft = shaft.cut(_thread_cutter(cq,r,d["pitch"],d["threadLength"]))
    head = cq.Workplane("XY", origin=(0,0,length)).circle(d["headDiameter"]/2).extrude(d["headLength"]).faces(">Z").edges().chamfer(d["headChamfer"]).val()
    socket = cq.Workplane("XY", origin=(0,0,length+d["headLength"]-d["socketDepth"])).polygon(6,2*d["socketAcrossFlats"]/math.sqrt(3)).extrude(d["socketDepth"]+1).val()
    return shaft.fuse(head).cut(socket).clean()


def _bearing(cq, d):
    width, pitch, ball = d["length"],d["pitchDiameter"]/2,d["ballDiameter"]/2
    inner = cq.Workplane("XY").circle(d["innerRingDiameter"]/2).circle(d["innerDiameter"]/2).extrude(width).edges("%CIRCLE").fillet(d["edgeRadius"]).val()
    outer = cq.Workplane("XY").circle(d["outerDiameter"]/2).circle(d["outerRingDiameter"]/2).extrude(width).edges("%CIRCLE").fillet(d["edgeRadius"]).val()
    groove_radius = d["ballDiameter"]*d["grooveRatio"]
    offset = groove_radius-ball-d["radialClearance"]/4
    # Curvature centers differ, retaining finite radial clearance at the groove bottoms.
    inner = inner.cut(cq.Solid.makeTorus(pitch+offset,groove_radius,(0,0,width/2))).clean()
    outer = outer.cut(cq.Solid.makeTorus(pitch-offset,groove_radius,(0,0,width/2))).clean()
    centers = [(pitch*math.cos(2*math.pi*i/int(d["ballCount"])),pitch*math.sin(2*math.pi*i/int(d["ballCount"])),width/2) for i in range(int(d["ballCount"]))]
    balls = [cq.Solid.makeSphere(ball,pnt=center,angleDegrees1=-90,angleDegrees2=90) for center in centers]
    # Two perforated sheet cages, with actual spherical pockets and axial rivets.
    # Sheet thickness and running clearance are explicit editable design values.
    sheet = d["cageThickness"]; halfheight = ball*.68
    ring_i = d["innerRingDiameter"]/2+.2; ring_o=d["outerRingDiameter"]/2-.2
    cages=[]
    for sign in (-1,1):
        z=width/2+sign*halfheight-(sheet if sign<0 else 0)
        cage=_annulus(cq,ring_i,ring_o,sheet,z)
        for center in centers:
            cavity=cq.Solid.makeSphere(ball+.12,pnt=center,angleDegrees1=-90,angleDegrees2=90)
            cage=cage.cut(cavity)
        cages.append(cage.clean())
    # Rivets between pockets connect the two cage halves into a physical carrier.
    carrier=cages[0]
    for i in range(int(d["ballCount"])):
        a=2*math.pi*(i+.5)/int(d["ballCount"])
        carrier=carrier.fuse(cq.Solid.makeCylinder(sheet*.65,2*(halfheight+sheet),(pitch*math.cos(a),pitch*math.sin(a),width/2-halfheight-sheet)))
    carrier=carrier.fuse(cages[1]).clean()
    return cq.Compound.makeCompound([inner,outer,*balls,carrier])


def build_standard_part(cq, feature, number, vector=None):
    kind,d=_dimensions(feature,number)
    if kind=="bearing": shape=_bearing(cq,d)
    elif kind=="socket_screw": shape=_screw(cq,d)
    elif kind=="washer": shape=_annulus(cq,d["innerDiameter"]/2,d["outerDiameter"]/2,d["length"])
    else:
        # ISO 2338 c is the axial end length, with a 15 degree edge slope.
        r=d["outerDiameter"]/2; c=d["endChamfer"]; a=c*math.tan(math.radians(15))
        shape=cq.Workplane("XZ").polyline([(0,0),(r-a,0),(r,c),(r,d["length"]-c),(r-a,d["length"]),(0,d["length"])]).close().revolve(360,(0,0),(0,1)).val()
    expected=int(d["ballCount"])+3 if kind=="bearing" else 1
    if not shape.isValid() or len(shape.Solids())!=expected: _error("标准件未能形成预期的独立有效实体。", "invalid_geometry")
    return cq.Workplane("XY").newObject([shape])
