"""Explicit, immutable public CAD resources, with private publication registry.

Only owned built revisions can create a resource. Public reads never return a
workspace record or source-document metadata. Trusted independent copies are
constructed from this server-authored registry, never from an uploaded ZIP.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from .cad_editor_pdm import _json, _open_lock, _request_id, _text
from .cad_feature_workspace import FeatureNotFound, public_feature_record
from .cad_import_assets import CadImportAssets, import_references
from .cad_plan import validate_plan
from .platform import ConflictError, NotFoundError, AuthorizationError, ValidationError

FORMAT = "joyniu-cad-community-v1"
_MAX_BYTES = 100 * 1024**2
_RESOURCE = re.compile(r"resource_[a-f0-9]{32}\Z")
_KINDS = {"step": ("model.step", "application/step"), "glb": ("model.glb", "model/gltf-binary")}


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _metrics(inspection):
    # Whitelist numeric geometry results, not paths, logs or source evidence.
    fields = ("valid", "solidCount", "volumeMm3", "surfaceAreaMm2", "bbox")
    result = {key: copy.deepcopy(inspection[key]) for key in fields if key in inspection}
    result["stepReadback"] = {key: copy.deepcopy(value) for key, value in inspection.get("stepReadback", {}).items() if key in fields}
    return result


def _public_plan(source, name):
    result = copy.deepcopy(validate_plan(source))
    result["name"] = name
    result.pop("notes", None); result.pop("questions", None)
    result["parameters"] = {key: {field: value for field, value in item.items() if field in {"value", "expression", "label"}}
                            for key, item in result["parameters"].items()}
    return result


class CadCommunity:
    def __init__(self, root, features):
        self.root, self.features = Path(root).resolve(), features
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, resource_id):
        if not isinstance(resource_id, str) or not _RESOURCE.fullmatch(resource_id):
            raise NotFoundError("社区资源不存在或已撤下。")
        return self.root / resource_id

    def _read(self, resource_id):
        directory = self._directory(resource_id)
        try: value = json.loads((directory / "registry.json").read_text())
        except (OSError, ValueError): raise NotFoundError("社区资源不存在或已撤下。") from None
        if value.get("format") != FORMAT or value.get("id") != resource_id:
            raise ValidationError("社区资源登记校验失败。")
        return value

    def _visible(self, resource_id):
        value = self._read(resource_id)
        if value["status"] != "published": raise NotFoundError("社区资源不存在或已撤下。")
        return value

    @staticmethod
    def public(value, actor=None):
        return {key: copy.deepcopy(value[key]) for key in ("id", "name", "description", "publishedAt", "status", "inspection")} | {
            "canWithdraw": actor is not None and actor == value["owner"] and value["status"] == "published",
            "sharing": "public-copy-download", "drawingAgreement": "not_checked", "productionReady": False,
            "artifacts": {kind: {"url": f"/api/cad/community/{value['id']}/artifacts/{kind}", "mimeType": mime}
                          for kind, (_, mime) in _KINDS.items()} if value["status"] == "published" else {}}

    def list(self, actor=None, query="", mine=False):
        if len(query) > 180: raise ValidationError("搜索不能超过 180 个字符。")
        if mine and not actor: raise AuthorizationError("请登录后查看我的发布。")
        entries = []
        for directory in self.root.glob("resource_*"):
            try: value = self._read(directory.name)
            except NotFoundError: continue
            if (mine and value["owner"] != actor) or (not mine and value["status"] != "published"): continue
            if query.casefold() not in (value["name"] + "\n" + value["description"]).casefold(): continue
            entries.append(value)
        entries.sort(key=lambda item: item["publishedAt"], reverse=True)
        return {"items": [self.public(value, actor) for value in entries[:200]], "hasMore": len(entries) > 200}

    def get(self, resource_id, actor=None):
        with _open_lock(self.root, resource_id if _RESOURCE.fullmatch(str(resource_id)) else "invalid"):
            return self.public(self._visible(resource_id), actor)

    def publish(self, owner, data):
        if set(data) != {"featureId", "revision", "name", "description", "requestId", "consent"} or data.get("consent") != "public-copy-download":
            raise ValidationError("请明确确认公开名称、说明、实体及可编辑模型，允许他人下载并创建副本。")
        name = _text(data.get("name"), "资源名称")
        description = data.get("description")
        if not isinstance(description, str) or len(description) > 2000 or any(ord(c) < 32 and c not in "\n\t" for c in description):
            raise ValidationError("资源说明不能超过 2000 字符。")
        if type(data.get("revision")) is not int or data["revision"] < 1: raise ValidationError("须选择精确已提交版本。")
        request_id = _request_id(data.get("requestId"))
        resource_id = "resource_" + _digest(f"community\0{owner}\0{request_id}".encode())[:32]
        fingerprint = _digest(_json(data).encode())
        with _open_lock(self.root, resource_id):
            directory = self._directory(resource_id)
            if directory.exists():
                old = self._read(resource_id)
                if old["owner"] != owner or old["fingerprint"] != fingerprint: raise ConflictError("该发布请求已用于其他内容。")
                if old["status"] != "published": raise ConflictError("此发布已撤下，请使用新的发布请求。")
                return self.public(old, owner)
            record = self.features.get(owner, data.get("featureId"), data["revision"])
            if record.get("status") != "built" or record.get("inspection", {}).get("stepReadback", {}).get("valid") is not True:
                raise ValidationError("请先完成实体重建，再发布该精确版本。")
            parts = {}
            # These are generated, shape-only OCCT/GLB outputs; raw uploaded
            # files and their names are never included in a resource.
            for kind, (filename, _) in _KINDS.items():
                path, _ = self.features.artifact(owner, record["id"], record["revision"], kind)
                if path.stat().st_size > 40 * 1024**2: raise ValidationError("模型文件超过 40 MB。")
                raw = path.read_bytes()
                if _digest(raw) != record["artifacts"][kind].get("sha256"): raise ValidationError("已提交实体文件校验失败。")
                parts[filename] = raw
            plan = _public_plan(record["plan"], name)
            imports = CadImportAssets(self.features.root / "imports")
            dependencies = imports.archive(owner, record["plan"], max_bytes=_MAX_BYTES - sum(map(len, parts.values())))
            remap = {}
            for old_id, sha in import_references(plan).items():
                new_id = "asset_" + _digest(f"{resource_id}\0{old_id}".encode())[:32]
                remap[old_id] = new_id
                raw = dependencies[f"assets/{old_id}.step"]
                old_meta = json.loads(dependencies[f"assets/{old_id}.json"])
                # Keep normalized STEP bytes: persistent topology face names
                # bind to this SHA. New IDs have no linkage to the private ID.
                parts[f"assets/{new_id}.step"] = raw
                parts[f"assets/{new_id}.json"] = _json({"id": new_id, "sha256": sha, "name": "社区导入实体",
                    "units": "mm", "sizeBytes": len(raw), "history": "imported_body",
                    "inspection": {key: old_meta.get("inspection", {}).get(key) for key in ("valid", "solidCount", "volumeMm3", "bbox")}}).encode()
            for feature in plan["features"]:
                if feature.get("op") == "import_step": feature["assetId"] = remap[feature["assetId"]]
            from .cad_feature_workspace import compile_manual_plan
            parts["plan.json"] = _json(compile_manual_plan(plan, record.get("suppressed", []))).encode()
            parts["editing.json"] = _json({"plan": plan, "suppressed": record.get("suppressed", []), "inspection": _metrics(record["inspection"])}).encode()
            if sum(map(len, parts.values())) > _MAX_BYTES: raise ValidationError("社区模型与导入依赖超过 100 MB。")
            value = {"format": FORMAT, "id": resource_id, "owner": owner, "fingerprint": fingerprint,
                "source": {"id": record["id"], "revision": record["revision"]}, "name": name, "description": description.strip(),
                "status": "published", "publishedAt": datetime.now(timezone.utc).isoformat(), "inspection": _metrics(record["inspection"]),
                "files": {key: _digest(raw) for key, raw in parts.items()}}
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=self.root) as temporary:
                folder = Path(temporary)
                for filename, raw in parts.items():
                    path = folder / filename; path.parent.mkdir(exist_ok=True); path.write_bytes(raw)
                (folder / "registry.json").write_text(_json(value))
                folder.rename(directory)
            return self.public(value, owner)

    def _parts(self, value):
        folder = self._directory(value["id"]); parts = {}; total = 0
        for filename, sha in value["files"].items():
            if filename not in {"model.step", "model.glb", "plan.json", "editing.json"} and not re.fullmatch(r"assets/asset_[a-f0-9]{32}\.(step|json)", filename):
                raise ValidationError("社区文件清单无效。")
            path = folder / filename
            if not path.is_file() or path.stat().st_size > 40 * 1024**2: raise ValidationError("社区模型文件缺失或超限。")
            total += path.stat().st_size
            if total > _MAX_BYTES: raise ValidationError("社区模型文件超限。")
            parts[filename] = path.read_bytes()
            if _digest(parts[filename]) != sha: raise ValidationError("社区模型校验失败。")
        if not {"model.step", "model.glb", "plan.json", "editing.json"} <= parts.keys(): raise ValidationError("社区模型文件缺失。")
        return parts

    def artifact(self, resource_id, kind):
        self._directory(resource_id)
        if kind not in _KINDS: raise NotFoundError("只提供公开 STEP 和 GLB 实体文件。")
        with _open_lock(self.root, resource_id):
            value = self._visible(resource_id)
            return self._parts(value)[_KINDS[kind][0]], _KINDS[kind][1]

    def withdraw(self, owner, resource_id):
        self._directory(resource_id)
        with _open_lock(self.root, resource_id):
            value = self._read(resource_id)
            if value["owner"] != owner: raise AuthorizationError("只有发布者可以撤下此资源。")
            value["status"] = "withdrawn"
            folder = self._directory(resource_id); pending = folder / ".registry.json"
            pending.write_text(_json(value)); pending.replace(folder / "registry.json")
            return self.public(value, owner)

    def open(self, owner, resource_id, data):
        if set(data) != {"fileId", "requestId"}: raise ValidationError("创建副本只接受新工作文件与请求编号。")
        file_id = _text(data["fileId"], "工作文件编号", 160); request_id = _request_id(data["requestId"])
        record_id = "feature_" + _digest(f"community-open\0{owner}\0{request_id}".encode())[:32]
        self._directory(resource_id)
        # Withdrawal serializes with the entire copy, so no new copy can pass
        # after withdrawal completed, including a retry for an earlier request.
        with _open_lock(self.root, resource_id), _open_lock(self.features.root, record_id):
            value = self._visible(resource_id)
            try: current = self.features.get(owner, record_id, 1)
            except FeatureNotFound: current = None
            if current:
                if current.get("communitySource") != {"resourceId": resource_id} or current["fileId"] != file_id:
                    raise ConflictError("重开请求编号已用于其他文件或资源。")
                return {"record": public_feature_record(current)}
            parts = self._parts(value); editing = json.loads(parts["editing.json"])
            plan = validate_plan(editing["plan"])
            CadImportAssets(self.features.root / "imports").restore_trusted(owner, plan, parts)
            folder = self.features.root / record_id / "community-r1"; folder.mkdir(parents=True, exist_ok=True)
            record = {"id": record_id, "revision": 1, "name": value["name"], "fileId": file_id, "sourceRun": None,
                "communitySource": {"resourceId": resource_id}, "status": "built", "plan": plan,
                "suppressed": editing["suppressed"], "inspection": editing["inspection"], "sharedDesign": None,
                "drawingAgreement": "not_checked", "productionReady": False,
                "changeNote": "从公开社区资源创建独立编辑副本", "updatedAt": datetime.now(timezone.utc).isoformat(), "artifacts": {}}
            try:
                for kind, (filename, mime) in {**_KINDS, "plan": ("plan.json", "application/json")}.items():
                    path = folder / filename; path.write_bytes(parts[filename])
                    record["artifacts"][kind] = {"path": str(path), "mimeType": mime, "sha256": _digest(parts[filename])}
                self.features._save(owner, record, 0)
            except Exception:
                shutil.rmtree(folder.parent, ignore_errors=True); raise
            return {"record": public_feature_record(record)}
