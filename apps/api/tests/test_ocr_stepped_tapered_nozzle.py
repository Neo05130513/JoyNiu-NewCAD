from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ocr import (
    BoundingBox,
    DimensionEvidence,
    DrawingRecognition,
    FeatureEvidence,
    OCRService,
    _REQUIRED_STEPPED_TAPERED_NOZZLE_CANDIDATE_FIELDS,
)
from app.platform import ValidationError
from app.platform_api import build_platform_services, create_platform_router


RECIPE_ID = "stepped_tapered_nozzle_with_insert_v1"
DWG_BYTES = b"AC1021-confirmation-audit-source"
DWG_SHA256 = hashlib.sha256(DWG_BYTES).hexdigest()
STANDARD = {
    "mainLength": 98,
    "headLength": 50,
    "neckLength": 20,
    "headLeftDiameter": 54.25449350717895,
    "headRightDiameter": 56,
    "neckDiameter": 30,
    "tipDiameter": 25,
    "counterboreDiameter": 40,
    "counterboreDepth": 40,
    "axialBoreDiameter": 13,
    "outletDiameter": 17,
    "outletTaperHalfAngle": 15,
    "insertOuterDiameter": 39.4,
    "insertLength": 40,
    "insertThreadDesignation": "M12",
    # Zero is the measured assembly datum, not a missing value.
    "insertAxialOffset": 0,
}


def _candidate(
    *,
    part_type: str = "stepped_tapered_nozzle",
    recipe_id: str = RECIPE_ID,
    parameters: dict[str, object] | None = None,
) -> DrawingRecognition:
    dimension = DimensionEvidence(
        id="dwg-dim-397",
        field="insertOuterDiameter",
        value=39.4,
        unit="mm",
        kind="diameter",
        source_text="Ø39.4",
        confidence=1.0,
        bbox=BoundingBox(
            x=2083.130677,
            y=1315.162134,
            width=0,
            height=39.4,
            coordinate_space="dxf-wcs",
        ),
        view="insert-axial-section",
        verified=False,
    )
    feature = FeatureEvidence(
        id="dwg-feature-insert",
        feature_type="separate_coaxial_threaded_insert",
        parameters={"outerDiameter": 39.4, "length": 40, "thread": "M12"},
        confidence=0.98,
        evidence_ids=(dimension.id,),
        view="insert-axial-section",
    )
    return DrawingRecognition(
        id="drw_dwg_nozzle",
        status="needs_review",
        part_type=part_type,
        source_filename="1(1).dwg",
        source_sha256=DWG_SHA256,
        image_width=2880,
        image_height=1165,
        confidence=0.96,
        dimensions=(dimension,),
        features=(feature,),
        model_recipe={
            "recipeId": recipe_id,
            "parameters": {},
            "source": "ai-multimodal-candidate",
            "parameterEvidence": {
                "insertOuterDiameter": {
                    "sourceText": "Ø39.4",
                    "sourceHandle": "397",
                    "confidence": 1.0,
                }
            },
            "dwgPreprocessing": {
                "sourceSha256": DWG_SHA256,
                "renderSha256": "f" * 64,
                "summaryVersion": "dwg-vector-summary-v1",
            },
        },
        assumptions=("right-hand section is a separate insert candidate",),
        warnings=("M12 pitch and tolerance class are not specified",),
        unresolved=("insert_retention_method",),
        ocr_text="",
        engine="ai-candidate",
        candidate_parameters=dict(STANDARD if parameters is None else parameters),
    )


@pytest.fixture(autouse=True)
def _use_fast_recipe_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Geometry/kernel behaviour has its own recipe tests. These tests isolate
    # the human-confirmation boundary while retaining all dimensional rules.
    monkeypatch.setattr(
        "app.stepped_tapered_nozzle.cadquery_status",
        lambda: {"available": False, "engine": "faceted-fallback"},
    )


def test_required_contract_excludes_only_optional_material_and_units() -> None:
    assert _REQUIRED_STEPPED_TAPERED_NOZZLE_CANDIDATE_FIELDS == set(STANDARD)
    assert "material" not in _REQUIRED_STEPPED_TAPERED_NOZZLE_CANDIDATE_FIELDS
    assert "units" not in _REQUIRED_STEPPED_TAPERED_NOZZLE_CANDIDATE_FIELDS


def test_confirm_persists_nozzle_recipe_and_preserves_dwg_audit() -> None:
    candidate = _candidate()
    confirmed = OCRService().confirm(
        candidate,
        reviewer_id="designer-42",
        confirmation_type="designer",
        confirmed_at="2026-09-04T08:30:00+00:00",
    )

    assert confirmed.status == "confirmed"
    assert confirmed.part_type == "stepped_tapered_nozzle"
    assert confirmed.source_filename == "1(1).dwg"
    assert confirmed.source_sha256 == DWG_SHA256
    assert confirmed.dimensions == candidate.dimensions
    assert confirmed.features == candidate.features
    assert confirmed.assumptions == candidate.assumptions
    assert confirmed.warnings == candidate.warnings
    assert confirmed.unresolved == candidate.unresolved

    recipe = confirmed.model_recipe
    assert recipe["recipeId"] == RECIPE_ID
    assert recipe["source"] == "ai-multimodal-candidate"
    assert recipe["parameterEvidence"] == candidate.model_recipe["parameterEvidence"]
    assert recipe["dwgPreprocessing"] == candidate.model_recipe["dwgPreprocessing"]
    assert recipe["parameters"]["mainLength"] == 98
    assert recipe["parameters"]["insertAxialOffset"] == 0
    assert recipe["parameters"]["material"] == "45# 钢"
    assert recipe["parameters"]["units"] == "mm"
    assert recipe["confirmedBy"] == "designer-42"
    assert recipe["confirmationType"] == "designer"
    assert recipe["confirmedAt"] == "2026-09-04T08:30:00+00:00"
    assert confirmed.confirmed_by == "designer-42"
    assert confirmed.confirmation_type == "designer"
    assert confirmed.confirmed_at == "2026-09-04T08:30:00+00:00"


