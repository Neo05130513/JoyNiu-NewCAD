from __future__ import annotations

import base64
import json
import struct
import tempfile
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.geometry import (
    GeneratedArtifact,
    audit_geometry,
    build_cadquery_shape,
    cadquery_status,
    generate_artifacts,
    validate_bracket,
)
from app.main import app
from app.schemas import BracketParameters
from app.platform_api import build_platform_services


CLIENT = TestClient(app)
BRACKET = {
    "baseLength": 100,
    "baseWidth": 50,
    "baseThickness": 10,
    "upperLength": 70,
    "upperWidth": 30,
    "upperHeight": 30,
    "totalHeight": 40,
    "notchOpening": 40,
    "notchRadius": 15,
    "bossDiameter": 20,
    "bossCenterDistance": 70,
}

# 1x1 transparent PNG; enough to exercise multipart and offline recognition.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_health_reports_fallback_or_cadquery() -> None:
    response = CLIENT.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "joyniu-cad-api"
    assert payload["geometry"]["engine"] in {"cadquery-occt", "faceted-fallback"}
    assert payload["capabilities"]["stepExport"] is True


def test_openapi_health_aliases_match_runtime_contract_without_duplicate_ids() -> None:
    # The platform compatibility router also exposes /health.  It remains
    # callable, but the geometry service owns all public health aliases and
    # therefore their documented response must be the same as runtime.
    app.openapi_schema = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = app.openapi()
    assert not any("Duplicate Operation ID" in str(item.message) for item in caught)
    for path in ("/health", "/api/health", "/api/v1/health"):
        operation = schema["paths"][path]["get"]
        assert operation["tags"] == ["system"]
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema["type"] == "object"
        assert response_schema.get("additionalProperties") is True


def test_bracket_validation_includes_arc_constraints() -> None:
    response = CLIENT.post("/api/brackets/validate", json=BRACKET)
    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["metrics"]["notchArcCenterZ"] == 40
    assert payload["metrics"]["notchBottomZ"] == 25
    assert payload["metrics"]["topologyAuditPassed"] is True
    assert payload["metrics"]["solidCount"] == 1
    assert payload["metrics"]["bboxLength"] == 100
    assert payload["metrics"]["bboxWidth"] == 50
    assert payload["metrics"]["bboxHeight"] == 40
    assert payload["metrics"]["volumeMm3"] > 0
    assert payload["metrics"]["estimatedVolumeMm3"] > 0
    assert payload["metrics"]["volumeMatchesEstimate"] is True
    assert payload["metrics"]["volumeRelativeError"] < 0.02
    assert payload["metrics"]["notchArcPresent"] is True
    assert payload["metrics"]["bossPairPresent"] is True
    rules = {item["ruleId"]: item for item in payload["issues"]}
    assert rules["notch.arc_center"]["passed"] is True
    assert rules["notch.bottom"]["passed"] is True
    assert rules["topology.audit"]["actual"]["notchArcCenterZ"] == 40
    assert rules["topology.audit"]["actual"]["notchBottomZ"] == 25


def test_invalid_bracket_returns_audit_report() -> None:
    invalid = {**BRACKET, "upperLength": 120, "totalHeight": 30}
    response = CLIENT.post("/api/brackets/validate", json=invalid)
    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is False
    assert any(
        item["ruleId"] == "envelope.upper_length" and not item["passed"]
        for item in payload["issues"]
    )


