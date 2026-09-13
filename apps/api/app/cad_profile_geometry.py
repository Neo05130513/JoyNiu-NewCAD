"""Explicit planar profiles and exact OCCT curves; no inferred contours."""
from copy import deepcopy
import math
import re

CONTOUR_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _error(message, code="invalid_profile"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def profile_contours(feature):
    return [{"id":"main","role":"outer","start":feature["start"],"segments":feature["segments"]}, *feature.get("contours", [])]


def validate_segments(start, segments, scalar, vector, *, dimensions=2):
    vector(start, dimensions)
    if not isinstance(segments,list) or not 1 <= len(segments) <= 128:
        _error("轮廓或路径须包含 1 至 128 个显式图元。")
    for segment in segments:
        kind=segment.get("type") if isinstance(segment,dict) else None
        required={"line":{"type","to"},"arc":{"type","through","to"},"spline":{"type","through","to"},
                  "ellipse":{"type","center","radii","rotation","startAngle","endAngle","to"}}
        if kind == "ellipse" and dimensions == 3: required["ellipse"] |= {"xDir", "normal"}
        if not isinstance(kind,str) or kind not in required:
            _error("图元类型须为直线、圆弧、样条或平面椭圆。")
        optional={"radius"} if kind=="arc" else set()
        if required[kind]-segment.keys() or set(segment)-required[kind]-optional:_error("图元字段不完整或含未知字段。")
        vector(segment["to"],dimensions)
        if kind=="arc":
            vector(segment["through"],dimensions)
            if "radius" in segment:scalar(segment["radius"])
        elif kind=="spline":
            if not isinstance(segment["through"],list) or not 1 <= len(segment["through"]) <= 62:_error("样条须明确给出 1 至 62 个经过点。")
            for point in segment["through"]:vector(point,dimensions)
        elif kind=="ellipse":
            vector(segment["center"],dimensions);vector(segment["radii"],2)
            if dimensions == 3:
                vector(segment["xDir"],3);vector(segment["normal"],3)
            for key in ("rotation","startAngle","endAngle"):scalar(segment[key])


def validate_profile(feature, scalar, vector):
    validate_segments(feature["start"],feature["segments"],scalar,vector)
    contours=feature.get("contours",[])
    if not isinstance(contours,list) or len(contours)>32:_error("额外轮廓最多 32 个。")
    ids={"main"};outer={"main"}
    for contour in contours:
        if not isinstance(contour,dict) or contour.get("role") not in {"outer","hole"}:_error("额外轮廓须明确为外轮廓或孔洞。")
        fields={"id","role","start","segments"}|({"parent"} if contour["role"]=="hole" else set())
        if set(contour)!=fields:_error("轮廓字段无效；孔洞必须明确所属外轮廓。")
        if not isinstance(contour["id"],str) or not CONTOUR_ID.fullmatch(contour["id"]) or contour["id"] in ids:_error("轮廓 ID 须有效且不重复，main 为保留标识。")
        ids.add(contour["id"])
        if contour["role"]=="outer":outer.add(contour["id"])
        validate_segments(contour["start"],contour["segments"],scalar,vector)
    for contour in contours:
        if contour["role"]=="hole" and (not isinstance(contour["parent"],str) or contour["parent"] not in outer):_error("孔洞的 parent 必须引用已定义的外轮廓。")
    if sum(len(item["segments"]) for item in profile_contours(feature))>512:_error("一个草图最多 512 个图元。","resource_limit")


def segment_schema(scalar, vec, dimensions=2):
    variants=[]
    for kind in ["line","arc","spline","ellipse"]:
        fields={"type":{"const":kind},"to":vec(dimensions)}
        if kind=="arc":fields["through"]=vec(dimensions)
        if kind=="spline":fields["through"]={"type":"array","minItems":1,"maxItems":62,"items":vec(dimensions)}
        if kind=="ellipse":
            fields.update(center=vec(dimensions),radii=vec(2),rotation=scalar,startAngle=scalar,endAngle=scalar)
            if dimensions == 3: fields.update(xDir=vec(3),normal=vec(3))
        required=list(fields)
        if kind=="arc":fields["radius"]=scalar
        variants.append({"type":"object","properties":fields,"required":required,"additionalProperties":False})
    return {"type":"array","minItems":1,"maxItems":128,"items":{"oneOf":variants}}


def contours_schema(scalar, vec):
    variants=[]
    for role in ("outer","hole"):
        fields={"id":{"type":"string","pattern":CONTOUR_ID.pattern},"role":{"const":role},"start":vec(2),"segments":segment_schema(scalar,vec)}
        if role=="hole":fields["parent"]={"type":"string","pattern":CONTOUR_ID.pattern}
        variants.append({"type":"object","properties":fields,"required":list(fields),"additionalProperties":False})
    return {"type":"array","maxItems":32,"items":{"oneOf":variants}}


def hermite_spline(cq, points):
    """Chord-length cubic Hermite interpolation, also used by the SVG editor."""
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfReal, TColStd_Array1OfInteger
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    lengths=[(b-a).Length for a,b in zip(points,points[1:])]
    if any(length<=1e-7 for length in lengths):_error("样条相邻经过点不能重合。")
    secants=[(b-a).multiply(1/length) for a,b,length in zip(points,points[1:],lengths)]
    derivatives=[secants[0]]+[(secants[i-1].multiply(lengths[i])+secants[i].multiply(lengths[i-1])).multiply(1/(lengths[i-1]+lengths[i])) for i in range(1,len(points)-1)]+[secants[-1]]
    if (points[0]-points[-1]).Length<=1e-7:
        tangent=(secants[-1].multiply(lengths[0])+secants[0].multiply(lengths[-1])).multiply(1/(lengths[-1]+lengths[0]))
        derivatives[0]=derivatives[-1]=tangent
    controls=[points[0]]
    for i,length in enumerate(lengths):controls.extend([points[i]+derivatives[i].multiply(length/3),points[i+1]-derivatives[i+1].multiply(length/3),points[i+1]])
    poles=TColgp_Array1OfPnt(1,len(controls))
    for i,point in enumerate(controls,1):poles.SetValue(i,point.toPnt())
    knots=TColStd_Array1OfReal(1,len(points));mult=TColStd_Array1OfInteger(1,len(points));t=0.
    for i in range(len(points)):
        if i:t+=lengths[i-1]
        knots.SetValue(i+1,t);mult.SetValue(i+1,4 if i in (0,len(points)-1) else 3)
    curve=Geom_BSplineCurve(poles,knots,mult,3,False)
    # The Hermite spans share exact first derivatives. Reduce the redundant
    # Bezier knots so OCCT also recognizes C1 continuity for a sweep spine.
    for index in range(curve.NbKnots()-1, 1, -1):
        if not curve.RemoveKnot(index, 2, 1e-9): _error("样条连续性构造失败。")
    return cq.Edge(BRepBuilderAPI_MakeEdge(curve).Edge())


def ellipse_point(segment, angle, number):
    center=[number(value) for value in segment["center"]];rx,ry=[number(value) for value in segment["radii"]]
    rotation=math.radians(number(segment["rotation"]));a=math.radians(angle)
    return [center[0]+rx*math.cos(a)*math.cos(rotation)-ry*math.sin(a)*math.sin(rotation),
            center[1]+rx*math.cos(a)*math.sin(rotation)+ry*math.sin(a)*math.cos(rotation)]


def curve_edge(cq, start, segment, number, plane=None):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.Geom import Geom_Ellipse
    from OCP.gp import gp_Ax2
    def point(raw):
        values=tuple(number(value) for value in raw)
        return plane.toWorldCoords(values) if plane else cq.Vector(*values)
    a,b=point(start),point(segment["to"]);kind=segment["type"]
    if kind=="line":
        if (b-a).Length<=1e-7:_error("直线端点不能重合。")
        return cq.Edge.makeLine(a,b)
    if kind=="arc":
        edge=cq.Edge.makeThreePointArc(a,point(segment["through"]),b)
        if "radius" in segment and not math.isclose(edge.radius(),number(segment["radius"]),rel_tol=1e-6,abs_tol=1e-5):_error("圆弧的经过点不满足指定半径。","arc_radius_mismatch")
        return edge
    if kind=="spline":return hermite_spline(cq,[a,*[point(value) for value in segment["through"]],b])
    rx,ry=[number(value) for value in segment["radii"]];begin,end=number(segment["startAngle"]),number(segment["endAngle"])
    if min(rx,ry)<=1e-7 or not 1e-7<abs(end-begin)<=360+1e-7:_error("椭圆半轴须大于零，弧角须非零且不超过 360 度。")
    if plane is None:
        from .cad_topology import custom_workplane
        spatial=custom_workplane(cq,{"frame":{"origin":segment["center"],"xDir":segment["xDir"],"normal":segment["normal"]}},lambda raw:tuple(number(v) for v in raw),{},{}).plane
        local_a,local_b=spatial.toLocalCoords(a),spatial.toLocalCoords(b)
        if abs(local_a.z)>1e-5 or abs(local_b.z)>1e-5:_error("空间椭圆起止点不在指定平面。")
        local={key:value for key,value in segment.items() if key not in {"xDir","normal"}}
        local.update(center=[0,0],to=[local_b.x,local_b.y])
        return curve_edge(cq,[local_a.x,local_a.y],local,number,spatial)
    if math.dist([number(v) for v in start],ellipse_point(segment,begin,number))>1e-5 or math.dist([number(v) for v in segment["to"]],ellipse_point(segment,end,number))>1e-5:_error("椭圆起止点与中心、半轴及角度不一致。")
    rotation=math.radians(number(segment["rotation"]));direction=plane.xDir.multiply(math.cos(rotation))+plane.yDir.multiply(math.sin(rotation))
    correction=0.
    if ry>rx:direction=plane.xDir.multiply(-math.sin(rotation))+plane.yDir.multiply(math.cos(rotation));correction=math.pi/2
    curve=Geom_Ellipse(gp_Ax2(point(segment["center"]).toPnt(),plane.zDir.toDir(),direction.toDir()),max(rx,ry),min(rx,ry))
    low,high=sorted([math.radians(begin)-correction,math.radians(end)-correction])
    edge=cq.Edge(BRepBuilderAPI_MakeEdge(curve,low,high).Edge())
    if end<begin:edge.wrapped.Reverse()
    return edge


def contour_wire(cq, contour, number, plane=None, *, close=True):
    current=contour["start"];edges=[];roles=[]
    for i,segment in enumerate(contour["segments"]):
        edge=curve_edge(cq,current,segment,number,plane)
        if segment["type"] == "spline":
            # Exact span boundaries keep OCCT surface integration and sweep
            # construction from treating C1 knots as one smooth patch.
            from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
            curve=edge._geomAdaptor().BSpline()
            for j in range(1,curve.NbKnots()):
                span=cq.Edge(BRepBuilderAPI_MakeEdge(curve,curve.Knot(j),curve.Knot(j+1)).Edge())
                edges.append(span);roles.append(((i,"span",j-1),span))
        else: edges.append(edge);roles.append((i,edge))
        current=segment["to"]
    if close and math.dist([number(v) for v in current],[number(v) for v in contour["start"]])>1e-7:
        edge=curve_edge(cq,current,{"type":"line","to":contour["start"]},number,plane);edges.append(edge);roles.append(("closing",edge))
    wire=cq.Wire.assembleEdges(edges)
    if not wire.isValid() or (close and not wire.IsClosed()):_error("轮廓未闭合或无法构成有效曲线。")
    return wire,roles


def profile_faces(cq, feature, number, plane):
    loops={item["id"]:item for item in profile_contours(feature)}
    wires={key:contour_wire(cq,item,number,plane) for key,item in loops.items()}
    filled={key:cq.Face.makeFromWires(wire[0]) for key,wire in wires.items()}
    for face in filled.values():
        if not face.isValid() or face.Area()<=1e-9:_error("草图轮廓自交、退化或没有有效面积。")
    result=[]
    for key,loop in loops.items():
        if loop["role"]!="outer":continue
        outer=wires[key][0];holes=[other for other in loops if loops[other].get("parent")==key]
        for hole in holes:
            if outer.distance(wires[hole][0])<=1e-6 or abs(filled[key].intersect(filled[hole]).Area()-filled[hole].Area())>1e-6:_error("孔洞须严格位于其所属外轮廓内部，不能接触边界。")
        for i,hole in enumerate(holes):
            for other in holes[i+1:]:
                if filled[hole].intersect(filled[other]).Area()>1e-7 or wires[hole][0].distance(wires[other][0])<=1e-6:_error("同一外轮廓的孔洞不能重叠、嵌套或相切。")
        face=cq.Face.makeFromWires(outer,[wires[hole][0] for hole in holes])
        if not face.isValid():_error("带孔洞轮廓未生成有效平面。")
        roles=[(role,edge) for role,edge in wires[key][1]]
        roles.extend((("hole",hole,role),edge) for hole in holes for role,edge in wires[hole][1])
        result.append({"id":key,"loop":loop,"face":face,"outer":outer,"holes":[wires[hole][0] for hole in holes],"holeIds":holes,"roles":roles})
    for i,region in enumerate(result):
        for other in result[i+1:]:
            if region["face"].intersect(other["face"]).Area()>1e-7 or region["face"].distance(other["face"])<=1e-6:_error("独立外轮廓不能重叠或相接，请调整轮廓或使用显式布尔操作。")
    return result


def build_profile_regions(cq, feature, plane, number, vector):
    from .cad_history import profile_builder
    parts=[]
    for region in profile_faces(cq,feature,number,plane.plane):
        local={**feature,"id":feature["id"] if region["id"]=="main" else f"{feature['id']}:region:{region['id']}","start":region["loop"]["start"],"segments":region["loop"]["segments"]}
        builder,shape,generated=profile_builder(cq,local,plane,cq.Workplane(plane.plane).newObject([region["outer"]]),number,vector,region["roles"],face=region["face"])
        parts.append((local,builder,generated,shape,plane))
    value=parts[0][3].val() if len(parts)==1 else cq.Compound.makeCompound([part[3].val() for part in parts])
    return cq.Workplane("XY").newObject([value]),parts
