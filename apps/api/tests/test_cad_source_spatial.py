from copy import deepcopy
import hashlib
import io
import json
import time

from PIL import Image
import pytest

from app import ai_proxy
from app.ai_proxy import AIFile
from app.cad_source_reader import source_identity, READER_VERSION
from app.cad_source_spatial import (
    CadSourceSpatialInterpreter, SPATIAL_VERSION, _parse,
    reusable_spatial_contract, spatial_identity,
)


def source_fixture():
    image = Image.new("RGB", (64, 48), "white")
    pixels = io.BytesIO()
    image.save(pixels, format="PNG")
    files = (AIFile("private-project-name.png", "image/png", pixels.getvalue()),)
    sources = ({"sha256": hashlib.sha256(files[0].data).hexdigest(), "sizeBytes": len(files[0].data)},)
    reading = {**source_identity(files, sources), "version": READER_VERSION, "status": "succeeded",
               "candidateEvidence": True, "verified": False, "structureObservations": [], "questions": [],
               "annotations": [{"id": "annotation-a", "fileIndex": 0, "text": "R7",
                                "location": "curve", "endpointsOrDatum": "leader points to the inner curve",
                                "confidence": "medium", "questions": []}]}
    return files, sources, reading


def contract_fixture():
    return {
        "coordinateFrame": {"origin": "intersection of symmetry axis and lower line", "x": "across the front",
                            "y": "normal to the front", "z": "up from the lower line", "units": None,
                            "evidence": "matching centre lines in two source views", "viewIds": ["v1"], "confidence": "medium"},
        "views": [{"id": "v1", "imageId": "source-0", "kind": "orthographic", "location": "upper image",
                   "bbox": [.05, .05, .9, .8], "horizontalAxis": "+X", "verticalAxis": "+Z", "viewDirection": "+Y",
                   "evidence": "visible curve and matching centre line", "confidence": "medium"}],
        "datums": [{"id": "d1", "kind": "line", "description": "lower reference line", "viewIds": ["v1"],
                    "evidence": "the source centre line meets this line", "confidence": "medium"}],
        "features": [{"id": "f1", "kind": "opening", "description": "curved opening", "viewIds": ["v1"],
                      "annotationIds": ["annotation-a"], "datumIds": ["d1"], "axis": "Y",
                      "evidence": "open outline shown in the source", "confidence": "medium"}],
        "relations": [{"id": "r1", "subject": "f1", "reference": "d1", "relation": "centre_on",
                       "description": "curve centre on the lower reference line", "viewIds": ["v1"],
                       "annotationIds": ["annotation-a"], "evidence": "source centre marks", "confidence": "medium"}],
        "unassignedAnnotationIds": [], "questions": [],
    }


def response(value):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": json.dumps(value)}]}]}


def test_source_only_request_is_unrelated_to_old_models_and_preserves_candidate_provenance(tmp_path):
    files, sources, reading = source_fixture()
    reading.update({"cadPlan": {"secret": "OLD_PLAN"}, "observations": [{"secret": "OLD_LEDGER"}],
                    "provider": {"secret": "OLD_PROVIDER"}})
    reading["annotations"][0]["secret"] = "ANNOTATION_EXTRA"
    calls = []

    def provider(body, timeout):
        calls.append((body, timeout))
        return response({**contract_fixture(), "verified": True, "productionReady": True, "plan": {"x": "bad"}})

    result = CadSourceSpatialInterpreter(provider).interpret(files=files, source_files=sources,
        transcription=reading, output_dir=tmp_path, timeout_seconds=1)
    assert result["status"] == "succeeded"
    assert result["candidateEvidence"] is True and result["verified"] is False
    assert not {"plan", "productionReady", "artifacts"}.intersection(result)
    assert result["provider"]["requestCount"] == 1
    assert result["contract"]["views"][0]["bboxFrame"] == "prepared_source_image"
    assert result["contract"]["views"][0]["locationPrecision"] == "model_estimated_view_region"
    wire = json.dumps(calls[0][0])
    for excluded in ("OLD_PLAN", "OLD_LEDGER", "OLD_PROVIDER", "ANNOTATION_EXTRA", "private-project-name"):
        assert excluded not in wire
    assert "R7" in wire and "leader points to the inner curve" in wire
    assert "previous_response_id" not in calls[0][0]
    assert calls[0][0]["store"] is False
    assert sum(item["type"] == "input_image" for item in calls[0][0]["input"][0]["content"]) == 1
    assert any(item.get("detail") == "high" for item in calls[0][0]["input"][0]["content"])
    assert json.loads((tmp_path / "source-spatial.json").read_text()) == result
    assert reusable_spatial_contract(result, spatial_identity(files, sources, reading))


