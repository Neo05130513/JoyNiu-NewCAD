"""Serializable, bounded CAD feature plans. No executable Python is accepted."""

from __future__ import annotations

import ast
from copy import deepcopy
import json
import math
import re
from typing import Any

VERSION = "cad-plan-v1"
MAX_FEATURES = 128
MAX_PARAMETERS = 100
MAX_MAGNITUDE = 1_000_000
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FUNCTIONS = {"sqrt": math.sqrt, "abs": abs, "min": min, "max": max,
              "sin": math.sin, "cos": math.cos, "radians": math.radians}


class PlanValidationError(ValueError):
    def __init__(self, message: str, *, feature_id: str | None = None, code: str = "invalid_plan",
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.feature_id, self.code = feature_id, code
        self.details = details or {}


class UnknownParametersError(PlanValidationError):
    def __init__(self, missing: list[str]):
        super().__init__("Parameters need confirmation: " + ", ".join(missing), code="unknown_parameters")
        self.missing = missing


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanValidationError("A dimension must be a finite number or a permitted expression")
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_MAGNITUDE:
        raise PlanValidationError(f"Numeric results must be finite and within +/-{MAX_MAGNITUDE} mm")
    return result


def _expression_tree(expression: str) -> ast.Expression:
    if not expression or len(expression) > 256:
        raise PlanValidationError("Expressions must contain 1 to 256 characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise PlanValidationError("Invalid dimension expression") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 80:
        raise PlanValidationError("Dimension expression is too complex")
    permitted = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
                 ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.UAdd, ast.USub, ast.Call)
    for node in nodes:
        if not isinstance(node, permitted):
            raise PlanValidationError(f"Expression syntax {type(node).__name__} is not permitted")
        if isinstance(node, ast.Constant):
            _finite(node.value)
        if isinstance(node, ast.Name) and not _IDENTIFIER.fullmatch(node.id):
            raise PlanValidationError("Invalid parameter name in expression")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name)
                or node.func.id not in _FUNCTIONS or node.keywords or not 1 <= len(node.args) <= 8):
            raise PlanValidationError("Only sqrt, abs, min, max, sin, cos and radians calls are permitted")
    return tree


def expression_names(expression: str) -> set[str]:
    tree = _expression_tree(expression)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id not in _FUNCTIONS}


def evaluate_expression(value: Any, parameters: dict[str, float]) -> float:
    """Interpret a small arithmetic AST; never call eval, compile or exec."""
    if not isinstance(value, str):
        return _finite(value)
    tree = _expression_tree(value)

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Constant):
            return _finite(node.value)
        if isinstance(node, ast.Name):
            if node.id not in parameters:
                raise UnknownParametersError([node.id])
            return parameters[node.id]
        if isinstance(node, ast.UnaryOp):
            operand = visit(node.operand)
            return _finite(-operand if isinstance(node.op, ast.USub) else operand)
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            else:
                if abs(right) > 8:
                    raise PlanValidationError("Expression exponents must be within +/-8")
                result = left ** right
            return _finite(result)
        if isinstance(node, ast.Call):
            return _finite(_FUNCTIONS[node.func.id](*(visit(arg) for arg in node.args)))
        raise PlanValidationError("Unsupported expression")

    try:
        return visit(tree.body)
    except (ZeroDivisionError, OverflowError, TypeError, ValueError) as exc:
        if isinstance(exc, PlanValidationError):
            raise
        raise PlanValidationError(f"Dimension expression failed: {exc}") from exc


def resolve_parameters(plan: dict[str, Any]) -> dict[str, float]:
    parameters = plan.get("parameters", {})
    unresolved = {name for name, p in parameters.items() if p.get("value") is None and not p.get("expression")}
    if unresolved:
        raise UnknownParametersError(sorted(unresolved))
    values: dict[str, float] = {}
    pending = dict(parameters)
    for _ in range(len(pending) + 1):
        for name, parameter in list(pending.items()):
            expression = parameter.get("expression")
            if expression:
                if not expression_names(expression).issubset(values):
                    continue
                values[name] = evaluate_expression(expression, values)
            else:
                values[name] = _finite(parameter["value"])
            del pending[name]
        if not pending:
            return values
    raise PlanValidationError("Derived parameters contain a cycle or reference unknown names: " + ", ".join(pending))


_OP_FIELDS = {
    "box": ({"size"}, {"origin"}),
    "cylinder": ({"radius", "height"}, {"origin", "direction"}),
    "profile_extrude": ({"plane", "start", "segments", "distance"}, {"origin"}),
    "profile_revolve": ({"plane", "start", "segments", "axisStart", "axisEnd"}, {"origin", "angle"}),
    "union": ({"inputs"}, set()), "cut": ({"inputs"}, set()), "intersect": ({"inputs"}, set()),
    "translate": ({"input", "vector"}, set()),
    "fillet": ({"input", "radius", "edges"}, set()),
}


