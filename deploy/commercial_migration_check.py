#!/usr/bin/env python3
"""Run inside the new API image against an isolated restored data directory.

No server/lifespan or supplier call is started. Output contains counts only.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys


def main():
    assert len(sys.argv) == 2, "Supply the verified backup manifest"
    baseline = json.loads(Path(sys.argv[1]).read_text())
    assert baseline.get("format") == "joyniu-commercial-offline-v1" and baseline.get("quiescent") is True
    for key in ("data/joyniu.sqlite3", "data/cad-agent/runs.sqlite3"):
        assert isinstance(baseline.get("databases", {}).get(key), dict) and baseline["databases"][key], f"Missing original table counts: {key}"
    from app import main as application

    assert Path(application.__file__).resolve() == Path("/opt/joyniu-api/app/main.py")
    assert application.platform_services is not None, "Platform initialization failed"
    billing = application.app.state.billing_service.status()
    assert billing["enabled"] is False and billing["purchaseEnabled"] is False
    assert billing["refundExecutionEnabled"] is False
    assert application.app.state.commercial_terms.ready() is False
    assert application.app.state.billing_policy.status()["enabled"] is False
    paths = {
        "data/joyniu.sqlite3": Path(os.environ["JOYNIU_DB"]),
        "data/cad-agent/runs.sqlite3": Path(os.environ["JOYNIU_CAD_AGENT_DIR"]) / "runs.sqlite3",
    }
    report = {}
    for relative, path in paths.items():
        with sqlite3.connect(path) as database:
            assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            before = baseline["databases"][relative]
            for table, count in before.items():
                actual = database.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                assert actual == count, f"Existing row count changed: {relative}:{table}"
            report[relative] = {"integrity": "ok", "preservedTables": len(before), "totalTables": len(tables)}
    import cryptography
    print(json.dumps({"migration": report, "charging": False, "purchase": False,
                      "refunds": False, "cryptography": cryptography.__version__,
                      "loadedApplication": str(Path(application.__file__).resolve())}))


if __name__ == "__main__":
    main()
