"""SQLite-backed PDM and RBAC domain services.

The module is dependency-free on purpose.  FastAPI is only imported by the
HTTP adapter in :mod:`app.platform_api`, so workers and tests can exercise the
same invariants without starting a web server.

Security properties provided here:

* password hashes use PBKDF2-HMAC-SHA256 and per-user random salts;
* access tokens are short-lived, HMAC-signed and re-check the current user;
* PDM versions are immutable and SHA-256 addressed;
* optimistic revision checks stop stale clients from silently overwriting;
* state-changing operations append an audit record.

This is suitable for a local/single-node deployment.  A production deployment
can preserve the public service API while replacing SQLite and the token
issuer with managed equivalents.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _utc_timestamp() -> str:
    """Return an RFC3339 UTC timestamp without a platform-specific dependency."""

    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PlatformError(RuntimeError):
    """Base class for errors that HTTP adapters can map predictably."""


class ValidationError(PlatformError):
    pass


class NotFoundError(PlatformError):
    pass


class ConflictError(PlatformError):
    pass


class AuthenticationError(PlatformError):
    pass


class AuthorizationError(PlatformError):
    pass


class Permission(str, Enum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    DOCUMENT_READ = "document:read"
    DOCUMENT_WRITE = "document:write"
    DOCUMENT_REVIEW = "document:review"
    DOCUMENT_RELEASE = "document:release"
    VERSION_CREATE = "version:create"
    OCR_READ = "ocr:read"
    OCR_RUN = "ocr:run"
    CAM_PLAN = "cam:plan"
    CAM_SIMULATE = "cam:simulate"
    CAM_APPROVE = "cam:approve"
    CAM_RELEASE = "cam:release"
    NC_DOWNLOAD = "nc:download"
    USER_MANAGE = "user:manage"
    AUDIT_READ = "audit:read"


class Role(str, Enum):
    VIEWER = "viewer"
    DESIGNER = "designer"
    REVIEWER = "reviewer"
    MANUFACTURING = "manufacturing"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.DOCUMENT_READ,
            Permission.OCR_READ,
        }
    ),
    Role.DESIGNER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PROJECT_WRITE,
            Permission.DOCUMENT_READ,
            Permission.DOCUMENT_WRITE,
            Permission.VERSION_CREATE,
            Permission.OCR_READ,
            Permission.OCR_RUN,
            Permission.CAM_PLAN,
            Permission.CAM_SIMULATE,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.DOCUMENT_READ,
            Permission.DOCUMENT_REVIEW,
            Permission.DOCUMENT_RELEASE,
            Permission.OCR_READ,
            Permission.CAM_APPROVE,
        }
    ),
    Role.MANUFACTURING: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.DOCUMENT_READ,
            Permission.OCR_READ,
            Permission.CAM_PLAN,
            Permission.CAM_SIMULATE,
            Permission.CAM_RELEASE,
            Permission.NC_DOWNLOAD,
        }
    ),
    Role.ADMIN: frozenset({"*"}),
}


def permissions_for_roles(roles: Iterable[str | Role]) -> frozenset[str]:
    values: set[str] = set()
    role_iterable = (roles,) if isinstance(roles, (str, Role)) else roles
    for raw_role in role_iterable:
        try:
            role = raw_role if isinstance(raw_role, Role) else Role(raw_role)
        except ValueError as exc:
            raise ValidationError(f"unknown role: {raw_role}") from exc
        values.update(permission.value if isinstance(permission, Enum) else str(permission) for permission in ROLE_PERMISSIONS[role])
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class User:
    id: str
    email: str
    display_name: str
    roles: tuple[str, ...]
    active: bool
    created_at: str
    updated_at: str

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for_roles(self.roles)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["permissions"] = sorted(self.permissions)
        return data


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    token_type: str
    expires_at: int
    user: User

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "user": self.user.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    owner_id: str
    description: str
    status: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    project_id: str
    name: str
    kind: str
    status: str
    created_by: str
    current_version_id: str | None
    current_revision: int
    metadata: dict[str, Any]
    created_at: str
    updated_at: str
    deleted_at: str | None = None

    @property
    def version(self) -> int:
        """Compatibility alias for clients that call the revision ``version``."""

        return self.current_revision

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: str
    document_id: str
    revision: int
    label: str
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    created_by: str
    created_at: str
    note: str
    metadata: dict[str, Any]

    @property
    def version(self) -> str:
        return self.label

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    details: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _SQLiteComponent:
    """Small shared connection helper with explicit transactions and locking."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        database_value = str(database)
        if database_value != ":memory:":
            Path(database_value).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.database = database_value
        self._connection = sqlite3.connect(database_value, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if database_value != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class AuthService(_SQLiteComponent):
    """Local account store, role assignment and signed access-token issuer."""

    _PBKDF2_ITERATIONS = 240_000

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        token_secret: str | bytes | None = None,
        token_ttl_seconds: int = 3600,
        clock: Any = time.time,
    ) -> None:
        super().__init__(database)
        configured_secret = token_secret or os.getenv("JOYNIU_AUTH_SECRET")
        if configured_secret is None:
            configured_secret = secrets.token_bytes(32)
        self._token_secret = (
            configured_secret.encode("utf-8")
            if isinstance(configured_secret, str)
            else configured_secret
        )
        if len(self._token_secret) < 24:
            raise ValidationError("token_secret must contain at least 24 bytes")
        if token_ttl_seconds < 1:
            raise ValidationError("token_ttl_seconds must be at least 1")
        self.token_ttl_seconds = token_ttl_seconds
        self._clock = clock
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    PRIMARY KEY (user_id, role)
                );
                CREATE TABLE IF NOT EXISTS auth_audit (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _normalise_email(email: str) -> str:
        value = str(email or "").strip().casefold()
        if "@" not in value or value.startswith("@") or value.endswith("@") or len(value) > 254:
            raise ValidationError("a valid email address is required")
        return value

    @classmethod
    def _hash_password(cls, password: str) -> str:
        password = str(password or "")
        if len(password) < 8:
            raise ValidationError("password must contain at least 8 characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, cls._PBKDF2_ITERATIONS
        )
        return "$".join(
            (
                "pbkdf2_sha256",
                str(cls._PBKDF2_ITERATIONS),
                _b64url_encode(salt),
                _b64url_encode(digest),
            )
        )

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            password = str(password or "")
            algorithm, iterations, salt, expected = encoded.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                _b64url_decode(salt),
                int(iterations),
            )
            return hmac.compare_digest(digest, _b64url_decode(expected))
        except (TypeError, ValueError):
            return False

    def count_users(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(
        self,
        email: str,
        password: str,
        display_name: str,
        *,
        roles: Iterable[str | Role] = (Role.VIEWER,),
        user_id: str | None = None,
        actor_id: str = "system",
    ) -> User:
        normalised_email = self._normalise_email(email)
        clean_name = str(display_name or "").strip()
        if not clean_name:
            raise ValidationError("display_name is required")
        role_iterable = (roles,) if isinstance(roles, (str, Role)) else roles
        try:
            role_values = tuple(sorted({Role(role).value for role in role_iterable}))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"unknown role: {roles}") from exc
        if not role_values:
            raise ValidationError("at least one role is required")
        created_at = _utc_timestamp()
        resolved_id = user_id or _new_id("usr")
        encoded_password = self._hash_password(password)
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO users
                    (id, email, display_name, password_hash, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        resolved_id,
                        normalised_email,
                        clean_name,
                        encoded_password,
                        created_at,
                        created_at,
                    ),
                )
                self._connection.executemany(
                    "INSERT INTO user_roles (user_id, role) VALUES (?, ?)",
                    ((resolved_id, role) for role in role_values),
                )
                self._record_audit(
                    actor_id,
                    "user.created",
                    resolved_id,
                    {"email": normalised_email, "roles": list(role_values)},
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"user already exists: {normalised_email}") from exc
        return self.get_user(resolved_id)

    # Compatibility alias used by callers that model this as registration.
    register_user = create_user

    def _row_to_user(self, row: sqlite3.Row) -> User:
        roles = tuple(
            value[0]
            for value in self._connection.execute(
                "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (row["id"],)
            ).fetchall()
        )
        return User(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            roles=roles,
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_user(self, user_id: str) -> User:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"user not found: {user_id}")
            return self._row_to_user(row)

    def get_user_by_email(self, email: str) -> User:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
                (self._normalise_email(email),),
            ).fetchone()
            if row is None:
                raise NotFoundError("user not found")
            return self._row_to_user(row)

    def list_users(self, *, include_inactive: bool = False) -> list[User]:
        query = "SELECT * FROM users"
        if not include_inactive:
            query += " WHERE active = 1"
        query += " ORDER BY created_at, id"
        with self._lock:
            return [self._row_to_user(row) for row in self._connection.execute(query)]

    def assign_roles(
        self,
        user_id: str,
        roles: Iterable[str | Role],
        *,
        actor_id: str,
    ) -> User:
        role_iterable = (roles,) if isinstance(roles, (str, Role)) else roles
        try:
            role_values = tuple(sorted({Role(role).value for role in role_iterable}))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"unknown role: {roles}") from exc
        if not role_values:
            raise ValidationError("at least one role is required")
        self.get_user(user_id)
        updated_at = _utc_timestamp()
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
            self._connection.executemany(
                "INSERT INTO user_roles (user_id, role) VALUES (?, ?)",
                ((user_id, role) for role in role_values),
            )
            self._connection.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?", (updated_at, user_id)
            )
            self._record_audit(
                actor_id, "user.roles_changed", user_id, {"roles": list(role_values)}
            )
        return self.get_user(user_id)

    def set_active(self, user_id: str, active: bool, *, actor_id: str) -> User:
        self.get_user(user_id)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE users SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), _utc_timestamp(), user_id),
            )
            self._record_audit(
                actor_id, "user.activated" if active else "user.deactivated", user_id, {}
            )
        return self.get_user(user_id)

    def authenticate(self, email: str, password: str) -> AccessToken:
        normalised_email = self._normalise_email(email)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (normalised_email,)
            ).fetchone()
            # Run a real hash even for unknown accounts to reduce timing differences.
            stored = row["password_hash"] if row is not None else self._hash_password("invalid-password")
            valid = self._verify_password(password, stored)
            if row is None or not valid or not bool(row["active"]):
                raise AuthenticationError("invalid email or password")
            user = self._row_to_user(row)
        return self.issue_token(user)

    login = authenticate

    def issue_token(self, user: User | str) -> AccessToken:
        resolved = self.get_user(user) if isinstance(user, str) else self.get_user(user.id)
        if not resolved.active:
            raise AuthenticationError("user account is inactive")
        issued_at = int(self._clock())
        expires_at = issued_at + self.token_ttl_seconds
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": resolved.id,
            "email": resolved.email,
            "roles": list(resolved.roles),
            "iat": issued_at,
            "exp": expires_at,
            "jti": uuid.uuid4().hex,
        }
        encoded_header = _b64url_encode(_canonical_json(header).encode("utf-8"))
        encoded_payload = _b64url_encode(_canonical_json(payload).encode("utf-8"))
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
        signature = hmac.new(self._token_secret, signing_input, hashlib.sha256).digest()
        return AccessToken(
            token=f"{signing_input.decode('ascii')}.{_b64url_encode(signature)}",
            token_type="bearer",
            expires_at=expires_at,
            user=resolved,
        )

    def verify_token(self, token: str) -> User:
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
            expected = hmac.new(self._token_secret, signing_input, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64url_decode(encoded_signature)):
                raise AuthenticationError("invalid access token")
            header = json.loads(_b64url_decode(encoded_header))
            payload = json.loads(_b64url_decode(encoded_payload))
            if header != {"alg": "HS256", "typ": "JWT"}:
                raise AuthenticationError("unsupported access token")
            if int(payload["exp"]) <= int(self._clock()):
                raise AuthenticationError("access token has expired")
            user = self.get_user(str(payload["sub"]))
            if not user.active:
                raise AuthenticationError("user account is inactive")
            return user
        except AuthenticationError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, NotFoundError) as exc:
            raise AuthenticationError("invalid access token") from exc

    def has_permission(self, user: User | str, permission: str | Permission) -> bool:
        resolved = self.get_user(user) if isinstance(user, str) else self.get_user(user.id)
        permissions = resolved.permissions
        value = permission.value if isinstance(permission, Permission) else str(permission)
        return "*" in permissions or value in permissions

    def require(self, user: User | str, *permissions: str | Permission) -> User:
        resolved = self.get_user(user) if isinstance(user, str) else self.get_user(user.id)
        missing = [
            value.value if isinstance(value, Permission) else str(value)
            for value in permissions
            if not self.has_permission(resolved, value)
        ]
        if missing:
            raise AuthorizationError(f"missing permissions: {', '.join(missing)}")
        return resolved

    def _record_audit(
        self, actor_id: str, action: str, target_id: str, details: Mapping[str, Any]
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO auth_audit (id, actor_id, action, target_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("evt"),
                actor_id,
                action,
                target_id,
                _canonical_json(details),
                _utc_timestamp(),
            ),
        )

    def list_audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM auth_audit ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "target_id": row["target_id"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # Common naming aliases for API/workflow integrations.
    create_account = create_user
    check_permission = has_permission
    get_current_user = verify_token


class PDMRepository(_SQLiteComponent):
    """Versioned project/document repository with immutable content blobs."""

    VALID_DOCUMENT_KINDS = frozenset(
        {"part", "assembly", "drawing", "model", "cam", "nc", "report", "other"}
    )
    VALID_STATUSES = frozenset({"draft", "in_review", "released", "obsolete", "archived"})
    STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
        "draft": frozenset({"in_review", "archived"}),
        "in_review": frozenset({"draft", "released", "archived"}),
        "released": frozenset({"obsolete", "archived"}),
        "obsolete": frozenset({"archived"}),
        "archived": frozenset({"draft"}),
    }

    def __init__(self, database: str | Path = ":memory:") -> None:
        super().__init__(database)
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pdm_projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pdm_documents (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES pdm_projects(id),
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    current_version_id TEXT,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    UNIQUE(project_id, name)
                );
                CREATE TABLE IF NOT EXISTS pdm_versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES pdm_documents(id),
                    revision INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content BLOB NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    note TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(document_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_pdm_documents_project
                    ON pdm_documents(project_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_pdm_versions_document
                    ON pdm_versions(document_id, revision);
                CREATE TABLE IF NOT EXISTS pdm_audit (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _clean_name(name: str, field_name: str = "name") -> str:
        value = str(name or "").strip()
        if not value:
            raise ValidationError(f"{field_name} is required")
        if len(value) > 255:
            raise ValidationError(f"{field_name} must not exceed 255 characters")
        return value

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            owner_id=row["owner_id"],
            description=row["description"],
            status=row["status"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> Document:
        return Document(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            kind=row["kind"],
            status=row["status"],
            created_by=row["created_by"],
            current_version_id=row["current_version_id"],
            current_revision=int(row["current_revision"]),
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> DocumentVersion:
        return DocumentVersion(
            id=row["id"],
            document_id=row["document_id"],
            revision=int(row["revision"]),
            label=row["label"],
            file_name=row["file_name"],
            content_type=row["content_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            note=row["note"],
            metadata=json.loads(row["metadata_json"]),
        )

    def create_project(
        self,
        name: str,
        owner_id: str,
        *,
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
        project_id: str | None = None,
    ) -> Project:
        clean_name = self._clean_name(name)
        clean_owner = self._clean_name(owner_id, "owner_id")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValidationError("project metadata must be an object")
        resolved_id = project_id or _new_id("prj")
        created_at = _utc_timestamp()
        try:
            metadata_json = _canonical_json(metadata or {})
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO pdm_projects
                    (id, name, owner_id, description, status, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        resolved_id,
                        clean_name,
                        clean_owner,
                        str(description or "").strip(),
                        metadata_json,
                        created_at,
                        created_at,
                    ),
                )
                self._record_audit(
                    clean_owner, "project.created", "project", resolved_id, {"name": clean_name}
                )
        except TypeError as exc:
            raise ValidationError("project metadata must be JSON serializable") from exc
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"project already exists: {clean_name}") from exc
        return self.get_project(resolved_id)

    def get_project(self, project_id: str) -> Project:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM pdm_projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"project not found: {project_id}")
        return self._project_from_row(row)

    def list_projects(
        self, *, owner_id: str | None = None, include_archived: bool = False
    ) -> list[Project]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if owner_id is not None:
            clauses.append("owner_id = ?")
            arguments.append(owner_id)
        if not include_archived:
            clauses.append("status != 'archived'")
        query = "SELECT * FROM pdm_projects"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id"
        with self._lock:
            rows = self._connection.execute(query, arguments).fetchall()
        return [self._project_from_row(row) for row in rows]

    def archive_project(self, project_id: str, *, actor_id: str) -> Project:
        self.get_project(project_id)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE pdm_projects SET status = 'archived', updated_at = ? WHERE id = ?",
                (_utc_timestamp(), project_id),
            )
            self._record_audit(actor_id, "project.archived", "project", project_id, {})
        return self.get_project(project_id)

    def rename_project(
        self,
        project_id: str,
        name: str,
        *,
        actor_id: str,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Project:
        """Update project display metadata without touching document versions."""

        self.get_project(project_id)
        clean_name = self._clean_name(name)
        metadata_json: str | None = None
        if metadata is not None:
            try:
                metadata_json = _canonical_json(metadata)
            except TypeError as exc:
                raise ValidationError("project metadata must be JSON serializable") from exc
        with self._lock, self._connection:
            if description is None and metadata_json is None:
                cursor = self._connection.execute(
                    "UPDATE pdm_projects SET name = ?, updated_at = ? WHERE id = ?",
                    (clean_name, _utc_timestamp(), project_id),
                )
            elif metadata_json is None:
                cursor = self._connection.execute(
                    "UPDATE pdm_projects SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                    (clean_name, str(description or "").strip(), _utc_timestamp(), project_id),
                )
            elif description is None:
                cursor = self._connection.execute(
                    "UPDATE pdm_projects SET name = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                    (clean_name, metadata_json, _utc_timestamp(), project_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE pdm_projects SET name = ?, description = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                    (clean_name, str(description or "").strip(), metadata_json, _utc_timestamp(), project_id),
                )
            if cursor.rowcount != 1:
                raise NotFoundError(f"project not found: {project_id}")
            self._record_audit(
                actor_id,
                "project.updated",
                "project",
                project_id,
                {
                    "name": clean_name,
                    **({"metadata_updated": True} if metadata_json is not None else {}),
                },
            )
        return self.get_project(project_id)

    def update_project_metadata(
        self,
        project_id: str,
        metadata: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> Project:
        """Replace project metadata atomically and append an audit event.

        Project membership is intentionally represented in the metadata JSON so
        existing databases remain backwards compatible.  The HTTP adapter
        validates the ``members`` shape and enforces owner/admin authorization;
        this repository method remains useful to workers that already perform
        their own authorization.
        """

        if not isinstance(metadata, Mapping):
            raise ValidationError("project metadata must be an object")
        project = self.get_project(project_id)
        try:
            metadata_json = _canonical_json(metadata)
        except TypeError as exc:
            raise ValidationError("project metadata must be JSON serializable") from exc
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE pdm_projects SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (metadata_json, _utc_timestamp(), project_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"project not found: {project_id}")
            self._record_audit(
                actor_id,
                "project.metadata_updated",
                "project",
                project_id,
                {
                    "previous_keys": sorted((str(key) for key in project.metadata), key=str),
                    "keys": sorted((str(key) for key in metadata), key=str),
                },
            )
        return self.get_project(project_id)

    def create_document(
        self,
        project_id: str,
        name: str,
        kind: str,
        created_by: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        document_id: str | None = None,
    ) -> Document:
        project = self.get_project(project_id)
        if project.status == "archived":
            raise ConflictError("cannot add documents to an archived project")
        clean_name = self._clean_name(name)
        clean_kind = str(kind or "").strip().casefold()
        if clean_kind not in self.VALID_DOCUMENT_KINDS:
            raise ValidationError(
                f"kind must be one of: {', '.join(sorted(self.VALID_DOCUMENT_KINDS))}"
            )
        clean_actor = self._clean_name(created_by, "created_by")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValidationError("document metadata must be an object")
        resolved_id = document_id or _new_id("doc")
        created_at = _utc_timestamp()
        try:
            metadata_json = _canonical_json(metadata or {})
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO pdm_documents
                    (id, project_id, name, kind, status, created_by, current_revision,
                     metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'draft', ?, 0, ?, ?, ?)
                    """,
                    (
                        resolved_id,
                        project_id,
                        clean_name,
                        clean_kind,
                        clean_actor,
                        metadata_json,
                        created_at,
                        created_at,
                    ),
                )
                self._record_audit(
                    clean_actor,
                    "document.created",
                    "document",
                    resolved_id,
                    {"project_id": project_id, "kind": clean_kind, "name": clean_name},
                )
        except TypeError as exc:
            raise ValidationError("document metadata must be JSON serializable") from exc
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"document name already exists in project: {clean_name}") from exc
        return self.get_document(resolved_id)

    def get_document(self, document_id: str, *, include_deleted: bool = False) -> Document:
        query = "SELECT * FROM pdm_documents WHERE id = ?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with self._lock:
            row = self._connection.execute(query, (document_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"document not found: {document_id}")
        return self._document_from_row(row)

    def list_documents(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        include_deleted: bool = False,
    ) -> list[Document]:
        self.get_project(project_id)
        clauses = ["project_id = ?"]
        arguments: list[Any] = [project_id]
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if kind is not None:
            clauses.append("kind = ?")
            arguments.append(kind)
        query = (
            "SELECT * FROM pdm_documents WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, id"
        )
        with self._lock:
            rows = self._connection.execute(query, arguments).fetchall()
        return [self._document_from_row(row) for row in rows]

    def rename_document(
        self,
        document_id: str,
        name: str,
        *,
        actor_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Document:
        """Rename a file while retaining all immutable versions."""

        document = self.get_document(document_id)
        clean_name = self._clean_name(name)
        metadata_json = (
            _canonical_json(metadata)
            if metadata is not None
            else None
        )
        try:
            with self._lock, self._connection:
                if metadata_json is None:
                    cursor = self._connection.execute(
                        "UPDATE pdm_documents SET name = ?, updated_at = ? WHERE id = ?",
                        (clean_name, _utc_timestamp(), document_id),
                    )
                else:
                    cursor = self._connection.execute(
                        "UPDATE pdm_documents SET name = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                        (clean_name, metadata_json, _utc_timestamp(), document_id),
                    )
                if cursor.rowcount != 1:
                    raise NotFoundError(f"document not found: {document_id}")
                self._record_audit(
                    actor_id,
                    "document.updated",
                    "document",
                    document_id,
                    {"name": clean_name, "previous_name": document.name},
                )
        except TypeError as exc:
            raise ValidationError("document metadata must be JSON serializable") from exc
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"document name already exists in project: {clean_name}") from exc
        return self.get_document(document_id)

    def get_document_manifest(self, document_id: str) -> dict[str, Any]:
        """Return a portable PDM manifest (metadata only, no blob duplication)."""

        document = self.get_document(document_id, include_deleted=True)
        return {
            "document": document.to_dict(),
            "versions": [version.to_dict() for version in self.list_versions(document_id)],
        }

    def get_project_manifest(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        documents = [
            self.get_document_manifest(document.id)
            for document in self.list_documents(project_id, include_deleted=True)
        ]
        return {"project": project.to_dict(), "documents": documents}

    @staticmethod
    def _content_bytes(content: bytes | bytearray | memoryview | str | Mapping[str, Any]) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, (bytearray, memoryview)):
            return bytes(content)
        if isinstance(content, str):
            return content.encode("utf-8")
        if isinstance(content, Mapping):
            try:
                return _canonical_json(content).encode("utf-8")
            except TypeError as exc:
                raise ValidationError("JSON content must be serializable") from exc
        raise ValidationError("content must be bytes, text, or a JSON object")

    def create_version(
        self,
        document_id: str,
        content: bytes | bytearray | memoryview | str | Mapping[str, Any],
        created_by: str,
        *,
        file_name: str | None = None,
        content_type: str = "application/octet-stream",
        note: str = "",
        metadata: Mapping[str, Any] | None = None,
        expected_current_revision: int | None = None,
        label: str | None = None,
    ) -> DocumentVersion:
        document = self.get_document(document_id)
        clean_actor = self._clean_name(created_by, "created_by")
        payload = self._content_bytes(content)
        if not payload:
            raise ValidationError("version content must not be empty")
        if len(payload) > 100 * 1024 * 1024:
            raise ValidationError("version content exceeds the local 100 MiB limit")
        if expected_current_revision is not None and expected_current_revision != document.current_revision:
            raise ConflictError(
                f"stale document revision: expected {expected_current_revision}, "
                f"current {document.current_revision}"
            )
        next_revision = document.current_revision + 1
        clean_label = str(label or f"v{next_revision}").strip()
        clean_file_name = self._clean_name(file_name or document.name, "file_name")
        clean_content_type = str(content_type or "").strip() or "application/octet-stream"
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValidationError("version metadata must be an object")
        version_id = _new_id("ver")
        created_at = _utc_timestamp()
        checksum = hashlib.sha256(payload).hexdigest()
        # The lock and current_revision predicate make the revision increment atomic.
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE pdm_documents
                SET current_revision = ?, current_version_id = ?, updated_at = ?
                WHERE id = ? AND current_revision = ? AND deleted_at IS NULL
                """,
                (next_revision, version_id, created_at, document_id, document.current_revision),
            )
            if cursor.rowcount != 1:
                raise ConflictError("document changed while the version was being created")
            self._connection.execute(
                """
                INSERT INTO pdm_versions
                (id, document_id, revision, label, file_name, content_type, content,
                 size_bytes, sha256, created_by, created_at, note, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    document_id,
                    next_revision,
                    clean_label,
                    clean_file_name,
                    clean_content_type,
                    payload,
                    len(payload),
                    checksum,
                    clean_actor,
                    created_at,
                    str(note or "").strip(),
                    _canonical_json(metadata or {}),
                ),
            )
            self._record_audit(
                clean_actor,
                "version.created",
                "version",
                version_id,
                {
                    "document_id": document_id,
                    "revision": next_revision,
                    "sha256": checksum,
                    "size_bytes": len(payload),
                },
            )
        return self.get_version(version_id)

    def get_version(self, version_id: str) -> DocumentVersion:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM pdm_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"version not found: {version_id}")
        return self._version_from_row(row)

    def get_version_content(self, version_id: str, *, verify_checksum: bool = True) -> bytes:
        with self._lock:
            row = self._connection.execute(
                "SELECT content, sha256 FROM pdm_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"version not found: {version_id}")
        content = bytes(row["content"])
        if verify_checksum and not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), row["sha256"]
        ):
            raise ConflictError(f"version checksum mismatch: {version_id}")
        return content

    def list_versions(self, document_id: str) -> list[DocumentVersion]:
        self.get_document(document_id, include_deleted=True)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM pdm_versions WHERE document_id = ? ORDER BY revision DESC",
                (document_id,),
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def change_document_status(
        self,
        document_id: str,
        status: str,
        *,
        actor_id: str,
        expected_current_revision: int | None = None,
    ) -> Document:
        document = self.get_document(document_id)
        target = status.strip().casefold()
        if target not in self.VALID_STATUSES:
            raise ValidationError(f"unknown document status: {status}")
        if target == document.status:
            return document
        if target not in self.STATUS_TRANSITIONS[document.status]:
            raise ConflictError(f"invalid status transition: {document.status} -> {target}")
        if target in {"in_review", "released"} and document.current_revision == 0:
            raise ConflictError("a document needs at least one version before review or release")
        if expected_current_revision is not None and expected_current_revision != document.current_revision:
            raise ConflictError("document revision changed before the status update")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE pdm_documents SET status = ?, updated_at = ? WHERE id = ?",
                (target, _utc_timestamp(), document_id),
            )
            self._record_audit(
                actor_id,
                "document.status_changed",
                "document",
                document_id,
                {"from": document.status, "to": target},
            )
        return self.get_document(document_id)

    def soft_delete_document(self, document_id: str, *, actor_id: str) -> Document:
        self.get_document(document_id)
        deleted_at = _utc_timestamp()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE pdm_documents SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (deleted_at, deleted_at, document_id),
            )
            self._record_audit(actor_id, "document.deleted", "document", document_id, {})
        return self.get_document(document_id, include_deleted=True)

    def restore_document(self, document_id: str, *, actor_id: str) -> Document:
        document = self.get_document(document_id, include_deleted=True)
        if document.deleted_at is None:
            return document
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE pdm_documents SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (_utc_timestamp(), document_id),
            )
            self._record_audit(actor_id, "document.restored", "document", document_id, {})
        return self.get_document(document_id)

    def _record_audit(
        self,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: Mapping[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO pdm_audit
            (id, actor_id, action, resource_type, resource_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("evt"),
                actor_id,
                action,
                resource_type,
                resource_id,
                _canonical_json(details),
                _utc_timestamp(),
            ),
        )

    def list_audit_events(
        self,
        *,
        resource_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        if limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        query = "SELECT * FROM pdm_audit"
        arguments: list[Any] = []
        if resource_id is not None:
            query += " WHERE resource_id = ?"
            arguments.append(resource_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        arguments.append(limit)
        with self._lock:
            rows = self._connection.execute(query, arguments).fetchall()
        return [
            AuditEvent(
                id=row["id"],
                actor_id=row["actor_id"],
                action=row["action"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                details=json.loads(row["details_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # File-oriented aliases make the repository convenient for PDM UIs where a
    # drawing/model is called a file.  They intentionally preserve the same
    # immutable version semantics.
    create_file = create_document
    get_file = get_document
    list_files = list_documents
    create_file_version = create_version
    get_file_version = get_version
    list_file_versions = list_versions


# Semantic alias used by dependency-injection code.
PDMService = PDMRepository


__all__ = [
    "AccessToken",
    "AuditEvent",
    "AuthService",
    "AuthenticationError",
    "AuthorizationError",
    "ConflictError",
    "Document",
    "DocumentVersion",
    "NotFoundError",
    "PDMRepository",
    "PDMService",
    "Permission",
    "PlatformError",
    "Project",
    "ROLE_PERMISSIONS",
    "Role",
    "User",
    "ValidationError",
    "permissions_for_roles",
]