def validate_plan(plan: Any, *, allow_unresolved: bool = True) -> dict[str, Any]:
    """Return a canonical JSON copy after structural and expression validation."""
    if not isinstance(plan, dict):
        raise PlanValidationError("CAD plan must be an object")
    try:
        if len(json.dumps(plan, allow_nan=False)) > 256_000:
            raise PlanValidationError("CAD plan exceeds 256 KB")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PlanValidationError("CAD plan must contain finite JSON values") from exc
    result = deepcopy(plan)
    extra = set(result) - {"version", "name", "units", "parameters", "features", "result", "notes", "questions"}
    if extra:
        raise PlanValidationError("Unknown plan fields: " + ", ".join(sorted(extra)))
    if result.get("version") != VERSION or result.get("units", "mm") != "mm":
        raise PlanValidationError("Expected version cad-plan-v1 and units mm")
    result.setdefault("units", "mm")
    result.setdefault("name", "CAD model")
    if not isinstance(result["name"], str) or len(result["name"]) > 200:
        raise PlanValidationError("Plan name must be text of at most 200 characters")
    for key in ("notes", "questions"):
        if key in result and (not isinstance(result[key], list) or len(result[key]) > 100
                or any(not isinstance(v, str) or len(v) > 2000 for v in result[key])):
            raise PlanValidationError(f"{key} must be a bounded list of strings")
    parameters = result.setdefault("parameters", {})
    if not isinstance(parameters, dict) or len(parameters) > MAX_PARAMETERS:
        raise PlanValidationError(f"Expected at most {MAX_PARAMETERS} named parameters")
    for name, parameter in parameters.items():
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or name in _FUNCTIONS:
            raise PlanValidationError(f"Invalid or reserved parameter name: {name}")
        if not isinstance(parameter, dict) or set(parameter) - {"value", "expression", "source", "question", "label", "status"}:
            raise PlanValidationError(f"Invalid parameter object: {name}")
        parameter.setdefault("value", None)
        if parameter["value"] is not None:
            _finite(parameter["value"])
        if parameter.get("expression"):
            if not isinstance(parameter["expression"], str):
                raise PlanValidationError(f"{name}: expression must be text")
            names = expression_names(parameter["expression"])
            if names - parameters.keys():
                raise PlanValidationError(f"{name}: expression references unknown parameters")
            if parameter["value"] is not None:
                raise PlanValidationError(f"{name}: use a value or an expression, never both")
        for key in ("question", "label", "status"):
            if key in parameter and (not isinstance(parameter[key], str) or len(parameter[key]) > 2000):
                raise PlanValidationError(f"{name}: {key} must be bounded text")
        if "source" in parameter and not isinstance(parameter["source"], (dict, str)):
            raise PlanValidationError(f"{name}: source must be an evidence object or text")

    def scalar(value: Any) -> None:
        if isinstance(value, str):
            unknown = expression_names(value) - parameters.keys()
            if unknown:
                raise PlanValidationError("Expression references unknown parameters: " + ", ".join(sorted(unknown)))
        else:
            _finite(value)

    def vector(value: Any, size: int) -> None:
        if not isinstance(value, list) or len(value) != size:
            raise PlanValidationError(f"Expected an array of {size} dimensions")
        for item in value:
            scalar(item)

    features = result.get("features")
    if not isinstance(features, list) or not 1 <= len(features) <= MAX_FEATURES:
        raise PlanValidationError(f"Expected 1 to {MAX_FEATURES} CAD features")
    seen: set[str] = set()
    for feature in features:
        feature_id = feature.get("id") if isinstance(feature, dict) else None
        try:
            if not isinstance(feature_id, str) or not _IDENTIFIER.fullmatch(feature_id) or feature_id in seen:
                raise PlanValidationError("Feature IDs must be unique identifiers")
            op = feature.get("op")
            if not isinstance(op, str) or op not in _OP_FIELDS:
                raise PlanValidationError(f"Unsupported feature operation: {op}")
            required, optional = _OP_FIELDS[op]
            if required - feature.keys() or set(feature) - required - optional - {"id", "op", "label"}:
                raise PlanValidationError(f"Invalid fields for {op}; required={sorted(required)}, optional={sorted(optional)}")
            if "label" in feature and (not isinstance(feature["label"], str) or len(feature["label"]) > 200):
                raise PlanValidationError("Feature label must be bounded text")
            if op in {"union", "cut", "intersect"}:
                refs = feature["inputs"]
                if not isinstance(refs, list) or not 2 <= len(refs) <= 32 or any(not isinstance(ref, str) or ref not in seen for ref in refs):
                    raise PlanValidationError("Boolean inputs must contain 2 to 32 earlier feature IDs")
            elif op in {"translate", "fillet"}:
                if not isinstance(feature["input"], str) or feature["input"] not in seen:
                    raise PlanValidationError("Input must name an earlier feature")
            for key in ("radius", "height", "distance", "angle"):
                if key in feature:
                    scalar(feature[key])
            for key in ("origin", "direction", "size", "vector"):
                if key in feature:
                    vector(feature[key], 3)
            if op.startswith("profile_"):
                if not isinstance(feature["plane"], str) or feature["plane"] not in {"XY", "XZ", "YZ"}:
                    raise PlanValidationError("Profile plane must be XY, XZ or YZ")
                vector(feature["start"], 2)
                segments = feature["segments"]
                if not isinstance(segments, list) or not 2 <= len(segments) <= 128:
                    raise PlanValidationError("Profile needs 2 to 128 line/arc segments; closing edge is automatic")
                for segment in segments:
                    if not isinstance(segment, dict) or not isinstance(segment.get("type"), str) or segment.get("type") not in {"line", "arc"}:
                        raise PlanValidationError("Profile segment must be line or arc")
                    keys = {"type", "to"} | ({"through"} if segment["type"] == "arc" else set())
                    optional_keys = {"radius"} if segment["type"] == "arc" else set()
                    if not keys.issubset(segment) or set(segment) - keys - optional_keys:
                        raise PlanValidationError("Unexpected profile segment fields")
                    vector(segment["to"], 2)
                    if "through" in segment:
                        vector(segment["through"], 2)
                    if "radius" in segment:
                        scalar(segment["radius"])
                if op == "profile_revolve":
                    vector(feature["axisStart"], 2)
                    vector(feature["axisEnd"], 2)
            if op == "fillet" and (not isinstance(feature["edges"], str) or feature["edges"] not in {"all", "parallelX", "parallelY", "parallelZ"}):
                raise PlanValidationError("Fillet edges must be all, parallelX, parallelY or parallelZ")
            seen.add(feature_id)
        except PlanValidationError as exc:
            exc.feature_id = feature_id
            raise
    if not isinstance(result.get("result"), str) or result.get("result") not in seen:
        raise PlanValidationError("result must name a feature")
    if not allow_unresolved:
        resolve_parameters(result)
    return result


