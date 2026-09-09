import json

import pytest

from app.ai_proxy import AIProxyError, AIProviderIncompleteError
from app.cad_agent import _parse_action


def response_message(action, *, phase="final_answer", status="completed"):
    return {"type": "message", "role": "assistant", "phase": phase, "status": status,
            "content": [{"type": "output_text", "text": json.dumps(action)}]}


def test_intermediate_complete_action_cannot_win_over_corrected_final_action():
    interim = {"action": "execute_plan", "message": "尚在整理的中间草稿。", "plan": {"result": "wrong_draft"}}
    final = {"action": "edit_plan", "message": "最终仅保存修正后的特征。", "edit": {"result": "corrected_body"}}
    result = _parse_action({"status": "completed", "output_text": json.dumps(interim)+"\n"+json.dumps(final),
                            "output": [response_message(interim, phase="commentary"), response_message(final)]})
    assert result == final


def test_incomplete_final_cannot_execute_earlier_complete_draft():
    interim = {"action": "execute_plan", "message": "旧草稿", "plan": {"result": "wrong_draft"}}
    payload = {"status": "completed", "output": [response_message(interim, phase="commentary"),
                response_message({"action": "finish", "message": "未完成"}, status="incomplete")]}
    with pytest.raises((AIProxyError, AIProviderIncompleteError)):
        _parse_action(payload)


def test_two_actions_inside_one_final_message_are_not_partially_executed():
    first = {"action": "execute_plan", "message": "草稿", "plan": {}}
    second = {"action": "ask_user", "message": "参数缺失", "questions": ["尺寸？"]}
    item = response_message(first)
    item["content"][0]["text"] += "\n"+json.dumps(second)
    with pytest.raises(AIProxyError):
        _parse_action({"status": "completed", "output": [item]})
