"""Independent visual explanations guide repair without erasing pixel evidence."""
import json
from pathlib import Path

import pytest

from app.cad_agent import _comparison_images, _digest
from tests.test_cad_agent import Executor, Provider, context, execute, finish, raster, record, run


def pixel_report(status="mismatch"):
    return {"scope": "original_drawing", "status": status, "views": [
        {"view": "front", "status": "mismatch" if status == "mismatch" else "supported",
         "reliability": "high", "differenceRegions": [{"finding": "The original opening contour differs."}]}],
        "artifacts": []}


def test_pixel_mismatch_gets_visual_explanation_before_repair_and_fresh_review_after_execution(tmp_path):
    compared, reviewed = [], []
    explanation = {"sourceImageId": "source-0", "sourceLocation": "中央开口",
                   "modelImageId": "projection-front", "modelLocation": "中部连接面",
                   "finding": "实体中多出的连接面封闭了开口", "repair": "移除开口内的连接材料，保留侧壁", "confidence": "high"}
    def compare(**kwargs):
        compared.append(kwargs)
        assert kwargs["source_views"][0]["imageId"] == "source-0"
        assert 0 < kwargs["timeout_seconds"] <= 45
        directory = kwargs["output_dir"]
        directory.mkdir(parents=True)
        overlay = directory / "front-overlay.png"
        overlay.write_bytes(raster().data)
        value = pixel_report("mismatch" if len(compared) == 1 else "uncertain")
        value["artifacts"] = [{"kind": "overlay", "view": "front", "path": str(overlay)}]
        return value
    def review(**kwargs):
        reviewed.append(kwargs)
        assert "plan" not in kwargs and "observations" not in kwargs and "transcription" not in kwargs
        assert len(kwargs["source_files"]) == 1 and len(kwargs["projection_files"]) == 3
        assert len(kwargs["comparison_files"]) == 1
        assert "artifacts" not in kwargs["comparison"]
        return {"status": "mismatch" if len(reviewed) == 1 else "consistent",
                "observations": ["Actual pixels compared."],
                "differences": [explanation] if len(reviewed) == 1 else [], "questions": []}
    def repair(body):
        current = context(body)
        assert current["projectionComparison"]["views"][0]["status"] == "mismatch"
        combined = current["currentDrawingReview"]
        assert combined["source"] == "pixel_projection_comparison" and combined["status"] == "mismatch"
        assert combined["independentReview"]["differences"] == [explanation]
        assert any(explanation["repair"] in difference for difference in combined["differences"])
        assert any("original opening contour differs" in difference for difference in combined["differences"])
        assert current["toolFeedback"]["independentDrawingReview"]["differences"] == [explanation]
        assert "Change the actual mismatched construction" in body["instructions"]
        assert len([part for part in body["input"][0]["content"] if part["type"] == "input_image"]) == 5
        action = execute()
        action["plan"]["features"][0]["origin"] = [1, 0, 0]
        return action
    events = []
    result = run(Provider([record(), execute(), repair, finish()]), Executor(), tmp_path,
                 files=[raster()], projection_comparer=compare, drawing_reviewer=review, progress=events.append)
    assert result["status"] == "review_required"
    assert len(compared) == len(reviewed) == 2
    independent_calls = [item for item in result["trace"] if item["action"] == "independent_drawing_review"]
    assert len(independent_calls) == 2
    assert independent_calls[0]["result"]["differences"] == [explanation]
    assert independent_calls[0]["planHash"] != independent_calls[1]["planHash"]
    assert result["drawingReview"]["independentReview"]["status"] == "consistent"
    assert result["drawingReview"]["independentReview"]["planHash"] == _digest(result["plan"])
    assert result["drawingReview"]["projectionComparison"]["planHash"] == _digest(result["plan"])
    assert any(event["stage"] == "geometry_repair" for event in events)
    assert any(item["action"] == "projection_compare" for item in result["trace"])


