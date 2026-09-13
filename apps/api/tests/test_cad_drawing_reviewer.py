"""Review isolation and conservative parsing; no provider requests."""
import copy
import io
import json

import pytest
from PIL import Image

from app import ai_proxy, cad_drawing_reviewer as reviewer
from app.ai_proxy import AIFile


def raster(name="source.png"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(output, format="PNG")
    return AIFile(name, "image/png", output.getvalue())


def evidence(view="front", **values):
    return {"sourceImageId": "source-0", "sourceLocation": "原图左侧轮廓",
            "modelImageId": f"projection-{view}", "modelLocation": "模型对应轮廓",
            "finding": "对照可见的外轮廓和开口", "confidence": "high", **values}


def response(**values):
    return {"status": "consistent", "observations": [evidence(view) for view in ("front", "top", "right")],
            "differences": [], "questions": [], **values}


def inspection():
    return {"solidCount": 1, "volumeMm3": 420, "bbox": {"min": [0, 0, 0], "max": [10, 7, 6], "size": [10, 7, 6]},
            "cylinders": [{"radius": 2, "diameter": 4, "origin": [0, 0, 0], "axis": [0, 0, 1], "area": 20}],
            "raySections": {"modelProbe": {"origin": [0, 0, 0], "direction": [0, 0, 1],
                                            "intervals": [[0, 6]], "fullMaterialIntervals": [[0, 6]]}}}


class Provider:
    def __init__(self, value=None, error=None):
        self.value = response() if value is None else value
        self.error = error
        self.requests = []

    def __call__(self, body, timeout, *, on_wait=None, on_diagnostics=None):
        self.requests.append((body, timeout))
        if on_wait:
            on_wait()
        if on_diagnostics:
            on_diagnostics({"eventCount": 4, "httpStatus": 200, "protocol": "sse", "secret": "never_copy"})
        if self.error:
            raise self.error
        return {"status": "completed", "output_text": json.dumps(self.value),
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                          "output_tokens_details": {"reasoning_tokens": 5}, "secret": "never_copy"}}


def review(provider=None, **changes):
    args = {"message": "按附件制作支架", "source_files": [raster()],
            "projection_files": {view: raster(f"generated-{view}.png") for view in ("front", "top", "right")},
            "inspection": inspection(), "provider_call": provider or Provider(), "timeout_seconds": 60}
    args.update(changes)
    return reviewer.review_drawing(**args)


def test_request_has_only_pixels_user_instruction_actual_numbers_and_coordinate_convention():
    secret = "PLAN_TRANSCRIPTION_OR_SELF_APPROVAL_MUST_NOT_ENTER"
    actual = inspection()
    actual.update({"plan": secret, "acceptance": {"expected": secret, "status": "passed"}, "valid": True,
                   "productionReady": True, "drawingAgreement": "passed", "scope": secret, "sourceTranscription": secret})
    actual["cylinders"][0]["comment"] = secret
    actual["raySections"][secret] = {"origin": [1, 2, 3], "direction": [0, 1, 0], "label": secret,
                                      "expected": [42], "passed": True, "fullMaterialIntervals": [[0, 2]]}
    provider = Provider()
    result = review(provider, inspection=actual, source_files=[raster(secret+".png")])
    assert result["status"] == "consistent"
    body, timeout = provider.requests[0]
    assert 0 < timeout <= 60
    assert body["model"] == ai_proxy._model() and body["reasoning"]["effort"] == reviewer._review_effort()
    assert len(body["input"]) == 1 and body["store"] is False
    content = body["input"][0]["content"]
    assert len([part for part in content if part["type"] == "input_image"]) == 4
    context = json.loads(content[0]["text"])
    assert set(context) == {"userMessage", "actualMeasurements", "coordinateConvention"}
    assert context["actualMeasurements"]["bbox"]["size"] == [10, 7, 6]
    assert isinstance(context["actualMeasurements"]["raySections"], list)
    serialized = json.dumps(body)
    for forbidden in (secret, "acceptance", "productionReady", "drawingAgreement", "cad-plan-v1", "execute_plan", "sourceTranscription"):
        assert forbidden not in serialized
    assert "圆弧中心" in body["instructions"] and "缺失/额外材料" in body["instructions"]
    assert "never_copy" not in json.dumps(result)
    assert "base64," not in json.dumps(result)
    assert result["humanConfirmed"] is False


