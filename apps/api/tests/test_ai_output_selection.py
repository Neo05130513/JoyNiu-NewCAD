"""Final-message semantics for Responses JSON, without any provider calls."""
import io
import json

import pytest

from app.ai_proxy import (
    AIProxyError, AIProviderIncompleteError, AIProviderUpstreamError,
    _final_json, _final_output_text, _output_layout, _parse_result, _stream_payload,
)


def action(name="finish"):
    return json.dumps({"action": name, "message": name})


def message(text, *, phase=None, status="completed", role="assistant", **extra):
    return {"type": "message", "role": role, "status": status, "phase": phase,
            "content": [{"type": "output_text", "text": text}], **extra}


def envelope(*items, **extra):
    return {"id": "resp_output_selection", "status": "completed", "output": list(items), **extra}


def sse(*events):
    return io.BytesIO(b"".join(b"data: "+json.dumps(event).encode()+b"\n\n" for event in events))


def test_explicit_final_answer_wins_over_aggregate_commentary_and_later_commentary():
    payload = envelope(message(action("edit_plan"), phase="commentary"),
                       message(action(), phase="final_answer"), message(action("ask_user"), phase="commentary"),
                       output_text=action("edit_plan")+"\n"+action())
    assert _final_json(payload)["action"] == "finish"


def test_without_phase_last_assistant_message_wins_not_first_or_tool_content():
    payload = envelope(message(action("edit_plan")), {"type": "reasoning", "content": [{"text": action("bad")}]},
                       message(action()), message(action("ask_user"), role="tool"))
    assert _final_json(payload)["action"] == "finish"


@pytest.mark.parametrize("channel", ["analysis", "commentary", "unknown_channel"])
def test_legacy_non_final_channel_is_not_an_executable_answer(channel):
    with pytest.raises(AIProxyError):
        _final_json(envelope(message(action("execute_plan"), channel=channel), output_text=action()))


def test_legacy_final_channel_takes_precedence_over_later_unlabelled_or_unknown_messages():
    assert _final_json(envelope(message(action(), channel="final"), message(action("edit_plan")),
                                message(action("ask_user"), channel="unknown_channel")))["action"] == "finish"


def test_invalid_legacy_final_channel_does_not_fall_back_to_unlabelled_message():
    with pytest.raises(AIProxyError):
        _final_json(envelope(message("", channel="final"), message(action("execute_plan"))))


@pytest.mark.parametrize("last", [
    message("", phase="final_answer"),
    message(action(), phase="final_answer", status="incomplete"),
    message("not JSON", phase="final_answer"),
    message(action(), phase="final_answer", content=[{"type": "refusal", "refusal": "private"}]),
    message(action(), phase="final_answer", content=None),
])
def test_invalid_final_never_falls_back_to_earlier_valid_final_or_aggregate(last):
    with pytest.raises(AIProxyError):
        _final_json(envelope(message(action("edit_plan"), phase="final_answer"), last, output_text=action("edit_plan")))


@pytest.mark.parametrize("phase", ["commentary", "unknown_phase"])
def test_commentary_or_unknown_phase_only_is_not_an_executable_answer(phase):
    with pytest.raises(AIProxyError):
        _final_json(envelope(message(action("execute_plan"), phase=phase), output_text=action("execute_plan")))


def test_same_message_text_parts_join_without_inserting_characters():
    text = action("execute_plan")
    parts = [{"type": "output_text", "text": text[:8]}, {"type": "output_text", "text": text[8:]}]
    assert _final_output_text(envelope(message("", phase="final_answer", content=parts))) == text


def test_several_json_objects_in_selected_message_are_rejected_not_first_wins():
    parts = [{"type": "output_text", "text": action("edit_plan")}, {"type": "output_text", "text": action()}]
    with pytest.raises(AIProxyError, match="ambiguous structured JSON"):
        _final_json(envelope(message("", phase="final_answer", content=parts)))


@pytest.mark.parametrize("text", [action(), "```json\n"+action()+"\n```", "The final result is:\n"+action()])
def test_single_plain_fenced_or_prefaced_legacy_json_remains_supported(text):
    assert _final_json({"output_text": text})["action"] == "finish"