@pytest.mark.parametrize("review_failed", [False, True])
def test_ai_consistent_claim_or_review_failure_cannot_override_a_reliable_contour_difference(tmp_path, review_failed):
    reviewed = []
    def reviewer(**kwargs):
        reviewed.append(kwargs)
        if review_failed:
            raise TimeoutError("private review transport diagnostics")
        return {"status": "consistent", "observations": ["AI claims these contours agree."], "differences": [], "questions": []}
    provider = Provider([record(), execute(), finish()])
    result = run(provider, Executor(), tmp_path,
                 files=[raster()], max_turns=3, projection_comparer=lambda **kwargs: pixel_report(), drawing_reviewer=reviewer)
    assert result["status"] == "failed"
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert result["trace"][-1]["result"]["errors"][0]["code"] == "source_contour_mismatch"
    assert result["drawingReview"]["status"] == "mismatch" and len(reviewed) == 1
    assert result["drawingReview"]["source"] == "pixel_projection_comparison"
    independent = result["drawingReview"]["independentReview"]
    assert independent["planHash"] == _digest(result["plan"])
    assert independent["status"] == ("uncertain" if review_failed else "consistent")
    assert len(provider.requests) == 3  # Failed interpretation must still reach the next repair/finish turn.
    assert "private review transport" not in json.dumps(result)
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["drawingReview"]["status"] == "mismatch"
    assert checkpoint["drawingReview"]["independentReview"] == independent


@pytest.mark.parametrize("failure", ["exception", "reported_error"])
def test_known_contour_mismatch_continues_to_real_repair_after_independent_review_failure(tmp_path, failure):
    compared, reviewed, events = [], [], []
    def compare(**kwargs):
        compared.append(kwargs)
        return pixel_report("mismatch" if len(compared) == 1 else "uncertain")
    def review(**kwargs):
        reviewed.append(kwargs)
        if len(reviewed) == 1:
            if failure == "exception":
                raise TimeoutError("private review transport diagnostics")
            return {"status": "uncertain", "observations": [], "differences": [], "questions": [],
                    "errorCode": "independent_review_failed"}
        return {"status": "consistent", "observations": ["The repaired source and projection agree."],
                "differences": [], "questions": []}
    def repair(body):
        current = context(body)
        assert current["currentDrawingReview"]["status"] == "mismatch"
        assert current["currentDrawingReview"]["source"] == "pixel_projection_comparison"
        assert current["projectionComparison"]["views"][0]["status"] == "mismatch"
        assert current["toolFeedback"]["independentDrawingReview"]["errorCode"] == "independent_review_failed"
        assert current["currentDrawingReview"]["independentReview"]["errorCode"] == "independent_review_failed"
        assert "repair" in current["toolFeedback"]["next"]
        action = execute()
        action["plan"]["features"][0]["origin"] = [2, 0, 0]
        return action
    executor = Executor()
    result = run(Provider([record(), execute(), repair, finish()]), executor, tmp_path,
                 files=[raster()], projection_comparer=compare, drawing_reviewer=review, progress=events.append)
    assert result["status"] == "review_required"
    assert len(executor.calls) == len(compared) == len(reviewed) == 2
    assert result["drawingReview"]["independentReview"]["status"] == "consistent"
    assert result["drawingReview"]["independentReview"]["planHash"] == _digest(result["plan"])
    assert result["drawingReview"]["status"] == "consistent"
    assert any(item["action"] == "independent_drawing_review" and item["result"].get("errorCode") == "independent_review_failed" for item in result["trace"])
    assert any(event["stage"] == "geometry_repair" for event in events)
    assert "private review transport" not in json.dumps(result)


def test_uncertain_registration_is_preserved_without_claiming_a_pixel_match(tmp_path):
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path,
                 files=[raster()], projection_comparer=lambda **kwargs: {
                     "scope": "original_drawing", "status": "uncertain", "views": [], "errorCode": "comparison_unavailable"})
    assert result["status"] == "review_required"
    assert result["projectionComparison"]["status"] == "uncertain"
    assert result["projectionComparison"]["errorCode"] == "comparison_unavailable"
    assert result["drawingReview"]["projectionComparison"]["status"] == "uncertain"


def test_comparison_images_cannot_read_outside_the_current_execution_directory(tmp_path):
    directory = tmp_path / "comparison"
    directory.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(raster().data)
    symbolic = directory / "symlink.png"
    symbolic.symlink_to(outside)
    report = {"artifacts": [{"kind": "overlay", "view": "front", "path": str(path)} for path in (outside, symbolic)]}
    assert _comparison_images(report, directory) == ()
