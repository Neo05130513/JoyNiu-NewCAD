"""Failure semantics and content-free diagnostics for compatible SSE relays."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from app.ai_proxy import (
    AIProviderIncompleteError,
    AIProviderUpstreamError,
    AIProxyError,
    _provider_error_code,
    _provider_error_is_retryable,
    _stream_payload,
)
from app.cad_source_reader import _safe_reader_diagnostics


def sse(*events):
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


OUTPUT = {"type": "response.output_text.delta", "delta": '{"action":"finish"}'}


@pytest.mark.parametrize("event", [
    {"type": "response.error", "error": {"code": "upstream_error", "message": "PRIVATE"}},
    {"type": "response.error", "response": {"error": {"code": "upstream_error", "message": "PRIVATE"}}},
    {"type": "relay.private_failure", "error": {"code": "upstream_error", "message": "PRIVATE"}},
    {"type": "relay.private_failure", "response": {"error": {"code": "upstream_error", "message": "PRIVATE"}}},
])
def test_explicit_error_in_nonstandard_event_wins_over_earlier_valid_json(event):
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(io.BytesIO(sse(OUTPUT, event)))
    error = caught.value
    assert _provider_error_code(error) == "upstream_error"
    assert error.upstream_error == {"code": "upstream_error"}
    assert error.diagnostics["terminalEventCount"] == 1
    assert error.diagnostics["terminalStatus"] == "failed"
    assert error.diagnostics["streamEndReason"] == "terminal_event"
    assert not any(secret in json.dumps(vars(error)) for secret in ("PRIVATE", "relay.private_failure", "finish"))


@pytest.mark.parametrize("event", [
    {"type": "response.cancelled"},
    {"type": "response.canceled"},
    {"type": "response.cancelled", "response": {"status": "completed", "output_text": '{"ok":true}'}},
    {"type": "relay.signal", "status": "cancelled"},
    {"type": "relay.signal", "response": {"status": "canceled"}},
    {"type": "response.completed", "response": {"status": " CANCELLED ", "output_text": '{"ok":true}'}},
])
def test_cancel_signal_cannot_recover_previous_output(event):
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(io.BytesIO(sse(OUTPUT, event)))
    error = caught.value
    assert error.upstream_error == {"code": "cancelled"}
    assert not _provider_error_is_retryable(error)
    assert error.diagnostics["terminalStatus"] == "cancelled"
    assert error.diagnostics["terminalEventCount"] == 1
    assert error.diagnostics["streamEndReason"] == "terminal_event"


@pytest.mark.parametrize("status", ["cancelled", "canceled", " Cancelled "])
def test_json_fallback_cancellation_is_not_a_success(status):
    payload = {"status": status, "output_text": '{"action":"finish"}'}
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(io.BytesIO(json.dumps(payload).encode()))
    assert caught.value.diagnostics["terminalStatus"] == "cancelled"
    assert caught.value.diagnostics["streamEndReason"] == "json_body"


@pytest.mark.parametrize("event", [
    {"type": "relay.signal", "status": "failed"},
    {"type": "relay.signal", "response": {"status": "failed", "output_text": '{"ok":true}'}},
])
def test_nonstandard_failed_status_is_an_upstream_failure(event):
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(io.BytesIO(sse(OUTPUT, event)))
    assert caught.value.diagnostics["streamEndReason"] == "terminal_event"
    assert caught.value.diagnostics["terminalStatus"] == "failed"


def test_unknown_incomplete_status_never_recovers_output():
    with pytest.raises(AIProviderIncompleteError) as caught:
        _stream_payload(io.BytesIO(sse(OUTPUT, {"type": "relay.signal", "response": {"status": "incomplete"}})))
    assert caught.value.diagnostics["terminalStatus"] == "incomplete"
    assert caught.value.diagnostics["terminalEventCount"] == 1


@pytest.mark.parametrize("event", [
    {"type": "relay.success", "response": {"status": "completed", "output_text": '{"ok":true}'}},
    {"type": "response.unknown_success", "status": "completed", "output_text": '{"ok":true}'},
    {"type": "response.unknown_delta", "delta": '{"ok":true}'},
])
def test_unknown_success_never_becomes_model_output(event):
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(sse(event)))
    diagnostic = caught.value.diagnostics
    assert _provider_error_code(caught.value) == "empty_response"
    assert diagnostic["streamEndReason"] == "eof"
    assert diagnostic["outputChars"] == 0 and diagnostic["terminalEventCount"] == 0
    assert diagnostic["unknownEventSamples"][0]["hasOutput"] is True


@pytest.mark.parametrize("ending,reason", [(b"", "eof"), (b"data: [DONE]\n\n", "done_marker")])
def test_empty_stream_distinguishes_eof_from_done_marker(ending, reason):
    raw = b": heartbeat PRIVATE\n" + sse({"type": "response.created"}, {"type": "response.in_progress"}) + ending
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(raw))
    diagnostic = caught.value.diagnostics
    assert diagnostic["eventCount"] == 2
    assert diagnostic["unknownEventCount"] == 0
    assert diagnostic["streamEndReason"] == reason
    assert "PRIVATE" not in json.dumps(diagnostic)


@pytest.mark.parametrize("ending,reason", [
    (b"", "eof"),
    (b"data: [DONE]\n", "done_marker"),
    (sse({"type": "response.completed", "response": {"status": "completed"}}), "terminal_event"),
])
def test_success_keeps_actual_stream_end_reason(ending, reason):
    snapshots = []
    result = _stream_payload(io.BytesIO(sse(OUTPUT) + ending), on_diagnostics=snapshots.append)
    assert result["status"] == "completed"
    assert snapshots[-1]["streamEndReason"] == reason


class TimeoutAtEOF(io.BytesIO):
    def readline(self, *_args):
        part = super().readline()
        if not part:
            raise TimeoutError("PRIVATE")
        return part


@pytest.mark.parametrize("event", [
    {"type": "response.error", "error": {"code": "server_error"}},
    {"type": "response.cancelled"},
    {"type": "relay.signal", "response": {"status": "incomplete"}},
])
def test_pending_failure_flushed_after_read_error_still_prevents_recovery(event):
    raw = sse(OUTPUT) + b"data: " + json.dumps(event).encode() + b"\n"
    with pytest.raises((AIProviderUpstreamError, AIProviderIncompleteError)):
        _stream_payload(TimeoutAtEOF(raw))


def test_read_error_diagnostics_survive_existing_complete_json_recovery():
    snapshots = []
    result = _stream_payload(TimeoutAtEOF(sse(OUTPUT)), on_diagnostics=snapshots.append)
    assert result["status"] == "completed"
    assert snapshots[-1]["streamEndReason"] == "read_error"


def test_invalid_event_has_termination_reason_without_raw_body():
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(b"data: {PRIVATE}\n\n"))
    assert caught.value.diagnostics["streamEndReason"] == "invalid_event"
    assert "PRIVATE" not in json.dumps(vars(caught.value))


def test_unknown_samples_are_bounded_and_keep_only_safe_class_and_fingerprint():
    names = ["", "ping", "response.reasoning.private", "response.private", "message.private", "sk-secret-private"]
    events = [{"type": name, "private": "NEVER_LOG", "reasoning": "NEVER_LOG"} for name in names]
    events += [{"type": f"private_{index}"} for index in range(12)]
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(sse(*events)))
    diagnostic = caught.value.diagnostics
    assert diagnostic["unknownEventCount"] == len(events)
    assert diagnostic["eventCounts"] == {"other": len(events)}
    samples = diagnostic["unknownEventSamples"]
    assert len(samples) == 8
    assert [sample["typeClass"] for sample in samples[:6]] == ["missing", "heartbeat", "reasoning_event", "response_event", "message_event", "other"]
    for index, sample in enumerate(samples):
        assert sample["eventIndex"] == index + 1
        assert sample["elapsedSeconds"] >= 0
        assert sample["typeHash"] == hashlib.sha256(events[index]["type"].encode()).hexdigest()[:16]
    assert "NEVER_LOG" not in json.dumps(diagnostic)
    assert "sk-secret-private" not in json.dumps(diagnostic)


def test_source_reader_retains_new_safe_fields_and_rejects_injected_content():
    source = {
        "streamEndReason": "eof", "unknownEventCount": 11,
        "unknownEventSamples": [{
            "eventIndex": 1, "elapsedSeconds": 3.1, "typeClass": "response_event",
            "typeHash": "0123456789abcdef", "hasError": False, "hasResponse": True,
            "hasOutput": False, "responseStatus": "in_progress", "message": "PRIVATE",
        }, {"eventIndex": True, "elapsedSeconds": float("inf"), "typeClass": "PRIVATE",
            "typeHash": "PRIVATE", "hasError": "PRIVATE", "responseStatus": "PRIVATE"}],
    }
    result = _safe_reader_diagnostics(source)
    assert result["streamEndReason"] == "eof" and result["unknownEventCount"] == 11
    assert result["unknownEventSamples"][0] == {key: value for key, value in source["unknownEventSamples"][0].items() if key != "message"}
    assert result["unknownEventSamples"][1] == {}
    assert "PRIVATE" not in json.dumps(result)
    assert "streamEndReason" not in _safe_reader_diagnostics({"streamEndReason": "PRIVATE"})
