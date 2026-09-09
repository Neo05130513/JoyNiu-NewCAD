#!/usr/bin/env python3
"""HELD-OUT ONLY: inspect a delivered STEP against the annotated shaft in 3.jpg.

Never import this module into production or pass its numbers/reference solid
to a model. X is the drawing's left-to-right axis by default. --axis and
--reverse are explicit human coordinate choices, never automatic guesses.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
from app.cad_inspector import export_projections, inspect_shape

# Independent transcription of the source drawing, not provider output.
# The dimension-chain spans include their adjacent relief grooves.
LENGTHS = [20, 17, 25, 80, 32, 25]
TOTAL_LENGTH = sum(LENGTHS)
OUTER_STATIONS = [(0, 20, 10), (20, 35, 15), (35, 37, 13), (37, 62, 18),
                  (62, 142, 26), (142, 144, 14), (144, 174, 15), (174, 199, 10)]
CYLINDER_SPANS = {20: [[0, 20], [174, 199]], 30: [[20, 35], [144, 174]], 26: [[35, 37]],
                  36: [[37, 62]], 52: [[62, 142]], 28: [[142, 144]],
                  16: [[0, 2], [192, 195]], 10: [[2, 192], [195, 199]]}


def source_step(path: Path) -> tuple[Path, dict[str, Any]]:
    if path.suffix.lower() in {".step", ".stp"}:
        return path.resolve(), {"kind": "delivered_step"}
    record = json.loads(path.read_text())
    descriptor = (record.get("artifacts") or {}).get("step") or {}
    value = descriptor.get("path") if isinstance(descriptor, dict) else None
    if not isinstance(value, str) or not value or "://" in value:
        raise ValueError("No delivered local STEP: a saved plan or AI inspection is not an actual artifact")
    resolved = Path(value)
    return (resolved if resolved.is_absolute() else path.parent/resolved).resolve(), {
        "kind": "agent_state", "path": str(path.resolve()), "declaredStatus": record.get("status"), "runId": record.get("runId")}


def close(actual, expected, tolerance):
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(close(a, e, tolerance) for a, e in zip(actual, expected))
    return type(actual) in (int, float) and math.isfinite(actual) and abs(actual-expected) <= tolerance


def merge_spans(spans, tolerance):
    merged = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1]+tolerance:
            merged[-1][1] = max(high, merged[-1][1])
        else:
            merged.append([low, high])
    return merged


def reference_shape(*, omit_internal_groove=False, central_radius=26):
    """Manual ideal nominal interpretation for hold-out control tests only."""
    import cadquery as cq
    axis = cq.Vector(1, 0, 0)
    segments = [cq.Solid.makeCylinder(central_radius if low == 62 else radius, high-low, cq.Vector(low, 0, 0), axis)
                for low, high, radius in OUTER_STATIONS]
    shape = segments[0].fuse(*segments[1:]).clean()
    shape = shape.cut(cq.Solid.makeCylinder(5, TOTAL_LENGTH+2, cq.Vector(-1, 0, 0), axis))
    shape = shape.cut(cq.Solid.makeCylinder(8, 3, cq.Vector(-1, 0, 0), axis))
    if not omit_internal_groove:
        shape = shape.cut(cq.Solid.makeCylinder(8, 3, cq.Vector(192, 0, 0), axis))
    return shape.clean()


def audit_step(step: Path, output_dir: Path, *, axis="X", reverse=False, tolerance_mm=.02):
    import cadquery as cq
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    if not step.is_file() or axis not in "XYZ" or len(axis) != 1 or not 0 < tolerance_mm <= .1:
        raise ValueError("Invalid STEP, explicit axis or numerical tolerance")
    delivered = cq.importers.importStep(str(step))
    original = inspect_shape(delivered)
    axial = "XYZ".index(axis)
    perpendicular = [index for index in range(3) if index != axial]
    bounds = original["bbox"]
    shift = [-(bounds["min"][i]+bounds["max"][i])/2 for i in range(3)]
    shift[axial] = -bounds["min"][axial]
    normalized = delivered.translate(tuple(shift))
    length = bounds["size"][axial]
    direction = [0, 0, 0]
    direction[axial] = -1 if reverse else 1
    def location(station=0, first=0, second=0):
        point = [0.0, 0.0, 0.0]
        point[axial] = length-station if reverse else station
        point[perpendicular[0]], point[perpendicular[1]] = first, second
        return point

    probes, expected_probes = [], []
    radial_stations = [
        ("left_counterbore", 1, 10, 8), ("left_end", 10, 10, 5), ("left_collar", 27, 15, 5),
        ("left_external_groove", 36, 13, 5), ("intermediate_shoulder", 49, 18, 5),
        ("central_drum", 102, 26, 5), ("right_external_groove", 143, 14, 5),
        ("right_collar", 159, 15, 5), ("right_end_before_groove", 184, 10, 5),
        ("right_internal_groove", 193.5, 10, 8), ("right_end_after_groove", 197, 10, 5),
    ]
    for name, station, outer, inner in radial_stations:
        for plane, vector_axis in enumerate(perpendicular):
            ray_direction = [0, 0, 0]
            ray_direction[vector_axis] = 1
            identifier = f"{name}_{plane}"
            probes.append({"id": identifier, "origin": location(station), "direction": ray_direction, "start": -30, "end": 30})
            expected_probes.append((identifier, [[-outer, -inner], [inner, outer]],
                                    f"轴向站位 {station} 的径向材料截面，方向 {'XYZ'[vector_axis]}"))
    axial_sections = [
        ("centerline_through_hole", 0, [], "中心线必须自左端到右端贯通"),
        ("inner_wall_reliefs", 6, [[2, 192], [195, 199]], "端部沉孔与隐藏内环槽对应的轴向壁材料"),
        ("continuous_outer_end_wall", 9, [[0, 199]], "端部外壁在内槽以外仍连续，不能误做外环槽"),
        ("shoulder_chain", 12, [[20, 174]], "两端小轴与中间所有台阶的轴向边界"),
        ("external_relief_chain", 14.4, [[20, 35], [37, 142], [144, 174]], "两个外环槽的位置与宽度"),
        ("intermediate_chain", 17, [[37, 142]], "中间台阶与大径段的轴向边界"),
        ("central_length", 23, [[62, 142]], "大径段轴向长度"),
    ]
    for name, radius, expected, label in axial_sections:
        probes.append({"id": name, "origin": location(0, radius), "direction": direction, "start": -1, "end": TOTAL_LENGTH+1})
        expected_probes.append((name, expected, label))
    measured = inspect_shape(normalized, ray_probes=probes)
    checks = []
    def check(identifier, label, actual, expected, tolerance=tolerance_mm):
        passed = actual == expected if tolerance is None else close(actual, expected, tolerance)
        checks.append({"id": identifier, "label": label, "actual": actual, "expected": expected,
                       "passed": bool(passed), "tolerance": tolerance})
    check("valid_brep", "STEP重导后的B-Rep有效", measured["valid"], True, None)
    check("single_solid", "一个连通实体", measured["solidCount"], 1, None)
    check("overall_axial_length", "完整轴向尺寸链总长", measured["bbox"]["size"][axial], TOTAL_LENGTH)
    for index in perpendicular:
        check(f"maximum_diameter_{index}", "大径段的完整径向包络", measured["bbox"]["size"][index], 52)
    faces = normalized.val().Faces()
    surface_records = []
    for cylinder in measured["cylinders"]:
        face = faces[cylinder["faceIndex"]]
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(face.wrapped, box, False, False)
        limits = box.Get()
        low, high = limits[axial], limits[axial+3]
        if reverse:
            low, high = length-high, length-low
        coaxial = (abs(abs(cylinder["axis"][axial])-1) <= 1e-6
                   and all(abs(cylinder["origin"][i]) <= tolerance_mm for i in perpendicular))
        surface_records.append({"faceIndex": cylinder["faceIndex"], "diameter": cylinder["diameter"],
                                "axis": cylinder["axis"], "axisOrigin": cylinder["origin"], "axialSpan": [low, high], "coaxial": coaxial})
    for diameter, expected in CYLINDER_SPANS.items():
        matching = [item for item in surface_records if item["coaxial"] and abs(item["diameter"]-diameter) <= tolerance_mm]
        spans = merge_spans([item["axialSpan"] for item in matching], tolerance_mm)
        check(f"cylinder_span_d{diameter}", f"共轴Ø{diameter}实际圆柱面所占轴向区间", spans, expected)
    for identifier, expected, label in expected_probes:
        actual = measured["raySections"][identifier]["fullMaterialIntervals"]
        check(identifier, label, actual, expected)
        checks[-1]["probe"] = next(probe for probe in probes if probe["id"] == identifier)

    reference = reference_shape()
    if axis == "Y":
        reference = reference.rotate((0, 0, 0), (0, 0, 1), 90)
    elif axis == "Z":
        reference = reference.rotate((0, 0, 0), (0, 1, 0), -90)
    if reverse:
        reference = reference.mirror("YZ" if axis == "X" else "XZ" if axis == "Y" else "XY")
        offset = [0, 0, 0]
        offset[axial] = length
        reference = reference.translate(tuple(offset))
    try:
        extra = abs(float(normalized.val().cut(reference).Volume()))
        missing = abs(float(reference.cut(normalized.val()).Volume()))
        reference_comparison = {"status": "completed", "extraVolumeMm3": extra, "missingVolumeMm3": missing,
                                "symmetricDifferenceVolumeMm3": extra+missing, "nominalMatch": extra+missing <= .05,
                                "policy": "Diagnostic nominal shape comparison; unannotated edge treatments require manual interpretation."}
    except Exception as error:
        reference_comparison = {"status": "failed", "errorType": type(error).__name__, "nominalMatch": False}
    alternative_axes = ["XYZ"[i] for i in range(3) if i != axial and abs(bounds["size"][i]-TOTAL_LENGTH) <= tolerance_mm]
    failed = [item["id"] for item in checks if not item["passed"]]
    status = "manual_review_required" if alternative_axes else "failed" if failed else "passed" if reference_comparison["nominalMatch"] else "manual_review_required"
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = output_dir/"normalized-delivered.step"
    cq.exporters.export(normalized, str(normalized_path), exportType="STEP")
    views = export_projections(normalized, output_dir/"actual-views")
    return {"auditVersion": "heldout-drawing3-v1", "purpose": "Independent holdout acceptance only; never model input",
            "createdAt": datetime.now(timezone.utc).isoformat(), "status": status, "passed": status == "passed",
            "sourceStep": {"path": str(step.resolve()), "sha256": hashlib.sha256(step.read_bytes()).hexdigest()},
            "normalization": {"method": "translation_only", "translation": shift, "inputAxis": axis, "reverse": reverse,
                              "originalBBox": bounds, "alternativeAxisRequiresManualSelection": alternative_axes,
                              "automaticAxisGuessing": False},
            "sourceInterpretation": {"viewCount": 1, "symmetry": "axisymmetric interpretation of diameter callouts",
                "dimensionChain": LENGTHS, "rightEndDetail": "Hidden-line closed recess between two internal shoulder lines: an internal annular groove, not an exterior groove or an open end counterbore.",
                "uncertaintiesNotInvented": ["材料、表面粗糙度及尺寸公差未标注，不予猜测或验收。",
                    "未标注倒角/圆角；理想名义参考不添加，若产物存在边缘处理需人工核对。",
                    "图上未单列单位，本脚本沿用当前建模任务mm；不自动进行单位换算。",
                    "图纸只有轴向侧投影；轴对称根据各Ø标注及母线解释，非轴对称附加细节未由此图支持。"]},
            "checks": checks, "checkCount": len(checks), "failedCheckIds": failed,
            "actualInspection": measured, "actualCylinderSpans": surface_records, "referenceComparison": reference_comparison,
            "artifacts": {"normalizedStep": str(normalized_path.resolve()), "views": views}}


def self_test(output_dir):
    import cadquery as cq
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    cases = [("nominal", reference_shape(), "X", False, "passed"),
             ("missing_internal_groove", reference_shape(omit_internal_groove=True), "X", False, "failed"),
             ("wrong_main_diameter", reference_shape(central_radius=25), "X", False, "failed"),
             ("explicit_Z_axis", reference_shape().rotate((0, 0, 0), (0, 1, 0), -90).translate((27, -43, 900)), "Z", False, "passed"),
             ("explicit_reversal", reference_shape().mirror("YZ").translate((77, 30, -10)), "X", True, "passed")]
    for name, shape, axis, reverse, expected in cases:
        step = output_dir/f"{name}.step"
        cq.exporters.export(shape, str(step), exportType="STEP")
        result = audit_step(step, output_dir/name, axis=axis, reverse=reverse)
        (output_dir/name/"independent-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        assert result["status"] == expected, (name, result["status"], result["failedCheckIds"])
        summary.append({"case": name, "status": result["status"], "checkCount": result["checkCount"], "failedCheckIds": result["failedCheckIds"]})
    (output_dir/"self-test.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"selfTestPassed": True, "cases": summary}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="delivered STEP or agent-state.json")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--axis", choices=["X", "Y", "Z"], default="X")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test(args.output_dir)
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        if args.input is None:
            raise ValueError("An actual STEP or state is required")
        step, provenance = source_step(args.input)
        result = audit_step(step, args.output_dir, axis=args.axis, reverse=args.reverse)
        result["sourceRecord"] = provenance
    except Exception as error:
        result = {"auditVersion": "heldout-drawing3-v1", "status": "failed", "passed": False,
                  "errorType": type(error).__name__, "message": str(error)}
    report = args.output_dir/"independent-audit.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({"status": result["status"], "reportPath": str(report.resolve()), "checkCount": result.get("checkCount"),
                      "failedCheckIds": result.get("failedCheckIds"), "error": result.get("message"), "artifacts": result.get("artifacts")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
