"""Run a frozen drawing manifest with bounded concurrency and resumable output.

Uses the production CAD service through cad_cost_probe.py. Does not answer
questions, repair failures by hand, or retry an entire drawing automatically.
Account quota snapshots are collected separately through the Codex app tool.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--exclude", nargs="*", default=[])
    parser.add_argument("--wait-existing", nargs="*", default=[], help="Explicit IDs already running outside this batch")
    args = parser.parse_args()
    manifest = read(args.selection)
    samples = manifest["samples"]
    if len({sample["id"] for sample in samples}) != len(samples):
        parser.error("sample IDs must be unique")
    for sample in samples:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sample["id"]):
            parser.error("unsafe sample ID")
        actual = hashlib.sha256(Path(sample["path"]).read_bytes()).hexdigest()
        if actual != sample["sha256"]:
            parser.error("source drawing hash changed: " + sample["id"])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "logs").mkdir(exist_ok=True)
    probe = Path(__file__).with_name("cad_cost_probe.py")
    def run(sample):
        identity = sample["id"]
        target = args.output / "runs" / identity
        existing = target / "summary.json"
        if existing.is_file():
            return {"id": identity, "skipped_existing": True, **read(existing)}
        if identity in args.wait_existing:
            deadline = time.monotonic() + args.timeout + 220
            while time.monotonic() < deadline:
                if existing.is_file():
                    result = read(existing)
                    print(json.dumps({"finished_existing": identity, "status": result.get("status")}), flush=True)
                    return {"id": identity, **result}
                time.sleep(2)
            return {"id": identity, "status": "existing_probe_not_finished", "usage_capture_complete": False}
        if target.exists() and any(target.iterdir()):
            return {"id": identity, "status": "incomplete_existing_requires_review"}
        if (args.output / "STOP").exists():
            return {"id": identity, "status": "not_started_stop_requested"}
        print(json.dumps({"started": identity, "complexity": sample["complexity"],
                          "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False), flush=True)
        command = [sys.executable, str(probe), "--drawing", sample["path"],
                   "--output", str(target), "--timeout", str(args.timeout), "--max-turns", "20"]
        with (args.output / "logs" / (identity + ".log")).open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=args.timeout + 220)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return {"id": identity, "status": "probe_hard_timeout", "usage_capture_complete": False}
        result = read(existing) if existing.is_file() else {"status": "probe_process_error", "return_code": code}
        compact = {"id": identity, **{k: result.get(k) for k in
                   ("status", "elapsed_seconds", "provider_calls", "geometry_generated", "pending_provider_calls")}}
        print(json.dumps({"finished": compact}, ensure_ascii=False), flush=True)
        return {"id": identity, **result}
    chosen = [sample for sample in samples if sample["id"] not in args.exclude]
    chosen.sort(key=lambda sample: 0 if sample["id"] in args.wait_existing else 1)
    results = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, sample) for sample in chosen]
        for future in as_completed(futures):
            results.append(future.result())
            (args.output / "batch-progress.json").write_text(json.dumps({
                "selected_count": len(samples), "batch_count": len(chosen), "completed_count": len(results),
                "elapsed_seconds": round(time.monotonic() - started, 3), "workers": args.workers,
                "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"batch_complete": True, "count": len(results)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
