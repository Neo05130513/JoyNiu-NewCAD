"""Regression for correcting and accepting the 10.jpg clevis candidate."""

from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai_proxy import AIConversationResult
from app.geometry import cadquery_status
from app.ocr import DrawingRecognition, OCRService, _REQUIRED_ARCHED_CLEVIS_CANDIDATE_FIELDS
from app.platform import ValidationError
from app.platform_api import build_platform_services, create_platform_router


RECIPE = "arched_clevis_support_v1"
SOURCE = b"independent-arched-clevis-confirmation-source"
STANDARD = {
    "archOuterRadius": 28, "archInnerRadius": 16, "baseWidth": 50, "baseThickness": 9,
    "earRadius": 15, "earHoleDiameter": 13, "earCenterHeight": 40, "earThickness": 10,
    "earGap": 30, "mountEarRadius": 15, "mountHoleDiameter": 13, "mountHoleCenterDistance": 80,
    "material": "45# 钢", "units": "mm",
}


def candidate(parameters=None, *, part_type="arched_clevis_support", recipe_id=RECIPE):
    return DrawingRecognition(
        id="drw_arched_confirmation", status="needs_review", part_type=part_type,
        source_filename="10.jpg", source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        image_width=1753, image_height=1275, confidence=0.8, dimensions=(), features=(),
        model_recipe={"recipeId": recipe_id, "parameters": {}, "source": "ai-multimodal-candidate",
                      "parameterEvidence": {"archOuterRadius": {"sourceText": "R28", "confidence": 0.8}}},
        assumptions=(), warnings=("确认人工更正尺寸",), unresolved=("readback_consensus",),
        ocr_text="", engine="ai-candidate", candidate_parameters=dict(STANDARD if parameters is None else parameters),
    )


def test_required_confirmation_fields_are_all_twelve_explicit_dimensions():
    assert _REQUIRED_ARCHED_CLEVIS_CANDIDATE_FIELDS == set(STANDARD) - {"material", "units"}


@pytest.mark.parametrize("field", sorted(_REQUIRED_ARCHED_CLEVIS_CANDIDATE_FIELDS))
def test_confirmation_does_not_fill_a_missing_or_empty_candidate_field(field):
    for empty in (None, ""):
        incomplete = {**STANDARD, field: empty}
        with pytest.raises(ValidationError, match="arched clevis candidate is incomplete") as error:
            OCRService().confirm(candidate(incomplete), reviewer_id="designer")
        assert field in str(error.value)
    incomplete.pop(field)
    with pytest.raises(ValidationError, match="arched clevis candidate is incomplete"):
        OCRService().confirm(candidate(incomplete), reviewer_id="designer")


@pytest.mark.parametrize("part_type", ["arched_clevis_support", "unknown"])
def test_confirmation_keeps_recipe_and_applies_explicit_corrections(part_type):
    source = candidate({**STANDARD, "archOuterRadius": 30, "baseThickness": 8, "mountHoleCenterDistance": 60}, part_type=part_type)
    confirmed = OCRService().confirm(source, reviewer_id="designer", confirmation_type="designer",
                                    parameter_overrides={"archOuterRadius": 28, "baseThickness": 9, "mountHoleCenterDistance": 80})
    assert confirmed.status == "confirmed"
    assert confirmed.part_type == "arched_clevis_support"
    assert confirmed.model_recipe["recipeId"] == RECIPE
    assert confirmed.model_recipe["parameters"] == STANDARD
    assert confirmed.model_recipe["parameterEvidence"] == source.model_recipe["parameterEvidence"]
    assert source.status == "needs_review"
    assert source.candidate_parameters["archOuterRadius"] == 30
    assert confirmed.source_sha256 == source.source_sha256


def test_snake_case_human_overrides_win_over_camel_case_candidate():
    source = candidate({**STANDARD, "archOuterRadius": 30, "baseThickness": 8, "mountHoleCenterDistance": 60})
    confirmed = OCRService().confirm(source, reviewer_id="designer", parameter_overrides={
        "arch_outer_radius": 28, "base_thickness": 9, "mount_hole_center_distance": 80})
    assert confirmed.model_recipe["parameters"] == STANDARD


def test_confirmation_rejects_wrong_identity_unknown_fields_and_invalid_geometry():
    with pytest.raises(ValidationError, match="requires recipeId"):
        OCRService().confirm(candidate(recipe_id="bracket_support_v1"), reviewer_id="designer")
    with pytest.raises(ValidationError, match="unknown parameter override"):
        OCRService().confirm(candidate(), reviewer_id="designer", parameter_overrides={"bossDiameter": 13})
    with pytest.raises(ValidationError, match="failed geometry validation"):
        OCRService().confirm(candidate(), reviewer_id="designer", parameter_overrides={"earGap": 40})


