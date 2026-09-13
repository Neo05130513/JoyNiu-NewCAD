"""Isolated host simulation; no Docker/production stops or model calls."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
SPEC = importlib.util.spec_from_file_location("commercial_operations", DEPLOY / "commercial_operations.py")
ops = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(DEPLOY))
try:
    SPEC.loader.exec_module(ops)
finally:
    sys.path.remove(str(DEPLOY))


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "deployment"
    (root / "data/cad-agent").mkdir(parents=True)
    (root / "data/operations").mkdir()
    (root / "data/operations/admission.lock").touch()
    (root / "data/cad-agent/original.dwg").write_bytes(b"customer drawing fixture")
    (root / "data/operations/customer-notes.txt").write_text("preserve even next to runtime locks")
    (root / "deploy").mkdir()
    (root / "deploy/.env.production").write_text("FAKE_SECRET=fixture-only")
    with closing(sqlite3.connect(root / "data/joyniu.sqlite3")) as db:
        db.execute("CREATE TABLE billing_ledger(id TEXT, credits INTEGER)")
        db.execute("INSERT INTO billing_ledger VALUES ('offline-fixture',42)")
        db.commit()
    with closing(sqlite3.connect(root / "data/cad-agent/runs.sqlite3")) as db:
        db.executescript("""
            CREATE TABLE cad_runs(run_id TEXT,revision INTEGER,payload TEXT);
            CREATE TABLE cad_job_attempts(run_id TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE cad_billing_operations(status TEXT);
            CREATE TABLE cad_provider_calls(completed_at TEXT,started_at TEXT,usage_json TEXT);
        """)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"root": str(root), "backupRoot": str(tmp_path / "backups"),
        "reportRoot": str(tmp_path / "reports"), "backupEnabled": True,
        "minimumFreeBytes": 0, "backupIntervalHours": 0}))
    return ops.configuration(path)


class FakeHost:
    def __init__(self, config):
        self.config = config
        self.events = []
        self.state = "running"
        self.health = "healthy"
        self.extra_services = []
        self.job_reads = 0
        self.race = False
        self.stop_fail = False
        self.start_fail = False

    def services(self):
        return [{"service": "api", "state": self.state, "health": self.health},
                {"service": "web", "state": "running", "health": ""},
                {"service": "cad-egress", "state": "running", "health": ""}, *self.extra_services]

    def gate(self):
        return {"gateVersion": "v1", "maintenance": (Path(self.config["root"]) / "data/operations/maintenance.json").exists()}

    def jobs(self):
        self.job_reads += 1
        result = ops.read_jobs(self.config["root"])
        if self.race and self.job_reads == 2:
            result["activeTotal"] = 1
        return result

    def stop(self):
        self.events.append("stop")
        assert self.gate()["maintenance"]
        with pytest.raises(BlockingIOError), ops.exclusive(Path(self.config["root"]) / "data/operations/admission.lock"):
            pass
        self.state = "exited"
        if self.stop_fail:
            raise RuntimeError("simulated stop command failure")

    def start(self):
        self.events.append("start")
        if self.start_fail:
            raise RuntimeError("simulated daemon failure")
        self.state = "running"

    def wait_ready(self):
        self.events.append("ready")
        assert not self.gate()["maintenance"]


def sql(config, statement, values=()):
    with closing(sqlite3.connect(Path(config["root"]) / "data/cad-agent/runs.sqlite3")) as db:
        db.execute(statement, values)
        db.commit()


def test_idle_backup_verifies_two_databases_and_restores_without_maintenance(config, tmp_path):
    host = FakeHost(config)
    result = ops.coordinated_backup(config, host, execute=True)
    assert result["status"] == "completed" and result["databases"] == 2
    assert host.events == ["stop", "start", "ready"]
    assert host.job_reads == 3
    assert not host.gate()["maintenance"]
    snapshot = Path(config["backupRoot"]) / result["backup"]
    restored = tmp_path / "isolated-restored"
    ops.commercial_backup.restore(snapshot, restored)
    assert (restored / "data/cad-agent/original.dwg").read_bytes() == b"customer drawing fixture"
    assert (restored / "data/operations/customer-notes.txt").is_file()
    assert not (restored / "data/operations/maintenance.json").exists()
    assert not (restored / "data/operations/admission.lock").exists()
    with closing(sqlite3.connect(restored / "data/joyniu.sqlite3")) as db:
        assert db.execute("SELECT credits FROM billing_ledger").fetchone()[0] == 42
    assert len(ops.managed_sets(config)) == 1
    assert not (Path(config["reportRoot"]) / "recovery.json").exists()


@pytest.mark.parametrize("kind", ["queued", "running", "cancel_requested", "billing", "provider", "legacy"])
def test_every_active_or_queued_writer_prevents_stop(config, kind):
    if kind in ops.ACTIVE:
        sql(config, "INSERT INTO cad_job_attempts VALUES (?,?,?,?)", ("fixture", kind, ops.stamp(), ops.stamp()))
    elif kind == "billing":
        sql(config, "INSERT INTO cad_billing_operations VALUES ('running')")
    elif kind == "provider":
        sql(config, "INSERT INTO cad_provider_calls VALUES (NULL,?,NULL)", (ops.stamp(),))
    else:
        sql(config, "INSERT INTO cad_runs VALUES ('fixture',1,?)", (json.dumps({"status": "running"}),))
    host = FakeHost(config)
    result = ops.coordinated_backup(config, host, execute=True)
    assert result["reason"] == "active_or_queued_work" and host.events == []
    assert not host.gate()["maintenance"]


def test_work_registered_between_checks_is_never_interrupted(config):
    host = FakeHost(config)
    host.race = True
    result = ops.coordinated_backup(config, host, execute=True)
    assert result["reason"] == "work_arrived_during_check"
    assert host.events == [] and not host.gate()["maintenance"]


def test_in_flight_http_request_causes_skip_without_stop(config):
    host = FakeHost(config)
    with open(Path(config["root"]) / "data/operations/admission.lock", "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        result = ops.coordinated_backup(config, host, execute=True)
    assert result["reason"] == "request_in_flight"
    assert host.events == [] and not host.gate()["maintenance"]


@pytest.mark.parametrize("failure", ["stop", "backup", "verify", "copy-blocking-io"])
def test_failure_always_attempts_restart_and_releases_owned_maintenance(config, monkeypatch, failure):
    host = FakeHost(config)
    def broken(*args, **kwargs):
        if failure == "copy-blocking-io":
            raise BlockingIOError("simulated copy error")
        raise RuntimeError("simulated fixture failure")
    if failure == "stop":
        host.stop_fail = True
    else:
        monkeypatch.setattr(ops.commercial_backup, "backup" if failure == "copy-blocking-io" else failure, broken)
    with pytest.raises((RuntimeError, BlockingIOError)):
        ops.coordinated_backup(config, host, execute=True)
    assert host.events == ["stop", "start", "ready"]
    assert not host.gate()["maintenance"]
    report = json.loads((Path(config["reportRoot"]) / "backup-latest.json").read_text())
    assert report["status"] == "failed" and report["recovery"] == "complete"


def test_failed_restart_remains_observable_and_can_be_recovered(config):
    host = FakeHost(config)
    host.start_fail = True
    with pytest.raises(RuntimeError):
        ops.coordinated_backup(config, host, execute=True)
    assert host.events == ["stop", "start"]
    assert not host.gate()["maintenance"]
    assert (Path(config["reportRoot"]) / "recovery.json").is_file()
    report = ops.monitor(config, host)
    assert {item["code"] for item in report["alerts"]} >= {"service_recovery_pending", "last_backup_attempt_failed"}
    host.start_fail = False
    assert ops.recover(config, host)["status"] == "recovered"
    latest = json.loads((Path(config["reportRoot"]) / "backup-latest.json").read_text())
    assert latest["status"] == "completed" and latest["recovery"] == "complete"
    assert latest["previousFailure"] == "service_recovery_failed"
    assert latest["attemptStartedAt"] < latest["recoveryCompletedAt"]
    assert ops.recover(config, host)["status"] == "no_recovery_needed"


@pytest.mark.parametrize("damage", ["copy-failed", "corrupt-backup"])
def test_recovered_api_never_promotes_missing_or_corrupt_backup(config, monkeypatch, damage):
    host = FakeHost(config)
    host.start_fail = True
    with monkeypatch.context() as change:
        if damage == "copy-failed":
            def fail(*args, **kwargs):
                raise OSError("simulated backup copy failure")
            change.setattr(ops.commercial_backup, "backup", fail)
        with pytest.raises(RuntimeError):
            ops.coordinated_backup(config, host, execute=True)
    latest_path = Path(config["reportRoot"]) / "backup-latest.json"
    previous = json.loads(latest_path.read_text())
    if damage == "corrupt-backup":
        (Path(config["backupRoot"]) / previous["backup"] / "snapshot/data/cad-agent/original.dwg").write_bytes(b"tampered")
    host.start_fail = False
    assert ops.recover(config, host)["status"] == "recovered"
    latest = json.loads(latest_path.read_text())
    assert latest["status"] == "failed" and latest["recovery"] == "complete"
    assert latest["reason"] == "backup_failed"


def test_recovery_supports_pre_operation_id_report_but_never_overwrites_another_run(config):
    host = FakeHost(config)
    host.start_fail = True
    with pytest.raises(RuntimeError):
        ops.coordinated_backup(config, host, execute=True)
    path = Path(config["reportRoot"]) / "backup-latest.json"
    previous = json.loads(path.read_text())
    previous.pop("operationId")
    ops.atomic_json(path, previous)
    host.start_fail = False
    ops.recover(config, host)
    assert json.loads(path.read_text())["status"] == "completed"
    different = {"format": ops.FORMAT, "status": "critical", "recovery": "pending", "operationId": "another-operation"}
    ops.atomic_json(path, different)
    ops.atomic_json(Path(config["reportRoot"]) / "recovery.json", {"format": ops.FORMAT, "deployment": config["root"], "token": "our-current-operation", "stopRequested": True})
    ops.recover(config, host)
    assert json.loads(path.read_text()) == different


def test_active_backup_is_maintenance_not_an_abandoned_recovery(config):
    host = FakeHost(config)
    host.state = "exited"
    reports = Path(config["reportRoot"])
    ops.atomic_json(reports / "recovery.json", {"format": ops.FORMAT, "deployment": config["root"], "token": "ours", "stopRequested": True})
    with ops.exclusive(reports / "action.lock"):
        report = ops.monitor(config, host)
    codes = {item["code"] for item in report["alerts"]}
    assert report["checks"]["backupInProgress"] is True
    assert "maintenance_active" in codes
    assert "service_recovery_pending" not in codes and "api_unhealthy" not in codes


def test_crash_journal_recovers_without_removing_another_owners_marker(config):
    host = FakeHost(config)
    reports = Path(config["reportRoot"])
    ops.atomic_json(reports / "recovery.json", {"format": ops.FORMAT, "deployment": config["root"], "token": "ours", "stopRequested": True})
    marker = Path(config["root"]) / "data/operations/maintenance.json"
    marker.write_text(json.dumps({"token": "another-operator"}))
    with pytest.raises(ValueError):
        ops.recover(config, host)
    assert marker.exists() and host.events == []
    marker.write_text(json.dumps({"token": "ours"}))
    assert ops.recover(config, host)["serviceRestarted"] is True
    assert host.events == ["start", "ready"] and not marker.exists()


@pytest.mark.parametrize("execute,enabled", [(False, True), (True, False), (False, False)])
def test_execution_and_configuration_both_required(config, execute, enabled):
    host = FakeHost(config)
    config["backupEnabled"] = enabled
    assert ops.coordinated_backup(config, host, execute=execute)["status"] == "planned"
    assert host.events == [] and not host.gate()["maintenance"]


def test_insufficient_disk_and_unknown_writer_fail_before_stop(config, monkeypatch):
    host = FakeHost(config)
    monkeypatch.setattr(ops, "disk_report", lambda path: {"freeBytes": 1})
    with pytest.raises(ValueError, match="capacity"):
        ops.coordinated_backup(config, host, execute=True)
    assert host.events == []
    host.extra_services = [{"service": "other-worker", "state": "running", "health": ""}]
    with pytest.raises(ValueError, match="topology"):
        ops.coordinated_backup(config, host, execute=True)


def test_second_attempt_inside_interval_never_stops_service(config):
    ops.coordinated_backup(config, FakeHost(config), execute=True)
    config["backupIntervalHours"] = 24
    host = FakeHost(config)
    assert ops.coordinated_backup(config, host, execute=True)["reason"] == "backup_not_due"
    assert host.events == []


def make_old_sets(config, count=3):
    sets = []
    for index in range(count):
        result = ops.coordinated_backup(config, FakeHost(config), execute=True)
        path = Path(config["backupRoot"]) / result["backup"]
        marker = json.loads((path / "managed-backup.json").read_text())
        marker["createdAt"] = (datetime.now(timezone.utc) - timedelta(days=10 + index)).isoformat()
        ops.atomic_json(path / "managed-backup.json", marker)
        sets.append(path)
    return sets


def test_retention_is_default_off_explicit_dry_run_and_only_managed_verified_sets(config):
    sets = make_old_sets(config)
    manual = Path(config["backupRoot"]) / "pre-commercial-manual"
    manual.mkdir()
    (manual / "original.dwg").write_text("do not delete")
    unmarked = Path(config["backupRoot"]) / "scheduled-20200101T010101Z-12345678"
    unmarked.mkdir()
    (unmarked / "customer.dwg").write_text("not a tool backup")
    assert ops.retention(config)["candidates"] == []
    with pytest.raises(ValueError):
        ops.retention(config, apply=True)
    config["retentionKeep"] = 1
    plan = ops.retention(config)
    assert len(plan["candidates"]) == 2 and all(path.exists() for path in sets)
    result = ops.retention(config, apply=True)
    assert len(result["deleted"]) == 2 and sets[0].exists()
    assert manual.exists() and unmarked.exists()
    assert (Path(config["root"]) / "data/cad-agent/original.dwg").is_file()


@pytest.mark.parametrize("damage", ["tamper", "symlink", "extra"])
def test_retention_validates_all_candidates_before_any_deletion(config, tmp_path, damage):
    sets = make_old_sets(config)
    config["retentionKeep"] = 1
    target = sets[-1] / "snapshot/data/cad-agent/original.dwg"
    if damage == "tamper":
        target.write_text("changed")
    elif damage == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "customer-file")
    else:
        (sets[-1] / "customer-extra.dwg").write_text("preserve")
    with pytest.raises(ValueError):
        ops.retention(config, apply=True)
    assert all(path.exists() for path in sets)


def test_monitor_reports_real_thresholds_and_safe_aggregate_contract(config, monkeypatch):
    now = ops.stamp()
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    sql(config, "INSERT INTO cad_job_attempts VALUES ('PRIVATE_CUSTOMER_ID','queued',?,?)", (old, now))
    for _ in range(3):
        sql(config, "INSERT INTO cad_provider_calls VALUES (?,?,?)", (now, old, '{"outcome":"failed","secret":"DO_NOT_REPORT"}'))
    host = FakeHost(config)
    monkeypatch.setattr(ops, "disk_report", lambda path: {"usedPercent": 95, "freeBytes": 10, "totalBytes": 100})
    report = ops.monitor(config, host)
    assert report["status"] == "critical"
    codes = {alert["code"] for alert in report["alerts"]}
    assert codes >= {"dataDisk_critical", "backupDisk_critical", "oldestQueuedSeconds_threshold", "providerFailuresLastHour_threshold", "backup_missing_or_stale"}
    assert report["notifications"] == {"configured": False, "delivery": "local_report_only"}
    assert report["offsiteBackup"]["verified"] is False
    rendered = (Path(config["reportRoot"]) / "status.json").read_text()
    assert "PRIVATE_CUSTOMER_ID" not in rendered and "DO_NOT_REPORT" not in rendered
    assert json.loads(rendered) == report


def test_database_read_failure_is_critical_and_backup_fails_closed(config):
    Path(config["root"], "data/cad-agent/runs.sqlite3").unlink()
    host = FakeHost(config)
    assert "job_journal_unreadable" in {row["code"] for row in ops.monitor(config, host)["alerts"]}
    with pytest.raises(FileNotFoundError):
        ops.coordinated_backup(config, host, execute=True)
    assert host.events == []


@pytest.mark.parametrize("change", [
    {"backupRoot": "data/backups"}, {"minimumFreeBytes": -1}, {"backupEnabled": "true"},
    {"diskWarningPercent": 95}, {"baseUrl": "http://external.invalid"}, {"retentionMinAgeDays": 0}])
def test_invalid_configuration_rejected(config, tmp_path, change):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({**config, **change}))
    with pytest.raises(ValueError):
        ops.configuration(path)


def test_backup_and_report_roots_cannot_overlap_customer_tree(config, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({**config, "backupRoot": str(Path(config["root"]) / "data/backups")}))
    with pytest.raises(ValueError):
        ops.configuration(path)


@pytest.mark.parametrize("command", ["recover", "retention", "backup"])
def test_real_cli_action_lock_contention_exits_successfully_without_false_failure(config, tmp_path, command):
    configuration = tmp_path / "cli-config.json"
    configuration.write_text(json.dumps(config))
    reports = Path(config["reportRoot"])
    journal = reports / "recovery.json"
    expected = {"format": ops.FORMAT, "deployment": config["root"], "token": "existing-operation", "stopRequested": True}
    ops.atomic_json(journal, expected)
    with ops.exclusive(reports / "action.lock"):
        result = subprocess.run([sys.executable, str(DEPLOY / "commercial_operations.py"), command,
                                 "--config", str(configuration), "--execute"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "skipped" and report["reason"] == "operation_in_progress"
    assert json.loads(journal.read_text()) == expected
    assert not list(reports.glob("*-failure.json"))


def test_io_failure_after_action_lock_acquisition_is_still_reported(config, tmp_path, monkeypatch, capsys):
    configuration = tmp_path / "cli-config.json"
    configuration.write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["commercial_operations.py", "recover", "--config", str(configuration)])
    monkeypatch.setattr(ops.signal, "signal", lambda *args: None)
    def broken(*args):
        raise BlockingIOError("actual simulated recovery I/O failure")
    monkeypatch.setattr(ops, "recover", broken)
    with pytest.raises(SystemExit) as raised:
        ops.main()
    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert (Path(config["reportRoot"]) / "recover-failure.json").exists()