def test_iterable_generated_projection_files_are_compatible_and_hashes_tie_review_to_inputs():
    first = review(projection_files=[raster("generated-front.png"), raster("generated-top.png"), raster("generated-right.png")])
    assert first["status"] == "consistent"
    assert [item.get("view") for item in first["images"][1:]] == ["front", "top", "right"]
    updated = inspection()
    updated["bbox"]["size"][0] = 11
    second = review(inspection=updated)
    assert first["inputFingerprint"] != second["inputFingerprint"]


def test_spatial_diagnostic_is_separate_and_can_explain_missing_material():
    finding = evidence(modelImageId="spatial-isometric", finding="A source connection is absent in the spatial view", repair="Restore the source connection")
    provider = Provider(response(status="mismatch", differences=[finding]))
    result = review(provider, spatial_files=[raster("generated-isometric.png")])
    assert result["status"] == "mismatch"
    assert result["differences"][0]["modelImageId"] == "spatial-isometric"
    assert len([item for item in result["images"] if item["kind"] == "actual_projection"]) == 3
    spatial = next(item for item in result["images"] if item["kind"] == "actual_spatial_projection")
    assert spatial["pixelRegistrationEligible"] is False and spatial["orthographicEngineeringView"] is False
    assert spatial["viewDirection"] == [1, -1, 1]
    assert "不是正交工程图" in provider.requests[0][0]["instructions"]


def test_spatial_evidence_does_not_replace_the_required_orthographic_review():
    provider = Provider(response(observations=[evidence(modelImageId="spatial-isometric")]))
    result = review(provider, spatial_files=[raster("generated-isometric.png")])
    assert result["status"] == "uncertain"
    assert review(spatial_files=[raster("generated-isometric.png")])["status"] == "consistent"


@pytest.mark.parametrize("files", [[raster("arbitrary.png")], [raster("generated-isometric.png")]*2])
def test_unsupported_spatial_attachments_fail_before_calling_provider(files):
    provider = Provider()
    result = review(provider, spatial_files=files)
    assert result["status"] == "uncertain" and result["errorCode"]
    assert provider.requests == []


def test_contour_overlay_adds_only_numeric_pixel_evidence_not_comparison_paths_or_planner_claims():
    secret = "PRIVATE_PATH_OR_PLAN_MUST_NOT_ENTER"
    comparison = {"plan": secret, "artifacts": [{"path": secret}], "views": [
        {"view": "front", "status": "mismatch", "reliability": "high", "sourceRegion": [.1, .1, .8, .7],
         "metrics": {"unsupportedFraction": .2, "p90DistancePixels": 12, "plan": secret}, "finding": secret}]}
    provider = Provider()
    result = review(provider, comparison=comparison, comparison_files=[raster("comparison-front.png")])
    assert result["status"] == "consistent"
    body = provider.requests[0][0]
    assert secret not in json.dumps(body)
    content = body["input"][0]["content"]
    assert len([item for item in content if item["type"] == "input_image"]) == 5
    data = json.loads(content[0]["text"])
    assert data["pixelComparison"]["views"][0]["metrics"]["unsupportedFraction"] == .2
    assert result["images"][-1]["kind"] == "contour_overlay"
    assert "没有红色也不能证明" in body["instructions"]


def test_specific_difference_and_repair_are_preserved():
    difference = evidence(repair="让该开口贯穿所示材料", finding="原图开口轮廓延续，模型对应位置被材料封闭")
    result = review(Provider(response(status="mismatch", differences=[difference])))
    assert result["status"] == "mismatch"
    assert result["differences"] == [difference]


