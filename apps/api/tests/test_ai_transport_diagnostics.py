"""Transport-only tests: no provider requests or provider response content in telemetry."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.ai_proxy import (
    AIProviderHTTPError,
    AIProviderTransportError,
    AIProviderUpstreamError,
    AIProxyError,
    _call_provider,
    _provider_error_code,
    _provider_error_is_retryable,
    _stream_payload,
)


class Response(io.BytesIO):
    status = 200


def sse(*events):
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


def test_valid_sse_error_retains_only_safe_upstream_identifiers():
    secret = "sk-never-store-this-provider-message"
    snapshots = []
    event = {"type": "error", "error": {
        "type": "server_error", "code": "upstream_timeout", "status_code": 503,
        "message": secret, "reasoning": secret, "request": {"api_key": secret},
    }}
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(Response(sse(event)), on_diagnostics=snapshots.append)
    error = caught.value
    assert _provider_error_code(error) == "upstream_error"
    assert error.upstream_error == {"type": "server_error", "code": "upstream_timeout", "httpStatus": 503}
    assert error.diagnostics["terminalStatus"] == "failed"
    assert error.diagnostics["terminalEventCount"] == 1
    assert error.diagnostics["eventCounts"] == {"error": 1}
    assert secret not in json.dumps({"snapshots": snapshots, "exception": vars(error)})
    assert secret not in str(error)
    # Preserve the existing generic SSE-error retry policy.
    assert not _provider_error_is_retryable(error)


def test_event_error_header_without_body_type_is_an_upstream_error():
    raw = b'event: error\ndata: {"code":"rate_limit_exceeded","status":429}\n\n'
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(Response(raw))
    assert caught.value.upstream_error == {"code": "rate_limit_exceeded", "httpStatus": 429}


@pytest.mark.parametrize("with_response", [True, False])
def test_failed_terminal_never_recovers_preceding_parseable_output(with_response):
    failed = {"status": "failed", "error": {"code": "server_error", "message": "PRIVATE"},
              "usage": {"input_tokens": 18, "output_tokens": 9, "total_tokens": 27,
                        "output_tokens_details": {"reasoning_tokens": 7, "text": "PRIVATE"}}}
    event = {"type": "response.failed", **({"response": failed} if with_response else failed)}
    raw = sse({"type": "response.output_text.delta", "delta": '{"message":"complete-looking"}'}, event)
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(Response(raw))
    diagnostic = caught.value.diagnostics
    assert _provider_error_is_retryable(caught.value)
    assert diagnostic["eventCount"] == 2
    assert diagnostic["terminalEventCount"] == 1
    assert diagnostic["usage"] == {"input_tokens": 18, "output_tokens": 9, "total_tokens": 27, "reasoning_tokens": 7}
    assert diagnostic["terminalStatus"] == "failed"
    assert "PRIVATE" not in json.dumps(diagnostic)


@pytest.mark.parametrize("raw", [b'data: {not JSON}\n\n', b'data: \xff\n\n', b'data: []\n\n'])
def test_damaged_sse_is_still_invalid_stream(raw):
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(Response(raw))
    assert not isinstance(caught.value, AIProviderUpstreamError)
    assert _provider_error_code(caught.value) == "invalid_stream"
    assert caught.value.diagnostics["errorCategory"] == "invalid_stream"


def test_unrecognized_identifier_strings_and_event_names_are_not_retained():
    identifier_shaped_secret = "secret_token_abcdefghijklmnopqrst"
    raw = sse({"type": identifier_shaped_secret, "reasoning": identifier_shaped_secret},
              {"type": "error", "error": {"type": identifier_shaped_secret, "code": identifier_shaped_secret,
                                          "status_code": "503", "message": identifier_shaped_secret}})
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(Response(raw))
    assert caught.value.upstream_error == {}
    assert caught.value.diagnostics["eventCounts"] == {"other": 1, "error": 1}
    assert identifier_shaped_secret not in json.dumps(vars(caught.value))


def test_call_provider_emits_timings_counts_numeric_usage_and_no_response_content(monkeypatch):
    text = '{"message":"private-model-text"}'
    terminal = {"status": "completed", "output_text": text, "usage": {
        "input_tokens": 50, "output_tokens": 11, "total_tokens": 61,
        "input_tokens_details": {"cached_tokens": 3, "secret": "PRIVATE"},
        "output_tokens_details": {"reasoning_tokens": 4, "reasoning": "PRIVATE"},
        "unknown": "PRIVATE",
    }}
    raw = sse({"type": "response.created", "response": {"id": "private-id"}},
              {"type": "response.output_text.delta", "delta": text[:10]},
              {"type": "response.output_text.delta", "delta": text[10:]},
              {"type": "response.completed", "response": terminal})
    monkeypatch.setattr("app.ai_proxy._provider_key", lambda: "test-credential")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(raw))
    snapshots = []
    result = _call_provider({"input": "private-input"}, 2, on_diagnostics=snapshots.append)
    assert result == terminal
    final = snapshots[-1]
    assert final["httpStatus"] == 200
    assert 0 <= final["headerSeconds"] <= final["firstByteSeconds"] <= final["firstOutputTextSeconds"] <= final["elapsedSeconds"]
    assert final["responseBytes"] == len(raw)
    assert final["outputChars"] == final["outputDeltaChars"] == len(text)
    assert final["protocol"] == "sse"
    assert final["eventCount"] == 4 and final["terminalEventCount"] == 1
    assert final["usage"] == {"input_tokens": 50, "output_tokens": 11, "total_tokens": 61,
                              "cached_tokens": 3, "reasoning_tokens": 4}
    serialized = json.dumps(snapshots)
    assert all(secret not in serialized for secret in ["private-model-text", "private-input", "private-id", "PRIVATE", "sk-private"])


def test_diagnostics_callback_cannot_mutate_collector_or_break_success():
    received = []
    def callback(snapshot):
        received.append(json.loads(json.dumps(snapshot)))
        snapshot["eventCounts"]["injected"] = 123
        snapshot["outputChars"] = -100
        raise RuntimeError("observer failed")
    result = _stream_payload(Response(sse({"type": "response.completed", "response": {
        "status": "completed", "output_text": '{"ok":true}',
    }})), on_diagnostics=callback)
    assert result["status"] == "completed"
    assert received[-1]["outputChars"] == len('{"ok":true}')
    assert "injected" not in received[-1]["eventCounts"]


def test_json_fallback_reports_only_numeric_usage_and_handles_malformed_status():
    snapshots = []
    payload = {"status": [], "output_text": '{"ok":true}', "usage": {
        "input_tokens": "20", "output_tokens": True, "total_tokens": -1,
        "input_tokens_details": {"cached_tokens": 10**30}, "output_tokens_details": {"reasoning_tokens": 0},
    }}
    raw = json.dumps(payload).encode()
    assert _stream_payload(Response(raw), on_diagnostics=snapshots.append) == payload
    assert snapshots[-1]["protocol"] == "json"
    assert snapshots[-1]["responseBytes"] == len(raw)
    assert snapshots[-1]["usage"] == {"reasoning_tokens": 0}
    assert snapshots[-1]["terminalStatus"] == "unknown"


def test_json_error_envelope_is_not_a_success_response():
    with pytest.raises(AIProviderUpstreamError) as caught:
        _stream_payload(Response(b'{"error":{"type":"api_error","message":"PRIVATE"}}'))
    assert caught.value.diagnostics["protocol"] == "json"
    assert caught.value.upstream_error == {"type": "api_error"}


@pytest.mark.parametrize("status", [401, 503])
def test_http_errors_have_safe_diagnostics_without_reading_error_bodies(monkeypatch, status):
    class Unreadable:
        def read(self, *_args):
            raise AssertionError("must never read HTTP error payload")
        def close(self):
            pass
    def request(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://private.invalid", status, "PRIVATE", {}, Unreadable())
    monkeypatch.setattr("app.ai_proxy._provider_key", lambda: "test-credential")
    monkeypatch.setattr("urllib.request.urlopen", request)
    with pytest.raises(AIProviderHTTPError) as caught:
        _call_provider({}, 2)
    assert caught.value.diagnostics["httpStatus"] == status
    assert caught.value.diagnostics["errorCategory"] == f"http_{status}"
    assert caught.value.diagnostics["responseBytes"] == 0
    assert "PRIVATE" not in json.dumps(caught.value.diagnostics)


def test_transport_timeout_has_diagnostics_even_before_headers(monkeypatch):
    def request(*_args, **_kwargs):
        raise urllib.error.URLError(TimeoutError("PRIVATE"))
    monkeypatch.setattr("app.ai_proxy._provider_key", lambda: "test-credential")
    monkeypatch.setattr("urllib.request.urlopen", request)
    with pytest.raises(AIProviderTransportError) as caught:
        _call_provider({}, 2)
    assert caught.value.diagnostics["errorCategory"] == "timeout"
    assert caught.value.diagnostics["httpStatus"] is None
    assert caught.value.diagnostics["firstByteSeconds"] is None
    assert "PRIVATE" not in json.dumps(vars(caught.value))


@pytest.mark.parametrize("terminal_type", ["error", "response.failed"])
def test_buffered_failure_at_timeout_does_not_recover_prior_output(terminal_type):
    raw = sse({"type": "response.output_text.delta", "delta": '{"ok":true}'})
    raw += b"data: " + json.dumps({"type": terminal_type, "error": {"code": "server_error"}}).encode() + b"\n"
    class TimeoutAtEOF(Response):
        def readline(self, *_args):
            part = super().readline()
            if not part:
                raise TimeoutError("PRIVATE")
            return part
    with pytest.raises(AIProviderUpstreamError):
        _stream_payload(TimeoutAtEOF(raw))