@pytest.fixture
def platform(monkeypatch):
    from app import main
    services = build_platform_services(":memory:", auth_secret="clevis-accept-regression" * 2)
    user = services.auth.create_user("arched-designer@example.com", "a-very-long-password", "Clevis designer", roles=["designer", "reviewer"])

    class CandidateAI:
        def converse(self, message, *, previous_response_id, model_state, files):
            assert files[0].data == SOURCE
            return AIConversationResult(
                response_id="resp_arched_confirmation", message="尺寸需要复核", needs_review=True, questions=(),
                parameter_patch={**STANDARD, "archOuterRadius": 30, "baseThickness": 8, "mountHoleCenterDistance": 60},
                part_type="arched_clevis_support", recipe_id=RECIPE,
                parameter_evidence={"archOuterRadius": {"sourceText": "R?", "value": 30, "confidence": 0.5}},
            )

    services.ai = CandidateAI()
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    monkeypatch.setattr(main, "platform_services", services)
    yield services, TestClient(app), TestClient(main.app), {"Authorization": f"Bearer {services.auth.issue_token(user).token}"}
    services.close()


@pytest.mark.parametrize("route", ["legacy_accept_bridge", "platform_accept", "reviewer_confirm"])
def test_real_http_conversation_correction_acceptance_and_generation(platform, route):
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT is required by this recipe")
    services, client, main_client, headers = platform
    analyzed = client.post("/api/v1/ai/conversation", headers=headers,
                           data={"message": "分析双耳支座"}, files={"file": ("10.jpg", SOURCE, "image/jpeg")})
    assert analyzed.status_code == 200, analyzed.text
    pending = analyzed.json()["drawingRecognition"]
    assert pending["partType"] == "arched_clevis_support"
    assert pending["modelRecipe"]["parameters"] == {}
    assert pending["candidateParameters"]["archOuterRadius"] == 30
    drawing_id = pending["id"]
    target = main_client if route == "legacy_accept_bridge" else client
    path = f"/api/v1/ocr/{drawing_id}/confirm" if route == "reviewer_confirm" else f"/api/v1/drawings/{drawing_id}/accept"
    # The bad AI dimensions remain unconfirmed; corrected input then passes
    # the same mounted HTTP route that failed in the browser.
    rejected = target.post(path, headers=headers, json={})
    assert rejected.status_code == 422, rejected.text
    assert services.recognitions[drawing_id].status == "needs_review"
    accepted = target.post(path, headers=headers, json={"parameterOverrides": {
        "archOuterRadius": 28, "baseThickness": 9, "mountHoleCenterDistance": 80}})
    assert accepted.status_code == 200, accepted.text
    confirmed = accepted.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["partType"] == "arched_clevis_support"
    assert confirmed["parameters"] == STANDARD
    assert confirmed["modelRecipe"]["recipeId"] == RECIPE
    assert confirmed["modelRecipe"]["parameterEvidence"]["archOuterRadius"]["value"] == 30
    generated = main_client.post("/api/v1/models/generate", json={
        "partType": "arched_clevis_support", "recipeId": RECIPE, "parameters": STANDARD,
        "formats": ["step", "glb"], "sourceDrawingId": drawing_id, "requireCadQuery": True})
    assert generated.status_code == 200, generated.text
    assert generated.json()["validation"]["metrics"]["topologyAuditPassed"] is True
    assert generated.json()["parameters"] == STANDARD
    assert {item["format"] for item in generated.json()["artifacts"]} == {"step", "glb"}
    if route == "legacy_accept_bridge":
        stored = client.post("/api/v1/workflows/drawing-to-model", headers=headers, json={
            "recognitionId": drawing_id, "imageBase64": base64.b64encode(SOURCE).decode(),
            "filename": "10.jpg", "formats": ["step", "glb"], "requireCadQuery": True})
        assert stored.status_code == 201, stored.text
        assert stored.json()["recipeId"] == RECIPE
        assert stored.json()["partType"] == "arched_clevis_support"


def test_http_accept_requires_omitted_dimension_then_records_user_supplied_value(platform):
    services, client, main_client, headers = platform
    raw = dict(STANDARD)
    del raw["baseThickness"]
    source = candidate(raw)
    services.recognitions[source.id] = source
    path = f"/api/v1/drawings/{source.id}/accept"
    rejected = main_client.post(path, headers=headers, json={})
    assert rejected.status_code == 422
    assert "baseThickness" in rejected.text
    accepted = main_client.post(path, headers=headers, json={"parameterOverrides": {"baseThickness": 9}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["parameters"] == STANDARD