@pytest.mark.parametrize("values,expected", [
    ({"questions": ["该圆弧基准线不清晰"]}, "uncertain"),
    ({"observations": [evidence()]}, "uncertain"),
    ({"observations": []}, "uncertain"),
    ({"observations": [evidence(view, confidence="low") for view in ("front", "top", "right")]}, "uncertain"),
    ({"differences": [evidence(repair="修正轮廓", confidence="high")]}, "mismatch"),
    ({"differences": [evidence(repair="需要更清晰局部", confidence="uncertain")]}, "uncertain"),
    ({"status": "uncertain"}, "uncertain"),
    ({"status": "uncertain", "differences": [evidence(repair="需要核查此处基准")]}, "uncertain"),
])
def test_consistency_is_not_inferred_despite_unresolved_or_incomplete_evidence(values, expected):
    result = review(Provider(response(**values)))
    assert result["status"] == expected


@pytest.mark.parametrize("values", [
    {"status": "passed"}, {"status": "failed"}, {"status": "mismatch"},
    {"plan": {}}, {"questions": "all clear"}, {"observations": "all clear"},
    {"observations": [evidence(sourceImageId="source-not-provided")]},
    {"observations": [evidence(modelImageId="made-up-view")]},
    {"observations": [evidence(confidence="confirmed")]},
    {"differences": [evidence()]},
])
def test_invalid_output_is_uncertain_with_stable_error_not_fake_success(values):
    result = review(Provider(response(**values)))
    assert result["status"] == "uncertain"
    assert result["errorCode"] == "invalid_drawing_review"
    assert result["questions"]


@pytest.mark.parametrize("error,code", [
    (ai_proxy.AIProviderTransportError("timeout"), "timeout"),
    (ai_proxy.AIProviderHTTPError(503), "http_503"),
    (RuntimeError("private-key-or-server-internals"), "invalid_drawing_review"),
])
def test_provider_failures_are_not_retried_or_exposed(error, code):
    provider = Provider(error=error)
    result = review(provider)
    assert result["status"] == "uncertain" and result["errorCode"] == code
    assert len(provider.requests) == 1
    assert "private-key" not in json.dumps(result)


def test_only_final_message_is_parsed_and_multiple_actions_are_rejected():
    def completed(body, timeout, **kwargs):
        return {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": json.dumps(response())}]},
            {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": json.dumps(response(status="uncertain", questions=["缺少可辨认的开口边界"]))}]}]}
    assert review(completed)["status"] == "uncertain"
    def ambiguous(body, timeout, **kwargs):
        return {"output_text": json.dumps(response())+json.dumps(response())}
    result = review(ambiguous)
    assert result["status"] == "uncertain" and "errorCode" in result


@pytest.mark.parametrize("arguments", [
    {"source_files": []}, {"source_files": [AIFile("drawing.pdf", "application/pdf", b"%PDF-unprepared")]},
    {"projection_files": []}, {"projection_files": {"unknown": raster()}},
    {"inspection": {}}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
])
def test_invalid_or_missing_inputs_never_call_provider(arguments):
    provider = Provider()
    result = review(provider, **arguments)
    assert result["status"] == "uncertain" and provider.requests == []


def test_pixel_limit_is_checked_before_provider_image_conversion(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_IMAGE_PIXELS", 2)
    def forbidden(*args, **kwargs):
        raise AssertionError("must reject before image conversion")
    monkeypatch.setattr(ai_proxy, "_attachment_content", forbidden)
    provider = Provider()
    result = review(provider)
    assert result["status"] == "uncertain" and provider.requests == []


def test_on_wait_and_safe_numeric_provider_metrics_are_forwarded():
    waits = []
    result = review(on_wait=lambda: waits.append(True))
    assert waits == [True]
    metrics = result["providerMetrics"]
    assert metrics["requestCount"] == 1
    assert metrics["usage"] == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30, "reasoning_tokens": 5}
    assert metrics["diagnostics"] == {"eventCount": 4, "httpStatus": 200, "protocol": "sse"}


def test_inputs_are_never_mutated():
    actual = inspection()
    before = copy.deepcopy(actual)
    review(inspection=actual)
    assert actual == before


class SequenceProvider:
    def __init__(self, *outcomes):
        self.outcomes = outcomes
        self.requests = []

    def __call__(self, body, timeout, **kwargs):
        self.requests.append((copy.deepcopy(body), timeout))
        outcome = self.outcomes[len(self.requests) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def envelope(value):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "phase": None,
            "channel": None, "content": [{"type": "output_text", "text": json.dumps(value)}]}]}


