"""Synthetic wire streams: framing, size, error locality, and secret-free evidence."""
import http.client
import io
import json

import pytest

from app.ai_proxy import AIProxyError, AIProviderUpstreamError, _call_provider, _final_json, _provider_error_code, _stream_payload
from app.cad_source_reader import _safe_reader_diagnostics


def final_event(*, padding=0):
    return {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "reasoning", "encrypted_content": "private-reasoning-marker-" + "A"*padding},
        {"id": "msg_final", "type": "message", "role": "assistant", "phase": "final_answer", "status": "completed",
         "content": [{"type": "output_text", "text": '{"message":"已保存草稿"}'}]}]}}


def frame(event):
    return b"data: "+json.dumps(event, ensure_ascii=False).encode()+b"\n\n"


def complete_message():
    event = final_event()["response"]["output"][1]
    return frame({"type": "response.output_item.done", "output_index": 0, "item": event})


class ChunkedSocket:
    """Use actual stdlib HTTP chunk parsing, not a nonstandard readline stub."""
    def __init__(self, payload, *, chunk_size=2):
        chunks = [payload[index:index+chunk_size] for index in range(0, len(payload), chunk_size)]
        self.wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\n\r\n"
                     + b"".join(f"{len(chunk):x}\r\n".encode()+chunk+b"\r\n" for chunk in chunks)+b"0\r\n\r\n")

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(io.BytesIO(self.wire))


@pytest.mark.parametrize("chunk_size", [1, 2, 7])
def test_utf8_split_across_actual_http_chunks_is_reassembled_before_decoding(chunk_size):
    response = http.client.HTTPResponse(ChunkedSocket(frame(final_event()), chunk_size=chunk_size))
    response.begin()
    snapshots = []
    result = _stream_payload(response, on_diagnostics=snapshots.append)
    assert _final_json(result) == {"message": "已保存草稿"}
    assert snapshots[-1]["terminalEventCount"] == 1
    assert "malformedEvent" not in snapshots[-1]


@pytest.mark.parametrize("multiline", [False, True])
def test_multi_megabyte_terminal_json_and_legal_multiline_sse_are_not_truncated(multiline):
    event = final_event(padding=3_000_000)
    encoded = json.dumps(event, ensure_ascii=False, indent=2 if multiline else None)
    raw = b"event: response.completed\r\n" + b"".join(b"data: "+line.encode()+b"\r\n" for line in encoded.splitlines())+b"\r\n"
    response = http.client.HTTPResponse(ChunkedSocket(raw, chunk_size=8191))
    response.begin()
    snapshots = []
    result = _stream_payload(response, on_diagnostics=snapshots.append)
    assert _final_json(result)["message"] == "已保存草稿"
    diagnostic = snapshots[-1]
    assert diagnostic["responseBytes"] == len(raw)
    assert diagnostic["maxEventDataBytes"] > 3_000_000
    assert diagnostic["maxSseLineBytes"] > 3_000_000
    assert "malformedEvent" not in diagnostic
    assert "private-reasoning-marker" not in json.dumps(snapshots)


@pytest.mark.parametrize("data,kind", [
    (b'{"type":"response.completed",}', "expected_property"),
    (b'{"type":"response.completed"', "expected_comma"),
    (b'{"private":"sk-do-not-persist","reasoning":"private-thoughts"} trailing', "extra_data"),
    (b'{"private":"sk-do-not-persist","reasoning":"private-thoughts}', "unterminated_string"),
    (b'{"private":"bad\\q"}', "invalid_escape"),
    (b'{"private":"bad\x00"}', "invalid_control"),
])
def test_json_syntax_diagnostic_reports_only_bounded_offsets_and_error_class(data, kind):
    snapshots = []
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(complete_message()+b"event: response.completed\ndata: "+data+b"\n\n"),
                        on_diagnostics=snapshots.append)
    diagnostic = caught.value.diagnostics
    malformed = diagnostic["malformedEvent"]
    assert _provider_error_code(caught.value) == "invalid_stream"
    assert malformed["context"] == "sse_event" and malformed["category"] == "json_syntax"
    assert malformed["eventType"] == "response.completed" and malformed["jsonKind"] == kind
    assert malformed["dataBytes"] == len(data) and malformed["dataLineCount"] == 1
    assert malformed["eventIndex"] == 2 and malformed["jsonLine"] == 1
    assert 0 <= malformed["jsonPosition"] <= len(data)
    assert malformed["jsonRemainingChars"] == malformed["dataChars"]-malformed["jsonPosition"]
    safe = json.dumps({"snapshots": snapshots, "error": vars(caught.value)})
    assert "sk-do-not-persist" not in safe and "private-thoughts" not in safe
    assert _safe_reader_diagnostics(diagnostic)["malformedEvent"] == malformed


@pytest.mark.parametrize("data,kind", [(b"\xff", "invalid_start"), (b"\xe4x", "invalid_continuation"), (b"\xe4", "unexpected_end")])
def test_bad_utf8_reports_byte_offsets_without_raw_bytes(data, kind):
    # EOF without a newline also makes a truncated multibyte sequence visible.
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(b"data: "+data))
    malformed = caught.value.diagnostics["malformedEvent"]
    assert malformed["category"] == "utf8" and malformed["context"] == "sse_line"
    assert malformed["utf8Kind"] == kind and malformed["utf8ByteStart"] == 6
    assert malformed["dataBytes"] == len(data)+6
    assert "dataChars" not in malformed
    assert _safe_reader_diagnostics(caught.value.diagnostics)["malformedEvent"] == malformed


