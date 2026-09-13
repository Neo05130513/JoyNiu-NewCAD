"""Expiring CAD file capabilities, separate from immutable geometry revisions.

The random signing key is never the legacy downloadToken: that value has
already been disclosed in old URLs. Keys and revocation history live alongside
CAD records, so a complete runs.sqlite3 backup includes access revocations.
HTTP routes must still enforce STEP confirmation and checked filesystem paths.
"""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from typing import Any, Callable, Mapping

from .commercial_ai_boundary import legacy_ai_available


_RUN = re.compile(r"cad_[a-f0-9]{32}\Z")
_RESOURCE = re.compile(r"(?:artifacts/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,95}|sources/(?:0|[1-9][0-9]{0,6})/(?:download|pages/[1-9][0-9]{0,6}))\Z")
_TOKEN = re.compile(r"v1\.([1-9][0-9]{0,11})\.(0|[1-9][0-9]{0,11})\.([A-Za-z0-9_-]{43})\Z")
_TRUE = {"1", "true", "yes"}


class CadFileAccessDenied(ValueError):
    """Safe public error; never include the rejected token or private key."""

    def __init__(self):
        super().__init__("文件链接已过期或已撤销，请登录后重新打开当前模型。")


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class CadFileAccess:
    def __init__(self, store, auth=None, *, billing=None, allow_anonymous: bool = False,
                 ttl_seconds: int | None = None, allow_legacy: bool | None = None,
                 clock: Callable[[], float] = time.time):
        self.database = store.database
        self.auth = auth
        self.billing = billing
        self.allow_anonymous = allow_anonymous is True
        self.clock = clock
        self.allow_legacy = allow_legacy
        value = ttl_seconds if ttl_seconds is not None else os.environ.get("JOYNIU_CAD_FILE_LINK_TTL_SECONDS", "3600")
        try:
            self.ttl_seconds = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("CAD file link lifetime must be an integer between 1 and 86400 seconds") from None
        if isinstance(value, bool) or self.ttl_seconds != float(value) or not 1 <= self.ttl_seconds <= 86400:
            raise ValueError("CAD file link lifetime must be an integer between 1 and 86400 seconds")
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cad_file_access (
                    run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, signing_key TEXT NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0), updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_file_access_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, owner TEXT NOT NULL,
                    actor_id TEXT NOT NULL, action TEXT NOT NULL, epoch INTEGER NOT NULL, at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS cad_file_access_audit_no_update
                BEFORE UPDATE ON cad_file_access_audit BEGIN SELECT RAISE(ABORT, 'file access audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS cad_file_access_audit_no_delete
                BEFORE DELETE ON cad_file_access_audit BEGIN SELECT RAISE(ABORT, 'file access audit is immutable'); END;
            """)

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.database, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @staticmethod
    def _identity(record: Mapping[str, Any]) -> tuple[str, int, str]:
        run_id, revision, owner = record.get("runId"), record.get("revision"), record.get("owner")
        if (not isinstance(run_id, str) or not _RUN.fullmatch(run_id)
                or type(revision) is not int or revision < 1
                or not isinstance(owner, str) or not owner):
            raise CadFileAccessDenied()
        return run_id, revision, owner

    def _noncommercial(self) -> bool:
        return legacy_ai_available(lambda: self.billing)

    def owner_active(self, record: Mapping[str, Any]) -> bool:
        """Check the owning account on every signed request, including old links."""
        try:
            _, _, owner = self._identity(record)
            if owner == "local-anonymous":
                return self.allow_anonymous and self._noncommercial()
            return self.auth is not None and self.auth.get_user(owner).active is True
        except Exception:
            return False

    def _state(self, db, record, *, create=False):
        run_id, revision, owner = self._identity(record)
        stored = db.execute("SELECT owner FROM cad_runs WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
        if stored is None or stored["owner"] != owner:
            raise CadFileAccessDenied()
        if create:
            db.execute("""INSERT OR IGNORE INTO cad_file_access
                (run_id, owner, signing_key, epoch, updated_at) VALUES (?, ?, ?, 0, ?)""",
                       (run_id, owner, secrets.token_urlsafe(32), _utc(int(self.clock()))))
        row = db.execute("SELECT * FROM cad_file_access WHERE run_id=?", (run_id,)).fetchone()
        if row is not None and row["owner"] != owner:
            raise CadFileAccessDenied()
        return row

    @staticmethod
    def _signature(key: str, record, resource: str, expires: int, epoch: int) -> str:
        run_id, revision, owner = CadFileAccess._identity(record)
        payload = json.dumps(["cad-file-v1", "GET", run_id, revision, owner, resource, expires, epoch],
                             ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return base64.urlsafe_b64encode(hmac.new(key.encode("ascii"), payload, hashlib.sha256).digest()).decode("ascii").rstrip("=")

    def _issue(self, record, resource, row, expires):
        if not isinstance(resource, str) or not _RESOURCE.fullmatch(resource):
            raise CadFileAccessDenied()
        epoch = row["epoch"]
        return f"v1.{expires}.{epoch}.{self._signature(row['signing_key'], record, resource, expires, epoch)}"

    def issue(self, record: Mapping[str, Any], resource: str) -> str:
        """Issue one capability; primarily useful for bounded file endpoints."""
        if not self.owner_active(record):
            raise CadFileAccessDenied()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._state(db, record, create=True)
        return self._issue(record, resource, row, int(self.clock()) + self.ttl_seconds)

    def allows(self, record: Mapping[str, Any], resource: str, access: str) -> bool:
        """No credentials are logged. Invalid requests never create key state."""
        if (not isinstance(access, str) or not access or len(access) > 256
                or not isinstance(resource, str) or not _RESOURCE.fullmatch(resource)
                or not self.owner_active(record)):
            return False
        match = _TOKEN.fullmatch(access)
        try:
            with self._db() as db:
                row = self._state(db, record)
            if match:
                expires, epoch = int(match[1]), int(match[2])
                if row is None or expires <= int(self.clock()) or epoch != row["epoch"]:
                    return False
                expected = self._signature(row["signing_key"], record, resource, expires, epoch)
                return hmac.compare_digest(match[3], expected)
            enabled = (self.allow_legacy is True if self.allow_legacy is not None else
                       os.environ.get("JOYNIU_CAD_ALLOW_LEGACY_FILE_TOKENS", "").strip().casefold() in _TRUE)
            # Once revoked, every old geometry revision's permanent token is
            # retired too. Disabling commercial mode must not revive it.
            if not enabled or not self._noncommercial() or (row is not None and row["epoch"] > 0):
                return False
            previous = record.get("downloadToken")
            return isinstance(previous, str) and bool(previous) and hmac.compare_digest(access, previous)
        except (CadFileAccessDenied, ValueError, TypeError, sqlite3.Error):
            return False

    def revoke(self, record: Mapping[str, Any], actor_id: str) -> dict[str, Any]:
        run_id, _, owner = self._identity(record)
        if actor_id != owner or not self.owner_active(record):
            raise CadFileAccessDenied()
        at = _utc(int(self.clock()))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._state(db, record, create=True)
            epoch = row["epoch"] + 1
            db.execute("UPDATE cad_file_access SET signing_key=?, epoch=?, updated_at=? WHERE run_id=?",
                       (secrets.token_urlsafe(32), epoch, at, run_id))
            db.execute("""INSERT INTO cad_file_access_audit (run_id,owner,actor_id,action,epoch,at)
                       VALUES (?,?,?,'file_links.revoked',?,?)""", (run_id, owner, actor_id, epoch, at))
        return {"runId": run_id, "fileLinksEpoch": epoch, "revokedAt": at}

    def protect_result(self, record: Mapping[str, Any], public_result: Mapping[str, Any], prefix: str) -> dict[str, Any]:
        """Replace all public CAD file URLs; never trust an existing URL target.

        Geometry eligibility remains the caller's responsibility: this helper
        only signs artifact entries that the existing export gate included.
        """
        result = copy.deepcopy(public_result)
        for artifact in result.get("artifacts", []):
            artifact.pop("url", None)
            artifact.pop("downloadUrl", None)
        for document in result.get("sourceDocuments", []):
            document.pop("downloadUrl", None)
            for page in document.get("pages", []):
                page.pop("url", None)
        result.update({"fileLinksAvailable": False, "fileLinksExpiresAt": None})
        if not self.owner_active(record):
            return result
        if not isinstance(prefix, str) or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", prefix):
            raise ValueError("CAD file route prefix must be a local absolute path")
        run_id, revision, _ = self._identity(record)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._state(db, record, create=True)
        expires = int(self.clock()) + self.ttl_seconds
        base = f"{prefix}/cad-agent/runs/{run_id}/{revision}"

        def url(resource):
            return f"{base}/{resource}?access={self._issue(record, resource, row, expires)}"

        for artifact in result.get("artifacts", []):
            link = url(f"artifacts/{artifact['id']}")
            artifact.update({"url": link, "downloadUrl": link})
        for document in result.get("sourceDocuments", []):
            match = re.fullmatch(r"source-(0|[1-9][0-9]{0,6})", str(document.get("id", "")))
            if not match or int(match[1]) >= len(record.get("files") or []):
                raise CadFileAccessDenied()
            index = match[1]
            document["downloadUrl"] = url(f"sources/{index}/download")
            for page in document.get("pages", []):
                if type(page.get("page")) is not int or page["page"] < 1:
                    raise CadFileAccessDenied()
                page["url"] = url(f"sources/{index}/pages/{page['page']}")
        result.update({"fileLinksAvailable": True, "fileLinksExpiresAt": _utc(expires), "fileLinksEpoch": row["epoch"]})
        return result
