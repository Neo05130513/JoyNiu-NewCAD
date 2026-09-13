"""Strict safe projection of the host monitor's protected read-only report."""
from datetime import datetime, timezone
import json
import math
import os
import stat


ALERTS = {
    "api_unhealthy": "API 服务未达到健康状态。", "web_unhealthy": "网页服务未正常运行。",
    "service_check_failed": "无法读取宿主服务状态。", "dataDisk_critical": "宿主数据磁盘已达到严重阈值。",
    "dataDisk_warning": "宿主数据磁盘已达到预警阈值。", "dataDisk_unavailable": "无法读取宿主数据磁盘。",
    "backupDisk_critical": "备份磁盘可用空间已达到严重阈值。", "backupDisk_warning": "备份磁盘已达到预警阈值。",
    "backupDisk_unavailable": "无法读取备份磁盘。", "queued_threshold": "排队任务数达到预警阈值。",
    "oldestQueuedSeconds_threshold": "有任务排队时间超过预警阈值。",
    "oldestRunningSeconds_threshold": "有任务长时间未更新运行状态。",
    "oldestProviderSeconds_threshold": "有上游调用超过运行时间阈值。",
    "providerFailuresLastHour_threshold": "最近一小时上游失败次数达到预警阈值。",
    "job_journal_unreadable": "无法读取任务与调用记录。", "maintenance_active": "协调备份维护正在进行。",
    "admission_gate_unavailable": "协调备份准入保护未连接。", "backup_missing_or_stale": "缺少可验证的近期自动备份。",
    "backup_verification_failed": "最近备份未通过完整校验。", "service_recovery_pending": "备份后的服务恢复仍待处理。",
    "last_backup_attempt_failed": "最近一次备份尝试失败。", "backup_attempt_report_unreadable": "无法读取备份执行报告。",
}


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**18 else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def read_operations_report():
    path = os.getenv("JOYNIU_OPERATIONS_REPORT")
    unavailable = {"available": False, "configured": bool(path), "stale": False,
                   "message": "尚未接入宿主持续监控；当前页面仍可查看业务数据库快照。"}
    if not path:
        return {"monitoring": unavailable, "alerts": [], "checks": {}}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Expected a regular report file")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("Report too large")
        report = json.loads(raw)
        if not isinstance(report, dict) or report.get("format") != "joyniu-operations-v1":
            raise ValueError("Unsupported report")
        generated = timestamp(report.get("generatedAt"))
        if generated is None:
            raise ValueError("Missing timestamp")
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        if age < -60:
            raise ValueError("Future timestamp")
        stale = age > 900
        monitoring = {"available": not stale, "configured": True, "stale": stale,
                      "generatedAt": generated.isoformat(), "ageSeconds": max(0, int(age)),
                      "status": "stale" if stale else report.get("status") if report.get("status") in {"ok", "warning", "critical"} else "unknown",
                      "message": "宿主监控报告已超过 15 分钟，请检查定时任务。" if stale else "已接入宿主定时检查；告警保存到本地并显示于后台，外部通知尚未配置。"}
        alerts = []
        for item in report.get("alerts", []) if isinstance(report.get("alerts"), list) else []:
            item = mapping(item)
            if item.get("code") in ALERTS:
                alerts.append({"code": "host_" + item["code"], "severity": "critical" if item.get("severity") == "critical" else "warning",
                               "count": None, "message": ALERTS[item["code"]]})
        if stale:
            alerts.append({"code": "host_monitor_stale", "severity": "warning", "count": None, "message": monitoring["message"]})
        source = mapping(report.get("checks"))
        checks = {}
        for key in ("dataDisk", "backupDisk"):
            value = mapping(source.get(key))
            checks[key] = {field: numeric(value.get(field)) for field in ("totalBytes", "freeBytes", "usedPercent")}
        backup = mapping(source.get("backup"))
        checks["backup"] = {"automaticEnabled": backup.get("automaticEnabled") is True,
                            "managedSets": numeric(backup.get("managedSets")), "verificationFailed": backup.get("verificationFailed") is True}
        for field in ("lastBackupAt", "lastVerifiedAt"):
            parsed = timestamp(backup.get(field))
            checks["backup"][field] = parsed.isoformat() if parsed else None
        checks["services"] = [{"service": row["service"], "state": row.get("state") if row.get("state") in {"running", "exited", "stopped", "restarting", "created", "paused", "dead"} else "unknown",
                               "health": row.get("health") if row.get("health") in {"healthy", "unhealthy", "starting", ""} else "unknown"}
                              for row in source.get("services", []) if isinstance(row, dict) and row.get("service") in {"api", "web", "cad-egress"}] if isinstance(source.get("services"), list) else []
        jobs = mapping(source.get("jobs"))
        checks["jobs"] = {field: numeric(jobs.get(field)) for field in ("queued", "running", "cancelRequested", "runningOperations", "unfinishedProviderCalls", "oldestQueuedSeconds", "oldestRunningSeconds", "oldestProviderSeconds", "providerFailuresLastHour")}
        return {"monitoring": monitoring, "alerts": alerts, "checks": checks}
    except (OSError, ValueError, TypeError, KeyError):
        unavailable["message"] = "宿主监控报告缺失、不可读取或格式无效，请检查定时任务与只读挂载。"
        return {"monitoring": unavailable, "alerts": [{"code": "host_monitor_unreadable", "severity": "warning", "count": None, "message": unavailable["message"]}], "checks": {}}
