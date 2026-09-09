"""Source identity and repair-context regressions; every provider/tool is local."""
from __future__ import annotations

import copy

import pytest

from app.cad_agent import CadAgentService
from tests.test_cad_agent import (
    Executor, Provider, StubSourceReader, StubDrawingReviewer, StubSpatialInterpreter, context, edit, execute, finish, raster, record,
)


class Reading(StubSourceReader):
    def __init__(self, height=30):
        super().__init__()
        self.height = height

    def read(self, **kwargs):
        result = super().read(**kwargs)
        result["annotations"] = [{"id": "visible_overall_height", "fileIndex": 0,
                                  "text": str(self.height), "view": "front",
                                  "endpointsOrDatum": "bottom-to-top overall dimension",
                                  "confidence": "high", "questions": []}]
        result["questions"] = []
        return result


def drawing_record(height=30):
    action = record(height)
    action["observations"][0]["source"] = {
        "type": "drawing", "text": f"visible_overall_height: overall height {height}",
        "fileIndex": 0, "view": "front",
    }
    return action


def ask():
    return {"action": "ask_user", "message": "需要明确尺寸。", "questions": ["整体高度是多少？"]}


def invoke(actions, directory, *, state=None, files=(), reader=None, max_turns=8):
    provider, executor = Provider(actions), Executor()
    reader = reader or Reading()
    result = CadAgentService(provider_call=provider, executor=executor, source_reader=reader,
                             drawing_reviewer=StubDrawingReviewer(), spatial_interpreter=StubSpatialInterpreter()).run(
        message="根据本次上传的来源建立模型，或继续处理已保存的草稿。", files=files,
        state=state, output_dir=directory, max_turns=max_turns,
    )
    return result, provider, executor, reader


@pytest.fixture
def saved_drawing(tmp_path):
    files = [raster(200), raster(205)]
    inspect = {"action": "inspect_source", "message": "查看来源局部。",
               "source": {"fileIndex": 0, "crop": [0, 0, 0.6, 1], "rotation": 0, "view": "front detail"}}
    result, *_ = invoke([inspect, drawing_record(), execute(), finish()], tmp_path / "original", files=files)
    assert result["status"] == "review_required"
    assert result["state"]["retainedSourceDetails"]
    return result["state"], files


@pytest.mark.parametrize("replaced_index", [0, 1])
def test_changed_image_discards_old_plan_ledger_and_all_crops_before_new_checks(tmp_path, saved_drawing, replaced_index):
    state, files = saved_drawing
    before = copy.deepcopy(state)
    files = list(files)
    files[replaced_index] = raster(220 + replaced_index)

    def attempt_old_execution(body):
        current = context(body)
        assert current["currentPlan"] is None
        assert current["sourceObservations"] == []
        # Changing a second image invalidates even a retained crop from the
        # unchanged first image: its context belonged to the previous source set.
        assert current["retainedSourceDetails"] == []
        assert current["sourceTranscription"]["annotations"][0]["text"] == "60"
        assert not current["currentExecutionSummary"]["hasFreshValidGeometry"]
        return execute(30)

    def record_new_source(body):
        current = context(body)
        assert current["toolFeedback"]["errors"][0]["code"] == "source_observations_required"
        assert current["sourceObservations"] == []
        return drawing_record(60)

    result, _, executor, reader = invoke(
        [attempt_old_execution, record_new_source, execute(60), finish()], tmp_path / "replaced",
        state=state, files=files, reader=Reading(60),
    )
    assert len(reader.calls) == 1
    assert len(executor.calls) == 1
    assert executor.calls[0]["plan"]["parameters"]["height"]["value"] == 60
    assert result["status"] == "review_required"
    assert result["inspection"]["acceptance"]["checks"][0]["expected"] == 60
    assert result["inspection"]["acceptance"]["status"] == "passed"
    invalidation = next(item for item in result["trace"] if item["action"] == "source_evidence_invalidated")
    assert invalidation["reason"] == "source_changed" and invalidation["previousPlanDiscarded"] is True
    assert state == before


@pytest.mark.parametrize("identity_component", ["sourceFiles", "preparedFiles"])
def test_either_original_or_prepared_identity_change_is_sufficient_to_invalidate(tmp_path, saved_drawing, identity_component):
    saved, files = saved_drawing
    state = copy.deepcopy(saved)
    # Model a persisted source/preprocessor identity that differs only in this
    # component. Neither the other component nor old fingerprint can authorize it.
    state["sourceTranscription"][identity_component][1]["sha256"] = "0" * 64
    def inspect_current(body):
        current = context(body)
        assert current["currentPlan"] is None
        assert current["sourceObservations"] == []
        assert current["retainedSourceDetails"] == []
        return ask()
    result, _, executor, reader = invoke([inspect_current], tmp_path / identity_component, state=state, files=files)
    assert len(reader.calls) == 1 and executor.calls == []
    assert result["status"] == "needs_input"
    assert result["plan"] is None


def test_reader_version_change_retains_draft_but_requires_new_observations(tmp_path, saved_drawing):
    saved, files = saved_drawing
    state = copy.deepcopy(saved)
    state["sourceTranscription"]["version"] = "old-reader-version"
    def attempt_draft(body):
        current = context(body)
        assert current["currentPlan"] == saved["cadPlan"]
        assert current["sourceObservations"] == []
        assert current["retainedSourceDetails"]
        assert not current["currentExecutionSummary"]["hasFreshValidGeometry"]
        return {"action": "execute_plan", "message": "尝试执行保留草稿。"}
    def replace_ledger(body):
        assert context(body)["toolFeedback"]["errors"][0]["code"] == "source_observations_required"
        return drawing_record()
    result, _, executor, reader = invoke(
        [attempt_draft, replace_ledger, {"action": "execute_plan", "message": "执行已重录依据的草稿。"}, finish()],
        tmp_path / "new-reader", state=state, files=files,
    )
    assert len(reader.calls) == len(executor.calls) == 1
    assert result["status"] == "review_required"
    event = next(item for item in result["trace"] if item["action"] == "source_evidence_invalidated")
    assert event["reason"] == "transcription_version_changed" and event["previousPlanDiscarded"] is False


