from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import time
import threading

import pytest
from PIL import Image

from app.cad_source_locations import (LOCATION_PROMPT, _parse_locations, cached_source_locations,
                                      locate_source_dimensions, source_location_progress)
from .test_cad_agent_api import api, run_result


def picture():
    output = io.BytesIO()
    Image.new("RGB", (1000, 800), "white").save(output, format="PNG")
    return output.getvalue()


def annotation(identifier="annotation-1", text="30", **kwargs):
    return {"id": identifier, "text": text, "fileIndex": 0,
            "preparedSha256": hashlib.sha256(picture()).hexdigest(),
            "sourceRegion": [.1, .2, .5, .4], "bbox": None,
            "bboxFrame": "prepared_source_image", "location": "left vertical dimension",
            "endpointsOrDatum": "between the top and bottom edges", **kwargs}


def with_source(api, monkeypatch, annotations=None):
    client, store, agent, services = api
    original_run = agent.run
    def run(**kwargs):
        result = original_run(**kwargs)
        digest = hashlib.sha256(kwargs["files"][0].data).hexdigest()
        reading = {"annotations": annotations or [annotation()],
                   "sourceFiles": [{"fileIndex": 0, "sha256": digest}],
                   "preparedFiles": [{"fileIndex": 0, "sha256": digest}]}
        result["sourceTranscription"] = reading
        result["state"]["sourceTranscription"] = copy.deepcopy(reading)
        return result
    monkeypatch.setattr(agent, "run", run)
    result = run_result(client.post("/api/v1/cad-agent/run", data={"message": "build from drawing"},
                                   files={"files": ("drawing.png", picture(), "image/png")}))
    return result


def output(*items):
    return {"output_text": json.dumps({"annotations": list(items)})}


def located(identifier="annotation-1", text="30", **kwargs):
    return {"id": identifier, "text": text, "bbox": [.2, .3, .06, .05],
            "confidence": "high", "ambiguous": False, **kwargs}


