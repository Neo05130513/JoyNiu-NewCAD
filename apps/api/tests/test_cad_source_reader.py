from __future__ import annotations

import io
import json
import threading
import time

import pytest
from app import ai_proxy
from app.ai_proxy import AIFile, AIProxy, AIProviderHTTPError
from app.cad_agent import CadAgentService
from app.cad_source_reader import CadSourceReader, reusable_transcription, source_identity, _reading_effort
from tests.test_cad_agent import Executor, Provider, StubSourceReader, StubSpatialInterpreter, context, execute, finish, plan, raster, record


def transcription():
    return {"annotations": [{"imageId": "source-0-original", "text": "R23", "view": "section view",
                              "location": "beside the circular contour", "bbox": [0.1, 0.2, 0.3, 0.4],
                              "endpointsOrDatum": "Leader touches an arc; its centre datum is not visible.",
                              "confidence": "medium", "questions": ["The radius centre datum is unclear."]}],
            "structureObservations": [{"imageId": "source-0-original", "text": "A circular contour is visible.",
                                       "view": "", "location": "central region", "bbox": None,
                                       "confidence": "high", "questions": []}], "questions": []}


def inputs(files=None):
    files = tuple(files or [raster(200, 100)])
    return files, [ai_proxy._attachment_metadata(item) for item in files]


def test_reader_request_is_independent_compact_visual_input_without_fake_regions(tmp_path):
    files, sources = inputs()
    provider = Provider([transcription()])
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "succeeded"
    assert result["candidateEvidence"] is True and result["verified"] is False
    assert result["annotations"][0]["text"] == "R23"
    assert result["annotations"][0]["bbox"] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert result["annotations"][0]["bboxFrame"] == "prepared_source_image"
    assert result["annotations"][0]["fileIndex"] == 0
    assert result["annotations"][0]["endpointsOrDatum"].endswith("not visible.")
    body, timeout = provider.requests[0]
    serialized = json.dumps(body)
    for forbidden in ("cad-plan-v1", "execute_plan", "currentPlan", "parameterPatch", "arched_clevis", "bracket_support", "sourceObservations"):
        assert forbidden not in serialized
    assert body["model"] == ai_proxy._model()
    assert body["reasoning"]["effort"] == _reading_effort()
    assert 170 < timeout <= 180
    assert "不同位置的相同文字分别保留" in body["instructions"]
    assert "不要求文字坐标框" in body["instructions"]
    assert len(body["instructions"]) < 1000
    content = body["input"][0]["content"]
    assert len([item for item in content if item["type"] == "input_image"]) == 1
    metadata = [json.loads(item["text"]) for item in content[1:] if item["type"] == "input_text"]
    assert metadata[0]["sourceType"] == "original_prepared_image"
    assert all(item["viewDetected"] is False for item in metadata)
    saved = json.loads((tmp_path / "source-transcription.json").read_text())
    assert saved == result
    assert "base64," not in json.dumps(result)
    assert "data:image" not in json.dumps(result)


@pytest.mark.parametrize("global_effort,stage_effort,expected", [
    ("high", None, "medium"), ("low", None, "low"), ("ultra", None, "medium"),
    ("high", "high", "high"), ("low", "xhigh", "xhigh"), ("high", "invalid", "medium"),
])
def test_transcription_has_an_explicit_independent_reasoning_setting(monkeypatch, global_effort, stage_effort, expected):
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", global_effort)
    if stage_effort is None:
        monkeypatch.delenv("JOYNIU_CAD_SOURCE_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("JOYNIU_CAD_SOURCE_REASONING_EFFORT", stage_effort)
    assert _reading_effort() == expected


def separated_ink():
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (700, 500), "white")
    draw = ImageDraw.Draw(image)
    for box in ((30, 25, 260, 160), (440, 45, 660, 190), (60, 345, 270, 465), (460, 325, 630, 455)):
        draw.rectangle(box, outline="black", width=3)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return AIFile("unknown.png", "image/png", output.getvalue())


