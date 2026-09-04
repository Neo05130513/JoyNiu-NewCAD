"""Whitelist registry for supported, server-owned CAD model recipes."""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError as PydanticValidationError

from .geometry import generate_artifacts, validate_bracket
from .schemas import (
    BracketParameters,
    SplitClampSupportParameters,
    SteppedTaperedNozzleParameters,
)
from .split_clamp_support import (
    generate_split_clamp_support_artifacts,
    validate_split_clamp_support,
)
from .stepped_tapered_nozzle import (
    generate_stepped_tapered_nozzle_artifacts,
    validate_stepped_tapered_nozzle,
)


RECIPE_PART_TYPES = {
    "bracket_support_v1": "bracket",
    "split_clamp_support_v1": "split_clamp_support",
    "stepped_tapered_nozzle_with_insert_v1": "stepped_tapered_nozzle",
}


ModelParameters = BracketParameters | SplitClampSupportParameters | SteppedTaperedNozzleParameters


def _model_for(
    recipe_id: str,
) -> type[BracketParameters] | type[SplitClampSupportParameters] | type[SteppedTaperedNozzleParameters]:
    if recipe_id == "bracket_support_v1":
        return BracketParameters
    if recipe_id == "split_clamp_support_v1":
        return SplitClampSupportParameters
    if recipe_id == "stepped_tapered_nozzle_with_insert_v1":
        return SteppedTaperedNozzleParameters
    raise ValueError(f"unsupported recipeId: {recipe_id}")


def parse_model_parameters(recipe_id: str, raw: Mapping[str, Any]) -> ModelParameters:
    """Validate a parameter object without silently dropping unknown keys."""

    if not isinstance(raw, Mapping):
        raise ValueError("parameters must be an object")
    model = _model_for(recipe_id)
    allowed = set(model.model_fields)
    allowed.update(
        field.alias
        for field in model.model_fields.values()
        if getattr(field, "alias", None)
    )
    unknown = sorted(str(key) for key in raw if str(key) not in allowed)
    if unknown:
        raise ValueError("unknown parameter(s): " + ", ".join(unknown))
    try:
        return model.model_validate(dict(raw))
    except PydanticValidationError as exc:
        raise ValueError(str(exc)) from exc


def validate_model_recipe(
    recipe_id: str,
    parameters: ModelParameters,
) -> dict[str, Any]:
    if recipe_id == "bracket_support_v1" and isinstance(parameters, BracketParameters):
        return validate_bracket(parameters).model_dump(mode="json", by_alias=True)
    if recipe_id == "split_clamp_support_v1" and isinstance(parameters, SplitClampSupportParameters):
        return validate_split_clamp_support(parameters)
    if recipe_id == "stepped_tapered_nozzle_with_insert_v1" and isinstance(
        parameters,
        SteppedTaperedNozzleParameters,
    ):
        return validate_stepped_tapered_nozzle(parameters)
    raise ValueError("parameters do not match recipeId")


def generate_model_recipe_artifacts(
    recipe_id: str,
    parameters: ModelParameters,
    formats: list[str],
    *,
    require_cadquery: bool,
):
    if recipe_id == "bracket_support_v1" and isinstance(parameters, BracketParameters):
        return generate_artifacts(parameters, formats, require_cadquery=require_cadquery)
    if recipe_id == "split_clamp_support_v1" and isinstance(parameters, SplitClampSupportParameters):
        return generate_split_clamp_support_artifacts(
            parameters,
            formats,
            require_cadquery=require_cadquery,
        )
    if recipe_id == "stepped_tapered_nozzle_with_insert_v1" and isinstance(
        parameters,
        SteppedTaperedNozzleParameters,
    ):
        return generate_stepped_tapered_nozzle_artifacts(
            parameters,
            formats,
            require_cadquery=require_cadquery,
        )
    raise ValueError("parameters do not match recipeId")


__all__ = [
    "RECIPE_PART_TYPES",
    "generate_model_recipe_artifacts",
    "parse_model_parameters",
    "validate_model_recipe",
]
