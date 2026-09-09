"""Atomic, bounded edits to a declarative CAD plan; no geometry is executed."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .cad_plan import MAX_PARAMETERS, VERSION, PlanValidationError, validate_plan

MAX_EDIT_FEATURES = 12
MAX_EDIT_PARAMETERS = 100
MAX_EDIT_BYTES = 256_000
_FIELDS = {"name", "parameters", "features", "removeFeatures", "result", "notes"}
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class PlanEditError(PlanValidationError):
    def __init__(self, message: str, *, feature_id: str | None = None):
        super().__init__(message, feature_id=feature_id, code="invalid_plan_edit")


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PlanEditError("Feature edit IDs must be valid identifiers")
    return value


def apply_plan_edit(current: dict[str, Any] | None, edit: dict[str, Any]) -> dict[str, Any]:
    """Return a validated complete prefix without mutating either input.

    A parameter/feature upsert replaces the entire entry. Existing feature
    order is preserved; new IDs append in request order. Removals and upserts
    together are limited to twelve feature IDs per edit. A valid prefix is
    only a saved construction description, never proof of valid geometry.
    """
    if not isinstance(edit, dict):
        raise PlanEditError("Plan edit must be an object")
    if set(edit) - _FIELDS:
        raise PlanEditError("Unknown plan edit fields: " + ", ".join(sorted(str(key) for key in set(edit) - _FIELDS)))
    try:
        if len(json.dumps(edit, allow_nan=False).encode("utf-8")) > MAX_EDIT_BYTES:
            raise PlanEditError("Plan edit exceeds the bounded JSON size")
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PlanEditError):
            raise
        raise PlanEditError("Plan edit must contain finite JSON values") from exc

    parameters = edit.get("parameters", {})
    upserts = edit.get("features", [])
    removals = edit.get("removeFeatures", [])
    if not isinstance(parameters, dict) or len(parameters) > min(MAX_EDIT_PARAMETERS, MAX_PARAMETERS):
        raise PlanEditError(f"An edit may upsert at most {MAX_EDIT_PARAMETERS} parameters")
    if not isinstance(upserts, list) or not isinstance(removals, list):
        raise PlanEditError("features and removeFeatures must be arrays")
    if len(upserts) + len(removals) > MAX_EDIT_FEATURES:
        raise PlanEditError(f"An edit may change at most {MAX_EDIT_FEATURES} feature IDs")
    update_ids = []
    for feature in upserts:
        if not isinstance(feature, dict):
            raise PlanEditError("Each feature upsert must be a complete feature object")
        update_ids.append(_identifier(feature.get("id")))
    remove_ids = [_identifier(value) for value in removals]
    if len(set(update_ids)) != len(update_ids) or len(set(remove_ids)) != len(remove_ids):
        raise PlanEditError("Duplicate feature IDs are not allowed in a plan edit")
    if set(update_ids) & set(remove_ids):
        raise PlanEditError("A feature cannot be removed and upserted in the same edit")

    if current is None:
        if not upserts or "result" not in edit:
            raise PlanEditError("The first edit requires at least one explicit feature and its result ID")
        candidate: dict[str, Any] = {"version": VERSION, "units": "mm", "parameters": {}, "features": []}
    else:
        # Never repair or fill a malformed base implicitly. Validation copies
        # the plan, so even a later failing edit cannot alter the caller's state.
        candidate = validate_plan(current, allow_unresolved=True)
    existing_ids = {feature["id"] for feature in candidate["features"]}
    unknown_removals = set(remove_ids) - existing_ids
    if unknown_removals:
        raise PlanEditError("Cannot remove unknown feature IDs: " + ", ".join(sorted(unknown_removals)))

    updates = {feature["id"]: deepcopy(feature) for feature in upserts}
    candidate["features"] = [updates.get(feature["id"], feature) for feature in candidate["features"]
                             if feature["id"] not in remove_ids]
    candidate["features"].extend(updates[identifier] for identifier in update_ids if identifier not in existing_ids)
    candidate["parameters"].update(deepcopy(parameters))
    for key in ("name", "notes", "result"):
        if key in edit:
            candidate[key] = deepcopy(edit[key])
    # Checks all previous references and expressions, not merely edited fields.
    # Unknown numeric values stay null; execution will request them explicitly.
    return validate_plan(candidate, allow_unresolved=True)


__all__ = ["apply_plan_edit", "PlanEditError", "MAX_EDIT_FEATURES", "MAX_EDIT_PARAMETERS"]