def test_ink_regions_follow_actual_blank_bands_not_fixed_quadrants_and_keep_provenance(tmp_path):
    calls = []
    def reply(body, timeout):
        content = body["input"][0]["content"]
        metadata = [json.loads(item["text"]) for item in content[1:] if item["type"] == "input_text"]
        calls.append(metadata)
        return {"output_text": json.dumps({"annotations": [{"text": "R23", "location": "near the outline", "endpointsOrDatum": "visible arc leader", "confidence": "high"}], "questions": []})}
    files, sources = inputs([separated_ink()])
    result = CadSourceReader(provider_call=reply).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "succeeded" and len(calls) == 4
    assert result["provider"]["requestCount"] == result["provider"]["plannedRequestCount"] == 4
    assert all(len(items) == 2 and items[0]["sourceType"] == "original_prepared_image" and items[1]["sourceType"] == "content_region" for items in calls)
    assert all(items[1]["viewDetected"] is False for items in calls)
    assert len({tuple(items[1]["crop"]) for items in calls}) == 4
    assert all(annotation["bbox"] is None and annotation["locationPrecision"] == "content_region_and_text_description" for annotation in result["annotations"])
    assert [a["id"] for a in result["annotations"]] == ["annotation-1", "annotation-2", "annotation-3", "annotation-4"]
    assert all(a["sourceRegion"] != [0, 0, 1, 1] for a in result["annotations"])


def test_region_box_mapping_is_explicit_without_claiming_exact_letter_coordinates():
    from app.cad_source_reader import _parse
    focus = {"imageId": "region", "fileIndex": 0, "preparedSha256": "test", "crop": [.4, .5, .3, .4]}
    payload = {"output_text": json.dumps({"annotations": [{"text": "12", "bbox": [.1, .2, .3, .4]}]})}
    result = _parse(payload, [focus], focus_image=focus)
    assert result["annotations"][0]["bbox"] == pytest.approx([.43, .58, .09, .16])


def test_single_sparse_view_gets_a_focus_without_losing_external_annotations(tmp_path):
    from PIL import Image, ImageDraw
    from app.cad_source_reader import _ink_regions
    image = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((220, 250, 650, 330), outline="black", width=3)
    # Outside the part, but still connected by its dimension extension line.
    draw.line((220, 235, 180, 235, 180, 350, 650, 350, 650, 330), fill="black", width=3)
    boxes = _ink_regions(image)
    assert len(boxes) == 1
    x0, y0, x1, y1 = boxes[0]
    assert x0 <= 180 and y0 <= 235 and x1 > 650 and y1 > 350
    assert (x1-x0)*(y1-y0) < image.width*image.height*.65
    output = io.BytesIO()
    image.save(output, format="PNG")
    files, sources = inputs([AIFile("arbitrary-name.png", "image/png", output.getvalue())])
    provider = Provider([{"annotations": [{"text": "visible annotation", "confidence": "medium"}]}])
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "succeeded"
    content = provider.requests[0][0]["input"][0]["content"]
    assert len([item for item in content if item["type"] == "input_image"]) == 2
    metadata = [json.loads(item["text"]) for item in content[1:] if item["type"] == "input_text"]
    assert metadata[0]["crop"] == [0, 0, 1, 1]
    assert metadata[1]["sourceType"] == "content_region" and metadata[1]["viewDetected"] is False
    assert result["annotations"][0]["sourceRegion"] == metadata[1]["crop"]


def test_dense_or_tiny_single_ink_region_does_not_create_a_fake_focus():
    from PIL import Image, ImageDraw
    from app.cad_source_reader import _ink_regions
    dense = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(dense).rectangle((2, 2, 397, 297), outline="black", width=3)
    assert _ink_regions(dense) == []
    tiny = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(tiny).rectangle((180, 140, 184, 144), fill="black")
    assert _ink_regions(tiny) == []


