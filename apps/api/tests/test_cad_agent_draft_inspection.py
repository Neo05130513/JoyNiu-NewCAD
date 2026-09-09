import json

from app.cad_agent import CadAgentService, _digest
from app.cad_acceptance import validate_observations
from tests.test_cad_agent import Provider, Executor, context, edit, finish, observation, plan, record


def checked(*, valid=True):
    return {"status": "succeeded" if valid else "failed", "valid": valid,
            "inspection": {"engine": "cadquery-occt", "kernelBacked": True, "valid": valid},
            "errors": [] if valid else [{"code": "invalid_geometry", "featureId": "body", "message": "Self-intersecting profile"}],
            # Even an accidental exporter return must not become deliverable state.
            "artifacts": {"step": {"path": "/not-a-delivery.step"}}}


def test_changed_draft_checks_geometry_and_repairs_before_final_execution(tmp_path):
    inspected, events = [], []
    def draft_inspector(value, directory, *, timeout_seconds):
        inspected.append(value)
        assert 0 < timeout_seconds <= 15
        return checked(valid=len(inspected) > 1)
    def repair(body):
        state = context(body)
        assert state["currentDraftInspection"]["errors"][0]["featureId"] == "body"
        assert "repair only the failing construction" in body["instructions"]
        assert state["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        assert state["sourceObservations"] == validate_observations([observation()])
        update = edit()
        update["edit"]["features"][0]["origin"] = [5, 0, 0]
        return update
    def execute_saved(body):
        state = context(body)
        assert state["currentDraftInspection"]["valid"] is True
        assert state["currentDraftInspection"]["drawingAgreement"] == "not_checked"
        assert state["currentDraftInspection"]["deliveryArtifactsAvailable"] is False
        assert "artifacts" not in state["currentDraftInspection"]
        assert state["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        return {"action": "execute_plan", "message": "执行完整模型并测量。"}
    provider = Provider([record(), edit(), repair, execute_saved, finish()])
    executor = Executor()
    result = CadAgentService(provider_call=provider, executor=executor, draft_inspector=draft_inspector).run(
        message="Build a 30 mm high block", output_dir=tmp_path, progress=events.append)
    assert result["status"] == "review_required"
    assert len(inspected) == 2 and len(executor.calls) == 1
    assert sum(e["stage"] == "inspect_draft" for e in events) == 2
    assert "/not-a-delivery.step" not in json.dumps(result)


def test_continuation_rechecks_saved_draft_instead_of_trusting_old_validity(tmp_path):
    saved = plan()
    state = {"cadPlan": saved, "observations": [observation()],
             "draftInspection": checked(),
             "trace": [{"action": "edit_plan", "planHash": _digest(saved), "result": {"status": "saved"}}]}
    calls = []
    def inspector(*args, **kwargs):
        calls.append(args)
        return checked(valid=False)
    def check_feedback(body):
        current = context(body)
        assert current["currentDraftInspection"]["status"] == "failed"
        assert current["currentDraftInspection"]["planHash"] == _digest(saved)
        assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        return {"action": "ask_user", "message": "需要结构说明。", "questions": ["边界应如何连接？"]}
    result = CadAgentService(provider_call=Provider([check_feedback]), draft_inspector=inspector).run(
        message="Continue", state=state, output_dir=tmp_path)
    assert len(calls) == 1
    assert result["artifacts"] == {} and result["inspection"] is None
    assert result["state"]["draftInspection"]["status"] == "failed"
    assert json.loads((tmp_path / "checkpoint.json").read_text())["draftInspection"]["status"] == "failed"


def test_draft_check_failure_cannot_lose_saved_plan_or_pass_acceptance(tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("worker failed")
    provider = Provider([record(), edit(), {"action": "ask_user", "message": "暂时保留草稿。", "questions": ["继续检查？"]}])
    result = CadAgentService(provider_call=provider, draft_inspector=broken).run(message="Build block", output_dir=tmp_path)
    assert result["plan"]["result"] == "body"
    assert result["draftInspection"]["status"] == "failed"
    assert result["inspection"] is None and result["artifacts"] == {}
    assert result["observations"] == validate_observations([observation()])


def test_identical_or_rejected_edit_does_not_rebuild_saved_draft(tmp_path):
    calls = []
    def inspector(*args, **kwargs):
        calls.append(args)
        return checked()
    invalid = {"action": "edit_plan", "message": "Invalid", "edit": {"features": [{"id": "body", "op": "cut", "inputs": ["missing"]}]}}
    actions = [record(), edit(), edit(), invalid, {"action": "ask_user", "message": "已保留。", "questions": ["继续？"]}]
    result = CadAgentService(provider_call=Provider(actions), draft_inspector=inspector).run(message="Build block", output_dir=tmp_path)
    assert len(calls) == 1
    assert result["draftInspection"]["valid"] is True
    assert result["artifacts"] == {}


def test_actual_draft_views_feed_next_action_without_authorizing_delivery(tmp_path):
    def sees_partial(body):
        current = context(body)
        draft = current["currentDraftInspection"]
        assert draft["valid"] is True
        assert {item["view"] for item in draft["projectionImages"]} == {"front", "top", "right"}
        assert all("path" not in item for item in draft["projectionImages"])
        assert len(draft["spatialImages"]) == 1
        assert draft["spatialImages"][0]["viewType"] == "isometric"
        assert draft["spatialImages"][0]["pixelRegistrationEligible"] is False
        assert "path" not in draft["spatialImages"][0]
        assert draft["deliveryArtifactsAvailable"] is False
        assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        content = body["input"][0]["content"]
        assert sum(item["type"] == "input_image" for item in content) == 4
        assert any("CURRENT PARTIAL DRAFT" in item.get("text", "") for item in content)
        return finish()
    def refuses_preview_finish(body):
        current = context(body)
        assert current["toolFeedback"]["stage"] == "finish_rejected"
        return {"action": "ask_user", "message": "已保留局部草稿。", "questions": ["是否增加安装特征？"]}
    result = CadAgentService(provider_call=Provider([record(), edit(), sees_partial, refuses_preview_finish])).run(
        message="Build a 30 mm high block", output_dir=tmp_path)
    assert result["status"] == "needs_input"
    assert result["inspection"] is None and result["artifacts"] == {}
    assert not list(tmp_path.rglob("*.step"))