def test_source_location_crop_mapping_cache_revision_and_immutable_model(api, monkeypatch):
    client, store, agent, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    before = copy.deepcopy(record)
    calls = []
    def provider(body, timeout):
        calls.append(body)
        assert timeout > 0
        return output(located())
    monkeypatch.setattr(agent, "provider_call", provider, raising=False)
    url = f"/api/v1/cad-agent/runs/{result['runId']}/source-locations"
    response = client.post(url, json={"revision": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "succeeded"
    assert data["annotations"][0]["bbox"] == [.2, .32, .03, .02]
    assert data["annotations"][0]["preparedSha256"] == annotation()["preparedSha256"]
    assert data["annotations"][0]["bboxFrame"] == "prepared_source_image"
    assert len(calls) == 1
    assert calls[0]["instructions"] == LOCATION_PROMPT
    assert "endpointsOrDatum" in calls[0]["input"][0]["content"][0]["text"]
    assert "parameters" not in calls[0]["input"][0]["content"][0]["text"]
    assert client.post(url, json={"revision": 1}).json() == data
    assert len(calls) == 1
    assert store.load(result["runId"]) == before
    assert list((store.directory(result["runId"]) / "source-locations").glob("*.json"))
    assert client.get(f"/api/v1/cad-agent/runs/{result['runId']}").json()["sourceDimensionLocations"] == data
    next_record = {**record, "revision": 2}
    store.save(next_record, previous_revision=1)
    latest = client.post(url, json={"revision": 2}).json()
    assert latest["revision"] == 2 and latest["annotations"] == data["annotations"]
    assert len(calls) == 1
    assert client.post(url, json={"revision": 1, "force": True}).status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize("item", [
    located(ambiguous=True), located(confidence="medium"), located(text="31"),
    located(identifier="unknown-id"), located(bbox=[0, 0, 1, 1]), located(bbox=[-.1, .1, .03, .03]),
    located(bbox=[.98, .1, .03, .03]), located(bbox=[.1, .1, 0, .03]),
    located(bbox=[True, .1, .03, .03]), located(bbox=[.1, .1, .00001, .03]),
])
def test_unreliable_or_region_sized_locations_never_become_text_boxes(item):
    assert _parse_locations(output(item), [annotation()], [.1, .2, .5, .4], (1000, 800)) == []


def test_same_numeric_value_requires_unique_annotation_and_nonoverlapping_position():
    targets = [annotation(), annotation("annotation-2")]
    assert _parse_locations(output(located(), located()), targets, [0, 0, 1, 1], (1000, 800)) == []
    assert _parse_locations(output(located(), located("annotation-2")), targets, [0, 0, 1, 1], (1000, 800)) == []
    result = _parse_locations(output(located(), located("annotation-2", bbox=[.6, .3, .06, .05])),
                              targets, [0, 0, 1, 1], (1000, 800))
    assert [item["id"] for item in result] == ["annotation-1", "annotation-2"]


def test_text_matching_keeps_diameter_semantics_and_does_not_match_number_only():
    targets = [annotation(text="Φ 30")]
    result = _parse_locations(output(located(text="Ø30")), targets, [0, 0, 1, 1], (1000, 800))
    assert result[0]["text"] == "Φ 30"
    assert _parse_locations(output(located(text="30")), targets, [0, 0, 1, 1], (1000, 800)) == []


def test_source_location_endpoint_auth_ownership_and_explicit_historical_revision(api, monkeypatch):
    client, store, agent, _ = api
    result = with_source(api, monkeypatch)
    url = f"/api/v1/cad-agent/runs/{result['runId']}/source-locations"
    monkeypatch.setattr(agent, "provider_call", lambda *args: output(located()), raising=False)
    for payload in ({"revision": True}, {"revision": 0}, {"revision": "1"}, {"force": "yes"},
                    {"operationId": "../bad"}, {"operationId": 4}, {"operationId": "x" * 97}):
        assert client.post(url, json=payload).status_code == 422
    assert client.post(url, json={"revision": 99}).status_code == 404
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "0")
    assert client.post(url, json={"revision": 1}).status_code == 401
    # The source's download bearer must not authorize a provider-backed action.
    token = store.load(result["runId"])["downloadToken"]
    assert client.post(url + f"?access={token}", json={"revision": 1}).status_code == 401
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    other = store.load(result["runId"])
    other.update({"runId": "cad_" + "a" * 32, "owner": "another-user"})
    store.save(other)
    assert client.post(url.replace(result["runId"], other["runId"]), json={}).status_code == 403
    running = store.load(result["runId"])
    running.update({"runId": "cad_" + "b" * 32, "status": "running"})
    store.save(running)
    assert client.post(url.replace(result["runId"], running["runId"]), json={}).status_code == 409


def test_source_or_annotation_identity_change_invalidates_cached_locations(api, monkeypatch):
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    calls = []
    def provider(*args):
        calls.append(1)
        return output(located())
    assert locate_source_dimensions(store, record, provider_call=provider)["status"] == "succeeded"
    changed = copy.deepcopy(record)
    changed["sourceTranscription"]["annotations"][0]["endpointsOrDatum"] = "other edges"
    assert cached_source_locations(store, changed) is None
    assert locate_source_dimensions(store, changed, provider_call=provider)["status"] == "succeeded"
    assert len(calls) == 2
    Path(record["files"][0]["path"]).write_bytes(b"replaced")
    assert cached_source_locations(store, record) is None
    failed = locate_source_dimensions(store, record, provider_call=provider)
    assert failed["status"] == "unavailable" and failed["annotations"] == []
    assert failed["errorCode"] == "source_identity_mismatch"
    assert len(calls) == 2


def test_partial_safe_failure_is_cached_and_retry_is_explicit(api, monkeypatch):
    _, store, _, _ = api
    result = with_source(api, monkeypatch, [annotation(), annotation("annotation-2", "40")])
    record = store.load(result["runId"])
    partial = locate_source_dimensions(store, record, provider_call=lambda *args: output(located()))
    assert partial["status"] == "partial" and partial["unlocatedAnnotationIds"] == ["annotation-2"]
    def failure(*args):
        raise RuntimeError("secret-token-at/private/path")
    assert locate_source_dimensions(store, record, provider_call=failure) == partial
    failed = locate_source_dimensions(store, record, provider_call=failure, force=True)
    assert failed["status"] == "partial"
    assert failed["annotations"] == partial["annotations"]
    assert failed["errorCode"] == "source_localization_unavailable"
    assert "secret-token" not in str(failed) and "/private/path" not in str(failed)


def test_force_refines_an_existing_transcription_box(api, monkeypatch):
    _, store, _, _ = api
    result = with_source(api, monkeypatch, [annotation(bbox=[.15, .25, .02, .02])])
    record = store.load(result["runId"])
    initial = locate_source_dimensions(store, record, provider_call=lambda *args: pytest.fail("valid original reused"))
    assert initial["annotations"][0]["bbox"] == [.15, .25, .02, .02]
    refined = locate_source_dimensions(store, record, force=True, provider_call=lambda *args: output(located()))
    assert refined["annotations"][0]["bbox"] == [.2, .32, .03, .02]


def test_successful_ambiguous_retry_withdraws_old_cached_location(api, monkeypatch):
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    assert locate_source_dimensions(store, record, provider_call=lambda *args: output(located()))["status"] == "succeeded"
    retry = locate_source_dimensions(store, record, force=True,
                                     provider_call=lambda *args: output(located(ambiguous=True, bbox=None)))
    assert retry["status"] == "unavailable" and retry["annotations"] == []
    assert retry["unlocatedAnnotationIds"] == ["annotation-1"]


def test_locations_reject_conflicts_across_overlapping_crop_groups(api, monkeypatch):
    _, store, _, _ = api
    result = with_source(api, monkeypatch, [annotation(), annotation("annotation-2", sourceRegion=[0, 0, 1, 1])])
    record = store.load(result["runId"])
    def provider(body, timeout):
        targets = json.loads(body["input"][0]["content"][0]["text"])["targets"]
        if targets[0]["id"] == "annotation-1":
            return output(located())
        return output(located("annotation-2", bbox=[.2, .32, .03, .02]))
    result = locate_source_dimensions(store, record, provider_call=provider)
    assert result["status"] == "unavailable" and result["annotations"] == []


def test_provider_that_ignores_deadline_cannot_extend_request_or_cache_late_result(api, monkeypatch):
    from app import cad_source_locations
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    monkeypatch.setattr(cad_source_locations, "_MAX_SECONDS", .01)
    def delayed(*args):
        time.sleep(.25)
        return output(located())
    started = time.monotonic()
    result = locate_source_dimensions(store, record, provider_call=delayed)
    assert time.monotonic() - started < .2
    assert result["status"] == "unavailable" and result["annotations"] == []
    time.sleep(.3)
    assert cached_source_locations(store, record) == result


def test_get_run_exposes_partial_boxes_and_progress_before_final_cache(api, monkeypatch):
    client, store, _, _ = api
    result = with_source(api, monkeypatch, [annotation(), annotation("annotation-2", "40", sourceRegion=[0, 0, 1, 1])])
    record = store.load(result["runId"])
    before = copy.deepcopy(record)
    waiting, release = threading.Event(), threading.Event()
    def provider(body, timeout):
        target = json.loads(body["input"][0]["content"][0]["text"])["targets"][0]
        if target["id"] == "annotation-2":
            waiting.set()
            assert release.wait(3)
        return output(located(target["id"], target["text"]))
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(locate_source_dimensions, store, record, provider_call=provider, operation_id="progress-test")
    try:
        assert waiting.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            progress = source_location_progress(store, record)
            if progress and progress["completedAnnotationCount"] == 1:
                break
            time.sleep(.005)
        response = client.get(f"/api/v1/cad-agent/runs/{record['runId']}").json()
        progress = response["sourceDimensionLocationProgress"]
        assert progress["operationId"] == "progress-test"
        assert progress["status"] == "running" and progress["stage"] == "locating"
        assert progress["completedAnnotationCount"] == progress["locatedAnnotationCount"] == 1
        assert progress["totalAnnotationCount"] == 2 and progress["elapsedSeconds"] >= 0
        assert progress["annotations"][0]["id"] == "annotation-1"
        assert "sourceDimensionLocations" not in response
        assert store.load(record["runId"]) == before
        changed = copy.deepcopy(record)
        changed["sourceTranscription"]["annotations"][0]["text"] = "32"
        assert source_location_progress(store, changed) is None
        assert source_location_progress(store, {**record, "revision": 2}) is None
    finally:
        release.set()
        pool.shutdown(wait=True)
    final = future.result()
    progress = source_location_progress(store, record)
    assert final["status"] == "succeeded" and final["operationId"] == "progress-test"
    assert progress["annotations"] == final["annotations"]
    assert progress["status"] == "succeeded" and progress["stage"] == "completed"
    assert progress["completedAnnotationCount"] == progress["totalAnnotationCount"] == 2
    assert progress["unlocatedAnnotationIds"] == []


def test_queue_wait_counts_towards_deadline_and_does_not_write_a_cache(api, monkeypatch):
    from app import cad_source_locations
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    monkeypatch.setattr(cad_source_locations, "_MAX_SECONDS", .03)
    index = int(hashlib.sha256(record["runId"].encode()).hexdigest()[:2], 16) % len(cad_source_locations._LOCKS)
    lock = cad_source_locations._LOCKS[index]
    lock.acquire()
    started = time.monotonic()
    try:
        outcome = locate_source_dimensions(store, record, provider_call=lambda *args: pytest.fail("still queued"),
                                           operation_id="queued-test")
    finally:
        lock.release()
    assert time.monotonic() - started < .2
    assert outcome["status"] == "unavailable" and outcome["errorCode"] == "source_localization_timeout"
    assert outcome["operationId"] == "queued-test"
    assert cached_source_locations(store, record) is None
    progress = source_location_progress(store, record)
    assert progress["status"] == "unavailable" and progress["stage"] == "completed"


def test_duplicate_queued_timeout_preserves_active_worker_progress(api, monkeypatch):
    from app import cad_source_locations
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    entered, release = threading.Event(), threading.Event()
    def provider(*args):
        entered.set()
        assert release.wait(2)
        return output(located())
    pool = ThreadPoolExecutor(max_workers=1)
    first = pool.submit(locate_source_dimensions, store, record, provider_call=provider, operation_id="active-test")
    try:
        assert entered.wait(1)
        monkeypatch.setattr(cad_source_locations, "_MAX_SECONDS", .02)
        second = locate_source_dimensions(store, record, provider_call=provider, operation_id="waiting-test")
        assert second["status"] == "unavailable" and second["operationId"] == "waiting-test"
        progress = source_location_progress(store, record)
        assert progress["operationId"] == "active-test" and progress["status"] == "running"
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert first.result()["status"] == "succeeded"


def test_slow_source_preparation_is_bounded_and_never_calls_provider_after_deadline(api, monkeypatch):
    from app import cad_source_locations
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    original_prepare = cad_source_locations._prepared_image
    def delayed(*args):
        time.sleep(.25)
        return original_prepare(*args)
    monkeypatch.setattr(cad_source_locations, "_prepared_image", delayed)
    monkeypatch.setattr(cad_source_locations, "_MAX_SECONDS", .03)
    started = time.monotonic()
    result = locate_source_dimensions(store, record, provider_call=lambda *args: pytest.fail("deadline expired"))
    assert time.monotonic() - started < .2
    assert result["status"] == "unavailable" and result["errorCode"] == "source_localization_timeout"
    progress = source_location_progress(store, record)
    assert progress["status"] == "unavailable" and progress["stage"] == "completed"
    time.sleep(.3)
    assert cached_source_locations(store, record) == result


def test_force_operation_progress_is_distinct_from_previous_cached_result(api, monkeypatch):
    client, store, agent, _ = api
    result = with_source(api, monkeypatch)
    monkeypatch.setattr(agent, "provider_call", lambda *args: output(located()), raising=False)
    url = f"/api/v1/cad-agent/runs/{result['runId']}/source-locations"
    first = client.post(url, json={"operationId": "first-operation"}).json()
    assert first["operationId"] == "first-operation"
    second = client.post(url, json={"operationId": "retry-operation", "force": True}).json()
    assert second["operationId"] == "retry-operation"
    record = store.load(result["runId"])
    assert source_location_progress(store, record)["operationId"] == "retry-operation"
    assert cached_source_locations(store, record)["operationId"] == "retry-operation"
    monkeypatch.setattr(__import__("app.cad_source_locations", fromlist=["_PROGRESS_TTL_SECONDS"]), "_PROGRESS_TTL_SECONDS", -1)
    assert source_location_progress(store, record) is None
    assert cached_source_locations(store, record)["status"] == "succeeded"


def test_prepared_hash_or_native_dxf_never_gets_guessed_image_frame(api, monkeypatch):
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    record["sourceTranscription"]["preparedFiles"][0]["sha256"] = "0" * 64
    failed = locate_source_dimensions(store, record, provider_call=lambda *args: pytest.fail("must not guess transformed frame"))
    assert failed["status"] == "unavailable"
    assert failed["errorCode"] == "prepared_frame_unavailable"
    record["sourceTranscription"]["preparedFiles"][0]["sha256"] = annotation()["preparedSha256"]
    record["files"][0]["filename"] = "native.dxf"
    assert locate_source_dimensions(store, record, provider_call=lambda *args: pytest.fail("DXF not raster"))["annotations"] == []


def test_pdf_reconstruction_requires_exact_saved_prepared_hash(api, monkeypatch):
    fitz = pytest.importorskip("fitz")
    from app.pdf_preprocessor import preprocess_pdf
    _, store, _, _ = api
    result = with_source(api, monkeypatch)
    record = store.load(result["runId"])
    document = fitz.open()
    document.new_page(width=300, height=200).insert_text((40, 50), "30")
    payload = document.tobytes()
    document.close()
    prepared = preprocess_pdf(payload, "drawing.pdf").png_bytes
    original_hash, prepared_hash = (hashlib.sha256(data).hexdigest() for data in (payload, prepared))
    Path(record["files"][0]["path"]).write_bytes(payload)
    record["files"][0].update({"filename": "drawing.pdf", "sha256": original_hash})
    reading = record["sourceTranscription"]
    reading["sourceFiles"][0]["sha256"] = original_hash
    reading["preparedFiles"][0]["sha256"] = prepared_hash
    reading["annotations"][0]["preparedSha256"] = prepared_hash
    data = locate_source_dimensions(store, record, provider_call=lambda *args: output(located()))
    assert data["status"] == "succeeded" and data["annotations"][0]["preparedSha256"] == prepared_hash
    reading["preparedFiles"][0]["sha256"] = "a" * 64
    data = locate_source_dimensions(store, record, provider_call=lambda *args: pytest.fail("PDF changed frame"))
    assert data["status"] == "unavailable" and data["annotations"] == []