def test_independent_region_reads_are_bounded_parallel_and_share_one_deadline(tmp_path):
    lock = threading.Lock()
    active = peak = 0
    def reply(body, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.05)
        with lock:
            active -= 1
        return {"output_text": json.dumps({"annotations": [], "questions": []})}
    files, sources = inputs([separated_ink()])
    started = time.monotonic()
    result = CadSourceReader(provider_call=reply).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "succeeded"
    assert 1 < peak <= 4 and time.monotonic() - started < .4
    assert len(result["regionReadings"]) == 4
    assert result["annotations"] == [] and result["questions"]


def test_timeout_counts_only_started_requests_and_does_not_start_queued_regions_later(tmp_path):
    release = threading.Event()
    lock = threading.Lock()
    calls = []

    def reply(body, timeout):
        with lock:
            calls.append(timeout)
        release.wait(3)
        return {"output_text": json.dumps({"annotations": [], "questions": []})}

    files, sources = inputs([separated_ink() for _ in range(4)])
    try:
        result = CadSourceReader(provider_call=reply).read(files=files, source_files=sources, output_dir=tmp_path,
                                                         timeout_seconds=1)
        assert result["status"] == "failed" and result["errorCode"] == "timeout"
        assert result["provider"]["plannedRequestCount"] == 16
        assert result["provider"]["requestCount"] == len(calls) == 4
        assert result["annotations"] == []
        saved = json.loads((tmp_path / "source-transcription.json").read_text())
        assert saved["provider"] == result["provider"]
    finally:
        release.set()
    time.sleep(.03)
    assert len(calls) == 4
    assert json.loads((tmp_path / "source-transcription.json").read_text()) == result


def test_one_unread_region_cannot_be_reported_as_complete_source_evidence(tmp_path):
    def reply(body, timeout):
        content = body["input"][0]["content"]
        metadata = [json.loads(item["text"]) for item in content[1:] if item["type"] == "input_text"]
        if metadata[-1]["imageId"].endswith("region-4"):
            raise AIProviderHTTPError(503)
        return {"output_text": json.dumps({"annotations": [{"text": "R23", "confidence": "high"}]})}

    files, sources = inputs([separated_ink()])
    result = CadSourceReader(provider_call=reply).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "failed"
    assert result["annotations"] == [] and result["structureObservations"] == []
    assert result["provider"]["requestCount"] == 5 and result["provider"]["plannedRequestCount"] == 4
    assert [item["status"] for item in result["regionReadings"]] == ["succeeded", "succeeded", "succeeded", "failed"]
    assert all(item["candidateReading"]["annotations"] for item in result["regionReadings"][:3])
    assert len(result["regionReadings"][-1]["attempts"]) == 2


@pytest.mark.parametrize("failure_kind", [409, 503, "upstream_error"])
def test_transient_region_failure_retries_only_that_region_once_inside_original_budget(tmp_path, failure_kind):
    lock = threading.Lock()
    calls = {}
    active = peak = 0
    failure = (AIProviderHTTPError(failure_kind) if isinstance(failure_kind, int)
               else ai_proxy.AIProviderUpstreamError({"code": "upstream_error"}))
    failure.diagnostics = {"terminalStatus": "failed", "eventCount": 1, "firstByteSeconds": .01}

    def reply(body, timeout):
        nonlocal active, peak
        content = body["input"][0]["content"]
        metadata = [json.loads(item["text"]) for item in content[1:] if item["type"] == "input_text"]
        region = metadata[-1]["imageId"]
        with lock:
            calls.setdefault(region, []).append((body, timeout))
            attempt = len(calls[region])
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.03)
            if region.endswith("region-1") and attempt == 1:
                raise failure
            return {"output_text": json.dumps({"annotations": [{"text": "R23", "location": region, "confidence": "high"}]})}
        finally:
            with lock:
                active -= 1

    files, sources = inputs([separated_ink()])
    result = CadSourceReader(provider_call=reply).read(files=files, source_files=sources, output_dir=tmp_path,
                                                     timeout_seconds=1)
    assert result["status"] == "succeeded" and len(result["annotations"]) == 4
    assert result["provider"]["requestCount"] == 5 and result["provider"]["plannedRequestCount"] == 4
    assert sorted(map(len, calls.values())) == [1, 1, 1, 2] and 1 < peak <= 4
    retried = calls["source-0-region-1"]
    assert retried[0][0] == retried[1][0]
    assert 0 < retried[1][1] < retried[0][1] <= 1
    report = result["regionReadings"][0]
    assert report["status"] == "succeeded" and "errorCode" not in report
    assert [attempt["status"] for attempt in report["attempts"]] == ["failed", "succeeded"]
    assert report["attempts"][0]["retryScheduled"] is True
    assert report["attempts"][0]["diagnostics"]["eventCount"] == 1
    assert report["candidateReading"]["annotations"][0]["text"] == "R23"


