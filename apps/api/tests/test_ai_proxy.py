from __future__ import annotations

import json
import base64
import hashlib
import io
import time
import urllib.error

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.ai_proxy import (  # noqa: E402
    AIConversationResult,
    AIFile,
    AIProviderNotConfigured,
    AIProxy,
    AIProxyError,
    _parse_result,
)
from app.platform_api import build_platform_services, create_platform_router  # noqa: E402


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 600.0),
        ("1800", 1800.0),
        ("10", 30.0),
        ("not-a-number", 600.0),
    ],
)
def test_proxy_default_timeout_is_long_and_configurable(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("JOYNIU_AI_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("JOYNIU_AI_TIMEOUT_SECONDS", configured)

    assert AIProxy().timeout_seconds == expected


@pytest.mark.parametrize(
    ("part_type", "recipe_id"),
    [
        ("circular_clamp", ""),
        ("circular_clamp_v1", ""),
        ("split_clamp_support_v1", ""),
        ("unknown", "circular_clamp"),
        ("unknown", "circular_clamp_v1"),
        ("unknown", "split_clamp_support"),
    ],
)
def test_parse_result_normalizes_split_clamp_identity_aliases(part_type, recipe_id):
    result = _parse_result(
        {
            "id": "resp_split_alias",
            "output_text": json.dumps(
                {
                    "message": "已识别开口夹紧座",
                    "part_type": part_type,
                    "recipe_id": recipe_id,
                    "parameter_patch": {"pedestalOuterRadius": 33},
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "split_clamp_support"
    assert result.recipe_id == "split_clamp_support_v1"
    assert result.parameter_patch == {"pedestalOuterRadius": 33.0}


def test_parse_result_infers_split_identity_from_recipe_unique_remote_fields():
    result = _parse_result(
        {
            "id": "resp_split_inferred",
            "output_text": json.dumps(
                {
                    "message": "识别到圆筒座和独立安装孔后缘基准",
                    "parameter_patch": {
                        "pedestalOuterRadius": 33,
                        "mountHoleCenterFromRear": 40,
                    },
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "split_clamp_support"
    assert result.recipe_id == "split_clamp_support_v1"
    assert result.parameter_patch == {
        "pedestalOuterRadius": 33.0,
        "mountHoleCenterFromRear": 40.0,
    }


def test_parse_result_does_not_infer_split_identity_from_shared_fields_only():
    result = _parse_result(
        {
            "id": "resp_identity_unknown",
            "output_text": json.dumps(
                {
                    "message": "只识别到底板",
                    "parameter_patch": {"baseLength": 125},
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "unknown"
    assert result.recipe_id == ""


def test_parse_result_infers_split_after_discarding_unsupported_identity_alias():
    result = _parse_result(
        {
            "id": "resp_descriptive_split_alias",
            "output_text": json.dumps(
                {
                    "message": "识别到开口夹紧座",
                    "part_type": "open_split_clamp",
                    "recipe_id": "open_split_clamp_v1",
                    "parameter_patch": {
                        "pedestalOuterRadius": 33,
                        "mountHoleCenterFromRear": 40,
                    },
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "split_clamp_support"
    assert result.recipe_id == "split_clamp_support_v1"


def test_parse_result_does_not_override_explicit_non_split_identity():
    result = _parse_result(
        {
            "id": "resp_explicit_bracket",
            "output_text": json.dumps(
                {
                    "message": "显式分类仍需复核",
                    "part_type": "bracket",
                    "recipe_id": "unsupported_bracket_variant",
                    "parameter_patch": {"pedestalOuterRadius": 33},
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "unknown"
    assert result.recipe_id == ""


def test_stream_recovers_complete_json_when_connection_ends_before_terminal_event():
    from app.ai_proxy import _stream_payload

    structured = json.dumps(
        {
            "message": "完整候选已到达",
            "parameter_patch": {"pedestalOuterRadius": 33},
            "needs_review": True,
            "questions": [],
        },
        ensure_ascii=False,
    )
    event = (
        "event: response.output_text.delta\n"
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': structured}, ensure_ascii=False)}\n\n"
    ).encode()

    class AbruptStream:
        def __init__(self):
            self.lines = iter(event.splitlines(keepends=True))

        def readline(self):
            try:
                return next(self.lines)
            except StopIteration:
                raise TimeoutError("socket ended before response.completed")

    payload = _stream_payload(AbruptStream())
    result = _parse_result(payload)
    assert result.message == "完整候选已到达"
    assert result.parameter_patch == {"pedestalOuterRadius": 33}


def test_proxy_sends_responses_schema_reasoning_and_previous_id(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode())
        return _Response(
            {
                "id": "resp_test_123",
                "output_text": json.dumps(
                    {
                        "message": "已将长度改为 80 mm",
                        "parameter_patch": {"length": 80},
                        "needs_review": False,
                        "questions": [],
                    }
                ),
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setenv("JOYNIU_AI_STORE_RESPONSES", "1")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "把轴加长到 80",
        previous_response_id="resp_previous_1",
        model_state={"kind": "shaft", "length": 70},
    )

    assert result.response_id == "resp_test_123"
    assert result.parameter_patch == {"length": 80}
    assert captured["url"] == "https://gptx.shop/v1/responses"
    assert captured["timeout"] == 7
    assert captured["body"]["model"] == "gpt-5.6-sol"
    assert captured["body"]["reasoning"] == {"effort": "high"}
    assert "max_output_tokens" not in captured["body"]
    assert captured["body"]["stream"] is True
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["body"]["store"] is True
    assert captured["body"]["previous_response_id"] == "resp_previous_1"
    assert captured["body"]["text"]["format"]["strict"] is True
    assert captured["body"]["text"]["format"]["name"] == "joyniu_cad_parameter_patch"
    # The credential is only an Authorization header and is not put in the
    # model input, structured result, or any client-facing field.
    assert "test-provider-key" not in json.dumps(captured["body"])


def test_proxy_does_not_add_input_or_output_token_caps(monkeypatch):
    captured = {}
    long_message = "输入" * 8_000
    long_state = "状态" * 60_000
    long_answer = "输出" * 7_000
    questions = ["待确认" * 180 for _ in range(25)]

    def fake_urlopen(request, timeout):
        assert timeout > 0
        captured["body"] = json.loads(request.data.decode())
        return _Response(
            {
                "id": "resp_unbounded",
                "output_text": json.dumps(
                    {
                        "message": long_answer,
                        "parameter_patch": {},
                        "needs_review": True,
                        "questions": questions,
                    },
                    ensure_ascii=False,
                ),
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        long_message,
        model_state={"kind": "bracket", "unboundedContext": long_state},
    )

    assert "max_output_tokens" not in captured["body"]
    assert long_message in captured["body"]["input"][0]["content"][0]["text"]
    assert long_state in captured["body"]["input"][0]["content"][1]["text"]
    assert result.message == long_answer
    assert result.questions == tuple(questions)


def test_proxy_retries_empty_result_without_schema_or_previous_id(monkeypatch):
    """A malformed relay turn gets one clean, stateless retry."""

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((json.loads(request.data.decode()), timeout))
        if len(requests) == 1:
            return _Response({"id": "resp_empty", "output": []})
        return _Response(
            {
                "id": "resp_recovered",
                "output_text": '{"message":"已恢复","parameter_patch":{"baseLength":120},"needs_review":false,"questions":[]}',
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setenv("JOYNIU_AI_STORE_RESPONSES", "1")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "把底板长度改为 120 mm",
        previous_response_id="resp_previous",
        model_state={"kind": "bracket", "baseLength": 100},
    )

    assert result.response_id == "resp_recovered"
    assert result.parameter_patch["baseLength"] == 120
    assert len(requests) == 2
    assert requests[0][0]["previous_response_id"] == "resp_previous"
    assert "text" in requests[0][0]
    assert "previous_response_id" not in requests[1][0]
    assert "text" not in requests[1][0]
    assert requests[1][0]["store"] is False
    assert "max_output_tokens" not in requests[1][0]
    assert 0 < requests[1][1] <= 7


def test_proxy_retries_incomplete_reasoning_without_app_token_cap(monkeypatch):
    bodies = []

    def fake_urlopen(request, timeout):
        assert timeout > 0
        bodies.append(json.loads(request.data.decode()))
        if len(bodies) == 1:
            return _Response(
                {
                    "id": "resp_incomplete",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "content": []}],
                }
            )
        return _Response(
            {
                "id": "resp_complete",
                "output_text": '{"message":"完成","parameter_patch":{},"needs_review":false,"questions":[]}',
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse("分析当前参数")

    assert result.response_id == "resp_complete"
    assert all("max_output_tokens" not in body for body in bodies)
    assert bodies[0]["store"] is False
    assert bodies[1]["store"] is False


def test_proxy_parses_responses_sse_stream(monkeypatch):
    structured = json.dumps(
        {
            "message": "流式完成",
            "parameter_patch": {"baseLength": 125},
            "needs_review": False,
            "questions": [],
        },
        ensure_ascii=False,
    )
    stream = (
        'event: response.created\n'
        'data: {"type":"response.created","response":{"id":"resp_stream_1","status":"in_progress"}}\n\n'
        'event: response.output_text.delta\n'
        f'data: {json.dumps({"type": "response.output_text.delta", "delta": structured[:20]}, ensure_ascii=False)}\n\n'
        'event: response.output_text.delta\n'
        f'data: {json.dumps({"type": "response.output_text.delta", "delta": structured[20:]}, ensure_ascii=False)}\n\n'
        'event: response.completed\n'
        f'data: {json.dumps({"type": "response.completed", "response": {"id": "resp_stream_1", "status": "completed", "output_text": structured}}, ensure_ascii=False)}\n\n'
        'data: [DONE]\n\n'
    ).encode("utf-8")

    class _StreamResponse:
        def __init__(self, body):
            self.lines = iter(body.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(self.lines, b"")

    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        captured["accept"] = request.headers["Accept"]
        assert timeout == 7
        return _StreamResponse(stream)

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse("把底板长度改为 125")

    assert captured["body"]["stream"] is True
    assert captured["accept"] == "text/event-stream"
    assert result.response_id == "resp_stream_1"
    assert result.parameter_patch == {"baseLength": 125}
    assert result.provider["streaming"] is True


def test_proxy_stream_emits_message_before_terminal_event_arrives(monkeypatch):
    structured = '{"message":"实时回调","parameter_patch":{},"needs_review":false,"questions":[]}'
    first_delta = '{"message":"实时'
    second_delta = structured[len(first_delta) :]
    stream_lines = iter(
        (
            b"event: response.output_text.delta\n",
            f'data: {json.dumps({"type": "response.output_text.delta", "delta": first_delta}, ensure_ascii=False)}\n'.encode(),
            b"\n",
            b"event: response.output_text.delta\n",
            f'data: {json.dumps({"type": "response.output_text.delta", "delta": second_delta}, ensure_ascii=False)}\n'.encode(),
            b"\n",
            b"event: response.completed\n",
            f'data: {json.dumps({"type": "response.completed", "response": {"id": "resp_slow", "status": "completed", "output_text": structured}}, ensure_ascii=False)}\n'.encode(),
            b"\n",
        )
    )
    timeline = {"updates": []}

    class _SlowStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            time.sleep(0.01)
            line = next(stream_lines, b"")
            if b'"type": "response.completed"' in line:
                timeline["terminal_arrived"] = time.monotonic()
            return line

    def on_update(text):
        timeline["updates"].append((text, time.monotonic()))

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", lambda _request, timeout: _SlowStream())
    result = AIProxy(timeout_seconds=7).converse("继续", on_message_update=on_update)

    assert result.response_id == "resp_slow"
    assert timeline["updates"][0][0] == "实时"
    assert timeline["updates"][0][1] < timeline["terminal_arrived"]


def test_proxy_stream_combines_surrogate_pair_before_sse_serialization(monkeypatch):
    structured = r'{"message":"\ud83d\ude00 已完成","parameter_patch":{},"needs_review":false,"questions":[]}'
    stream = (
        "event: response.output_text.delta\n"
        f'data: {json.dumps({"type": "response.output_text.delta", "delta": structured}, ensure_ascii=False)}\n\n'
        "event: response.completed\n"
        f'data: {json.dumps({"type": "response.completed", "response": {"id": "resp_emoji", "status": "completed", "output_text": structured}}, ensure_ascii=False)}\n\n'
    ).encode()

    class _EmojiStream:
        def __init__(self):
            self.lines = iter(stream.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(self.lines, b"")

    updates = []
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", lambda _request, timeout: _EmojiStream())
    result = AIProxy(timeout_seconds=7).converse("继续", on_message_update=updates.append)

    assert result.message == "😀 已完成"
    assert updates[0] == "😀 已完成"
    assert json.dumps({"text": updates[0]}, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize(
    ("event_type", "status"),
    (("response.failed", "failed"), ("response.incomplete", "incomplete")),
)
def test_proxy_rejects_non_completed_stream_with_parseable_patch(monkeypatch, event_type, status):
    structured = '{"message":"不可靠结果","parameter_patch":{"baseLength":999},"needs_review":false,"questions":[]}'
    response = {
        "id": f"resp_{status}",
        "status": status,
        "output_text": structured,
    }
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    stream = (
        f"event: {event_type}\n"
        f'data: {json.dumps({"type": event_type, "response": response}, ensure_ascii=False)}\n\n'
    ).encode()

    class _TerminalStream:
        def __init__(self):
            self.lines = iter(stream.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(self.lines, b"")

    calls = 0

    def fake_urlopen(_request, timeout):
        nonlocal calls
        calls += 1
        return _TerminalStream()

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(AIProxyError):
        AIProxy(timeout_seconds=7).converse("应用这个修改")

    assert calls == 2


def test_proxy_replays_explicit_history_without_provider_storage(monkeypatch):
    captured = {}
    updates = []
    statuses = []

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        return _Response(
            {
                "id": "resp_history",
                "output_text": '{"message":"我记得上一轮，已改为120","parameter_patch":{"baseLength":120},"needs_review":false,"questions":[]}',
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setenv("JOYNIU_AI_STORE_RESPONSES", "1")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "把它改成 120 mm",
        previous_response_id="resp_previous",
        model_state={"kind": "bracket", "baseLength": 100},
        history=(
            {"role": "user", "text": "当前底板多长？"},
            {"role": "assistant", "text": "当前底板长度是 100 mm。"},
        ),
        on_message_update=updates.append,
        on_status=statuses.append,
    )

    assert captured["body"]["input"][0] == {"role": "user", "content": "当前底板多长？"}
    assert captured["body"]["input"][1] == {"role": "assistant", "content": "当前底板长度是 100 mm。"}
    assert captured["body"]["input"][2]["role"] == "user"
    assert "previous_response_id" not in captured["body"]
    assert result.parameter_patch == {"baseLength": 120}
    assert updates[-1] == "我记得上一轮，已改为120"
    assert statuses == ["正在连接远程大模型…"]


def test_remote_result_is_never_overlaid_by_local_text_parser(monkeypatch):
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _Response(
            {
                "id": "resp_model_only",
                "output_text": '{"message":"请先确认目标字段","parameter_patch":{},"needs_review":true,"questions":["要修改哪个长度？"]}',
            }
        ),
    )

    result = AIProxy().converse(
        "把长度改成 120 mm",
        model_state={"kind": "bracket", "baseLength": 100},
    )

    assert result.parameter_patch == {}
    assert result.message == "请先确认目标字段"


def test_remote_failure_does_not_apply_local_text_fallback(monkeypatch):
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _Response({"id": "resp_empty", "output": []}),
    )

    with pytest.raises(AIProxyError):
        AIProxy().converse(
            "把底板长度改成 120 mm",
            model_state={"kind": "bracket", "baseLength": 100},
        )


def test_proxy_stream_accepts_back_to_back_data_lines_and_exposes_only_message(monkeypatch):
    structured = '{"message":"正在连续回答","parameter_patch":{},"needs_review":false,"questions":[]}'
    events = [
        {"type": "response.output_text.delta", "response_id": "resp_compact", "delta": structured[:24]},
        {"type": "response.output_text.delta", "response_id": "resp_compact", "delta": structured[24:]},
        {"type": "response.output_text.done", "response_id": "resp_compact", "text": structured},
    ]
    stream = "".join(f"data: {json.dumps(item, ensure_ascii=False)}\n" for item in events).encode()

    class _CompactStream:
        def __init__(self, body):
            self.lines = iter(body.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(self.lines, b"")

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", lambda _request, timeout: _CompactStream(stream))
    updates = []
    result = AIProxy(timeout_seconds=7).converse("继续", on_message_update=updates.append)

    assert result.response_id == "resp_compact"
    assert result.message == "正在连续回答"
    assert updates[-1] == "正在连续回答"
    assert all("parameter_patch" not in update for update in updates)


def test_drawing_retry_uses_original_image_high_then_low_and_only_remote_patch(monkeypatch):
    from app import ai_proxy
    from app.recognition import canonical_bracket_parameters

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    local_parameters = canonical_bracket_parameters().model_dump(by_alias=True)
    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"id": "local_verified", "status": "confirmed", "parameters": local_parameters},
    )
    requests = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode())
        requests.append((body, timeout))
        if len(requests) == 1:
            return _Response({"id": "resp_empty_vision", "output": []})
        return _Response(
            {
                "id": "resp_remote_vision",
                "output_text": json.dumps(
                    {
                        "message": "模型读取原图后给出候选",
                        "parameter_patch": {"baseLength": 123, "baseWidth": 49},
                        "needs_review": True,
                        "questions": ["请确认底板厚度"],
                    },
                    ensure_ascii=False,
                ),
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "直接分析原图",
        files=(AIFile("drawing.png", "image/png", png),),
    )

    assert len(requests) == 4
    first_image = next(item for item in requests[0][0]["input"][0]["content"] if item["type"] == "input_image")
    second_image = next(item for item in requests[1][0]["input"][0]["content"] if item["type"] == "input_image")
    audit_image = next(item for item in requests[2][0]["input"][0]["content"] if item["type"] == "input_image")
    arbitration_image = next(item for item in requests[3][0]["input"][0]["content"] if item["type"] == "input_image")
    assert first_image["detail"] == "high"
    assert second_image["detail"] == "low"
    assert audit_image["detail"] == "high"
    assert arbitration_image["detail"] == "high"
    assert len({first_image["image_url"], second_image["image_url"], audit_image["image_url"], arbitration_image["image_url"]}) == 1
    assert all(body["stream"] is True for body, _timeout in requests)
    assert all(timeout == 7 for _body, timeout in requests)
    assert result.provider["mode"] == "remote"
    assert result.parameter_patch == {"baseLength": 123, "baseWidth": 49}
    assert result.parameter_patch != local_parameters


def test_drawing_pipeline_audits_and_arbitrates_remote_candidates_before_emitting(monkeypatch):
    from app import ai_proxy

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    final_patch = {
        "baseLength": 125,
        "baseWidth": 95,
        "baseThickness": 15,
        "baseMainDepth": 80,
        "frontTongueWidth": 80,
        "rearBridgeWidth": 86,
        "totalHeight": 75,
        "pedestalOuterRadius": 33,
        "pedestalCenterFromRear": 35,
        "pedestalHeight": 40,
        "rearClampRise": 20,
        "boreDiameter": 36,
        "boreFloorZ": 40,
        "splitWidth": 12,
        "mountHoleCount": 2,
        "mountHoleDiameter": 12,
        "mountHoleCenterDistance": 96,
        "mountHoleCenterFromRear": 40,
        "crossHoleDiameter": 12,
        "crossHoleCenterZ": 55,
        "ribHeight": 20,
        "ribThickness": 10,
        "outerCornerRadius": 8,
        "neckConcaveRadius": 5,
        "neckConvexRadius": 8,
    }
    extraction_patch = {
        "pedestalCenterFromRear": 40,
        "pedestalHeight": 25,
        "rearClampRise": 35,
        "mountHoleCenterFromRear": 35,
    }
    arbitration_patch = {
        **final_patch,
        "boreFloorZ": 35,
        "crossHoleCenterZ": 40,
        "pedestalCenterFromRear": 40,
        "mountHoleCenterFromRear": 35,
    }
    remote_payloads = [
        {
            "id": "resp_extract",
            "message": "首轮视觉提取",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": extraction_patch,
        },
        {
            "id": "resp_audit",
            "message": "二轮尺寸链审校",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": final_patch,
        },
        {
            "id": "resp_arbitrate",
            "message": "三轮远程裁决完成",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": arbitration_patch,
        },
        {
            "id": "resp_position_datum_1",
            "message": "第一次位置尺寸线专项复核完成",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": {
                "pedestalCenterFromRear": 35,
                "mountHoleCenterFromRear": 40,
            },
        },
        {
            "id": "resp_position_datum_2",
            "message": "第二次位置尺寸线专项复核完成",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": {
                "pedestalCenterFromRear": 35,
                "mountHoleCenterFromRear": 40,
            },
        },
        {
            "id": "resp_height_datum_1",
            "message": "第一次高度尺寸线专项复核完成",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": {
                "boreFloorZ": 40,
                "crossHoleCenterZ": 55,
                "pedestalHeight": 40,
                "rearClampRise": 20,
            },
        },
        {
            "id": "resp_height_datum_2",
            "message": "第二次高度尺寸线专项复核完成",
            "part_type": "split_clamp_support",
            "recipe_id": "split_clamp_support_v1",
            "parameter_patch": {
                "boreFloorZ": 40,
                "crossHoleCenterZ": 55,
                "pedestalHeight": 40,
                "rearClampRise": 20,
            },
        },
    ]
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data.decode()))
        item = remote_payloads[len(requests) - 1]
        return _Response(
            {
                "id": item["id"],
                "output_text": json.dumps(
                    {
                        "message": item["message"],
                        "part_type": item["part_type"],
                        "recipe_id": item["recipe_id"],
                        "parameter_patch": item["parameter_patch"],
                        "parameter_evidence": {},
                        "needs_review": True,
                        "questions": [],
                    },
                    ensure_ascii=False,
                ),
            }
        )

    # Local recognition is audit-only and must never enter either remote
    # candidate or a later-stage prompt.
    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"status": "confirmed", "parameters": {"pedestalHeight": 999}},
    )
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    statuses = []
    updates = []
    result = AIProxy(timeout_seconds=7).converse(
        "解析图纸",
        files=(AIFile("drawing.png", "image/png", png),),
        on_status=statuses.append,
        on_message_update=updates.append,
    )

    assert len(requests) == 7
    assert statuses == [
        "阶段 1/3：远程模型正在提取图纸候选…",
        "阶段 2/3：远程模型正在审校尺寸链与视图关系…",
        "阶段 3/3：远程模型正在裁决冲突并补全候选…",
        "位置基准专项复核 1/2：远程模型正在独立核对尺寸界线…",
        "位置基准专项复核 2/2：远程模型正在独立核对尺寸界线…",
        "高度基准专项复核 1/2：远程模型正在独立核对尺寸界线…",
        "高度基准专项复核 2/2：远程模型正在独立核对尺寸界线…",
    ]
    assert updates == [
        "三轮远程裁决完成\n\n"
        "位置基准专项复核：远程独立复读已对 mountHoleCenterFromRear、"
        "pedestalCenterFromRear 形成两票一致。\n\n"
        "高度基准专项复核：远程独立复读已对 boreFloorZ、crossHoleCenterZ、"
        "pedestalHeight、rearClampRise 形成两票一致。"
    ]
    assert result.response_id == "resp_height_datum_2"
    assert result.parameter_patch == final_patch
    assert result.provider["mode"] == "remote"
    assert result.provider["attempts"] == 7
    assert all(body["stream"] is True for body in requests)
    assert all("max_output_tokens" not in body for body in requests)
    audit_text = "\n".join(
        item["text"]
        for item in requests[1]["input"][0]["content"]
        if item["type"] == "input_text"
    )
    arbitration_text = "\n".join(
        item["text"]
        for item in requests[2]["input"][0]["content"]
        if item["type"] == "input_text"
    )
    focused_text = "\n".join(
        item["text"]
        for item in requests[3]["input"][0]["content"]
        if item["type"] == "input_text"
    )
    assert "远程第二阶段尺寸审校" in audit_text
    assert '"pedestalHeight":25' in audit_text
    assert "远程第三阶段盲审裁决" in arbitration_text
    assert "本轮刻意不提供其数值以避免锚定" in arbitration_text
    assert '"pedestalHeight":25' not in arbitration_text
    assert '"pedestalHeight":40' not in arbitration_text
    assert "独立的位置基准视觉校验" in focused_text
    assert '"pedestalCenterFromRear":40' not in focused_text
    assert '"mountHoleCenterFromRear":35' not in focused_text
    height_text = "\n".join(
        item["text"]
        for item in requests[5]["input"][0]["content"]
        if item["type"] == "input_text"
    )
    assert "独立的高度基准视觉校验" in height_text
    assert '"boreFloorZ":35' not in height_text
    assert '"crossHoleCenterZ":40' not in height_text
    assert "999" not in audit_text
    assert "999" not in arbitration_text


def test_drawing_pipeline_skips_arbitration_when_remote_audit_is_complete_and_consistent(monkeypatch):
    from app import ai_proxy

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    shaft_patch = {
        "outerDiameter": 24,
        "length": 70,
        "holeDiameter": 10,
        "keywayWidth": 10,
        "keywayDepth": 20,
        "keywayLength": 20,
    }
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data.decode()))
        return _Response(
            {
                "id": f"resp_shaft_{len(requests)}",
                "output_text": json.dumps(
                    {
                        "message": "尺寸链一致" if len(requests) == 2 else "首轮轴候选",
                        "part_type": "shaft",
                        "recipe_id": "shaft_v1",
                        "parameter_patch": shaft_patch,
                        "parameter_evidence": {},
                        "needs_review": True,
                        "questions": [],
                    },
                    ensure_ascii=False,
                ),
            }
        )

    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: None)
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    statuses = []
    updates = []
    result = AIProxy(timeout_seconds=7).converse(
        "解析轴图纸",
        files=(AIFile("shaft.png", "image/png", png),),
        on_status=statuses.append,
        on_message_update=updates.append,
    )

    assert len(requests) == 2
    assert statuses == [
        "阶段 1/3：远程模型正在提取图纸候选…",
        "阶段 2/3：远程模型正在审校尺寸链与视图关系…",
    ]
    assert updates == ["尺寸链一致"]
    assert result.response_id == "resp_shaft_2"
    assert result.parameter_patch == shaft_patch
    assert result.provider["attempts"] == 2


def test_complex_split_clamp_always_enters_third_remote_arbitration_when_first_two_agree(monkeypatch):
    from app import ai_proxy
    from app.schemas import SplitClampSupportParameters

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    split_patch = SplitClampSupportParameters().model_dump(by_alias=True)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data.decode()))
        stage = len(requests)
        return _Response(
            {
                "id": f"resp_consistent_split_{stage}",
                "output_text": json.dumps(
                    {
                        "message": f"一致的开口夹紧座候选，第 {stage} 轮",
                        "part_type": "split_clamp_support",
                        "recipe_id": "split_clamp_support_v1",
                        "parameter_patch": split_patch,
                        "parameter_evidence": {},
                        "needs_review": True,
                        "questions": [],
                    },
                    ensure_ascii=False,
                ),
            }
        )

    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: None)
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    statuses = []
    updates = []
    result = AIProxy(timeout_seconds=7).converse(
        "解析复杂夹紧座图纸",
        files=(AIFile("split-clamp.png", "image/png", png),),
        on_status=statuses.append,
        on_message_update=updates.append,
    )

    assert len(requests) == 7
    assert statuses == [
        "阶段 1/3：远程模型正在提取图纸候选…",
        "阶段 2/3：远程模型正在审校尺寸链与视图关系…",
        "阶段 3/3：远程模型正在裁决冲突并补全候选…",
        "位置基准专项复核 1/2：远程模型正在独立核对尺寸界线…",
        "位置基准专项复核 2/2：远程模型正在独立核对尺寸界线…",
        "高度基准专项复核 1/2：远程模型正在独立核对尺寸界线…",
        "高度基准专项复核 2/2：远程模型正在独立核对尺寸界线…",
    ]
    assert updates == [
        "一致的开口夹紧座候选，第 3 轮\n\n"
        "位置基准专项复核：远程独立复读已对 mountHoleCenterFromRear、"
        "pedestalCenterFromRear 形成两票一致。\n\n"
        "高度基准专项复核：远程独立复读已对 boreFloorZ、crossHoleCenterZ、"
        "pedestalHeight、rearClampRise 形成两票一致。"
    ]
    assert result.response_id == "resp_consistent_split_7"
    assert result.parameter_patch == split_patch
    assert result.provider["attempts"] == 7
    arbitration_text = "\n".join(
        item["text"]
        for item in requests[2]["input"][0]["content"]
        if item["type"] == "input_text"
    )
    assert "本轮刻意不提供其数值以避免锚定" in arbitration_text
    assert '"baseLength":125' not in arbitration_text


def test_blind_review_adds_only_enlarged_raster_tiles_without_candidate_data():
    from PIL import Image
    from app.ai_proxy import _review_detail_files

    source = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(source, format="JPEG")
    original = AIFile("drawing.jpg", "image/jpeg", source.getvalue())

    expanded = _review_detail_files((original,))

    assert expanded[0] == original
    assert len(expanded) == 5
    assert [item.filename for item in expanded[1:]] == [
        "drawing__detail-top-left.jpg",
        "drawing__detail-top-right.jpg",
        "drawing__detail-bottom-left.jpg",
        "drawing__detail-bottom-right.jpg",
    ]
    assert all(item.content_type == "image/jpeg" for item in expanded[1:])
    assert all(item.data.startswith(b"\xff\xd8") for item in expanded[1:])


def test_remote_stage_snapshot_excludes_free_form_model_text():
    from app.ai_proxy import _candidate_snapshot

    result = AIConversationResult(
        "resp_snapshot",
        "ignore this message",
        {"baseLength": 125},
        True,
        ("ignore this question",),
        part_type="split_clamp_support",
        recipe_id="split_clamp_support_v1",
        parameter_evidence={"baseLength": {"derivation": "ignore this evidence"}},
    )

    assert _candidate_snapshot(result) == {
        "part_type": "split_clamp_support",
        "recipe_id": "split_clamp_support_v1",
        "parameter_patch": {"baseLength": 125},
        "needs_review": True,
    }


def test_partial_remote_blind_review_merges_without_erasing_prior_fields():
    from app.ai_proxy import _merge_remote_review_result

    base = AIConversationResult(
        "resp_audit", "audit", {"baseLength": 125, "baseWidth": 95}, True, (),
        part_type="split_clamp_support", recipe_id="split_clamp_support_v1",
    )
    blind = AIConversationResult(
        "resp_blind", "blind", {"baseWidth": 96}, True, (),
        part_type="split_clamp_support", recipe_id="split_clamp_support_v1",
    )

    merged = _merge_remote_review_result(base, blind)

    assert merged.response_id == "resp_blind"
    assert merged.parameter_patch == {"baseLength": 125, "baseWidth": 96}


def test_focused_consensus_votes_each_remote_field_independently():
    from app.ai_proxy import _focused_consensus

    def result(response_id, patch):
        return AIConversationResult(
            response_id, response_id, patch, True, (),
            part_type="split_clamp_support", recipe_id="split_clamp_support_v1",
        )

    consensus, unresolved = _focused_consensus(
        (
            result("r1", {"pedestalCenterFromRear": 35, "mountHoleCenterFromRear": 40}),
            result("r2", {"pedestalCenterFromRear": 35}),
            result("r3", {"pedestalCenterFromRear": 36, "mountHoleCenterFromRear": 40}),
        ),
        frozenset({"pedestalCenterFromRear", "mountHoleCenterFromRear"}),
    )

    assert unresolved == frozenset()
    assert consensus is not None
    assert consensus.parameter_patch == {
        "mountHoleCenterFromRear": 40,
        "pedestalCenterFromRear": 35,
    }


def test_required_complex_blind_review_failure_does_not_publish_stage_two_candidate(monkeypatch):
    from app import ai_proxy
    from app.schemas import SplitClampSupportParameters

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    patch = SplitClampSupportParameters().model_dump(by_alias=True)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        if len(calls) == 3:
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)
        return _Response(
            {
                "id": f"resp_stage_{len(calls)}",
                "output_text": json.dumps(
                    {
                        "message": "remote candidate",
                        "part_type": "split_clamp_support",
                        "recipe_id": "split_clamp_support_v1",
                        "parameter_patch": patch,
                        "parameter_evidence": {},
                        "needs_review": True,
                        "questions": [],
                    }
                ),
            }
        )

    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: None)
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AIProxy(timeout_seconds=7).converse(
        "解析复杂夹紧座图纸",
        files=(AIFile("split-clamp.png", "image/png", png),),
    )

    assert len(calls) == 3
    assert result.provider["mode"] == "local-fallback"
    assert result.provider["lastErrorCode"] == "http_401"
    assert result.parameter_patch == {}


def test_proxy_retries_transient_http_failure_but_not_auth_failure(monkeypatch):
    calls = []

    def transient_urlopen(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        return _Response(
            {
                "id": "resp_after_503",
                "output_text": '{"message":"ok","parameter_patch":{},"needs_review":false,"questions":[]}',
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", transient_urlopen)
    assert AIProxy(timeout_seconds=7).converse("继续").response_id == "resp_after_503"
    assert len(calls) == 2

    auth_calls = []

    def auth_urlopen(request, timeout):
        auth_calls.append(timeout)
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", auth_urlopen)
    with pytest.raises(AIProxyError, match="HTTP 401"):
        AIProxy(timeout_seconds=7).converse("继续")
    assert len(auth_calls) == 1


def test_provider_image_normalization_keeps_original_audit_metadata():
    """Vision relays receive JPEG while the uploaded PNG identity is unchanged."""
    from app.ai_proxy import _attachment_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    metadata, attachment = _attachment_content(AIFile("drawing.png", "image/png", png))
    assert metadata["contentType"] == "image/png"
    assert metadata["sizeBytes"] == len(png)
    assert metadata["sha256"] == hashlib.sha256(png).hexdigest()
    assert attachment["type"] == "input_image"
    assert attachment["image_url"].startswith("data:image/jpeg;base64,")
    assert attachment["detail"] == "high"

    _metadata, low_attachment = _attachment_content(
        AIFile("drawing.png", "image/png", png), image_detail="low"
    )
    assert low_attachment["detail"] == "low"

    # Browsers and DWG/PDM gateways sometimes lose the MIME declaration. The
    # extension and file signature must still select the vision path.
    octet_metadata, octet_attachment = _attachment_content(
        AIFile("drawing.png", "application/octet-stream", png)
    )
    assert octet_metadata["contentType"] == "application/octet-stream"
    assert octet_attachment["type"] == "input_image"
    assert octet_attachment["image_url"].startswith("data:image/jpeg;base64,")


def test_proxy_reads_key_file_without_requiring_environment(monkeypatch, tmp_path):
    key_file = tmp_path / "provider.key"
    key_file.write_text("file-provider-key\n", encoding="utf-8")
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.setenv("JOYNIU_AI_API_KEY_FILE", str(key_file))
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _Response(
            {
                "id": "resp_file_key",
                "output_text": '{"message":"ok","parameter_patch":{},"needs_review":false,"questions":[]}',
            }
        ),
    )
    assert AIProxy().converse("继续").response_id == "resp_file_key"


def test_proxy_parses_markdown_key_file(monkeypatch, tmp_path):
    """Deployment notes may be Markdown; send only the code-block token."""
    key_file = tmp_path / "provider.md"
    key_file.write_text(
        "# Provider credential\n\n```text\nsk-" + "A" * 24 + "\n```\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.setenv("JOYNIU_AI_API_KEY_FILE", str(key_file))
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers["Authorization"]
        return _Response(
            {
                "id": "resp_markdown_key",
                "output_text": '{"message":"ok","parameter_patch":{},"needs_review":false,"questions":[]}',
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert AIProxy().converse("继续").response_id == "resp_markdown_key"
    assert captured["authorization"] == "Bearer sk-" + "A" * 24


def test_proxy_parses_non_openai_markdown_token_and_respects_config_priority(monkeypatch, tmp_path):
    """GPTX deployments may use a non-sk token in a fenced key note."""
    from app.ai_proxy import _provider_key

    key_file = tmp_path / "provider.md"
    key_file.write_text("# relay\n\n```text\nrelay-token-" + "B" * 20 + "\n```\n", encoding="utf-8")
    monkeypatch.delenv("JOYNIU_LLM_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.setenv("JOYNIU_AI_API_KEY_FILE", str(key_file))
    assert _provider_key() == "relay-token-" + "B" * 20

    # The explicit LLM compatibility name is canonical when both are set.
    monkeypatch.setenv("JOYNIU_LLM_API_KEY", "llm-priority-token")
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "ai-priority-token")
    assert _provider_key() == "llm-priority-token"


def test_proxy_does_not_consume_generic_openai_environment_key(monkeypatch):
    """A generic key must not be forwarded to the GPTX relay by accident."""
    from app.ai_proxy import _provider_key

    monkeypatch.delenv("JOYNIU_LLM_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_LLM_API_KEY_FILE", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "generic-key-must-not-forward")
    with pytest.raises(AIProviderNotConfigured) as error:
        _provider_key()
    assert "generic-key-must-not-forward" not in str(error.value)


def test_unknown_drawing_returns_review_envelope_without_provider(monkeypatch):
    """Missing relay configuration must not turn unknown evidence into 503."""
    from app import ai_proxy

    for name in (
        "JOYNIU_LLM_API_KEY", "JOYNIU_AI_API_KEY", "JOYNIU_LLM_API_KEY_FILE",
        "JOYNIU_AI_API_KEY_FILE", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"id": "compat_unknown", "status": "needs_review", "parameters": {}},
    )
    result = AIProxy().converse(
        "解析这份图纸",
        files=(AIFile("unknown.dwg", "application/octet-stream", b"AC10"),),
    )
    assert result.needs_review is True
    assert result.parameter_patch == {}
    assert result.drawing["id"] == "compat_unknown"


def test_unknown_drawing_degraded_message_is_request_scoped_and_retryable(monkeypatch):
    """A failed turn must not claim the whole relay is unavailable."""

    from app import ai_proxy

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    calls = []
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"id": "compat_unknown", "status": "needs_review", "parameters": {}},
    )

    def empty_urlopen(_request, timeout):
        calls.append(timeout)
        return _Response({"id": f"resp_empty_{len(calls)}", "output": []})

    monkeypatch.setattr("urllib.request.urlopen", empty_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "解析图纸",
        files=(AIFile("unknown.png", "image/png", png),),
    )

    assert len(calls) == 2
    assert result.provider["mode"] == "local-fallback"
    assert result.provider["lastErrorCode"] == "empty_response"
    assert result.provider["attempts"] == 2
    assert result.needs_review is True
    assert result.questions == ()
    assert "中转站不可用" not in result.message
    assert "自动重试与多阶段审校" in result.message
    assert "没有用本地 OCR 或模板值替代" in result.message
    assert "可直接再次分析" in result.message


def test_platform_conversation_does_not_promote_compatibility_scaffold(monkeypatch):
    """Legacy canonical preview values stay out of the AI candidate envelope."""
    from app.recognition import canonical_bracket_parameters

    services = build_platform_services(":memory:", auth_secret="c" * 32)
    canonical = canonical_bracket_parameters().model_dump(by_alias=True)
    services.ai.converse = lambda *args, **kwargs: AIConversationResult(
        response_id="local_compat",
        message="候选待确认",
        parameter_patch={},
        needs_review=True,
        questions=("请确认视图与尺寸",),
        drawing={
            "id": "legacy-compat",
            "status": "needs_review",
            "partType": "bracket",
            "engine": "tesseract-compatible",
            "parameters": canonical,
            "modelRecipe": {"parameters": canonical, "source": "compatibility-recognizer"},
        },
        provider={"mode": "local-fallback"},
    )
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("JOYNIU_ENV", "development")
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    client = TestClient(app)
    response = client.post(
        "/api/v1/ai/conversation",
        data={"message": "解析图纸"},
        files={"file": ("unknown.png", b"not-a-real-image", "image/png")},
    )
    assert response.status_code == 200, response.text
    drawing = response.json()["drawingRecognition"]
    assert drawing["status"] == "needs_review"
    assert drawing["partType"] == "unknown"
    assert drawing["parameters"] == {}
    assert drawing["candidateParameters"] == {}


def test_platform_does_not_promote_ocr_dimensions_when_model_patch_is_empty(monkeypatch):
    from app.ocr import DimensionEvidence, DrawingRecognition

    services = build_platform_services(":memory:", auth_secret="d" * 32)
    services.ai.converse = lambda *args, **kwargs: AIConversationResult(
        response_id="local_remote_failed",
        message="远程模型没有返回参数",
        parameter_patch={},
        needs_review=True,
        questions=(),
        provider={"mode": "local-fallback", "streaming": True},
    )
    services.ocr.analyze = lambda *_args, **_kwargs: DrawingRecognition(
        id="drw_local_ocr",
        status="needs_review",
        part_type="bracket",
        source_filename="drawing.png",
        source_sha256="a" * 64,
        image_width=100,
        image_height=100,
        confidence=0.9,
        dimensions=(
            DimensionEvidence(
                id="dim_ocr",
                field="base_length",
                value=999,
                unit="mm",
                kind="linear",
                source_text="999",
                confidence=0.9,
            ),
        ),
        features=(),
        model_recipe={"parameters": {"baseLength": 999}, "source": "live-ocr"},
        assumptions=(),
        warnings=(),
        unresolved=(),
        ocr_text="999",
        engine="tesseract",
        candidate_parameters={"baseLength": 999},
    )
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("JOYNIU_ENV", "development")
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    response = TestClient(app).post(
        "/api/v1/ai/conversation",
        data={"message": "请分析原图"},
        files={"file": ("drawing.png", b"image", "image/png")},
    )

    assert response.status_code == 200, response.text
    drawing = response.json()["drawingRecognition"]
    assert drawing["engine"] == "ai-no-candidate"
    assert drawing["parameters"] == {}
    assert drawing["candidateParameters"] == {}
    assert drawing["dimensions"][0]["value"] == 999
    assert drawing["modelRecipe"]["evidenceEngine"] == "tesseract"


def test_anonymous_ai_requires_loopback_or_development_environment(monkeypatch):
    from types import SimpleNamespace

    from app.platform_api import _anonymous_ai_request_allowed

    remote_request = SimpleNamespace(client=SimpleNamespace(host="10.20.30.40"))
    monkeypatch.delenv("JOYNIU_ENV", raising=False)
    assert _anonymous_ai_request_allowed(remote_request) is False
    monkeypatch.setenv("JOYNIU_ENV", "development")
    assert _anonymous_ai_request_allowed(remote_request) is True



def test_proxy_rejects_unsupported_provider_patch(monkeypatch):
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _Response(
            {
                "id": "resp_bad_patch",
                "output_text": '{"message":"bad","parameter_patch":{"shellCommand":"rm"},"needs_review":true,"questions":[]}',
            }
        ),
    )
    with pytest.raises(AIProxyError, match="unsupported parameter"):
        AIProxy().converse("修改模型")


def test_verified_local_drawing_does_not_substitute_for_remote_model(monkeypatch):
    """Even calibrated drawing values must not masquerade as model output."""
    from app import ai_proxy
    from app.recognition import canonical_bracket_parameters

    drawing = {
        "status": "confirmed",
        "parameters": canonical_bracket_parameters().model_dump(by_alias=True),
        "id": "drw_verified",
    }
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: drawing)
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY_FILE", raising=False)

    result = AIProxy().converse(
        "请生成三维模型",
        files=(AIFile("drawing.jpg", "image/jpeg", b"fixture"),),
    )
    assert result.provider["mode"] == "local-fallback"
    assert result.needs_review is True
    assert result.parameter_patch == {}
    assert "没有写入任何自动候选参数" in result.message


def test_attachment_never_uses_local_text_parser_as_model_output(monkeypatch):
    """Upload candidates must come from the multimodal model only."""
    from app import ai_proxy

    monkeypatch.setattr(
        ai_proxy,
        "_recognize_attachment",
        lambda _item: {"status": "needs_review", "parameters": {}},
    )
    monkeypatch.delenv("JOYNIU_AI_API_KEY", raising=False)
    monkeypatch.delenv("JOYNIU_AI_API_KEY_FILE", raising=False)
    result = AIProxy().converse(
        "把底板长度改为 110 mm",
        model_state={"kind": "bracket"},
        files=(AIFile("drawing.pdf", "application/pdf", b"pdf"),),
    )
    assert result.parameter_patch == {}
    assert result.needs_review is True


def test_local_dimension_grammar_prefers_target_value_and_separates_keyway_length(monkeypatch):
    """Offline edits should use the new value, not an old dimension token."""
    from app.ai_proxy import _text_parameter_patch

    assert _text_parameter_patch("把底板长度从100改为90", {"kind": "bracket"}) == {"baseLength": 90}
    assert _text_parameter_patch("修改 R15 为 R12", {"kind": "bracket"}) == {"notchRadius": 12}
    assert _text_parameter_patch("把键槽长度改为45", {"kind": "shaft"}) == {"keywayLength": 45}
    assert _text_parameter_patch(
        "把轴长度改为80，同时把键槽长度改为45", {"kind": "shaft"}
    ) == {"length": 80, "keywayLength": 45}


def _client_for(services):
    app = FastAPI()
    app.include_router(create_platform_router(services), prefix="/api/v1")
    return TestClient(app)


def _auth_for(services, user):
    return {"Authorization": f"Bearer {services.auth.issue_token(user).token}"}


def test_ai_route_requires_auth_and_restricts_viewer():
    services = build_platform_services(":memory:", auth_secret="a" * 32)
    viewer = services.auth.create_user("viewer@example.com", "long-password", "Viewer", roles=["viewer"])
    client = _client_for(services)
    assert "/api/v1/ai/conversation" in client.app.openapi()["paths"]
    assert client.post("/api/v1/ai/conversation", data={"message": "修改长度"}).status_code == 401
    assert client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, viewer),
        data={"message": "修改长度"},
    ).status_code == 403


def test_ai_route_passes_file_and_turn_state_to_injected_proxy():
    services = build_platform_services(":memory:", auth_secret="b" * 32)
    designer = services.auth.create_user("designer@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        def __init__(self):
            self.calls = []

        def converse(self, message, *, previous_response_id, model_state, files):
            self.calls.append((message, previous_response_id, model_state, files))
            return AIConversationResult("resp_fake_next", "已根据图纸更新", {"baseLength": 110}, True, ("请确认单位",))

    fake = FakeAI()
    services.ai = fake
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, designer),
        data={
            "message": "把底板加长",
            "previous_response_id": "resp_previous",
            "model_state_json": json.dumps({"kind": "bracket", "baseLength": 100}),
        },
        files={"file": ("drawing.pdf", b"%PDF-test", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["responseId"] == "resp_fake_next"
    assert payload["message"] == "已根据图纸更新"
    assert payload["parameterPatch"] == {"baseLength": 110}
    assert payload["needsReview"] is True
    assert payload["questions"] == ["请确认单位"]
    # The id is from the platform OCR service, not the compatibility AI
    # recognizer, so it can be used by /brackets/generate and reviewer routes.
    drawing = payload["drawingRecognition"]
    assert drawing["status"] == "needs_review"
    assert drawing["id"] in services.recognitions
    # This synthetic PDF has no deterministic recipe; the compatibility alias
    # is still present and intentionally empty until reviewer confirmation.
    assert drawing["parameters"] == {}
    assert drawing["candidateParameters"] == {"baseLength": 110}
    assert drawing["engine"] == "ai-candidate"
    assert services.recognitions[drawing["id"]].source_filename == "drawing.pdf"
    assert fake.calls[0][0] == "把底板加长"
    assert fake.calls[0][1] == "resp_previous"
    assert fake.calls[0][2]["baseLength"] == 100
    assert fake.calls[0][3][0].filename == "drawing.pdf"
    assert fake.calls[0][3][0].data == b"%PDF-test"


def test_ai_stream_route_emits_live_updates_and_terminal_validated_result(monkeypatch):
    services = build_platform_services(":memory:", auth_secret="h" * 32)

    class FakeStreamingAI:
        allow_anonymous = True

        def __init__(self):
            self.history = None

        def status(self):
            return {"mode": "remote", "model": "gpt-5.6-sol", "streaming": True, "configured": True}

        def converse(
            self,
            message,
            *,
            previous_response_id,
            model_state,
            files,
            history,
            on_message_update,
            on_status,
        ):
            assert message == "把它改成 120 mm"
            assert model_state["baseLength"] == 100
            assert files == []
            self.history = history
            on_status("正在连接远程大模型…")
            on_message_update("我记得")
            on_message_update("我记得上一轮，现在改为 120 mm。")
            return AIConversationResult(
                "resp_stream_route",
                "我记得上一轮，现在改为 120 mm。",
                {"baseLength": 120},
                False,
                (),
                provider=self.status(),
            )

    fake = FakeStreamingAI()
    services.ai = fake
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("JOYNIU_ENV", "development")
    response = _client_for(services).post(
        "/api/v1/ai/conversation/stream",
        data={
            "message": "把它改成 120 mm",
            "model_state_json": json.dumps({"kind": "bracket", "baseLength": 100}),
            "history_json": json.dumps(
                [
                    {"role": "user", "text": "当前底板多长？"},
                    {"role": "assistant", "text": "当前是 100 mm。"},
                ],
                ensure_ascii=False,
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: turn.started" in response.text
    assert response.text.count("event: assistant.delta") == 2
    assert "event: turn.result" in response.text
    assert '"baseLength":120' in response.text
    assert "event: turn.done" in response.text
    assert fake.history[0]["text"] == "当前底板多长？"


def test_ai_route_drops_unregistered_compatibility_recognition(monkeypatch):
    services = build_platform_services(":memory:", auth_secret="e" * 32)
    designer = services.auth.create_user("compat@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        def converse(self, message, *, previous_response_id, model_state, files):
            return AIConversationResult(
                "resp_compat", "收到", {}, True, (),
                drawing={"id": "compat_not_registered", "status": "confirmed"},
            )

    class BrokenOCR:
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("parser unavailable")

    services.ai = FakeAI()
    services.ocr = BrokenOCR()
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, designer),
        data={"message": "解析"},
        files={"file": ("drawing.dxf", b"0\nSECTION", "application/dxf")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "drawingRecognition" not in payload
    assert payload["needsReview"] is True


def test_ai_route_explicit_anonymous_demo_accepts_file_alias(monkeypatch):
    """Guest mode is opt-in and accepts the common singular ``file`` field."""
    services = build_platform_services(":memory:", auth_secret="d" * 32)
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")

    class FakeAI:
        allow_anonymous = True

        def converse(self, message, *, previous_response_id, model_state, files):
            assert message == "解析图纸"
            assert len(files) == 1 and files[0].filename == "drawing.dwg"
            return AIConversationResult("local_guest", "已收到", {}, True, ("请确认",))

    services.ai = FakeAI()
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/chat",
        data={"message": "解析图纸"},
        files={"file": ("drawing.dwg", b"AC10", "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["responseId"] == "local_guest"
    assert payload["attachments"][0]["filename"] == "drawing.dwg"
    assert payload["drawingRecognition"]["status"] == "needs_review"


def test_ai_chat_alias_preserves_camel_case_turn_state():
    services = build_platform_services(":memory:", auth_secret="f" * 32)
    designer = services.auth.create_user("camel@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        allow_anonymous = False

        def __init__(self):
            self.call = None

        def converse(self, message, *, previous_response_id, model_state, files):
            self.call = (message, previous_response_id, model_state)
            return AIConversationResult("resp_camel", "ok", {}, False, ())

    fake = FakeAI()
    services.ai = fake
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/chat",
        headers=_auth_for(services, designer),
        data={
            "message": "继续",
            "previousResponseId": "resp_previous",
            "modelState": json.dumps({"kind": "bracket", "baseLength": 100}),
        },
    )
    assert response.status_code == 200, response.text
    assert fake.call == ("继续", "resp_previous", {"kind": "bracket", "baseLength": 100})


def test_ai_route_rejects_malformed_model_state_as_validation_error():
    services = build_platform_services(":memory:", auth_secret="g" * 32)
    designer = services.auth.create_user("bad-state@example.com", "long-password", "Designer", roles=["designer"])

    class FakeAI:
        allow_anonymous = False

        def converse(self, **_kwargs):  # pragma: no cover - must not be called
            raise AssertionError("malformed state should be rejected before AI invocation")

    services.ai = FakeAI()
    client = _client_for(services)
    response = client.post(
        "/api/v1/ai/conversation",
        headers=_auth_for(services, designer),
        data={"message": "继续", "model_state_json": "{not-json"},
    )
    assert response.status_code == 422, response.text


def test_platform_recognition_id_is_accepted_by_geometry_generate(monkeypatch):
    """AI-upload ids must cross the platform/legacy geometry boundary."""
    from types import SimpleNamespace

    from app import main as geometry_main

    services = build_platform_services(":memory:", auth_secret="c" * 32)
    drawing_id = "drw_platform_confirmed"
    services.recognitions[drawing_id] = SimpleNamespace(
        id=drawing_id,
        status="confirmed",
        source_sha256="unavailable-for-this-regression",
    )
    monkeypatch.setattr(geometry_main, "platform_services", services)
    client = TestClient(geometry_main.app)
    response = client.post(
        "/api/brackets/generate",
        json={
            "sourceDrawingId": drawing_id,
            "formats": ["glb"],
            "parameters": {
                "baseLength": 100,
                "baseWidth": 50,
                "baseThickness": 10,
                "upperLength": 70,
                "upperWidth": 50,
                "upperHeight": 30,
                "totalHeight": 40,
                "notchOpening": 40,
                "notchRadius": 15,
                "slotLength": 30,
                "slotWidth": 10,
                "pocketDepth": 10,
                "saddleDepth": 50,
                "holeDepth": 40,
                "holeThrough": True,
                "bossDiameter": 20,
                "bossCenterDistance": 70,
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["sourceDrawingId"] == drawing_id
