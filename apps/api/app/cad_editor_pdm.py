"""Server-authored PDM bundles of exact, owned manual CAD revisions.

The registry is written in the same PDM transaction as the immutable blob.
Ordinary user-uploaded PDM ZIPs can never authorize persistent CAD bindings.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import threading
from uuid import uuid4
import zipfile

from .cad_feature_workspace import CadFeatureWorkspace, FeatureNotFound, public_feature_record
from .platform import ConflictError, NotFoundError, ValidationError

FORMAT = "joyniu-cad-pdm-v1"
_LOCK = threading.RLock()
_FILES = {"step": "model.step", "glb": "model.glb", "plan": "plan.json"}
_MAX_BUNDLE_BYTES = 100 * 1024**2


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, label, limit=180):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValidationError(f"{label}须填写 1 至 {limit} 个字符。")
    return value.strip()


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        raise ValidationError("保存请求编号无效。")
    return value


@contextmanager
def _open_lock(root, record_id):
    # Coordinate same-request recovery across API processes. The file remains
    # as a harmless lock inode so concurrent waiters never lock different files.
    locks = root / "pdm-locks"; locks.mkdir(exist_ok=True)
    with (locks / (record_id + ".lock")).open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


class CadEditorPdm:
    def __init__(self, pdm, features: CadFeatureWorkspace, designs):
        self.pdm, self.features, self.designs = pdm, features, designs
        with pdm._lock, pdm._connection:
            pdm._connection.execute("""CREATE TABLE IF NOT EXISTS cad_editor_pdm_versions (
                version_id TEXT PRIMARY KEY REFERENCES pdm_versions(id),
                source_owner TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                archive_sha256 TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                metadata_json TEXT NOT NULL)""")
            pdm._connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS cad_editor_pdm_requests ON cad_editor_pdm_versions(source_owner,request_id)")

    def documents(self, project_id):
        entries = []
        for document in self.pdm.list_documents(project_id):
            versions = []
            for version in self.pdm.list_versions(document.id):
                with self.pdm._lock:
                    row = self.pdm._connection.execute("SELECT metadata_json FROM cad_editor_pdm_versions WHERE version_id=?", (version.id,)).fetchone()
                if row:
                    meta = json.loads(row[0])
                    versions.append({**version.to_dict(), "source": meta["source"], "engineeringDesignId": meta["engineeringDesignId"]})
            if versions:
                entries.append({"document": document.to_dict(), "versions": versions})
        return entries

    def _saved(self, version_id):
        version = self.pdm.get_version(version_id)
        with self.pdm._lock:
            row = self.pdm._connection.execute("SELECT metadata_json FROM cad_editor_pdm_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("此版本不是编辑器保存的可信 CAD 版本。")
        metadata = json.loads(row[0])
        document = self.pdm.get_document(version.document_id)
        return {"document": document.to_dict(), "version": version.to_dict(), **metadata,
                "pdmReference": {"projectId": document.project_id, "documentId": document.id, "versionId": version.id,
                                 "revision": version.revision, "source": metadata["source"]}}

    def save(self, owner, data):
        if set(data) - {"projectId", "name", "featureId", "revision", "requestId", "expectedCurrentRevision"}:
            raise ValidationError("保存只接受项目、名称和已提交版本引用。")
        request_id = _request_id(data.get("requestId"))
        project_id = _text(data.get("projectId"), "项目编号")
        name = _text(data.get("name"), "名称")
        if type(data.get("revision")) is not int or data["revision"] < 1:
            raise ValidationError("请选择精确的已提交版本。")
        expected = data.get("expectedCurrentRevision")
        if type(expected) is not int or expected < 0:
            raise ValidationError("须提供 PDM 文档当前版本号。")
        fingerprint = hashlib.sha256(_json({key: data.get(key) for key in ("projectId", "name", "featureId", "revision")}).encode()).hexdigest()
        with _LOCK, self.pdm._lock:
            # A retry can recover a completed transfer even after its original
            # working copy was removed. Access to the target project is checked
            # by the route on every call, including this idempotent path.
            prior = self.pdm._connection.execute("SELECT version_id,fingerprint FROM cad_editor_pdm_versions WHERE source_owner=? AND request_id=?", (owner, request_id)).fetchone()
            if prior:
                if prior[1] != fingerprint: raise ConflictError("同一请求不能保存不同的 CAD 版本。")
                return self._saved(prior[0])
            project = self.pdm.get_project(project_id)
            if project.status == "archived": raise ConflictError("项目已归档，不能保存新版本。")
            documents = self.pdm.list_documents(project_id)
            document = next((item for item in documents if item.name == name), None)
            if document:
                same = self.pdm._connection.execute("SELECT r.version_id FROM cad_editor_pdm_versions r JOIN pdm_versions v ON v.id=r.version_id WHERE v.document_id=? AND r.source_owner=? AND r.fingerprint=?", (document.id, owner, fingerprint)).fetchone()
                if same: return self._saved(same[0])
                if document.metadata.get("format") != FORMAT: raise ConflictError("此名称已由其他类型的 PDM 文档使用。")
                if document.status in {"released", "obsolete", "archived"}: raise ConflictError("该 PDM 文档状态不允许追加编辑版本。")
            if expected != (document.current_revision if document else 0): raise ConflictError("PDM 版本已变化，请刷新列表后保存。")
            record = self.features.get(owner, data.get("featureId"), data["revision"])
            if record.get("status") != "built" or record.get("inspection", {}).get("stepReadback", {}).get("valid") is not True:
                raise ValidationError("请先完成实体重建，再保存到 PDM。")
            parts = {}
            for kind in ("step", "glb", "plan"):
                path, _ = self.features.artifact(owner, record["id"], record["revision"], kind)
                if path.stat().st_size > 40 * 1024**2: raise ValidationError("CAD 文件超过保存大小限制。")
                payload = path.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                if digest != record["artifacts"][kind].get("sha256"):
                    raise ValidationError("已提交实体文件校验失败，请重新构建。")
                parts[_FILES[kind]] = payload
            public = public_feature_record(record)
            public.pop("sharedDesign", None)
            parts["record.json"] = _json(public).encode()
            # Archive dependencies from the complete editing plan, including
            # suppressed bodies, so a later restore can unsuppress/rebuild.
            from .cad_import_assets import CadImportAssets
            parts.update(CadImportAssets(self.features.root / "imports").archive(owner, record["plan"], max_bytes=_MAX_BUNDLE_BYTES-sum(len(value) for value in parts.values())))
            if sum(len(value) for value in parts.values()) > _MAX_BUNDLE_BYTES:
                raise ValidationError("包含导入源的 PDM CAD 包超过 100 MB。")
            source = {"featureId": record["id"], "revision": record["revision"], "fileId": record["fileId"]}
            manifest = {"format": FORMAT, "source": source, "files": {key: hashlib.sha256(value).hexdigest() for key, value in parts.items()}}
            if sum(len(value) for value in parts.values()) + len(_json(manifest).encode()) > _MAX_BUNDLE_BYTES:
                raise ValidationError("包含导入源的 PDM CAD 包超过 100 MB。")
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                for filename, payload in parts.items(): archive.writestr(filename, payload)
                archive.writestr("manifest.json", _json(manifest))
            archive_bytes = stream.getvalue()
            digest = hashlib.sha256(archive_bytes).hexdigest()
            # Independently import the exact STEP into the real engineering
            # library, not the optional sharedDesign field of a manual record.
            engineering = self.designs.import_step(owner, name + ".step", parts["model.step"], record["fileId"], name,
                source_feature={"id": record["id"], "revision": record["revision"]})
            metadata = {"format": FORMAT, "source": source, "engineeringDesignId": engineering["id"],
                        "drawingAgreement": "not_checked", "productionReady": False}
            now = datetime.now(timezone.utc).isoformat()
            document_id = document.id if document else "doc_" + uuid4().hex
            version_id = "ver_" + uuid4().hex
            try:
                with self.pdm._connection as db:
                    db.execute("BEGIN IMMEDIATE")
                    completed = db.execute("SELECT version_id,fingerprint FROM cad_editor_pdm_versions WHERE source_owner=? AND request_id=?", (owner,request_id)).fetchone()
                    if completed:
                        if completed[1] != fingerprint: raise ConflictError("同一请求不能保存不同的 CAD 版本。")
                        shutil.rmtree(self.designs._directory(owner, engineering["id"]), ignore_errors=True)
                        return self._saved(completed[0])
                    current_project = db.execute("SELECT status FROM pdm_projects WHERE id=?", (project_id,)).fetchone()
                    if not current_project or current_project[0] == "archived": raise ConflictError("项目已归档或失效，不能保存新版本。")
                    if not document and db.execute("SELECT id FROM pdm_documents WHERE project_id=? AND name=? AND deleted_at IS NULL", (project_id,name)).fetchone():
                        raise ConflictError("同名 PDM 文档已由其他操作创建，请刷新后重试。")
                    if document:
                        live = db.execute("SELECT status FROM pdm_documents WHERE id=? AND deleted_at IS NULL", (document_id,)).fetchone()
                        if not live or live[0] in {"released", "obsolete", "archived"}: raise ConflictError("该 PDM 文档状态已变化，请刷新后重试。")
                    if not document:
                        db.execute("INSERT INTO pdm_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                            (document_id, project_id, name, "model", "draft", owner, None, 0, _json({"format": FORMAT}), now, now))
                    updated = db.execute("UPDATE pdm_documents SET current_revision=?,current_version_id=?,updated_at=? WHERE id=? AND current_revision=? AND deleted_at IS NULL", (expected + 1, version_id, now, document_id, expected))
                    if updated.rowcount != 1: raise ConflictError("PDM 文档已被其他操作更新。")
                    db.execute("INSERT INTO pdm_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (version_id, document_id, expected + 1, f"v{expected + 1}", name + ".joycad.zip", "application/zip", archive_bytes,
                         len(archive_bytes), digest, owner, now, f"CAD 精确版本 r{record['revision']}", _json(metadata)))
                    db.execute("INSERT INTO cad_editor_pdm_versions VALUES (?,?,?,?,?,?,?,?)",
                        (version_id, owner, record["id"], record["revision"], digest, request_id, fingerprint, _json(metadata)))
                    self.pdm._record_audit(owner, "cad_editor.saved", "version", version_id, {"document_id": document_id, "source": source, "sha256": digest})
            except Exception:
                shutil.rmtree(self.designs._directory(owner, engineering["id"]), ignore_errors=True)
                raise
            return self._saved(version_id)

    def bundle(self, version_id):
        saved = self._saved(version_id)
        with self.pdm._lock:
            row = self.pdm._connection.execute("SELECT archive_sha256 FROM cad_editor_pdm_versions WHERE version_id=?", (version_id,)).fetchone()
        payload = self.pdm.get_version_content(version_id)
        if hashlib.sha256(payload).hexdigest() != row[0]: raise ValidationError("PDM CAD 包校验失败。")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            base = {"model.step", "model.glb", "plan.json", "record.json", "manifest.json"}
            if len(names) != len(set(names)) or not base.issubset(names) or any(name not in base and not re.fullmatch(r"assets/asset_[a-f0-9]{32}\.(step|json)", name) for name in names):
                raise ValidationError("PDM CAD 包内容不完整。")
            if any(item.file_size > 40 * 1024**2 for item in archive.infolist()) or sum(item.file_size for item in archive.infolist()) > _MAX_BUNDLE_BYTES: raise ValidationError("PDM CAD 包内容超限。")
            parts = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(parts.pop("manifest.json"))
        if manifest.get("format") != FORMAT or manifest.get("source") != saved["source"] or manifest.get("files") != {key: hashlib.sha256(value).hexdigest() for key, value in parts.items()}:
            raise ValidationError("PDM CAD 包版本关联或内容校验失败。")
        from .cad_import_assets import import_references
        refs = import_references(json.loads(parts["record.json"])["plan"])
        expected_assets = {f"assets/{asset_id}.{suffix}" for asset_id in refs for suffix in ("step", "json")}
        if {name for name in parts if name.startswith("assets/")} != expected_assets:
            raise ValidationError("PDM CAD 包导入源关联不完整。")
        return saved, parts

    def open(self, owner, version_id, data):
        if set(data) != {"fileId", "requestId"}: raise ValidationError("重开须提供新工作文件和请求编号。")
        file_id = _text(data["fileId"], "工作文件编号", 160)
        request_id = _request_id(data["requestId"])
        saved, parts = self.bundle(version_id)
        origin = json.loads(parts["record.json"])
        from .cad_import_assets import CadImportAssets
        CadImportAssets(self.features.root / "imports").restore_trusted(owner, origin["plan"], parts)
        record_id = "feature_" + hashlib.sha256(f"{owner}\0{request_id}".encode()).hexdigest()[:32]
        reference = saved["pdmReference"]
        with _LOCK, _open_lock(self.features.root, record_id):
            try: current = self.features.get(owner, record_id)
            except FeatureNotFound: current = None
            if current:
                first = self.features.get(owner, record_id, 1)
                if first.get("pdmSource") != reference or first["fileId"] != file_id: raise ConflictError("重开请求编号已用于其他文件。")
                return {"record": public_feature_record(first), "pdmReference": reference}
            folder = self.features.root / record_id / "pdm-r1"
            folder.mkdir(parents=True, exist_ok=True)
            record = {**copy.deepcopy(origin), "id": record_id, "revision": 1, "name": saved["document"]["name"],
                      "fileId": file_id, "sourceRun": None, "sharedDesign": None, "pdmSource": reference,
                      "drawingAgreement": "not_checked", "productionReady": False,
                      "updatedAt": datetime.now(timezone.utc).isoformat(), "changeNote": f"从 PDM v{saved['version']['revision']} 创建独立编辑副本", "artifacts": {}}
            try:
                for kind, filename in _FILES.items():
                    path = folder / filename; path.write_bytes(parts[filename])
                    record["artifacts"][kind] = {"path": str(path), "mimeType": {"step": "application/step", "glb": "model/gltf-binary", "plan": "application/json"}[kind], "sha256": hashlib.sha256(parts[filename]).hexdigest()}
                self.features._save(owner, record, 0)
            except Exception:
                shutil.rmtree(folder.parent, ignore_errors=True)
                raise
        return {"record": public_feature_record(record), "pdmReference": reference}