@pytest.mark.parametrize("failure_kind", [401, 403, 402, 400, "authentication_error", "permission_denied",
                                          "insufficient_quota", "context_length_exceeded", "invalid_prompt"])
def test_permanent_region_failure_is_not_retried(tmp_path, failure_kind):
    failure = (AIProviderHTTPError(failure_kind) if isinstance(failure_kind, int)
               else ai_proxy.AIProviderUpstreamError({"code": failure_kind, "status": 503}, retryable=True))
    files, sources = inputs()
    provider = Provider([failure])
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "failed"
    assert result["provider"]["requestCount"] == len(provider.requests) == 1
    assert len(result["regionReadings"][0]["attempts"]) == 1
    assert result["regionReadings"][0]["attempts"][0]["retryScheduled"] is False


def test_permanent_failure_does_not_discard_other_regions_still_finishing(tmp_path):
    def reply(body, timeout):
        content = body["input"][0]["content"]
        metadata = [json.loads(item["text"]) for item in content[1:] if item["type"] == "input_text"]
        if metadata[-1]["imageId"].endswith("region-1"):
            raise AIProviderHTTPError(403)
        time.sleep(.06)
        return {"output_text": json.dumps({"annotations": [{"text": "R23", "confidence": "high"}]})}

    files, sources = inputs([separated_ink()])
    result = CadSourceReader(provider_call=reply).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "failed" and result["errorCode"] == "http_403"
    assert result["provider"]["requestCount"] == 4
    assert [item["status"] for item in result["regionReadings"]] == ["failed", "succeeded", "succeeded", "succeeded"]
    assert all(item["candidateReading"]["annotations"] for item in result["regionReadings"][1:])
    assert result["annotations"] == []


def test_failed_region_retains_safe_upstream_diagnostics_with_two_argument_provider(tmp_path):
    failure = ai_proxy.AIProviderUpstreamError({"type": "server_error", "code": "upstream_error", "status": 502,
                                              "message": "private-provider-body"})
    failure.diagnostics = {"httpStatus": 200, "firstByteSeconds": .2, "firstOutputTextSeconds": None,
                           "eventCount": 3, "terminalStatus": "failed", "protocol": "sse",
                           "errorCategory": "upstream_error", "eventCounts": {"error": 1, "private-event": 2},
                           "usage": {"input_tokens": 100, "output_tokens": 2, "reasoning": "private-reasoning"},
                           "upstreamError": {**failure.upstream_error, "message": "private-provider-body"},
                           "body": "private-provider-body", "reasoning": "private-reasoning"}
    failure.upstream_error["message"] = "private-provider-body"
    files, sources = inputs()
    provider = Provider([failure, failure])
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "failed" and result["errorCode"] == "upstream_error"
    report = result["regionReadings"][0]
    assert report["imageId"] == "source-0-original" and report["status"] == "failed"
    assert report["upstreamError"] == {"type": "server_error", "code": "upstream_error", "httpStatus": 502}
    assert report["diagnostics"]["firstByteSeconds"] == .2
    assert report["diagnostics"]["eventCounts"] == {"error": 1}
    assert report["diagnostics"]["usage"] == {"input_tokens": 100, "output_tokens": 2}
    assert report["diagnostics"]["terminalStatus"] == "failed"
    assert len(provider.requests) == result["provider"]["requestCount"] == 2
    assert [attempt["errorCode"] for attempt in report["attempts"]] == ["upstream_error", "upstream_error"]
    assert "private-" not in (tmp_path / "source-transcription.json").read_text()


