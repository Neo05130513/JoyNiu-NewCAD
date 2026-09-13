#!/usr/bin/env python3
"""Host-side monitoring and opt-in coordinated backups. No external messages.

Use only for the documented single API writer deployment. Customer data is
never purged. Retention can remove only this tool's verified backup sets.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from urllib.request import urlopen
from uuid import uuid4

try:
    from . import commercial_backup
except ImportError:
    import commercial_backup


FORMAT = "joyniu-operations-v1"
ACTIVE = ("running", "queued", "cancel_requested")
MANAGED_NAME = re.compile(r"scheduled-\d{8}T\d{6}Z-[a-f0-9]{8}")
DEFAULTS = {
    "root": "/opt/joyniu-cad", "backupRoot": "/opt/joyniu-backups/scheduled",
    "reportRoot": "/var/lib/joyniu-cad-operations", "baseUrl": "http://127.0.0.1",
    "backupEnabled": False, "minimumFreeBytes": 5 * 1024**3,
    "diskWarningPercent": 80, "diskCriticalPercent": 90,
    "queuedWarningCount": 10, "queuedWarningMinutes": 30,
    "runningWarningMinutes": 60, "providerWarningMinutes": 30,
    "providerFailureWarningCount": 3, "backupMaxAgeHours": 26, "backupIntervalHours": 24,
    "retentionKeep": 0, "retentionMinAgeDays": 7, "restartTimeoutSeconds": 180,
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def parsed_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


def configuration(path):
    values = json.loads(Path(path).read_text())
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError("Unknown operations configuration")
    result = {**DEFAULTS, **values}
    for key in ("root", "backupRoot", "reportRoot"):
        candidate = Path(result[key])
        if not candidate.is_absolute() or any(p.is_symlink() for p in (candidate, *candidate.parents)):
            raise ValueError("Operations directories must be absolute and not symlinked")
        result[key] = str(candidate.resolve())
    root, backups, reports = (Path(result[key]) for key in ("root", "backupRoot", "reportRoot"))
    for left, right in ((root, backups), (root, reports), (backups, reports)):
        if left == right or left in right.parents or right in left.parents:
            raise ValueError("Deployment, backups and reports must be disjoint trees")
    if type(result["backupEnabled"]) is not bool:
        raise ValueError("backupEnabled must be boolean")
    for key, default in DEFAULTS.items():
        if type(default) is int and (type(result[key]) is not int or result[key] < 0):
            raise ValueError("Operations thresholds must be nonnegative integers")
    if not 0 < result["diskWarningPercent"] < result["diskCriticalPercent"] <= 100:
        raise ValueError("Invalid disk thresholds")
    if not 1 <= result["restartTimeoutSeconds"] <= 600 or result["retentionMinAgeDays"] < 1:
        raise ValueError("Invalid recovery or retention bounds")
    if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?", result["baseUrl"]):
        raise ValueError("Use the host loopback Web endpoint")
    return result


def atomic_json(path, value, mode=0o640):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive(path, *, create=True):
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o640)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


class OperationInProgress(Exception):
    """The host action is owned by another live invocation, not a failure."""


@contextmanager
def action_lock(path):
    acquired = False
    try:
        with exclusive(path):
            acquired = True
            yield
    except BlockingIOError:
        if acquired:
            raise  # An I/O failure inside the action is still a real failure.
        raise OperationInProgress from None


def read_jobs(root, current=None):
    """Aggregate only; never expose owners, prompts, files, errors or SQL rows."""
    current = current or datetime.now(timezone.utc)
    path = (Path(root) / "data/cad-agent/runs.sqlite3").resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        counts = dict(db.execute("SELECT status,count(*) FROM cad_job_attempts GROUP BY status"))
        operations = db.execute("SELECT count(*) FROM cad_billing_operations WHERE status='running'").fetchone()[0]
        unfinished = db.execute("SELECT count(*) FROM cad_provider_calls WHERE completed_at IS NULL").fetchone()[0]
        legacy = db.execute("""SELECT count(*) FROM cad_runs r WHERE revision=(
            SELECT max(revision) FROM cad_runs WHERE run_id=r.run_id)
            AND json_extract(payload,'$.status') IN ('running','queued','cancel_requested')""").fetchone()[0]

        def oldest(sql, params=()):
            value = db.execute(sql, params).fetchone()[0]
            return max(0, (current - parsed_time(value)).total_seconds()) if value else 0

        queued_age = oldest("SELECT min(created_at) FROM cad_job_attempts WHERE status='queued'")
        running_age = oldest("SELECT min(updated_at) FROM cad_job_attempts WHERE status IN ('running','cancel_requested')")
        provider_age = oldest("SELECT min(started_at) FROM cad_provider_calls WHERE completed_at IS NULL")
        failures = db.execute("""SELECT count(*) FROM cad_provider_calls WHERE completed_at>=?
            AND json_extract(usage_json,'$.outcome') IN ('failed','interrupted','incomplete','error')""",
            ((current - timedelta(hours=1)).isoformat(),)).fetchone()[0]
    return {"byStatus": counts, "queued": counts.get("queued", 0),
            "running": counts.get("running", 0), "cancelRequested": counts.get("cancel_requested", 0),
            "runningOperations": operations, "unfinishedProviderCalls": unfinished,
            "activeLegacyRevisions": legacy,
            "activeTotal": sum(counts.get(status, 0) for status in ACTIVE) + operations + unfinished + legacy,
            "oldestQueuedSeconds": queued_age, "oldestRunningSeconds": running_age,
            "oldestProviderSeconds": provider_age, "providerFailuresLastHour": failures}


class Host:
    def __init__(self, config):
        self.config = config

    def compose(self, *args, timeout=20):
        # Never log command output: Compose config and environment may be secret.
        return subprocess.run(["docker", "compose", *args], cwd=self.config["root"],
                              check=True, capture_output=True, text=True, timeout=timeout).stdout

    def services(self):
        output = self.compose("ps", "--all", "--format", "json")
        values = json.loads(output) if output.strip().startswith("[") else [json.loads(row) for row in output.splitlines() if row.strip()]
        return [{"service": row["Service"], "state": row.get("State", "unknown"),
                 "health": row.get("Health", ""), "publishers": row.get("Publishers") or []} for row in values]

    def gate(self):
        with urlopen(self.config["baseUrl"] + "/api/v1/operations/readiness", timeout=5) as response:
            result = json.load(response)
        if result.get("gateVersion") != "v1" or type(result.get("maintenance")) is not bool:
            raise ValueError("Coordinated backup gate is not installed")
        return result

    def jobs(self):
        return read_jobs(self.config["root"])

    def stop(self):
        self.compose("stop", "api", timeout=90)

    def start(self):
        # Start the existing container/image only. No build, pull or migration.
        self.compose("start", "api", timeout=90)

    def wait_ready(self):
        deadline = time.monotonic() + self.config["restartTimeoutSeconds"]
        while time.monotonic() < deadline:
            try:
                values = self.services()
                api = [row for row in values if row["service"] == "api"]
                with urlopen(self.config["baseUrl"] + "/api/v1/auth/account-capabilities", timeout=5) as response:
                    available = response.status == 200
                if len(api) == 1 and api[0]["state"] == "running" and api[0]["health"] == "healthy" and available:
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("Service recovery readiness failed")


def disk_report(path):
    value = shutil.disk_usage(path)
    return {"totalBytes": value.total, "freeBytes": value.free,
            "usedPercent": round(value.used * 100 / value.total, 2)}


def managed_sets(config):
    result = []
    root = Path(config["backupRoot"])
    if not root.exists():
        return result
    for path in root.iterdir():
        if not MANAGED_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        try:
            marker_path, manifest_path = path / "managed-backup.json", path / "manifest.json"
            if marker_path.is_symlink() or manifest_path.is_symlink():
                continue
            marker = json.loads(marker_path.read_text())
            if marker.get("format") != FORMAT or marker.get("deployment") != config["root"] or marker.get("name") != path.name:
                continue
            if marker.get("manifestSha256") != commercial_backup._digest(manifest_path):
                continue
            created = parsed_time(marker["createdAt"])
            result.append((created, path))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, reverse=True)


def retention(config, *, apply=False, current=None):
    sets = managed_sets(config)
    keep = config["retentionKeep"]
    current = current or datetime.now(timezone.utc)
    candidates = [(created, path) for created, path in sets[keep:]
                  if keep > 0 and current - created >= timedelta(days=config["retentionMinAgeDays"])]
    deleted = []
    if apply:
        if keep < 1:
            raise ValueError("Retention deletion is disabled")
        # Check every candidate before the first deletion. Only direct children
        # with valid provenance and full payload verification are eligible.
        for _, path in candidates:
            commercial_backup.verify(path)
            if set(item.name for item in path.iterdir()) != {"snapshot", "manifest.json", "managed-backup.json"}:
                raise ValueError("Backup contains unrecognized extra files")
        for _, path in candidates:
            shutil.rmtree(path)
            deleted.append(path.name)
    return {"enabled": keep > 0, "applied": apply, "keep": keep,
            "candidates": [path.name for _, path in candidates], "deleted": deleted}


def monitor(config, host=None):
    host = host or Host(config)
    alerts, checks = [], {}

    def alert(code, severity="warning"):
        alerts.append({"code": code, "severity": severity})

    try:
        services = host.services()
        checks["services"] = [{key: row[key] for key in ("service", "state", "health")} for row in services]
        for service in ("api", "web"):
            rows = [row for row in services if row["service"] == service]
            if len(rows) != 1 or rows[0]["state"] != "running" or (service == "api" and rows[0]["health"] != "healthy"):
                alert(service + "_unhealthy", "critical")
    except Exception:
        checks["services"] = None
        alert("service_check_failed", "critical")
    for key, path in (("dataDisk", Path(config["root"]) / "data"), ("backupDisk", Path(config["backupRoot"]).parent)):
        try:
            value = checks[key] = disk_report(path)
            if value["usedPercent"] >= config["diskCriticalPercent"] or value["freeBytes"] < config["minimumFreeBytes"]:
                alert(key + "_critical", "critical")
            elif value["usedPercent"] >= config["diskWarningPercent"]:
                alert(key + "_warning")
        except OSError:
            checks[key] = None
            alert(key + "_unavailable", "critical")
    try:
        jobs = checks["jobs"] = host.jobs()
        for key, setting, multiplier in (("queued", "queuedWarningCount", 1),
                ("oldestQueuedSeconds", "queuedWarningMinutes", 60),
                ("oldestRunningSeconds", "runningWarningMinutes", 60),
                ("oldestProviderSeconds", "providerWarningMinutes", 60),
                ("providerFailuresLastHour", "providerFailureWarningCount", 1)):
            if jobs[key] > 0 and jobs[key] >= config[setting] * multiplier:
                alert(key + "_threshold")
    except Exception:
        checks["jobs"] = None
        alert("job_journal_unreadable", "critical")
    try:
        checks["admissionGate"] = host.gate()
        if checks["admissionGate"]["maintenance"]:
            alert("maintenance_active")
    except Exception:
        checks["admissionGate"] = None
        alert("admission_gate_unavailable")
    try:
        sets = managed_sets(config)
        latest = sets[0] if sets else None
        checks["backup"] = {"automaticEnabled": config["backupEnabled"], "managedSets": len(sets),
                            "lastBackupAt": latest[0].isoformat() if latest else None, "lastVerifiedAt": None}
        if latest:
            commercial_backup.verify(latest[1])
            checks["backup"]["lastVerifiedAt"] = stamp()
        if not latest or datetime.now(timezone.utc) - latest[0] > timedelta(hours=config["backupMaxAgeHours"]):
            alert("backup_missing_or_stale", "critical")
    except Exception:
        checks["backup"] = {"automaticEnabled": config["backupEnabled"], "verificationFailed": True}
        alert("backup_verification_failed", "critical")
    if (Path(config["reportRoot"]) / "recovery.json").exists():
        try:
            with exclusive(Path(config["reportRoot"]) / "action.lock"):
                alert("service_recovery_pending", "critical")
        except BlockingIOError:
            checks["backupInProgress"] = True
            alerts = [item for item in alerts if item["code"] not in {"api_unhealthy", "admission_gate_unavailable", "maintenance_active"}]
            alert("maintenance_active")
    try:
        attempts = [json.loads(path.read_text()) for name in ("backup-latest.json", "backup-failure.json")
                    if (path := Path(config["reportRoot"]) / name).is_file()]
        attempt = max(attempts, key=lambda value: parsed_time(value["generatedAt"])) if attempts else {}
        checks["lastBackupAttempt"] = {key: attempt.get(key) for key in ("generatedAt", "status", "reason", "recovery")}
        if attempt.get("status") in {"failed", "critical"}:
            alert("last_backup_attempt_failed", "critical")
    except FileNotFoundError:
        checks["lastBackupAttempt"] = None
    except (OSError, ValueError):
        alert("backup_attempt_report_unreadable")
    report = {"format": FORMAT, "generatedAt": stamp(), "checks": checks, "alerts": alerts,
              "status": "critical" if any(item["severity"] == "critical" for item in alerts) else "warning" if alerts else "ok",
              "notifications": {"configured": False, "delivery": "local_report_only"},
              "offsiteBackup": {"configured": False, "verified": False}}
    atomic_json(Path(config["reportRoot"]) / "status.json", report)
    return report


def _owned_marker(config, token):
    path = Path(config["root"]) / "data/operations/maintenance.json"
    if path.is_symlink():
        raise ValueError("Unsafe maintenance marker")
    if path.exists():
        if json.loads(path.read_text()).get("token") != token:
            raise ValueError("Maintenance is owned by another operation")
        return path
    return None


def _finish_recovery_report(config, state):
    path = Path(config["reportRoot"]) / "backup-latest.json"
    if not path.is_file() or path.is_symlink():
        return
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(previous, dict) or previous.get("format") != FORMAT or previous.get("recovery") != "pending":
        return
    name = previous.get("backup")
    legacy_match = (isinstance(name, str) and MANAGED_NAME.fullmatch(name)
                    and name.endswith("-" + state["token"][:8]))
    if previous.get("operationId") != state["token"] and not legacy_match:
        return  # Never rewrite another operation's failure history.
    valid_backup = False
    if name:
        try:
            matching = next((directory for _, directory in managed_sets(config) if directory.name == name), None)
            if matching is not None:
                commercial_backup.verify(matching)
                valid_backup = True
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass
    # A healthy API does not repair a missing, partial or corrupt snapshot.
    # Preserve the original failure alongside the current recovery result.
    previous.update(attemptStartedAt=previous.get("attemptStartedAt", previous.get("generatedAt")),
                    previousFailure=previous.get("reason", "service_recovery_failed"),
                    generatedAt=stamp(), recoveryCompletedAt=stamp(), recovery="complete",
                    serviceRestarted=state["stopRequested"], status="completed" if valid_backup else "failed",
                    reason="service_recovered" if valid_backup else "backup_failed")
    atomic_json(path, previous)


def recover(config, host=None):
    """Idempotent crash recovery, called by systemd ExecStopPost as well."""
    host = host or Host(config)
    journal = Path(config["reportRoot"]) / "recovery.json"
    if not journal.exists():
        return {"status": "no_recovery_needed"}
    state = json.loads(journal.read_text())
    if (state.get("format") != FORMAT or state.get("deployment") != config["root"]
            or type(state.get("stopRequested")) is not bool):
        raise ValueError("Recovery journal does not belong to this deployment")
    marker = _owned_marker(config, state["token"])
    error = None
    try:
        if state["stopRequested"]:
            host.start()
    except Exception as exc:
        error = exc
    finally:
        # Even a failed start must not leave a hidden admission outage behind.
        if marker is not None:
            marker.unlink()
    if error is not None:
        raise RuntimeError("API restart failed; recovery remains pending") from None
    if state["stopRequested"]:
        host.wait_ready()
    _finish_recovery_report(config, state)
    journal.unlink()
    return {"status": "recovered", "serviceRestarted": state["stopRequested"]}


def coordinated_backup(config, host=None, *, execute=False):
    host = host or Host(config)
    report_root = Path(config["reportRoot"])
    report_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    result = {"format": FORMAT, "generatedAt": stamp(), "status": "planned", "serviceRestarted": False}
    if not execute or not config["backupEnabled"]:
        result["reason"] = "execution_not_enabled"
        atomic_json(report_root / "backup-latest.json", result)
        return result
    with action_lock(report_root / "action.lock"):
        if (report_root / "recovery.json").exists():
            raise ValueError("An earlier recovery is pending; run recover first")
        sets = managed_sets(config)
        if sets and datetime.now(timezone.utc) - sets[0][0] < timedelta(hours=config["backupIntervalHours"]):
            commercial_backup.verify(sets[0][1])
            result.update(status="skipped", reason="backup_not_due")
            atomic_json(report_root / "backup-latest.json", result)
            return result
        services = host.services()
        api = [row for row in services if row["service"] == "api"]
        if len(api) != 1 or api[0]["state"] != "running" or api[0]["health"] != "healthy":
            raise ValueError("Backup requires exactly one healthy running API")
        # Extra application services may be independent writers. This tool is
        # deliberately scoped to the reviewed api/web/egress topology.
        if any(row["service"] not in {"api", "web", "cad-egress"} and row["state"] == "running" for row in services):
            raise ValueError("Unknown running service; writer topology needs review")
        if host.gate()["maintenance"]:
            raise ValueError("Maintenance already active")
        jobs = host.jobs()
        if jobs["activeTotal"]:
            result.update(status="skipped", reason="active_or_queued_work")
            atomic_json(report_root / "backup-latest.json", result)
            return result
        backup_root = Path(config["backupRoot"])
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Include source/config size as well as data. This is a conservative
        # preflight; actual copy failure still follows the restart finally path.
        size = sum(path.stat().st_size for name in commercial_backup.INCLUDE
                   for path in ([Path(config["root"]) / name] if (Path(config["root"]) / name).is_file()
                                else (Path(config["root"]) / name).rglob("*"))
                   if path.is_file() and not path.is_symlink())
        if disk_report(backup_root)["freeBytes"] < size * 2 + config["minimumFreeBytes"]:
            raise ValueError("Insufficient backup capacity; customer files are never purged")
        gate_root = Path(config["root"]) / "data/operations"
        if gate_root.is_symlink() or not (gate_root / "admission.lock").is_file():
            raise ValueError("API admission lock is unavailable")
        token = uuid4().hex
        result["operationId"] = token
        state = {"format": FORMAT, "deployment": config["root"], "token": token, "stopRequested": False}
        atomic_json(report_root / "recovery.json", state, 0o600)
        try:
            # O_EXCL prevents overwriting a human or another tool's maintenance.
            descriptor = os.open(gate_root / "maintenance.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(descriptor, "w") as target:
                json.dump({"format": FORMAT, "token": token, "createdAt": stamp()}, target)
                target.flush()
                os.fsync(target.fileno())
            try:
                acquired = False
                with exclusive(gate_root / "admission.lock", create=False):
                    acquired = True
                    if not host.gate()["maintenance"]:
                        raise ValueError("Gate did not observe maintenance")
                    # An accepted request may have registered work between the
                    # first check and admission closure. Never stop that work.
                    if host.jobs()["activeTotal"]:
                        result.update(status="skipped", reason="work_arrived_during_check")
                    else:
                        state["stopRequested"] = True
                        atomic_json(report_root / "recovery.json", state, 0o600)
                        host.stop()
                        stopped = [row for row in host.services() if row["service"] == "api"]
                        if len(stopped) != 1 or stopped[0]["state"] not in {"exited", "stopped"}:
                            raise ValueError("API did not stop; snapshot refused")
                        if host.jobs()["activeTotal"]:
                            raise ValueError("Work detected after stopping; snapshot refused")
                        name = "scheduled-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + token[:8]
                        destination = backup_root / name
                        counts = commercial_backup.backup(config["root"], destination, quiescent=True)
                        commercial_backup.verify(destination)
                        atomic_json(destination / "managed-backup.json", {
                            "format": FORMAT, "deployment": config["root"], "name": name,
                            "createdAt": stamp(), "manifestSha256": commercial_backup._digest(destination / "manifest.json")}, 0o600)
                        result.update(status="completed", backup=name, **counts)
            except BlockingIOError:
                if acquired:
                    raise
                result.update(status="skipped", reason="request_in_flight")
        except BaseException:
            result.update(status="failed", reason="backup_failed")
            raise
        finally:
            try:
                recovery = recover(config, host)
                result["serviceRestarted"] = recovery.get("serviceRestarted", False)
                result["recovery"] = "complete"
            except Exception:
                result.update(status="critical", recovery="pending", reason="service_recovery_failed")
                raise
            finally:
                atomic_json(report_root / "backup-latest.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("monitor", "backup", "recover", "retention"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute", action="store_true", help="Enable the explicitly configured backup stop/start sequence")
    parser.add_argument("--apply-retention", action="store_true", help="Delete only eligible tool-created verified backup sets")
    args = parser.parse_args()
    config = None
    try:
        config = configuration(args.config)
        reports = Path(config["reportRoot"])
        reports.mkdir(parents=True, exist_ok=True, mode=0o750)
        # Ordinary systemd stop/timeout executes finally; SIGKILL/power loss is
        # handled by ExecStopPost and the boot recovery unit's saved journal.
        def interrupted(signum, frame):
            raise RuntimeError("Operations interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        if args.command == "backup":
            result = coordinated_backup(config, execute=args.execute)
        elif args.command == "monitor":
            result = monitor(config)
        else:
            with action_lock(reports / "action.lock"):
                result = recover(config) if args.command == "recover" else retention(config, apply=args.apply_retention)
        print(json.dumps(result, ensure_ascii=False))
        if args.command == "monitor" and result["status"] == "critical":
            raise SystemExit(2)
    except OperationInProgress:
        print(json.dumps({"format": FORMAT, "generatedAt": stamp(), "status": "skipped",
                          "command": args.command, "reason": "operation_in_progress"}))
    except Exception:
        result = {"format": FORMAT, "generatedAt": stamp(), "status": "failed", "command": args.command,
                  "reason": "operations_check_failed", "details": "Inspect the protected local report and service journal."}
        if config is not None:
            atomic_json(Path(config["reportRoot"]) / (args.command + "-failure.json"), result)
        print(json.dumps(result))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
