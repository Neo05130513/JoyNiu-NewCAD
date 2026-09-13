import pytest

from app import cad_agent
from app.ai_proxy import AIProviderTransportError
from app.cad_agent import _initial_operation_effort, _normalize_observation_wrappers, _operation_effort, _operation_stream, _planner_limits
from tests.test_cad_agent import Executor, Provider, context, edit, execute, finish, raster, record, run


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
    assert [body["reasoning"]["effort"] for body, _ in provider.requests] == ["medium", "medium", "high"]
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


def stalled_partial():
    # Production 9.jpg: first output arrived only after 406.73 seconds;
    # 5,622 characters and 2,131 events still contained no complete action.
    return {"iteration": 6, "action": "provider_error", "code": "timeout", "providerCall": {
        "elapsedSeconds": 471.0, "outputChars": 5622, "transport": {
            "httpStatus": 200, "protocol": "sse", "outputChars": 5622, "eventCount": 2131,
            "firstOutputTextSeconds": 406.73, "terminalEventCount": 0}}}


def test_long_timeout_with_partial_output_uses_medium_until_next_complete_operation():
    failure = stalled_partial()
    assert _operation_effort("high", [failure]) == "medium"
    assert _operation_effort("ultra", [failure]) == "medium"
    assert _operation_effort("low", [failure]) == "low"
    assert _operation_effort("high", [failure, {"action": "edit_plan"}]) == "high"
    assert _operation_effort("high", [{**failure, "code": "incomplete"}]) == "medium"
    assert _operation_effort("high", [{**failure, "providerCall": {**failure["providerCall"], "elapsedSeconds": 3}}]) == "high"
    assert _operation_effort("high", [failure, {"action": "source_evidence_invalidated", "reason": "source_changed"}]) == "high"


@pytest.mark.parametrize("configured,expected", [(None, "medium"), (" LOW ", "low"), ("high", "high"),
    ("xhigh", "xhigh"), ("max", "max"), ("ultra", "ultra"), ("invalid", "medium")])
