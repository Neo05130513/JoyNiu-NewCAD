#!/usr/bin/env python3
"""Read existing pilot evidence and round-trip saved STEP files without AI calls.

This verifies local file integrity and OCCT exchange only. It neither confirms
source-to-model geometry nor changes task state or grants customer acceptance.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

import cadquery as cq
import OCP


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shape_metrics(shape):
    box = shape.BoundingBox()
    return {"valid": bool(shape.isValid()), "solids": len(shape.Solids()), "volume": shape.Volume(),
            "bounds": [box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax],
            "size": [box.xlen, box.ylen, box.zlen]}


def verify(root):
    selection = json.loads((root / "selection.json").read_text())
    if not 1 <= len(selection["samples"]) <= 100:
        raise ValueError("Expected 1–100 preselected sources")
    sources, artifacts, statuses = [], [], Counter()
    for sample in selection["samples"]:
        source = Path(sample["path"])
        actual = sha256(source) if source.is_file() else None
        sources.append({"sampleId": sample["id"], "expectedSha256": sample["sha256"], "actualSha256": actual, "unchanged": actual == sample["sha256"]})
        summary_path = root / "runs" / sample["id"] / "summary.json"
        if not summary_path.is_file():
            statuses["missing_summary"] += 1
            continue
        summary = json.loads(summary_path.read_text())
        statuses[summary.get("status", "unknown")] += 1
        descriptor = (summary.get("artifacts") or {}).get("step")
        if not descriptor:
            continue
        path = Path(descriptor["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("Saved STEP must be within the pilot directory and below 100 MB")
        item = {"sampleId": sample["id"], "taskStatus": summary.get("status"), "humanVerified": summary.get("human_verified") is True,
                "stepPath": str(path.relative_to(root.resolve())), "stepSha256": sha256(path), "stepBytes": path.stat().st_size}
        try:
            original = cq.importers.importStep(str(path)).val()
            before = shape_metrics(original)
            with tempfile.TemporaryDirectory(prefix="joyniu-step-roundtrip-") as temporary:
                exported = Path(temporary) / "roundtrip.step"
                cq.exporters.export(original, str(exported), exportType="STEP")
                after = shape_metrics(cq.importers.importStep(str(exported)).val())
            bounds_error = max(abs(a - b) for a, b in zip(before["bounds"], after["bounds"]))
            volume_error = abs(before["volume"] - after["volume"])
            passed = before["valid"] and after["valid"] and before["solids"] > 0 and before["solids"] == after["solids"] and before["volume"] > 0 and bounds_error <= 1e-6 and volume_error <= max(1e-6, before["volume"] * 1e-9)
            item.update(before=before, after=after, maxBoundsError=bounds_error, volumeError=volume_error, passed=passed)
        except Exception as error:
            item.update(passed=False, errorType=type(error).__name__)
        item["sourceStepUnchanged"] = sha256(path) == item["stepSha256"]
        artifacts.append(item)
    return {"checkedAt": datetime.now(timezone.utc).isoformat(), "mode": "offline-existing-artifacts-only", "modelCalls": 0,
            "engines": {"CadQuery": cq.__version__, "OCP": OCP.__version__},
            "scope": "Source hash integrity and local STEP import/export/import. Does not assert drawing equivalence, customer CAD compatibility or delivery acceptance.",
            "statuses": dict(statuses), "sources": sources, "artifacts": artifacts,
            "allLocalChecksPassed": all(item["unchanged"] for item in sources) and bool(artifacts) and all(item["passed"] and item["sourceStepUnchanged"] for item in artifacts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Do not overwrite a previous report or any customer source by accident.
    if args.output.exists():
        parser.error("output already exists; select a new report filename")
    report = verify(args.pilot_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps({"sourcesChecked": len(report["sources"]), "stepArtifactsChecked": len(report["artifacts"]), "allLocalChecksPassed": report["allLocalChecksPassed"], "statuses": report["statuses"]}, ensure_ascii=False))
    return 0 if report["allLocalChecksPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
