from __future__ import annotations

import io
import json
from pathlib import Path
import time
import urllib.error

import pytest
from PIL import Image

from app.ai_proxy import AIFile, AIProxy, AIProviderHTTPError
from app.cad_agent import CadAgentService, _digest, _execution_summary
from app.cad_source_reader import READER_VERSION, source_identity


def plan(height=30):
    return {"version": "cad-plan-v1", "name": "User block", "units": "mm",
            "parameters": {"height": {"value": height, "source": {"type": "user", "text": "height 30 mm"},
                                       "question": "方块高度是多少毫米？"}},
            "features": [{"id": "body", "op": "box", "size": [10, 20, "height"], "origin": [0, 0, 0]}], "result": "body"}


def observation(height=30):
    return {"id": "source_height", "kind": "bbox_size", "axis": 2, "expected": height,
            "label": "User stated overall height", "source": {"type": "user", "text": "height 30 mm"}}


def record(height=30):
    return {"action": "record_observations", "message": "先独立记录用户给出的总高度。", "observations": [observation(height)]}


def execute(height=30):
    return {"action": "execute_plan", "message": "建立长方体并测量包络。", "plan": plan(height)}


def finish(*, status="consistent", **extras):
    return {"action": "finish", "message": "几何测量与记录的尺寸一致，等待人工确认。",
            "drawingReview": {"status": status, "observations": ["生成侧视轮廓与输入要求对应。"], "differences": []}, **extras}


class Provider:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def __call__(self, body, timeout):
        self.requests.append((body, timeout))
        result = self.actions.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(body)
        return {"id": f"resp_{len(self.requests)}", "status": "completed", "output_text": json.dumps(result)}


def context(body):
    return json.loads(body["input"][0]["content"][0]["text"])


def raster(width=200, height=120):
    output = io.BytesIO()
    Image.new("RGB", (width, height), (230, 245, 255)).save(output, format="PNG")
    return AIFile("source.png", "image/png", output.getvalue())


class Executor:
    """Deterministic local tool stub, independently reporting actual height."""
    def __init__(self, *, fail_first=False, height_override=None, views=True):
        self.calls = []
        self.fail_first = fail_first
        self.height_override = height_override
        self.views = views

    def __call__(self, value, output_dir, *, timeout_seconds, ray_probes=None):
        self.calls.append({"plan": value, "output_dir": output_dir, "probes": ray_probes})
        if self.fail_first and len(self.calls) == 1:
            return {"status": "failed", "valid": False, "errors": [{"code": "invalid_geometry", "featureId": "body", "message": "height must be positive"}], "artifacts": {}}
        if value["parameters"]["height"]["value"] is None:
            return {"status": "needs_input", "valid": False, "missingParameters": ["height"], "errors": [], "artifacts": {}}
        height = self.height_override if self.height_override is not None else value["parameters"]["height"]["value"]
        artifacts = {"views": {}}
        for name in ("front", "top", "right") if self.views else ():
            path = Path(output_dir) / f"{name}.png"
            path.write_bytes(raster().data)
            artifacts["views"][name] = {"path": str(path), "mimeType": "image/png"}
        return {"status": "succeeded", "valid": True, "plan": value, "errors": [], "artifacts": artifacts,
                "resolvedParameters": {"height": height},
                "inspection": {"valid": True, "kernelBacked": True, "engine": "cadquery-occt", "bbox": {"size": [10, 20, height]}, "solidCount": 1, "volume": 200*height,
                               "drawingAgreement": "not_checked"}}


class StubSourceReader:
    """Keep loop-specific tests independent of the separately tested reader."""
    def __init__(self):
        self.calls = []

    def read(self, **kwargs):
        self.calls.append(kwargs)
        return {"version": READER_VERSION, **source_identity(kwargs["files"], kwargs["source_files"]),
                "status": "succeeded", "candidateEvidence": True, "verified": False,
                "annotations": [], "structureObservations": [], "questions": ["Test source annotation is unclear."]}


class StubDrawingReviewer:
    """Loop fixture for synthetic blank images; real reviewer has separate tests."""
    def __call__(self, **kwargs):
        return {"status": "consistent", "observations": ["Synthetic fixture matches."],
                "differences": [], "questions": []}


class StubSpatialInterpreter:
    """Isolate loop mechanics from the separately tested source-only vision pass."""
    def __init__(self):
        self.calls = []

    def interpret(self, **kwargs):
        from app.cad_source_spatial import spatial_identity, _validate_contract
        self.calls.append(kwargs)
        identity = spatial_identity(kwargs["files"], kwargs["source_files"], kwargs["transcription"])
        fixture = {"coordinateFrame": {"origin": "synthetic fixture origin", "x": "across", "y": "depth", "z": "up",
                   "units": "mm", "evidence": "synthetic fixture only", "viewIds": ["v1"], "confidence": "high"},
                   "views": [{"id": "v1", "imageId": "source-0", "kind": "orthographic", "location": "test image",
                              "bbox": [0, 0, 1, 1], "horizontalAxis": "+X", "verticalAxis": "+Z", "viewDirection": "+Y",
                              "evidence": "synthetic fixture only", "confidence": "high"}],
                   "features": [{"id": "synthetic_fixture", "kind": "solid_region", "description": "Synthetic test fixture only",
                                 "viewIds": ["v1"], "annotationIds": [], "datumIds": [], "axis": None,
                                 "evidence": "synthetic fixture only", "confidence": "high"}],
                   "datums": [], "relations": [], "unassignedAnnotationIds": identity["annotationIds"], "questions": []}
        contract = _validate_contract(fixture, image_ids=set(identity["imageIds"]), annotation_ids=set(identity["annotationIds"]))
        return {**identity,
                "status": "succeeded", "candidateEvidence": True, "verified": False,
                "contract": contract, "provider": {"requestCount": 0}}