def test_default_reader_collects_successful_transport_diagnostics(monkeypatch, tmp_path):
    def call(body, timeout, *, on_diagnostics):
        on_diagnostics({"headerSeconds": .1, "firstByteSeconds": .2, "firstOutputTextSeconds": .3,
                        "terminalStatus": "completed", "terminalEventCount": 1, "outputChars": 22,
                        "usage": {"input_tokens": 30, "output_tokens": 5, "reasoning_tokens": 2}})
        return {"output_text": json.dumps(transcription())}

    monkeypatch.setattr(ai_proxy, "_call_provider", call)
    files, sources = inputs()
    result = CadSourceReader().read(files=files, source_files=sources, output_dir=tmp_path)
    assert result["status"] == "succeeded"
    report = result["regionReadings"][0]
    assert report["status"] == "succeeded" and report["diagnostics"]["terminalStatus"] == "completed"
    assert report["diagnostics"]["firstOutputTextSeconds"] == .3
    assert report["diagnostics"]["usage"]["reasoning_tokens"] == 2


def test_deadline_keeps_latest_region_diagnostics_and_ignores_late_callbacks(monkeypatch, tmp_path):
    release = threading.Event()

    def call(body, timeout, *, on_diagnostics):
        on_diagnostics({"httpStatus": 200, "firstByteSeconds": .1, "eventCount": 2,
                        "terminalStatus": "in_progress", "outputChars": 0})
        release.wait(2)
        on_diagnostics({"terminalStatus": "completed", "outputChars": 90})
        return {"output_text": json.dumps(transcription())}

    monkeypatch.setattr(ai_proxy, "_call_provider", call)
    files, sources = inputs()
    try:
        result = CadSourceReader().read(files=files, source_files=sources, output_dir=tmp_path, timeout_seconds=.3)
        assert result["status"] == "failed" and result["errorCode"] == "timeout"
        report = result["regionReadings"][0]
        assert report["status"] == "incomplete" and report["errorCode"] == "timeout"
        assert report["diagnostics"]["terminalStatus"] == "in_progress"
        assert report["diagnostics"]["firstByteSeconds"] == .1
        assert result["annotations"] == []
    finally:
        release.set()
    time.sleep(.03)
    assert json.loads((tmp_path / "source-transcription.json").read_text()) == result
    assert report["diagnostics"]["terminalStatus"] == "in_progress"


@pytest.mark.parametrize("failure", ["http", "invalid_json", "invented_image", "timeout"])
def test_reader_failure_is_bounded_and_never_fills_missing_evidence(tmp_path, failure):
    files, sources = inputs()
    if failure == "http":
        action = AIProviderHTTPError(503)
    elif failure == "invalid_json":
        action = {"plan": plan(), "ready": True}
    elif failure == "invented_image":
        action = transcription()
        action["annotations"][0]["imageId"] = "/unprovided/path.png"
    else:
        def action(body):
            time.sleep(0.2)
            return transcription()
    provider = Provider([action, action] if failure == "http" else [action])
    started = time.monotonic()
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path,
                                                          timeout_seconds=0.04 if failure == "timeout" else 180)
    assert result["status"] == "failed"
    assert result["annotations"] == [] and result["structureObservations"] == []
    assert result["verified"] is False
    assert len(provider.requests) == (2 if failure == "http" else 1)
    assert "plan" not in result and "ready" not in result
    if failure == "timeout":
        assert time.monotonic()-started < 0.2
        assert result["errorCode"] == "timeout"


def test_reader_cache_requires_all_ordered_source_hashes_and_reader_version(tmp_path):
    files, sources = inputs([raster(200, 100), raster(180, 110)])
    value = StubSourceReader().read(files=files, source_files=sources)
    identity = source_identity(files, sources)
    assert reusable_transcription(value, identity)
    changed, changed_sources = inputs([files[0], raster(181, 110)])
    assert not reusable_transcription(value, source_identity(changed, changed_sources))
    assert not reusable_transcription(value, source_identity(tuple(reversed(files)), list(reversed(sources))))
    assert not reusable_transcription({**value, "version": "obsolete"}, identity)
    assert not reusable_transcription({**value, "verified": True}, identity)
    assert not reusable_transcription({**value, "status": "failed"}, identity)