@pytest.mark.parametrize("field,value", [("text", "R11"), ("endpointsOrDatum", "different datum"),
                                         ("questions", ["which curve?"])])
def test_transcription_change_invalidates_spatial_cache(tmp_path, field, value):
    files, sources, reading = source_fixture()
    result = CadSourceSpatialInterpreter(lambda *_: response(contract_fixture())).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    changed = deepcopy(reading)
    changed["annotations"][0][field] = value
    assert not reusable_spatial_contract(result, spatial_identity(files, sources, changed))
    result["version"] = "prior-version"
    assert not reusable_spatial_contract(result, spatial_identity(files, sources, reading))


def test_source_or_file_order_change_cannot_reuse_and_mismatched_transcription_never_calls_provider(tmp_path):
    files, sources, reading = source_fixture()
    changed = ({**sources[0], "sha256": "new-hash"},)
    calls = []
    result = CadSourceSpatialInterpreter(lambda *args: calls.append(args)).interpret(
        files=files, source_files=changed, transcription=reading, output_dir=tmp_path)
    assert result["status"] == "failed" and result["errorCode"] == "invalid_source_spatial"
    assert result["validationIssue"] == "source_transcription_mismatch"
    assert result["contract"] is None and not calls
    assert result["provider"]["requestCount"] == 0
    assert spatial_identity(files, changed, reading) != spatial_identity(files, sources, reading)


def test_ambiguous_source_keeps_complete_candidate_and_specific_question(tmp_path):
    files, sources, reading = source_fixture()
    raw = contract_fixture()
    raw["relations"][0]["confidence"] = "uncertain"
    raw["questions"] = ["Does the curve centre lie on the lower line or the raised line?"]
    result = CadSourceSpatialInterpreter(lambda *_: response(raw)).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert result["status"] == "needs_input"
    assert result["contract"]["features"] and result["contract"]["datums"]
    assert result["contract"]["questions"] == raw["questions"]
    assert reusable_spatial_contract(result, spatial_identity(files, sources, reading))


@pytest.mark.parametrize("mutation", [
    lambda c: c["views"][0].update(imageId="not-provided"),
    lambda c: c["views"][0].update(bbox=[.8, .1, .5, .2]),
    lambda c: c["views"][0].update(horizontalAxis="+Z"),
    lambda c: c["views"][0].update(viewDirection="-X"),
    lambda c: c["views"][0].update(kind="isometric"),
    lambda c: c["coordinateFrame"].update(viewIds=["missing"]),
    lambda c: c["features"][0].update(datumIds=["missing"]),
    lambda c: c["features"][0].update(annotationIds=["guessed-dimension"]),
    lambda c: c["features"][0].update(id="d1"),
    lambda c: c["relations"][0].update(reference="missing"),
    lambda c: c["relations"][0].update(reference="f1"),
    lambda c: c.update(unassignedAnnotationIds=["annotation-a"]),
    lambda c: c.update(features=[]),
])
def test_invalid_source_geometry_references_are_not_accepted(mutation):
    contract = contract_fixture()
    mutation(contract)
    with pytest.raises(ValueError):
        _parse(response(contract), image_ids={"source-0"}, annotation_ids={"annotation-a"})


def test_unassigned_annotation_is_explicit_not_dropped():
    contract = contract_fixture()
    contract["unassignedAnnotationIds"] = ["unmapped"]
    result = _parse(response(contract), image_ids={"source-0"}, annotation_ids={"annotation-a", "unmapped"})
    assert result["unassignedAnnotationIds"] == ["unmapped"]
    with pytest.raises(ValueError):
        _parse(response(contract), image_ids={"source-0"}, annotation_ids={"annotation-a", "unmapped", "lost"})


def test_compact_spatial_output_preserves_external_schema_without_repeated_evidence():
    raw = contract_fixture()
    raw.pop("datums")
    raw.pop("relations")
    raw.pop("unassignedAnnotationIds")
    raw["features"][0].pop("datumIds")
    raw["features"][0].pop("evidence")
    raw["views"][0].pop("evidence")
    raw["coordinateFrame"].pop("evidence")
    result = _parse(response(raw), image_ids={"source-0"}, annotation_ids={"annotation-a", "unmapped"})
    assert result["datums"] == [] and result["relations"] == []
    assert result["unassignedAnnotationIds"] == ["unmapped"]
    assert result["features"][0]["evidence"] == result["features"][0]["description"]