def run(provider, executor, tmp_path, **kwargs):
    reader = kwargs.pop("source_reader", StubSourceReader())
    reviewer = kwargs.pop("drawing_reviewer", StubDrawingReviewer())
    spatial = kwargs.pop("spatial_interpreter", StubSpatialInterpreter())
    comparer = kwargs.pop("projection_comparer", None)
    questions = kwargs.pop("question_resolver", lambda **kwargs: {"status": "failed", "errorCode": "stub_unresolved"})
    return CadAgentService(provider_call=provider, executor=executor, source_reader=reader,
                           drawing_reviewer=reviewer, spatial_interpreter=spatial, projection_comparer=comparer,
                           question_resolver=questions).run(message="Create a block of height 30 mm.", output_dir=tmp_path, **kwargs)


def test_failed_geometry_is_returned_to_provider_then_repaired_and_rechecked(tmp_path):
    executor = Executor(fail_first=True)

    def repair(body):
        feedback = context(body)["toolFeedback"]
        assert feedback["result"]["errors"][0]["featureId"] == "body"
        summary = context(body)["currentExecutionSummary"]
        assert summary["status"] == "failed"
        assert summary["errors"][0]["featureId"] == "body"
        assert summary["hasFreshValidGeometry"] is False
        return execute(30)

    def review(body):
        assert context(body)["toolFeedback"]["result"]["inspection"]["acceptance"]["status"] == "passed"
        assert len([item for item in body["input"][0]["content"] if item["type"] == "input_image"]) == 3
        return finish(ready=True, productionReady=True, passed=True)

    provider = Provider([record(), execute(-3), repair, review])
    events = []
    result = run(provider, executor, tmp_path, progress=events.append)
    assert result["status"] == "review_required"
    assert result["plan"]["parameters"]["height"]["value"] == 30
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert result["drawingReview"]["humanConfirmed"] is False
    assert "ready" not in result and "productionReady" not in result
    assert len(executor.calls) == 2
    assert json.loads((tmp_path / "iteration-2/submitted-plan.json").read_text())["parameters"]["height"]["value"] == -3
    assert json.loads((tmp_path / "iteration-3/submitted-plan.json").read_text())["parameters"]["height"]["value"] == 30
    assert any(item["stage"] == "inspect_geometry" for item in events)
    assert (tmp_path / "agent-state.json").exists()
    assert json.loads((tmp_path / "agent-state.json").read_text())["state"]["cadPlan"] == result["plan"]
    assert "base64," not in json.dumps(result["trace"])
    assert str(tmp_path) not in json.dumps(result["trace"])
    instructions = provider.requests[0][0]["instructions"]
    assert "bracket_support_v1" not in instructions
    assert "arched_clevis_support_v1" not in instructions


def test_deterministic_measurement_mismatch_cannot_be_overridden_by_ai_claim(tmp_path):
    provider = Provider([record(), execute(25), finish(), {"action": "ask_user", "message": "实际高度为25，仍需修改。", "questions": ["请确认总高度。"]}])
    result = run(provider, Executor(), tmp_path)
    assert result["status"] == "needs_input"
    assert result["inspection"]["acceptance"]["status"] == "failed"
    assert context(provider.requests[-1][0])["toolFeedback"]["errors"][0]["code"] == "source_measurements_not_passed"


def test_provider_cannot_change_expected_value_to_match_bad_geometry(tmp_path):
    provider = Provider([record(30), execute(25), record(25), {"action": "ask_user", "message": "仍需修正模型。", "questions": ["是否继续按30毫米修正？"]}])
    result = run(provider, Executor(), tmp_path)
    assert result["observations"][0]["expected"] == 30
    assert "Do not change expected" in context(provider.requests[-1][0])["toolFeedback"]["errors"][0]["message"]


