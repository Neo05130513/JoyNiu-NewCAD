"""Durable CAD jobs and immutable terminal revisions with their source files."""

from __future__ import annotations

import json
import copy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping
from uuid import uuid4


PROCESS_INSTANCE = uuid4().hex
TERMINAL_RUN_STATUSES = {"needs_input", "review_required", "failed", "interrupted"}
MAX_REVISION_REQUESTS = 128
MAX_REVISION_REQUEST_CHARACTERS = 64000


def source_question_reviews(value: Any) -> list[dict[str, Any]]:
    """Retain bounded source candidates, never delivery/verification claims."""
    if not isinstance(value, list):
        return []
    fields = ("version", "scope", "status", "questions", "answers", "unresolvedQuestions",
              "images", "sourceManifest", "questionFingerprint", "inputFingerprint",
              "errorCode", "providerMetrics", "elapsedSeconds")
    reports = []
    for item in value[-2:]:
        if (not isinstance(item, dict) or item.get("scope") != "source_question_reread"
                or item.get("status") not in {"succeeded", "failed"}
                or item.get("candidateEvidence") is not True or item.get("verified") is not False):
            continue
        report = {key: copy.deepcopy(item[key]) for key in fields if key in item}
        report.update({"candidateEvidence": True, "verified": False})
        reports.append(report)
    return reports


def comparison_policy(value: Any) -> dict[str, Any]:
    """Normalize server policy; incomplete/legacy claims retain the source gate."""
    strict = {"mode": "source_reproduction"}
    if not isinstance(value, Mapping):
        return strict
    requests = value.get("requests")
    if (value.get("mode") != "user_revision" or value.get("source") != "server_verified_parent"
            or not isinstance(value.get("baselinePlanHash"), str)
            or re.fullmatch(r"[a-f0-9]{64}", value["baselinePlanHash"]) is None
            or not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REVISION_REQUESTS
            or any(not isinstance(item, str) or not item.strip() or len(item) > 16000 for item in requests)
            or sum(len(item) for item in requests) > MAX_REVISION_REQUEST_CHARACTERS):
        return strict
    return {"mode": "user_revision", "source": "server_verified_parent",
            "baselinePlanHash": value["baselinePlanHash"], "requests": list(requests)}


def _bind_comparison_policy(record: dict[str, Any], authority: Mapping[str, Any]) -> dict[str, Any]:
    """Workers/checkpoints may revoke an exemption, never grant or widen it.

    The first durable API record is the authority for the run. Provider state
    and checkpoint drafts do not own this policy, including its request ledger.
    """
    result = copy.deepcopy(record)
    state = result.get("state")
    state = state if isinstance(state, dict) else {}
    policy = comparison_policy(authority.get("comparisonPolicy"))
    for proposed in (result.get("comparisonPolicy"), state.get("comparisonPolicy")):
        if isinstance(proposed, Mapping) and proposed.get("mode") == "source_reproduction":
            policy = {"mode": "source_reproduction"}
    result["comparisonPolicy"] = copy.deepcopy(policy)
    state["comparisonPolicy"] = copy.deepcopy(policy)
    result["state"] = state
    return result


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadRunConflict(ValueError):
    pass


class CadRunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "runs.sqlite3"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cad_runs (
                run_id TEXT NOT NULL, revision INTEGER NOT NULL, owner TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(run_id, revision))""")
        self.recover_interrupted()

    def _connect(self):
        return sqlite3.connect(self.database, timeout=15)

    def directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"cad_[a-f0-9]{32}", run_id):
            raise ValueError("invalid CAD run id")
        return self.root / run_id

    def checked_path(self, value: str | Path) -> Path:
        path = Path(value).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("CAD artifact is missing or outside its workspace")
        return path

    def load(self, run_id: str, revision: int | None = None) -> dict[str, Any]:
        self.directory(run_id)
        with self._connect() as db:
            if revision is None:
                row = db.execute("SELECT payload FROM cad_runs WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)).fetchone()
            else:
                row = db.execute("SELECT payload FROM cad_runs WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
        if row is None:
            raise KeyError("CAD run not found")
        return json.loads(row[0])

    def save(self, record: dict[str, Any], *, previous_revision: int = 0) -> None:
        run_id = record["runId"]
        self.directory(run_id)
        if record["revision"] != previous_revision + 1:
            raise CadRunConflict("CAD revision must advance by one")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = db.execute("SELECT MAX(revision) FROM cad_runs WHERE run_id=?", (run_id,)).fetchone()[0] or 0
            if head != previous_revision:
                raise CadRunConflict("模型已更新，请重新载入当前版本后再确认。")
            if head:
                row = db.execute("SELECT owner, payload FROM cad_runs WHERE run_id=? AND revision=?", (run_id, head)).fetchone()
                if row[0] != record["owner"]:
                    raise CadRunConflict("CAD run owner cannot change")
                record = _bind_comparison_policy(record, json.loads(row[1]))
            payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
            db.execute("INSERT INTO cad_runs VALUES (?, ?, ?, ?)", (run_id, record["revision"], record["owner"], payload))

    def complete_running(self, record: dict[str, Any]) -> dict[str, Any]:
        """Commit revision 1 once; no artifacts exist before this transition.

        Progress/running is a job lifecycle, not a published CAD revision.
        Once terminal, the existing append-only revision/CAS rules apply.
        """
        self.directory(record["runId"])
        if record.get("status") not in TERMINAL_RUN_STATUSES or record.get("revision") != 1:
            raise CadRunConflict("Only a running first revision can complete")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner, payload FROM cad_runs WHERE run_id=? ORDER BY revision DESC LIMIT 1", (record["runId"],)).fetchone()
            current = json.loads(row[1]) if row else {}
            if not row or row[0] != record["owner"] or current.get("revision") != 1 or current.get("status") != "running":
                raise CadRunConflict("CAD job is no longer running")
            record = _bind_comparison_policy(record, current)
            payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
            db.execute("UPDATE cad_runs SET payload=? WHERE run_id=? AND revision=1", (payload, record["runId"]))
        return record

    def update_progress(self, run_id: str, owner: str, progress: dict[str, Any]) -> bool:
        self.directory(run_id)
        # Validate/copy before acquiring the transaction; callbacks cannot
        # mutate persisted state after this call.
        progress = json.loads(json.dumps(progress, ensure_ascii=False, allow_nan=False))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner, payload FROM cad_runs WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)).fetchone()
            if not row or row[0] != owner:
                raise CadRunConflict("CAD job owner does not match")
            record = json.loads(row[1])
            if record.get("status") != "running":
                return False
            record.update({"progress": progress, "updatedAt": _utc()})
            db.execute("UPDATE cad_runs SET payload=? WHERE run_id=? AND revision=?",
                       (json.dumps(record, ensure_ascii=False, allow_nan=False), run_id, record["revision"]))
        return True

    def interrupted_record(self, record: dict[str, Any], *, status: str = "interrupted", code: str = "worker_interrupted") -> dict[str, Any]:
        """Recover only drafts/evidence from a server-owned checkpoint.

        A checkpoint is never a geometry pass or an export authorization.
        """
        result = copy.deepcopy(record)
        state = copy.deepcopy(record.get("state") or {})
        build_dir = self.directory(record["runId"]) / "builds"
        checkpoint = {}
        for filename in ("agent-state.json", "checkpoint.json"):
            path = (build_dir / filename).resolve()
            try:
                if not path.is_relative_to(build_dir.resolve()) or path.stat().st_size > 8 * 1024 * 1024:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    continue
                checkpoint = value.get("state", value) if filename == "agent-state.json" else value
                if not isinstance(checkpoint, dict):
                    checkpoint = {}
                    continue
                break
            except (OSError, ValueError):
                continue
        for key in ("observations", "trace", "sourceTranscription", "sourceSpatialContract", "sourceQuestionReviews", "comparisonPolicy", "retainedSourceDetails", "sourceFiles"):
            if key in checkpoint:
                state[key] = copy.deepcopy(checkpoint[key])
        state["sourceQuestionReviews"] = source_question_reviews(state.get("sourceQuestionReviews", record.get("sourceQuestionReviews")))
        plan = checkpoint.get("cadPlan", checkpoint.get("plan", state.get("cadPlan", result.get("plan"))))
        plan = copy.deepcopy(plan) if isinstance(plan, dict) else None
        draft = checkpoint.get("draftInspection", state.get("draftInspection"))
        plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest() if plan is not None else None
        if isinstance(draft, dict) and draft.get("scope") == "draft_construction" and plan_hash and draft.get("planHash") == plan_hash:
            draft = {key: copy.deepcopy(draft[key]) for key in ("status", "valid", "errors", "missingParameters", "inspection", "featureTrace", "planHash") if key in draft}
            draft.update({"scope": "draft_construction", "drawingAgreement": "not_checked", "deliveryArtifactsAvailable": False})
        else:
            draft = None
        state["draftInspection"] = draft
        review = {"status": "unverified", "source": "ai_visual_review", "humanConfirmed": False}
        trace = state.get("trace") if isinstance(state.get("trace"), list) else []
        trace = [*trace, {"action": "task_interrupted" if status == "interrupted" else "task_error", "code": code, "at": _utc()}]
        message = "服务中断，已保留原图和最新草稿；可以继续建模。" if status == "interrupted" else "本次建模异常结束，已保留原图和最新草稿；可以继续重试。"
        state.update({"status": status, "cadPlan": plan, "trace": trace, "drawingReview": review, "questions": []})
        result.update({"status": status, "message": message, "plan": plan, "state": state,
                       "trace": trace, "observations": state.get("observations", []),
                       "sourceTranscription": state.get("sourceTranscription"),
                       "sourceQuestionReviews": state["sourceQuestionReviews"],
                       "sourceSpatialContract": state.get("sourceSpatialContract"), "questions": [],
                       "comparisonPolicy": state.get("comparisonPolicy"),
                       "draftInspection": draft,
                       "inspection": None, "resolvedParameters": {}, "artifacts": {}, "drawingReview": review,
                       "completedAt": _utc(), "updatedAt": _utc(), "progress": {"stage": status, "message": message},
                       "provider": {**(result.get("provider") or {}), "lastErrorCode": code}})
        return _bind_comparison_policy(result, record)

    def recover_interrupted(self) -> int:
        """Resolve abandoned jobs without interrupting another live API worker."""
        with self._connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM cad_runs WHERE json_extract(payload, '$.status')='running'")]
        recovered = 0
        for record in records:
            pid = record.get("workerPid")
            if pid == os.getpid() and record.get("workerInstance") == PROCESS_INSTANCE:
                continue
            if type(pid) is int and pid > 0 and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    continue
                except PermissionError:
                    continue  # An inaccessible process may still be alive.
                except ProcessLookupError:
                    pass
            try:
                self.complete_running(self.interrupted_record(record))
                recovered += 1
            except CadRunConflict:
                pass  # A live worker completed concurrently; retain its result.
        return recovered