def test_generate_step_and_glb_and_download() -> None:
    response = CLIENT.post(
        "/api/brackets/generate",
        json={"parameters": BRACKET, "formats": ["step", "glb"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "completed"
    assert len(payload["artifacts"]) == 2
    by_format = {item["format"]: item for item in payload["artifacts"]}
    for fmt in ("step", "glb"):
        artifact = by_format[fmt]
        download = CLIENT.get(artifact["downloadUrl"])
        assert download.status_code == 200
        assert len(download.content) == artifact["sizeBytes"]
        assert download.headers["x-joyniu-sha256"] == artifact["sha256"]
    step = CLIENT.get(by_format["step"]["downloadUrl"]).content
    glb = CLIENT.get(by_format["glb"]["downloadUrl"]).content
    assert step.startswith(b"ISO-10303-21;")
    assert b"\nHEADER;\n" in step
    assert b"\\nHEADER;\\n" not in step
    assert glb[:4] == b"glTF"
    _magic, version, total_length = struct.unpack("<4sII", glb[:12])
    assert version == 2
    assert total_length == len(glb)
    json_length = struct.unpack("<I", glb[12:16])[0]
    bin_header_offset = 20 + json_length
    assert glb[bin_header_offset + 4 : bin_header_offset + 8] == b"BIN\x00"


def test_generation_response_downgrades_when_step_exporter_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful kernel audit must not mask a later STEP export failure."""

    from app import main as main_module

    def exporter_fallback(
        _parameters: BracketParameters,
        _formats: list[str],
        *,
        require_cadquery: bool = False,
    ) -> list[GeneratedArtifact]:
        assert require_cadquery is False
        return [
            GeneratedArtifact(
                format="step",
                data=b"ISO-10303-21;\n/* preview */\nEND-ISO-10303-21;\n",
                engine="faceted-step-fallback",
                production_ready=False,
                warnings=["simulated OCCT STEP exporter failure"],
            ),
            GeneratedArtifact(
                format="glb",
                data=b"glTF" + b"\x00" * 8,
                engine="mesh-fallback",
                production_ready=False,
                warnings=["simulated mesh fallback"],
            ),
        ]

    monkeypatch.setattr(main_module, "generate_artifacts", exporter_fallback)
    response = CLIENT.post(
        "/api/brackets/generate",
        json={"parameters": BRACKET, "formats": ["step", "glb"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["engine"] == "faceted-step-fallback"
    assert payload["validation"]["engine"] == "faceted-step-fallback"
    assert payload["validation"]["productionReady"] is False
    metrics = payload["validation"]["metrics"]
    assert metrics["artifactProductionReady"] is False
    assert metrics["stepArtifactProductionReady"] is False
    assert metrics["artifactEngine"] == "faceted-step-fallback"
    assert metrics["previewOnly"] is True
    assert "preview" in metrics["productionReadyReason"]
    step = next(item for item in payload["artifacts"] if item["format"] == "step")
    assert step["engine"] == "faceted-step-fallback"
    assert step["productionReady"] is False
    assert step["warnings"] == ["simulated OCCT STEP exporter failure"]


def test_direct_export_endpoint_returns_attachment() -> None:
    response = CLIENT.post("/api/brackets/export/glb", json=BRACKET)
    assert response.status_code == 200
    assert response.content[:4] == b"glTF"
    assert "attachment" in response.headers["content-disposition"]


def test_drawing_upload_and_result_submission() -> None:
    response = CLIENT.post(
        "/api/drawings/recognize",
        files={"file": ("test.png", PNG, "image/png")},
    )
    assert response.status_code == 200, response.text
    drawing = response.json()
    assert drawing["partType"] == "bracket"
    assert drawing["parameters"]["baseLength"] == 100
    assert drawing["validation"]["valid"] is True
    fetched = CLIENT.get(f"/api/drawings/{drawing['id']}")
    assert fetched.status_code == 200

    submitted = CLIENT.post(
        "/api/drawings/results",
        json={
            "sourceFilename": "vision.json",
            "sourceSha256": "external-test",
            "confidence": 0.97,
            "confirmed": True,
            "parameters": BRACKET,
            "evidence": [
                {
                    "field": "baseLength",
                    "value": 100,
                    "source": "test",
                    "confidence": 0.99,
                }
            ],
        },
    )
    assert submitted.status_code == 200, submitted.text
    # This compatibility route has no reviewer identity, so a client-supplied
    # ``confirmed`` flag must never authorize production geometry generation.
    assert submitted.json()["status"] == "needs_review"
    assert "reviewer confirmation" in submitted.json()["warnings"][0]
    blocked = CLIENT.post(
        "/api/brackets/generate",
        json={"sourceDrawingId": submitted.json()["id"], "parameters": BRACKET},
    )
    assert blocked.status_code == 409, blocked.text


def test_untrusted_hints_require_reviewer_confirmation_before_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compatibility uploads cannot self-confirm with eleven client hints."""

    hints = {
        "baseLength": 100,
        "baseWidth": 50,
        "baseThickness": 10,
        "upperLength": 70,
        "upperWidth": 30,
        "upperHeight": 30,
        "totalHeight": 40,
        "notchOpening": 40,
        "notchRadius": 15,
        "bossDiameter": 20,
        "bossCenterDistance": 70,
    }
    upload = CLIENT.post(
        "/api/drawings/recognize",
        files={"file": ("hinted.png", PNG, "image/png")},
        data={"hints": json.dumps(hints)},
    )
    assert upload.status_code == 200, upload.text
    drawing = upload.json()
    assert drawing["status"] == "needs_review"
    blocked = CLIENT.post(
        "/api/brackets/generate",
        json={"sourceDrawingId": drawing["id"], "parameters": BRACKET},
    )
    assert blocked.status_code == 409, blocked.text

    services = build_platform_services(":memory:", auth_secret="confirm" * 6)
    reviewer = services.auth.create_user(
        "drawing-reviewer@example.com",
        "a-very-long-password",
        "Drawing reviewer",
        roles=["reviewer"],
    )
    monkeypatch.setattr("app.main.platform_services", services)
    token = services.auth.issue_token(reviewer).token
    no_auth = CLIENT.post(f"/api/drawings/{drawing['id']}/confirm", json={})
    assert no_auth.status_code == 401, no_auth.text
    typo = CLIENT.post(
        f"/api/drawings/{drawing['id']}/confirm",
        headers={"Authorization": f"Bearer {token}"},
        json={"parameterOverrides": {"notchRadiu": 15}},
    )
    assert typo.status_code == 422, typo.text
    confirmed = CLIENT.post(
        f"/api/drawings/{drawing['id']}/confirm",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    generated = CLIENT.post(
        "/api/brackets/generate",
        json={"sourceDrawingId": drawing["id"], "parameters": BRACKET, "formats": ["glb"]},
    )
    assert generated.status_code == 200, generated.text


def test_generate_rejects_invalid_geometry() -> None:
    response = CLIENT.post(
        "/api/brackets/generate",
        json={"parameters": {**BRACKET, "notchRadius": 30}},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["validation"]["valid"] is False


def test_require_cadquery_never_silently_downgrades_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict callers receive an error instead of an unreleaseable preview."""

    import app.geometry as geometry_module

    monkeypatch.setattr(geometry_module, "get_cadquery", lambda: None)
    monkeypatch.setattr(
        geometry_module,
        "_CADQUERY_ERROR",
        "simulated missing OCCT",
    )
    with pytest.raises(RuntimeError, match="CadQuery is required"):
        generate_artifacts(
            BracketParameters(**BRACKET),
            ["step"],
            require_cadquery=True,
        )


def test_analytic_topology_audit_is_explicit_and_reproducible() -> None:
    parameters = BracketParameters(**BRACKET)
    audit = audit_geometry(
        parameters,
        engine="faceted-fallback",
        try_kernel=False,
    )
    assert audit["topologyAuditEngine"] == "analytic-fallback"
    assert audit["kernelBacked"] is False
    assert audit["topologyAuditPassed"] is True
    assert audit["solidCount"] == 1
    assert audit["faceCount"] > 100
    assert audit["vertexCount"] > 100
    assert audit["bboxMatchesParameters"] is True
    assert audit["volumeMm3"] > 0
    assert audit["estimatedVolumeMm3"] > 0
    assert audit["volumeMatchesEstimate"] is True
    assert audit["volumeRelativeError"] == pytest.approx(0, abs=0.02)
    assert audit["notchArcFaceCount"] == 1
    assert audit["bossCylindricalFaceCount"] == 2
    assert audit["notchArcCenterZ"] == 40
    assert audit["notchBottomZ"] == 25
    assert audit["notchArcCenterMatchesParameters"] is True
    assert audit["notchBottomMatchesParameters"] is True
    assert audit["bossCenterDistanceMeasured"] == 70
    assert audit["bossCenterDistanceMatchesParameters"] is True
    assert audit["bossHeightMeasured"] == 30
    assert audit["bossHeightMatchesParameters"] is True

    report = validate_bracket(parameters, engine="faceted-fallback")
    assert report.valid is True
    assert report.production_ready is False
    assert report.metrics["kernelBacked"] is False
    assert any(issue.rule_id == "topology.audit" for issue in report.issues)


def test_cadquery_topology_audit_when_occt_is_available() -> None:
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    parameters = BracketParameters(**BRACKET)
    shape = build_cadquery_shape(parameters)
    audit = audit_geometry(parameters, shape, engine="cadquery-occt")
    assert audit["topologyAuditEngine"] == "cadquery-occt"
    assert audit["kernelBacked"] is True
    assert audit["kernelShapeValid"] is True
    assert audit["topologyAuditPassed"] is True
    assert audit["solidCount"] == 1
    assert audit["faceCount"] >= 10
    assert audit["edgeCount"] >= 20
    assert audit["bboxMatchesParameters"] is True
    assert audit["bboxLength"] == pytest.approx(100, abs=0.01)
    assert audit["bboxWidth"] == pytest.approx(50, abs=0.01)
    assert audit["bboxHeight"] == pytest.approx(40, abs=0.01)
    assert audit["volumeMm3"] > 100_000
    assert audit["notchArcFaceCount"] >= 1
    assert audit["bossCylindricalFaceCount"] >= 2
    assert audit["notchArcCenterMatchesParameters"] is True
    assert audit["notchBottomMatchesParameters"] is True
    assert audit["notchAxisAligned"] is True
    assert audit["bossAxesAligned"] is True
    assert audit["bossCenterDistanceMeasured"] == pytest.approx(70, abs=0.01)
    assert audit["bossCenterDistanceMatchesParameters"] is True
    assert audit["bossHeightMeasured"] == pytest.approx(30, abs=0.01)
    assert audit["bossHeightMatchesParameters"] is True


def test_cadquery_audit_rejects_a_shifted_arc_even_with_matching_bbox() -> None:
    """Feature coordinates are measured from OCCT, not copied from inputs."""

    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    parameters = BracketParameters(**BRACKET)
    shape = build_cadquery_shape(parameters)
    # Translating only in Z preserves the 100 x 50 x 40 envelope, so a bbox
    # check alone would miss this defect; the measured arc/boss axes must fail.
    shifted = shape.translate((0, 0, 1))
    audit = audit_geometry(parameters, shifted, engine="cadquery-occt")
    assert audit["bboxMatchesParameters"] is True
    assert audit["notchArcCenterZ"] == pytest.approx(41, abs=0.01)
    assert audit["notchBottomZ"] == pytest.approx(26, abs=0.01)
    assert audit["notchArcCenterMatchesParameters"] is False
    assert audit["notchBottomMatchesParameters"] is False
    assert audit["topologyAuditPassed"] is False


def test_cadquery_step_round_trip_keeps_the_audited_envelope() -> None:
    """A production STEP must reopen as a solid with the same envelope."""

    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    import cadquery as cq  # type: ignore

    parameters = BracketParameters(**BRACKET)
    artifact = generate_artifacts(parameters, ["step"], require_cadquery=True)[0]
    assert artifact.engine == "cadquery-occt"
    assert artifact.production_ready is True
    with tempfile.TemporaryDirectory(prefix="joyniu-step-") as directory:
        path = Path(directory) / "bracket.step"
        path.write_bytes(artifact.data)
        reopened = cq.importers.importStep(str(path))
        value = reopened.val()
        box = value.BoundingBox()
        assert len(value.Solids()) == 1
        assert box.xlen == pytest.approx(100, abs=0.01)
        assert box.ylen == pytest.approx(50, abs=0.01)
        assert box.zlen == pytest.approx(40, abs=0.01)
        reopened_audit = audit_geometry(parameters, value, engine="cadquery-occt")
        assert reopened_audit["topologyAuditPassed"] is True


def test_cadquery_notch_keeps_the_40mm_top_opening() -> None:
    """The lead-in cutter must survive OCCT coplanar-face regularisation."""

    if not cadquery_status()["available"]:
        pytest.skip("CadQuery/OCCT optional dependency is unavailable")
    shape = build_cadquery_shape(BracketParameters(**BRACKET))
    value = shape.val() if hasattr(shape, "val") else shape
    # The rectangular lead-in is intentionally overlapped by 0.01 mm, so its
    # lower shoulder is at z=39.99 while the nominal arc centre remains z=40.
    opening_edges = []
    arc_bottoms = []
    for edge in value.Edges():
        box = edge.BoundingBox()
        if (
            box.ymin == pytest.approx(-15, abs=0.05)
            and box.ymax == pytest.approx(-15, abs=0.05)
            and box.zmin == pytest.approx(39.99, abs=0.02)
            and box.zmax == pytest.approx(39.99, abs=0.02)
        ):
            opening_edges.append((box.xmin, box.xmax))
        if edge.geomType().upper() == "CIRCLE":
            arc_bottoms.append((box.zmin, box.zmax))
    assert any(
        lo == pytest.approx(-20, abs=0.05)
        and hi == pytest.approx(-15, abs=0.05)
        for lo, hi in opening_edges
    )
    assert any(
        lo == pytest.approx(15, abs=0.05)
        and hi == pytest.approx(20, abs=0.05)
        for lo, hi in opening_edges
    )
    # At least one circular edge belongs to each boss, and the notch's arc
    # reaches the documented Z=25 bottom plane.
    assert any(lo == pytest.approx(25, abs=0.05) for lo, _ in arc_bottoms)
