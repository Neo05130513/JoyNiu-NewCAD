"""Immutable owned imported bodies; plans contain IDs and hashes, never paths."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

MAX_IMPORT_BYTES = 20 * 1024**2
_ID = re.compile(r"asset_[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
IMPORT_OP_FIELDS = {"import_step": ({"assetId", "sha256"}, set())}


def _error(message, code="invalid_import"):
    from .cad_plan import PlanValidationError
    raise PlanValidationError(message, code=code)


def _missing():
    from .cad_feature_workspace import FeatureNotFound
    raise FeatureNotFound("导入实体不存在、已失效或不属于当前账号。")


def validate_import_feature(feature, scalar=None, vector=None):
    if not isinstance(feature.get("assetId"), str) or not _ID.fullmatch(feature["assetId"]): _error("导入实体编号无效。")
    if not isinstance(feature.get("sha256"), str) or not _SHA.fullmatch(feature["sha256"]): _error("导入实体校验值无效。")


def import_schema_properties(scalar=None, vec=None):
    return {"assetId": {"type": "string", "pattern": "^asset_[a-f0-9]{32}$"}, "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}


def import_references(plan):
    found = {}
    for feature in (plan or {}).get("features", []):
        if feature.get("op") != "import_step": continue
        validate_import_feature(feature)
        if feature["assetId"] in found and found[feature["assetId"]] != feature["sha256"]: _error("同一导入实体不能引用不同内容。")
        found[feature["assetId"]] = feature["sha256"]
    if len(found) > 32: _error("单个模型最多引用 32 份导入实体。", "resource_limit")
    return found


def build_import_step(cq, feature, imported_assets=None):
    validate_import_feature(feature)
    entry = (imported_assets or {}).get(feature["assetId"])
    if not isinstance(entry, dict) or entry.get("sha256") != feature["sha256"]: _error("导入实体尚未通过当前账号授权。", "untrusted_import_asset")
    path = Path(entry.get("path", ""))
    if not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != feature["sha256"]: _error("导入实体文件缺失或内容已变化，请重新导入。", "import_asset_changed")
    from .cad_design_workspace import _load
    return cq.Workplane("XY").newObject([_load(path)])


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


class CadImportAssets:
    def __init__(self, root):
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)

    def _owner(self, owner):
        if not isinstance(owner, str) or not owner: _missing()
        path = self.root / hashlib.sha256(owner.encode()).hexdigest(); path.mkdir(exist_ok=True)
        return path

    def get(self, owner, asset_id):
        if not isinstance(asset_id, str) or not _ID.fullmatch(asset_id): _missing()
        directory = self._owner(owner) / asset_id
        try: record = json.loads((directory / "asset.json").read_text())
        except (OSError, ValueError): _missing()
        if record.get("id") != asset_id or record.get("owner") != owner: _missing()
        return record

    @staticmethod
    def public(record):
        return {key: value for key, value in record.items() if key not in {"owner", "requestFingerprint"}}

    def path(self, owner, asset_id, sha256):
        record = self.get(owner, asset_id)
        if record.get("sha256") != sha256: _error("导入实体版本校验不一致。", "import_asset_changed")
        path = self._owner(owner) / asset_id / "model.step"
        if not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES: _missing()
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha256: _error("导入实体文件校验失败。", "import_asset_changed")
        return path

    def resolve(self, owner, plan):
        return {asset_id: {"path": str(self.path(owner, asset_id, sha)), "sha256": sha} for asset_id,sha in import_references(plan).items()}

    def upload(self, owner, filename, payload, name, units, request_id, cancel_event=None):
        filename = Path(str(filename).replace("\\", "/")).name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".step", ".stp", ".stl"}: _error("请选择 STEP、STP 或 STL 实体文件。")
        if not payload or len(payload) > MAX_IMPORT_BYTES: _error("导入文件须为 1 字节至 20 MB。")
        if units not in {"mm", "cm", "m", "inch"} or suffix != ".stl" and units != "mm": _error("STEP 使用文件内置单位；STL 须明确长度单位。")
        if not isinstance(name,str) or not name.strip() or len(name)>180 or any(ord(c)<32 for c in name): _error("请填写 1 至 180 字符的实体名称。")
        if not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}",request_id): _error("导入请求编号无效。")
        if suffix != ".stl" and b"ISO-10303-21" not in payload[:4096]: _error("文件没有有效的 STEP 头。")
        raw_sha=hashlib.sha256(payload).hexdigest()
        fingerprint=hashlib.sha256(json.dumps([raw_sha,suffix,units,name.strip()]).encode()).hexdigest()
        asset_id="asset_"+hashlib.sha256(f"{owner}\0{request_id}".encode()).hexdigest()[:32]
        from .cad_editor_pdm import _open_lock
        with _open_lock(self.root,asset_id):
            directory=self._owner(owner)/asset_id
            if directory.exists():
                record=self.get(owner,asset_id)
                if record.get("requestFingerprint")!=fingerprint: _error("同一导入请求不能用于不同文件。", "import_request_conflict")
                self.path(owner,asset_id,record["sha256"])
                return self.public(record)
            with tempfile.TemporaryDirectory(prefix=".upload-",dir=self._owner(owner)) as temporary:
                folder=Path(temporary); (folder/("source.stl" if suffix==".stl" else "source.step")).write_bytes(payload)
                info=self._run(folder,suffix,units,cancel_event)
                normalized=folder/"model.step"
                if normalized.stat().st_size>MAX_IMPORT_BYTES: _error("转换后的 STEP 超过 20 MB，请减少网格面数。", "resource_limit")
                record={"id":asset_id,"owner":owner,"name":name.strip(),"originalFilename":filename,"originalFormat":"stl" if suffix==".stl" else "step","originalUnits":units if suffix==".stl" else "file","units":"mm",
                        "originalSha256":raw_sha,"sha256":hashlib.sha256(normalized.read_bytes()).hexdigest(),"sizeBytes":normalized.stat().st_size,"requestFingerprint":fingerprint,"inspection":info,
                        "history":"imported_body","feature":{"op":"import_step","assetId":asset_id,"sha256":hashlib.sha256(normalized.read_bytes()).hexdigest()}}
                _write(folder/"asset.json",record)
                if cancel_event is not None and cancel_event.is_set(): _error("导入已取消。", "user_cancelled")
                # Atomic publication; no partial source is ever resolvable.
                folder.rename(directory)
            return self.public(record)

    def _run(self,folder,suffix,units,cancel_event):
        from .cad_design_workspace import _KERNEL_SLOTS
        if cancel_event is not None and cancel_event.is_set(): _error("导入已取消。", "user_cancelled")
        if not _KERNEL_SLOTS.acquire(False): _error("已有几何任务运行，请稍后重试。", "resource_limit")
        request=folder/"import-request.json"; result=folder/"import-result.json"
        _write(request,{"folder":str(folder),"format":"stl" if suffix==".stl" else "step","units":units})
        try:
            environment={key:value for key,value in os.environ.items() if key in {"PATH","TMPDIR","TEMP","TMP","LANG","LC_ALL"}}
            environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]),OMP_NUM_THREADS="1",OPENBLAS_NUM_THREADS="1")
            with subprocess.Popen([sys.executable,"-m","app.cad_import_assets",str(request)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=environment) as process:
                started=time.monotonic()
                while process.poll() is None:
                    if (cancel_event is not None and cancel_event.is_set()) or time.monotonic()-started>100:
                        process.terminate()
                        try: process.wait(timeout=2)
                        except subprocess.TimeoutExpired: process.kill();process.wait()
                        _error("导入已取消。" if cancel_event is not None and cancel_event.is_set() else "实体转换超时，请减少网格面数。","user_cancelled" if cancel_event is not None and cancel_event.is_set() else "worker_timeout")
                    time.sleep(.05)
            if not result.is_file(): _error("实体内核未完成导入，请检查文件。", "import_failed")
            response=json.loads(result.read_text())
            if not response.get("ok"): _error(response.get("message","实体无法导入。"),response.get("code","import_failed"))
            return response["inspection"]
        finally:
            request.unlink(missing_ok=True);result.unlink(missing_ok=True);_KERNEL_SLOTS.release()

    def archive(self,owner,plan,*,max_bytes=100*1024**2):
        parts={};remaining=max_bytes
        for asset_id,sha in import_references(plan).items():
            record=self.get(owner,asset_id);path=self.path(owner,asset_id,sha)
            metadata=json.dumps(self.public(record),ensure_ascii=False,allow_nan=False).encode()
            if path.stat().st_size+len(metadata)>remaining: _error("包含导入源的 PDM CAD 包超过保存大小限制。", "resource_limit")
            parts[f"assets/{asset_id}.step"]=path.read_bytes();parts[f"assets/{asset_id}.json"]=metadata
            remaining-=len(parts[f"assets/{asset_id}.step"])+len(metadata)
        return parts

    def restore_trusted(self,owner,plan,parts):
        from .cad_editor_pdm import _open_lock
        for asset_id,sha in import_references(plan).items():
            raw=parts.get(f"assets/{asset_id}.step"); metadata=parts.get(f"assets/{asset_id}.json")
            if not isinstance(raw,bytes) or not metadata or len(raw)>MAX_IMPORT_BYTES or hashlib.sha256(raw).hexdigest()!=sha: _error("PDM 包缺少匹配的导入源实体。")
            record=json.loads(metadata)
            if record.get("id")!=asset_id or record.get("sha256")!=sha: _error("PDM 导入源关联校验失败。")
            with _open_lock(self.root,asset_id):
                directory=self._owner(owner)/asset_id
                if directory.exists(): self.path(owner,asset_id,sha);continue
                with tempfile.TemporaryDirectory(prefix=".restore-",dir=self._owner(owner)) as temporary:
                    folder=Path(temporary);(folder/"model.step").write_bytes(raw)
                    _write(folder/"asset.json",{**record,"owner":owner,"restoredFromPdm":True});folder.rename(directory)


def _stl_mesh(payload,scale):
    triangles=[]
    count=struct.unpack_from("<I",payload,80)[0] if len(payload)>=84 else 0
    if len(payload)>=84 and 84+50*count==len(payload):
        if not 4<=count<=10000: _error("STL 须包含 4 至 10000 个三角面。", "resource_limit")
        for i in range(count):
            values=struct.unpack_from("<12f",payload,84+50*i)
            triangles.append([values[j:j+3] for j in (3,6,9)])
    else:
        try: source=payload.decode("ascii")
        except UnicodeError: _error("STL 二进制长度或文本编码无效。")
        if not source.lstrip().lower().startswith("solid") or "endsolid" not in source.lower(): _error("STL 文本结构无效。")
        rows=re.findall(r"\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)",source,re.I)
        if len(rows)%3 or not 12<=len(rows)<=30000: _error("STL 须包含 4 至 10000 个完整三角面。", "resource_limit")
        try: triangles=[[tuple(float(v) for v in row) for row in rows[i:i+3]] for i in range(0,len(rows),3)]
        except ValueError: _error("STL 顶点坐标无效。")
    vertices=[]; indices={}; faces=[]
    for tri in triangles:
        face=[]
        for point in tri:
            key=tuple(v*scale for v in point)
            if any(not math.isfinite(v) or abs(v)>1e6 for v in key): _error("STL 坐标超出有效范围。")
            if key not in indices: indices[key]=len(vertices);vertices.append(list(key))
            face.append(indices[key])
        if len(set(face))!=3: _error("STL 包含退化三角面。")
        faces.append(face)
    if len(vertices)>10000: _error("STL 顶点数超过 10000。", "resource_limit")
    return vertices,faces


def _normalize(request):
    import cadquery as cq
    from .cad_design_workspace import _load,_inspect
    folder=Path(request["folder"])
    if request["format"]=="stl":
        from .cad_surface_features import build_surface_feature
        vertices,triangles=_stl_mesh((folder/"source.stl").read_bytes(),{"mm":1,"cm":10,"m":1000,"inch":25.4}[request["units"]])
        shape=build_surface_feature(cq,{"op":"mesh_body","vertices":vertices,"triangles":triangles},{},float,lambda p:tuple(p),{}).val().clean()
        if not shape.isValid() or not shape.Solids() or shape.Volume()<=0: _error("STL 无法形成有效封闭实体。", "open_mesh")
    else: shape=_load(folder/"source.step")
    cq.exporters.export(shape,str(folder/"model.step"))
    readback=_load(folder/"model.step")
    if len(readback.Solids())!=len(shape.Solids()) or not math.isclose(readback.Volume(),shape.Volume(),rel_tol=2e-5,abs_tol=1e-6): _error("导入 STEP 往返校验失败。")
    return {**_inspect(readback)["metrics"], "faceCount":len(readback.Faces()), "facetedSource":request["format"]=="stl"}


if __name__=="__main__":
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(90,95))
    if sys.platform!="darwin": resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,4*1024**3))
    request=json.loads(Path(sys.argv[1]).read_text())
    try: response={"ok":True,"inspection":_normalize(request)}
    except Exception as error: response={"ok":False,"message":str(error)[:500],"code":getattr(error,"code","import_failed")}
    _write(Path(request["folder"])/"import-result.json",response)
