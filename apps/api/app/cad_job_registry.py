"""Server-owned task identities, dispatch limits and provider-call journal.

This journal never estimates or reserves credits. Terminal usage is handed to
the explicit billing policy. Geometry and revision authorization stay in CadRunStore.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
from uuid import uuid4

from .cad_agent_store import PROCESS_INSTANCE


ACTIVE = {"queued", "running", "cancel_requested"}


def now():
    return datetime.now(timezone.utc).isoformat()


class JobConflict(ValueError):
    pass


class JobCapacityExceeded(ValueError):
    pass


class JobRequestCancelled(JobConflict):
    pass


def _setting(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


class CadJobRegistry:
    def __init__(self, store):
        self.store = store
        self.database = store.database
        self.max_concurrent = _setting("JOYNIU_CAD_MAX_CONCURRENT_RUNS", 1, 1, 16)
        self.max_queued = _setting("JOYNIU_CAD_MAX_QUEUED_RUNS", 20, 0, 500)
        self.max_per_owner = _setting("JOYNIU_CAD_MAX_ACTIVE_PER_USER", 1, 1, self.max_concurrent)
        self._lock = threading.RLock()
        self.billing = self.billing_policy = None
        with self._connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS cad_job_attempts (
                run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, owner TEXT NOT NULL,
                request_id TEXT NOT NULL, request_hash TEXT NOT NULL, parent_run_id TEXT,
                change_kind TEXT NOT NULL, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner,request_id));
              CREATE INDEX IF NOT EXISTS cad_job_owner ON cad_job_attempts(owner,created_at);
              CREATE TABLE IF NOT EXISTS cad_request_cancellations (
                owner TEXT NOT NULL, request_id TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(owner,request_id));
              CREATE TABLE IF NOT EXISTS cad_billing_operations (
                attempt_id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
                status TEXT NOT NULL, worker_pid INTEGER NOT NULL, worker_instance TEXT NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT);
              CREATE TABLE IF NOT EXISTS cad_provider_calls (
                call_id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                stage TEXT NOT NULL, identity_json TEXT NOT NULL, started_at TEXT NOT NULL,
                completed_at TEXT, usage_json TEXT, delivered INTEGER NOT NULL DEFAULT 0);
            """)
            # Existing task registries upgrade without touching CAD revisions.
            for table in ("cad_job_attempts", "cad_provider_calls"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "worker_pid" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN worker_pid INTEGER NOT NULL DEFAULT 0")
                if "worker_instance" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN worker_instance TEXT NOT NULL DEFAULT ''")

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def identity(row):
        return {"runId": row["run_id"], "jobId": row["job_id"], "attemptId": row["run_id"],
                "requestId": row["request_id"], "changeKind": row["change_kind"], "status": row["status"],
                "cancelRequested": bool(row["cancel_requested"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"]}

    def get(self, run_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM cad_job_attempts WHERE run_id=?", (run_id,)).fetchone()
        return self.identity(row) if row else None

    def by_request(self, owner, request_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM cad_job_attempts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            cancelled = db.execute("SELECT 1 FROM cad_request_cancellations WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
        return self.identity(row) if row else {"runId": None, "requestId": request_id, "status": "cancelled"} if cancelled else None

    def cancel_request(self, owner, request_id):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO cad_request_cancellations VALUES (?,?,?)", (owner, request_id, now()))
            row = db.execute("SELECT * FROM cad_job_attempts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if row and row["status"] in ACTIVE:
                status = "queued" if row["status"] == "queued" else "cancel_requested"
                db.execute("UPDATE cad_job_attempts SET cancel_requested=1,status=?,updated_at=? WHERE run_id=?", (status, now(), row["run_id"]))
                row = db.execute("SELECT * FROM cad_job_attempts WHERE run_id=?", (row["run_id"],)).fetchone()
            return self.identity(row) if row else {"runId": None, "requestId": request_id, "status": "cancelled"}

    def _eligible(self, db, owner):
        count = db.execute("SELECT count(*) FROM cad_job_attempts WHERE status IN ('running','cancel_requested')").fetchone()[0]
        mine = db.execute("SELECT count(*) FROM cad_job_attempts WHERE owner=? AND status IN ('running','cancel_requested')", (owner,)).fetchone()[0]
        count += db.execute("SELECT count(*) FROM cad_billing_operations WHERE status='running'").fetchone()[0]
        mine += db.execute("SELECT count(*) FROM cad_billing_operations WHERE owner=? AND status='running'", (owner,)).fetchone()[0]
        return count < self.max_concurrent and mine < self.max_per_owner

    def _oldest_eligible(self, db):
        rows = db.execute("SELECT * FROM cad_job_attempts WHERE status='queued' AND cancel_requested=0 ORDER BY created_at,run_id").fetchall()
        return next((item for item in rows if self._eligible(db, item["owner"])), None)

    def claim(self, *, run_id, owner, request_id, request_hash, parent=None, change_kind="new_drawing"):
        """Atomically de-duplicate and reserve only execution capacity, not money."""
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM cad_job_attempts WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise JobConflict("同一请求编号不能提交不同的图纸或建模要求。")
                return self.identity(prior), False
            if db.execute("SELECT 1 FROM cad_request_cancellations WHERE owner=? AND request_id=?", (owner, request_id)).fetchone():
                raise JobRequestCancelled("此请求已取消，不会再启动建模。")
            status = "running" if self._eligible(db, owner) and self._oldest_eligible(db) is None else "queued"
            if status == "queued" and db.execute("SELECT count(*) FROM cad_job_attempts WHERE status='queued'").fetchone()[0] >= self.max_queued:
                raise JobCapacityExceeded("当前任务队列已满，请稍后重试；本次尚未开始建模。")
            parent_run_id = parent.get("runId") if parent else None
            parent_row = db.execute("SELECT job_id FROM cad_job_attempts WHERE run_id=? AND owner=?", (parent_run_id, owner)).fetchone() if parent_run_id else None
            job_id = parent_row[0] if parent_row else parent.get("jobId") if parent else None
            job_id = job_id or ("job_" + parent_run_id[4:] if parent_run_id else "job_" + uuid4().hex)
            stamp = now()
            db.execute("INSERT INTO cad_job_attempts(run_id,job_id,owner,request_id,request_hash,parent_run_id,change_kind,status,cancel_requested,created_at,updated_at,worker_pid,worker_instance) VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?)", (run_id, job_id, owner, request_id, request_hash, parent_run_id, change_kind, status, stamp, stamp, os.getpid(), PROCESS_INSTANCE))
            return self.identity(db.execute("SELECT * FROM cad_job_attempts WHERE run_id=?", (run_id,)).fetchone()), True

    def try_start(self, run_id):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cad_job_attempts WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["cancel_requested"]:
                return False
            if row["status"] == "running":
                return True
            if row["status"] != "queued" or not self._eligible(db, row["owner"]):
                return False
            # Earliest eligible owner wins; a busy owner does not block everyone.
            first = self._oldest_eligible(db)
            if not first or first["run_id"] != run_id:
                return False
            db.execute("UPDATE cad_job_attempts SET status='running',updated_at=? WHERE run_id=?", (now(), run_id))
            return True

    def request_cancel(self, run_id, owner):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cad_job_attempts WHERE run_id=? AND owner=?", (run_id, owner)).fetchone()
            if not row:
                return None
            if row["status"] in ACTIVE:
                # Queued tasks stay queued in the capacity count until their
                # owning dispatcher acknowledges cancellation.
                status = "queued" if row["status"] == "queued" else "cancel_requested"
                db.execute("UPDATE cad_job_attempts SET cancel_requested=1,status=?,updated_at=? WHERE run_id=?", (status, now(), run_id))
            return self.identity(db.execute("SELECT * FROM cad_job_attempts WHERE run_id=?", (run_id,)).fetchone())

    def finish(self, run_id, status):
        # Known terminal usage is charged before releasing dispatch capacity,
        # so the next queued attempt sees the updated balance/due state.
        with self._connect() as db:
            row = db.execute("SELECT * FROM cad_job_attempts WHERE run_id=?", (run_id,)).fetchone()
        if row and self.billing_policy is not None:
            self._settle_one(self.billing, self.billing_policy, row["owner"], row["job_id"], run_id, status)
        with self._lock, self._connect() as db:
            db.execute("UPDATE cad_job_attempts SET status=?,updated_at=? WHERE run_id=? AND status IN ('running','queued','cancel_requested')", (status, now(), run_id))

    def reconcile(self):
        self.store.recover_interrupted()
        finished_jobs, finished_operations = [], []
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT a.*,r.payload FROM cad_job_attempts a LEFT JOIN cad_runs r ON r.run_id=a.run_id AND r.revision=(SELECT max(revision) FROM cad_runs WHERE run_id=a.run_id) WHERE a.status IN ('running','queued','cancel_requested')").fetchall()
            for row in rows:
                if not row["payload"]:
                    if not self._worker_alive(row):
                        finished_jobs.append((row["run_id"], "interrupted"))
                    continue  # Claim/save is a short separate transaction window.
                record = json.loads(row["payload"])
                if record.get("status") != "running":
                    finished_jobs.append((row["run_id"], record["status"]))
            unfinished = db.execute("SELECT * FROM cad_provider_calls WHERE completed_at IS NULL").fetchall()
            for row in unfinished:
                if not self._worker_alive(row):
                    db.execute("UPDATE cad_provider_calls SET completed_at=?,usage_json=? WHERE call_id=? AND completed_at IS NULL", (now(), json.dumps({"input_tokens": None, "output_tokens": None, "cached_input_tokens": None, "reasoning_output_tokens": None, "cost_micro_usd": None, "outcome": "interrupted"}), row["call_id"]))
            for row in db.execute("SELECT * FROM cad_billing_operations WHERE status='running'").fetchall():
                if not self._worker_alive(row):
                    finished_operations.append(row["attempt_id"])
        # Also preserve settle-before-release ordering during reads/recovery,
        # which can race a worker after its final CAD record is persisted.
        for run_id, status in finished_jobs:
            self.finish(run_id, status)
        for attempt_id in finished_operations:
            self.finish_operation(attempt_id, "interrupted")

    @staticmethod
    def _worker_alive(row):
        if row["worker_pid"] == os.getpid():
            return row["worker_instance"] == PROCESS_INSTANCE
        if row["worker_pid"] <= 0:
            return False
        try:
            os.kill(row["worker_pid"], 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False

    def decorate(self, record):
        value = dict(record)
        identity = self.get(record["runId"])
        if identity:
            value.update({key: identity[key] for key in ("jobId", "attemptId", "requestId", "changeKind", "cancelRequested")})
            if record.get("status") == "running":
                value["status"] = "cancel_requested" if identity["cancelRequested"] else identity["status"]
                if value["status"] == "queued":
                    value["message"] = "正在排队等待建模资源，可离开页面后回来查看。"
                elif value["status"] == "cancel_requested":
                    value["message"] = "正在取消，等待已开始的调用和几何进程停止；不会再启动后续步骤。"
                value["progress"] = {**(record.get("progress") or {}), "status": value["status"]}
                if value["status"] != "running":
                    value["progress"].update({"stage": value["status"], "message": value["message"]})
        else:
            value.update({"jobId": record.get("jobId") or "job_" + record["runId"][4:], "attemptId": record["runId"]})
        return value

    def list_jobs(self, owner, *, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("分页参数不正确")
        self.reconcile()
        with self._connect() as db:
            total = db.execute("SELECT count(DISTINCT run_id) FROM cad_runs WHERE owner=?", (owner,)).fetchone()[0]
            rows = db.execute("SELECT payload FROM cad_runs r WHERE owner=? AND revision=(SELECT max(revision) FROM cad_runs WHERE run_id=r.run_id) ORDER BY json_extract(payload,'$.createdAt') DESC,run_id DESC LIMIT ? OFFSET ?", (owner, limit, offset)).fetchall()
        items = []
        for row in rows:
            record = self.decorate(json.loads(row[0]))
            items.append({key: record.get(key) for key in ("runId", "jobId", "attemptId", "requestId", "parentRunId", "revision", "status", "changeKind", "createdAt", "updatedAt", "completedAt", "message", "progress", "provider")})
            items[-1]["name"] = (record.get("plan") or {}).get("name") or next((item.get("filename") for item in record.get("files", []) if item.get("filename")), "建模任务")
            items[-1]["sourceFiles"] = [{key: item.get(key) for key in ("filename", "contentType", "sha256")} for item in record.get("files", [])]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def begin_call(self, *, call_id, owner, job_id, attempt_id, stage, identity):
        with self._connect() as db:
            db.execute("INSERT INTO cad_provider_calls(call_id,owner,job_id,attempt_id,stage,identity_json,started_at,worker_pid,worker_instance) VALUES (?,?,?,?,?,?,?,?,?)", (call_id, owner, job_id, attempt_id, stage, json.dumps(identity), now(), os.getpid(), PROCESS_INSTANCE))

    def begin_operation(self, *, attempt_id, owner, job_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._eligible(db, owner):
                raise JobCapacityExceeded("当前建模资源繁忙，请稍后重试尺寸定位；本次尚未调用AI。")
            db.execute("INSERT INTO cad_billing_operations VALUES (?,?,?,'running',?,?,?,NULL)", (attempt_id, owner, job_id, os.getpid(), PROCESS_INSTANCE, now()))

    def finish_operation(self, attempt_id, status):
        with self._connect() as db:
            row = db.execute("SELECT * FROM cad_billing_operations WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row and self.billing_policy is not None:
            self._settle_one(self.billing, self.billing_policy, row["owner"], row["job_id"], attempt_id, status)
        with self._connect() as db:
            db.execute("UPDATE cad_billing_operations SET status=?,completed_at=? WHERE attempt_id=? AND status='running'", (status, now(), attempt_id))

    def _settle_one(self, billing, policy, owner, job_id, attempt_id, status):
        try:
            self.deliver_usage(billing)
            with self._connect() as db:
                calls = db.execute("SELECT call_id,completed_at FROM cad_provider_calls WHERE owner=? AND attempt_id=?", (owner, attempt_id)).fetchall()
            if any(row["completed_at"] is None for row in calls):
                return
            policy.settle_attempt(owner_id=owner, job_id=job_id, attempt_id=attempt_id,
                                  terminal_status=status, call_ids=[row["call_id"] for row in calls])
        except Exception:
            pass  # Persisted call/attempt records will be retried by recovery.

    def settle_pending(self, billing, policy, *, owner=None):
        if policy is None:
            return
        self.deliver_usage(billing)
        with self._connect() as db:
            batches = db.execute("SELECT run_id AS attempt_id,owner,job_id,status FROM cad_job_attempts WHERE status NOT IN ('running','queued','cancel_requested') UNION ALL SELECT attempt_id,owner,job_id,status FROM cad_billing_operations WHERE status!='running'").fetchall()
            for batch in batches:
                if owner is not None and owner != batch["owner"]:
                    continue
                self._settle_one(billing, policy, batch["owner"], batch["job_id"], batch["attempt_id"], batch["status"])

    def finish_call(self, call_id, usage):
        with self._connect() as db:
            db.execute("UPDATE cad_provider_calls SET completed_at=?,usage_json=? WHERE call_id=? AND completed_at IS NULL", (now(), json.dumps(usage, allow_nan=False), call_id))

    def deliver_usage(self, billing, *, call_id=None):
        if billing is None:
            return
        with self._connect() as db:
            rows = db.execute("SELECT * FROM cad_provider_calls WHERE completed_at IS NOT NULL AND delivered=0" + (" AND call_id=?" if call_id else ""), (call_id,) if call_id else ()).fetchall()
        for row in rows:
            try:
                billing.record_usage(owner_id=row["owner"], job_id=row["job_id"], attempt_id=row["attempt_id"], call_id=row["call_id"], stage=row["stage"], **json.loads(row["identity_json"]), **json.loads(row["usage_json"]))
            except Exception:
                # Durable pending delivery is retried; never discard usage or
                # fabricate zero cost because the billing DB was unavailable.
                continue
            with self._connect() as db:
                db.execute("UPDATE cad_provider_calls SET delivered=1 WHERE call_id=?", (row["call_id"],))