def cad_plan_schema() -> dict[str, Any]:
    """A self-contained JSON schema and explicit coordinate conventions for agents."""
    scalar = {"anyOf": [{"type": "number"}, {"type": "string", "maxLength": 256}]}
    def vec(size: int) -> dict[str, Any]:
        return {"type": "array", "items": scalar, "minItems": size, "maxItems": size}
    common = {"id": {"type": "string", "pattern": _IDENTIFIER.pattern}, "label": {"type": "string"}}
    properties = {
        "size": vec(3), "origin": vec(3), "direction": vec(3), "vector": vec(3),
        "radius": scalar, "height": scalar, "distance": scalar, "angle": scalar,
        "plane": {"enum": ["XY", "XZ", "YZ"]}, "start": vec(2), "axisStart": vec(2), "axisEnd": vec(2),
        "input": {"type": "string"}, "inputs": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 32},
        "edges": {"enum": ["all", "parallelX", "parallelY", "parallelZ"]},
        "segments": {"type": "array", "minItems": 2, "maxItems": 128, "items": {"oneOf": [
            {"type": "object", "properties": {"type": {"const": "line"}, "to": vec(2)}, "required": ["type", "to"], "additionalProperties": False},
            {"type": "object", "properties": {"type": {"const": "arc"}, "to": vec(2), "through": vec(2),
                "radius": {**scalar, "description": "Optional expected arc radius in mm. The three-point arc must match this value or parameter expression within numerical tolerance; it does not move the supplied points."}},
             "required": ["type", "to", "through"], "additionalProperties": False},
        ]}},
    }
    features = [{"type": "object", "properties": {**common, "op": {"const": op}, **{key: properties[key] for key in required | optional}},
                 "required": ["id", "op", *sorted(required)], "additionalProperties": False}
                for op, (required, optional) in _OP_FIELDS.items()]
    return {"type": "object", "additionalProperties": False,
            "description": "Bounded CAD plan, no recipes or Python. Expressions permit named parameters and + - * / **, sqrt abs min max sin cos radians. Box origin is its minimum corner. Cylinder origin is base center, direction is unit-normalized. Plane local (u,v,normal): XY=(X,Y,+Z), XZ=(X,Z,-Y), YZ=(Y,Z,+X); positive extrusion follows normal. Profiles close automatically. Boolean first input is target. Derived parameters use expression and value=null. Unknown dimensions use value=null plus question; never fill invented defaults.",
            "properties": {"version": {"const": VERSION}, "name": {"type": "string"}, "units": {"const": "mm"},
                "parameters": {"type": "object", "maxProperties": MAX_PARAMETERS, "additionalProperties": {"type": "object", "additionalProperties": False,
                    "properties": {"value": {"type": ["number", "null"]}, "expression": {"type": "string"}, "source": {"type": ["object", "string"]}, "question": {"type": "string"}, "label": {"type": "string"}, "status": {"type": "string"}}}},
                "features": {"type": "array", "minItems": 1, "maxItems": MAX_FEATURES, "items": {"oneOf": features}},
                "result": {"type": "string"}, "notes": {"type": "array", "items": {"type": "string"}}, "questions": {"type": "array", "items": {"type": "string"}}},
            "required": ["version", "parameters", "features", "result"]}
