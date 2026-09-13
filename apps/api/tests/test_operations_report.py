from datetime import datetime, timedelta, timezone
import json

import pytest

from app.operations_report import read_operations_report
from .test_admin_operations import setup


@pytest.fixture
def host_report(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setenv("JOYNIU_OPERATIONS_REPORT", str(path))
    now = datetime.now(timezone.utc).isoformat()
    report = {"format": "joyniu-operations-v1", "generatedAt": now, "status": "warning",
              "checks": {"backup": {"automaticEnabled": True, "managedSets": 3, "lastBackupAt": now, "lastVerifiedAt": now},
                         "dataDisk": {"totalBytes": 100, "freeBytes": 17, "usedPercent": 83},
                         "services": [{"service": "api", "state": "running", "health": "healthy"}],
                         "jobs": {"providerFailuresLastHour": 3}},
              "alerts": [{"code": "dataDisk_warning", "severity": "warning"}],
              "notifications": {"configured": True}, "offsiteBackup": {"configured": True}}
    path.write_text(json.dumps(report))
    return path, report


def test_report_fields_are_allowlisted_and_never_expose_host_secrets(host_report):
    path, report = host_report
    secret = "PRIVATE_TOKEN_SOURCE_PATH_CUSTOMER_PROMPT"
    report["message"] = report["checks"]["backup"]["path"] = secret
    report["checks"]["jobs"]["owner"] = secret
    report["alerts"][0]["message"] = secret
    report["alerts"].append({"code": secret, "severity": "critical"})
    report["checks"]["services"].append({"service": secret, "state": secret})
    path.write_text(json.dumps(report))
    value = read_operations_report()
    assert value["monitoring"]["available"] is True
    assert value["checks"]["dataDisk"]["usedPercent"] == 83
    assert secret not in json.dumps(value)
    assert [item["code"] for item in value["alerts"]] == ["host_dataDisk_warning"]


def test_report_over_fifteen_minutes_is_explicitly_stale(host_report):
    path, report = host_report
    report["generatedAt"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    path.write_text(json.dumps(report))
    value = read_operations_report()
    assert value["monitoring"]["available"] is False
    assert value["monitoring"]["stale"] is True
    assert value["checks"]["dataDisk"]["usedPercent"] == 83
    assert "host_monitor_stale" in {item["code"] for item in value["alerts"]}


@pytest.mark.parametrize("kind", ["missing", "invalid", "oversized", "future", "symlink", "wrong-format"])
def test_missing_or_invalid_monitor_report_never_becomes_live(host_report, tmp_path, kind):
    path, report = host_report
    if kind == "missing":
        path.unlink()
    elif kind == "invalid":
        path.write_text("PRIVATE INVALID JSON")
    elif kind == "oversized":
        path.write_text(" " * 65537)
    elif kind == "future":
        report["generatedAt"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(report))
    elif kind == "symlink":
        target = tmp_path / "secret.json"
        target.write_text(json.dumps(report))
        path.unlink()
        path.symlink_to(target)
    else:
        report["format"] = "other"
        path.write_text(json.dumps(report))
    value = read_operations_report()
    assert value["monitoring"]["available"] is False
    assert "PRIVATE" not in json.dumps(value)


def test_existing_admin_system_and_overview_use_local_report_without_promoting_external_services(setup, host_report):
    env = setup
    value = env.ops.system(env.users["admin"].id)
    assert value["monitoring"]["available"] is True
    assert value["backup"]["available"] is True
    assert value["backup"]["automaticEnabled"] is True
    assert value["externalNotifications"]["available"] is False
    assert value["offsiteBackup"]["available"] is False
    assert value["hostOperations"]["dataDisk"]["usedPercent"] == 83
    assert "monitoring_unconfigured" not in {item["code"] for item in value["alerts"]}
    assert env.ops.overview(env.users["ops"].id)["monitoring"]["available"] is True
