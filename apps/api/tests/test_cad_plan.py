from copy import deepcopy

import pytest

from app.cad_plan import (PlanValidationError, UnknownParametersError, cad_plan_schema,
                          evaluate_expression, resolve_parameters, validate_plan)


def simple_plan():
    return {"version": "cad-plan-v1", "parameters": {"width": {"value": 20}},
            "features": [{"id": "body", "op": "box", "size": ["width", 30, 10]}], "result": "body"}


@pytest.mark.parametrize("expression", ["__import__('os').system('touch /tmp/unwanted')", "width.__class__", "[width][0]",
                                         "(x for x in [1])", "lambda: 1", "open('/tmp/secret')", "True", "1e999",
                                         "sqrt(value=2)", "1<<2", "{1:2}", "'hello'", "1//2", "sum([1,2])"])
def test_expressions_reject_executable_or_unbounded_syntax(expression):
    with pytest.raises(PlanValidationError):
        evaluate_expression(expression, {"width": 20})


@pytest.mark.parametrize("expression", ["0**-1", "10**10000000", "1/0", "sqrt(-1)", "1000000*1000000", "(-1)**0.5"])
def test_expressions_bound_numeric_failures(expression):
    with pytest.raises(PlanValidationError):
        evaluate_expression(expression, {})


def test_derived_parameters_resolve_and_edit_without_cached_answers():
    plan = simple_plan()
    plan["parameters"].update({"radius": {"value": 5}, "height": {"expression": "sqrt(width**2-radius**2)"}})
    canonical = validate_plan(plan, allow_unresolved=False)
    assert resolve_parameters(canonical)["height"] == pytest.approx(375**0.5)
    plan["parameters"]["width"]["value"] = 13
    assert resolve_parameters(plan)["height"] == 12
    assert "value" not in plan["parameters"]["height"]  # validation never mutates the source


def test_unknown_parameters_are_preserved_and_block_generation():
    plan = simple_plan()
    plan["parameters"]["width"] = {"value": None, "question": "Please confirm the width", "source": {"view": "top"}}
    canonical = validate_plan(plan)
    assert canonical["parameters"]["width"]["value"] is None
    with pytest.raises(UnknownParametersError) as error:
        validate_plan(plan, allow_unresolved=False)
    assert error.value.missing == ["width"]


def test_cycles_and_value_expression_disagreement_are_rejected():
    plan = simple_plan()
    plan["parameters"] = {"width": {"expression": "height"}, "height": {"expression": "width"}}
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(plan, allow_unresolved=False)
    plan["parameters"] = {"width": {"value": 20, "expression": "5+5"}}
    with pytest.raises(PlanValidationError, match="never both"):
        validate_plan(plan)


@pytest.mark.parametrize("patch", [
    {"features": [{"id": "script", "op": "python", "code": "print(1)"}], "result": "script"},
    {"outputPath": "/etc/passwd"},
    {"features": [{"id": "body", "op": "box", "size": [True, 20, 20]}]},
    {"features": [{"id": "body", "op": "box", "size": ["unseen", 20, 20]}]},
    {"features": [{"id": "body", "op": "box", "size": [20, 20, 20], "code": "import os"}]},
    {"features": [{"id": "body", "op": "cut", "inputs": ["future", "future"]}]},
    {"result": "missing"},
    {"units": "inches"},
])
def test_plans_reject_unsupported_operations_references_and_hidden_code(patch):
    plan = {**deepcopy(simple_plan()), **patch}
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_schema_covers_only_executable_operations():
    schema = cad_plan_schema()
    operations = {item["properties"]["op"]["const"] for item in schema["properties"]["features"]["items"]["oneOf"]}
    assert operations == {"box", "cylinder", "profile_extrude", "profile_revolve", "union", "cut", "intersect", "translate", "fillet"}