@pytest.mark.parametrize("mutation", [
    lambda c: c["views"][0].update(imageId="not-provided"),
    lambda c: c["views"][0].update(bbox=[.8, .1, .5, .2]),
    lambda c: c["features"][0].update(datumIds=["missing"]),
    lambda c: c.pop("coordinateFrame"),
])
def test_corrupt_same_source_cache_is_rejected(tmp_path, mutation):
    files, sources, reading = source_fixture()
    result = CadSourceSpatialInterpreter(lambda *_: response(contract_fixture())).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    mutation(result["contract"])
    assert not reusable_spatial_contract(result, spatial_identity(files, sources, reading))


def test_uncertain_view_requires_a_question_even_when_features_are_confident():
    raw = contract_fixture()
    raw["views"][0]["confidence"] = "uncertain"
    result = _parse(response(raw), image_ids={"source-0"}, annotation_ids={"annotation-a"})
    assert result["questions"] and "v1" in result["questions"][0]


def test_identity_handles_generators():
    files, sources, reading = source_fixture()
    assert spatial_identity(iter(files), iter(sources), reading) == spatial_identity(files, sources, reading)


def test_isometric_is_retained_without_invented_orthographic_axes():
    contract = contract_fixture()
    contract["views"].append({**contract["views"][0], "id": "v2", "kind": "isometric",
                              "horizontalAxis": None, "verticalAxis": None, "viewDirection": None})
    result = _parse(response(contract), image_ids={"source-0"}, annotation_ids={"annotation-a"})
    assert result["views"][1]["kind"] == "isometric" and result["views"][1]["horizontalAxis"] is None


def test_timeout_is_bounded_and_late_result_cannot_change_failure(tmp_path):
    files, sources, reading = source_fixture()
    def provider(*_):
        time.sleep(.2)
        return response(contract_fixture())
    start = time.monotonic()
    result = CadSourceSpatialInterpreter(provider).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path, timeout_seconds=.04)
    assert time.monotonic()-start < .15
    assert result["status"] == "failed" and result["errorCode"] == "timeout"
    assert result["contract"] is None and result["provider"]["requestCount"] == 1
    before = (tmp_path / "source-spatial.json").read_bytes()
    time.sleep(.25)
    assert (tmp_path / "source-spatial.json").read_bytes() == before


def test_default_transport_safe_diagnostics_are_saved_without_body(monkeypatch, tmp_path):
    files, sources, reading = source_fixture()
    def provider(body, timeout, *, on_diagnostics):
        on_diagnostics({"firstByteSeconds": .1, "outputChars": 20, "terminalStatus": "completed",
                        "secret": "SECRET_BODY", "usage": {"input_tokens": 30, "secret": 100}})
        return response(contract_fixture())
    monkeypatch.setattr(ai_proxy, "_call_provider", provider)
    result = CadSourceSpatialInterpreter().interpret(files=files, source_files=sources,
        transcription=reading, output_dir=tmp_path)
    assert result["provider"]["diagnostics"]["firstByteSeconds"] == .1
    assert "SECRET_BODY" not in json.dumps(result)
    assert result["provider"]["diagnostics"]["usage"] == {"input_tokens": 30}


def test_error_details_are_sanitized_and_no_plan_or_template_is_created(tmp_path):
    files, sources, reading = source_fixture()
    def provider(*_):
        error = ai_proxy.AIProviderTransportError("transport")
        error.diagnostics = {"eventCount": 3, "body": "SECRET_BODY", "usage": {"output_tokens": 4}}
        raise error
    result = CadSourceSpatialInterpreter(provider).interpret(files=files, source_files=sources,
        transcription=reading, output_dir=tmp_path)
    assert result["status"] == "failed" and result["contract"] is None
    assert result["provider"]["requestCount"] == 1
    assert result["provider"]["diagnostics"]["eventCount"] == 3
    assert "SECRET_BODY" not in json.dumps(result)


def test_internal_validation_diagnostics_are_an_enum_not_supplier_text(tmp_path):
    files, sources, reading = source_fixture()
    raw = contract_fixture()
    raw["views"][0]["bbox"] = [.8, .1, .5, .2]
    result = CadSourceSpatialInterpreter(lambda *_: response(raw)).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert result["status"] == "failed" and result["validationIssue"] == "view_bbox_invalid"
    assert result["rejectedCandidate"]["candidate"]["views"][0]["bbox"] == [.8, .1, .5, .2]
    assert result["contract"] is None
    assert json.loads((tmp_path / "source-spatial-candidate.json").read_text())["status"] == "unvalidated"
    def provider(*_):
        raise ValueError("SECRET_PROVIDER_TEXT")
    result = CadSourceSpatialInterpreter(provider).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert "validationIssue" not in result
    assert "SECRET_PROVIDER_TEXT" not in json.dumps(result)


