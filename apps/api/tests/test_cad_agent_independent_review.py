"""A planner's success claim cannot override an isolated drawing review."""
import json

from app.cad_agent import _digest
from tests.test_cad_agent import Executor, Provider, context, execute, finish, plan, raster, record, run


def review(status="consistent", **extra):
    return {"status": status, "observations": ["Actual source/projection comparison."],
            "differences": ["An opening visible in the original is filled in the projection."] if status == "mismatch" else [],
            "questions": [], **extra}


def test_planner_cannot_override_independent_mismatch(tmp_path):
    events = []
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path,
                 files=[raster()], drawing_reviewer=lambda **kw: review("mismatch"),
                 max_turns=3, progress=events.append)
    assert result["status"] == "failed"
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert result["drawingReview"]["status"] == "mismatch"
    assert result["trace"][-1]["result"]["errors"][0]["code"] == "independent_drawing_review_required"
    assert any(item["stage"] == "independent_drawing_review" for item in events)
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["drawingReview"]["independentReview"]["status"] == "mismatch"


def test_repair_must_execute_and_pass_a_new_independent_review(tmp_path):
    calls = []
    def reviewer(**kwargs):
        calls.append(kwargs)
        assert "plan" not in kwargs and "observations" not in kwargs
        assert len(kwargs["source_files"]) == 1
        assert len(kwargs["projection_files"]) == 3
        assert 0 < kwargs["timeout_seconds"] <= 180
        return review("mismatch" if len(calls) == 1 else "consistent")
    def repair(body):
        current = context(body)
        assert current["currentDrawingReview"]["status"] == "mismatch"
        assert current["toolFeedback"]["independentDrawingReview"]["differences"]
        action = execute()
        action["plan"]["features"][0]["origin"] = [1, 0, 0]
        return action
    result = run(Provider([record(), execute(), repair, finish()]), Executor(), tmp_path,
                 files=[raster()], drawing_reviewer=reviewer)
    assert result["status"] == "review_required"
    assert len(calls) == 2
    independent = result["drawingReview"]["independentReview"]
    assert independent["status"] == "consistent"
    assert independent["planHash"] == _digest(result["plan"])
    assert result["drawingReview"]["humanConfirmed"] is False


def test_review_transport_failure_preserves_geometry_without_allowing_confirmation(tmp_path):
    def broken(**kwargs):
        raise TimeoutError("secret transport details must not be persisted")
    result = run(Provider([record(), execute()]), Executor(), tmp_path,
                 files=[raster()], drawing_reviewer=broken)
    assert result["status"] == "failed"
    assert result["inspection"]["valid"] is True
    assert result["drawingReview"]["independentReview"]["errorCode"] == "independent_review_failed"
    assert "secret transport" not in json.dumps(result)


def test_contradictory_consistent_review_is_not_accepted(tmp_path):
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path,
                 files=[raster()], max_turns=3,
                 drawing_reviewer=lambda **kw: review(questions=["Which contour is authoritative?"]))
    assert result["status"] == "failed"
    assert result["drawingReview"]["status"] == "uncertain"


def test_structured_visual_differences_are_visible_without_losing_evidence(tmp_path):
    difference = {"sourceImageId": "source-0", "sourceLocation": "原图下边缘开口",
                  "modelImageId": "projection-front", "modelLocation": "投影底部",
                  "finding": "额外材料封闭了开口", "repair": "修正贯通开口", "confidence": "high"}
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path,
                 files=[raster()], max_turns=3,
                 drawing_reviewer=lambda **kw: review("mismatch", differences=[difference]))
    assert result["drawingReview"]["differences"] == ["原图下边缘开口 · 额外材料封闭了开口 · 修正贯通开口"]
    assert result["drawingReview"]["independentReview"]["differences"] == [difference]


def test_saved_independent_success_cannot_replace_fresh_review(tmp_path):
    state = {"cadPlan": plan(), "drawingReview": {"status": "consistent",
             "independentReview": review(planHash=_digest(plan()))}}
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path,
                 files=[raster()], state=state, max_turns=3,
                 drawing_reviewer=lambda **kw: review("mismatch"))
    assert result["status"] == "failed"
    assert result["drawingReview"]["independentReview"]["status"] == "mismatch"


def test_text_only_model_does_not_require_drawing_reviewer(tmp_path):
    def forbidden(**kwargs):
        raise AssertionError("No drawing exists")
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path,
                 drawing_reviewer=forbidden)
    assert result["status"] == "review_required"
    assert result["drawingReview"]["status"] == "not_applicable"
    assert "independentReview" not in result["drawingReview"]
