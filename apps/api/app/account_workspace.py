"""Account-owned ProjectStore snapshots with atomic SQLite revision checks."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_WORKSPACE_BYTES = 20 * 1024 * 1024
MAX_JSON_DEPTH = 128
MAX_REVISION = 2**53 - 1
_CREDENTIAL_KEY = re.compile(
    r"^(?:access_?token|refresh_?token|session_?token|login_?token|auth_?token|"
    r"authorization|password|api_?key)$", re.IGNORECASE
)


class WorkspaceValidationError(ValueError):
    pass


class WorkspaceTooLargeError(WorkspaceValidationError):
    pass


class WorkspaceConflictError(RuntimeError):
    def __init__(self, revision: int):
        super().__init__("工作区已在其他窗口更新，请重新加载后再保存。")
        self.revision = revision


def is_workspace_credential_key(key: str, path: tuple[str, ...] = ()) -> bool:
    # Artifact-scoped download tokens and arbitrary CAD parameters named
    # `token` are data. Only the known session containers treat it as a login.
    return bool(_CREDENTIAL_KEY.fullmatch(key)) or (
        key.casefold() == "token" and (not path or path[-1].casefold() in {"platform", "session", "auth"})
    )


def validate_workspace(snapshot: Any, *, max_bytes: int = MAX_WORKSPACE_BYTES) -> str:
    """Keep the complete data schema, rejecting credentials instead of losing data."""
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
        raise WorkspaceValidationError("snapshot 必须是 schemaVersion 为 1 的 ProjectStore。")
    if not isinstance(snapshot.get("projects"), list):
        raise WorkspaceValidationError("snapshot.projects 必须是数组。")
    for project in snapshot["projects"]:
        if not isinstance(project, dict) or not isinstance(project.get("id"), str) or not project["id"]:
            raise WorkspaceValidationError("每个项目必须包含有效的 id。")
        if not isinstance(project.get("files"), list):
            raise WorkspaceValidationError("每个项目的 files 必须是数组。")
    pending: list[tuple[Any, tuple[str, ...], int]] = [(snapshot, (), 0)]
    while pending:
        value, path, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise WorkspaceValidationError("工作区 JSON 嵌套过深。")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise WorkspaceValidationError("工作区 JSON 字段名必须为字符串。")
                if is_workspace_credential_key(key, path):
                    raise WorkspaceValidationError("工作区不能包含登录凭据。")
                pending.append((child, (*path, key), depth + 1))
        elif isinstance(value, list):
            pending.extend((child, path, depth + 1) for child in value)
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise WorkspaceValidationError("工作区必须仅包含 JSON 数据。")
        elif isinstance(value, float) and not math.isfinite(value):
            raise WorkspaceValidationError("工作区不能包含非有限数字。")
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise WorkspaceValidationError("工作区必须是有效的 UTF-8 JSON。") from exc
    if size > max_bytes:
        raise WorkspaceTooLargeError("工作区超过允许的保存大小。")
    return encoded


class AccountWorkspaceStore:
    """The owner key is supplied by the authenticated adapter, never the body."""

    def __init__(self, database: str | Path = ":memory:", *, max_bytes: int = MAX_WORKSPACE_BYTES):
        self.database = str(database)
        self.max_bytes = max_bytes
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.database, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS account_workspaces (
                owner_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK(revision > 0),
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_workspace_audit (
                owner_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                action TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, revision)
            );
        """)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _owner(owner_id: str) -> str:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise WorkspaceValidationError("已验证的账号 id 为必填项。")
        return owner_id

    def load(self, owner_id: str) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT revision, snapshot_json, updated_at FROM account_workspaces WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if row is None:
            return {"revision": 0, "snapshot": None, "updatedAt": None}
        return {"revision": row["revision"], "snapshot": json.loads(row["snapshot_json"]), "updatedAt": row["updated_at"]}

    def save(self, owner_id: str, snapshot: Any, *, expected_revision: int) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < MAX_REVISION:
            raise WorkspaceValidationError("expectedRevision 必须是非负整数。")
        encoded = validate_workspace(snapshot, max_bytes=self.max_bytes)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT revision FROM account_workspaces WHERE owner_id = ?", (owner_id,),
                ).fetchone()
                revision = row["revision"] if row else 0
                if revision != expected_revision:
                    raise WorkspaceConflictError(revision)
                revision += 1
                now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                self._connection.execute("""
                    INSERT INTO account_workspaces (owner_id, revision, snapshot_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id) DO UPDATE SET
                        revision = excluded.revision, snapshot_json = excluded.snapshot_json, updated_at = excluded.updated_at
                """, (owner_id, revision, encoded, now, now))
                self._connection.execute("""
                    INSERT INTO account_workspace_audit
                        (owner_id, revision, action, snapshot_sha256, size_bytes, created_at)
                    VALUES (?, ?, 'workspace.saved', ?, ?, ?)
                """, (owner_id, revision, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), len(encoded.encode("utf-8")), now))
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {"revision": revision, "snapshot": json.loads(encoded), "updatedAt": now}