@pytest.mark.parametrize("missing", ["mainLength", "outletTaperHalfAngle", "insertAxialOffset"])
def test_confirm_rejects_incomplete_nozzle_candidate(missing: str) -> None:
    parameters = dict(STANDARD)
    parameters.pop(missing)
    with pytest.raises(ValidationError, match="stepped tapered nozzle candidate is incomplete") as exc:
        OCRService().confirm(_candidate(parameters=parameters), reviewer_id="designer-42")
    assert missing in str(exc.value)


def test_confirm_routes_unknown_recipe_to_nozzle_instead_of_bracket() -> None:
    confirmed = OCRService().confirm(
        _candidate(part_type="unknown"),
        reviewer_id="designer-42",
    )
    assert confirmed.part_type == "stepped_tapered_nozzle"
    assert confirmed.model_recipe["recipeId"] == RECIPE_ID
    assert "baseLength" not in confirmed.model_recipe["parameters"]


def test_confirm_rejects_recipe_mismatch_unknown_field_and_invalid_geometry() -> None:
    with pytest.raises(ValidationError, match="requires recipeId"):
        OCRService().confirm(
            _candidate(recipe_id="split_clamp_support_v1"),
            reviewer_id="designer-42",
        )

    with pytest.raises(ValidationError, match="unknown parameter override"):
        OCRService().confirm(
            _candidate(parameters={**STANDARD, "untrustedCadCode": "import os"}),
            reviewer_id="designer-42",
        )

    with pytest.raises(ValidationError, match="failed geometry validation"):
        OCRService().confirm(
            _candidate(parameters={**STANDARD, "insertOuterDiameter": 40.5}),
            reviewer_id="designer-42",
        )


def test_platform_designer_accepts_nozzle_candidate_with_auditable_zero_offset() -> None:
    services = build_platform_services(":memory:", auth_secret="n" * 32)
    designer = services.auth.create_user(
        "nozzle-designer@example.com",
        "a-very-long-password",
        "Nozzle Designer",
        roles=["designer"],
    )
    candidate_parameters = dict(STANDARD)
    candidate_parameters.pop("insertAxialOffset")
    candidate = _candidate(parameters=candidate_parameters)
    services.recognitions[candidate.id] = candidate
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)
    token = services.auth.issue_token(designer).token

    response = client.post(
        f"/api/v1/drawings/{candidate.id}/accept",
        headers={"Authorization": f"Bearer {token}"},
        json={"parameterOverrides": {"insertAxialOffset": 0}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "confirmed"
    assert payload["partType"] == "stepped_tapered_nozzle"
    assert payload["sourceSha256"] == DWG_SHA256
    assert payload["dimensions"][0]["source_text"] == "Ø39.4"
    assert payload["modelRecipe"]["recipeId"] == RECIPE_ID
    assert payload["modelRecipe"]["parameters"]["insertAxialOffset"] == 0
    assert payload["modelRecipe"]["parameterEvidence"]["insertOuterDiameter"]["sourceHandle"] == "397"
    assert payload["confirmationType"] == "designer"
    assert payload["confirmedBy"] == designer.id


def test_confirmed_nozzle_runs_through_auditable_pdm_workflow() -> None:
    """The one-call PDM path must dispatch the confirmed recipe, not bracket."""

    services = build_platform_services(":memory:", auth_secret="p" * 32)
    designer = services.auth.create_user(
        "nozzle-workflow@example.com",
        "a-very-long-password",
        "Nozzle Workflow Designer",
        roles=["designer"],
    )
    candidate = _candidate()
    confirmed = services.ocr.confirm(
        candidate,
        reviewer_id=designer.id,
        confirmation_type="designer",
    )
    services.recognitions[confirmed.id] = confirmed
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)
    token = services.auth.issue_token(designer).token

    response = client.post(
        "/api/v1/workflows/drawing-to-model",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "recognitionId": confirmed.id,
            "imageBase64": base64.b64encode(DWG_BYTES).decode(),
            "filename": "1(1).dwg",
            "contentType": "application/acad",
            "formats": ["step", "glb"],
            "requireCadQuery": False,
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["partType"] == "stepped_tapered_nozzle"
    assert payload["recipeId"] == RECIPE_ID
    assert payload["recognition"]["sourceSha256"] == DWG_SHA256
    assert payload["validation"]["valid"] is True
    assert payload["validation"]["parameters"]["mainLength"] == 98
    assert payload["validation"]["metrics"]["solidCount"] == 2
    assert payload["pdm"]["parameterDocument"]["metadata"]["recipeId"] == RECIPE_ID
    assert {item["format"] for item in payload["pdm"]["artifacts"]} == {"step", "glb"}