@pytest.mark.parametrize("field", ["id", "label", "source"])
def test_reworded_observations_are_rejected_with_retained_real_measurements_and_finish_guidance(tmp_path, field):
    reworded = record()
    reworded["observations"][0][field] = {"type": "user", "text": "User specified a total height of 30 mm"} if field == "source" else "reworded_height"

    def review(body):
        current = context(body)
        summary = current["currentExecutionSummary"]
        assert summary["status"] == "succeeded"
        assert summary["matchesCurrentPlan"] is True
        assert summary["hasFreshValidGeometry"] is True
        assert summary["inspection"]["bbox"]["size"] == [10, 20, 30]
        assert summary["resolvedParameters"] == {"height": 30}
        assert summary["inspection"]["acceptance"]["status"] == "passed"
        feedback = current["toolFeedback"]
        assert feedback["errors"][0]["code"] == "invalid_observations"
        assert feedback["preservedObservations"] == current["sourceObservations"]
        assert feedback["preservedObservations"][0]["id"] == "source_height"
        assert feedback["currentExecutionSummary"] == summary
        assert feedback["geometryRetained"] is True
        assert feedback["nextAction"] == "review_current_projections"
        assert "Preserve sourceObservations exactly" in feedback["next"]
        assert "No rebuild is needed" in feedback["next"]
        return finish()

    executor = Executor()
    provider = Provider([record(), execute(), reworded, review])
    result = run(provider, executor, tmp_path)
    assert result["status"] == "review_required"
    assert result["observations"][0][field] == observation()[field]
    assert len(executor.calls) == 1
    assert len(result["artifacts"]["views"]) == 3
    assert result["drawingReview"]["planHash"] == result["trace"][1]["planHash"]
    assert context(provider.requests[0][0])["currentExecutionSummary"]["status"] == "not_executed"


def test_rejected_ledger_edit_does_not_encourage_finish_when_actual_dimensions_fail(tmp_path):
    reworded = record()
    reworded["observations"][0]["label"] = "Reworded total height"

    def continue_repair(body):
        current = context(body)
        summary = current["currentExecutionSummary"]
        assert summary["hasFreshValidGeometry"] is True
        assert summary["inspection"]["acceptance"]["status"] == "failed"
        assert summary["inspection"]["bbox"]["size"] == [10, 20, 25]
        feedback = current["toolFeedback"]
        assert feedback["preservedObservations"][0]["expected"] == 30
        assert feedback["nextAction"] == "repair_or_request_evidence"
        assert "Do not finish" in feedback["next"]
        return {"action": "ask_user", "message": "实际高度25仍未达到要求。", "questions": ["请核对尚需修正的结构。"]}

    executor = Executor()
    result = run(Provider([record(), execute(25), reworded, continue_repair]), executor, tmp_path)
    assert result["status"] == "needs_input"
    assert len(executor.calls) == 1
    assert result["inspection"]["acceptance"]["status"] == "failed"


def test_execution_summary_cannot_promote_an_old_plan_hash_or_client_checks():
    previous = {"status": "succeeded", "valid": True,
                "inspection": {"valid": True, "kernelBacked": True, "engine": "cadquery-occt",
                               "bbox": {"size": [10, 20, 30]}, "acceptance": {"status": "passed"}},
                "resolvedParameters": {"height": 30}, "errors": []}
    stale = _execution_summary(previous, plan(31), _digest(plan(30)), projections_available=True)
    assert stale["inspection"] == previous["inspection"]
    assert stale["matchesCurrentPlan"] is False
    assert stale["hasFreshValidGeometry"] is False
    assert stale["projectionImagesAvailable"] is False
    no_execution = _execution_summary(None, plan(), None, projections_available=False)
    assert no_execution["inspection"] is None
    assert no_execution["status"] == "not_executed"


def test_unknown_dimensions_stop_without_defaults_and_resume_from_saved_state(tmp_path):
    first_provider = Provider([record(), execute(None)])
    first_executor = Executor()
    first = run(first_provider, first_executor, tmp_path / "first")
    assert first["status"] == "needs_input"
    assert first["plan"]["parameters"]["height"]["value"] is None
    assert first["questions"] == ["方块高度是多少毫米？"]
    assert first["artifacts"] == {}
    second_provider = Provider([record(), execute(30), finish()])
    second = run(second_provider, Executor(), tmp_path / "second", state={"agentState": first["state"]})
    assert context(second_provider.requests[0][0])["currentPlan"]["parameters"]["height"]["value"] is None
    assert second["status"] == "review_required"
    assert len(second["trace"]) > len(first["trace"])


def test_unknown_source_observation_stops_before_executor(tmp_path):
    executor = Executor()
    result = run(Provider([record(None)]), executor, tmp_path)
    assert result["status"] == "needs_input"
    assert result["observations"][0]["expected"] is None
    assert executor.calls == []
    assert result["plan"] is None


def test_source_crop_and_rotation_are_real_then_sent_back_as_images(tmp_path):
    def after_crop(body):
        info = context(body)["toolFeedback"]["sourceInspection"]
        assert info["imageSize"] == [120, 100]
        images = [item for item in body["input"][0]["content"] if item["type"] == "input_image"]
        assert len(images) == 2
        assert info["rotation"] == 90
        return {"action": "ask_user", "message": "局部标注仍不明确。", "questions": ["请确认厚度。"]}
    provider = Provider([{"action": "inspect_source", "message": "读取右侧标注。", "source": {"fileIndex": 0, "crop": [0.5, 0, 0.5, 1], "rotation": 90}}, after_crop])
    result = run(provider, Executor(), tmp_path, files=[raster()])
    assert result["status"] == "needs_input"
    with Image.open(tmp_path / "source-inspection-1.png") as image:
        assert image.size == (120, 100)
    assert "base64," not in json.dumps(result)


