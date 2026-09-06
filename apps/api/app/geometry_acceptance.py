"""Post-generation dimensional acceptance gates."""
from __future__ import annotations
from typing import Any, Mapping

_ALIASES = {
    "baseLength": "bboxLength", "baseWidth": "bboxWidth", "totalHeight": "bboxHeight",
    "baseThickness": "baseThicknessMeasured", "boreDiameter": "boreDiameterMeasured",
    "mountHoleCenterDistance": "mountHoleCenterDistanceMeasured", "splitWidth": "splitWidthMeasured",
}

def validate_generated_geometry(recipe_id: str, parameters: Mapping[str, Any], metrics: Mapping[str, Any], *, tolerance: float = 0.05) -> dict[str, Any]:
    """Compare measured artifact metrics with recipe parameters.

    Missing measurements are review-blocking rather than silently passing.
    ``tolerance`` is in model units (millimetres for current recipes).
    """
    fields = {
        "bracket_support_v1": ("baseLength", "baseWidth", "totalHeight", "bossCenterDistance"),
        "split_clamp_support_v1": ("baseLength", "baseWidth", "totalHeight", "mountHoleCenterDistance"),
        "stepped_tapered_nozzle_with_insert_v1": ("mainLength", "counterboreDiameter", "insertOuterDiameter"),
    }.get(recipe_id, ())
    checks=[]
    for field in fields:
        expected = parameters.get(field)
        metric_name = _ALIASES.get(field, field + "Measured")
        actual = metrics.get(metric_name)
        if actual is None:
            checks.append({"field":field,"expected":expected,"actual":None,"status":"needs_review","reason":"missing measured geometry metric"})
            continue
        try: deviation=float(actual)-float(expected)
        except (TypeError, ValueError):
            checks.append({"field":field,"expected":expected,"actual":actual,"status":"needs_review","reason":"non-numeric geometry metric"}); continue
        checks.append({"field":field,"expected":expected,"actual":actual,"deviation":round(deviation,6),"tolerance":tolerance,"status":"pass" if abs(deviation)<=tolerance else "fail"})
    failed=[c["field"] for c in checks if c["status"]=="fail"]
    review=[c["field"] for c in checks if c["status"]=="needs_review"]
    status="failed" if failed else "needs_review" if review else "passed"
    return {"status":status,"productionReady":status=="passed","checks":checks,"failedFields":failed,"reviewFields":review}

__all__=["validate_generated_geometry"]
