"""Owned STEP documents, exact topology measurements, assemblies and drawings.

CAD work runs in bounded child processes. Persistent STEP files are the common
source for inspection, assembly, interference and drawing operations; a mesh or
bounding box is never substituted for a missing B-Rep.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

MAX_STEP_BYTES = 20 * 1024 * 1024
_ID = re.compile(r"design_[a-f0-9]{32}\Z")
_LOCK = threading.RLock()
_KERNEL_SLOTS = threading.BoundedSemaphore(2)


class DesignError(ValueError):
    pass


class DesignNotFound(DesignError):
    pass


def _number(value, label="数值", limit=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        raise DesignError(f"{label}须为 ±{limit} 范围内的有限数值。")
    return float(value)


def _vector(value, label="坐标"):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise DesignError(f"{label}需要 X、Y、Z 三个数值。")
    return [_number(v, label) for v in value]


def _name(value, default="未命名设计"):
    if not isinstance(value, str) or len(value) > 180 or any(ord(c) < 32 for c in value):
        raise DesignError("名称须为不超过 180 字符的文字。")
    return value.strip() or default


def _write(path, value):
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class CadDesignWorkspace:
    def __init__(self, root: str | Path, *, timeout=100):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def _owner(self, owner):
        if not isinstance(owner, str) or not owner:
            raise DesignNotFound("请先登录。")
        folder = self.root / hashlib.sha256(owner.encode()).hexdigest()
        folder.mkdir(exist_ok=True)
        return folder

    def _directory(self, owner, design_id):
        if not isinstance(design_id, str) or not _ID.fullmatch(design_id):
            raise DesignNotFound("设计文件不存在。")
        path = self._owner(owner) / design_id
        if not (path / "design.json").is_file():
            raise DesignNotFound("设计文件不存在。")
        return path

    def get(self, owner, design_id):
        return json.loads((self._directory(owner, design_id) / "design.json").read_text())

    def list(self, owner, file_id=None):
        result = []
        for path in self._owner(owner).glob("design_*/design.json"):
            try:
                item = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            if file_id is None or item.get("fileId") == file_id:
                result.append({k: item.get(k) for k in ("id", "fileId", "name", "kind", "createdAt", "metrics", "sha256")})
        return sorted(result, key=lambda x: x["createdAt"], reverse=True)[:500]

    def _run(self, operation, data, directory):
        if not _KERNEL_SLOTS.acquire(blocking=False):
            raise DesignError("几何服务正在处理其他任务，请稍后重试。")
        request, response = directory / f"request-{uuid4().hex}.json", directory / f"result-{uuid4().hex}.json"
        try:
            _write(request, {"operation": operation, "data": data, "directory": str(directory), "response": str(response)})
            try:
                completed = subprocess.run([sys.executable, "-m", "app.cad_design_workspace", str(request)],
                    cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, timeout=self.timeout,
                    env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
            except subprocess.TimeoutExpired:
                raise DesignError("几何运算超时；原设计未改变，请简化零件或减少实例后重试。") from None
            if not response.is_file():
                raise DesignError("几何内核未完成运算，原设计未改变。")
            result = json.loads(response.read_text())
            if not result.get("ok"):
                raise DesignError(result.get("error") or "几何运算失败。")
            return result["result"]
        finally:
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)
            _KERNEL_SLOTS.release()

    @contextmanager
    def _new(self, owner, name, file_id, kind):
        name = _name(name)
        if not isinstance(file_id, str) or not file_id.strip() or len(file_id) > 160:
            raise DesignError("需要有效的工作区文件 ID。")
        design_id = "design_" + uuid4().hex
        folder = self._owner(owner) / design_id
        folder.mkdir()
        record = {"id": design_id, "fileId": file_id, "name": name, "kind": kind, "units": "mm",
                  "createdAt": datetime.now(timezone.utc).isoformat(), "artifacts": {}}
        try:
            yield folder, record
            for artifact in record.get("artifacts", {}).values():
                artifact["sha256"] = hashlib.sha256((folder / artifact["filename"]).read_bytes()).hexdigest()
            _write(folder / "design.json", record)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def import_step(self, owner, filename, payload, file_id, name=None, *, source_feature=None):
        filename = Path(str(filename).replace("\\", "/")).name
        if Path(filename).suffix.lower() not in {".step", ".stp"}:
            raise DesignError("请选择 STEP 或 STP 文件。")
        if not payload or len(payload) > MAX_STEP_BYTES:
            raise DesignError("STEP 文件须为 1 字节至 20 MB。")
        if b"ISO-10303-21" not in payload[:4096]:
            raise DesignError("文件没有有效的 STEP 文本头。")
        with self._new(owner, name or filename, file_id, "part") as (folder, record):
            (folder / "source.step").write_bytes(payload)
            result = self._run("import", {}, folder)
            record.update(result, originalFilename=filename, sha256=hashlib.sha256(payload).hexdigest())
            if source_feature is not None:
                # Only the internal feature builder passes this server-owned reference.
                record["sourceFeature"] = source_feature
        return record

    def artifact(self, owner, design_id, key):
        record = self.get(owner, design_id)
        item = record.get("artifacts", {}).get(key)
        if not item or not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,120}", key):
            raise DesignNotFound("文件不存在。")
        folder = self._directory(owner, design_id)
        path = folder / item["filename"]
        if not path.is_file() or path.resolve().parent != folder.resolve():
            raise DesignNotFound("文件不存在。")
        return path, item

    def measure(self, owner, design_id, data):
        folder = self._directory(owner, design_id)
        return self._run("measure", data, folder)

    def assemble(self, owner, data):
        instances = data.get("instances")
        if not isinstance(instances, list) or not 1 <= len(instances) <= 30:
            raise DesignError("装配需要 1 至 30 个真实零件实例。")
        resolved, seen = [], set()
        for i, item in enumerate(instances):
            if not isinstance(item, dict):
                raise DesignError("装配实例格式无效。")
            source = self.get(owner, item.get("designId"))
            name = str(item.get("id", f"part{i + 1}"))
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or name in seen:
                raise DesignError("实例 ID 必须唯一且仅含英文字母、数字、下划线。")
            seen.add(name)
            resolved.append({"id": name, "designId": source["id"], "name": source["name"],
                "source": str(self._directory(owner, source["id"]) / "model.step"),
                "position": _vector(item.get("position", [0, 0, 0])),
                "rotation": _vector(item.get("rotation", [0, 0, 0]), "角度"), "fixed": item.get("fixed") is True})
        constraints = data.get("constraints", [])
        if not isinstance(constraints, list) or len(constraints) > 60:
            raise DesignError("最多可设置 60 条配合。")
        with self._new(owner, data.get("name", "装配设计"), data.get("fileId"), "assembly") as (folder, record):
            result = self._run("assembly", {"instances": resolved, "constraints": constraints}, folder)
            record.update(result)
            record["sha256"] = hashlib.sha256((folder / "model.step").read_bytes()).hexdigest()
        return record

    def drawing(self, owner, design_id, data):
        folder = self._directory(owner, design_id)
        # Unique artifacts keep earlier downloads valid, even when sheets are regenerated.
        prefix = "sheet-" + uuid4().hex[:16]
        result = self._run("drawing", {**data, "prefix": prefix}, folder)
        for artifact in result["artifacts"].values():
            artifact["sha256"] = hashlib.sha256((folder / artifact["filename"]).read_bytes()).hexdigest()
        with _LOCK:
            record = self.get(owner, design_id)
            record["artifacts"].update(result["artifacts"])
            record["lastDrawing"] = result
            _write(folder / "design.json", record)
        return record


def _cq():
    try:
        import cadquery as cq
    except ImportError:
        raise DesignError("需要 CadQuery / OCCT 几何内核，请安装 geometry 依赖。") from None
    return cq


def _load(path):
    cq = _cq()
    try:
        value = cq.importers.importStep(str(path)).val()
    except Exception:
        raise DesignError("STEP 无法读取，请检查文件是否完整。") from None
    solids = value.Solids()
    if not solids or len(solids) > 256 or not value.isValid() or len(value.Faces()) > 20000:
        raise DesignError("需要包含有效封闭实体的 STEP（最多 256 个实体、20000 个面）。")
    if value.Volume() <= 0 or value.BoundingBox().DiagonalLength > 1_000_000:
        raise DesignError("实体体积或尺寸无效。")
    return value


def _xyz(vector):
    return [float(vector.x), float(vector.y), float(vector.z)]


def _select(shape, selector):
    if not isinstance(selector, dict) or selector.get("kind") not in {"vertex", "edge", "face"}:
        raise DesignError("请选择点、边或面的拓扑编号。")
    values = {"vertex": shape.Vertices, "edge": shape.Edges, "face": shape.Faces}[selector["kind"]]()
    index = selector.get("index")
    if type(index) is not int or not 0 <= index < len(values):
        raise DesignError("拓扑编号已失效，请重新选择当前文件的点、边或面。")
    return values[index]


def _axis(entity):
    cq = _cq()
    if isinstance(entity, cq.Face) and entity.geomType() == "CYLINDER":
        cylinder = entity._geomAdaptor().Cylinder()
        return cq.Vector(cylinder.Location()), cq.Vector(cylinder.Axis().Direction()), float(cylinder.Radius())
    if isinstance(entity, cq.Edge) and entity.geomType() == "CIRCLE":
        circle = entity._geomAdaptor().Circle()
        return cq.Vector(circle.Location()), cq.Vector(circle.Axis().Direction()), float(circle.Radius())
    raise DesignError("同轴配合需要圆柱面或圆形边。")


def _entity_info(entity, kind, index):
    result = {"kind": kind, "index": index, "center": _xyz(entity.Center())}
    if kind == "vertex":
        result["point"] = _xyz(entity.Center())
    else:
        result["type"] = entity.geomType()
        result["length" if kind == "edge" else "area"] = float(entity.Length() if kind == "edge" else entity.Area())
        if result["type"] in {"CYLINDER", "CIRCLE"}:
            origin, direction, radius = _axis(entity)
            result.update(axisOrigin=_xyz(origin), axisDirection=_xyz(direction), radius=radius, diameter=2 * radius)
        if kind == "face" and result["type"] == "PLANE":
            result["normal"] = _xyz(entity.normalAt())
    return result


def _inspect(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box(); BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    topology = []
    counts = {}
    for kind, values in (("vertex", shape.Vertices()), ("edge", shape.Edges()), ("face", shape.Faces())):
        counts[kind] = len(values)
        topology.extend(_entity_info(entity, kind, i) for i, entity in enumerate(values[:1500]))
    return {"metrics": {"valid": True, "kernel": "CadQuery / OCCT", "solidCount": len(shape.Solids()),
        "volume": float(shape.Volume()), "area": float(shape.Area()), "size": [xmax-xmin, ymax-ymin, zmax-zmin],
        "min": [xmin, ymin, zmin], "max": [xmax, ymax, zmax]},
        "topology": topology, "topologyCounts": counts, "topologyTruncated": any(v > 1500 for v in counts.values())}


def _artifacts(folder, shape, *, export_step=True):
    cq = _cq()
    from .geometry import _mesh_from_cadquery, mesh_to_glb
    if export_step:
        cq.exporters.export(shape, str(folder / "model.step"), exportType="STEP")
    canonical = _load(folder / "model.step")
    mesh = _mesh_from_cadquery(cq.Workplane().add(canonical))
    if mesh is None:
        raise DesignError("真实实体三角化失败，未生成替代网格。")
    (folder / "model.glb").write_bytes(mesh_to_glb(mesh, name="Imported CAD"))
    return {**_inspect(canonical), "artifacts": {
        "step": {"filename": "model.step", "mimeType": "application/step", "label": "STEP 实体"},
        "glb": {"filename": "model.glb", "mimeType": "model/gltf-binary", "label": "GLB 三维预览"}}}


def _measure(shape, data):
    a = _select(shape, data.get("a"))
    result = {"a": _entity_info(a, data["a"]["kind"], data["a"]["index"]), "units": "mm", "kernel": "OCCT"}
    if data.get("b") is not None:
        b = _select(shape, data["b"])
        result.update(b=_entity_info(b, data["b"]["kind"], data["b"]["index"]),
            minimumDistance=float(a.distance(b)), centerDistance=float((a.Center() - b.Center()).Length),
            delta=_xyz(b.Center() - a.Center()))
        try:
            def direction(entity, kind):
                if kind == "face" and entity.geomType() == "PLANE":
                    return entity.normalAt()
                if kind == "edge" and entity.geomType() == "LINE":
                    return entity.tangentAt()
                return _axis(entity)[1]
            va = direction(a, data["a"]["kind"])
            vb = direction(b, data["b"]["kind"])
            result["angleDegrees"] = math.degrees(va.getAngle(vb))
            result["angleBasis"] = "平面法向、直边方向或圆柱/圆边轴线的夹角"
        except DesignError:
            pass
    return result


def _assembly(folder, data):
    cq = _cq()
    assembly = cq.Assembly(name="assembly")
    shapes, instances = {}, data["instances"]
    for item in instances:
        shapes[item["id"]] = _load(item["source"])
        location = cq.Location(tuple(item["position"]), tuple(item["rotation"]))
        assembly.add(shapes[item["id"]], name=item["id"], loc=location)
    constraints = data["constraints"]
    prepared = []
    if constraints:
        fixed = [x["id"] for x in instances if x["fixed"]] or [instances[0]["id"]]
        for name in fixed:
            assembly.constrain(name, "Fixed")
        for i, constraint in enumerate(constraints):
            if not isinstance(constraint, dict) or constraint.get("kind") not in {"concentric", "coincident", "distance"}:
                raise DesignError(f"配合 {i + 1} 类型无效。")
            na, nb = constraint.get("aInstance"), constraint.get("bInstance")
            if na not in shapes or nb not in shapes or na == nb:
                raise DesignError(f"配合 {i + 1} 需要两个不同实例。")
            a, b = _select(shapes[na], constraint.get("a")), _select(shapes[nb], constraint.get("b"))
            kind = constraint["kind"]
            distance = _number(constraint.get("value", 0), "配合距离")
            if distance < 0:
                raise DesignError("配合距离不能为负数。")
            if kind == "concentric":
                oa, da, _ = _axis(a); ob, db, _ = _axis(b)
                a = cq.Edge.makeLine(oa, oa + da); b = cq.Edge.makeLine(ob, ob + db)
                assembly.constrain(na, a, nb, b, "Axis", 0)
                assembly.constrain(na, cq.Vertex.makeVertex(*_xyz(oa)), nb, b, "PointOnLine")
            elif kind == "coincident":
                if not isinstance(a, cq.Face) or not isinstance(b, cq.Face) or a.geomType() != "PLANE" or b.geomType() != "PLANE":
                    raise DesignError("重合配合需要两个平面；本工具对齐所选面的中心与相反法向。")
                assembly.constrain(na, a, nb, b, "Plane", 180)
            else:
                assembly.constrain(na, a, nb, b, "Point", distance)
            prepared.append((constraint, a, b, distance))
        try:
            assembly.solve()
        except Exception:
            raise DesignError("配合约束求解失败，请检查是否存在冲突、无效拓扑或同时固定了两个需要移动的实例。") from None
    reports = []
    for i, (constraint, a, b, distance) in enumerate(prepared):
        a = a.located(assembly.objects[constraint["aInstance"]].loc)
        b = b.located(assembly.objects[constraint["bInstance"]].loc)
        kind = constraint["kind"]
        if kind == "concentric":
            delta = a.Center() - b.Center()
            linear = float(delta.cross(b.tangentAt()).Length)
            angular = float(a.tangentAt().cross(b.tangentAt()).Length)
        elif kind == "coincident":
            linear = float((a.Center() - b.Center()).Length)
            angular = float((a.normalAt() + b.normalAt()).Length)
        else:
            linear, angular = abs(float((a.Center() - b.Center()).Length) - distance), 0
        if linear > 0.01 or angular > 0.0001:
            raise DesignError(f"配合 {i + 1} 未满足精度要求（位置残差 {linear:.5g} mm），装配未保存。")
        reports.append({"index": i, "kind": kind, "passed": True, "residualMm": linear, "angularResidual": angular})
    placed, public = [], []
    for item in instances:
        location = assembly.objects[item["id"]].loc
        placed.append((item["id"], shapes[item["id"]].located(location)))
        position, rotation = location.toTuple()
        public.append({k: item[k] for k in ("id", "designId", "name", "fixed")} | {"position": list(position), "rotation": list(rotation)})
    interference = []
    for i, (na, sa) in enumerate(placed):
        for nb, sb in placed[i + 1:]:
            try:
                common = sa.intersect(sb)
                volume = sum(abs(s.Volume()) for s in common.Solids())
            except Exception:
                raise DesignError(f"实例 {na} / {nb} 的实体干涉运算失败，未给出无干涉结论。") from None
            interference.append({"a": na, "b": nb, "volumeMm3": float(volume), "interferes": volume > 1e-7})
    assembly.save(str(folder / "model.step"), exportType="STEP", mode="default")
    result = _artifacts(folder, assembly.toCompound(), export_step=False)
    counts = Counter(item["designId"] for item in instances)
    bom = [{"designId": design_id, "name": next(item["name"] for item in instances if item["designId"] == design_id), "quantity": count}
           for design_id, count in counts.items()]
    import csv
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(["零件 ID", "名称", "数量"])
    for item in bom:
        name = item["name"]
        writer.writerow([item["designId"], "'" + name if name.lstrip().startswith(("=", "+", "-", "@")) else name, item["quantity"]])
    (folder / "bom.csv").write_text("\ufeff" + output.getvalue(), encoding="utf-8")
    result["artifacts"]["bom"] = {"filename": "bom.csv", "mimeType": "text/csv;charset=utf-8", "label": "BOM 明细"}
    return {**result, "instances": public, "constraints": constraints, "constraintResults": reports,
            "interference": interference, "bom": bom}


_VIEWS = {"front": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
          "top": ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
          "right": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
          "rear": ((0, 1, 0), (-1, 0, 0), (0, 0, 1)),
          "bottom": ((0, 0, -1), (1, 0, 0), (0, -1, 0)),
          "left": ((-1, 0, 0), (0, -1, 0), (0, 0, 1))}


def _view_frame(spec):
    """One right-handed frame drives both B-Rep projection and annotations."""
    cq = _cq()
    name = spec.get("view")
    if name in _VIEWS:
        n,h,v = _VIEWS[name]
        return {"normal":list(n),"horizontal":list(h),"vertical":list(v),"origin":[0,0,0]}
    if name not in {"section","custom","isometric"}:
        raise DesignError("请选择六个正投影视图、轴测图、自定义方向或剖面。")
    axis = spec.get("axis", "custom" if "normal" in spec else "Z")
    if name == "isometric": normal = [1,-1,1]
    elif name == "custom" or axis == "custom": normal = _vector(spec.get("normal"),"视图法向")
    elif axis in {"X","Y","Z"}: normal = [int(c==axis) for c in "XYZ"]
    else: raise DesignError("剖切方向须为 X、Y、Z 或自定义法向。")
    n = cq.Vector(*normal)
    if n.Length < 1e-9: raise DesignError("视图法向不能为零向量。")
    n = n.normalized()
    if "horizontal" in spec:
        hint=cq.Vector(*_vector(spec["horizontal"],"视图水平方向"))
        h=hint-n*hint.dot(n)
        if h.Length < 1e-9:raise DesignError("视图水平方向不能与法向平行。")
        h=h.normalized()
    else:
        up=cq.Vector(0,0,1) if abs(n.z)<.95 else cq.Vector(0,1,0)
        h=up.cross(n).normalized()
    v=n.cross(h).normalized()
    position = _number(spec.get("position",spec.get("z",0)),"剖切平面位置") if name=="section" else 0
    origin=n*position
    return {"normal":_xyz(n),"horizontal":_xyz(h),"vertical":_xyz(v),"origin":_xyz(origin),"position":position}


def _plane_point(point, frame):
    delta=[a-b for a,b in zip(_xyz(point),frame["origin"])]
    return [sum(a*b for a,b in zip(delta,frame[key])) for key in ("horizontal","vertical")]


def _projection(shape, frame):
    from OCP.BRepLib import BRepLib
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    cq = _cq()
    if isinstance(frame,str):frame=_view_frame({"view":frame})
    normal, horizontal = frame["normal"],frame["horizontal"]
    value = shape.copy(); BRepLib.EncodeRegularity_s(value.wrapped)
    algorithm = HLRBRep_Algo(); algorithm.Add(value.wrapped)
    algorithm.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(*frame["origin"]), gp_Dir(*normal), gp_Dir(*horizontal))))
    algorithm.Update(); algorithm.Hide()
    output = HLRBRep_HLRToShape(algorithm)
    result = []
    for hidden, compounds in ((False, (output.VCompound(), output.OutLineVCompound())), (True, (output.HCompound(), output.OutLineHCompound()))):
        for compound in compounds:
            if compound.IsNull():
                continue
            BRepLib.BuildCurves3d_s(compound, 1e-7)
            for edge in cq.Shape(compound).Edges():
                points = edge.sample(0.05)[0]
                result.append({"points": [[p.x, p.y] for p in points], "hidden": hidden})
    return result


def _section(shape, spec):
    cq = _cq()
    if isinstance(spec,(int,float)):spec={"view":"section","z":spec}
    frame=_view_frame(spec)
    try:
        plane=cq.Plane(origin=frame["origin"],xDir=frame["horizontal"],normal=frame["normal"])
        section = cq.Workplane(plane).add(shape).section().val()
        edges = section.Edges()
    except Exception:
        raise DesignError("该平面没有可生成的实体截面，请调整方向或位置。") from None
    faces=section.Faces()
    if not faces or sum(face.Area() for face in faces)<1e-9:
        raise DesignError("剖切平面未穿过实体的有效截面。")
    result = [{"points": [_plane_point(p,frame) for p in edge.sample(0.05)[0]], "hidden": False} for edge in edges]
    if not result:
        raise DesignError("剖切平面未穿过实体。")
    # Even/odd clipping across all real section loops preserves holes.
    segments = [(a, b) for line in result for a, b in zip(line["points"], line["points"][1:])]
    bounds = [p[1] - p[0] for a, b in segments for p in (a, b)]
    step = max(2, (max(bounds) - min(bounds)) / 300)
    for i in range(math.floor(min(bounds) / step), math.ceil(max(bounds) / step) + 1):
        offset, hits = i * step, []
        for a, b in segments:
            da, db = a[1] - a[0] - offset, b[1] - b[0] - offset
            if (da < 0 <= db) or (db < 0 <= da):
                t = da / (da - db); hits.append([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])])
        hits.sort()
        result.extend({"points": hits[j:j + 2], "hidden": False, "hatch": True} for j in range(0, len(hits) - 1, 2))
    points=[p for path in result if not path.get("hatch") for p in path["points"]]
    metrics={"areaMm2":sum(face.Area() for face in faces),"faceCount":len(faces),"edgeCount":len(edges),
        "sizeMm":[max(p[i] for p in points)-min(p[i] for p in points) for i in (0,1)]}
    mode=spec.get("sectionMode","cutaway")
    if mode not in {"cutaway","cross_section"}:raise DesignError("剖视模式须为完整剖视或仅断面。")
    metrics["mode"]=mode
    if mode=="cutaway":
        # A true OCCT half-space intersection retains n·p <= position. HLR
        # then shows geometry behind the cut as well as the section perimeter.
        # This is separate from GPU clipping and never changes the source STEP.
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeHalfSpace
        from OCP.gp import gp_Pnt
        try:
            origin=cq.Vector(*frame["origin"]);normal=cq.Vector(*frame["normal"])
            plane=cq.Face.makePlane(basePnt=origin,dir=normal)
            inside=origin-normal
            halfspace=cq.Solid(BRepPrimAPI_MakeHalfSpace(plane.wrapped,gp_Pnt(*_xyz(inside))).Solid())
            retained=shape.intersect(halfspace)
            if not retained.isValid() or not retained.Solids():raise ValueError()
            projection=_projection(retained,frame)
        except Exception:
            raise DesignError("真实实体剖切或剖视投影失败，请调整平面；原实体未改变。") from None
        metrics.update(retainedVolumeMm3=float(retained.Volume()),retainedSolidCount=len(retained.Solids()))
        result=projection+[path for path in result if path.get("hatch")]
    return {"paths":result,"frame":frame,"metrics":metrics}


def _drawing(folder, shape, data):
    import ezdxf
    import fitz
    from html import escape
    page = data.get("page", "A3")
    if page not in {"A4", "A3"}:
        raise DesignError("图幅支持 A4、A3。")
    width, height = (297, 210) if page == "A4" else (420, 297)
    specs = data.get("views") or [{"view": "front"}, {"view": "top"}, {"view": "right"}]
    if not isinstance(specs, list) or not 1 <= len(specs) <= 8:
        raise DesignError("图纸需要 1 至 8 个视图。")
    dimensions = data.get("dimensions", [])
    if not isinstance(dimensions, list) or len(dimensions) > 100:
        raise DesignError("最多支持 100 处尺寸标注。")
    doc = ezdxf.new("R2010"); doc.units = 4
    doc.linetypes.new("CAD_HIDDEN", dxfattribs={"description":"Hidden edges", "pattern":[3,2,-1]})
    for layer, color in (("VISIBLE", 7), ("HIDDEN", 8), ("SECTION", 8), ("DIM", 2), ("TEXT", 7)):
        doc.layers.new(layer, dxfattribs={"color": color, "linetype":"CAD_HIDDEN" if layer=="HIDDEN" else "CONTINUOUS"})
    msp = doc.modelspace(); pdf = fitz.open(); pdfpage = pdf.new_page(width=width * 72 / 25.4, height=height * 72 / 25.4)
    # Built-in CJK aliases such as china-s create an unembedded Heiti font
    # requiring the reader's Adobe-GB1 CMaps. Embed PyMuPDF's bundled glyphs
    # under a custom resource name so exported sheets are self-contained.
    pdfpage.insert_font(fontname="JoyNiuCJK", fontbuffer=fitz.Font("cjk").buffer)
    factor = 72 / 25.4
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>']
    def line(points, layer="VISIBLE"):
        if len(points) < 2:
            return
        msp.add_lwpolyline(points, dxfattribs={"layer": layer})
        color = (0.50, 0.50, 0.50) if layer in {"HIDDEN", "SECTION"} else (0.12, 0.17, 0.23)
        pdfpage.draw_polyline([fitz.Point(x * factor, (height - y) * factor) for x, y in points], color=color, width=.45 if layer == "SECTION" else .7,
                              dashes="[3 2] 0" if layer == "HIDDEN" else None)
        coords = " ".join(f"{x:.5f},{height-y:.5f}" for x, y in points)
        svg.append(f'<polyline points="{coords}" fill="none" stroke="{"#888" if layer in {"HIDDEN", "SECTION"} else "#263747"}" stroke-width="0.2"'+(' stroke-dasharray="2,1"' if layer == "HIDDEN" else '')+'/>')
    def text(x, y, content, size=3):
        msp.add_text(content, dxfattribs={"insert": (x, y), "height": size, "layer": "TEXT"})
        pdfpage.insert_text((x * factor, (height - y) * factor), content, fontname="JoyNiuCJK", fontsize=size * factor)
        svg.append(f'<text x="{x:.4f}" y="{height-y:.4f}" font-family="sans-serif" font-size="{size}">{escape(content)}</text>')
    line([[8, 8], [width - 8, 8], [width - 8, height - 8], [8, height - 8], [8, 8]])
    text(14, 14, _name(data.get("title", "CAD 工程图")), 4)
    text(width - 155, 14, "单位 mm · OCCT 实体投影 · 曲线离散 0.05 mm", 2.7)
    layouts = []
    columns = 2; rows = math.ceil(len(specs) / columns)
    cellw, cellh = (width - 38) / columns, (height - 60) / rows
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise DesignError("视图配置须为对象。")
        name = spec.get("view")
        frame=_view_frame(spec)
        section=_section(shape,spec) if name=="section" else None
        paths=section["paths"] if section else _projection(shape,frame)
        points = [p for path in paths for p in path["points"]]
        if not points:
            raise DesignError("当前视图没有可投影的边。")
        minx, maxx = min(p[0] for p in points), max(p[0] for p in points)
        miny, maxy = min(p[1] for p in points), max(p[1] for p in points)
        natural = min((cellw - 35) / max(maxx-minx, 1), (cellh - 35) / max(maxy-miny, 1), 1)
        scale = _number(spec.get("scale", natural), "视图比例", 100)
        if scale <= 0:
            raise DesignError("视图比例须大于零。")
        cx = _number(spec.get("x", 20 + cellw * (index % 2 + .5)), "视图 X")
        cy = _number(spec.get("y", height - 24 - cellh * (index // 2 + .5)), "视图 Y")
        tx, ty = cx - (minx + maxx) * scale / 2, cy - (miny + maxy) * scale / 2
        if minx * scale + tx < 12 or maxx * scale + tx > width - 12 or miny * scale + ty < 30 or maxy * scale + ty > height - 12:
            raise DesignError(f"视图 {index + 1} 超出图框，请缩小比例或调整布局。")
        for path in paths:
            if path.get("hidden") and data.get("hidden", True) is False:
                continue
            line([[p[0] * scale + tx, p[1] * scale + ty] for p in path["points"]], "SECTION" if path.get("hatch") else "HIDDEN" if path["hidden"] else "VISIBLE")
        if section:
            direction=spec.get('axis','custom' if 'normal' in spec else 'Z')
            if direction=='custom':direction='N('+','.join(f'{v:.2g}' for v in frame['normal'])+')'
            caption=f"{'CROSS' if section['metrics']['mode']=='cross_section' else 'SECTION'} {direction}={frame['position']:g}"
        else:caption=name.upper()
        text(cx - 18, miny * scale + ty - 7, f"{caption}  {scale:g}:1", 3)
        layouts.append({"view": name, "index": index, "scale": scale, "x": cx, "y": cy, "tx": tx, "ty": ty,
            "frame":frame,"naturalSizeMm":[maxx-minx,maxy-miny],**({"section":section["metrics"]} if section else {})})
    measured = []
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, dict):
            raise DesignError("尺寸格式无效。")
        view_index = dimension.get("viewIndex", 0)
        if type(view_index) is not int or not 0 <= view_index < len(layouts):
            raise DesignError("尺寸视图编号无效。")
        view = layouts[view_index]; scale = view["scale"]
        frame=view["frame"]
        a = _select(shape, dimension.get("a"))
        def projected(entity):
            point = entity.Center() if hasattr(entity, "Center") else entity
            return _plane_point(point,frame)
        orientation = dimension.get("orientation", "aligned")
        offset = _number(dimension.get("offset", 10), "标注偏移", 100)
        if orientation in {"diameter", "radius"}:
            origin, direction, radius = _axis(a)
            if direction.cross(_cq().Vector(*frame["normal"])).Length > 1e-6:
                raise DesignError(f"尺寸 {index + 1} 需要沿圆轴线观察的视图，请选择能看到圆的视图。")
            center = projected(origin)
            center = [center[0]*scale+view["tx"], center[1]*scale+view["ty"]]
            end = [center[0]+radius*scale*.70710678, center[1]+radius*scale*.70710678]
            note = [end[0]+offset, end[1]+offset]
            if not 12 <= note[0] <= width-25 or not 25 <= note[1] <= height-12:
                raise DesignError(f"尺寸 {index + 1} 超出图框，请调整偏移。")
            override = {"dimlfac":1/scale,"dimtxt":2.5,"dimasz":1.5,"dimclrd":7}
            factory = msp.add_diameter_dim if orientation=="diameter" else msp.add_radius_dim
            native = factory(center=center,radius=radius*scale,angle=45,location=note,override=override,dxfattribs={"layer":"DIM"})
            native.render()
            value = radius*(2 if orientation=="diameter" else 1)
            existing = len(list(msp))
            line([center,end,note],"DIM")
            text(note[0]+1,note[1]+1,("Ø" if orientation=="diameter" else "R")+f"{value:.3f}".rstrip('0').rstrip('.'),2.7)
            for entity in list(msp)[existing:]:
                msp.delete_entity(entity)
            measured.append({"index":index,"value":value,"units":"mm","orientation":orientation})
            continue
        b = _select(shape, dimension.get("b"))
        pa, pb = projected(a), projected(b)
        p1, p2 = [pa[0]*scale+view["tx"], pa[1]*scale+view["ty"]], [pb[0]*scale+view["tx"], pb[1]*scale+view["ty"]]
        if orientation not in {"horizontal", "vertical", "aligned"}:
            raise DesignError("尺寸方向支持水平、垂直、对齐、直径和半径。")
        dx, dy = pb[0]-pa[0], pb[1]-pa[1]
        value = abs(dx) if orientation == "horizontal" else abs(dy) if orientation == "vertical" else math.hypot(dx, dy)
        if value < 1e-8:
            raise DesignError(f"尺寸 {index + 1} 的投影距离为零。")
        if orientation == "horizontal":
            q1, q2 = [p1[0], max(p1[1],p2[1])+offset], [p2[0], max(p1[1],p2[1])+offset]
        elif orientation == "vertical":
            q1, q2 = [max(p1[0],p2[0])+offset,p1[1]], [max(p1[0],p2[0])+offset,p2[1]]
        else:
            length = math.hypot(p2[0]-p1[0],p2[1]-p1[1]); normal = [-(p2[1]-p1[1])/length, (p2[0]-p1[0])/length]
            q1, q2 = [[p[j]+normal[j]*offset for j in range(2)] for p in (p1,p2)]
        if any(not 10 <= p[0] <= width-10 or not 25 <= p[1] <= height-10 for p in (q1,q2)):
            raise DesignError(f"尺寸 {index + 1} 超出图框，请调整偏移或布局。")
        # Native DIMENSION in DXF, same measured value and explicit paper scale.
        override = {"dimlfac": 1/scale, "dimtxt": 2.5, "dimasz": 1.5, "dimtad": 1, "dimclrd": 7}
        if orientation == "aligned":
            dim = msp.add_aligned_dim(p1=p1,p2=p2,distance=offset,override=override,dxfattribs={"layer":"DIM"})
        else:
            dim = msp.add_linear_dim(base=q1,p1=p1,p2=p2,angle=0 if orientation=="horizontal" else 90,override=override,dxfattribs={"layer":"DIM"})
        dim.render()
        # PDF/SVG use the same dimension endpoints, without duplicate DXF lines.
        start_entities = list(msp)
        for points in ((p1,q1),(p2,q2),(q1,q2)):
            line(points,"DIM")
        for q in (q1,q2):
            line([[q[0]-1,q[1]-1],[q[0]+1,q[1]+1]],"DIM")
        text((q1[0]+q2[0])/2+1,(q1[1]+q2[1])/2+1.5,f"{value:.3f}".rstrip('0').rstrip('.'),2.7)
        for entity in list(msp)[len(start_entities):]:
            msp.delete_entity(entity)
        measured.append({"index": index,"value":value,"units":"mm","orientation":orientation})
    svg.append("</svg>")
    prefix = data["prefix"]
    (folder/f"{prefix}.svg").write_text("".join(svg),encoding="utf-8")
    doc.saveas(folder/f"{prefix}.dxf")
    pdf.subset_fonts()
    pdf.save(folder/f"{prefix}.pdf", garbage=4, deflate=True); pdf.close()
    return {"page":page,"layouts":layouts,"dimensions":measured,"curveToleranceMm":.05,
        "settings":{"page":page,"hidden":data.get("hidden",True),"views":specs,"dimensions":dimensions},
        "artifacts":{f"{prefix}.{fmt}":{"filename":f"{prefix}.{fmt}","mimeType":mime,"label":f"工程图 {fmt.upper()}"}
            for fmt,mime in (("svg","image/svg+xml"),("dxf","application/dxf"),("pdf","application/pdf"))}}


def _worker(request):
    directory = Path(request["directory"])
    operation, data = request["operation"], request["data"]
    if operation == "import":
        return _artifacts(directory, _load(directory / "source.step"))
    if operation == "assembly":
        return _assembly(directory, data)
    shape = _load(directory / "model.step")
    if operation == "measure":
        return _measure(shape, data)
    if operation == "drawing":
        return _drawing(directory, shape, data)
    raise DesignError("操作不受支持。")


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (90, 95))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (6 * 1024**3, 6 * 1024**3))
        result = {"ok": True, "result": _worker(request)}
    except DesignError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception:
        result = {"ok": False, "error": "实体运算失败，请检查拓扑选择、配合条件或图纸设置。"}
    _write(Path(request["response"]), result)