def test_same_source_and_reader_reuse_ledger_but_require_fresh_geometry(tmp_path, saved_drawing):
    state, files = saved_drawing
    def attempt_finish(body):
        current = context(body)
        assert current["currentPlan"] == state["cadPlan"]
        assert current["sourceObservations"] == state["observations"]
        assert current["retainedSourceDetails"]
        assert not current["currentExecutionSummary"]["hasFreshValidGeometry"]
        return finish()
    def fresh_execute(body):
        assert context(body)["toolFeedback"]["errors"][0]["code"] == "fresh_execution_required"
        return {"action": "execute_plan", "message": "重新执行当前草稿。"}
    result, _, executor, reader = invoke([attempt_finish, fresh_execute, finish()], tmp_path / "same", state=state, files=files)
    assert reader.calls == [] and len(executor.calls) == 1
    assert result["status"] == "review_required"
    assert not any(item["action"] == "source_evidence_invalidated" for item in result["trace"])


def test_rejected_observation_is_restored_for_repair_only_and_can_finish_after_validation(tmp_path):
    invalid = record()
    invalid["observations"][0]["kind"] = "local_height"
    first, *_ = invoke([invalid], tmp_path / "first", max_turns=1)
    assert first["status"] == "failed" and first["observations"] == []
    original_state = copy.deepcopy(first["state"])
    def repair(body):
        current = context(body)
        assert current["toolFeedback"]["rejectedObservations"] == invalid["observations"]
        assert current["sourceObservations"] == [] and current["currentPlan"] is None
        assert "Repair only" in body["instructions"]
        return record()
    result, _, executor, _ = invoke([repair, execute(), finish()], tmp_path / "resumed", state=first["state"])
    assert result["status"] == "review_required" and len(executor.calls) == 1
    assert result["observations"][0]["kind"] == "bbox_size"
    assert first["state"] == original_state


def invalid_edit():
    return {"action": "edit_plan", "message": "无效替换，不得保存。", "edit": {
        "features": [{"id": "body", "op": "cut", "inputs": ["body", "missing"]}],
    }}


def test_rejected_edit_is_restored_without_replacing_valid_draft_or_restoring_execution(tmp_path):
    first, *_ = invoke([record(), edit(), invalid_edit()], tmp_path / "first", max_turns=3)
    assert first["status"] == "failed"
    valid_draft = copy.deepcopy(first["plan"])
    def repair(body):
        current = context(body)
        assert current["toolFeedback"]["rejectedEdit"] == invalid_edit()["edit"]
        assert current["currentPlan"] == valid_draft
        assert current["currentPlan"]["features"][0]["op"] == "box"
        assert not current["currentExecutionSummary"]["hasFreshValidGeometry"]
        return edit()
    result, _, executor, _ = invoke(
        [repair, {"action": "execute_plan", "message": "执行修复后的草稿。"}, finish()],
        tmp_path / "resumed", state=first["state"],
    )
    assert result["status"] == "review_required"
    assert len(executor.calls) == 1
    assert executor.calls[0]["plan"] == valid_draft


@pytest.mark.parametrize("barrier", ["record_observations", "edit_plan", "execute_plan", "ask_user", "finish", "source_evidence_invalidated"])
@pytest.mark.parametrize("proposal_type", ["record_observations", "edit_plan"])
def test_obsolete_rejected_proposal_is_not_replayed_past_later_resolution(tmp_path, barrier, proposal_type):
    invalid = record()
    invalid["observations"][0]["kind"] = "local_height"
    action = invalid if proposal_type == "record_observations" else invalid_edit()
    rejected, *_ = invoke([action], tmp_path / "rejected", max_turns=1)
    state = copy.deepcopy(rejected["state"])
    state["trace"].append({"iteration": 2, "action": barrier, "result": {"status": "succeeded"}})
    # Bookkeeping/provider interruptions after the resolution must not expose
    # an even older proposal when scanning backwards on the next run.
    state["trace"].append({"iteration": 3, "action": "provider_error", "code": "timeout"})
    def check(body):
        feedback = context(body)["toolFeedback"]
        assert feedback["stage"] == "start"
        assert "rejectedObservations" not in feedback and "rejectedEdit" not in feedback
        return ask()
    result, _, executor, _ = invoke([check], tmp_path / "resumed", state=state)
    assert result["status"] == "needs_input" and executor.calls == []


def test_source_invalidation_blocks_repair_of_a_previous_drawings_rejected_proposal(tmp_path, saved_drawing):
    saved, files = saved_drawing
    invalid = drawing_record()
    invalid["observations"][0]["kind"] = "local_height"
    rejected, *_ = invoke([invalid], tmp_path / "rejected", state=saved, files=files, max_turns=1)
    def check(body):
        current = context(body)
        assert current["toolFeedback"]["stage"] == "start"
        assert current["sourceObservations"] == [] and current["currentPlan"] is None
        return ask()
    result, _, executor, _ = invoke([check], tmp_path / "replaced", state=rejected["state"], files=[raster(240)])
    assert result["status"] == "needs_input" and executor.calls == []