@pytest.mark.parametrize("data,category", [(b"[]", "non_object"),
                                          (b'{"value":'+b"1"*5000+b"}", "json_numeric_limit")],
                         ids=["non_object", "numeric_limit"])
def test_non_object_depth_and_python_integer_limits_have_distinct_diagnostics(data, category):
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(b"data: "+data+b"\n\n"))
    assert caught.value.diagnostics["malformedEvent"]["category"] == category
    assert _provider_error_code(caught.value) == "invalid_stream"


def test_json_decoder_recursion_limit_is_classified_without_retaining_content(monkeypatch):
    # CAD dependencies can change the process recursion limit. Exercise the
    # decoder failure without increasing the depth enough to risk the C stack.
    raw = '{"private":"sensitive-nested-content"}'
    original_loads = json.loads
    def limited_loads(value, *args, **kwargs):
        if value == raw:
            raise RecursionError("maximum recursion depth exceeded")
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(json, "loads", limited_loads)
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(io.BytesIO(b"data: "+raw.encode()+b"\n\n"))
    assert caught.value.diagnostics["malformedEvent"]["category"] == "json_depth"
    assert "sensitive-nested-content" not in json.dumps(vars(caught.value))


@pytest.mark.parametrize("stream,accept", [(False, "application/json"), (True, "text/event-stream"),
                                          (None, "text/event-stream")])
def test_accept_header_follows_explicit_stream_flag_and_parses_json_response(monkeypatch, stream, accept):
    captured = {}
    class Response(io.BytesIO):
        status = 200
    def urlopen(request, **_kwargs):
        captured["accept"] = request.headers["Accept"]
        captured["body"] = json.loads(request.data)
        return Response(json.dumps(final_event()["response"], ensure_ascii=False).encode())
    monkeypatch.setattr("app.ai_proxy._provider_key", lambda: "test-credential")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    body = {"input": "text request"}
    if stream is not None:
        body["stream"] = stream
    diagnostics = []
    result = _call_provider(body, 2, on_diagnostics=diagnostics.append)
    assert captured == {"accept": accept, "body": body}
    assert _final_json(result) == {"message": "已保存草稿"}
    assert diagnostics[-1]["protocol"] == "json"


def test_nonstream_json_response_keeps_terminal_failure_guard(monkeypatch):
    class Response(io.BytesIO):
        status = 200
    payload = final_event()["response"]
    payload.update(status="failed", error={"code": "server_error"})
    monkeypatch.setattr("app.ai_proxy._provider_key", lambda: "test-credential")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(json.dumps(payload).encode()))
    with pytest.raises(AIProviderUpstreamError):
        _call_provider({"stream": False}, 2)


@pytest.mark.parametrize("error_kind", ["timeout", "incomplete_read"])
def test_malformed_pending_terminal_cannot_be_rescued_by_earlier_complete_final(error_kind):
    pending = b'data: {"type":"response.completed","response":'
    class Broken(io.BytesIO):
        def readline(self, *args):
            data = super().readline(*args)
            if data:
                return data
            if error_kind == "incomplete_read":
                raise http.client.IncompleteRead(pending)
            raise TimeoutError("synthetic interrupted socket")
    raw = complete_message()+(pending+b"\n" if error_kind == "timeout" else b"")
    with pytest.raises(AIProxyError) as caught:
        _stream_payload(Broken(raw))
    diagnostic = caught.value.diagnostics
    assert diagnostic["errorCategory"] == "invalid_stream"
    assert diagnostic["malformedEvent"]["category"] == "json_syntax"
    assert diagnostic["streamEndReason"] == "invalid_event"
    assert diagnostic["responseBytes"] == len(complete_message())+len(pending)+(1 if error_kind == "timeout" else 0)


def test_partial_http_failure_terminal_remains_failed_after_complete_final():
    failure = b'data: {"type":"response.failed","response":{"status":"failed","error":{"code":"server_error"}}}'
    class Broken(io.BytesIO):
        def readline(self, *args):
            value = super().readline(*args)
            if value:
                return value
            raise http.client.IncompleteRead(failure)
    with pytest.raises(AIProviderUpstreamError):
        _stream_payload(Broken(complete_message()))


def test_reader_diagnostic_allowlist_drops_injected_body_secrets_and_unbounded_values():
    secret = "sk-never-save-this"
    report = _safe_reader_diagnostics({"malformedEvent": {"context": "sse_event", "category": "json_syntax",
        "jsonKind": secret, "eventType": secret, "error": secret, "body": secret, "dataBytes": 12,
        "jsonPosition": True, "jsonLine": -1, "jsonColumn": 10**20, "jsonAtEnd": True,
        "utf8Kind": secret}, "maxEventDataBytes": 12, "maxSseLineBytes": float("inf")})
    assert report == {"maxEventDataBytes": 12, "malformedEvent": {"context": "sse_event", "category": "json_syntax",
                                                                   "dataBytes": 12, "jsonAtEnd": True}}
    assert secret not in json.dumps(report)