def test_equal_text_on_different_physical_marks_is_not_dropped_by_parser(tmp_path):
    files, sources = inputs()
    reading = transcription()
    second = {**reading["annotations"][0], "imageId": "source-0-original", "bbox": [0.05, 0.1, 0.1, 0.1],
              "endpointsOrDatum": "A separate leader points to another circular feature."}
    reading["annotations"].append(second)
    result = CadSourceReader(provider_call=Provider([reading])).read(files=files, source_files=sources, output_dir=tmp_path)
    assert [item["text"] for item in result["annotations"]] == ["R23", "R23"]
    assert result["annotations"][0]["bbox"] != result["annotations"][1]["bbox"]


def test_agent_reader_gets_180_second_cap_inside_unchanged_600_second_total_budget(tmp_path):
    reader = StubSourceReader()
    provider = Provider([{"action": "ask_user", "message": "请补充必要尺寸。", "questions": ["槽深是多少？"]}])
    result = CadAgentService(proxy=AIProxy(timeout_seconds=600), source_reader=reader, provider_call=provider, spatial_interpreter=StubSpatialInterpreter()).run(
        message="按图建模", files=[raster(200, 100)], output_dir=tmp_path, timeout_seconds=600)
    assert result["status"] == "needs_input"
    assert len(reader.calls) == 1 and reader.calls[0]["timeout_seconds"] == 180
    assert 500 < provider.requests[0][1] <= 600


def test_navigation_has_pixel_and_byte_limits_before_provider_call(monkeypatch, tmp_path):
    files, sources = inputs()
    provider = Provider([])
    monkeypatch.setattr("app.cad_source_reader.MAX_SOURCE_PIXELS", 100)
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path / "pixels")
    assert result["status"] == "failed" and provider.requests == []
    monkeypatch.setattr("app.cad_source_reader.MAX_SOURCE_PIXELS", 80_000_000)
    monkeypatch.setattr("app.cad_source_reader.MAX_NAVIGATION_BYTES", 1)
    files, sources = inputs([separated_ink()])
    result = CadSourceReader(provider_call=provider).read(files=files, source_files=sources, output_dir=tmp_path / "bytes")
    assert result["status"] == "failed" and provider.requests == []


def test_independent_reader_runs_before_planner_and_saved_candidates_reach_every_turn(tmp_path):
    seen = []

    def read(body):
        seen.append("reader")
        assert "987654" not in json.dumps(body)
        assert "currentPlan" not in json.dumps(body)
        checkpoint = json.loads((tmp_path / "source-reader/source-transcription.json").read_text())
        assert checkpoint["status"] == "running"
        return transcription()

    def observe(body):
        seen.append("planner")
        current = context(body)
        assert current["sourceTranscription"]["annotations"][0]["text"] == "R23"
        assert current["sourceTranscription"]["verified"] is False
        assert current["currentPlan"]["parameters"]["height"]["value"] == 987654
        assert "annotation ID, corrected reading/datum, inspection iteration and reason" in body["instructions"]
        return record()

    provider = Provider([read, observe, execute(), finish()])
    executor = Executor()
    from tests.test_cad_agent import StubDrawingReviewer
    result = CadAgentService(provider_call=provider, executor=executor, drawing_reviewer=StubDrawingReviewer(), spatial_interpreter=StubSpatialInterpreter()).run(
        message="Current edit contains 987654 but independent reading must not receive it.",
        files=[raster(200, 100)], state={"cadPlan": plan(987654)}, output_dir=tmp_path)
    assert seen == ["reader", "planner"]
    assert result["status"] == "review_required"
    assert len(executor.calls) == 1
    assert result["state"]["sourceTranscription"] == result["sourceTranscription"]
    for body, _ in provider.requests[1:]:
        evidence = context(body)["sourceTranscription"]
        raw = result["sourceTranscription"]
        for key in ("version", "status", "candidateEvidence", "verified", "sourceFingerprint", "questions"):
            assert evidence[key] == raw[key]
        for key in ("annotations", "structureObservations"):
            assert evidence[key] == [{name: value for name, value in item.items() if name not in {"imageId", "preparedSha256"}}
                                     for item in raw[key]]
        assert "provider" not in evidence and "regionReadings" not in evidence
    assert result["trace"][0]["action"] == "read_source"


