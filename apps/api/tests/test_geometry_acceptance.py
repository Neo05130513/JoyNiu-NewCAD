from app.geometry_acceptance import validate_generated_geometry


def test_dimensional_acceptance_passes_matching_metrics():
    result = validate_generated_geometry(
        "split_clamp_support_v1",
        {"baseLength": 125, "baseWidth": 95, "totalHeight": 75, "mountHoleCenterDistance": 96},
        {"bboxLength": 125.001, "bboxWidth": 94.999, "bboxHeight": 75.0, "mountHoleCenterDistanceMeasured": 96.0},
    )
    assert result["status"] == "passed"
    assert result["productionReady"] is True


def test_dimensional_acceptance_blocks_missing_or_wrong_measurement():
    result = validate_generated_geometry(
        "split_clamp_support_v1",
        {"baseLength": 125, "baseWidth": 95, "totalHeight": 75, "mountHoleCenterDistance": 96},
        {"bboxLength": 125, "bboxWidth": 95, "bboxHeight": 75, "mountHoleCenterDistanceMeasured": 95.4},
    )
    assert result["status"] == "failed"
    assert result["productionReady"] is False
    assert "mountHoleCenterDistance" in result["failedFields"]
