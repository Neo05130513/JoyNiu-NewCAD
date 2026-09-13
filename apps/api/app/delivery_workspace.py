"""Immutable, owner-scoped delivery snapshots of already exportable CAD files.

This service freezes files and provenance. It grants neither AI acceptance nor
manufacturing approval, and never substitutes a preview for an unavailable STEP.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
import zipfile

from .cad_design_workspace import DesignError, DesignNotFound
from .cad_feature_workspace import FeatureWorkspaceError, FeatureNotFound
from .native_drawing import DrawingError

MAX_SOURCE_REFS = 20
MAX_RESOLVED_SOURCES = 100
MAX_FILES = 300
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
_ID = re.compile(r"delivery_[a-f0-9]{32}\Z")
_FILE_ID = re.compile(r"file_[a-f0-9]{24}\Z")
_KINDS = {"engineering", "feature", "native", "cad_run"}
_MIMES = {"step": "application/step", "stp": "application/step", "glb": "model/gltf-binary",
          "dxf": "application/dxf", "json": "application/json", "pdf": "application/pdf",
          "csv": "text/csv;charset=utf-8", "svg": "image/svg+xml", "png": "image/png",
          "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
          "bmp": "image/bmp", "dwg": "application/acad"}


class DeliveryError(ValueError):
    def __init__(self, message, *, code="invalid_delivery", status=422):
        super().__init__(message)
        self.code, self.status = code, status


def _missing():
    return DeliveryError("交付包或来源文件不存在。", code="delivery_not_found", status=404)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _text(value, label, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise DeliveryError(f"{label}须为{'0' if empty else '1'} 至 {maximum} 个字符。")
    return value.strip()


def _filename(value):
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")
    suffix = Path(name).suffix
    if len(name) > 180:
        name = name[:180-len(suffix)] + suffix if len(suffix) <= 8 else name[:180]
    return name or "document"


def _reference(value):
    if not isinstance(value, dict) or set(value) - {"kind", "id", "revision"}:
        raise DeliveryError("来源引用格式无效。")
    kind, identity, revision = value.get("kind"), value.get("id"), value.get("revision")
    if not isinstance(kind, str) or kind not in _KINDS or not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
        raise DeliveryError("来源类型或编号无效。")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise DeliveryError("来源版本号须为正整数。")
    if kind == "engineering" and revision is not None:
        raise DeliveryError("工程设计以文件哈希锁定快照，不接受数值版本号。")
    return {"kind": kind, "id": identity, "revision": revision}


def _file_bytes(path, boundary, expected=None):
    """Read a regular file with an owner-boundary check and no symlink final hop."""
    path, original_boundary = Path(path), Path(boundary).absolute()
    boundary = original_boundary.resolve()
    if original_boundary != boundary:
        raise DeliveryError("来源目录不能通过符号链接指向其他位置。", code="source_integrity")
    if not path.resolve().is_relative_to(boundary):
        raise DeliveryError("来源文件路径不属于当前设计。", code="source_integrity")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FILE_BYTES:
                raise DeliveryError("单个来源文件须为 1 字节至 32 MiB 的普通文件。", code="source_size")
            data = handle.read(MAX_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise DeliveryError("来源文件在读取期间发生变化，请重试。", code="source_changed", status=409)
    except (OSError, ValueError):
        raise DeliveryError("来源文件缺失或不可读取，请恢复后再封存。", code="source_integrity") from None
    if expected and _sha(data) != expected:
        raise DeliveryError("来源文件与已存哈希不一致，拒绝封存。", code="source_integrity")
    return data


class DeliveryWorkspace:
    def __init__(self, root, *, engineering_store, feature_store, native_store, cad_store=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "deliveries.sqlite3"
        self.engineering, self.features, self.native, self.cad = engineering_store, feature_store, native_store, cad_store
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(owner, request_id))""")
        self.database.chmod(0o600)

    def _db(self):
        return sqlite3.connect(self.database, timeout=15)

    def _owner(self, owner):
        if not isinstance(owner, str) or not owner:
            raise _missing()
        folder = self.root / _sha(owner.encode())
        folder.mkdir(exist_ok=True, mode=0o700)
        if folder.is_symlink():
            raise _missing()
        return folder

    @contextmanager
    def _lock(self, owner):
        # File locks cover multiple API workers as well as threads and release on crash.
        handles = []
        try:
            for index in range(2):
                handle = (self.root / f".slot-{index}.lock").open("a+b")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                handles.append(handle)
                break
            if not handles:
                raise DeliveryError("已有两个交付包正在封存，请稍后重试。", code="delivery_busy", status=409)
            handle = (self._owner(owner) / ".create.lock").open("a+b")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeliveryError("当前账号正在封存交付包，请稍后用相同请求编号重试。", code="delivery_busy", status=409) from None
            yield
        finally:
            for handle in reversed(handles):
                handle.close()

    def _load(self, owner, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise _missing()
        with self._db() as db:
            row = db.execute("SELECT payload FROM deliveries WHERE owner=? AND id=?", (owner, identity)).fetchone()
        if not row:
            raise _missing()
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            raise DeliveryError("交付索引损坏，请联系管理员恢复备份。", code="delivery_integrity") from None

    def _replay(self, owner, request_id, fingerprint):
        with self._db() as db:
            row = db.execute("SELECT id,fingerprint FROM deliveries WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
        if row:
            if row[1] != fingerprint:
                raise DeliveryError("请求编号已用于不同交付内容，请使用新的请求编号。", code="request_conflict", status=409)
            return self.get(owner, row[0])

    def list(self, owner):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM deliveries WHERE owner=? ORDER BY rowid DESC LIMIT 500", (owner,)).fetchall()
        return [{"id": item["id"], "title": item["title"], "createdAt": item["createdAt"],
                 "sourceCount": len(item["sources"]), "fileCount": len(item["files"]),
                 "verificationStatus": item["verification"]["status"]}
                for item in (json.loads(row[0]) for row in rows)]

    def get(self, owner, identity):
        result = self._load(owner, identity)
        folder = self._owner(owner) / identity
        manifest = _file_bytes(folder / "manifest.json", folder, result["manifest"]["sha256"])
        expected = {"schemaVersion": "joyniu-delivery-v1", **{key: value for key, value in result.items() if key not in {"manifest", "archive"}}}
        if json.loads(manifest) != expected:
            raise DeliveryError("交付清单与索引记录不一致，请联系管理员恢复备份。", code="delivery_integrity")
        return result

    def file(self, owner, identity, file_id):
        result = self.get(owner, identity)
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise _missing()
        item = next((item for item in result["files"] if item["id"] == file_id), None)
        if not item:
            raise _missing()
        folder = self._owner(owner) / identity
        path = folder / "files" / file_id / item["name"]
        _file_bytes(path, folder, item["sha256"])
        return path, item

    def archive(self, owner, identity):
        result = self.get(owner, identity)
        path = self._owner(owner) / identity / "archive.zip"
        # Archives may exceed the per-source size bound. Hash in bounded chunks.
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != result["archive"]["bytes"]:
                raise OSError()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != result["archive"]["sha256"]:
                raise OSError()
        except OSError:
            raise DeliveryError("交付 ZIP 缺失或校验失败，请联系管理员恢复备份。", code="delivery_integrity") from None
        return path, {**result["archive"], "name": _filename(result["title"]) + ".zip", "mimeType": "application/zip"}

    def _cad_record(self, owner, identity, revision=None):
        if self.cad is None:
            raise _missing()
        try:
            record = self.cad.load(identity, revision)
        except (KeyError, ValueError, TypeError):
            raise _missing() from None
        if record.get("owner") != owner:
            raise _missing()
        return record

    def _native_record(self, owner, identity, revision=None):
        from .native_drawing import _read, _snapshot
        with self.native._db() as db:
            head = self.native._record(db, owner, identity)
            revision = head["revision"] if revision is None else revision
            row = db.execute("SELECT dxf,metadata FROM native_drawing_revisions WHERE document_id=? AND revision=?", (identity, revision)).fetchone()
        if not row:
            raise _missing()
        data, metadata = bytes(row["dxf"]), json.loads(row["metadata"])
        if not 0 < len(data) <= MAX_FILE_BYTES:
            raise DeliveryError("原生图纸版本超过 32 MiB 上限。")
        doc = _read(data)
        record = {**head, "revision": revision, "name": metadata.get("name", head["name"])}
        snapshot = _snapshot(doc, record, metadata)
        # The native revision table does not store its own timestamp. Do not
        # attach the mutable head's updatedAt to an immutable historical version.
        snapshot.pop("updatedAt", None)
        # Retain design parameters/constraints, but never database or converter internals.
        snapshot["source"] = {key: metadata.get("source", {}).get(key) for key in ("filename", "sha256", "contentType") if key in metadata.get("source", {})}
        if "filename" in snapshot["source"]:
            snapshot["source"]["filename"] = _filename(snapshot["source"]["filename"])
        return snapshot, data

    def sources(self, owner):
        candidates = []
        for item in self.engineering.list(owner)[:100]:
            candidates.append({"kind": "engineering", "id": item["id"], "revision": None, "name": item["name"], "status": "built"})
        for item in self.features.list(owner):
            candidates.append({"kind": "feature", **{key: item[key] for key in ("id", "revision", "name", "status")}})
            if item["status"] != "built":
                version = next((v for v in self.features.versions(owner, item["id"]) if v["status"] == "built"), None)
                if version:
                    historical = self.features.get(owner, item["id"], version["revision"])
                    candidates.append({"kind": "feature", "id": item["id"], "revision": version["revision"], "name": historical["name"], "status": "built"})
        for item in self.native.list(owner)["items"][:100]:
            candidates.append({"kind": "native", **{key: item[key] for key in ("id", "revision", "name")}, "status": "saved"})
        if self.cad:
            with self.cad._connect() as db:
                rows = db.execute("SELECT payload FROM cad_runs r WHERE owner=? AND revision=(SELECT MAX(revision) FROM cad_runs WHERE run_id=r.run_id) ORDER BY rowid DESC LIMIT 100", (owner,)).fetchall()
            for row in rows:
                item = json.loads(row[0])
                candidates.append({"kind": "cad_run", "id": item["runId"], "revision": item["revision"], "name": (item.get("plan") or {}).get("name") or "AI 建模任务", "status": item["status"]})
        for item in candidates:
            item["name"] = _filename(item["name"])
            reason = ""
            try:
                if item["kind"] == "feature":
                    if item["status"] != "built":
                        reason = "当前版本尚未成功重建；可选择已重建的历史版本。"
                    else:
                        self.features.artifact(owner, item["id"], item["revision"], "step")
                elif item["kind"] == "engineering":
                    record = self.engineering.get(owner, item["id"])
                    if not record.get("metrics", {}).get("valid"):
                        reason = "工程实体未通过有效性检查。"
                    self.engineering.artifact(owner, item["id"], "step")
                elif item["kind"] == "cad_run":
                    from .cad_agent_api import _step_export_issue
                    record = self._cad_record(owner, item["id"], item["revision"])
                    reason = _step_export_issue(record) or ""
                    if not reason and not (record.get("artifacts") or {}).get("step"):
                        reason = "已确认任务缺少 STEP 文件。"
            except (FeatureWorkspaceError, DesignError, DeliveryError, ValueError, KeyError, TypeError):
                reason = "来源文件缺失或不可读取，请恢复后再封存。"
            item.update(eligible=not reason, reason=reason)
        return candidates

    def create(self, owner, data):
        if not isinstance(data, dict) or set(data) - {"requestId", "title", "notes", "sourceRefs"}:
            raise DeliveryError("交付请求字段无效。")
        request_id = data.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
            raise DeliveryError("请提供 8 至 128 位字母、数字、下划线或连字符的请求编号。")
        title = _text(data.get("title"), "交付名称", 180)
        notes = _text(data.get("notes", ""), "交付说明", 4000, empty=True)
        refs = data.get("sourceRefs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_SOURCE_REFS:
            raise DeliveryError("每次请选择 1 至 20 个来源。")
        refs = [_reference(item) for item in refs]
        refs.sort(key=lambda item: (item["kind"], item["id"], item["revision"] or 0))
        if len({_json(item) for item in refs}) != len(refs):
            raise DeliveryError("不能重复选择相同来源版本。")
        fingerprint = _sha(_json({"title": title, "notes": notes, "sourceRefs": refs}))
        if result := self._replay(owner, request_id, fingerprint):
            return result
        with self._lock(owner):
            if result := self._replay(owner, request_id, fingerprint):
                return result
            with self._db() as db:
                count = db.execute("SELECT COUNT(*) FROM deliveries WHERE owner=?", (owner,)).fetchone()[0]
            if count >= 500:
                raise DeliveryError("账号已达到 500 个交付快照上限，请联系管理员归档。", code="delivery_limit")
            identity = "delivery_" + uuid4().hex
            temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=self._owner(owner)))
            final = self._owner(owner) / identity
            try:
                sources, files, steps = self._collect(owner, refs, temporary)
                self._verify_steps(steps, temporary)
                confirmed = all(source["kind"] == "cad_run" for source in sources)
                verification = {"status": "confirmed_source" if confirmed else "requires_review",
                    "message": ("所选 AI 来源通过原有确认与导出门禁；封存不代表制造放行。" if confirmed else
                        "文件已按来源版本封存；包含手工、工程或二维设计，未作制造确认。请核对尺寸、工程图和适用工况。")}
                record = {"id": identity, "title": title, "notes": notes,
                          "createdAt": datetime.now(timezone.utc).isoformat(),
                          "sources": sources, "files": files, "verification": verification}
                manifest = _json({"schemaVersion": "joyniu-delivery-v1", **record})
                (temporary / "manifest.json").write_bytes(manifest)
                with zipfile.ZipFile(temporary / "archive.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    archive.writestr("manifest.json", manifest)
                    for item in files:
                        relative = f"files/{item['id']}/{item['name']}"
                        archive.write(temporary / relative, relative)
                archive_path = temporary / "archive.zip"
                digest = hashlib.sha256()
                with archive_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                record["manifest"] = {"name": "manifest.json", "bytes": len(manifest), "sha256": _sha(manifest)}
                record["archive"] = {"bytes": archive_path.stat().st_size, "sha256": digest.hexdigest()}
                temporary.rename(final)
                with self._db() as db:
                    db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)", (identity, owner, request_id, fingerprint, _json(record).decode()))
                return record
            except (FeatureNotFound, DesignNotFound):
                raise _missing() from None
            except DrawingError as exc:
                raise DeliveryError("原生图纸来源不可读取或版本无效。", status=exc.status, code="source_integrity") from None
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if isinstance(exc, DeliveryError):
                    raise
                raise DeliveryError("来源文件不完整或校验失败，未创建交付包。", code="source_integrity") from None
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
                # A failed insert must not leave a downloadable unindexed snapshot.
                with self._db() as db:
                    saved = db.execute("SELECT 1 FROM deliveries WHERE id=?", (identity,)).fetchone()
                if not saved:
                    shutil.rmtree(final, ignore_errors=True)

    def _collect(self, owner, refs, folder):
        sources, files, steps, visited = [], [], [], set()
        pending = [(ref, True) for ref in refs]
        total = 0

        def add(source, name, payload, *, expected_geometry=None):
            nonlocal total
            if not 0 < len(payload) <= MAX_FILE_BYTES or len(files) >= MAX_FILES or total + len(payload) > MAX_TOTAL_BYTES:
                raise DeliveryError("交付包最多 300 个文件、总计 200 MiB，单文件最多 32 MiB。", code="delivery_size")
            name = _filename(name)
            extension = Path(name).suffix.lower().lstrip(".")
            if extension not in _MIMES:
                raise DeliveryError("来源含有不支持的交付文件格式。")
            if extension == "glb" and (payload[:4] != b"glTF" or len(payload) < 12 or int.from_bytes(payload[8:12], "little") != len(payload)):
                raise DeliveryError("GLB 预览文件校验失败。", code="source_integrity")
            if extension in {"step", "stp"} and b"ISO-10303-21" not in payload[:4096]:
                raise DeliveryError("STEP 实体文件校验失败。", code="source_integrity")
            file_id = "file_" + uuid4().hex[:24]
            target = folder / "files" / file_id / name
            target.parent.mkdir(parents=True)
            target.write_bytes(payload)
            item = {"id": file_id, "name": name, "bytes": len(payload), "sha256": _sha(payload), "mimeType": _MIMES[extension]}
            files.append(item); total += len(payload)
            source.setdefault("fileIds", []).append(file_id)
            if extension in {"step", "stp"}:
                steps.append({"path": str(target), "expected": expected_geometry or {}})

        while pending:
            ref, selected = pending.pop(0)
            kind, identity, revision = ref["kind"], ref["id"], ref.get("revision")
            # Resolve the exact revision before deduplicating latest aliases.
            if kind == "feature":
                record = self.features.get(owner, identity, revision); revision = record["revision"]
            elif kind == "engineering":
                record = self.engineering.get(owner, identity)
            elif kind == "cad_run":
                record = self._cad_record(owner, identity, revision); revision = record["revision"]
            else:
                record, native_dxf = self._native_record(owner, identity, revision); revision = record["revision"]
            key = kind, identity, revision
            if key in visited:
                continue
            visited.add(key)
            if len(visited) > MAX_RESOLVED_SOURCES:
                raise DeliveryError("关联来源超过 100 项，请拆分交付。")
            source = {"kind": kind, "id": identity, "revision": revision,
                      "name": _filename(record.get("name") or (record.get("plan") or {}).get("name") or "CAD 设计"),
                      "status": record.get("status", "saved" if kind == "native" else "built"),
                      "provenance": {"selected": selected, "manufacturingReleased": False}}
            sources.append(source)
            provenance = source["provenance"]
            if kind == "engineering":
                if not record.get("metrics", {}).get("valid"):
                    raise DeliveryError("工程设计缺少有效实体检查，不能封存。")
                provenance.update(fileId=record.get("fileId"), drawingAgreement="not_checked")
                linked = record.get("sourceFeature")
                if not linked:
                    # Older feature versions already carry a proved forward link.
                    with self.features._db() as db:
                        row = db.execute("SELECT id,revision FROM manual_features WHERE owner=? AND json_extract(payload,'$.sharedDesign.id')=? ORDER BY revision DESC LIMIT 1", (owner, identity)).fetchone()
                    linked = {"id": row[0], "revision": row[1]} if row else None
                if linked:
                    feature = self.features.get(owner, linked["id"], linked["revision"])
                    if (feature.get("sharedDesign") or {}).get("id") != identity:
                        raise DeliveryError("工程设计的来源特征关联不一致。", code="source_integrity")
                    provenance["sourceFeature"] = {"id": feature["id"], "revision": feature["revision"]}
                    pending.append(({"kind": "feature", **provenance["sourceFeature"]}, False))
                for item in record.get("instances", []):
                    child = self.engineering.get(owner, item["designId"])
                    pending.append(({"kind": "engineering", "id": child["id"], "revision": None}, False))
                provenance["instances"] = [{key: item[key] for key in ("id", "designId", "position", "rotation", "fixed") if key in item} for item in record.get("instances", [])]
                origin = record.get("source") or {}
                if origin.get("kind") == "assembly_plan":
                    provenance["assemblyPlan"] = {key: origin[key] for key in ("kind", "jobId", "partId") if key in origin}
                artifacts = {key: value for key, value in record.get("artifacts", {}).items() if key in {"step", "glb", "bom"}}
                drawing = record.get("lastDrawing") or {}
                for key in drawing.get("artifacts", {}):
                    artifacts[key] = record["artifacts"][key]
                if not {"step", "glb"} <= set(artifacts) or (record.get("kind") == "assembly" and "bom" not in artifacts):
                    raise DeliveryError("工程设计缺少 STEP、GLB 或装配 BOM。", code="source_integrity")
                for key, item in artifacts.items():
                    path, _ = self.engineering.artifact(owner, identity, key)
                    payload = _file_bytes(path, self.engineering._directory(owner, identity), item.get("sha256"))
                    add(source, item["filename"], payload, expected_geometry=record["metrics"] if key == "step" else None)
                document = {key: record[key] for key in ("id", "fileId", "name", "kind", "units", "metrics", "instances", "constraints", "constraintResults", "interference", "bom") if key in record}
                # Public instances contain no worker paths; copy only known fields anyway.
                document["instances"] = provenance["instances"]
                if drawing:
                    document["drawing"] = {key: drawing[key] for key in ("page", "layouts", "dimensions", "settings", "curveToleranceMm") if key in drawing}
                document["provenance"] = provenance
                add(source, "engineering-design.json", _json(document))
            elif kind == "feature":
                if record.get("status") != "built" or not (record.get("inspection") or {}).get("valid"):
                    raise DeliveryError("所选特征版本尚未成功重建，不能封存。", code="source_not_exportable", status=409)
                provenance.update(fileId=record.get("fileId"), drawingAgreement="not_checked", changeNote=record.get("changeNote", ""))
                source_run = record.get("sourceRun")
                if source_run:
                    original = self._cad_record(owner, source_run["runId"], source_run["revision"])
                    original_hash = _sha(json.dumps(original.get("plan"), sort_keys=True).encode())
                    if original_hash != source_run.get("planSha256"):
                        raise DeliveryError("特征来源任务计划哈希不一致。", code="source_integrity")
                    provenance["sourceRun"] = {key: source_run[key] for key in ("runId", "revision", "statusAtFork", "planSha256") if key in source_run}
                    # Reference an unconfirmed origin without copying its blocked files.
                    from .cad_agent_api import _step_export_issue
                    provenance["sourceRun"]["artifactsIncluded"] = not bool(_step_export_issue(original))
                    if provenance["sourceRun"]["artifactsIncluded"]:
                        pending.append(({"kind": "cad_run", "id": original["runId"], "revision": original["revision"]}, False))
                shared = record.get("sharedDesign")
                if shared:
                    actual = self.engineering.get(owner, shared["id"])
                    provenance["sharedDesignId"] = actual["id"]
                    pending.append(({"kind": "engineering", "id": actual["id"], "revision": None}, False))
                for fmt in ("step", "glb", "plan"):
                    path, _ = self.features.artifact(owner, identity, revision, fmt)
                    payload = _file_bytes(path, self.features.root / identity, record["artifacts"][fmt].get("sha256"))
                    if fmt == "plan" and json.loads(payload) != record["plan"]:
                        # Suppression changes the executed plan, which is checked separately.
                        from .cad_feature_workspace import compile_manual_plan
                        if json.loads(payload) != compile_manual_plan(record["plan"], record["suppressed"]):
                            raise DeliveryError("特征计划文件与版本不一致。", code="source_integrity")
                    add(source, "plan.json" if fmt == "plan" else f"model.{fmt}", payload,
                        expected_geometry=record["inspection"] if fmt == "step" else None)
                add(source, "feature-design.json", _json({key: record[key] for key in ("id", "revision", "name", "plan", "suppressed", "resolvedParameters", "changeNote", "drawingAgreement", "productionReady") if key in record}))
            elif kind == "native":
                provenance.update(drawingAgreement="not_checked", sourceFile=record.get("source", {}))
                add(source, source["name"] + ".dxf", native_dxf)
                add(source, "native-document.json", _json(record))
            else:
                from .cad_agent_api import _step_export_issue, _artifact_entries
                if issue := _step_export_issue(record):
                    raise DeliveryError(issue, code="source_not_exportable", status=409)
                if not (record.get("artifacts") or {}).get("step"):
                    raise DeliveryError("已确认任务缺少 STEP 实体。", code="source_integrity")
                provenance.update(confirmedAt=record.get("confirmedAt"), confirmationStatus="ready", drawingAgreement=(record.get("drawingReview") or {}).get("status", "not_checked"))
                for _, fmt, _, item in _artifact_entries(record["artifacts"]):
                    path = self.cad.checked_path(item["path"])
                    payload = _file_bytes(path, self.cad.directory(identity), item.get("sha256"))
                    add(source, path.name, payload, expected_geometry=record.get("inspection") if fmt == "step" else None)
                add(source, "cad-plan.json", _json(record["plan"]))
                provenance["sourceFiles"] = []
                for item in record.get("files", []):
                    path = self.cad.checked_path(item["path"])
                    # A resumed run may reuse an upload from an owned parent run.
                    containing_run = path.relative_to(self.cad.root).parts[0]
                    self._cad_record(owner, containing_run)
                    payload = _file_bytes(path, self.cad.directory(containing_run), item.get("sha256"))
                    name = _filename(item["filename"])
                    add(source, "source-" + name, payload)
                    provenance["sourceFiles"].append({"filename": name, "sha256": _sha(payload)})
        return sources, files, steps

    def _verify_steps(self, steps, directory):
        if not steps:
            return
        request, response = directory / "validation-request.json", directory / "validation-result.json"
        request.write_bytes(_json({"steps": steps, "response": str(response)}))
        try:
            environment = {key: value for key, value in os.environ.items() if key in {
                "PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "JOYNIU_DISABLE_CADQUERY"}}
            environment.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
            process = subprocess.Popen([sys.executable, "-m", "app.delivery_workspace", str(request)],
                cwd=str(Path(__file__).resolve().parents[1]), env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > 75:
                        raise DeliveryError("交付实体检查超时，请减少来源后重试。", code="delivery_timeout", status=409)
                    if sys.platform == "darwin":
                        try:
                            rss = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, timeout=1)
                            if rss.returncode == 0 and int(rss.stdout.strip() or 0) > 4096 * 1024:
                                raise DeliveryError("交付实体检查达到内存上限，请拆分来源后重试。", code="delivery_limit", status=409)
                        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                            if isinstance(exc, DeliveryError):
                                raise
                    time.sleep(.1)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            result = json.loads(response.read_text()) if response.is_file() else {}
            if result.get("valid") is not True:
                raise DeliveryError("交付 STEP 的独立实体回读或尺寸校验失败，未创建交付包。", code="source_integrity")
        finally:
            request.unlink(missing_ok=True); response.unlink(missing_ok=True)


def _validate_worker(request):
    import resource
    from .cad_executor import _apply_worker_limits
    _apply_worker_limits(60, 4096 * 1024 * 1024)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from .cad_design_workspace import _load
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    for item in request["steps"]:
        shape = _load(Path(item["path"]))
        box = Bnd_Box(); BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
        bounds = box.Get(); size = [bounds[i+3] - bounds[i] for i in range(3)]
        expected = item["expected"]
        volume = expected.get("volumeMm3", expected.get("volume"))
        expected_size = (expected.get("bbox") or {}).get("size", expected.get("size"))
        if volume is not None and not math.isclose(shape.Volume(), volume, rel_tol=2e-5, abs_tol=1e-6):
            raise ValueError("volume mismatch")
        if expected_size is not None and any(not math.isclose(a, b, rel_tol=2e-5, abs_tol=1e-5) for a, b in zip(size, expected_size)):
            raise ValueError("size mismatch")
        if expected.get("solidCount") is not None and len(shape.Solids()) != expected["solidCount"]:
            raise ValueError("solid count mismatch")
    return {"valid": True}


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        outcome = _validate_worker(request)
    except Exception:
        outcome = {"valid": False}
    Path(request["response"]).write_bytes(_json(outcome))
