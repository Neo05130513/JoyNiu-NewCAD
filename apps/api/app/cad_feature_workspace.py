"""Owned manual feature-plan versions, independent of AI drawing acceptance."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from uuid import uuid4

from .cad_executor import execute_cad_plan, preview_cad_plan
from .cad_plan import PlanValidationError, validate_plan

_ID = re.compile(r"feature_[a-f0-9]{32}\Z")
_PREVIEW_ID = re.compile(r"preview_[a-f0-9]{32}\Z")
_SLOTS = threading.BoundedSemaphore(2)
_COMMIT_ADMISSION_TIMEOUT_SECONDS = 5.0


class FeatureWorkspaceError(ValueError):
    def __init__(self, message, *, feature_id=None, code=None):
        super().__init__(message)
        self.feature_id = feature_id
        self.code = code


class FeatureNotFound(FeatureWorkspaceError):
    pass


class FeatureConflict(FeatureWorkspaceError):
    pass


def _acquire_commit_slot(cancel_event=None):
    """Give a finished/cancelled preview bounded time to release its worker.

    Preview requests remain fail-fast. Commits wait at most five seconds and
    never create a destination or reserve a version while waiting. The same
    semaphore still limits all running manual kernel operations to two.
    """
    deadline = time.monotonic() + _COMMIT_ADMISSION_TIMEOUT_SECONDS
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise FeatureWorkspaceError("操作已取消，原版本保持不变。", code="user_cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FeatureWorkspaceError("已有两个手工建模任务在执行，请稍后重试。", code="resource_limit")
        if _SLOTS.acquire(timeout=min(0.05, remaining)):
            # Cancellation can arrive exactly as another worker releases its
            # slot. Return that slot before aborting; no kernel may start.
            if cancel_event is not None and cancel_event.is_set():
                _SLOTS.release()
                raise FeatureWorkspaceError("操作已取消，原版本保持不变。", code="user_cancelled")
            return


def compile_manual_plan(plan, suppressed):
    """Resolve suppression without rewriting or silently deleting dependants."""
    canonical = validate_plan(plan)
    known = {item["id"] for item in canonical["features"]}
    if (not isinstance(suppressed, list) or len(suppressed) > 128
            or any(not isinstance(item, str) or item not in known for item in suppressed)
            or len(set(suppressed)) != len(suppressed)):
        raise FeatureWorkspaceError("抑制列表须由当前计划中不重复的特征 ID 组成。")
    active = copy.deepcopy(canonical)
    active["features"] = [feature for feature in active["features"] if feature["id"] not in suppressed]
    dependants = [feature["id"] for feature in active["features"]
                  if feature.get("input") in suppressed or set(feature.get("inputs", [])) & set(suppressed)
                  or feature.get("planeSource", {}).get("sourceFeatureId") in suppressed]
    if dependants:
        raise FeatureWorkspaceError("这些特征仍依赖被抑制项，请先修改引用或一起抑制：" + "、".join(dependants))
    if active["result"] in suppressed:
        raise FeatureWorkspaceError("结果特征已被抑制，请选择另一个未抑制特征作为输出。")
    return validate_plan(active)


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FeatureWorkspaceError(f"{label}须填写 1 至 {limit} 个字符。")
    return value.strip()


class CadFeatureWorkspace:
    def __init__(self, root, *, source_store=None, design_store=None, executor=execute_cad_plan, preview_executor=preview_cad_plan):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "features.sqlite3"
        self.source_store, self.design_store, self.executor = source_store, design_store, executor
        self.preview_executor = preview_executor
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS manual_features (id TEXT, revision INTEGER, owner TEXT, payload TEXT, PRIMARY KEY(id,revision))")
            db.execute("CREATE TABLE IF NOT EXISTS manual_feature_previews (id TEXT PRIMARY KEY, owner TEXT, created REAL, payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS manual_feature_commits (owner TEXT, request_id TEXT, digest TEXT, design_id TEXT, revision INTEGER, PRIMARY KEY(owner,request_id))")

    def _import_assets(self, owner, active, baseline=None):
        from .cad_import_assets import CadImportAssets
        plans = {"features": [*active.get("features", []), *(baseline or {}).get("features", [])]}
        return CadImportAssets(self.root / "imports").resolve(owner, plans)

    def _db(self):
        return sqlite3.connect(self.database, timeout=15)

    def get(self, owner, design_id, revision=None):
        if not isinstance(design_id, str) or not _ID.fullmatch(design_id):
            raise FeatureNotFound("特征设计不存在。")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise FeatureWorkspaceError("版本号无效。")
        with self._db() as db:
            row = (db.execute("SELECT payload FROM manual_features WHERE id=? AND owner=? ORDER BY revision DESC LIMIT 1", (design_id, owner)).fetchone()
                   if revision is None else db.execute("SELECT payload FROM manual_features WHERE id=? AND owner=? AND revision=?", (design_id, owner, revision)).fetchone())
        if row is None:
            raise FeatureNotFound("特征设计不存在。")
        return json.loads(row[0])

    def list(self, owner):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM manual_features f WHERE owner=? AND revision=(SELECT MAX(revision) FROM manual_features WHERE id=f.id) ORDER BY rowid DESC LIMIT 100", (owner,)).fetchall()
        return [{key: json.loads(row[0]).get(key) for key in ("id", "revision", "name", "status", "updatedAt", "sourceRun")} for row in rows]

    def versions(self, owner, design_id):
        self.get(owner, design_id)
        with self._db() as db:
            rows = db.execute("SELECT payload FROM manual_features WHERE id=? AND owner=? ORDER BY revision DESC LIMIT 100", (design_id, owner)).fetchall()
        return [{key: json.loads(row[0]).get(key) for key in ("revision", "status", "updatedAt", "changeNote")} for row in rows]

    def _save(self, owner, record, previous):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            head = db.execute("SELECT MAX(revision) FROM manual_features WHERE id=? AND owner=?", (record["id"], owner)).fetchone()[0] or 0
            if head != previous:
                raise FeatureConflict("设计已经在其他操作中更新，请重新加载后再保存。")
            db.execute("INSERT INTO manual_features VALUES (?,?,?,?)", (record["id"], record["revision"], owner, json.dumps(record, ensure_ascii=False, allow_nan=False)))
        return record

    def _prepare_save(self, owner, data, design_id=None):
        from .cad_history import authorize_bindings
        if not isinstance(data, dict) or len(json.dumps(data, allow_nan=False).encode()) > 300_000:
            raise FeatureWorkspaceError("特征草稿不能超过 300 KB。")
        previous = 0
        source = None
        if design_id:
            old = self.get(owner, design_id)
            if type(data.get("expectedRevision")) is not int or data["expectedRevision"] != old["revision"]:
                raise FeatureConflict("版本已变化，请重新加载后再保存。")
            previous, source = old["revision"], old.get("sourceRun")
        elif data.get("sourceRun"):
            reference = data["sourceRun"]
            if (not isinstance(reference, dict) or type(reference.get("revision")) is not int
                    or reference["revision"] < 1 or self.source_store is None):
                raise FeatureWorkspaceError("来源任务引用无效。")
            try:
                actual = self.source_store.load(reference.get("runId"), reference["revision"])
            except (KeyError, ValueError, TypeError):
                raise FeatureNotFound("来源建模任务不存在。") from None
            if actual.get("owner") != owner:
                raise FeatureNotFound("来源建模任务不存在。")
            source = {"runId": actual["runId"], "revision": actual["revision"], "statusAtFork": actual["status"],
                      "planSha256": hashlib.sha256(json.dumps(actual.get("plan"), sort_keys=True).encode()).hexdigest()}
        canonical = validate_plan(data.get("plan"))
        self._import_assets(owner, canonical)
        authorize_bindings(canonical, old["plan"] if design_id else None)
        suppressed = data.get("suppressed", [])
        compile_manual_plan(canonical, suppressed)
        current_id = design_id or "feature_" + uuid4().hex
        # Imported provenance remains context. These are user-owned manual
        # edits; they cannot attest to original drawing agreement.
        record = {"id": current_id, "revision": previous + 1,
                  "name": _text(data.get("name", canonical.get("name", "手工特征设计")), "设计名称", 180),
                  "plan": canonical, "suppressed": suppressed, "sourceRun": source,
                  "fileId": _text(data.get("fileId") or (old.get("fileId") if design_id else "") or current_id, "文件标识", 160),
                  "changeNote": _text(data.get("changeNote"), "修改说明", 2000),
                  "status": "draft", "drawingAgreement": "not_checked", "productionReady": False,
                  "updatedAt": datetime.now(timezone.utc).isoformat(), "artifacts": {}}
        return record, previous

    def save(self, owner, data, design_id=None):
        record, previous = self._prepare_save(owner, data, design_id)
        return self._save(owner, record, previous)

    @staticmethod
    def _check_result(result):
        if result.get("status") == "succeeded" and result.get("valid") is True:
            return
        errors = result.get("errors", [])
        first = errors[0] if errors else {}
        missing = result.get("missingParameters", [])
        detail = "请补充参数：" + "、".join(missing) if missing else "；".join(item.get("message", "") for item in errors)
        raise FeatureWorkspaceError(detail or "实体未通过内核检查，请修正尺寸与特征依赖。", feature_id=first.get("featureId"), code=first.get("code"))

    def preview(self, owner, data, cancel_event=None):
        from .cad_topology import digest, feature_prefix
        from .cad_history import authorize_bindings
        if not isinstance(data, dict) or len(json.dumps(data, allow_nan=False).encode()) > 300000:
            raise FeatureWorkspaceError("预览草稿不能超过 300 KB。")
        design_id = data.get("designId")
        if design_id:
            current = self.get(owner, design_id)
            if type(data.get("expectedRevision")) is not int or data["expectedRevision"] != current["revision"]:
                raise FeatureConflict("设计版本已变化，请重新加载后预览。")
        canonical = validate_plan(data.get("plan"))
        self._import_assets(owner, canonical)
        authorize_bindings(canonical, current["plan"] if design_id else None)
        suppressed = data.get("suppressed", [])
        # Validate suppression first, then preview only the requested prefix.
        target = data.get("featureId") or canonical["result"]
        original_result = canonical["result"]
        canonical["result"] = target
        active = compile_manual_plan(canonical, suppressed)
        if target != original_result: active.pop("bodyStates", None)
        active = feature_prefix(active, target)
        if not _SLOTS.acquire(blocking=False): raise FeatureWorkspaceError("已有两个手工建模任务在执行，请稍后重试。", code="resource_limit")
        preview_id = "preview_" + uuid4().hex
        destination = self.root / "previews" / preview_id
        retained = False
        try:
            result = self.preview_executor(active, destination, timeout_seconds=45, cancel_event=cancel_event,
                                           persist_topology=True, binding_base_plan=current["plan"] if design_id else None, imported_assets=self._import_assets(owner, active, current["plan"] if design_id else None))
            self._check_result(result)
            if cancel_event is not None and cancel_event.is_set(): raise FeatureWorkspaceError("预览已取消。", code="user_cancelled")
            if design_id and self.get(owner, design_id)["revision"] != data["expectedRevision"]: raise FeatureConflict("预览期间设计已更新，请重新加载。")
            canonical["result"] = original_result
            result.update({"previewId": preview_id, "draftHash": digest(canonical), "scope": "transaction_preview", "expiresAt": time.time() + 1800})
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO manual_feature_previews VALUES (?,?,?,?)", (preview_id, owner, time.time(), json.dumps(result, allow_nan=False)))
                old = db.execute("SELECT id FROM manual_feature_previews WHERE created<? OR (owner=? AND id NOT IN (SELECT id FROM manual_feature_previews WHERE owner=? ORDER BY created DESC LIMIT 8))", (time.time()-1800, owner, owner)).fetchall()
                db.executemany("DELETE FROM manual_feature_previews WHERE id=?", old)
            retained = True
            for (old_id,) in old:
                if _PREVIEW_ID.fullmatch(old_id): shutil.rmtree(self.root / "previews" / old_id, ignore_errors=True)
            return self._public_preview(result)
        finally:
            if not retained: shutil.rmtree(destination, ignore_errors=True)
            _SLOTS.release()

    @staticmethod
    def _public_preview(record):
        result = copy.deepcopy(record)
        result["artifacts"] = {"glb": {"mimeType": "model/gltf-binary", "url": f"/api/cad/features/previews/{record['previewId']}/artifacts/glb"}}
        return result

    def preview_artifact(self, owner, preview_id, format):
        if not isinstance(preview_id, str) or not _PREVIEW_ID.fullmatch(preview_id) or format != "glb": raise FeatureNotFound("预览文件不存在。")
        with self._db() as db:
            row = db.execute("SELECT payload FROM manual_feature_previews WHERE id=? AND owner=? AND created>?", (preview_id, owner, time.time()-1800)).fetchone()
        if not row: raise FeatureNotFound("预览不存在或已过期，请重新预览。")
        path = (self.root / "previews" / preview_id / "preview.glb").resolve()
        if not path.is_relative_to(self.root / "previews") or not path.is_file(): raise FeatureNotFound("预览文件已失效。")
        return path, "model/gltf-binary"

    def commit(self, owner, data, cancel_event=None):
        from .cad_topology import digest
        request_id = data.get("requestId")
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id)):
            raise FeatureWorkspaceError("提交请求编号无效。")
        fingerprint = digest(data)
        if request_id:
            with self._db() as db:
                existing = db.execute("SELECT digest,design_id,revision FROM manual_feature_commits WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if existing:
                if existing[0] != fingerprint: raise FeatureConflict("同一提交请求编号不能用于不同的修改。")
                return self.get(owner, existing[1], existing[2])
        record, previous = self._prepare_save(owner, data, data.get("designId"))
        active = compile_manual_plan(record["plan"], record["suppressed"])
        _acquire_commit_slot(cancel_event)
        destination = self.root / record["id"] / f"commit-{uuid4().hex}"
        published = False
        try:
            result = self.executor(active, destination, timeout_seconds=120, cancel_event=cancel_event,
                                   persist_topology=True, binding_base_plan=self.get(owner, record["id"])["plan"] if previous else None, include_projections=False, imported_assets=self._import_assets(owner, active, self.get(owner, record["id"])["plan"] if previous else None))
            self._check_result(result)
            if result.get("plan"):
                by_id = {feature["id"]: feature for feature in result["plan"]["features"]}
                record["plan"]["features"] = [by_id.get(feature["id"], feature) for feature in record["plan"]["features"]]
                for field in ("sketches", "annotations", "bodyStates"):
                    if field in result["plan"]:
                        updated = {item["id"]: item for item in result["plan"][field]}
                        record["plan"][field] = [copy.deepcopy(updated.get(item["id"], item)) for item in record["plan"].get(field, [])]
            if cancel_event is not None and cancel_event.is_set(): raise FeatureWorkspaceError("操作已取消，原版本保持不变。", code="user_cancelled")
            for key in ("step", "glb", "plan"):
                result["artifacts"][key]["sha256"] = hashlib.sha256(Path(result["artifacts"][key]["path"]).read_bytes()).hexdigest()
            built = {**record, "status": "built", "updatedAt": datetime.now(timezone.utc).isoformat(), "inspection": result.get("inspection"), "resolvedParameters": result.get("resolvedParameters"),
                     "execution": result.get("execution"), "artifacts": {key: result["artifacts"][key] for key in ("step", "glb", "plan")}, "sharedDesign": None}
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if request_id:
                    existing = db.execute("SELECT digest,design_id,revision FROM manual_feature_commits WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
                    if existing:
                        if existing[0] != fingerprint: raise FeatureConflict("提交编号冲突。")
                        return self.get(owner, existing[1], existing[2])
                head = db.execute("SELECT MAX(revision) FROM manual_features WHERE id=? AND owner=?", (record["id"], owner)).fetchone()[0] or 0
                if head != previous: raise FeatureConflict("构建期间设计已被更新，本次修改未发布，请重新加载。")
                if cancel_event is not None and cancel_event.is_set(): raise FeatureWorkspaceError("操作已取消。", code="user_cancelled")
                db.execute("INSERT INTO manual_features VALUES (?,?,?,?)", (built["id"], built["revision"], owner, json.dumps(built, ensure_ascii=False, allow_nan=False)))
                if request_id: db.execute("INSERT INTO manual_feature_commits VALUES (?,?,?,?,?)", (owner, request_id, fingerprint, built["id"], built["revision"]))
            published = True
            return built
        finally:
            if not published: shutil.rmtree(destination, ignore_errors=True)
            _SLOTS.release()

    def build(self, owner, design_id, expected_revision):
        record = self.get(owner, design_id)
        if type(expected_revision) is not int or expected_revision != record["revision"]:
            raise FeatureConflict("设计已更新，请保存当前草稿后再重建。")
        active = compile_manual_plan(record["plan"], record["suppressed"])
        if not _SLOTS.acquire(blocking=False):
            raise FeatureWorkspaceError("已有两个手工建模任务在执行，请稍后重试。")
        try:
            destination = self.root / design_id / f"build-{uuid4().hex}"
            result = self.executor(active, destination, timeout_seconds=120, persist_topology=True, binding_base_plan=record["plan"], include_projections=False, imported_assets=self._import_assets(owner, active, record["plan"]))
            if result.get("status") != "succeeded" or result.get("valid") is not True:
                errors = result.get("errors", [])
                feature_id = next((item["featureId"] for item in errors if item.get("featureId")), None)
                messages = [item.get("message", "") for item in errors]
                missing = result.get("missingParameters", [])
                detail = "请补充参数：" + "、".join(missing) if missing else "；".join(messages)
                location = f"特征 {feature_id}：" if feature_id else ""
                raise FeatureWorkspaceError("实体未通过重建：" + location + (detail or "请检查必需尺寸和特征依赖。"), feature_id=feature_id)
            shared = None
            if self.design_store:
                step = Path(result["artifacts"]["step"]["path"]).read_bytes()
                shared = self.design_store.import_step(owner, record["name"] + ".step", step, record["fileId"], record["name"],
                    source_feature={"id": design_id, "revision": expected_revision + 1})
            for key in ("step", "glb", "plan"):
                artifact = result["artifacts"][key]
                artifact["sha256"] = hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
            if result.get("plan"):
                by_id = {feature["id"]: feature for feature in result["plan"]["features"]}
                record["plan"]["features"] = [by_id.get(feature["id"], feature) for feature in record["plan"]["features"]]
                for field in ("sketches", "annotations", "bodyStates"):
                    if field in result["plan"]:
                        updated = {item["id"]: item for item in result["plan"][field]}
                        record["plan"][field] = [copy.deepcopy(updated.get(item["id"], item)) for item in record["plan"].get(field, [])]
            built = {**record, "revision": expected_revision + 1, "status": "built",
                     "updatedAt": datetime.now(timezone.utc).isoformat(), "inspection": result.get("inspection"),
                     "resolvedParameters": result.get("resolvedParameters"), "execution": result.get("execution"),
                     "artifacts": {key: result["artifacts"][key] for key in ("step", "glb", "plan")},
                     "sharedDesign": shared, "changeNote": "重建手工设计：" + record["changeNote"][:1800]}
            return self._save(owner, built, expected_revision)
        finally:
            _SLOTS.release()

    def artifact(self, owner, design_id, revision, format):
        record = self.get(owner, design_id, revision)
        if record["status"] != "built" or format not in {"step", "glb", "plan"}:
            raise FeatureNotFound("此版本尚无可下载的手工设计实体。")
        artifact = record["artifacts"].get(format)
        if not artifact:
            raise FeatureNotFound("文件不存在。")
        path = Path(artifact["path"]).resolve()
        if not path.is_relative_to(self.root / design_id) or not path.is_file():
            raise FeatureNotFound("文件已失效，请重新构建。")
        return path, artifact["mimeType"]


def public_feature_record(record):
    value = copy.deepcopy(record)
    value["artifacts"] = {key: {"format": key, "mimeType": item["mimeType"],
        "url": f"/api/cad/features/{record['id']}/versions/{record['revision']}/artifacts/{key}"}
        for key, item in record.get("artifacts", {}).items()}
    return value