def test_legacy_proxy_parameter_parser_uses_final_message_too():
    draft = json.dumps({"message": "draft", "parameter_patch": {"length": 10}})
    final = json.dumps({"message": "final", "parameter_patch": {"length": 20}})
    parsed = _parse_result(envelope(message(draft, phase="commentary"), message(final, phase="final_answer")))
    assert parsed.parameter_patch == {"length": 20} and parsed.message == "final"


def test_layout_is_only_bounded_enum_metadata_and_text_counts():
    secret = "PRIVATE_identifier_shaped_secret_12345"
    payload = envelope(message(secret, phase="final_answer", id=secret),
                       {"type": secret, "phase": secret, "channel": secret, "status": secret, "role": secret,
                        "content": [{"type": secret, "text": secret}]}, output_text=secret)
    layout = _output_layout(payload)
    assert layout["itemCount"] == 2
    assert layout["items"][0]["phase"] == "final_answer"
    assert layout["items"][0]["content"][0]["textChars"] == len(secret)
    assert layout["items"][1]["phase"] == "other"
    assert secret not in json.dumps(layout)


def item_events(index, phase, text, *, status="completed"):
    item = message(text, phase=phase, status=status, id=f"msg_{index}")
    return [
        {"type": "response.output_item.added", "output_index": index,
         "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.output_text.done", "output_index": index, "content_index": 0, "item_id": f"msg_{index}", "text": text},
        {"type": "response.output_item.done", "output_index": index, "item": item},
    ]


def test_sse_without_response_terminal_preserves_final_phase_and_item_identity():
    result = _stream_payload(sse(*item_events(0, "commentary", action("edit_plan")),
                                 *item_events(1, "final_answer", action())))
    assert [item["phase"] for item in result["output"]] == ["commentary", "final_answer"]
    assert _final_json(result)["action"] == "finish"


@pytest.mark.parametrize("timeout", [False, True])
def test_commentary_json_never_recovers_as_completed_action_without_terminal(timeout):
    raw = sse(*item_events(0, "commentary", action("execute_plan"))).getvalue()
    class Source(io.BytesIO):
        def readline(self, *args):
            part = super().readline(*args)
            if timeout and not part:
                raise TimeoutError("relay ended")
            return part
    with pytest.raises((AIProxyError, TimeoutError)):
        _stream_payload(Source(raw))


@pytest.mark.parametrize("final_text,final_status", [("invalid final JSON", "completed"), (action(), "incomplete"), ("", "completed")])
def test_sse_early_valid_action_does_not_rescue_invalid_final_item(final_text, final_status):
    with pytest.raises(AIProxyError):
        _stream_payload(sse(*item_events(0, "final_answer", action("edit_plan")),
                            *item_events(1, "final_answer", final_text, status=final_status)))


def test_sse_multipart_final_is_assembled_by_content_index_not_last_done_event():
    text = action("execute_plan")
    result = _stream_payload(sse(
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "message", "role": "assistant", "phase": "final_answer", "status": "in_progress", "id": "m", "content": []}},
        {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "item_id": "m", "text": text[:12]},
        {"type": "response.output_text.done", "output_index": 0, "content_index": 1, "item_id": "m", "text": text[12:]},
        {"type": "response.output_item.done", "output_index": 0, "item": {"id": "m", "type": "message", "status": "completed", "phase": "final_answer"}},
    ))
    assert _final_json(result)["action"] == "execute_plan"


def test_sse_completed_aggregate_does_not_override_structured_items():
    result = _stream_payload(sse(*item_events(0, "commentary", action("edit_plan")),
                                 *item_events(1, "final_answer", action()),
                                 {"type": "response.completed", "response": {"status": "completed", "output_text": action("edit_plan")}}))
    assert _final_json(result)["action"] == "finish"


def test_explicit_failed_terminal_stays_failed_even_after_valid_final_message():
    with pytest.raises(AIProviderUpstreamError):
        _stream_payload(sse(*item_events(0, "final_answer", action()),
                            {"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error"}}}))
