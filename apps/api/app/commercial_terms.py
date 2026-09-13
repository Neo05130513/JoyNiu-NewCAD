"""Operator-authored immutable terms and explicit, account-scoped acceptance.

No legal text or consent is seeded. Publishing each kind changes one global
revision; a purchase/job must reference a receipt for the current exact set.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Mapping
from uuid import uuid4

from .billing import BillingUnavailable
from .platform import AuthorizationError, ConflictError, ValidationError, permissions_for_roles


KINDS = ("service", "privacy", "credits")


class CommercialTermsUnavailable(BillingUnavailable):
    def __init__(self):
        super().__init__("服务条款、隐私说明或积分规则尚未正式发布，暂不能购买或开始付费任务。")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValidationError(f"{name}不能为空且不得超过 {maximum} 个字符")
    return value.strip()


class CommercialTermsService:
    def __init__(self, auth):
        # Persist in the account database. File-backed operations use separate
        # short-lived connections: holding the auth lock while waiting for a
        # billing writer would block that writer's terms/identity reads.
        self.auth = auth
        self.database = auth.database
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS commercial_terms_documents (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('service','privacy','credits')),
                    title TEXT NOT NULL, version TEXT NOT NULL, body TEXT NOT NULL,
                    body_hash TEXT NOT NULL, published_at TEXT NOT NULL, published_by TEXT NOT NULL,
                    UNIQUE(kind,version)
                );
                CREATE TABLE IF NOT EXISTS commercial_terms_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision>=0),
                    active_json TEXT NOT NULL
                );
                INSERT OR IGNORE INTO commercial_terms_state VALUES (1,0,'{}');
                CREATE TABLE IF NOT EXISTS commercial_terms_acceptances (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), revision INTEGER NOT NULL,
                    versions_json TEXT NOT NULL, document_ids_json TEXT NOT NULL, accepted_at TEXT NOT NULL,
                    UNIQUE(owner_id,revision)
                );
                CREATE TABLE IF NOT EXISTS commercial_terms_acceptance_keys (
                    owner_id TEXT NOT NULL REFERENCES users(id), idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, acceptance_id TEXT NOT NULL REFERENCES commercial_terms_acceptances(id),
                    PRIMARY KEY(owner_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS commercial_terms_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    entity_id TEXT NOT NULL, revision INTEGER NOT NULL, details_json TEXT NOT NULL, at TEXT NOT NULL
                );
            """)
            for table in ("commercial_terms_documents", "commercial_terms_acceptances", "commercial_terms_acceptance_keys", "commercial_terms_audit"):
                for operation in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable commercial terms record'); END")

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self):
        if self.database == ":memory:":
            with self.auth._lock:
                yield self.auth._connection
        else:
            with closing(self._connect()) as db:
                yield db

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _owner(self, owner_id, db=None):
        if db is None:
            with self._connection() as connection:
                return self._owner(owner_id, connection)
        user = db.execute("SELECT active FROM users WHERE id=?", (owner_id,)).fetchone()
        if user is None:
            raise AuthorizationError("账号不可用，请重新登录。")
        if not user["active"]:
            raise AuthorizationError("账号已停用。")
        return tuple(row[0] for row in db.execute("SELECT role FROM user_roles WHERE user_id=?", (owner_id,)).fetchall())

    def _admin(self, actor_id, db=None):
        permissions = permissions_for_roles(self._owner(actor_id, db))
        if "*" not in permissions and "terms:manage" not in permissions:
            raise AuthorizationError("当前账号没有条款管理权限。")

    @staticmethod
    def _document(row):
        return {"id": row["id"], "kind": row["kind"], "title": row["title"], "version": row["version"],
                "body": row["body"], "publishedAt": row["published_at"]}

    def _snapshot(self, db):
        state = db.execute("SELECT * FROM commercial_terms_state WHERE id=1").fetchone()
        active = json.loads(state["active_json"])
        documents = []
        for kind in KINDS:
            row = db.execute("SELECT * FROM commercial_terms_documents WHERE id=? AND kind=?", (active.get(kind), kind)).fetchone()
            if row is not None:
                documents.append(self._document(row))
        return {"ready": len(documents) == 3, "revision": state["revision"], "documents": documents}

    def terms(self):
        with self._connection() as db:
            return self._snapshot(db)

    def ready(self) -> bool:
        return self.terms()["ready"]

    def publish(self, actor_id, values):
        self._admin(actor_id)
        if not isinstance(values, Mapping) or set(values) != {"kind", "title", "version", "body", "expectedRevision"}:
            raise ValidationError("须提供条款类型、标题、版本、正文和当前发布状态版本。")
        if not isinstance(values["kind"], str) or values["kind"] not in KINDS:
            raise ValidationError("条款类型必须是 service、privacy 或 credits。")
        if type(values["expectedRevision"]) is not int or not 0 <= values["expectedRevision"] <= 2**53 - 1:
            raise ValidationError("当前发布状态版本必须是非负整数。")
        title = _text(values["title"], "标题", 200)
        version = _text(values["version"], "版本名称", 80)
        body = _text(values["body"], "正式条款正文", 100_000)
        document_id, published_at = "terms_" + uuid4().hex, _now()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._transaction() as db:
            # Activation/role changes from another worker serialize with this
            # write; stale UI permission alone can never authorize a publish.
            self._admin(actor_id, db)
            state = db.execute("SELECT * FROM commercial_terms_state WHERE id=1").fetchone()
            if values["expectedRevision"] != state["revision"]:
                raise ConflictError("条款已被更新，请重新载入后核对再发布。")
            if db.execute("SELECT 1 FROM commercial_terms_documents WHERE kind=? AND version=?", (values["kind"], version)).fetchone():
                raise ConflictError("该类型的版本名称已发布，旧正文不可修改，请使用新的版本名称。")
            active = json.loads(state["active_json"])
            previous_id = active.get(values["kind"])
            active[values["kind"]] = document_id
            db.execute("INSERT INTO commercial_terms_documents VALUES (?,?,?,?,?,?,?,?)",
                       (document_id, values["kind"], title, version, body, body_hash, published_at, actor_id))
            revision = state["revision"] + 1
            changed = db.execute("UPDATE commercial_terms_state SET revision=?, active_json=? WHERE id=1 AND revision=?",
                                 (revision, _json(active), state["revision"]))
            if changed.rowcount != 1:
                raise ConflictError("条款已被更新，请重新载入后核对再发布。")
            db.execute("INSERT INTO commercial_terms_audit (actor_id,action,entity_id,revision,details_json,at) VALUES (?,?,?,?,?,?)",
                       (actor_id, "terms.published", document_id, revision,
                        _json({"kind": values["kind"], "version": version, "bodyHash": body_hash, "replacesId": previous_id}), published_at))
            return self._snapshot(db)

    @staticmethod
    def _receipt(row):
        return {"acceptanceId": row["id"], "ownerId": row["owner_id"], "revision": row["revision"],
                "versions": json.loads(row["versions_json"]), "documentIds": json.loads(row["document_ids_json"]),
                "acceptedAt": row["accepted_at"]}

    def _current_receipt(self, db, owner_id, snapshot):
        if not snapshot["ready"]:
            return None
        row = db.execute("SELECT * FROM commercial_terms_acceptances WHERE owner_id=? AND revision=?",
                         (owner_id, snapshot["revision"])).fetchone()
        if row is None:
            return None
        receipt = self._receipt(row)
        current = {item["kind"]: item["id"] for item in snapshot["documents"]}
        return receipt if receipt["versions"] == current else None

    def acceptance(self, owner_id):
        with self._connection() as db:
            self._owner(owner_id, db)
            snapshot = self._snapshot(db)
            receipt = self._current_receipt(db, owner_id, snapshot)
            return {"ready": snapshot["ready"], "revision": snapshot["revision"], "accepted": receipt is not None, "receipt": receipt}

    def require_acceptance(self, owner_id):
        status = self.acceptance(owner_id)
        if not status["ready"]:
            raise CommercialTermsUnavailable()
        if not status["accepted"]:
            raise ConflictError("请先阅读并明确接受当前服务条款、隐私说明和积分规则。")
        return status["receipt"]

    def accept(self, owner_id, *, document_ids, idempotency_key):
        self._owner(owner_id)
        if (not isinstance(document_ids, list) or len(document_ids) != 3
                or any(not isinstance(item, str) or not re.fullmatch(r"terms_[a-f0-9]{32}", item) for item in document_ids)
                or len(set(document_ids)) != 3):
            raise ValidationError("须明确提交当前三份不同的正式条款文档。")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key):
            raise ValidationError("接受请求须提供 8 至 128 位幂等标识。")
        normalized = sorted(document_ids)
        fingerprint = hashlib.sha256(_json(normalized).encode()).hexdigest()
        with self._transaction() as db:
            self._owner(owner_id, db)
            snapshot = self._snapshot(db)
            if not snapshot["ready"]:
                raise CommercialTermsUnavailable()
            versions = {item["kind"]: item["id"] for item in snapshot["documents"]}
            if sorted(versions.values()) != normalized:
                raise ConflictError("条款已更新或文档不匹配，请重新阅读当前三份正式文本后再接受。")
            prior = db.execute("SELECT * FROM commercial_terms_acceptance_keys WHERE owner_id=? AND idempotency_key=?",
                               (owner_id, idempotency_key)).fetchone()
            if prior is not None and prior["request_hash"] != fingerprint:
                raise ConflictError("该接受请求标识已用于其他版本，请为本次明确接受使用新标识。")
            receipt = self._current_receipt(db, owner_id, snapshot)
            if prior is not None:
                # A replay is valid only for the same owner and still-current
                # set. It never grants consent for texts published afterwards.
                if receipt is None or receipt["acceptanceId"] != prior["acceptance_id"]:
                    raise ConflictError("该接受记录已不属于当前条款，请重新阅读后接受。")
            elif receipt is None:
                acceptance_id, accepted_at = "accept_" + uuid4().hex, _now()
                db.execute("INSERT INTO commercial_terms_acceptances VALUES (?,?,?,?,?,?)",
                           (acceptance_id, owner_id, snapshot["revision"], _json(versions), _json(normalized), accepted_at))
                db.execute("INSERT INTO commercial_terms_audit (actor_id,action,entity_id,revision,details_json,at) VALUES (?,?,?,?,?,?)",
                           (owner_id, "terms.accepted", acceptance_id, snapshot["revision"], _json({"versions": versions}), accepted_at))
                receipt = self._receipt(db.execute("SELECT * FROM commercial_terms_acceptances WHERE id=?", (acceptance_id,)).fetchone())
            if prior is None:
                db.execute("INSERT INTO commercial_terms_acceptance_keys VALUES (?,?,?,?)",
                           (owner_id, idempotency_key, fingerprint, receipt["acceptanceId"]))
            return {"ready": True, "revision": snapshot["revision"], "accepted": True, "receipt": receipt}

    def history(self, actor_id, *, limit=50, offset=0):
        self._admin(actor_id)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValidationError("历史条款分页参数不正确。")
        with self._connection() as db:
            snapshot = self._snapshot(db)
            current = {item["id"] for item in snapshot["documents"]}
            rows = db.execute("SELECT * FROM commercial_terms_documents ORDER BY published_at DESC,id DESC LIMIT ? OFFSET ?",
                                    (limit, offset)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM commercial_terms_documents").fetchone()[0]
            return {"revision": snapshot["revision"], "total": total,
                    "documents": [{**self._document(row), "createdBy": row["published_by"], "active": row["id"] in current} for row in rows]}
