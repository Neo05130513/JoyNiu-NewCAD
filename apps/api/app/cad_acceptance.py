"""Compare separately recorded source observations with actual B-Rep measurements.

An observation ledger is recorded before building. This module never imports
the CAD plan or turns its input parameters into an expected answer.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1_000_000:
        raise ValueError("Observation dimensions must be finite numeric evidence, not expressions")
    return float(value)


def _vector(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Observation coordinates require three numbers")
    return [_number(item) for item in value]


def _unit(value):
    vector = _vector(value)
    length = math.sqrt(sum(item * item for item in vector))
    if length < 1e-9:
        raise ValueError("Observation axis must be nonzero")
    return [item / length for item in vector]


def validate_observations(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 64:
        raise ValueError("At most 64 source observations are allowed")
    items = copy.deepcopy(raw)
    seen = set()
    envelopes = {}
    for item in items:
        if not isinstance(item, dict) or set(item) - {"id", "label", "kind", "source", "expected", "tolerance", "axis", "probe"}:
            raise ValueError("Invalid source observation")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", identifier) or identifier in seen:
            raise ValueError("Observation IDs must be unique identifiers")
        seen.add(identifier)
        source = item.get("source")
        if not isinstance(source, dict) or source.get("type") not in {"drawing", "user", "derived"}:
            raise ValueError("Every observation must identify its drawing, user or derivation source")
        if not isinstance(source.get("text"), str) or not source["text"].strip() or len(source["text"]) > 2000:
            raise ValueError("An observation must quote or describe its independent source")
        if source["type"] == "drawing" and (type(source.get("fileIndex")) is not int or source["fileIndex"] < 0 or not isinstance(source.get("view"), str) or not source["view"].strip()):
            raise ValueError("Drawing observations require fileIndex and view")
        if "label" in item and (not isinstance(item["label"], str) or len(item["label"]) > 200):
            raise ValueError("Observation label must be short text")
        tolerance = _number(item.get("tolerance", 0.05))
        if not 0 <= tolerance <= 0.5:
            raise ValueError("Observation comparison tolerance must be between 0 and 0.5 mm")
        item["tolerance"] = tolerance
        if "expected" not in item:
            raise ValueError("Unknown source dimensions must be explicitly null")
        expected = item["expected"]
        kind = item.get("kind")
        if kind == "bbox_size":
            # Axis labels are an unambiguous wire-format alias, not a change
            # to the expected dimension or its measured geometry semantics.
            if isinstance(item.get("axis"), str) and item["axis"].strip().upper() in {"X", "Y", "Z"}:
                item["axis"] = {"X": 0, "Y": 1, "Z": 2}[item["axis"].strip().upper()]
            if type(item.get("axis")) is not int or item["axis"] not in (0, 1, 2):
                raise ValueError(f"{identifier}: bbox_size axis must be 0 (X), 1 (Y) or 2 (Z), not a direction vector")
            if expected is not None and _number(expected) <= 0:
                raise ValueError("Envelope dimensions must be positive")
            if expected is not None:
                previous = envelopes.get(item["axis"])
                if previous and abs(previous["expected"] - expected) > previous["tolerance"] + tolerance:
                    raise ValueError(f"Conflicting whole-solid bounding-box expectations: {previous['id']} and {identifier}. bbox_size measures the entire solid, never local thickness, a radius, hole spacing or a centre height. Use cylinder or ray_intervals for local features.")
                envelopes[item["axis"]] = item
        elif kind == "solid_count":
            if expected is not None and (type(expected) is not int or not 1 <= expected <= 128):
                raise ValueError("Expected solid count must be a positive integer")
        elif kind == "cylinder":
            if expected is not None:
                if not isinstance(expected, dict) or set(expected) - {"diameter", "axis", "center", "count"}:
                    raise ValueError("Cylinder evidence requires diameter and axis")
                if _number(expected.get("diameter")) <= 0:
                    raise ValueError("Cylinder diameter must be positive")
                _unit(expected.get("axis"))
                if "center" in expected:
                    _vector(expected["center"])
                if "count" in expected and (type(expected["count"]) is not int or not 1 <= expected["count"] <= 128):
                    raise ValueError("Cylinder count must be positive")
        elif kind == "ray_intervals":
            probe = item.get("probe")
            if not isinstance(probe, dict) or set(probe) != {"origin", "direction", "start", "end"}:
                raise ValueError("Ray evidence requires origin, direction, start and end")
            _vector(probe["origin"])
            _unit(probe["direction"])
            if _number(probe["end"]) <= _number(probe["start"]):
                raise ValueError("Ray extent must increase")
            if expected is not None:
                if not isinstance(expected, list) or len(expected) > 128:
                    raise ValueError("Expected material intervals must be an array")
                end = -math.inf
                for interval in expected:
                    if not isinstance(interval, list) or len(interval) != 2 or _number(interval[0]) < end or _number(interval[1]) <= _number(interval[0]):
                        raise ValueError("Expected ray intervals must be ordered and non-overlapping")
                    end = interval[1]
        else:
            raise ValueError(f"{identifier}: unsupported observation kind; choose bbox_size, cylinder, ray_intervals or solid_count with their documented fields")
    if sum(item["kind"] == "ray_intervals" for item in items) > 32:
        raise ValueError("At most 32 material sections are allowed")
    return items


def acceptance_ray_probes(observations):
    return [{"id": item["id"], **item["probe"]} for item in validate_observations(observations) if item["kind"] == "ray_intervals"]


def _perpendicular(point, axis):
    axial = sum(a * b for a, b in zip(point, axis))
    return [point[index] - axial * axis[index] for index in range(3)]


def _full_ray_measurement(inspection: dict[str, Any], item: dict[str, Any]) -> tuple[Any, dict[str, Any], str | None]:
    """Require complete, correctly positioned evidence; never trust a window."""
    section = (inspection.get("raySections") or {}).get(item["id"])
    section = section if isinstance(section, dict) else {}
    coverage = section.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    measurement = {"scope": "full_solid_section", "requestedRange": section.get("requestedRange"),
                   "windowIntervals": section.get("intervals"), "coverage": coverage}
    missing = "缺少覆盖整个实体的射线截面证据，须重新执行测量；窗口内占用不能证明完整厚度或贯穿性"
    actual = section.get("fullMaterialIntervals")

    def finite(value):
        return type(value) in (int, float) and abs(value) <= 10**12 and math.isfinite(value)

    def pair(value):
        return isinstance(value, list) and len(value) == 2 and all(finite(number) for number in value) and value[0] < value[1]

    def measured_vector(value):
        if not isinstance(value, list) or len(value) != 3 or not all(finite(number) for number in value):
            raise ValueError("Incomplete measured coordinates")
        return value

    if (coverage.get("complete") is not True or coverage.get("basis") != "actual_solid_bbox"
            or not pair(coverage.get("range")) or not pair(coverage.get("bboxProjection"))
            or not isinstance(actual, list)):
        return None, measurement, missing
    try:
        origin, direction = _vector(section["origin"]), _unit(section["direction"])
        probe = item["probe"]
        if (math.dist(origin, probe["origin"]) > 1e-7 or math.dist(direction, _unit(probe["direction"])) > 1e-7
                or section.get("requestedRange") != [probe["start"], probe["end"]]):
            return None, measurement, "射线测量的位置、方向或提交范围与当前依据不一致，须重新执行测量"
        box = inspection["bbox"]
        lower, upper = measured_vector(box["min"]), measured_vector(box["max"])
        if any(low >= high for low, high in zip(lower, upper)):
            return None, measurement, missing
        projected = [sum(((lower[i] if direction[i] >= 0 else upper[i])-origin[i])*direction[i] for i in range(3)),
                     sum(((upper[i] if direction[i] >= 0 else lower[i])-origin[i])*direction[i] for i in range(3))]
        low, high = coverage["range"]
        if (low > projected[0]-1e-7 or high < projected[1]+1e-7
                or any(abs(got-expected) > 1e-6 for got, expected in zip(coverage["bboxProjection"], projected))):
            return None, measurement, missing
        previous = -math.inf
        for interval in actual:
            if not pair(interval) or interval[0] < previous or interval[0] < low or interval[1] > high:
                return None, measurement, missing
            previous = interval[1]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, measurement, missing
    return actual, measurement, None


def evaluate_cad_acceptance(inspection: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    observations = validate_observations(observations)
    checks = []
    for item in observations:
        expected, actual, passed = item["expected"], None, None
        tolerance = item["tolerance"]
        kind = item["kind"]
        measurement, explanation = None, None
        if expected is not None:
            if kind == "bbox_size":
                size = (inspection.get("bbox") or {}).get("size") or []
                actual = size[item["axis"]] if len(size) == 3 else None
                passed = actual is not None and abs(actual - expected) <= tolerance
            elif kind == "solid_count":
                actual = inspection.get("solidCount")
                passed = actual == expected
            elif kind == "ray_intervals":
                actual, measurement, explanation = _full_ray_measurement(inspection, item)
                passed = (explanation is None and actual is not None and len(actual) == len(expected)
                          and all(abs(got - target) <= tolerance for pair, wanted in zip(actual, expected) for got, target in zip(pair, wanted)))
                if explanation is None:
                    explanation = ("完整实体截面的材料区间与记录依据一致" if passed else
                                   "完整实体截面的材料区间与记录依据不符；检测到提交探针范围之外的材料"
                                   if measurement["coverage"].get("materialOutsideRequestedRange") else
                                   "完整实体截面的材料区间与记录依据不符")
            else:
                axis = _unit(expected["axis"])
                matches, axes = [], []
                for cylinder in inspection.get("cylinders") or []:
                    direction = _unit(cylinder["axis"])
                    if abs(abs(sum(a * b for a, b in zip(direction, axis))) - 1) > 1e-5:
                        continue
                    if abs(cylinder["diameter"] - expected["diameter"]) > tolerance:
                        continue
                    center = _perpendicular(cylinder["origin"], axis)
                    if "center" in expected and math.dist(center, _perpendicular(expected["center"], axis)) > tolerance:
                        continue
                    if not any(math.dist(center, existing) <= max(tolerance, 1e-5) for existing in axes):
                        axes.append(center)
                        matches.append({"diameter": cylinder["diameter"], "axis": cylinder["axis"], "center": center})
                actual = {"count": len(matches), "cylinders": matches}
                passed = len(matches) == expected["count"] if "count" in expected else bool(matches)
        check = {"id": item["id"], "label": item.get("label") or item["id"], "kind": kind,
                 "source": item["source"], "expected": expected, "actual": actual, "passed": passed,
                 "message": explanation or ("原图或描述尚缺少明确数据" if passed is None else "实体测量与记录的依据一致" if passed else "实体测量与记录的依据不符")}
        if measurement is not None:
            check["measurement"] = measurement
        checks.append(check)
    status = "failed" if any(item["passed"] is False for item in checks) else "needs_input" if any(item["passed"] is None for item in checks) else "passed" if checks else "not_checked"
    return {"status": status, "checks": checks, "scope": "Exact geometry compared with separately recorded source observations. Source interpretation and unmeasured features still require review."}
