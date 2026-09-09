#!/usr/bin/env python3
"""HELD-OUT ACCEPTANCE ONLY: independently audit a delivered STEP for 10.jpg.

Never import this script into the production Agent or send its expected values,
reference plan, generated reference or audit feedback as blind model input.
Input may be an agent-state.json with artifacts.step.path, or a local STEP.
Only an XY-centre/Z-bottom translation is applied. Axes are never guessed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys
from typing import Any

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app.cad_executor import build_plan_shape
from app.cad_inspector import export_projections, inspect_shape
from tests.test_cad_executor import clevis_drawing_plan


def source_step(input_path: Path, override: Path | None = None) -> tuple[Path, dict[str, Any]]:
    if override is not None:
        return override.resolve(), {"kind": "explicit_step_override", "input": str(input_path.resolve())}
    if input_path.suffix.lower() in {".step", ".stp"}:
        return input_path.resolve(), {"kind": "step"}
    record = json.loads(input_path.read_text())
    descriptor = (record.get("artifacts") or {}).get("step") or {}
    path = descriptor.get("path") if isinstance(descriptor, dict) else None
    if not isinstance(path, str) or not path.strip() or "://" in path:
        raise ValueError("Input state has no delivered local STEP path. A saved plan or AI completion claim is not geometry.")
    value = Path(path)
    if not value.is_absolute():
        value = input_path.parent / value
    return value.resolve(), {"kind": "agent_state", "input": str(input_path.resolve()),
                             "declaredStatus": record.get("status"), "runId": record.get("runId")}


def close_values(actual: Any, expected: Any, tolerance: float) -> bool:
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(close_values(a, e, tolerance) for a, e in zip(actual, expected))
    return type(actual) in (int, float) and math.isfinite(actual) and abs(actual-expected) <= tolerance


def cylinder_axes(inspection: dict[str, Any], diameter: float, axis: int, tolerance: float) -> list[dict[str, Any]]:
    """Group cylindrical surfaces by physical axis, not B-Rep seam/face count."""
    found = []
    for cylinder in inspection["cylinders"]:
        direction = cylinder["axis"]
        if abs(abs(direction[axis])-1) > 1e-6 or abs(cylinder["diameter"]-diameter) > tolerance:
            continue
        center = [float(value) if index != axis else 0.0 for index, value in enumerate(cylinder["origin"])]
        if any(math.dist(center, item["centerPerpendicularToAxis"]) <= tolerance for item in found):
            continue
        found.append({"diameter": cylinder["diameter"], "axis": direction, "centerPerpendicularToAxis": center})
    return sorted(found, key=lambda item: tuple(item["centerPerpendicularToAxis"]))


def audit_step(step: Path, output_dir: Path, *, tolerance_mm: float = 0.02,
               difference_tolerance_mm3: float = 0.05) -> dict[str, Any]:
    from cadquery import exporters, importers

    if not step.is_file():
        raise ValueError("Delivered STEP does not exist")
    if not 0 < tolerance_mm <= 0.1 or not 0 <= difference_tolerance_mm3 <= 1:
        raise ValueError("Audit tolerance is outside the accepted numerical range")
    output_dir.mkdir(parents=True, exist_ok=True)
    delivered = importers.importStep(str(step))  # ignore all submitted plan/inspection claims
    original = inspect_shape(delivered)
    bounds = original["bbox"]
    shift = [-(bounds["min"][0]+bounds["max"][0])/2,
             -(bounds["min"][1]+bounds["max"][1])/2, -bounds["min"][2]]
    normalized = delivered.translate(tuple(shift))
    bridge_z = math.sqrt(559)
    probes_with_expected = [
        ("bridgeCenter", [0, 0, 0], [0, 0, 1], -1, 60, [[16, bridge_z]], "中央桥材料高度：内R16至sqrt(28²−15²)"),
        ("bridgeOffset", [8, 0, 0], [0, 0, 1], -1, 60, [[math.sqrt(16**2-8**2), bridge_z]], "偏心桥截面仍位于同一内圆弧与桥平面之间"),
        ("outerArchSide", [20, 20, 0], [0, 0, 1], -1, 60, [[0, math.sqrt(28**2-20**2)]], "外R28的实际材料截面"),
        ("clevisHoleAxis", [0, 0, 40], [0, 1, 0], -30, 30, [], "孔中心Z40，沿Y穿过两耳及中间间隙"),
        ("earWallsRight", [11, 0, 40], [0, 1, 0], -30, 30, [[-25, -15], [15, 25]], "两耳厚10、间隙30、总宽50"),
        ("earWallsLeft", [-11, 0, 40], [0, 1, 0], -30, 30, [[-25, -15], [15, 25]], "另一侧同样的10–30–10材料分布"),
        ("frontEarVertical", [0, 20, 0], [0, 0, 1], -1, 60, [[16, 33.5], [46.5, 55]], "前耳Ø13孔、中心高40、上耳外R15及内拱开口"),
        ("backEarVertical", [0, -20, 0], [0, 0, 1], -1, 60, [[16, 33.5], [46.5, 55]], "后耳同样的实际孔段与顶部高度"),
        ("frontEarHorizontal", [0, 20, 40], [1, 0, 0], -30, 30, [[-15, -6.5], [6.5, 15]], "上耳横截面外R15、内孔Ø13"),
        ("leftMountHole", [-40, 0, 0], [0, 0, 1], -1, 60, [], "左安装孔轴X=-40，沿Z贯通"),
        ("rightMountHole", [40, 0, 0], [0, 0, 1], -1, 60, [], "右安装孔轴X=40，沿Z贯通"),
        ("leftFootThickness", [-40, 10, 0], [0, 0, 1], -1, 60, [[0, 9]], "左安装耳只具有9mm底厚"),
        ("rightFootThickness", [40, 10, 0], [0, 0, 1], -1, 60, [[0, 9]], "右安装耳只具有9mm底厚"),
    ]
    probes = [{"id": identifier, "origin": origin, "direction": direction, "start": start, "end": end}
              for identifier, origin, direction, start, end, _, _ in probes_with_expected]
    measured = inspect_shape(normalized, ray_probes=probes)
    checks: list[dict[str, Any]] = []

    def check(identifier, label, actual, expected, passed, tolerance=tolerance_mm):
        checks.append({"id": identifier, "label": label, "actual": actual, "expected": expected,
                       "passed": bool(passed), "status": "passed" if passed else "failed", "tolerance": tolerance})

    check("validBRep", "重新导入STEP的B-Rep有效", measured["valid"], True, measured["valid"], None)
    check("solidCount", "单个连通实体", measured["solidCount"], 1, measured["solidCount"] == 1, None)
    expected_bbox = [110, 50, 55]
    size = measured["bbox"]["size"]
    check("overallSize", "完整包络X/Y/Z", size, expected_bbox, close_values(size, expected_bbox, tolerance_mm))
    possible_permutations = [list(permutation) for permutation in itertools.permutations(range(3))
                             if list(permutation) != [0, 1, 2] and close_values([size[index] for index in permutation], expected_bbox, tolerance_mm)]
    for identifier, origin, direction, start, end, expected, label in probes_with_expected:
        actual = measured["raySections"][identifier]["intervals"]
        check(identifier, label, actual, expected, close_values(actual, expected, tolerance_mm))
        checks[-1]["probe"] = {"origin": origin, "direction": direction, "start": start, "end": end}

    cylinder_specs = [
        ("outerArchCylinder", 56, 1, [[0, 0, 0]], "外拱R28，轴Y，圆心基准Z0"),
        ("innerArchCylinder", 32, 1, [[0, 0, 0]], "内拱R16，轴Y，与外拱同心"),
        ("earOuterCylinder", 30, 1, [[0, 0, 40]], "上耳R15，轴Y，孔心至底面40"),
        ("earHoleCylinder", 13, 1, [[0, 0, 40]], "两耳同轴Ø13孔；孔段数另由射线验证"),
        ("mountEarCylinders", 30, 2, [[-40, 0, 0], [40, 0, 0]], "两安装耳R15，轴Z"),
        ("mountHoleCylinders", 13, 2, [[-40, 0, 0], [40, 0, 0]], "两Ø13安装孔，轴Z且轴线距80"),
    ]
    for identifier, diameter, axis, centers, label in cylinder_specs:
        axes = cylinder_axes(measured, diameter, axis, tolerance_mm)
        actual_centers = [item["centerPerpendicularToAxis"] for item in axes]
        check(identifier, label, axes, {"diameter": diameter, "axisIndex": axis, "centers": centers},
              close_values(actual_centers, centers, tolerance_mm))
    mounting_axes = cylinder_axes(measured, 13, 2, tolerance_mm)
    pitch = math.dist(mounting_axes[0]["centerPerpendicularToAxis"], mounting_axes[1]["centerPerpendicularToAxis"]) if len(mounting_axes) == 2 else None
    check("mountPitch", "实测两安装孔轴线距离", pitch, 80, close_values(pitch, 80, tolerance_mm))

    reference, _, _ = build_plan_shape(clevis_drawing_plan())
    reference_measurements = inspect_shape(reference)
    reference_box = reference_measurements["bbox"]
    reference = reference.translate((-(reference_box["min"][0]+reference_box["max"][0])/2,
                                     -(reference_box["min"][1]+reference_box["max"][1])/2, -reference_box["min"][2]))
    try:
        extra = normalized.cut(reference).val()
        missing = reference.cut(normalized).val()
        extra_volume, missing_volume = abs(float(extra.Volume())), abs(float(missing.Volume()))
        difference = extra_volume + missing_volume
        boolean = {"status": "completed", "extraVolumeMm3": extra_volume, "missingVolumeMm3": missing_volume,
                   "symmetricDifferenceVolumeMm3": difference,
                   "relativeToReferenceVolume": difference/reference_measurements["volumeMm3"]}
        check("referenceSymmetricDifference", "与人工核对通用特征参考体的布尔对称差", difference, 0,
              difference <= difference_tolerance_mm3, difference_tolerance_mm3)
    except Exception as exc:
        boolean = {"status": "failed", "errorType": type(exc).__name__}
        check("referenceSymmetricDifference", "布尔差未完成，不能判定参考体一致", None, 0, False, difference_tolerance_mm3)

    normalized_step = output_dir / "normalized-delivered.step"
    exporters.export(normalized, str(normalized_step), exportType="STEP")
    views = export_projections(normalized, output_dir / "actual-views")
    all_passed = all(item["passed"] for item in checks)
    return {"auditVersion": "heldout-drawing10-v1", "purpose": "Independent acceptance only; never model input",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if all_passed else "failed", "passed": all_passed,
            "sourceStep": {"path": str(step.resolve()), "sha256": hashlib.sha256(step.read_bytes()).hexdigest()},
            "normalization": {"method": "translation_only", "translation": shift,
                              "originalBBox": original["bbox"], "normalizedBBox": measured["bbox"],
                              "axisConvention": "X mounting pitch, Y clevis bore axis, Z from bottom",
                              "axisStatus": "manual_review_required" if possible_permutations else "unchanged",
                              "possibleExpectedToInputAxisPermutations": possible_permutations,
                              "rotatedOrReflected": False},
            "checks": checks, "checkCount": len(checks), "failedCheckIds": [item["id"] for item in checks if not item["passed"]],
            "actualInspection": measured, "referenceComparison": boolean,
            "referenceSource": "tests.test_cad_executor.clevis_drawing_plan (held-out reviewed interpretation of 10.jpg)",
            "referenceVolumeMm3": reference_measurements["volumeMm3"],
            "artifacts": {"normalizedStep": str(normalized_step.resolve()), "views": views},
            "limitations": "No automatic axis permutation; inspect generated views if axes differ. Source/reference interpretation is the prior human-reviewed acceptance basis. Passing this audit is not manufacturing certification."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="agent-state.json or delivered .step/.stp")
    parser.add_argument("--step", type=Path, help="Explicit local STEP when the state has only public URLs")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        step, source = source_step(args.input, args.step)
        result = audit_step(step, args.output_dir)
        result["sourceRecord"] = source
    except Exception as exc:
        result = {"auditVersion": "heldout-drawing10-v1", "status": "failed", "passed": False,
                  "errorType": type(exc).__name__, "message": str(exc), "artifacts": {}}
    report = args.output_dir / "independent-audit.json"
    report.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    print(json.dumps({"status": result["status"], "reportPath": str(report.resolve()),
                      "checkCount": result.get("checkCount", 0), "failedCheckIds": result.get("failedCheckIds", []),
                      "errorType": result.get("errorType"), "message": result.get("message"),
                      "artifacts": result.get("artifacts")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