def test_schema_enumerates_only_actual_images_and_is_compatible_with_codex(tmp_path):
    from app.cad_codex_provider import _prepare

    provider = Provider()
    result = review(provider, spatial_files=[raster("generated-isometric.png")],
                    comparison_files=[raster("comparison-front.png")])
    assert result["status"] == "consistent"
    body = provider.requests[0][0]
    format_ = body["text"]["format"]
    assert format_["type"] == "json_schema" and format_["strict"] is True
    assert body["max_output_tokens"] == reviewer.MAX_OUTPUT_TOKENS
    schema = format_["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"status", "observations", "differences", "questions"}
    for field in ("observations", "differences"):
        item = schema["properties"][field]["items"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(item["properties"])
        assert item["properties"]["sourceImageId"]["enum"] == ["source-0"]
        assert set(item["properties"]["modelImageId"]["enum"]) == {"projection-front", "projection-top", "projection-right", "spatial-isometric"}
    _prompt, images, _effort, schema_path = _prepare(body, tmp_path)
    assert len(images) == 6
    assert json.loads(schema_path.read_text()) == schema


def test_invalid_json_retries_once_with_identical_independent_pixels_and_no_old_answer():
    malformed = {"status": "completed", "output_text": '{"PRIVATE_FAILED_RESPONSE": "invalid\\q"}'}
    provider = SequenceProvider(malformed, envelope(response()))
    actual = inspection()
    actual["plan"] = "PRIVATE_PLANNER_CLAIM"
    result = review(provider, inspection=actual)
    assert result["status"] == "consistent" and "errorCode" not in result
    assert result["humanConfirmed"] is False
    assert result["providerMetrics"]["requestCount"] == 2
    first, second = (item[0] for item in provider.requests)
    assert first["input"] == second["input"]
    assert first["text"] == second["text"]
    assert all(item["store"] is False for item in (first, second))
    assert "previous_response_id" not in second
    assert "PRIVATE_FAILED_RESPONSE" not in json.dumps(second) + json.dumps(result)
    assert "PRIVATE_PLANNER_CLAIM" not in json.dumps(second)
    assert result["providerMetrics"]["attempts"][0]["validationCode"] == "invalid_final_json"


def test_invalid_stream_retries_as_non_stream_json_and_schema_rejection_uses_compatibility_format():
    provider = SequenceProvider(ai_proxy.AIProxyError("AI provider returned an invalid streaming event"), envelope(response()))
    result = review(provider)
    assert result["status"] == "consistent"
    assert [body["stream"] for body, _ in provider.requests] == [True, False]
    assert all(body["text"]["format"]["type"] == "json_schema" for body, _ in provider.requests)
    provider = SequenceProvider(ai_proxy.AIProviderHTTPError(400), envelope(response()))
    result = review(provider)
    assert result["status"] == "consistent"
    assert [body["text"]["format"]["type"] for body, _ in provider.requests] == ["json_schema", "json_object"]
    assert provider.requests[0][0]["input"] == provider.requests[1][0]["input"]
    assert '"projection-front"' in provider.requests[1][0]["instructions"]


@pytest.mark.parametrize("value,code", [
    (response(extra="PRIVATE_FIELD"), "invalid_review_fields"),
    (response(observations=[evidence(modelImageId="comparison-front")]), "unknown_evidence_image"),
    (response(differences=[evidence()]), "invalid_evidence_fields"),
    (response(status="mismatch"), "missing_difference_evidence"),
    (response(observations=[evidence(finding="x" * 701)]), "invalid_evidence_text"),
])
def test_two_malformed_results_never_bypass_evidence_validation(value, code):
    provider = SequenceProvider(envelope(value), envelope(value))
    result = review(provider)
    assert len(provider.requests) == 2
    assert result["status"] == "uncertain"
    assert result["errorCode"] == "invalid_drawing_review"
    assert result["validationCode"] == code
    assert result["observations"] == result["differences"] == []
    assert "PRIVATE_FIELD" not in json.dumps(result)


@pytest.mark.parametrize("value,expected", [
    (response(status="mismatch", differences=[evidence(repair="保持原图要求，修正缺失材料")]), "mismatch"),
    (response(questions=["需要核对未看清的底部开口"]), "uncertain"),
    (response(observations=[evidence()]), "uncertain"),
])
def test_valid_mismatch_or_uncertainty_is_never_retried_for_a_more_favorable_answer(value, expected):
    provider = SequenceProvider(envelope(value))
    result = review(provider)
    assert result["status"] == expected
    assert len(provider.requests) == 1
    assert "errorCode" not in result


def test_missing_final_and_incomplete_final_have_safe_distinct_codes_and_no_retry():
    provider = SequenceProvider({"status": "completed", "output": [{"type": "reasoning"}], "output_text": json.dumps(response())})
    result = review(provider)
    assert result["status"] == "uncertain" and result["validationCode"] == "missing_final_output"
    assert len(provider.requests) == 1
    provider = SequenceProvider({**envelope(response()), "status": "incomplete"})
    result = review(provider)
    assert result["validationCode"] == "incomplete_final_response"
    assert result["errorCode"] == "incomplete"
    assert len(provider.requests) == 1


def test_retry_uses_only_remaining_wall_budget_and_rejects_late_success(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reviewer.time, "monotonic", lambda: clock[0])
    calls = []

    def provider(body, timeout, **kwargs):
        calls.append(timeout)
        clock[0] += 7 if len(calls) == 1 else 14
        return {"status": "completed", "output_text": "{"} if len(calls) == 1 else envelope(response())

    result = review(provider, timeout_seconds=20)
    assert calls == [20, 13]
    assert result["status"] == "uncertain" and result["errorCode"] == "timeout"
    assert result["observations"] == []


def test_short_remaining_budget_does_not_start_another_review(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reviewer.time, "monotonic", lambda: clock[0])
    calls = []

    def provider(body, timeout, **kwargs):
        calls.append(timeout)
        clock[0] += 28
        return {"status": "completed", "output_text": "{"}

    result = review(provider, timeout_seconds=31)
    assert calls == [31]
    assert result["errorCode"] == "invalid_json" and result["status"] == "uncertain"


def test_image_preparation_is_included_in_review_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reviewer.time, "monotonic", lambda: clock[0])
    original = reviewer._image_content

    def prepare(*args):
        value = original(*args)
        clock[0] += 4
        return value

    monkeypatch.setattr(reviewer, "_image_content", prepare)
    provider = Provider()
    result = review(provider, timeout_seconds=3)
    assert provider.requests == []
    assert result["errorCode"] == "timeout"


@pytest.mark.parametrize("configured,expected", [(None, "medium"), (" LOW ", "low"), ("high", "high"),
    ("xhigh", "xhigh"), ("max", "max"), ("ultra", "ultra"), ("invalid", "medium"), ("", "medium")])
def test_review_effort_is_independent_configured_and_validated(monkeypatch, configured, expected):
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", "high")
    if configured is None:
        monkeypatch.delenv("JOYNIU_CAD_REVIEW_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("JOYNIU_CAD_REVIEW_REASONING_EFFORT", configured)
    provider = Provider()
    result = review(provider)
    assert result["status"] == "consistent"
    assert provider.requests[0][0]["reasoning"]["effort"] == expected
    assert result["providerMetrics"]["attempts"][0]["reasoningEffort"] == expected
    assert ai_proxy._reasoning_effort() == "high"
