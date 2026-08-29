#!/usr/bin/env python3
"""Run the local, auditable drawing-to-CAD acceptance check.

The check intentionally works with the Python standard library and the domain
services, so it can diagnose a fresh checkout before optional FastAPI,
CadQuery/OCCT or OCR wheels are installed.  When those wheels are present it
also exercises the geometry exporter and reports whether the generated STEP is
an OCCT B-Rep or the explicitly-labelled fallback preview.

Examples::

    # Exact image from the acceptance conversation (if the temporary file still exists)
    python apps/api/scripts/acceptance_check.py --drawing /path/to/drawing.jpg

    # Deterministic fixture-only smoke check (works without the image file)
    python apps/api/scripts/acceptance_check.py --fixture bracket_support_v1

Exit status is non-zero only when a required service invariant fails.  Missing
optional dependencies are reported as ``skipped`` rather than hidden.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = REPO_ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.cam import CAMService, StockDefinition  # noqa: E402
from app.ocr import ACCEPTANCE_DRAWING_SHA256, OCRService  # noqa: E402
from app.platform import AuthService, PDMRepository, Permission  # noqa: E402


EXPECTED = {
    "baseLength": 100.0,
    "baseWidth": 50.0,
    "baseThickness": 10.0,
    "upperLength": 70.0,
    "upperWidth": 50.0,
    "upperHeight": 30.0,
    "totalHeight": 40.0,
    "notchOpening": 40.0,
    "notchRadius": 15.0,
    "slotLength": 30.0,
    "slotWidth": 10.0,
    "pocketDepth": 10.0,
    "saddleDepth": 50.0,
    "holeDepth": 40.0,
    "holeThrough": True,
    "bossDiameter": 20.0,
    "bossCenterDistance": 70.0,
}


def _status(ok: bool | None) -> str:
    return "passed" if ok is True else "failed" if ok is False else "skipped"


def _check(name: str, ok: bool | None, details: str = "") -> dict[str, Any]:
    item = {"name": name, "status": _status(ok)}
    if details:
        item["details"] = details
    marker = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    print(f"[{marker}] {name}" + (f": {details}" if details else ""))
    return item


def _find_default_drawing() -> Path | None:
    candidates = [
        Path(os.getenv("JOYNIU_ACCEPTANCE_DRAWING", "")),
        Path(
            "/var/folders/7l/7gm7qtgj14d4gyp7f34q1yl40000gn/T/"
            "codex-clipboard-6532c96b-cdb4-4481-8ffc-b1ad27ad26ac.jpg"
        ),
    ]
    for candidate in candidates:
        if str(candidate) and candidate.exists() and candidate.is_file():
            return candidate
    return None


def _load_geometry_capability() -> tuple[Any | None, str]:
    try:
        from app.geometry import generate_artifacts, validate_bracket
        from app.schemas import BracketParameters

        return (generate_artifacts, validate_bracket, BracketParameters), "available"
    except Exception as exc:  # pydantic is optional outside the API extra
        return None, f"{type(exc).__name__}: {exc}"


def run_check(
    *,
    drawing: Path | None,
    fixture_id: str,
    strict_optional: bool = False,
) -> tuple[bool, dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    print("JoyNiu NewCAD v0.2 acceptance check")
    print(f"repository: {REPO_ROOT}")

    payload: bytes
    source_name = "acceptance-fixture.bin"
    if drawing is not None:
        payload = drawing.read_bytes()
        source_name = drawing.name
        checks.append(
            _check(
                "drawing file readable",
                bool(payload),
                f"{source_name}, {len(payload)} bytes, sha256={hashlib.sha256(payload).hexdigest()}",
            )
        )
    else:
        # Fixture IDs are explicit in this mode.  The service records the
        # actual synthetic hash in its response, so this cannot be mistaken for
        # an exact upload match.
        payload = b"joyniu-acceptance-fixture-placeholder"
        checks.append(_check("drawing file readable", None, "using explicit fixture id (no file supplied)"))

    # ``--fixture`` is an explicit fixture-only smoke mode and may use the
    # placeholder payload; real uploads always require the fixture SHA to
    # match the bytes, preventing fixture-id spoofing in the API.
    ocr = OCRService(allow_unverified_fixture=drawing is None)
    recognition = ocr.analyze(payload, filename=source_name, fixture_id=fixture_id)
    parameters = recognition.model_recipe.get("parameters", {})
    missing: list[str] = []
    for key, expected in EXPECTED.items():
        actual = parameters.get(key)
        if isinstance(expected, bool):
            matches = actual is expected
        else:
            try:
                matches = float(actual) == float(expected)
            except (TypeError, ValueError):
                matches = False
        if not matches:
            missing.append(key)
    checks.append(
        _check(
            "OCR evidence and dimensions",
            recognition.status == "confirmed" and not missing,
            f"status={recognition.status}, engine={recognition.engine}, dimensions={len(recognition.dimensions)}"
            + (f", missing={missing}" if missing else ""),
        )
    )
    checks.append(
        _check(
            "OCR evidence is traceable",
            bool(recognition.source_sha256) and bool(recognition.features) and all(
                evidence.verified for evidence in recognition.dimensions
            ),
            f"source_sha256={recognition.source_sha256}, features={len(recognition.features)}",
        )
    )
    geometry_request = recognition.to_geometry_request()
    checks.append(
        _check(
            "OCR → geometry recipe",
            geometry_request["parameters"].get("notchRadius") == 15
            and geometry_request["parameters"].get("bossDiameter") == 20
            and geometry_request["parameters"].get("upperWidth") == 50
            and geometry_request["parameters"].get("slotLength") == 30
            and geometry_request["parameters"].get("slotWidth") == 10
            and geometry_request["parameters"].get("pocketDepth") == 10
            and geometry_request["parameters"].get("saddleDepth") == 50
            and geometry_request["parameters"].get("holeDepth") == 40
            and geometry_request["parameters"].get("holeThrough") is True,
            "recipe contains full-width upper body, Y-pocket, R15-through-Y cut and Ø20 vertical through-hole aliases",
        )
    )
    def _feature_value(feature: Any, snake_name: str, camel_name: str, default: Any = None) -> Any:
        if isinstance(feature, Mapping):
            return feature.get(snake_name, feature.get(camel_name, default))
        return getattr(feature, snake_name, default)

    feature_types = {
        str(_feature_value(feature, "feature_type", "featureType", ""))
        for feature in recognition.features
    }
    feature_aliases = {
        str(alias)
        for feature in recognition.features
        for alias in (_feature_value(feature, "feature_type_aliases", "featureTypeAliases", []) or [])
    }
    checks.append(
        _check(
            "OCR subtractive feature semantics",
            "rectangular_pocket_pair" in feature_types
            and "side_notch_cut_pair" in feature_types
            and "vertical_through_hole_pair" in feature_aliases
            and "vertical_boss_pair" not in feature_types,
            f"features={sorted(feature_types)}, aliases={sorted(feature_aliases)}",
        )
    )

    # PDM + RBAC are tested against a throw-away SQLite file to exercise the
    # same persistence path used by the API without leaving repository state.
    with tempfile.TemporaryDirectory(prefix="joyniu-acceptance-") as directory:
        database = str(Path(directory) / "acceptance.sqlite3")
        pdm = PDMRepository(database)
        auth = AuthService(database, token_secret="a" * 32)
        designer = auth.create_user(
            "designer@example.com",
            "acceptance-password",
            "Acceptance Designer",
            roles=["designer"],
        )
        reviewer = auth.create_user(
            "reviewer@example.com",
            "acceptance-password",
            "Acceptance Reviewer",
            roles=["reviewer"],
        )
        manufacturer = auth.create_user(
            "manufacturing@example.com",
            "acceptance-password",
            "Acceptance Manufacturing",
            roles=["manufacturing"],
        )
        checks.append(
            _check(
                "RBAC account and permissions",
                auth.has_permission(designer, Permission.CAM_SIMULATE)
                and not auth.has_permission(designer, Permission.CAM_RELEASE)
                and auth.has_permission(manufacturer, Permission.CAM_RELEASE),
                "designer can simulate; manufacturing can release; separation is enforced",
            )
        )
        token = auth.authenticate("designer@example.com", "acceptance-password")
        checks.append(
            _check("RBAC signed token", auth.verify_token(token.token).id == designer.id, "token verified")
        )
        project = pdm.create_project("Acceptance bracket", designer.id)
        source_document = pdm.create_document(project.id, "drawing.jpg", "drawing", designer.id)
        source_version = pdm.create_version(
            source_document.id,
            payload,
            designer.id,
            file_name=source_name,
            content_type="image/jpeg",
            metadata={"sha256": recognition.source_sha256, "recognitionId": recognition.id},
        )
        model_document = pdm.create_document(project.id, "bracket-parameters.json", "model", designer.id)
        model_version = pdm.create_version(
            model_document.id,
            geometry_request["parameters"],
            designer.id,
            file_name="bracket-parameters.json",
            content_type="application/json",
            metadata={"sourceDrawingVersion": source_version.id},
        )
        manifest = pdm.get_project_manifest(project.id)
        checks.append(
            _check(
                "PDM source/model version linkage",
                len(manifest["documents"]) == 2
                and source_version.sha256 == recognition.source_sha256
                and model_version.revision == 1,
                f"documents={len(manifest['documents'])}, sourceRevision={source_version.revision}, modelRevision={model_version.revision}",
            )
        )

        # CAM workflow: source geometry hash is tied to the parameter snapshot;
        # reviewer approval and manufacturing release are separate actors.
        geometry_hash = hashlib.sha256(
            json.dumps(geometry_request["parameters"], sort_keys=True).encode("utf-8")
        ).hexdigest()
        cam = CAMService()
        plan = cam.create_plan(
            actor_id=designer.id,
            geometry_hash=geometry_hash,
            stock=StockDefinition(110, 60, 45, material="45# steel"),
            project_id=project.id,
            source_document_id=model_document.id,
            source_version_id=model_version.id,
        )
        cam.add_operation(
            plan.id,
            actor_id=designer.id,
            operation_type="facing",
            tool_id="T10",
            depth=1,
            feed_rate=600,
            spindle_rpm=6000,
            retract_height=5,
            path_length=100,
        )
        simulation = cam.simulate(plan.id, actor_id=designer.id)
        checks.append(
            _check(
                "CAM deterministic simulation",
                simulation.passed and simulation.geometry_hash == geometry_hash,
                f"engine={simulation.engine}, passed={simulation.passed}, warnings={len(simulation.warnings)}",
            )
        )
        cam.approve(plan.id, actor_id=reviewer.id, role="reviewer", simulation_id=simulation.id)
        nc = cam.release_nc(
            plan.id,
            actor_id=manufacturer.id,
            actor_permissions=manufacturer.permissions,
            postprocessor="generic-3axis",
        )
        checks.append(
            _check(
                "CAM approval and NC release gate",
                nc.status == "released" and nc.plan_id == plan.id and bool(nc.sha256),
                f"nc={nc.id}, sha256={nc.sha256}, bytes={len(nc.text.encode('utf-8'))}",
            )
        )

    geometry_capability, geometry_detail = _load_geometry_capability()
    fastapi_available = importlib.util.find_spec("fastapi") is not None
    checks.append(
        _check(
            "FastAPI import",
            True if fastapi_available else (False if strict_optional else None),
            "available" if fastapi_available else "install apps/api[dev] to run the HTTP adapter",
        )
    )
    if geometry_capability is None:
        checks.append(_check("CadQuery/OCCT geometry export", None, "install apps/api[dev] (and optionally [geometry])"))
        if strict_optional:
            checks[-1]["status"] = "failed"
    else:
        generate_artifacts, validate_bracket, BracketParameters = geometry_capability
        pydantic_parameters = BracketParameters.model_validate(parameters)
        report = validate_bracket(pydantic_parameters)
        checks.append(
            _check(
                "geometry validation",
                report.valid and report.metrics.get("boundingLength") == 100.0
                and report.metrics.get("boundingWidth") == 50.0
                and report.metrics.get("boundingHeight") == 40.0,
                f"engine={report.engine}, valid={report.valid}, productionReady={report.production_ready}",
            )
        )
        artifacts = generate_artifacts(pydantic_parameters, ["step", "glb"])
        by_format = {artifact.format: artifact for artifact in artifacts}
        step = by_format.get("step")
        glb = by_format.get("glb")
        export_ok = bool(step and glb and step.data.startswith(b"ISO-10303-21;") and glb.data[:4] == b"glTF")
        checks.append(
            _check(
                "STEP/GLB artifact generation",
                export_ok,
                f"stepEngine={step.engine if step else None}, glbEngine={glb.engine if glb else None}, "
                f"stepProductionReady={step.production_ready if step else None}",
            )
        )
        if strict_optional and step is not None and step.engine != "cadquery-occt":
            checks[-1]["status"] = "failed"

    failed = [item for item in checks if item["status"] == "failed"]
    summary = {
        "ok": not failed,
        "fixtureId": fixture_id,
        "acceptanceDrawingSha256": ACCEPTANCE_DRAWING_SHA256,
        "checks": checks,
    }
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    return not failed, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drawing",
        type=Path,
        default=None,
        help="path to the uploaded drawing; defaults to the supplied temp image when it exists",
    )
    parser.add_argument(
        "--fixture",
        default="bracket_support_v1",
        help="deterministic OCR fixture id (default: bracket_support_v1)",
    )
    parser.add_argument(
        "--strict-optional",
        action="store_true",
        help="fail when FastAPI/CadQuery optional packages are unavailable",
    )
    args = parser.parse_args(argv)
    drawing = args.drawing or _find_default_drawing()
    if args.drawing and not args.drawing.exists():
        parser.error(f"drawing does not exist: {args.drawing}")
    ok, _summary = run_check(
        drawing=drawing,
        fixture_id=args.fixture,
        strict_optional=args.strict_optional,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
