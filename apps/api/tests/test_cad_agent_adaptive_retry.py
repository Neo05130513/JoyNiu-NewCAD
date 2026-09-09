import pytest

from app.cad_agent import _operation_effort, _operation_stream
from tests.test_cad_agent import Executor, Provider, execute, finish, record, run


def interrupted(**metrics):
    return {"iteration": 1, "action": "provider_error", "code": "empty_response",
            "providerCall": {"elapsedSeconds": 90, "transport": {"outputChars": 0}, **metrics}}


@pytest.mark.parametrize("event,expected", [
    (interrupted(), "medium"),
    (interrupted(elapsedSeconds=2), "high"),
    (interrupted(outputChars=100), "high"),
    (interrupted(transport={"outputChars": 100}), "high"),
    ({**interrupted(), "code": "invalid_json"}, "high"),
])
def test_effort_changes_only_for_long_empty_retry(event, expected):
    assert _operation_effort("high", [event]) == expected
    assert _operation_effort("low", [event]) == "low"


def test_resume_limits_one_interrupted_operation_then_restores_configured_effort(tmp_path, monkeypatch):
    monkeypatch.setattr("app.cad_agent.ai_proxy._reasoning_effort", lambda: "high")
    provider = Provider([record(), execute(), finish()])
    result = run(provider, Executor(), tmp_path, state={"trace": [interrupted()]})
    assert result["status"] == "review_required"
    assert [body["reasoning"]["effort"] for body, _ in provider.requests] == ["medium", "high", "high"]
    assert result["inspection"]["acceptance"]["status"] == "passed"


def test_repeated_relay_limit_bounds_later_operations_until_source_changes():
    trace = [interrupted(), interrupted(), {"action": "record_observations"}]
    assert _operation_effort("high", trace) == "medium"
    trace.append({"action": "source_evidence_invalidated", "reason": "source_changed"})
    assert _operation_effort("high", trace) == "high"


def test_long_eof_with_only_unfinished_items_is_an_empty_operation():
    value = {**interrupted(), "code": "invalid_response", "providerCall": {
        "elapsedSeconds": 70, "transport": {"outputChars": 0, "protocol": "sse", "streamEndReason": "eof", "terminalEventCount": 0}}}
    assert _operation_effort("high", [value]) == "medium"
    assert _operation_effort("ultra", [value]) == "medium"
    value["providerCall"]["transport"]["terminalEventCount"] = 1
    assert _operation_effort("high", [value]) == "high"


def malformed_stream():
    return {"action": "provider_error", "code": "invalid_stream", "providerCall": {
        "elapsedSeconds": 100, "transport": {"protocol": "sse", "streamEndReason": "invalid_event", "outputChars": 200}}}


def test_only_malformed_sse_retry_uses_json_transport():
    failure = malformed_stream()
    assert _operation_stream([failure]) is False
    assert _operation_stream([failure, {"action": "edit_plan"}]) is True
    assert _operation_stream([failure, {"action": "source_evidence_invalidated", "reason": "source_changed"}]) is True
    assert _operation_stream([{**failure, "code": "http_401"}]) is True
    assert _operation_stream([{**failure, "providerCall": {"transport": {"protocol": "json", "streamEndReason": "invalid_event"}}}]) is True
    assert _operation_stream([{**failure, "providerCall": {"transport": {"protocol": "sse", "streamEndReason": "eof"}}}]) is True


def test_json_retry_preserves_real_execute_and_confirmation_boundaries(tmp_path):
    provider = Provider([record(), execute(), finish()])
    result = run(provider, Executor(), tmp_path, state={"trace": [malformed_stream()]})
    assert [body["stream"] for body, _ in provider.requests] == [False, True, True]
    assert result["status"] == "review_required"
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert result["trace"][1]["providerCall"]["streamRequested"] is False


def test_json_retry_cannot_turn_a_finish_without_geometry_into_success(tmp_path):
    from tests.test_cad_agent import plan
    result = run(Provider([finish()]), Executor(), tmp_path, max_turns=1,
                 state={"cadPlan": plan(), "trace": [malformed_stream()]})
    assert result["status"] == "failed" and not result["artifacts"]
    assert result["trace"][-1]["result"]["errors"][0]["code"] == "fresh_execution_required"