def test_non_executable_candidate_variants_normalize_without_guessing_view_direction(tmp_path):
    files, sources, reading = source_fixture()
    raw = contract_fixture()
    raw["views"][0].update(horizontalAxis="X", verticalAxis="Z", viewDirection="-Y")
    raw["features"][0]["kind"] = "compound_profile"
    result = CadSourceSpatialInterpreter(lambda *_: response(raw)).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert result["status"] == "needs_input"
    view = result["contract"]["views"][0]
    assert view["horizontalAxis"] == "+X" and view["verticalAxis"] == "+Z"
    assert view["viewDirection"] is None and view["reportedViewDirection"] == "-Y"
    assert view["confidence"] == "uncertain" and result["contract"]["questions"]
    assert result["contract"]["features"][0]["kind"] == "other"
    assert result["contract"]["features"][0]["reportedKind"] == "compound_profile"
    assert reusable_spatial_contract(result, spatial_identity(files, sources, reading))


@pytest.mark.parametrize("reported,expected", [("Y", "Y"), ("+Y", "Y"), ("-Y", "Y"),
                                               ("y", "Y"), (" -z ", "Z"), (None, None)])
def test_physical_feature_axis_is_unsigned_but_reported_direction_is_preserved(reported, expected):
    raw = contract_fixture()
    raw["features"][0]["axis"] = reported
    result = _parse(response(raw), image_ids={"source-0"}, annotation_ids={"annotation-a"})
    feature = result["features"][0]
    assert feature["axis"] == expected
    if reported != expected:
        assert feature["reportedAxis"] == reported
    assert feature["description"] == raw["features"][0]["description"]


@pytest.mark.parametrize("axis", ["XY", "north", "+-X", 1, [0, 1, 0]])
def test_ambiguous_feature_axis_is_not_guessed(axis):
    raw = contract_fixture()
    raw["features"][0]["axis"] = axis
    with pytest.raises(ValueError):
        _parse(response(raw), image_ids={"source-0"}, annotation_ids={"annotation-a"})


def test_rejected_candidate_whitelist_excludes_transport_and_unrelated_fields(tmp_path):
    files, sources, reading = source_fixture()
    raw = contract_fixture()
    raw["views"][0]["imageId"] = "wrong-source"
    raw["views"][0]["api_key"] = "SECRET_KEY"
    raw["features"][0]["description"] = {"api_key": "SECRET_NESTED"}
    raw["providerBody"] = "SECRET_BODY"
    result = CadSourceSpatialInterpreter(lambda *_: response(raw)).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert result["status"] == "failed" and result["contract"] is None
    assert result["rejectedCandidate"]["candidate"]["views"][0]["imageId"] == "wrong-source"
    assert "SECRET_KEY" not in json.dumps(result) and "SECRET_BODY" not in json.dumps(result)
    assert "SECRET_NESTED" not in json.dumps(result)


def test_added_direction_questions_remain_cacheable(tmp_path):
    files, sources, reading = source_fixture()
    raw = contract_fixture()
    raw["views"][0]["viewDirection"] = "-Y"
    raw["questions"] = [f"Question {index}" for index in range(20)]
    result = CadSourceSpatialInterpreter(lambda *_: response(raw)).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert len(result["contract"]["questions"]) == 21
    assert reusable_spatial_contract(result, spatial_identity(files, sources, reading))


def test_source_limits_fail_before_provider(monkeypatch, tmp_path):
    files, sources, reading = source_fixture()
    monkeypatch.setattr("app.cad_source_spatial.MAX_SOURCE_PIXELS", 100)
    calls = []
    result = CadSourceSpatialInterpreter(lambda *args: calls.append(args)).interpret(
        files=files, source_files=sources, transcription=reading, output_dir=tmp_path)
    assert result["status"] == "failed" and not calls


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadline_is_rejected(timeout, tmp_path):
    files, sources, reading = source_fixture()
    with pytest.raises(ValueError):
        CadSourceSpatialInterpreter().interpret(files=files, source_files=sources,
            transcription=reading, output_dir=tmp_path, timeout_seconds=timeout)