def test_a_fresh_execution_and_projection_review_are_required_after_state_resume(tmp_path):
    state = {"cadPlan": plan(), "inspection": {"valid": True, "productionReady": True}, "observations": [observation()]}
    provider = Provider([finish(), execute(), finish()])
    result = run(provider, Executor(), tmp_path, state=state)
    assert result["status"] == "review_required"
    assert context(provider.requests[1][0])["toolFeedback"]["errors"][0]["code"] == "fresh_execution_required"


@pytest.mark.parametrize("echo", ["empty", "same", "metadata"])
def test_finish_optional_plan_echo_never_discards_or_overwrites_executed_model(tmp_path, echo):
    proposed = {} if echo == "empty" else plan()
    if echo == "metadata":
        proposed.update(name="New review label", notes=["Reviewed"], questions=[])
        proposed["parameters"]["height"].update(source={"type": "derived", "text": "Unsubmitted annotation"},
                                                   label="height", status="reviewed", question="")
        proposed["features"][0]["label"] = "New feature label"
    executor = Executor()
    result = run(Provider([record(), execute(), finish(plan=proposed)]), executor, tmp_path)
    assert result["status"] == "review_required"
    assert result["plan"] == plan()
    assert len(executor.calls) == 1
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert len(result["artifacts"]["views"]) == 3
    assert result["drawingReview"]["planHash"] == result["trace"][1]["planHash"]
    assert result["trace"][-1]["ignoredPlanSubmission"] is True


@pytest.mark.parametrize("change", ["dimension", "topology"])
def test_finish_changed_plan_requires_execution_without_destroying_current_result(tmp_path, change):
    proposed = plan(31) if change == "dimension" else plan()
    if change == "topology":
        proposed["features"][0]["origin"] = [1, 0, 0]

    def correct_finish(body):
        current = context(body)
        assert current["currentPlan"] == plan()
        assert current["toolFeedback"]["errors"][0]["code"] == "finish_plan_requires_execution"
        assert len([item for item in body["input"][0]["content"] if item["type"] == "input_image"]) == 3
        return finish()

    executor = Executor()
    result = run(Provider([record(), execute(), finish(plan=proposed), correct_finish]), executor, tmp_path)
    assert result["status"] == "review_required"
    assert result["plan"] == plan()
    assert len(executor.calls) == 1
    assert result["inspection"]["bbox"]["size"] == [10, 20, 30]
    assert result["drawingReview"]["planHash"] == result["trace"][1]["planHash"]


def test_identical_observation_replay_is_idempotent_and_ignores_extraneous_plan(tmp_path):
    repeated = {**record(), "plan": {}}

    def review(body):
        feedback = context(body)["toolFeedback"]
        assert feedback["changed"] is False
        assert feedback["geometryGenerated"] is True
        assert context(body)["currentPlan"] == plan()
        assert len([item for item in body["input"][0]["content"] if item["type"] == "input_image"]) == 3
        return finish(plan={})

    executor, events = Executor(), []
    result = run(Provider([record(), execute(), repeated, review]), executor, tmp_path, progress=events.append)
    assert result["status"] == "review_required"
    assert len(executor.calls) == 1
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert result["drawingReview"]["planHash"] == result["trace"][1]["planHash"]
    recorded_events = [item for item in events if item["stage"] == "observations_recorded"]
    assert [item["geometryGenerated"] for item in recorded_events] == [False, True]


def test_changed_observations_still_invalidate_geometry_until_reexecuted(tmp_path):
    provider = Provider([record(), execute(),
                         {"action": "inspect_source", "message": "重新核对高度。", "source": {"fileIndex": 0}},
                         record(31), finish()])
    executor = Executor()
    result = run(provider, executor, tmp_path, files=[raster()], max_turns=5)
    assert result["status"] == "failed"
    assert len(executor.calls) == 1
    assert result["observations"][0]["expected"] == 31
    assert result["artifacts"] == {}
    assert result["trace"][-1]["result"]["errors"][0]["code"] == "fresh_execution_required"


def test_finish_without_actual_projection_images_never_becomes_reviewable(tmp_path):
    provider = Provider([record(), execute(), finish()])
    result = run(provider, Executor(views=False), tmp_path, max_turns=3)
    assert result["status"] == "failed"
    assert result["trace"][-1]["result"]["errors"][0]["code"] == "projection_review_required"


def test_old_iteration_projections_cannot_stand_in_for_the_current_shape(tmp_path):
    class StaleProjectionExecutor(Executor):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            if len(self.calls) == 1:
                self.old_views = result["artifacts"]["views"]
            else:
                result["artifacts"]["views"] = self.old_views
            return result
    provider = Provider([record(), execute(), execute(), finish()])
    result = run(provider, StaleProjectionExecutor(), tmp_path, max_turns=4)
    assert result["status"] == "failed"
    assert result["trace"][-1]["result"]["errors"][0]["code"] == "projection_review_required"


def test_chatting_does_not_create_a_default_model(tmp_path):
    executor = Executor()
    provider = Provider([{"action": "ask_user", "message": "你好，可以告诉我你想建立什么模型。", "questions": []}])
    result = CadAgentService(provider_call=provider, executor=executor).run(message="你好", output_dir=tmp_path)
    assert result["status"] == "needs_input"
    assert result["plan"] is None
    assert executor.calls == []


