from app.drawing_pipeline import dimension_constraint_report, preprocess_raster_drawing


def test_dimension_constraint_report_blocks_mismatch():
    result = dimension_constraint_report(
        {"totalHeight": 39.5},
        [{"id": "height", "field": "totalHeight", "expected": 40, "tolerance": 0.1}],
    )
    assert result["status"] == "failed"


def test_raster_pipeline_returns_view_candidates():
    from PIL import Image
    import io
    image = Image.new("RGB", (800, 600), "white")
    output = io.BytesIO(); image.save(output, format="JPEG")
    result = preprocess_raster_drawing(output.getvalue(), "drawing.jpg")
    assert result["available"] is True
    assert len(result["views"]) == 4
    assert result["previewBytes"]