def test_initial_effort_is_independent_and_validated(monkeypatch, configured, expected):
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", "high")
    if configured is None:
        monkeypatch.delenv("JOYNIU_CAD_INITIAL_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("JOYNIU_CAD_INITIAL_REASONING_EFFORT", configured)
    assert _initial_operation_effort() == expected
    assert cad_agent.ai_proxy._reasoning_effort() == "high"


def test_initial_evidence_and_first_edit_are_medium_then_construction_returns_to_high(tmp_path, monkeypatch):
    monkeypatch.delenv("JOYNIU_CAD_INITIAL_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", "high")
    provider = Provider([record(), edit(), {"action": "execute_plan", "message": "执行保存的草稿"}, finish()])
    result = run(provider, Executor(), tmp_path)
    assert result["status"] == "review_required"
    assert [body["reasoning"]["effort"] for body, _ in provider.requests] == ["medium", "medium", "high", "high"]
    assert "1–3 features" in provider.requests[1][0]["instructions"]
    assert "finite JSON numbers" in provider.requests[0][0]["instructions"]
    assert "only CAD plan numeric slots accept arithmetic expressions" in provider.requests[0][0]["instructions"]


@pytest.mark.parametrize("operation,reserve,expected", [(None, None, (180, 120, 15)),
    ("9999", "9999", (180, 180, 15)), ("1", "-1", (30, 0, 15)),
    ("nan", "inf", (180, 120, 15)), ("invalid", "invalid", (180, 120, 15)),
    ("90", "100", (90, 100, 15))])
def test_planner_budget_configuration_has_finite_bounds(monkeypatch, operation, reserve, expected):
    for name, value in (("JOYNIU_CAD_PLANNER_TIMEOUT_SECONDS", operation), ("JOYNIU_CAD_FINISH_RESERVE_SECONDS", reserve)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert _planner_limits(900) == expected
    assert _planner_limits(60)[1] <= 15


def test_471_second_remaining_window_caps_call_and_saves_small_retry_before_execution(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cad_agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("JOYNIU_CAD_INITIAL_REASONING_EFFORT", "high")
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", "high")
    calls = []

    def limited_call(self, body, timeout, on_wait=None, on_diagnostics=None):
        calls.append((body, timeout))
        if len(calls) == 1:
            clock[0] += timeout
            on_diagnostics(stalled_partial()["providerCall"]["transport"])
            raise AIProviderTransportError("timeout")
        if len(calls) == 2:
            assert context(body)["sourceObservations"] == record()["observations"]
            assert context(body)["currentPlan"] is None
            assert "1–3 features" in body["instructions"]
            clock[0] += 20
            return {"output_text": cad_agent.json.dumps(edit())}
        if len(calls) == 3:
            checkpoint = cad_agent.json.loads((tmp_path / "checkpoint.json").read_text())
            assert checkpoint["plan"]["result"] == "body"
            return {"output_text": cad_agent.json.dumps({"action": "execute_plan", "message": "执行已保存草稿"})}
        return {"output_text": cad_agent.json.dumps(finish())}

    monkeypatch.setattr(cad_agent.CadAgentService, "_call", limited_call)
    # Draft rendering is separately covered; keep this budget test clock-only.
    monkeypatch.setattr(cad_agent, "_default_draft_inspector", lambda *a, **k: {"status": "succeeded", "valid": True})

    def progress(event):
        if event.get("stage") == "planning" and not calls:
            clock[0] = 529.0  # 900 - 429 = 471 seconds remain before first call.

    result = run(Provider([]), Executor(), tmp_path, state={"observations": record()["observations"]},
                 timeout_seconds=900, progress=progress)
    assert result["status"] == "review_required"
    assert [timeout for _, timeout in calls] == [180, 171, 151, 180]
    assert [body["reasoning"]["effort"] for body, _ in calls] == ["high", "medium", "high", "high"]
    assert result["provider"]["retryCount"] == 1
    assert result["plan"]["result"] == "body"
    assert result["trace"][0]["providerCall"]["executionReviewReserveSeconds"] == 120


def test_tail_budget_never_starts_a_new_planner_request(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cad_agent.time, "monotonic", lambda: clock[0])

    def progress(event):
        if event.get("stage") == "observe_source":
            clock[0] = 870.0  # 130 seconds left, including 120 reserved.

    provider, executor = Provider([]), Executor()
    result = run(provider, executor, tmp_path, timeout_seconds=900, progress=progress)
    assert result["status"] == "failed" and result["provider"]["lastErrorCode"] == "time_limit"
    assert result["provider"]["attempts"] == 0
    assert provider.requests == executor.calls == []
    assert result["plan"] is None
    assert result["elapsedSeconds"] == 770
    assert "原图证据与处理记录已保存" in result["message"]


@pytest.mark.parametrize("with_drawing", [False, True])
def test_valid_geometry_with_failed_dimensions_keeps_budget_for_repair(tmp_path, monkeypatch, with_drawing):
    clock = [100.0]
    monkeypatch.setattr(cad_agent.time, "monotonic", lambda: clock[0])
    executor = Executor()
    provider = Provider([record(), execute(25), execute(30), finish()])

    def progress(event):
        if event.get("stage") == "planning" and len(executor.calls) == 1:
            clock[0] = 780.0  # 220 seconds remain; failed checks still need 120.

    result = run(provider, executor, tmp_path, files=[raster()] if with_drawing else [],
                 timeout_seconds=900, progress=progress)
    assert result["status"] == "review_required"
    repair_context = context(provider.requests[2][0])
    assert repair_context["currentExecutionSummary"]["inspection"]["acceptance"]["status"] == "failed"
    assert repair_context["executionReviewReserveSeconds"] == 120
    assert provider.requests[2][1] <= 100
    assert context(provider.requests[3][0])["executionReviewReserveSeconds"] == 0
    assert len(executor.calls) == 2


def test_call_returning_after_operation_limit_never_applies_complete_looking_plan(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cad_agent.time, "monotonic", lambda: clock[0])

    def late_call(self, body, timeout, **kwargs):
        assert timeout == 180
        clock[0] += 181
        return {"status": "completed", "output_text": cad_agent.json.dumps(execute())}

    monkeypatch.setattr(cad_agent.CadAgentService, "_call", late_call)
    executor = Executor()
    result = run(Provider([]), executor, tmp_path, state={"observations": record()["observations"]},
                 timeout_seconds=900, max_turns=1)
    assert result["status"] == "failed" and result["provider"]["lastErrorCode"] == "timeout"
    assert result["plan"] is None and executor.calls == []


def test_incomplete_response_with_5622_partial_characters_cannot_supply_a_plan(tmp_path):
    payload = {"status": "incomplete", "output_text": cad_agent.json.dumps(execute()) + " " * 5622}
    # Supply the raw relay envelope without the fixture Provider wrapping it.
    result = run(lambda body, timeout: payload, Executor(), tmp_path,
                 state={"observations": record()["observations"]}, timeout_seconds=900, max_turns=1)
    assert result["status"] == "failed" and result["provider"]["lastErrorCode"] == "incomplete"
    assert result["plan"] is None and result["artifacts"] == {}


def test_exact_duplicate_measurement_wrapper_is_normalized_then_validated(tmp_path):
    from app.cad_acceptance import validate_observations
    wrapped = record()
    wrapped["observations"][0]["expected"] = {"kind": "bbox_size", "axis": 2, "expected": 30}
    provider, executor = Provider([wrapped, execute(), finish()]), Executor()
    result = run(provider, executor, tmp_path)
    assert result["status"] == "review_required"
    assert result["observations"] == validate_observations(record()["observations"])
    assert result["trace"][0]["normalization"] == {"code": "duplicate_measurement_wrapper", "observationIndexes": [0]}
    assert wrapped["observations"][0]["expected"] == {"kind": "bbox_size", "axis": 2, "expected": 30}
    assert '"expected":30,"source"' in provider.requests[0][0]["instructions"]
    assert len(executor.calls) == 1


@pytest.mark.parametrize("expected", [
    {"kind": "bbox_size", "axis": 1, "expected": 30},
    {"kind": "cylinder", "axis": 2, "expected": 30},
    {"kind": "bbox_size", "axis": 2, "expected": 30, "units": "mm"},
    {"kind": "bbox_size", "axis": 2, "expected": "15*2"},
    {"kind": "bbox_size", "axis": 2, "expected": None},
    {"kind": "bbox_size", "axis": 2, "expected": True},
    {"kind": "bbox_size", "axis": 2, "expected": {"kind": "bbox_size", "axis": 2, "expected": 30}},
])
def test_wrapper_normalization_cannot_hide_conflicts_or_invalid_numbers(tmp_path, expected):
    wrapped = record()
    wrapped["observations"][0]["expected"] = expected
    executor = Executor()
    result = run(Provider([wrapped]), executor, tmp_path, max_turns=1)
    assert result["status"] == "failed"
    assert result["observations"] == [] and executor.calls == []
    assert result["trace"][0]["result"]["status"] == "failed"


def test_wrapper_redundant_axis_and_probe_must_match_types_and_values():
    observation = record()["observations"][0]
    observation["axis"] = 1
    observation["expected"] = {"kind": "bbox_size", "axis": True, "expected": 30}
    normalized, indexes = _normalize_observation_wrappers([observation])
    assert indexes == [] and normalized == [observation]
    probe = {"origin": [0, 0, 0], "direction": [1, 0, 0], "start": 0, "end": 30}
    ray = {"id": "ray", "kind": "ray_intervals", "probe": probe,
           "expected": {"kind": "ray_intervals", "probe": {**probe, "origin": [False, 0, 0]}, "expected": [[0, 30]]}}
    assert _normalize_observation_wrappers([ray])[1] == []
    ray["expected"]["probe"] = probe
    assert _normalize_observation_wrappers([ray]) == ([{**ray, "expected": [[0, 30]]}], [0])