def test_same_drawing_text_edit_reuses_reading_but_new_second_image_is_read_again(tmp_path):
    reader = StubSourceReader()
    files = [raster(200, 100), raster(180, 110)]
    ask = {"action": "ask_user", "message": "请补充一个必要尺寸。", "questions": ["槽的深度是多少？"]}
    first = CadAgentService(source_reader=reader, provider_call=Provider([ask]), spatial_interpreter=StubSpatialInterpreter()).run(message="按图建模", files=files, output_dir=tmp_path / "first")
    assert len(reader.calls) == 1
    same = CadAgentService(source_reader=reader, provider_call=Provider([ask]), spatial_interpreter=StubSpatialInterpreter()).run(message="槽深按用户要求修改", files=files,
                                                                                   state={"agentState": first["state"]}, output_dir=tmp_path / "same")
    assert len(reader.calls) == 1
    assert same["sourceTranscription"] == first["sourceTranscription"]
    assert any(item["action"] == "read_source" and item["reused"] for item in same["trace"])
    changed = CadAgentService(source_reader=reader, provider_call=Provider([ask]), spatial_interpreter=StubSpatialInterpreter()).run(message="更换第二张图", files=[files[0], raster(181, 110)],
                                                                                      state={"agentState": first["state"]}, output_dir=tmp_path / "changed")
    assert len(reader.calls) == 2
    assert changed["sourceTranscription"]["sourceFingerprint"] != first["sourceTranscription"]["sourceFingerprint"]


def test_text_only_modeling_does_not_add_a_reader_provider_call(tmp_path):
    reader, provider = StubSourceReader(), Provider([record(), execute(), finish()])
    result = CadAgentService(source_reader=reader, provider_call=provider, executor=Executor()).run(message="Make a 10×20×30 block.", output_dir=tmp_path)
    assert result["status"] == "review_required"
    assert reader.calls == [] and len(provider.requests) == 3
    assert result["sourceTranscription"] is None


def test_failed_visual_reading_stops_before_planner_and_executor(tmp_path):
    provider, executor = Provider([AIProviderHTTPError(503), AIProviderHTTPError(503)]), Executor()
    result = CadAgentService(provider_call=provider, executor=executor).run(message="按图建模", files=[raster(200, 100)], output_dir=tmp_path)
    assert result["status"] == "failed"
    assert result["sourceTranscription"]["annotations"] == []
    assert result["plan"] is None and result["artifacts"] == {}
    assert executor.calls == [] and len(provider.requests) == 2
    assert result["trace"][0]["result"]["status"] == "failed"


def test_reader_time_is_charged_to_the_whole_agent_budget(monkeypatch, tmp_path):
    def prepare(files, **kwargs):
        time.sleep(0.02)
        return tuple(files), (), [ai_proxy._attachment_metadata(item) for item in files]
    monkeypatch.setattr(ai_proxy, "_prepare_provider_attachments", prepare)
    def slow_read(body):
        time.sleep(0.3)
        return transcription()
    provider = Provider([slow_read])
    started = time.monotonic()
    result = CadAgentService(provider_call=provider, executor=Executor()).run(message="按图建模", files=[raster(200, 100)],
                                                                          output_dir=tmp_path, timeout_seconds=0.1)
    assert time.monotonic()-started < 0.2
    assert result["status"] == "failed"
    assert len(provider.requests) == 1
    assert provider.requests[0][1] < 0.08
    assert result["provider"]["lastErrorCode"] == "timeout"