def test_true_provider_transport_failure_is_safe_and_never_uses_recipe_fallback(monkeypatch, tmp_path):
    secret = "test-secret-never-in-result"
    monkeypatch.setenv("JOYNIU_AI_API_KEY", secret)
    def failed_request(request, timeout):
        assert request.full_url.endswith("/responses")
        body = json.loads(request.data)
        assert body["text"]["format"]["type"] == "json_object"
        assert body["store"] is False
        assert secret not in json.dumps(body)
        raise urllib.error.URLError("private transport details " + secret)
    monkeypatch.setattr("urllib.request.urlopen", failed_request)
    executor = Executor()
    result = CadAgentService(proxy=AIProxy(timeout_seconds=1), executor=executor).run(message="创建零件", output_dir=tmp_path)
    assert result["status"] == "failed"
    assert result["provider"]["lastErrorCode"] == "transport"
    assert secret not in json.dumps(result)
    assert "private transport" not in json.dumps(result)
    assert executor.calls == []


def test_wall_clock_limit_stops_streaming_provider_and_does_not_execute(tmp_path):
    def slow_provider(body, timeout):
        time.sleep(0.15)
        return {"output_text": json.dumps(execute())}
    executor = Executor()
    started = time.monotonic()
    result = run(slow_provider, executor, tmp_path, timeout_seconds=0.035)
    assert time.monotonic()-started < 0.14
    assert result["status"] == "failed"
    assert executor.calls == []
    assert result["provider"]["lastErrorCode"] in {"timeout", "time_limit"}


