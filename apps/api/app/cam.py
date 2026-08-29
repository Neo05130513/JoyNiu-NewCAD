"""CAM/NC planning, deterministic simulation and release gating.

The service is deliberately kernel-agnostic.  A plan references an immutable
geometry/version hash, operations are validated before simulation, and NC
release is impossible until a *matching* simulation and approval exist.  The
built-in simulator is a deterministic safety pre-check suitable for CI and
workflow demos; it is clearly labelled ``deterministic-precheck`` and must be
replaced/augmented by a machine-specific verifier before production cutting.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .platform import AuthorizationError, Permission, PlatformError, ValidationError


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CAMError(PlatformError):
    """Base CAM domain error mapped by the shared platform HTTP adapter."""

    pass


class CAMNotFoundError(CAMError):
    pass


class CAMConflictError(CAMError):
    pass


class CAMGateRejected(CAMError):
    """Raised when a plan is not safe/reviewed enough for NC release."""

    def __init__(self, decision: "GateDecision") -> None:
        self.decision = decision
        super().__init__("CAM release gate rejected: " + "; ".join(decision.reasons))


class OperationType(str, Enum):
    FACING = "facing"
    POCKET = "pocket"
    PROFILE = "profile"
    DRILL = "drill"
    SLOT = "slot"
    CHAMFER = "chamfer"
    DEBURR = "deburr"


class CAMPlanStatus(str, Enum):
    DRAFT = "draft"
    SIMULATED = "simulated"
    APPROVED = "approved"
    RELEASED = "released"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StockDefinition:
    length: float
    width: float
    height: float
    material: str = "steel"
    origin: str = "center_xy_bottom"

    def validate(self) -> None:
        for name in ("length", "width", "height"):
            value = getattr(self, name)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = float("nan")
            if not math.isfinite(numeric) or numeric <= 0:
                raise ValidationError(f"stock {name} must be a positive finite number")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    id: str
    name: str
    kind: str
    diameter: float
    flute_length: float
    max_rpm: int = 12000
    max_feed: float = 1200.0
    material: str = "carbide"

    def validate(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValidationError("tool id and name are required")
        if self.diameter <= 0 or self.flute_length <= 0:
            raise ValidationError("tool diameter and flute_length must be positive")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fluteLength"] = data.pop("flute_length")
        data["maxRpm"] = data.pop("max_rpm")
        data["maxFeed"] = data.pop("max_feed")
        return data


@dataclass(frozen=True, slots=True)
class CAMOperation:
    id: str
    operation_type: str
    tool_id: str
    depth: float
    feed_rate: float
    spindle_rpm: int
    retract_height: float
    path_length: float
    parameters: dict[str, Any]
    enabled: bool = True
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["operationType"] = data.pop("operation_type")
        data["toolId"] = data.pop("tool_id")
        data["feedRate"] = data.pop("feed_rate")
        data["spindleRpm"] = data.pop("spindle_rpm")
        data["retractHeight"] = data.pop("retract_height")
        data["pathLength"] = data.pop("path_length")
        return data


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    actor_id: str
    role: str
    comment: str
    plan_revision: int
    simulation_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actorId"] = data.pop("actor_id")
        data["planRevision"] = data.pop("plan_revision")
        data["simulationId"] = data.pop("simulation_id")
        data["createdAt"] = data.pop("created_at")
        return data


@dataclass(frozen=True, slots=True)
class CAMPlan:
    id: str
    project_id: str | None
    source_document_id: str | None
    source_version_id: str | None
    geometry_hash: str
    units: str
    machine: str
    stock: StockDefinition
    operations: tuple[CAMOperation, ...]
    status: str
    revision: int
    created_by: str
    created_at: str
    updated_at: str
    approvals: tuple[Approval, ...] = ()
    latest_simulation_id: str | None = None
    released_nc_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "projectId": self.project_id,
            "sourceDocumentId": self.source_document_id,
            "sourceVersionId": self.source_version_id,
            "geometryHash": self.geometry_hash,
            "units": self.units,
            "machine": self.machine,
            "stock": self.stock.to_dict(),
            "operations": [item.to_dict() for item in self.operations],
            "status": self.status,
            "revision": self.revision,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "approvals": [item.to_dict() for item in self.approvals],
            "latestSimulationId": self.latest_simulation_id,
            "releasedNcId": self.released_nc_id,
        }


@dataclass(frozen=True, slots=True)
class SimulationResult:
    id: str
    plan_id: str
    plan_revision: int
    geometry_hash: str
    engine: str
    passed: bool
    collision_count: int
    gouge_count: int
    envelope_violations: int
    unresolved_operations: int
    stock_remaining_volume: float
    max_tool_load: float
    runtime_seconds: float
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    checks: dict[str, bool]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["planId"] = data.pop("plan_id")
        data["planRevision"] = data.pop("plan_revision")
        data["geometryHash"] = data.pop("geometry_hash")
        data["collisionCount"] = data.pop("collision_count")
        data["gougeCount"] = data.pop("gouge_count")
        data["envelopeViolations"] = data.pop("envelope_violations")
        data["unresolvedOperations"] = data.pop("unresolved_operations")
        data["stockRemainingVolume"] = data.pop("stock_remaining_volume")
        data["maxToolLoad"] = data.pop("max_tool_load")
        data["runtimeSeconds"] = data.pop("runtime_seconds")
        data["createdAt"] = data.pop("created_at")
        data["warnings"] = list(data["warnings"])
        data["errors"] = list(data["errors"])
        return data


@dataclass(frozen=True, slots=True)
class GateDecision:
    passed: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    checks: dict[str, bool]
    plan_id: str | None = None
    simulation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "checks": dict(self.checks),
            "planId": self.plan_id,
            "simulationId": self.simulation_id,
        }


@dataclass(frozen=True, slots=True)
class NCProgram:
    id: str
    plan_id: str
    simulation_id: str
    postprocessor: str
    status: str
    text: str
    sha256: str
    created_by: str
    created_at: str
    released_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["planId"] = data.pop("plan_id")
        data["simulationId"] = data.pop("simulation_id")
        data["createdBy"] = data.pop("created_by")
        data["createdAt"] = data.pop("created_at")
        data["releasedAt"] = data.pop("released_at")
        return data


class SimulationEngine(Protocol):
    name: str

    def simulate(self, plan: CAMPlan, tools: Mapping[str, ToolDefinition]) -> SimulationResult:
        ...


class DeterministicSimulationEngine:
    """Fast, reproducible checks for workflow gating and integration tests."""

    name = "deterministic-precheck"

    def simulate(self, plan: CAMPlan, tools: Mapping[str, ToolDefinition]) -> SimulationResult:
        errors: list[str] = []
        warnings: list[str] = [
            "Deterministic pre-check only; run a machine-specific material-removal simulation before cutting."
        ]
        checks: dict[str, bool] = {}
        collision_count = gouge_count = envelope_violations = unresolved = 0
        plan.stock.validate()
        enabled_operations = [operation for operation in plan.operations if operation.enabled]
        checks["has_operations"] = bool(enabled_operations)
        if not enabled_operations:
            errors.append("CAM plan contains no enabled operations")
        checks["geometry_hash_present"] = bool(plan.geometry_hash.strip())
        if not checks["geometry_hash_present"]:
            errors.append("CAM plan is not linked to a geometry hash")
        checks["machine_declared"] = bool(plan.machine.strip())
        if not checks["machine_declared"]:
            errors.append("machine is required")
        total_path = 0.0
        max_load = 0.0
        for operation in enabled_operations:
            tool = tools.get(operation.tool_id)
            operation_label = f"operation {operation.id}"
            if tool is None:
                unresolved += 1
                errors.append(f"{operation_label}: tool '{operation.tool_id}' is not registered")
                continue
            try:
                tool.validate()
            except ValidationError as exc:
                unresolved += 1
                errors.append(f"{operation_label}: invalid tool ({exc})")
            if operation.operation_type not in {item.value for item in OperationType}:
                unresolved += 1
                errors.append(f"{operation_label}: unsupported operation type '{operation.operation_type}'")
            if not math.isfinite(operation.depth) or operation.depth <= 0:
                unresolved += 1
                errors.append(f"{operation_label}: depth must be positive")
            if operation.depth > plan.stock.height + 1e-9:
                envelope_violations += 1
                errors.append(f"{operation_label}: depth exceeds stock height")
            if operation.feed_rate <= 0 or operation.spindle_rpm <= 0:
                unresolved += 1
                errors.append(f"{operation_label}: feed and spindle speed must be positive")
            if operation.retract_height < 0:
                envelope_violations += 1
                errors.append(f"{operation_label}: retract height cannot be negative")
            if operation.path_length < 0:
                unresolved += 1
                errors.append(f"{operation_label}: path length cannot be negative")
            total_path += max(0.0, operation.path_length)
            if tool is not None:
                load = min(100.0, (operation.feed_rate / max(tool.max_feed, 1.0)) * 50.0)
                max_load = max(max_load, load)
                if tool.diameter > min(plan.stock.length, plan.stock.width) * 2:
                    envelope_violations += 1
                    errors.append(f"{operation_label}: tool diameter exceeds stock envelope")
            # Explicit flags let a real-kernel adapter and tests feed collision
            # evidence into the same release gate.
            if bool(operation.parameters.get("collision")):
                collision_count += 1
                errors.append(f"{operation_label}: collision reported by simulator")
            if bool(operation.parameters.get("gouge")):
                gouge_count += 1
                errors.append(f"{operation_label}: gouge reported by simulator")
            if operation.parameters.get("safe_retract") is False:
                envelope_violations += 1
                errors.append(f"{operation_label}: safe retract clearance failed")
            if operation.parameters.get("requires_review"):
                unresolved += 1
                warnings.append(f"{operation_label}: marked for manual review")
        checks["no_collisions"] = collision_count == 0
        checks["no_gouges"] = gouge_count == 0
        checks["within_envelope"] = envelope_violations == 0
        checks["operations_resolved"] = unresolved == 0
        checks["tool_load_under_limit"] = max_load <= 100.0
        if total_path == 0 and enabled_operations:
            warnings.append("All operations have zero path length; verify toolpath geometry.")
        # The volume is a conservative bookkeeping estimate, not a solid-kernel
        # result.  Keep it deterministic and disclose that in the engine name.
        stock_volume = plan.stock.length * plan.stock.width * plan.stock.height
        removed_estimate = min(stock_volume * 0.99, total_path * max(0.1, plan.stock.height * 0.01))
        remaining_volume = max(0.0, stock_volume - removed_estimate)
        passed = not errors and all(checks.values())
        return SimulationResult(
            id=_id("sim"),
            plan_id=plan.id,
            plan_revision=plan.revision,
            geometry_hash=plan.geometry_hash,
            engine=self.name,
            passed=passed,
            collision_count=collision_count,
            gouge_count=gouge_count,
            envelope_violations=envelope_violations,
            unresolved_operations=unresolved,
            stock_remaining_volume=remaining_volume,
            max_tool_load=round(max_load, 4),
            runtime_seconds=0.001,
            warnings=tuple(warnings),
            errors=tuple(errors),
            checks=checks,
            created_at=_now(),
        )


class CAMReleaseGate:
    """Pure policy object, easy to audit or replace with organisation policy."""

    def __init__(self, *, require_approval: bool = True, require_separate_reviewer: bool = True) -> None:
        self.require_approval = require_approval
        self.require_separate_reviewer = require_separate_reviewer

    def evaluate(
        self,
        plan: CAMPlan,
        simulation: SimulationResult | None,
        *,
        actor_id: str | None = None,
        actor_permissions: Iterable[str] = (),
    ) -> GateDecision:
        permissions = {
            item.value if isinstance(item, Permission) else str(item) for item in actor_permissions
        }
        reasons: list[str] = []
        warnings: list[str] = []
        checks: dict[str, bool] = {}
        checks["simulation_present"] = simulation is not None
        if simulation is None:
            reasons.append("a simulation result is required")
        else:
            checks["simulation_passed"] = simulation.passed
            if not simulation.passed:
                reasons.append("simulation did not pass")
            checks["simulation_matches_plan"] = (
                simulation.plan_id == plan.id
                and simulation.plan_revision == plan.revision
                and simulation.geometry_hash == plan.geometry_hash
            )
            if not checks["simulation_matches_plan"]:
                reasons.append("simulation is stale or references a different geometry")
            if simulation.engine == "deterministic-precheck":
                warnings.append("deterministic pre-check is not a production material-removal proof")
        checks["no_unresolved_operations"] = bool(
            simulation is not None and simulation.unresolved_operations == 0
        )
        if not checks["no_unresolved_operations"]:
            reasons.append("one or more operations are unresolved")
        checks["release_permission"] = "*" in permissions or Permission.CAM_RELEASE.value in permissions
        if actor_permissions and not checks["release_permission"]:
            reasons.append("actor lacks cam:release permission")
        if self.require_approval:
            valid_approvals = [
                approval
                for approval in plan.approvals
                if simulation is not None
                and approval.simulation_id == simulation.id
                and approval.plan_revision == plan.revision
            ]
            checks["approval_present"] = bool(valid_approvals)
            if not valid_approvals:
                reasons.append("a reviewer approval for this simulation is required")
            if self.require_separate_reviewer and actor_id:
                checks["separate_reviewer"] = any(item.actor_id != actor_id for item in valid_approvals)
                if not checks["separate_reviewer"]:
                    reasons.append("release actor must differ from the reviewer")
        else:
            checks["approval_present"] = True
            checks["separate_reviewer"] = True
        # A missing permissions iterable means policy is being evaluated offline;
        # in that mode don't reject solely for an omitted actor context.
        passed = not reasons
        return GateDecision(
            passed=passed,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            checks=checks,
            plan_id=plan.id,
            simulation_id=simulation.id if simulation else None,
        )


class CAMService:
    """Thread-safe CAM plan store with optional SQLite snapshot persistence.

    The domain objects remain in memory for fast simulation, while a single
    versioned JSON snapshot can be persisted transactionally in SQLite.  This
    keeps the service independent from a particular PDM schema and, unlike a
    best-effort event log, restores plans, operations, simulations, approvals
    and released NC text as one consistent state after a process restart.
    Pass ``database=None`` to retain the original ephemeral behaviour.
    """

    DEFAULT_TOOLS: tuple[ToolDefinition, ...] = (
        ToolDefinition("T10", "10 mm carbide end mill", "endmill", 10.0, 22.0),
        ToolDefinition("T06", "6 mm carbide end mill", "endmill", 6.0, 18.0),
        ToolDefinition("D20", "20 mm drill", "drill", 20.0, 35.0, max_rpm=3000, max_feed=300.0),
        ToolDefinition("CH2", "2 mm chamfer mill", "chamfer", 2.0, 8.0),
    )

    def __init__(
        self,
        *,
        tools: Iterable[ToolDefinition] | None = None,
        simulator: SimulationEngine | None = None,
        gate: CAMReleaseGate | None = None,
        authorizer: Callable[[str, str], bool] | None = None,
        database: str | Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.tools: dict[str, ToolDefinition] = {
            tool.id: tool for tool in (tools or self.DEFAULT_TOOLS)
        }
        for tool in self.tools.values():
            tool.validate()
        self.simulator = simulator or DeterministicSimulationEngine()
        self.gate = gate or CAMReleaseGate()
        self.authorizer = authorizer
        self._plans: dict[str, CAMPlan] = {}
        self._simulations: dict[str, SimulationResult] = {}
        self._nc_programs: dict[str, NCProgram] = {}
        self._storage_database = str(database) if database is not None else None
        self._storage_connection: sqlite3.Connection | None = None
        if self._storage_database is not None:
            self._open_storage(self._storage_database)

    def _open_storage(self, database: str) -> None:
        if database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if database != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        self._storage_connection = connection
        with self._lock, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cam_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT payload_json FROM cam_snapshots WHERE snapshot_id = 'default'"
            ).fetchone()
        if row is not None:
            try:
                payload = json.loads(str(row["payload_json"]))
                self._restore_locked(payload, replace=True, persist=False)
            except (TypeError, ValueError, json.JSONDecodeError, PlatformError) as exc:
                raise ValidationError("persisted CAM snapshot is invalid") from exc

    def close(self) -> None:
        """Close the optional SQLite connection (safe to call repeatedly)."""

        with self._lock:
            if self._storage_connection is not None:
                self._storage_connection.close()
                self._storage_connection = None

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "savedAt": _now(),
            "tools": [
                self.tools[key].to_dict() for key in sorted(self.tools)
            ],
            "plans": [
                self._plans[key].to_dict() for key in sorted(self._plans)
            ],
            "simulations": [
                self._simulations[key].to_dict() for key in sorted(self._simulations)
            ],
            "ncPrograms": [
                self._nc_programs[key].to_dict() for key in sorted(self._nc_programs)
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable, immutable-state snapshot."""

        with self._lock:
            return self._snapshot_locked()

    # Explicit aliases make the persistence contract discoverable to workers
    # and API adapters without coupling callers to the private helper above.
    export_snapshot = snapshot

    def snapshot_json(self) -> str:
        with self._lock:
            return _json(self._snapshot_locked())

    def _persist_locked(self) -> None:
        connection = self._storage_connection
        if connection is None:
            return
        payload = _json(self._snapshot_locked())
        with connection:
            connection.execute(
                """
                INSERT INTO cam_snapshots (snapshot_id, schema_version, payload_json, updated_at)
                VALUES ('default', 1, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (payload, _now()),
            )

    @staticmethod
    def _optional_text(raw: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = raw.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _stock_from_snapshot(raw: Any) -> StockDefinition:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot stock must be an object")
        try:
            stock = StockDefinition(
                length=float(raw["length"]),
                width=float(raw["width"]),
                height=float(raw["height"]),
                material=str(raw.get("material", "steel")),
                origin=str(raw.get("origin", "center_xy_bottom")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("CAM snapshot stock is invalid") from exc
        stock.validate()
        return stock

    @classmethod
    def _tool_from_snapshot(cls, raw: Any) -> ToolDefinition:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot tool must be an object")
        try:
            tool = ToolDefinition(
                id=str(raw.get("id", "")).strip(),
                name=str(raw.get("name", "")).strip(),
                kind=str(raw.get("kind", "endmill")).strip(),
                diameter=float(raw["diameter"]),
                flute_length=float(raw.get("fluteLength", raw.get("flute_length"))),
                max_rpm=int(raw.get("maxRpm", raw.get("max_rpm", 12000))),
                max_feed=float(raw.get("maxFeed", raw.get("max_feed", 1200.0))),
                material=str(raw.get("material", "carbide")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("CAM snapshot tool is invalid") from exc
        tool.validate()
        return tool

    @classmethod
    def _operation_from_snapshot(cls, raw: Any) -> CAMOperation:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot operation must be an object")
        try:
            parameters = raw.get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise ValidationError("CAM snapshot operation parameters must be an object")
            operation = CAMOperation(
                id=str(raw.get("id", "")).strip(),
                operation_type=str(raw.get("operationType", raw.get("operation_type", "profile"))).strip().casefold(),
                tool_id=str(raw.get("toolId", raw.get("tool_id", ""))).strip(),
                depth=float(raw.get("depth", 0)),
                feed_rate=float(raw.get("feedRate", raw.get("feed_rate", 600))),
                spindle_rpm=int(raw.get("spindleRpm", raw.get("spindle_rpm", 6000))),
                retract_height=float(raw.get("retractHeight", raw.get("retract_height", 5))),
                path_length=float(raw.get("pathLength", raw.get("path_length", 0))),
                parameters=dict(parameters),
                enabled=bool(raw.get("enabled", True)),
                sequence=int(raw.get("sequence", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("CAM snapshot operation is invalid") from exc
        if not operation.id or not operation.tool_id:
            raise ValidationError("CAM snapshot operation id and tool_id are required")
        if operation.operation_type not in {item.value for item in OperationType}:
            raise ValidationError(f"unsupported operation type in CAM snapshot: {operation.operation_type}")
        values = (operation.depth, operation.feed_rate, operation.retract_height, operation.path_length)
        if any(not math.isfinite(value) for value in values):
            raise ValidationError("CAM snapshot operation dimensions must be finite")
        return operation

    @classmethod
    def _approval_from_snapshot(cls, raw: Any) -> Approval:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot approval must be an object")
        try:
            approval = Approval(
                id=str(raw.get("id", "")).strip(),
                actor_id=str(raw.get("actorId", raw.get("actor_id", ""))).strip(),
                role=str(raw.get("role", "reviewer")).strip().casefold(),
                comment=str(raw.get("comment", "")),
                plan_revision=int(raw.get("planRevision", raw.get("plan_revision", 0))),
                simulation_id=str(raw.get("simulationId", raw.get("simulation_id", ""))).strip(),
                created_at=str(raw.get("createdAt", raw.get("created_at", ""))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("CAM snapshot approval is invalid") from exc
        if not approval.id or not approval.actor_id or not approval.simulation_id:
            raise ValidationError("CAM snapshot approval identity is required")
        if approval.role not in {"reviewer", "admin"}:
            raise ValidationError("CAM snapshot approval role is invalid")
        return approval

    @classmethod
    def _plan_from_snapshot(cls, raw: Any) -> CAMPlan:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot plan must be an object")
        operations_raw = raw.get("operations", [])
        approvals_raw = raw.get("approvals", [])
        if not isinstance(operations_raw, (list, tuple)) or not isinstance(approvals_raw, (list, tuple)):
            raise ValidationError("CAM snapshot plan operations/approvals must be arrays")
        try:
            operations = tuple(cls._operation_from_snapshot(item) for item in operations_raw)
            approvals = tuple(cls._approval_from_snapshot(item) for item in approvals_raw)
            status = str(raw.get("status", CAMPlanStatus.DRAFT.value)).strip().casefold()
            stock = cls._stock_from_snapshot(raw.get("stock", {}))
            plan = CAMPlan(
                id=str(raw.get("id", "")).strip(),
                project_id=cls._optional_text(raw, "projectId", "project_id"),
                source_document_id=cls._optional_text(raw, "sourceDocumentId", "source_document_id"),
                source_version_id=cls._optional_text(raw, "sourceVersionId", "source_version_id"),
                geometry_hash=str(raw.get("geometryHash", raw.get("geometry_hash", ""))).strip(),
                units=str(raw.get("units", "mm")).strip(),
                machine=str(raw.get("machine", "3-axis-mill")).strip(),
                stock=stock,
                operations=operations,
                status=status,
                revision=int(raw.get("revision", 1)),
                created_by=str(raw.get("createdBy", raw.get("created_by", ""))).strip(),
                created_at=str(raw.get("createdAt", raw.get("created_at", ""))),
                updated_at=str(raw.get("updatedAt", raw.get("updated_at", ""))),
                approvals=approvals,
                latest_simulation_id=cls._optional_text(raw, "latestSimulationId", "latest_simulation_id"),
                released_nc_id=cls._optional_text(raw, "releasedNcId", "released_nc_id"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("CAM snapshot plan is invalid") from exc
        if not plan.id or not plan.created_by or not plan.geometry_hash:
            raise ValidationError("CAM snapshot plan identity and geometry_hash are required")
        if plan.units not in {"mm", "in"}:
            raise ValidationError("CAM snapshot plan units are invalid")
        if plan.status not in {item.value for item in CAMPlanStatus}:
            raise ValidationError("CAM snapshot plan status is invalid")
        if plan.revision < 1:
            raise ValidationError("CAM snapshot plan revision must be positive")
        return plan

    @classmethod
    def _simulation_from_snapshot(cls, raw: Any) -> SimulationResult:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot simulation must be an object")
        try:
            warnings = raw.get("warnings", [])
            errors = raw.get("errors", [])
            checks = raw.get("checks", {})
            if not isinstance(warnings, (list, tuple)) or not isinstance(errors, (list, tuple)):
                raise ValidationError("CAM snapshot simulation warnings/errors must be arrays")
            if not isinstance(checks, Mapping):
                raise ValidationError("CAM snapshot simulation checks must be an object")
            result = SimulationResult(
                id=str(raw.get("id", "")).strip(),
                plan_id=str(raw.get("planId", raw.get("plan_id", ""))).strip(),
                plan_revision=int(raw.get("planRevision", raw.get("plan_revision", 0))),
                geometry_hash=str(raw.get("geometryHash", raw.get("geometry_hash", ""))).strip(),
                engine=str(raw.get("engine", "")).strip(),
                passed=bool(raw.get("passed", False)),
                collision_count=int(raw.get("collisionCount", raw.get("collision_count", 0))),
                gouge_count=int(raw.get("gougeCount", raw.get("gouge_count", 0))),
                envelope_violations=int(raw.get("envelopeViolations", raw.get("envelope_violations", 0))),
                unresolved_operations=int(raw.get("unresolvedOperations", raw.get("unresolved_operations", 0))),
                stock_remaining_volume=float(raw.get("stockRemainingVolume", raw.get("stock_remaining_volume", 0))),
                max_tool_load=float(raw.get("maxToolLoad", raw.get("max_tool_load", 0))),
                runtime_seconds=float(raw.get("runtimeSeconds", raw.get("runtime_seconds", 0))),
                warnings=tuple(str(item) for item in warnings),
                errors=tuple(str(item) for item in errors),
                checks={str(key): bool(value) for key, value in checks.items()},
                created_at=str(raw.get("createdAt", raw.get("created_at", ""))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("CAM snapshot simulation is invalid") from exc
        if not result.id or not result.plan_id or not result.geometry_hash:
            raise ValidationError("CAM snapshot simulation identity is required")
        if result.plan_revision < 1:
            raise ValidationError("CAM snapshot simulation revision must be positive")
        return result

    @classmethod
    def _nc_from_snapshot(cls, raw: Any) -> NCProgram:
        if not isinstance(raw, Mapping):
            raise ValidationError("CAM snapshot NC program must be an object")
        try:
            program = NCProgram(
                id=str(raw.get("id", "")).strip(),
                plan_id=str(raw.get("planId", raw.get("plan_id", ""))).strip(),
                simulation_id=str(raw.get("simulationId", raw.get("simulation_id", ""))).strip(),
                postprocessor=str(raw.get("postprocessor", "generic-3axis")).strip(),
                status=str(raw.get("status", "released")).strip(),
                text=str(raw.get("text", "")),
                sha256=str(raw.get("sha256", "")).strip(),
                created_by=str(raw.get("createdBy", raw.get("created_by", ""))).strip(),
                created_at=str(raw.get("createdAt", raw.get("created_at", ""))),
                released_at=cls._optional_text(raw, "releasedAt", "released_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("CAM snapshot NC program is invalid") from exc
        if not program.id or not program.plan_id or not program.simulation_id or not program.created_by:
            raise ValidationError("CAM snapshot NC identity is required")
        if hashlib.sha256(program.text.encode("utf-8")).hexdigest() != program.sha256:
            raise ValidationError("CAM snapshot NC checksum mismatch")
        return program

    def _restore_locked(
        self,
        snapshot: Mapping[str, Any],
        *,
        replace: bool = True,
        persist: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping):
            raise ValidationError("CAM snapshot must be an object")
        try:
            schema_version = int(snapshot.get("schemaVersion", snapshot.get("schema_version", 1)))
        except (TypeError, ValueError) as exc:
            raise ValidationError("CAM snapshot schemaVersion is invalid") from exc
        if schema_version != 1:
            raise ValidationError(f"unsupported CAM snapshot schema version: {schema_version}")
        tools_raw = snapshot.get("tools", [])
        plans_raw = snapshot.get("plans", [])
        simulations_raw = snapshot.get("simulations", [])
        nc_raw = snapshot.get("ncPrograms", snapshot.get("nc_programs", []))
        for value, label in (
            (tools_raw, "tools"),
            (plans_raw, "plans"),
            (simulations_raw, "simulations"),
            (nc_raw, "ncPrograms"),
        ):
            if not isinstance(value, (list, tuple)):
                raise ValidationError(f"CAM snapshot {label} must be an array")
        parsed_tools = {
            tool.id: tool for tool in (self._tool_from_snapshot(item) for item in tools_raw)
        }
        if not parsed_tools and replace:
            parsed_tools = {tool.id: tool for tool in self.DEFAULT_TOOLS}
        parsed_plans = {
            plan.id: plan for plan in (self._plan_from_snapshot(item) for item in plans_raw)
        }
        parsed_simulations = {
            simulation.id: simulation
            for simulation in (self._simulation_from_snapshot(item) for item in simulations_raw)
        }
        parsed_nc = {
            program.id: program for program in (self._nc_from_snapshot(item) for item in nc_raw)
        }
        if tools_raw and len(parsed_tools) != len(tools_raw):
            raise ValidationError("duplicate tool id in CAM snapshot")
        if len(parsed_plans) != len(plans_raw):
            raise ValidationError("duplicate plan id in CAM snapshot")
        if len(parsed_simulations) != len(simulations_raw):
            raise ValidationError("duplicate simulation id in CAM snapshot")
        if len(parsed_nc) != len(nc_raw):
            raise ValidationError("duplicate NC id in CAM snapshot")
        known_tools = set(parsed_tools)
        known_plans = set(parsed_plans)
        known_simulations = set(parsed_simulations)
        known_nc_ids = set(parsed_nc)
        if not replace:
            known_tools.update(self.tools)
            known_plans.update(self._plans)
            known_simulations.update(self._simulations)
            known_nc_ids.update(self._nc_programs)

        def known_simulation(identifier: str) -> SimulationResult | None:
            return parsed_simulations.get(identifier) or self._simulations.get(identifier)

        def lookup_nc(identifier: str) -> NCProgram | None:
            return parsed_nc.get(identifier) or self._nc_programs.get(identifier)

        for plan in parsed_plans.values():
            for operation in plan.operations:
                if operation.tool_id not in known_tools:
                    raise ValidationError(f"CAM snapshot references unknown tool: {operation.tool_id}")
            if plan.latest_simulation_id and plan.latest_simulation_id not in known_simulations:
                raise ValidationError(f"CAM snapshot references unknown simulation: {plan.latest_simulation_id}")
            if plan.latest_simulation_id:
                simulation = known_simulation(plan.latest_simulation_id)
                if simulation is None or simulation.plan_id != plan.id:
                    raise ValidationError("CAM snapshot latest simulation does not belong to its plan")
            if plan.released_nc_id and plan.released_nc_id not in known_nc_ids:
                raise ValidationError(f"CAM snapshot references unknown NC: {plan.released_nc_id}")
            if plan.released_nc_id:
                program = lookup_nc(plan.released_nc_id)
                if program is None or program.plan_id != plan.id:
                    raise ValidationError("CAM snapshot released NC does not belong to its plan")
            for approval in plan.approvals:
                if approval.simulation_id not in known_simulations:
                    raise ValidationError(f"CAM snapshot approval references unknown simulation: {approval.simulation_id}")
                simulation = known_simulation(approval.simulation_id)
                if simulation is None or simulation.plan_id != plan.id:
                    raise ValidationError("CAM snapshot approval simulation does not belong to its plan")
        for simulation in parsed_simulations.values():
            if simulation.plan_id not in known_plans:
                raise ValidationError(f"CAM snapshot simulation references unknown plan: {simulation.plan_id}")
        for program in parsed_nc.values():
            if program.plan_id not in known_plans or program.simulation_id not in known_simulations:
                raise ValidationError("CAM snapshot NC references unknown plan or simulation")
            simulation = known_simulation(program.simulation_id)
            if simulation is None or simulation.plan_id != program.plan_id:
                raise ValidationError("CAM snapshot NC simulation does not belong to its plan")
        if replace:
            self.tools = parsed_tools
            self._plans = parsed_plans
            self._simulations = parsed_simulations
            self._nc_programs = parsed_nc
        else:
            collections = (
                (self.tools, parsed_tools, "tool"),
                (self._plans, parsed_plans, "plan"),
                (self._simulations, parsed_simulations, "simulation"),
                (self._nc_programs, parsed_nc, "NC program"),
            )
            # Check every collection before mutating any of them so a merge
            # failure cannot leave a half-restored snapshot in memory.
            for collection, incoming, label in collections:
                collisions = set(collection).intersection(incoming)
                if collisions:
                    raise CAMConflictError(f"CAM snapshot {label} already exists: {sorted(collisions)[0]}")
            for collection, incoming, _label in collections:
                collection.update(incoming)
        if persist:
            self._persist_locked()
        return self._snapshot_locked()

    def restore(
        self,
        snapshot: Mapping[str, Any] | str | bytes,
        *,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Atomically restore a snapshot and return the resulting state.

        ``snapshot`` may be a mapping or UTF-8 JSON text/bytes.  Validation is
        performed into temporary collections first, so a malformed import does
        not partially overwrite live CAM state.
        """

        if isinstance(snapshot, (str, bytes, bytearray)):
            try:
                snapshot = json.loads(bytes(snapshot).decode("utf-8") if not isinstance(snapshot, str) else snapshot)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                raise ValidationError("CAM snapshot JSON is invalid") from exc
        with self._lock:
            return self._restore_locked(snapshot, replace=replace, persist=True)  # type: ignore[arg-type]

    restore_snapshot = restore

    def save_snapshot(self, path: str | Path) -> Path:
        """Write a portable JSON snapshot for backup or migration tooling."""

        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.snapshot_json(), encoding="utf-8")
        return destination

    def load_snapshot(self, path: str | Path, *, replace: bool = True) -> dict[str, Any]:
        source = Path(path).expanduser()
        try:
            payload = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(f"unable to read CAM snapshot: {source}") from exc
        return self.restore(payload, replace=replace)

    def _require(self, actor_id: str, permission: Permission) -> None:
        if self.authorizer is not None and not self.authorizer(actor_id, permission.value):
            raise AuthorizationError(f"missing permissions: {permission.value}")

    def register_tool(self, tool: ToolDefinition) -> ToolDefinition:
        tool.validate()
        with self._lock:
            if tool.id in self.tools:
                raise CAMConflictError(f"tool already exists: {tool.id}")
            self.tools[tool.id] = tool
            self._persist_locked()
        return tool

    def list_tools(self) -> list[ToolDefinition]:
        with self._lock:
            return list(self.tools.values())

    @staticmethod
    def _validate_hash(value: str) -> str:
        clean = value.strip()
        if not clean or len(clean) > 128:
            raise ValidationError("geometry_hash is required and must be at most 128 characters")
        return clean

    def create_plan(
        self,
        *,
        actor_id: str,
        geometry_hash: str,
        stock: StockDefinition | Mapping[str, Any],
        machine: str = "3-axis-mill",
        units: str = "mm",
        project_id: str | None = None,
        source_document_id: str | None = None,
        source_version_id: str | None = None,
        plan_id: str | None = None,
    ) -> CAMPlan:
        self._require(actor_id, Permission.CAM_PLAN)
        if isinstance(stock, Mapping):
            try:
                stock = StockDefinition(
                    length=float(stock["length"]),
                    width=float(stock["width"]),
                    height=float(stock["height"]),
                    material=str(stock.get("material", "steel")),
                    origin=str(stock.get("origin", "center_xy_bottom")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError("stock requires numeric length, width and height") from exc
        elif isinstance(stock, StockDefinition):
            try:
                stock = StockDefinition(
                    length=float(stock.length),
                    width=float(stock.width),
                    height=float(stock.height),
                    material=str(stock.material),
                    origin=str(stock.origin),
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError("stock requires numeric length, width and height") from exc
        else:
            raise ValidationError("stock must be a StockDefinition or object")
        stock.validate()
        if not machine.strip():
            raise ValidationError("machine is required")
        if units not in {"mm", "in"}:
            raise ValidationError("units must be mm or in")
        now = _now()
        plan = CAMPlan(
            id=plan_id or _id("cam"),
            project_id=project_id,
            source_document_id=source_document_id,
            source_version_id=source_version_id,
            geometry_hash=self._validate_hash(geometry_hash),
            units=units,
            machine=machine.strip(),
            stock=stock,
            operations=(),
            status=CAMPlanStatus.DRAFT.value,
            revision=1,
            created_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            if plan.id in self._plans:
                raise CAMConflictError(f"CAM plan already exists: {plan.id}")
            self._plans[plan.id] = plan
            self._persist_locked()
        return plan

    def get_plan(self, plan_id: str) -> CAMPlan:
        with self._lock:
            try:
                return self._plans[plan_id]
            except KeyError as exc:
                raise CAMNotFoundError(f"CAM plan not found: {plan_id}") from exc

    def list_plans(self, *, project_id: str | None = None) -> list[CAMPlan]:
        with self._lock:
            plans = list(self._plans.values())
        if project_id is not None:
            plans = [plan for plan in plans if plan.project_id == project_id]
        return sorted(plans, key=lambda plan: (plan.updated_at, plan.id), reverse=True)

    def add_operation(
        self,
        plan_id: str,
        *,
        actor_id: str,
        operation_type: str,
        tool_id: str,
        depth: float,
        feed_rate: float = 600.0,
        spindle_rpm: int = 6000,
        retract_height: float = 5.0,
        path_length: float = 0.0,
        parameters: Mapping[str, Any] | None = None,
        enabled: bool = True,
        expected_revision: int | None = None,
        operation_id: str | None = None,
    ) -> CAMOperation:
        self._require(actor_id, Permission.CAM_PLAN)
        plan = self.get_plan(plan_id)
        if plan.status in {CAMPlanStatus.RELEASED.value, CAMPlanStatus.APPROVED.value}:
            raise CAMConflictError("cannot edit an approved or released CAM plan")
        if expected_revision is not None and expected_revision != plan.revision:
            raise CAMConflictError("CAM plan revision is stale")
        clean_type = (
            operation_type.value
            if isinstance(operation_type, OperationType)
            else str(operation_type).strip().casefold()
        )
        if clean_type not in {item.value for item in OperationType}:
            raise ValidationError(f"unsupported operation type: {operation_type}")
        if tool_id not in self.tools:
            raise ValidationError(f"unknown tool: {tool_id}")
        values = (depth, feed_rate, retract_height, path_length)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValidationError("operation dimensions must be finite")
        operation = CAMOperation(
            id=operation_id or _id("op"),
            operation_type=clean_type,
            tool_id=tool_id,
            depth=float(depth),
            feed_rate=float(feed_rate),
            spindle_rpm=int(spindle_rpm),
            retract_height=float(retract_height),
            path_length=float(path_length),
            parameters=dict(parameters or {}),
            enabled=bool(enabled),
            sequence=len(plan.operations) + 1,
        )
        updated = replace(
            plan,
            operations=plan.operations + (operation,),
            status=CAMPlanStatus.DRAFT.value,
            revision=plan.revision + 1,
            updated_at=_now(),
            approvals=(),
            latest_simulation_id=None,
        )
        with self._lock:
            self._plans[plan_id] = updated
            self._persist_locked()
        return operation

    def simulate(
        self,
        plan_id: str,
        *,
        actor_id: str,
        expected_revision: int | None = None,
        engine: SimulationEngine | None = None,
    ) -> SimulationResult:
        # Simulation is a CAM-plan operation as well as a simulator action;
        # requiring both permissions keeps service/HTTP semantics aligned.
        self._require(actor_id, Permission.CAM_PLAN)
        self._require(actor_id, Permission.CAM_SIMULATE)
        plan = self.get_plan(plan_id)
        if expected_revision is not None and expected_revision != plan.revision:
            raise CAMConflictError("CAM plan revision is stale")
        selected_engine = engine or self.simulator
        try:
            result = selected_engine.simulate(plan, self.tools)
        except ValidationError as exc:
            # Keep a failed result available for audit and UI diagnosis.
            result = SimulationResult(
                id=_id("sim"),
                plan_id=plan.id,
                plan_revision=plan.revision,
                geometry_hash=plan.geometry_hash,
                engine=getattr(selected_engine, "name", selected_engine.__class__.__name__),
                passed=False,
                collision_count=0,
                gouge_count=0,
                envelope_violations=1,
                unresolved_operations=1,
                stock_remaining_volume=0.0,
                max_tool_load=0.0,
                runtime_seconds=0.001,
                warnings=(),
                errors=(str(exc),),
                checks={"engine_input_valid": False},
                created_at=_now(),
            )
        with self._lock:
            self._simulations[result.id] = result
            updated = replace(
                plan,
                status=(CAMPlanStatus.SIMULATED.value if result.passed else CAMPlanStatus.BLOCKED.value),
                updated_at=_now(),
                latest_simulation_id=result.id,
                approvals=(),
            )
            self._plans[plan_id] = updated
            self._persist_locked()
        return result

    def get_simulation(self, simulation_id: str) -> SimulationResult:
        with self._lock:
            try:
                return self._simulations[simulation_id]
            except KeyError as exc:
                raise CAMNotFoundError(f"simulation not found: {simulation_id}") from exc

    def approve(
        self,
        plan_id: str,
        *,
        actor_id: str,
        role: str = "reviewer",
        comment: str = "",
        simulation_id: str | None = None,
    ) -> Approval:
        self._require(actor_id, Permission.CAM_APPROVE)
        clean_role = str(role or "").strip().casefold() or "reviewer"
        if clean_role not in {"reviewer", "admin"}:
            raise AuthorizationError("CAM approval role must be reviewer or admin")
        plan = self.get_plan(plan_id)
        selected_id = simulation_id or plan.latest_simulation_id
        if not selected_id:
            raise CAMConflictError("run a simulation before approval")
        simulation = self.get_simulation(selected_id)
        if simulation.plan_id != plan.id or simulation.plan_revision != plan.revision:
            raise CAMConflictError("approval must reference the latest simulation for this plan revision")
        if not simulation.passed:
            raise CAMConflictError("cannot approve a failed simulation")
        approval = Approval(
            id=_id("apr"),
            actor_id=actor_id,
            role=clean_role,
            comment=comment.strip(),
            plan_revision=plan.revision,
            simulation_id=simulation.id,
            created_at=_now(),
        )
        with self._lock:
            updated = replace(
                plan,
                status=CAMPlanStatus.APPROVED.value,
                approvals=plan.approvals + (approval,),
                updated_at=_now(),
            )
            self._plans[plan_id] = updated
            self._persist_locked()
        return approval

    def gate_status(
        self,
        plan_id: str,
        *,
        actor_id: str | None = None,
        actor_permissions: Iterable[str] = (),
    ) -> GateDecision:
        plan = self.get_plan(plan_id)
        simulation = self._simulations.get(plan.latest_simulation_id or "")
        return self.gate.evaluate(
            plan,
            simulation,
            actor_id=actor_id,
            actor_permissions=actor_permissions,
        )

    @staticmethod
    def _render_nc(plan: CAMPlan, simulation: SimulationResult, postprocessor: str) -> str:
        lines = [
            "%",
            f"(JOYNIU NC DRAFT - {postprocessor})",
            f"(PLAN {plan.id} REV {plan.revision})",
            f"(GEOMETRY SHA256 {plan.geometry_hash})",
            f"(SIMULATION {simulation.id} ENGINE {simulation.engine})",
            "(RELEASE GATED: REVIEWED SIMULATION REQUIRED)",
            "G21 G90 G17",
            "G54",
        ]
        for operation in sorted(plan.operations, key=lambda item: item.sequence):
            if not operation.enabled:
                continue
            lines.extend(
                [
                    f"(OP {operation.sequence} {operation.operation_type.upper()} TOOL {operation.tool_id})",
                    f"S{operation.spindle_rpm} M03",
                    f"F{operation.feed_rate:g}",
                    f"(DEPTH {operation.depth:g} RETRACT {operation.retract_height:g} PATH {operation.path_length:g})",
                    "G00 Z" + f"{operation.retract_height:g}",
                    "M05",
                ]
            )
        lines.extend(["M30", "%"])
        return "\n".join(lines) + "\n"

    def release_nc(
        self,
        plan_id: str,
        *,
        actor_id: str,
        postprocessor: str = "generic-3axis",
        actor_permissions: Iterable[str] = (),
    ) -> NCProgram:
        self._require(actor_id, Permission.CAM_RELEASE)
        plan = self.get_plan(plan_id)
        decision = self.gate_status(
            plan_id,
            actor_id=actor_id,
            actor_permissions=actor_permissions,
        )
        # If an authorizer is installed, include the permission in policy input
        # even when the caller did not pass a permissions list explicitly.
        if self.authorizer is not None and not actor_permissions:
            decision = self.gate.evaluate(
                plan,
                self._simulations.get(plan.latest_simulation_id or ""),
                actor_id=actor_id,
                actor_permissions=(Permission.CAM_RELEASE.value,),
            )
        if not decision.passed:
            with self._lock:
                self._plans[plan_id] = replace(plan, status=CAMPlanStatus.BLOCKED.value, updated_at=_now())
            raise CAMGateRejected(decision)
        simulation = self._simulations[plan.latest_simulation_id or ""]
        text = self._render_nc(plan, simulation, postprocessor.strip() or "generic-3axis")
        program_id = _id("nc")
        program = NCProgram(
            id=program_id,
            plan_id=plan.id,
            simulation_id=simulation.id,
            postprocessor=postprocessor.strip() or "generic-3axis",
            status="released",
            text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            created_by=actor_id,
            created_at=_now(),
            released_at=_now(),
        )
        with self._lock:
            self._nc_programs[program_id] = program
            self._plans[plan_id] = replace(
                plan,
                status=CAMPlanStatus.RELEASED.value,
                released_nc_id=program_id,
                updated_at=_now(),
            )
            self._persist_locked()
        return program

    def get_nc(self, program_id: str) -> NCProgram:
        with self._lock:
            try:
                return self._nc_programs[program_id]
            except KeyError as exc:
                raise CAMNotFoundError(f"NC program not found: {program_id}") from exc


# A concise alias for callers that prefer ``NCService`` terminology.
NCService = CAMService


__all__ = [
    "Approval",
    "CAMConflictError",
    "CAMError",
    "CAMGateRejected",
    "CAMNotFoundError",
    "CAMOperation",
    "CAMPlan",
    "CAMPlanStatus",
    "CAMReleaseGate",
    "CAMService",
    "DeterministicSimulationEngine",
    "GateDecision",
    "NCProgram",
    "NCService",
    "OperationType",
    "SimulationResult",
    "StockDefinition",
    "ToolDefinition",
]
