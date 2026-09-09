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


def _parse_patch_envelope(**fields):
    return _parse_result({
        "id": "resp_patch_envelope",
        "output_text": json.dumps({"message": "将外径调整为 36 mm", "needs_review": False, "questions": [], **fields}),
    })


@pytest.mark.parametrize("container", ["parameters", "candidate_parameters", "candidateParameters", "parameter_patch", "parameterPatch", "recognized_parameters", "recognizedParameters"])
def test_parse_result_accepts_top_level_remote_parameter_containers(container):
    result = _parse_patch_envelope(**{container: {"outer_diameter": 36}})
    assert result.parameter_patch == {"outerDiameter": 36}
    assert result.to_dict()["parameterPatch"] == {"outerDiameter": 36}


def test_parse_result_merges_fields_with_canonical_camel_patch_priority():
    result = _parse_patch_envelope(
        parameters={"outerDiameter": 24, "length": 70, "holeDiameter": 8},
        candidate_parameters={"outerDiameter": 26, "length": 75},
        candidateParameters={"outerDiameter": 28, "length": 80},
        parameter_patch={"outerDiameter": 32, "length": None, "keywayDepth": 3},
        parameterPatch={"outer_diameter": 36, "outerDiameter": 40, "keywayLength": 45},
    )
    assert result.parameter_patch == {"outerDiameter": 40, "length": 80, "holeDiameter": 8, "keywayDepth": 3, "keywayLength": 45}


def test_parse_result_empty_snake_patch_does_not_erase_camel_edit():
    result = _parse_patch_envelope(parameter_patch={}, parameterPatch={"outerDiameter": 36})
    assert result.parameter_patch == {"outerDiameter": 36}


def test_legacy_text_response_without_identity_still_preserves_explicit_parameters():
    result = _parse_patch_envelope(parameter_patch={"outerDiameter": 36, "keywayLength": 40})
    assert result.part_type == "unknown"
    assert result.recipe_id == ""
    assert result.parameter_patch == {"outerDiameter": 36, "keywayLength": 40}


def test_unknown_identity_is_supported_without_selecting_a_recipe_from_material():
    from app.ai_proxy import response_schema

    schema = response_schema()
    assert "unknown" in schema["properties"]["part_type"]["enum"]
    assert "" in schema["properties"]["recipe_id"]["enum"]
    result = _parse_patch_envelope(part_type="unknown", recipe_id="", parameter_patch={"material": "AL6061 铝合金"})
    assert result.part_type == "unknown"
    assert result.recipe_id == ""
    assert result.parameter_patch == {"material": "AL6061 铝合金"}


@pytest.mark.parametrize("canonical", [{}, None, {"outerDiameter": None}])
def test_parse_result_explicit_edit_container_never_revives_recognition_snapshot(canonical):
    result = _parse_patch_envelope(
        parameterPatch=canonical,
        recognized_parameters={"outerDiameter": 70},
        recognizedParameters={"outerDiameter": 80},
        drawingRecognition={"parameters": {"outerDiameter": 90}},
    )
    assert result.parameter_patch == {}


def test_parse_result_never_promotes_nested_drawing_recognition_to_an_edit():
    result = _parse_patch_envelope(drawingRecognition={"candidateParameters": {"outerDiameter": 90}})
    assert result.parameter_patch == {}


def test_parse_result_merging_preserves_zero_offsets_and_false_booleans():
    result = _parse_patch_envelope(
        candidateParameters={"insertAxialOffset": 2, "holeThrough": True},
        parameterPatch={"insert_axial_offset": 0, "holeThrough": False},
    )
    assert result.parameter_patch == {"insertAxialOffset": 0, "holeThrough": False}


@pytest.mark.parametrize("patch,expected", [({"outerDiameter": "36"}, "invalid numeric parameter"), ({"shellCommand": "ignore"}, "unsupported parameter")])
def test_parse_result_compatibility_containers_still_enforce_parameter_allowlist(patch, expected):
    with pytest.raises(AIProxyError, match=expected):
        _parse_patch_envelope(candidateParameters=patch)


def test_parse_result_rejects_malformed_compatibility_container():
    with pytest.raises(AIProxyError, match="invalid parameter patch"):
        _parse_patch_envelope(parameterPatch="outerDiameter=36")


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