def test_cancel_callback_stops_before_provider_or_cad_execution(tmp_path):
    provider, executor = Provider([]), Executor()
    def cancelled(_event):
        raise RuntimeError("CAD request cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        run(provider, executor, tmp_path, progress=cancelled)
    assert provider.requests == executor.calls == []


def test_real_executor_generates_projection_images_for_the_next_provider_turn(tmp_path):
    from app.geometry import cadquery_status
    if not cadquery_status()["available"]:
        pytest.skip("CadQuery unavailable")
    provider = Provider([record(), execute(), finish()])
    result = CadAgentService(provider_call=provider).run(message="创建10×20×30毫米方块。", output_dir=tmp_path)
    assert result["status"] == "review_required", result
    assert result["inspection"]["bbox"]["size"] == pytest.approx([10, 20, 30])
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert Path(result["artifacts"]["step"]["path"]).is_file()
    assert Path(result["artifacts"]["glb"]["path"]).is_file()
    images = [item for item in provider.requests[-1][0]["input"][0]["content"] if item["type"] == "input_image"]
    assert len(images) == 4
    executed = next(item for item in result["trace"] if item["action"] == "execute_plan")
    assert {item["view"] for item in executed["projectionImages"]} == {"front", "top", "right"}
    assert len(executed["spatialImages"]) == 1
    assert executed["spatialImages"][0]["viewType"] == "isometric"
    assert executed["spatialImages"][0]["pixelRegistrationEligible"] is False
    assert set(result["artifacts"]["views"]) == {"front", "top", "right"}


def test_source_reading_and_observation_progress_do_not_claim_geometry_exists(tmp_path):
    provider = Provider([record(), {"action": "ask_user", "message": "还需要一个尺寸。", "questions": ["孔中心在哪里？"]}])
    executor, events = Executor(), []
    result = run(provider, executor, tmp_path, files=[raster()], progress=events.append)
    assert result["status"] == "needs_input"
    assert result["inspection"] is None and executor.calls == []
    stages = {event["stage"]: event for event in events}
    assert stages["observe_source"]["geometryGenerated"] is False
    assert stages["record_observations"]["geometryGenerated"] is False
    assert stages["observations_recorded"]["geometryGenerated"] is False
    assert stages["observations_recorded"]["observationKinds"] == ["bbox_size"]
    instructions = provider.requests[0][0]["instructions"]
    # These semantics must reach the actual request, including a relay that
    # accepts JSON objects but does not implement tool/function calling.
    for requirement in ("ENTIRE FINAL ENTITY", "Hole-centre spacing", "Local plate thickness",
                        "EXECUTABLE CHECK SUBSET", "components perpendicular"):
        assert requirement in instructions
    assert "profile_extrude:" not in instructions
    construction_instructions = provider.requests[1][0]["instructions"]
    assert "extrude -Y" in construction_instructions and "world coordinates" in construction_instructions


def test_invalid_observation_progress_exposes_rejection_without_geometry_success(tmp_path):
    invalid = record()
    invalid["observations"][0]["id"] = "not-a-valid-observation-id"
    provider = Provider([invalid, {"action": "ask_user", "message": "检查依据需要修正。", "questions": ["请补充依据。"]}])
    executor, events = Executor(), []
    result = run(provider, executor, tmp_path, progress=events.append)
    assert result["status"] == "needs_input"
    assert result["observations"] == [] and executor.calls == []
    rejection = next(event for event in events if event["stage"] == "observations_rejected")
    assert rejection["geometryGenerated"] is False
    assert rejection["errors"][0]["code"] == "invalid_observations"


@pytest.mark.parametrize("question_location", ["questions", "drawingReview"])
def test_text_only_finish_routine_approval_is_corrected_without_hiding_geometry(tmp_path, question_location):
    premature = finish()
    approval = ["没有提供源图纸或参考视图，无法进行图纸投影对比；请人工确认生成投影是否符合预期。"]
    if question_location == "questions":
        premature["questions"] = approval
    else:
        premature["drawingReview"]["questions"] = approval

    def corrected_finish(body):
        feedback = context(body)["toolFeedback"]
        assert feedback["stage"] == "finish_rejected"
        assert feedback["errors"][0]["code"] == "finish_questions_require_resolution"
        assert "source image is not required" in feedback["errors"][0]["message"]
        assert "confirmation button" in feedback["errors"][0]["message"]
        assert len([item for item in body["input"][0]["content"] if item["type"] == "input_image"]) == 3
        return finish(status="not_applicable", questions=[])

    executor = Executor()
    provider = Provider([record(), execute(), premature, corrected_finish])
    result = run(provider, executor, tmp_path)
    assert result["status"] == "review_required"
    assert result["questions"] == []
    assert result["drawingReview"]["status"] == "not_applicable"
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert len(result["artifacts"]["views"]) == 3
    assert len(executor.calls) == 1


def test_finish_with_true_missing_information_must_use_explicit_ask_user_action(tmp_path):
    provider = Provider([record(), execute(), finish(questions=["侧孔的轴向位置是多少？"]),
                         {"action": "ask_user", "message": "侧孔位置尚未指定。", "questions": ["侧孔距离端面多少毫米？"]}])
    result = run(provider, Executor(), tmp_path)
    assert result["status"] == "needs_input"
    assert result["questions"] == ["侧孔距离端面多少毫米？"]
    assert result["trace"][-2]["result"]["errors"][0]["code"] == "finish_questions_require_resolution"
    assert result["trace"][-1]["action"] == "ask_user"


def striped_source():
    output = io.BytesIO()
    image = Image.new("RGB", (80, 20))
    for index in range(8):
        image.paste((20+index*27, 210-index*19, 35+index*23), (index*10, 0, (index+1)*10, 20))
    image.save(output, format="PNG")
    return AIFile("independent-views.png", "image/png", output.getvalue())


def inspect_strip(index):
    return {"action": "inspect_source", "message": f"读取第{index+1}个局部视图。",
            "source": {"fileIndex": 0, "view": f"region-{index}", "crop": [index/8, 0, 1/8, 1], "rotation": 0}}


def test_distinct_crops_remain_visible_together_and_duplicate_bytes_do_not_grow_context(tmp_path):
    provider = Provider([inspect_strip(0), inspect_strip(1), inspect_strip(0),
                         {"action": "ask_user", "message": "两个局部之间的连接关系仍有歧义。", "questions": ["这两处是否贯通？"]}])
    result = run(provider, Executor(), tmp_path, files=[striped_source()])
    for body, _timeout in provider.requests[2:]:
        content = body["input"][0]["content"]
        assert len([item for item in content if item["type"] == "input_image"]) == 3  # original + both details
        details = []
        for item in content:
            if item["type"] != "input_text":
                continue
            try:
                metadata = json.loads(item["text"])
            except ValueError:
                continue
            if "sourceDetail" in metadata:
                details.append(metadata["sourceDetail"])
        assert {item["sourceInspections"][0]["view"] for item in details} == {"region-0", "region-1"}
        assert [item["sourceInspections"][0]["crop"] for item in details] == [[0, 0, 1/8, 1], [1/8, 0, 1/8, 1]]
    assert context(provider.requests[-1][0])["toolFeedback"]["newImageEvidence"] is False
    assert len(result["state"]["retainedSourceDetails"]) == 2


def test_retained_source_details_evict_oldest_when_six_unique_images_are_kept(tmp_path):
    provider = Provider([*[inspect_strip(index) for index in range(7)],
                         {"action": "ask_user", "message": "还需要确认一处标注。", "questions": ["这处标注的单位是什么？"]}])
    result = run(provider, Executor(), tmp_path, files=[striped_source()])
    details = context(provider.requests[-1][0])["retainedSourceDetails"]
    assert len(details) == 6
    assert [item["sourceInspections"][0]["view"] for item in details] == [f"region-{index}" for index in range(1, 7)]
    assert len([item for item in provider.requests[-1][0]["input"][0]["content"] if item["type"] == "input_image"]) == 7
    assert len(result["state"]["retainedSourceDetails"]) == 6


def test_each_provider_call_has_a_structured_running_checkpoint_before_it_starts(monkeypatch, tmp_path):
    secret = "checkpoint-must-not-contain-provider-key"
    monkeypatch.setenv("JOYNIU_AI_API_KEY", secret)
    executor = Executor(fail_first=True)
    calls = []

    def provider(body, timeout):
        checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
        calls.append(checkpoint)
        assert checkpoint["status"] == "running"
        assert checkpoint["iteration"] == len(calls)
        serialized = json.dumps(checkpoint)
        assert secret not in serialized and "base64," not in serialized and "data:image" not in serialized
        if len(calls) == 1:
            assert checkpoint["plan"] is None
            assert checkpoint["trace"][-1]["action"] == "interpret_source_spatial"
            assert checkpoint["sourceTranscription"]["candidateEvidence"] is True
            assert checkpoint["sourceSpatialContract"]["candidateEvidence"] is True
            action = record()
        elif len(calls) == 2:
            assert checkpoint["observations"][0]["expected"] == 30
            assert checkpoint["trace"][-1]["action"] == "record_observations"
            action = execute(-3)
        else:
            assert checkpoint["plan"]["parameters"]["height"]["value"] == -3
            assert context(body)["currentExecutionSummary"]["errors"][0]["featureId"] == "body"
            if len(calls) == 4:
                assert checkpoint["trace"][-1]["action"] == "provider_error"
            raise AIProviderHTTPError(503)
        return {"status": "completed", "output_text": json.dumps(action)}

    result = run(provider, executor, tmp_path, files=[raster()])
    assert result["status"] == "failed"
    assert len(calls) == 4 and len(executor.calls) == 1
    assert json.loads((tmp_path / "checkpoint.json").read_text())["iteration"] == 4


def edit(height=30):
    draft = plan(height)
    return {"action": "edit_plan", "message": "保存主体特征草稿。", "edit": {
        "name": draft["name"], "parameters": draft["parameters"],
        "features": draft["features"], "result": draft["result"]}}


def test_incremental_draft_executes_current_plan_without_repeating_it(tmp_path):
    provider = Provider([record(), edit(), {"action": "execute_plan", "message": "执行当前草稿。"}, finish()])
    executor, events = Executor(), []
    result = run(provider, executor, tmp_path, progress=events.append)
    assert result["status"] == "review_required"
    assert len(executor.calls) == 1
    assert executor.calls[0]["plan"]["parameters"]["height"]["value"] == 30
    event = next(item for item in events if item["stage"] == "plan_saved")
    assert event["geometryGenerated"] is False
    assert event["featureCount"] == 1
    current = context(provider.requests[2][0])
    assert current["currentPlan"]["result"] == "body"
    assert all("plan" not in item and "edit" not in item and "observations" not in item for item in current["actionHistory"])


def test_draft_is_recoverable_when_next_provider_operation_fails(tmp_path):
    provider = Provider([record(), edit(), AIProviderHTTPError(503)])
    executor = Executor()
    result = run(provider, executor, tmp_path)
    assert result["status"] == "failed"
    assert result["plan"]["features"][0]["id"] == "body"
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["plan"] == result["plan"]
    assert checkpoint["requestMetrics"]["imageCount"] == 4
    current = context(provider.requests[-1][0])
    draft = current["currentDraftInspection"]
    assert draft["planHash"] == checkpoint["draftInspection"]["planHash"] == _digest(result["plan"])
    assert draft["scope"] == "draft_construction" and draft["drawingAgreement"] == "not_checked"
    assert draft["deliveryArtifactsAvailable"] is False
    assert {item["view"] for item in draft["projectionImages"]} == {"front", "top", "right"}
    assert len(draft["spatialImages"]) == 1
    assert draft["spatialImages"][0]["viewType"] == "isometric"
    assert draft["spatialImages"][0]["pixelRegistrationEligible"] is False
    content = provider.requests[-1][0]["input"][0]["content"]
    assert sum(item["type"] == "input_image" for item in content) == 4
    assert any("CURRENT PARTIAL DRAFT" in item.get("text", "") for item in content)
    assert not any(item.get("text") == "Actual OCCT projections of the latest executed plan" for item in content)
    assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is False
    assert executor.calls == []
    assert 0 < checkpoint["requestMetrics"]["instructionChars"] < 15000
    assert result["trace"][-1]["providerCall"]["elapsedSeconds"] >= 0
    assert result["inspection"] is None and result["artifacts"] == {}
    continuation = Provider([{"action": "execute_plan", "message": "继续执行已保存草稿。"}, finish()])
    resumed_executor = Executor()
    resumed = run(continuation, resumed_executor, tmp_path / "resumed", state=result["state"])
    assert resumed["status"] == "review_required"
    assert context(continuation.requests[0][0])["currentExecutionSummary"]["hasFreshValidGeometry"] is False
    assert len(resumed_executor.calls) == 1


def test_rejected_incremental_edit_keeps_executed_geometry(tmp_path):
    invalid = {"action": "edit_plan", "message": "错误的局部替换。", "edit": {
        "features": [{"id": "body", "op": "cut", "inputs": ["body", "missing"]}]}}
    def after_rejection(body):
        current = context(body)
        assert current["toolFeedback"]["errors"][0]["code"] == "invalid_plan_edit"
        assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is True
        assert current["currentPlan"]["features"][0]["op"] == "box"
        return finish()
    result = run(Provider([record(), execute(), invalid, after_rejection]), Executor(), tmp_path)
    assert result["status"] == "review_required"


def test_changed_incremental_parameter_invalidates_previous_artifacts_until_execution(tmp_path):
    update = {"action": "edit_plan", "message": "修改主体高度。", "edit": {"parameters": plan(25)["parameters"]}}
    def after_edit(body):
        current = context(body)
        assert current["currentPlan"]["parameters"]["height"]["value"] == 25
        assert current["currentExecutionSummary"]["hasFreshValidGeometry"] is False
        assert current["currentExecutionSummary"]["inspection"] is None
        return {"action": "ask_user", "message": "需要澄清高度要求。", "questions": ["按哪个高度建模？"]}
    result = run(Provider([record(), execute(), update, after_edit]), Executor(), tmp_path)
    assert result["status"] == "needs_input"
    assert result["artifacts"] == {} and result["inspection"] is None
    assert result["observations"][0]["expected"] == 30


def test_saved_draft_checkpoint_exists_before_progress_consumer_can_cancel(tmp_path):
    def cancel_after_saved(event):
        if event.get("stage") == "plan_saved":
            checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
            assert checkpoint["plan"]["features"][0]["id"] == "body"
            assert checkpoint["trace"][-1]["action"] == "edit_plan"
            assert checkpoint["trace"][-1]["result"]["status"] == "saved"
            raise RuntimeError("consumer cancelled after saving")
    executor = Executor()
    with pytest.raises(RuntimeError, match="consumer cancelled"):
        run(Provider([record(), edit()]), executor, tmp_path, progress=cancel_after_saved)
    assert executor.calls == []


def test_one_transient_provider_failure_retries_current_state_without_losing_draft(tmp_path):
    provider = Provider([record(), edit(), AIProviderHTTPError(503),
                         {"action": "execute_plan", "message": "继续执行保存的草稿。"}, finish()])
    executor, events = Executor(), []
    result = run(provider, executor, tmp_path, progress=events.append)
    assert result["status"] == "review_required"
    assert len(executor.calls) == 1
    retry = context(provider.requests[3][0])
    assert retry["toolFeedback"]["stage"] == "provider_retry"
    assert retry["currentPlan"]["result"] == "body"
    assert retry["sourceObservations"][0]["expected"] == 30
    assert any(item["stage"] == "provider_retry" for item in events)


def test_continuation_rebuilds_retained_crops_from_same_original_without_provider_crop_calls(tmp_path):
    source = striped_source()
    first = run(Provider([inspect_strip(0), {"action": "ask_user", "message": "待继续", "questions": ["继续吗？"]}]),
                Executor(), tmp_path / "first", files=[source])
    saved = first["state"]
    def continued(body):
        current = context(body)
        assert len(current["retainedSourceDetails"]) == 1
        assert len([item for item in body["input"][0]["content"] if item["type"] == "input_image"]) == 2
        assert current["retainedSourceDetails"][0]["sha256"] == saved["retainedSourceDetails"][0]["sha256"]
        return {"action": "ask_user", "message": "仍需确认。", "questions": ["尺寸是多少？"]}
    result = run(Provider([continued]), Executor(), tmp_path / "continued", files=[source], state=saved)
    assert result["status"] == "needs_input"
    # A replaced image must not inherit an unrelated crop or its provenance.
    def changed(body):
        assert context(body)["retainedSourceDetails"] == []
        return {"action": "ask_user", "message": "新原图。", "questions": ["需要什么？"]}
    run(Provider([changed]), Executor(), tmp_path / "replaced", files=[raster()], state=saved)


def test_upstream_generation_error_retries_but_credentials_and_context_errors_do_not(tmp_path):
    from app.ai_proxy import AIProviderUpstreamError
    from app.cad_agent import _retryable_agent_failure
    for code in ("server_error", "generation_error", "upstream_timeout"):
        assert _retryable_agent_failure(AIProviderUpstreamError({"code": code}))
    for code in ("invalid_api_key", "insufficient_quota", "context_length_exceeded", "permission_denied"):
        assert not _retryable_agent_failure(AIProviderUpstreamError({"code": code}, retryable=True))
    provider = Provider([AIProviderUpstreamError({"code": "server_error"}), record(), execute(), finish()])
    result = run(provider, Executor(), tmp_path)
    assert result["status"] == "review_required"
    assert result["provider"]["retryCount"] == 1
    assert "lastErrorCode" not in result["provider"]


def test_invalid_measurement_is_available_for_targeted_repair_without_becoming_evidence(tmp_path):
    invalid = record()
    invalid["observations"][0]["kind"] = "total_height"
    def repair(body):
        current = context(body)
        rejected = current["toolFeedback"]["rejectedObservations"]
        assert current["sourceObservations"] == []
        assert rejected[0]["expected"] == 30
        assert rejected[0]["kind"] == "total_height"
        assert "source_height" in current["toolFeedback"]["errors"][0]["message"]
        assert "Repair only" in body["instructions"]
        return record()
    result = run(Provider([invalid, repair, execute(), finish()]), Executor(), tmp_path)
    assert result["status"] == "review_required"
    assert result["trace"][0]["rejectedObservations"][0]["kind"] == "total_height"
    assert result["observations"][0]["kind"] == "bbox_size"
