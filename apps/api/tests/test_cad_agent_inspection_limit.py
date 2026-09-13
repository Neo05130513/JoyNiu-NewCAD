"""Repeated source crops must lead to saved geometry or a bounded clarification."""
import json

from app.cad_agent import _source_inspection_state
from tests.test_cad_agent import Executor, Provider, context, edit, execute, finish, inspect_strip, record, run, striped_source


def crop_trace(count=2):
    return [{"action": "inspect_source", "result": {"status": "succeeded"}} for _ in range(count)]


def test_inspection_streak_survives_retries_and_resets_only_for_real_progress():
    previous = crop_trace()
    assert _source_inspection_state([*previous, {"action": "provider_error"}, {"action": "read_source"}]) == (2, 0)
    assert _source_inspection_state([*previous, {"action": "edit_plan", "result": {"status": "saved", "changed": False}}]) == (2, 0)
    assert _source_inspection_state([*previous, {"action": "edit_plan", "result": {"status": "saved", "changed": True, "geometryChanged": False}}]) == (2, 0)
    for progress in ({"action": "edit_plan", "result": {"status": "saved", "changed": True}},
                     {"action": "execute_plan", "result": {"status": "succeeded", "valid": True}},
                     {"action": "source_evidence_invalidated", "reason": "source_changed"}):
        assert _source_inspection_state([*previous, progress, *crop_trace(1)]) == (1, 0)
    denied = {"action": "inspect_source", "result": {"status": "failed", "errors": [{"code": "source_inspection_limit"}]}}
    assert _source_inspection_state([*previous, denied]) == (2, 1)


def test_third_crop_is_blocked_then_medium_can_execute_saved_geometry(tmp_path, monkeypatch):
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", "high")
    monkeypatch.setattr("app.cad_agent._default_draft_inspector", lambda *a, **k: {"status": "succeeded", "valid": True})
    provider = Provider([record(), edit(), inspect_strip(0), inspect_strip(1), inspect_strip(2),
                         {"action": "execute_plan", "message": "执行保留的草稿"}, finish()])
    executor = Executor()
    result = run(provider, executor, tmp_path, files=[striped_source()])
    assert result["status"] == "review_required"
    assert len(executor.calls) == 1
    assert len(list(tmp_path.glob("source-inspection-*.png"))) == 2
    assert [body["reasoning"]["effort"] for body, _ in provider.requests[4:]] == ["medium", "medium", "high"]
    for body, _ in provider.requests[4:6]:
        assert "source inspection limit is reached" in body["instructions"]
        assert "Inspect the actual partial-draft projections" not in body["instructions"]
    assert context(provider.requests[5][0])["toolFeedback"]["errors"][0]["code"] == "source_inspection_limit"
    assert len(result["state"]["retainedSourceDetails"]) == 2


def test_repeated_forbidden_crop_finishes_with_saved_state_before_exhausting_turns(tmp_path):
    provider = Provider([inspect_strip(0), inspect_strip(1), inspect_strip(2), inspect_strip(3), execute()])
    executor = Executor()
    result = run(provider, executor, tmp_path, files=[striped_source()], max_turns=20)
    assert result["status"] == "failed" and result["provider"]["lastErrorCode"] == "source_inspection_limit"
    assert len(provider.requests) == 4 and executor.calls == []
    assert len(list(tmp_path.glob("source-inspection-*.png"))) == 2
    assert result["plan"] is None and result["artifacts"] == {}
    saved = json.loads((tmp_path / "agent-state.json").read_text())
    assert saved["state"]["retainedSourceDetails"] == result["state"]["retainedSourceDetails"]
    assert len(result["state"]["retainedSourceDetails"]) == 2


def test_after_crop_limit_real_question_still_uses_independent_source_reread(tmp_path):
    questions = []
    def resolver(**kwargs):
        questions.append(kwargs["questions"])
        return {"status": "failed", "errorCode": "stub_unresolved"}
    ask = {"action": "ask_user", "message": "需核对孔深", "questions": ["右侧孔是否贯通？"]}
    result = run(Provider([inspect_strip(0), inspect_strip(1), ask]), Executor(), tmp_path,
                 files=[striped_source()], question_resolver=resolver)
    assert result["status"] == "needs_input" and result["questions"] == ask["questions"]
    assert questions == [ask["questions"]]
    assert any(item["action"] == "source_question_review" for item in result["trace"])


def test_continuation_starts_bounded_then_geometry_save_allows_a_new_crop(tmp_path, monkeypatch):
    monkeypatch.setenv("JOYNIU_LLM_REASONING_EFFORT", "high")
    monkeypatch.setattr("app.cad_agent._default_draft_inspector", lambda *a, **k: {"status": "succeeded", "valid": True})
    first = run(Provider([inspect_strip(0), inspect_strip(1)]), Executor(), tmp_path / "first",
                files=[striped_source()], max_turns=2)
    provider = Provider([record(), edit(), inspect_strip(2), {"action": "execute_plan", "message": "执行"}, finish()])
    result = run(provider, Executor(), tmp_path / "resumed", files=[striped_source()], state=first["state"])
    assert result["status"] == "review_required"
    assert "source inspection limit is reached" in provider.requests[0][0]["instructions"]
    assert "record_observations" in provider.requests[0][0]["instructions"]
    assert provider.requests[2][0]["reasoning"]["effort"] == "high"
    assert (tmp_path / "resumed" / "source-inspection-3.png").is_file()


def test_editing_only_notes_cannot_reset_the_geometry_progress_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.cad_agent._default_draft_inspector", lambda *a, **k: {"status": "succeeded", "valid": True})
    note_edit = {"action": "edit_plan", "message": "更新说明", "edit": {"notes": ["尚有结构待补充"]}}
    provider = Provider([record(), edit(), inspect_strip(0), inspect_strip(1), note_edit, inspect_strip(2), inspect_strip(3)])
    result = run(provider, Executor(), tmp_path, files=[striped_source()])
    assert result["status"] == "failed" and result["provider"]["lastErrorCode"] == "source_inspection_limit"
    assert len(list(tmp_path.glob("source-inspection-*.png"))) == 2
    edits = [item for item in result["trace"] if item["action"] == "edit_plan"]
    assert edits[0]["result"]["geometryChanged"] is True
    assert edits[1]["result"]["changed"] is True and edits[1]["result"]["geometryChanged"] is False