def test_parse_result_accepts_complete_stepped_nozzle_candidate_and_zero_offset():
    patch = {
        "mainLength": 98,
        "headLength": 50,
        "neckLength": 20,
        "headLeftDiameter": 54.25449350717895,
        "headRightDiameter": 56,
        "neckDiameter": 30,
        "tipDiameter": 25,
        "counterboreDiameter": 40,
        "counterboreDepth": 40,
        "axialBoreDiameter": 13,
        "outletDiameter": 17,
        "outletTaperHalfAngle": 15,
        "insertOuterDiameter": 39.4,
        "insertLength": 40,
        "insertThreadDesignation": "M12",
        "insertAxialOffset": 0,
        "units": "mm",
    }
    result = _parse_result(
        {
            "id": "resp_stepped_nozzle",
            "output_text": json.dumps(
                {
                    "message": "已从DWG尺寸与轮廓形成两实体候选",
                    "part_type": "stepped_tapered_nozzle",
                    "recipe_id": "stepped_tapered_nozzle_with_insert_v1",
                    "parameter_patch": patch,
                    "parameter_evidence": [
                        {
                            "parameter": "mainLength",
                            "sourceType": "direct_dimension",
                            "dimensionId": "dimension_00013",
                        },
                        {
                            "parameter": "outletTaperHalfAngle",
                            "sourceType": "vector_derived",
                            "dimensionId": "dimension_00015",
                        },
                    ],
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "stepped_tapered_nozzle"
    assert result.recipe_id == "stepped_tapered_nozzle_with_insert_v1"
    assert result.parameter_patch == patch
    assert result.parameter_patch["insertAxialOffset"] == 0.0
    assert result.parameter_evidence["mainLength"]["sourceType"] == "direct_dimension"
    assert result.parameter_evidence["outletTaperHalfAngle"]["sourceType"] == "vector_derived"


def test_parse_result_infers_stepped_nozzle_from_recipe_unique_fields():
    result = _parse_result(
        {
            "id": "resp_stepped_inferred",
            "output_text": json.dumps(
                {
                    "message": "识别到独立镶件与出口锥",
                    "parameter_patch": {
                        "mainLength": 98,
                        "insertOuterDiameter": 39.4,
                        "insertThreadDesignation": "M12",
                        "insertAxialOffset": 0,
                    },
                    "needs_review": True,
                    "questions": [],
                }
            ),
        }
    )

    assert result.part_type == "stepped_tapered_nozzle"
    assert result.recipe_id == "stepped_tapered_nozzle_with_insert_v1"


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


def test_blank_design_text_request_can_return_explicit_shaft_identity_and_keyway_fields(monkeypatch):
    captured = {}
    shaft_patch = {"outerDiameter": 36, "length": 100, "holeDiameter": 10, "keywayWidth": 6, "keywayDepth": 3, "keywayLength": 40}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        schema = captured["body"]["text"]["format"]["schema"]
        # A strict Responses schema previously prohibited these identity keys,
        # leaving the UI to mistake generic slot fields for a saddle bracket.
        assert schema["additionalProperties"] is False
        assert {"part_type", "recipe_id"}.issubset(schema["required"])
        assert "shaft" in schema["properties"]["part_type"]["enum"]
        assert "shaft_v1" in schema["properties"]["recipe_id"]["enum"]
        parameter_properties = schema["properties"]["parameter_patch"]["properties"]
        assert "shaft/shaft_v1 only" in parameter_properties["keywayLength"]["description"]
        assert "never a shaft keyway" in parameter_properties["slotLength"]["description"]
        return _Response({
            "id": "resp_blank_shaft",
            "output_text": json.dumps({
                "message": "将创建外径36、总长100、通孔10，键槽6×3×40的轴。",
                "part_type": "shaft", "recipe_id": "shaft_v1",
                "parameter_patch": {key: shaft_patch.get(key) for key in parameter_properties},
                "needs_review": False, "questions": [],
            }),
        })

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = AIProxy(timeout_seconds=7).converse(
        "创建一个轴，外径36、总长100、通孔10，键槽宽6、深3、长40。",
        model_state={"kind": "", "name": "AI从空白建模", "material": "45# 钢"},
    )
    assert result.part_type == "shaft"
    assert result.recipe_id == "shaft_v1"
    assert result.parameter_patch == shaft_patch
    instructions = captured["body"]["instructions"]
    assert "keywayLength=键槽长度" in instructions
    assert "A shaft keyway is never slotLength, slotWidth or pocketDepth" in instructions
    context = "\n".join(item["text"] for item in captured["body"]["input"][-1]["content"])
    assert "The current design is empty" in context
    assert "Do not assume a default shaft or bracket" in context


def test_blank_design_guard_survives_schema_free_relay_retry():
    from app.ai_proxy import _provider_body

    body = _provider_body("把材料改为铝", {"kind": "", "name": "空白零件"}, (), None, include_schema=False)
    assert "text" not in body
    assert "part_type=unknown" in body["instructions"]
    context = "\n".join(item["text"] for item in body["input"][-1]["content"])
    assert "material-only request without a part" in context


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
    assert updates == [result.message]
    assert result.message.startswith("当前候选参数（专项复读后")
    assert "圆筒轴距后缘：35 mm" in result.message
    assert "横孔中心绝对高度：40 mm → 55 mm" in result.message
    assert "早期分析记录，尺寸结论已被当前候选取代：\n\n> 三轮远程裁决完成" in result.message
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
    assert updates == [result.message]
    assert result.message.startswith("当前候选参数（专项复读后")
    assert "本轮修正或撤回" not in result.message
    assert "早期分析记录，尺寸结论已被当前候选取代：\n\n> 一致的开口夹紧座候选，第 3 轮" in result.message
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


def test_explicit_unknown_review_withdraws_old_recipe_and_dimensions():
    from app.ai_proxy import _merge_remote_review_result

    base = AIConversationResult("resp_old", "支架候选", {"upperHeight": 55, "totalHeight": 63, "bossDiameter": 30}, True, (), part_type="bracket", recipe_id="bracket_support_v1")
    rejection = _parse_patch_envelope(part_type="unknown", recipe_id="", parameter_patch={})
    merged = _merge_remote_review_result(base, rejection)
    assert merged.part_type == "unknown"
    assert merged.recipe_id == ""
    assert merged.parameter_patch == {}
    assert merged.recipe_compatibility["status"] == "uncertain"
    assert merged.recipe_compatibility["unsupportedFeatures"]


def test_unsupported_remote_features_are_visible_and_cannot_select_a_similar_recipe():
    result = _parse_result({
        "id": "resp_unrepresentable",
        "output_text": json.dumps({
            "message": "存在无法表达的倾斜支臂", "part_type": "bracket", "recipe_id": "bracket_support_v1",
            "parameter_patch": {"baseLength": 100, "obliqueArmAngle": 35},
            "needs_review": False, "questions": [],
        }),
    }, tolerate_patch_errors=True)
    assert result.parameter_patch == {"baseLength": 100}
    assert result.part_type == "unknown"
    assert result.recipe_id == ""
    assert result.needs_review is True
    assert "obliqueArmAngle" in " ".join(result.questions)
    assert result.to_dict()["recipeCompatibility"]["unsupportedFeatures"]


def test_cross_recipe_geometry_is_not_silently_accepted_as_a_bracket():
    result = _parse_patch_envelope(part_type="bracket", recipe_id="bracket_support_v1", parameter_patch={"bossDiameter": 30, "archOuterRadius": 28, "earHoleDiameter": 13})
    assert result.part_type == "unknown"
    assert result.recipe_compatibility["status"] == "uncertain"
    assert "earHoleDiameter" in " ".join(result.questions)


def test_mixed_exclusive_recipe_fields_without_identity_remain_unknown():
    result = _parse_patch_envelope(parameter_patch={"archOuterRadius": 32, "pedestalOuterRadius": 33})
    assert result.part_type == "unknown"
    assert result.recipe_id == ""
    assert result.recipe_compatibility["status"] == "uncertain"


def test_supported_label_cannot_override_an_explicit_unrepresented_feature():
    result = _parse_patch_envelope(part_type="bracket", recipe_id="bracket_support_v1", parameter_patch={"baseLength": 100}, recipe_compatibility={"status": "supported", "unsupportedFeatures": ["斜向封闭筋板尚不能表达"]})
    assert result.part_type == "unknown"
    assert result.recipe_compatibility["status"] == "uncertain"
    assert result.needs_review is True


def test_new_clevis_identity_is_allowed_in_schema_and_platform_registration():
    from app.ai_proxy import response_schema
    from app.platform_api import _supported_ai_recipe_identity

    schema = response_schema()
    assert "arched_clevis_support" in schema["properties"]["part_type"]["enum"]
    assert "arched_clevis_support_v1" in schema["properties"]["recipe_id"]["enum"]
    assert _supported_ai_recipe_identity("arched_clevis_support", "arched_clevis_support_v1")


def test_arched_clevis_remote_review_changes_topology_without_inheriting_bracket_fields(monkeypatch):
    from app import ai_proxy

    patch = {"archOuterRadius": 32, "archInnerRadius": 18, "baseWidth": 60, "baseThickness": 10, "earRadius": 16, "earHoleDiameter": 14, "earCenterHeight": 45, "earThickness": 12, "earGap": 36, "mountEarRadius": 18, "mountHoleDiameter": 14, "mountHoleCenterDistance": 92}
    bodies = []
    answers = [
        {"message": "首轮误判", "part_type": "bracket", "recipe_id": "bracket_support_v1", "parameter_patch": {"baseLength": 100, "bossDiameter": 30}},
        {"message": "重新识别为双耳拱形支座", "part_type": "arched_clevis_support", "recipe_id": "arched_clevis_support_v1", "parameter_patch": patch},
        {"message": "独立复核拱体和分离耳板", "part_type": "arched_clevis_support", "recipe_id": "arched_clevis_support_v1", "parameter_patch": patch},
    ]
    def fake_urlopen(request, timeout):
        bodies.append(json.loads(request.data.decode()))
        answer = {**answers[min(len(bodies) - 1, 2)], "needs_review": True, "questions": [], "recipe_compatibility": {"status": "supported", "unsupportedFeatures": []}}
        answer["parameter_evidence"] = _arched_test_evidence(answer["parameter_patch"])
        return _Response({"id": f"resp_clevis_{len(bodies)}", "output_text": json.dumps(answer)})
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _: None)
    result = AIProxy(timeout_seconds=7).converse("请按图建模", files=(AIFile("customer-design.png", "image/png", base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+ A8AAQUBAScY42YAAAAASUVORK5CYII=".replace(" ", ""))),))
    assert len(bodies) == 7  # Changed identity requires arbitration, then four focused votes.
    assert result.part_type == "arched_clevis_support"
    assert result.recipe_id == "arched_clevis_support_v1"
    assert result.parameter_patch == patch
    assert "bossDiameter" not in result.parameter_patch
    assert result.recipe_compatibility["status"] == "supported"
    prompts = json.dumps(bodies, ensure_ascii=False)
    assert "earCenterHeight+earRadius" in prompts
    assert "也可能错误，必须独立接受或否定" in prompts
    assert "archOuterRadius=外拱半径" in prompts


def _arched_test_evidence(patch):
    return {key: {"sourceView": "俯视图" if key == "mountHoleCenterDistance" else "主视图", "sourceText": str(value), "derivation": "沿箭头及尺寸界线追踪到对应特征", "confidence": 0.9} for key, value in patch.items()}


@pytest.mark.parametrize("pitch_votes,expected_pitch", [([92, 92], 92), ([56, 56, 92], None), ([92, 94, 96], None)])
def test_arched_focused_reads_correct_ambiguous_dimensions_and_withdraw_unconfirmed_pitch(monkeypatch, pitch_votes, expected_pitch):
    from PIL import Image
    from app import ai_proxy

    original_patch = {"archOuterRadius": 29, "archInnerRadius": 18, "baseWidth": 60, "baseThickness": 6, "earRadius": 16, "earHoleDiameter": 14, "earCenterHeight": 45, "earThickness": 12, "earGap": 36, "mountEarRadius": 18, "mountHoleDiameter": 14, "mountHoleCenterDistance": 56}
    output = io.BytesIO()
    Image.new("RGB", (1000, 700), "white").save(output, format="PNG")
    bodies = []
    mounting_calls = []
    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode())
        bodies.append(body)
        prompt = "\n".join(item.get("text", "") for item in body["input"][-1]["content"])
        if "独立主视图尺寸复读" in prompt:
            patch = {"archOuterRadius": 32, "baseThickness": 10}
        elif "独立俯视图孔距复读" in prompt:
            patch = {"mountHoleCenterDistance": pitch_votes[len(mounting_calls)]}
            mounting_calls.append(body)
        else:
            patch = original_patch
        answer = {"message": "从图纸复读候选", "part_type": "arched_clevis_support", "recipe_id": "arched_clevis_support_v1", "parameter_patch": patch, "parameter_evidence": _arched_test_evidence(patch), "needs_review": True, "questions": [], "recipe_compatibility": {"status": "supported", "unsupportedFeatures": []}}
        return _Response({"id": f"resp_focus_{len(bodies)}", "output_text": json.dumps(answer)})
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _: None)

    result = AIProxy(timeout_seconds=7).converse("请按图建模", files=(AIFile("unrelated-customer-drawing.png", "image/png", output.getvalue()),))

    assert len(bodies) == 4 + len(pitch_votes)  # Two agreeing full passes replace a third full pass.
    assert result.parameter_patch["archOuterRadius"] == 32
    assert result.parameter_patch["baseThickness"] == 10
    assert result.parameter_evidence["archOuterRadius"]["sourceText"] == "32"
    assert result.parameter_evidence["baseThickness"]["sourceText"] == "10"
    for key, value in original_patch.items():
        if key not in {"archOuterRadius", "baseThickness", "mountHoleCenterDistance"}:
            assert result.parameter_patch[key] == value
    if expected_pitch is not None:
        assert result.parameter_patch["mountHoleCenterDistance"] == expected_pitch
        assert result.parameter_evidence["mountHoleCenterDistance"]["sourceText"] == str(expected_pitch)
        assert not result.questions
    else:
        assert "mountHoleCenterDistance" not in result.parameter_patch
        assert "mountHoleCenterDistance" not in result.parameter_evidence
        assert any("mountHoleCenterDistance" in question for question in result.questions)
        if pitch_votes[0] == 56:
            assert any("mountHoleCenterDistance/2 - mountHoleDiameter/2 > archOuterRadius" in question for question in result.questions)
    assert result.needs_review is True
    assert result.message.startswith("当前候选参数（专项复读后")
    summary = result.message.split("早期分析记录，尺寸结论已被当前候选取代：")[0]
    assert "外拱半径：32 mm" in summary
    assert "底板厚度：10 mm" in summary
    if expected_pitch is None:
        assert "缺失待确认：安装孔中心距" in summary
        assert "安装孔中心距：已撤回早期读值 56 mm" in summary
    else:
        assert "安装孔中心距：92 mm" in summary
    for body in bodies[2:]:
        prompt = "\n".join(item.get("text", "") for item in body["input"][-1]["content"])
        assert "不能假定主视图或俯视图位于固定象限" in prompt
        assert "若目标不在局部块，回到全图" in prompt
        assert "必须给出全部必需字段" not in prompt
        assert "待审校的远程候选JSON" not in prompt
        assert "\"archOuterRadius\":29" not in prompt
        assert "\"mountHoleCenterDistance\":56" not in prompt
        images = [item for item in body["input"][-1]["content"] if item["type"] == "input_image"]
        assert len(images) >= 2  # Original full drawing remains available if the assumed crop misses a view.
        dimensions = []
        for item in images:
            assert item["detail"] == "high"
            with Image.open(io.BytesIO(base64.b64decode(item["image_url"].split(",", 1)[1]))) as image:
                dimensions.append(image.size)
        assert (1000, 700) in dimensions
        assert any(max(size) == 1800 for size in dimensions)


def test_arched_focus_requires_reading_evidence_and_never_reuses_old_value_evidence():
    from app.ai_proxy import _merge_focused_remote_result, _reject_inconsistent_arched_focus

    base = AIConversationResult("before", "之前读值", {"archOuterRadius": 29, "archInnerRadius": 18}, True, (), part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1", parameter_evidence=_arched_test_evidence({"archOuterRadius": 29}))
    vote = AIConversationResult("after", "新读值", {"archOuterRadius": 32}, True, (), part_type=base.part_type, recipe_id=base.recipe_id)
    allowed = frozenset({"archOuterRadius"})
    rejected = _reject_inconsistent_arched_focus(base, vote, allowed)
    assert rejected.parameter_patch == {}
    assert any("尺寸界线证据" in question for question in rejected.questions)
    merged = _merge_focused_remote_result(base, vote, allowed, "专项复读")
    assert merged.parameter_patch["archOuterRadius"] == 32
    assert "archOuterRadius" not in merged.parameter_evidence


def test_focused_final_summary_uses_current_values_and_preserves_unresolved_question_origins():
    from dataclasses import replace
    from app.ai_proxy import _finalize_focused_summary

    before = AIConversationResult("before", "结构假设为带分离耳板的拱形支座。外拱半径 29 mm，底厚 6 mm。", {"archOuterRadius": 29, "baseThickness": 6, "mountHoleCenterDistance": 56}, True, ("底厚 6 mm 是否正确？", "未注明材料与公差。"), part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1")
    final = replace(before, parameter_patch={"archOuterRadius": 32, "baseThickness": 10}, parameter_evidence=_arched_test_evidence({"archOuterRadius": 32, "baseThickness": 10}), questions=(*before.questions, "安装孔距的尺寸线仍不清晰。"))
    result = _finalize_focused_summary(before, final, frozenset({"archOuterRadius", "baseThickness", "mountHoleCenterDistance"}))

    current, historical = result.message.split("早期分析记录，尺寸结论已被当前候选取代：")
    assert current.startswith("当前候选参数")
    assert "外拱半径：32 mm" in current
    assert "底板厚度：10 mm" in current
    assert "外拱半径：29 mm → 32 mm" in current
    assert "底板厚度：6 mm → 10 mm" in current
    assert "安装孔中心距：已撤回早期读值 56 mm，待人工确认" in current
    assert "缺失待确认：" in current
    assert "分离耳板" not in current
    assert "> " + before.message in historical
    assert len(result.questions) == 3
    assert result.questions[0].startswith("早期待核查问题（旧读数以当前候选为准")
    assert result.questions[0].endswith("底厚 6 mm 是否正确？")
    assert result.questions[1].endswith("未注明材料与公差。")
    assert result.questions[2] == "专项待解决问题：安装孔距的尺寸线仍不清晰。"
    assert result.parameter_patch == final.parameter_patch
    assert result.parameter_evidence == final.parameter_evidence


def test_focused_summary_formats_hole_count_as_count_and_labels_existing_split_recipe():
    from app.ai_proxy import _finalize_focused_summary

    candidate = AIConversationResult("split", "开口夹紧座", {"mountHoleCount": 4, "pedestalHeight": 27.5, "material": "Q235", "units": "mm"}, True, (), part_type="split_clamp_support", recipe_id="split_clamp_support_v1")
    result = _finalize_focused_summary(candidate, candidate, frozenset({"pedestalHeight"}))
    assert "安装孔数量：4 个" in result.message
    assert "低圆筒净高：27.5 mm" in result.message
    assert "材料：Q235" in result.message
    assert "本轮修正或撤回" not in result.message


def test_summary_does_not_change_a_path_without_focused_reviews():
    from app.ai_proxy import _finalize_focused_summary

    candidate = _parse_patch_envelope(part_type="shaft", recipe_id="shaft_v1", parameter_patch={"outerDiameter": 36}, questions=["还需要轴长。"])
    assert _finalize_focused_summary(candidate, candidate, frozenset()) is candidate


def test_focused_summary_has_chinese_labels_for_all_supported_focused_recipe_fields():
    from app.ai_proxy import _ARCHED_CLEVIS_REVIEW_FIELDS, _FOCUSED_PARAMETER_LABELS, _SPLIT_CLAMP_REVIEW_FIELDS

    assert (_ARCHED_CLEVIS_REVIEW_FIELDS | _SPLIT_CLAMP_REVIEW_FIELDS).issubset(_FOCUSED_PARAMETER_LABELS)


def test_rotated_reading_tiles_keep_original_pixels_and_use_opposite_orientations():
    from PIL import Image
    from app.ai_proxy import _focused_review_files

    source = Image.new("RGB", (100, 60), "white")
    source.paste((255, 0, 0), (0, 0, 20, 20))
    output = io.BytesIO()
    source.save(output, format="PNG")
    original = AIFile("layout.png", "image/png", output.getvalue())
    tile = AIFile("layout__detail-top-left.png", "image/png", output.getvalue())
    left = _focused_review_files((original, tile), "top-left", 0, keep_originals=True, rotate_for_reading=True)
    right = _focused_review_files((original, tile), "top-left", 1, keep_originals=True, rotate_for_reading=True)
    assert left[0] is original and right[0] is original
    assert len(left) == len(right) == 2
    assert "reading-90" in left[1].filename
    assert "reading-270" in right[1].filename
    with Image.open(io.BytesIO(left[1].data)) as rotated_left, Image.open(io.BytesIO(right[1].data)) as rotated_right:
        assert rotated_left.size == rotated_right.size == (60, 100)
        assert rotated_left.getpixel((5, 94))[0] > 230 and rotated_left.getpixel((5, 94))[1] < 30
        assert rotated_right.getpixel((54, 5))[0] > 230 and rotated_right.getpixel((54, 5))[1] < 30


def test_arched_focus_accepts_equivalent_evidence_field_names_and_structured_derivation():
    from app.ai_proxy import _reject_inconsistent_arched_focus

    base = _parse_patch_envelope(part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1", parameter_patch={"archOuterRadius": 32, "mountHoleDiameter": 14})
    vote = _parse_patch_envelope(part_type=base.part_type, recipe_id=base.recipe_id, parameter_patch={"mountHoleCenterDistance": 92}, parameter_evidence={"mount_hole_center_distance": {"view": "俯视图", "raw_text": "92", "derive": ["尺寸界线穿过左右孔圆心", {"mapping": "中心距直接读值"}]}})
    checked = _reject_inconsistent_arched_focus(base, vote, frozenset({"mountHoleCenterDistance"}))
    assert checked.parameter_patch == {"mountHoleCenterDistance": 92}
    assert checked.parameter_evidence["mountHoleCenterDistance"]["sourceView"] == "俯视图"
    assert "左右孔圆心" in checked.parameter_evidence["mountHoleCenterDistance"]["derivation"]
    assert checked.questions == ()


@pytest.mark.parametrize("evidence_shape", ["list", "object"])
def test_single_geometric_parameter_safely_binds_unlabelled_evidence_even_with_material_units(evidence_shape):
    row = {"sourceView": "俯视图", "sourceText": "92", "derivation": "两端延长线分别与两安装孔中心线重合"}
    result = _parse_patch_envelope(part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1", parameter_patch={"mountHoleCenterDistance": 92, "units": "mm", "material": "钢"}, parameter_evidence=[row] if evidence_shape == "list" else row)
    assert result.parameter_evidence["mountHoleCenterDistance"] == row
    assert "material" not in result.parameter_evidence


def test_multiple_geometric_parameters_do_not_guess_unlabelled_evidence_assignment():
    result = _parse_patch_envelope(part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1", parameter_patch={"archOuterRadius": 32, "baseThickness": 10}, parameter_evidence=[{"sourceView": "主视图", "sourceText": "10", "derivation": "尺寸界线映射"}])
    assert result.parameter_evidence == {}


def test_differing_rotated_readings_cannot_be_overruled_by_a_third_matching_vote():
    from app.ai_proxy import _focused_consensus, _orientation_conflicts

    fields = frozenset({"archOuterRadius", "baseThickness"})
    def reading(thickness):
        return _parse_patch_envelope(part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1", parameter_patch={"archOuterRadius": 32, "baseThickness": thickness}, parameter_evidence=_arched_test_evidence({"archOuterRadius": 32, "baseThickness": thickness}))
    results = (reading(12), reading(21), reading(12))
    conflicts = _orientation_conflicts(results, fields)
    assert conflicts == frozenset({"baseThickness"})
    consensus, unresolved = _focused_consensus(results, fields, blocked_fields=conflicts)
    assert consensus.parameter_patch == {"archOuterRadius": 32}
    assert unresolved == frozenset({"baseThickness"})


def test_ambiguous_digit_orientation_cannot_become_a_high_confidence_vote():
    from app.ai_proxy import _reject_inconsistent_arched_focus

    base = _parse_patch_envelope(part_type="arched_clevis_support", recipe_id="arched_clevis_support_v1", parameter_patch={"archOuterRadius": 32})
    evidence = _arched_test_evidence({"baseThickness": 6})
    evidence["baseThickness"].update({"orientationAmbiguous": True, "confidence": 0.99})
    vote = _parse_patch_envelope(part_type=base.part_type, recipe_id=base.recipe_id, parameter_patch={"baseThickness": 6}, parameter_evidence=evidence)
    checked = _reject_inconsistent_arched_focus(base, vote, frozenset({"baseThickness"}))
    assert checked.parameter_patch == {}
    assert "baseThickness" not in checked.parameter_evidence
    assert any("阅读方向仍有歧义" in question for question in checked.questions)


@pytest.mark.parametrize("context_kind,ids,reading,expected", [
    ("image_geometry_candidate", ["dimension_00001"], 98, "ai_interpreted"),
    ("pdf_text_and_geometry_candidates", ["dimension_00001"], 98, "ai_interpreted"),
    ("joyniu.dwg-vector-summary.v1", ["dimension_00001"], 98, "direct_dimension"),
    ("joyniu.dwg-vector-summary.v1", ["dimension_99999"], 98, "ai_interpreted"),
    ("joyniu.dwg-vector-summary.v1", ["dimension_00001"], 89, "ai_interpreted"),
    ("joyniu.dwg-vector-summary.v1", [], 98, "ai_interpreted"),
])
def test_native_dimension_source_requires_matching_server_vector_id_and_measurement(context_kind, ids, reading, expected):
    from app.ai_proxy import _validated_parameter_provenance

    context = {"schemaVersion": context_kind, "sourceType": context_kind, "dimensions": [{"id": "dimension_00001", "measurement": 98}]}
    result = _parse_patch_envelope(part_type="shaft", recipe_id="shaft_v1", parameter_patch={"length": reading}, parameter_evidence={"length": {"sourceType": "dimension", "sourceIds": ids, "sourceText": str(reading)}})
    evidence = _validated_parameter_provenance(result, (context,))
    assert evidence["length"]["sourceType"] == expected
    assert evidence["length"]["provenanceVerified"] is (expected == "direct_dimension")
    assert evidence["length"]["sourceText"] == str(reading)
    assert result.parameter_patch["length"] == reading


def test_vector_derived_label_needs_matching_server_field_evidence_not_only_dimension_ids():
    from app.ai_proxy import _validated_parameter_provenance

    result = _parse_patch_envelope(part_type="shaft", recipe_id="shaft_v1", parameter_patch={"length": 98}, parameter_evidence={"length": {"sourceType": "vector_derived", "sourceIds": ["dimension_00001"]}})
    context = {"schemaVersion": "joyniu.dwg-vector-summary.v1", "dimensions": [{"id": "dimension_00001", "measurement": 100}]}
    assert _validated_parameter_provenance(result, (context,))["length"]["sourceType"] == "ai_interpreted"
    context["vectorParameterCandidates"] = {"fieldCandidates": {"length": {"sourceType": "vector_derived", "sourceIds": ["dimension_00001"], "value": 98}}}
    assert _validated_parameter_provenance(result, (context,))["length"]["sourceType"] == "vector_derived"
    context["vectorParameterCandidates"]["fieldCandidates"]["length"]["value"] = 89
    assert _validated_parameter_provenance(result, (context,))["length"]["sourceType"] == "ai_interpreted"


def test_raster_context_prompt_does_not_claim_native_cad_dimension_evidence():
    from app.ai_proxy import _provider_body

    body = _provider_body("读图", None, (AIFile("image.png", "image/png", b"image"),), None, drawing_contexts=({"sourceType": "image_geometry_candidate", "dimensions": []},))
    prompt = json.dumps(body, ensure_ascii=False)
    assert "IMAGE_ANALYSIS_CONTEXT" in prompt
    assert "CAD_VECTOR_EVIDENCE（" not in prompt
    assert "不是原生尺寸，必须标为ai_interpreted" in prompt


@pytest.mark.parametrize("route", ["/api/v1/ai/conversation", "/api/v1/ai/conversation/stream"])
def test_platform_never_registers_incompatible_drawing_as_confirmable_old_recipe(monkeypatch, route):
    services = build_platform_services(":memory:", auth_secret="u" * 32)
    class FakeAI:
        allow_anonymous = True
        def status(self): return {"mode": "remote", "configured": True}
        def converse(self, *args, **kwargs):
            return AIConversationResult("resp_not_supported", "配方无法表达倾斜支臂", {"baseLength": 100}, True, ("请选择能够表达该形体的配方",), part_type="bracket", recipe_id="bracket_support_v1", recipe_compatibility={"status": "unsupported", "unsupportedFeatures": ["倾斜支臂"]})
    services.ai = FakeAI()
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("JOYNIU_ENV", "development")
    response = _client_for(services).post(route, data={"message": "按图建模"}, files={"file": ("customer-design.pdf", b"%PDF test", "application/pdf")})
    assert response.status_code == 200, response.text
    if route.endswith("/stream"):
        lines = response.text.splitlines()
        payload = json.loads(lines[lines.index("event: turn.result") + 1].removeprefix("data: "))
    else:
        payload = response.json()
    assert payload["partType"] == "unknown"
    assert payload["recipeId"] == ""
    assert payload["parameterPatch"] == {}
    assert "drawingRecognition" not in payload
    assert payload["recipeCompatibility"]["status"] == "unsupported"
    assert payload["attachments"][0]["filename"] == "customer-design.pdf"
    assert not services.recognitions


def test_nozzle_pipeline_keeps_ai_candidate_field_when_later_reviews_do_not_repeat_it(
    monkeypatch,
):
    """Every shown value may come from the model, while later passes refine it."""

    from app import ai_proxy

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode()))
        patch = {
            "mainLength": 98,
            "insertOuterDiameter": 39.4,
            "insertThreadDesignation": "M12",
        }
        if len(calls) == 1:
            patch["insertAxialOffset"] = 0
        return _Response(
            {
                "id": f"resp_nozzle_merge_{len(calls)}",
                "output_text": json.dumps(
                    {
                        "message": "AI候选，待人工确认",
                        "part_type": "stepped_tapered_nozzle",
                        "recipe_id": "stepped_tapered_nozzle_with_insert_v1",
                        "parameter_patch": patch,
                        "parameter_evidence": {
                            "insertAxialOffset": {
                                "sourceType": "ai_interpreted",
                                "confidence": 0.5,
                            }
                        }
                        if len(calls) == 1
                        else {},
                        "needs_review": True,
                        "questions": ["请确认装配轴向基准"],
                    }
                ),
            }
        )

    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: None)
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AIProxy(timeout_seconds=7).converse(
        "解析阶梯锥体",
        files=(AIFile("drawing.png", "image/png", png),),
    )

    assert len(calls) == 3
    assert result.parameter_patch["insertAxialOffset"] == 0
    assert result.parameter_evidence["insertAxialOffset"]["sourceType"] == "ai_interpreted"
    assert result.needs_review is True


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


def test_required_complex_blind_review_failure_keeps_remote_candidate_for_human_review(monkeypatch):
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

    # The split-clamp recipe may still run its focused datum checks after the
    # blind arbitration failed; the already validated candidate must survive.
    assert len(calls) >= 3
    assert result.provider["mode"] == "remote"
    assert result.provider["reviewComplete"] is False
    assert result.provider["lastErrorCode"] == "http_401"
    assert result.parameter_patch == patch
    assert result.needs_review is True
    assert "远程复核未完成" in result.message
    assert any("远程复核未完成" in question for question in result.questions)


def test_dwg_invalid_audit_and_transport_failed_arbitration_keep_extraction_candidate(
    monkeypatch,
):
    """A later relay failure must not erase a validated DWG extraction."""

    from app import ai_proxy
    from app.dwg_preprocessor import DWGPreprocessResult, DWGSourceMetadata

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    raw_dwg = b"AC1021" + b"validated-extraction-review-failure"
    patch = {
        "mainLength": 98,
        "headLength": 50,
        "neckLength": 20,
        "headLeftDiameter": 54.25449350717895,
        "headRightDiameter": 56,
        "neckDiameter": 30,
        "tipDiameter": 25,
        "counterboreDiameter": 40,
        "counterboreDepth": 40,
        "axialBoreDiameter": 13,
        "outletDiameter": 17,
        "outletTaperHalfAngle": 15,
        "insertOuterDiameter": 39.4,
        "insertLength": 40,
        "insertThreadDesignation": "M12",
        "insertAxialOffset": 0,
    }

    def fake_preprocess(payload, filename):
        return DWGPreprocessResult(
            original_metadata=DWGSourceMetadata(
                filename=filename,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature="AC1021",
                version="AutoCAD 2007/2009",
            ),
            converter="libredwg-dwgread",
            dxf_bytes=b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n",
            png_bytes=png,
            summary={
                "schemaVersion": "joyniu.dwg-vector-summary.v1",
                "units": {"code": 4, "name": "Millimeters"},
                "sourceEntityCount": 47,
                "entityCount": 46,
                "dimensions": [{"id": "dimension_00001", "measurement": 98.0}],
                "geometry": [],
            },
            audit_summary={"source": {"filename": filename}},
        )

    calls = []

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode()))
        if len(calls) == 1:
            return _Response(
                {
                    "id": "resp_dwg_extraction",
                    "output_text": json.dumps(
                        {
                            "message": "DWG 远程提取候选",
                            "part_type": "stepped_tapered_nozzle",
                            "recipe_id": "stepped_tapered_nozzle_with_insert_v1",
                            "parameter_patch": patch,
                            "parameter_evidence": {
                                "mainLength": {
                                    "sourceType": "direct_dimension",
                                    "evidenceId": "dimension_00001",
                                }
                            },
                            "needs_review": True,
                            "questions": [],
                        }
                    ),
                }
            )
        if len(calls) == 2:
            return _Response({"id": "resp_bad_audit", "output_text": "not valid JSON"})
        raise urllib.error.URLError("relay disconnected during arbitration")

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("app.dwg_preprocessor.preprocess_dwg", fake_preprocess)
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AIProxy(timeout_seconds=7).converse(
        "解析这个 DWG",
        files=(AIFile("1(1).dwg", "application/acad", raw_dwg),),
    )

    assert len(calls) == 3
    assert result.part_type == "stepped_tapered_nozzle"
    assert result.recipe_id == "stepped_tapered_nozzle_with_insert_v1"
    assert result.parameter_patch == patch
    assert result.parameter_evidence["mainLength"]["sourceType"] == "direct_dimension"
    assert result.provider["mode"] == "remote"
    assert result.provider["reviewComplete"] is False
    assert result.provider["lastErrorCode"] == "transport"
    assert result.needs_review is True
    assert "远程复核未完成" in result.message
    assert any("逐项确认" in question for question in result.questions)


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


def test_raw_dwg_attachment_cannot_be_serialized_to_provider():
    """Defence in depth prevents reintroducing opaque DWG pass-through."""
    from app.ai_proxy import _attachment_content

    with pytest.raises(AIProxyError, match="must be preprocessed"):
        _attachment_content(
            AIFile("source.dwg", "application/acad", b"AC1021opaque-binary")
        )


def test_dwg_is_preprocessed_once_and_every_remote_stage_uses_image_and_vector_evidence(
    monkeypatch,
):
    from app import ai_proxy
    from app.dwg_preprocessor import DWGPreprocessResult, DWGSourceMetadata

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    raw_dwg = b"AC1021" + b"opaque-dwg-binary-that-must-never-reach-the-relay"
    preprocess_calls = []

    def fake_preprocess(payload, filename):
        preprocess_calls.append((payload, filename))
        return DWGPreprocessResult(
            original_metadata=DWGSourceMetadata(
                filename=filename,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature="AC1021",
                version="AutoCAD 2007/2009",
            ),
            converter="libredwg-dwgread",
            dxf_bytes=b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n",
            png_bytes=png,
            summary={
                "schemaVersion": "joyniu.dwg-vector-summary.v1",
                "units": {"code": 4, "name": "Millimeters"},
                "sourceEntityCount": 2,
                "entityCount": 2,
                "dimensions": [
                    {
                        "id": "dimension_00001",
                        "measurement": 98.0,
                        "text": "<>",
                        "textMode": "measurement-placeholder",
                        "dimensionType": 0,
                        "defpoints": {
                            "defpoint2": [0.0, 0.0, 0.0],
                            "defpoint3": [98.0, 0.0, 0.0],
                        },
                    }
                ],
                "geometry": [
                    {"type": "LINE", "start": [0.0, 0.0, 0.0], "end": [98.0, 0.0, 0.0]}
                ],
            },
            audit_summary={"source": {"filename": filename}},
        )

    remote_answer = {
        "message": "已结合DWG尺寸对象和渲染图形成候选",
        "part_type": "shaft",
        "recipe_id": "shaft_v1",
        "parameter_patch": {
            "outerDiameter": 54.2545,
            "length": 98,
            "holeDiameter": 13,
            "keywayWidth": 7.4641,
            "keywayDepth": 2,
            "keywayLength": 20,
        },
        "parameter_evidence": {
            "length": {"sourceType": "direct_dimension", "evidenceId": "dimension_00001"}
        },
        "needs_review": True,
        "questions": [],
    }
    bodies = []

    def fake_urlopen(request, timeout):
        bodies.append(json.loads(request.data.decode()))
        return _Response(
            {
                "id": f"resp_dwg_{len(bodies)}",
                "output_text": json.dumps(remote_answer),
            }
        )

    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr("app.dwg_preprocessor.preprocess_dwg", fake_preprocess)
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda _item: None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AIProxy(timeout_seconds=7).converse(
        "解析这个DWG",
        files=(AIFile("part.dwg", "application/octet-stream", raw_dwg),),
    )

    assert preprocess_calls == [(raw_dwg, "part.dwg")]
    assert len(bodies) == 2  # extraction plus independent audit
    raw_base64 = base64.b64encode(raw_dwg).decode()
    for body in bodies:
        serialized = json.dumps(body, ensure_ascii=False)
        assert raw_base64 not in serialized
        assert "input_file" not in serialized
        assert "CAD_VECTOR_EVIDENCE" in serialized
        assert "dimension_00001" in serialized
        attachments = [
            item
            for item in body["input"][-1]["content"]
            if item.get("type") == "input_image"
        ]
        assert len(attachments) == 1
        assert attachments[0]["image_url"].startswith("data:image/jpeg;base64,")
    assert result.provider["mode"] == "remote"
    assert result.attachments[0]["filename"] == "part.dwg"
    assert result.attachments[0]["sha256"] == hashlib.sha256(raw_dwg).hexdigest()
    assert result.attachments[0]["dwgPreprocessing"]["dimensionCount"] == 1
    assert result.attachments[0]["dwgPreprocessing"]["derivedFromSha256"] == hashlib.sha256(raw_dwg).hexdigest()


def test_dwg_preprocess_failure_makes_zero_provider_calls(monkeypatch):
    from app import ai_proxy
    from app.dwg_preprocessor import DWGInputError

    provider_calls = []
    monkeypatch.setenv("JOYNIU_AI_API_KEY", "test-provider-key")
    monkeypatch.setattr(
        "app.dwg_preprocessor.preprocess_dwg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DWGInputError("hostile detail")),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: provider_calls.append(True),
    )

    result = AIProxy(timeout_seconds=7).converse(
        "解析",
        files=(AIFile("bad.dwg", "application/octet-stream", b"AC1021bad"),),
    )

    assert provider_calls == []
    assert result.provider["mode"] == "dwg-preprocess-error"
    assert result.provider["lastErrorCode"] == "invalid_dwg_input"
    assert "hostile detail" not in result.message


def test_platform_attachment_finalizer_preserves_dwg_derivative_provenance():
    from app.platform_api import _canonical_ai_attachment_metadata

    raw = b"AC1021raw-source"
    digest = hashlib.sha256(raw).hexdigest()
    result = AIConversationResult(
        response_id="resp_metadata",
        message="ok",
        parameter_patch={},
        needs_review=True,
        questions=(),
        attachments=(
            {
                "filename": "provider-must-not-override.dwg",
                "contentType": "application/x-dwg",
                "sizeBytes": 1,
                "sha256": digest,
                "dwgPreprocessing": {
                    "status": "parsed",
                    "engine": "libredwg-dwgread",
                    "dimensionCount": 15,
                    "previewSha256": "a" * 64,
                    "derivedFromSha256": digest,
                },
            },
        ),
    )

    metadata = _canonical_ai_attachment_metadata(
        result,
        [AIFile("original.dwg", "application/octet-stream", raw)],
    )

    assert metadata[0]["filename"] == "original.dwg"
    assert metadata[0]["contentType"] == "application/octet-stream"
    assert metadata[0]["sizeBytes"] == len(raw)
    assert metadata[0]["sha256"] == digest
    assert metadata[0]["dwgPreprocessing"]["dimensionCount"] == 15
    assert metadata[0]["dwgPreprocessing"]["derivedFromSha256"] == digest


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


def test_invalid_dwg_returns_typed_preprocess_envelope_without_provider(monkeypatch):
    """Opaque DWG bytes never reach OCR/provider fallback paths."""
    from app import ai_proxy

    for name in (
        "JOYNIU_LLM_API_KEY", "JOYNIU_AI_API_KEY", "JOYNIU_LLM_API_KEY_FILE",
        "JOYNIU_AI_API_KEY_FILE", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    recognizer_calls = []
    monkeypatch.setattr(ai_proxy, "_recognize_attachment", lambda item: recognizer_calls.append(item))
    result = AIProxy().converse(
        "解析这份图纸",
        files=(AIFile("unknown.dwg", "application/octet-stream", b"AC10"),),
    )
    assert result.needs_review is True
    assert result.parameter_patch == {}
    assert result.drawing is None
    assert result.provider["mode"] == "dwg-preprocess-error"
    assert result.provider["lastErrorCode"] == "invalid_dwg_input"
    assert result.attachments[0]["dwgPreprocessing"]["status"] == "failed"
    assert recognizer_calls == []


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


def test_ai_stream_terminal_result_preserves_partial_review_dwg_candidate(monkeypatch):
    """SSE must deliver a validated extraction even if later review failed."""

    services = build_platform_services(":memory:", auth_secret="r" * 32)
    candidate_patch = {
        "mainLength": 98,
        "headLength": 50,
        "neckLength": 20,
        "headLeftDiameter": 54.25449350717895,
        "headRightDiameter": 56,
        "neckDiameter": 30,
        "tipDiameter": 25,
        "counterboreDiameter": 40,
        "counterboreDepth": 40,
        "axialBoreDiameter": 13,
        "outletDiameter": 17,
        "outletTaperHalfAngle": 15,
        "insertOuterDiameter": 39.4,
        "insertLength": 40,
        "insertThreadDesignation": "M12",
        "insertAxialOffset": 0,
    }

    class PartialReviewAI:
        allow_anonymous = True

        def status(self):
            return {"mode": "remote", "model": "gpt-5.6-sol", "streaming": True, "configured": True}

        def converse(self, _message, **kwargs):
            kwargs["on_status"]("阶段 3/3：远程复核未完成，正在保留提取候选…")
            return AIConversationResult(
                "resp_partial_dwg",
                "远程复核未完成；以下参数必须逐项人工确认。",
                candidate_patch,
                True,
                ("远程复核未完成，请逐项确认当前 AI 候选参数。",),
                part_type="stepped_tapered_nozzle",
                recipe_id="stepped_tapered_nozzle_with_insert_v1",
                parameter_evidence={
                    "mainLength": {"sourceType": "direct_dimension"},
                },
                provider={
                    **self.status(),
                    "reviewComplete": False,
                    "lastErrorCode": "transport",
                },
            )

    class BrokenOCR:
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("DWG OCR intentionally unavailable")

    services.ai = PartialReviewAI()
    services.ocr = BrokenOCR()
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("JOYNIU_ENV", "development")

    response = _client_for(services).post(
        "/api/v1/ai/conversation/stream",
        data={"message": "解析 DWG"},
        files={"files": ("1(1).dwg", b"AC1021test-dwg", "application/acad")},
    )

    assert response.status_code == 200, response.text
    blocks = [block for block in response.text.split("\n\n") if block.strip()]
    result_block = next(block for block in blocks if block.startswith("event: turn.result\n"))
    result = json.loads(next(line[6:] for line in result_block.splitlines() if line.startswith("data: ")))
    assert result["partType"] == "stepped_tapered_nozzle"
    assert result["recipeId"] == "stepped_tapered_nozzle_with_insert_v1"
    assert result["parameterPatch"] == candidate_patch
    assert result["provider"]["mode"] == "remote"
    assert result["provider"]["reviewComplete"] is False
    assert result["drawingRecognition"]["engine"] == "ai-candidate"
    assert result["drawingRecognition"]["candidateParameters"] == candidate_patch
    assert "event: turn.done" in response.text


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
