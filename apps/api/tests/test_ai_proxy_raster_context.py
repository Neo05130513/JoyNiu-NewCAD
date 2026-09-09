"""Raster grid navigation must not become a fabricated drawing-view prior."""

import io
from types import SimpleNamespace

from PIL import Image

from app.ai_proxy import AIFile, _prepare_provider_attachments


def raster_bytes():
    output = io.BytesIO()
    Image.new("RGB", (200, 160), "white").save(output, format="PNG")
    return output.getvalue()


def test_raster_quadrants_are_navigation_tiles_without_projection_identity(monkeypatch):
    boxes = [[0, 0, 100, 80], [100, 0, 200, 80], [0, 80, 100, 160], [100, 80, 200, 160]]
    monkeypatch.setattr("app.drawing_pipeline.preprocess_raster_drawing", lambda *_: {
        "available": True, "width": 200, "height": 160,
        "views": [{"id": f"view-{index + 1}", "bbox": box, "view_type": "isometric_hint" if index == 3 else "orthographic_candidate",
                   "confidence": .35} for index, box in enumerate(boxes)],
        "dimensions": [{"id": "text-1", "kind": "radius", "value": 21, "bbox": [95, 30, 115, 44], "viewId": "view-1", "needsReview": True}],
    })
    original = AIFile("unseen-layout.png", "image/png", raster_bytes())
    files, contexts, _ = _prepare_provider_attachments((original,))
    assert files == (original,)  # Image bytes and transport resolution are untouched.
    context = contexts[0]
    assert context["sourceType"] == "image_geometry_candidate"
    assert context["views"] == []
    assert [tile["bbox"] for tile in context["tiles"]] == boxes
    assert all(tile["sourceType"] == "fixed_grid_tile" and tile["viewDetected"] is False for tile in context["tiles"])
    assert all("view_type" not in tile and "confidence" not in tile for tile in context["tiles"])
    dimension = context["dimensions"][0]
    assert dimension["value"] == 21 and dimension["bbox"] == [95, 30, 115, 44]
    assert dimension["tileId"] == "grid-tile-1" and "viewId" not in dimension
    assert "cross tile boundaries" in context["evidencePolicy"]


def test_dwg_native_vector_summary_remains_unchanged(monkeypatch):
    summary = {"schemaVersion": "joyniu.dwg-vector-summary.v1", "units": "mm", "sourceEntityCount": 4, "entityCount": 4,
               "views": [{"id": "actual-view", "geometryIds": ["line-1"]}],
               "dimensions": [{"id": "dimension_00001", "measurement": 47, "viewId": "actual-view"}],
               "geometry": [{"id": "line-1", "type": "LINE", "start": [0, 0], "end": [47, 0]}]}
    monkeypatch.setattr("app.dwg_preprocessor.preprocess_dwg", lambda *_: SimpleNamespace(
        summary=summary, dxf_bytes=b"parsed-vector", png_bytes=raster_bytes(), converter="fixture",
        original_metadata=SimpleNamespace(signature="AC1032", version="R2018")))
    files, contexts, metadata = _prepare_provider_attachments((AIFile("drawing.dwg", "application/acad", b"AC1032fixture"),))
    assert contexts == (summary,)
    assert "tiles" not in contexts[0]
    assert contexts[0]["dimensions"][0]["viewId"] == "actual-view"
    assert metadata[0]["dwgPreprocessing"]["dimensionCount"] == 1
    assert files[0].filename.endswith("__dwg-vector-preview.png")


def test_pdf_page_geometry_summary_remains_unchanged(monkeypatch):
    summary = {"pageCount": 1, "renderedPageCount": 1, "omittedPageCount": 0,
               "evidenceType": "pdf_text_and_geometry_candidates",
               "pages": [{"page": 1, "width": 210, "height": 297, "rotation": 0,
                          "geometry": [{"id": "pdf-path-1", "bbox": [10, 20, 100, 80]}]}]}
    monkeypatch.setattr("app.pdf_preprocessor.preprocess_pdf", lambda *_: SimpleNamespace(
        summary=summary, page_count_rendered=1, page_count_omitted=0, png_bytes=raster_bytes()))
    files, contexts, metadata = _prepare_provider_attachments((AIFile("drawing.pdf", "application/pdf", b"%PDF-1.7 fixture"),))
    assert contexts == (summary,)
    assert "tiles" not in contexts[0]
    assert contexts[0]["pages"][0]["geometry"][0]["id"] == "pdf-path-1"
    assert metadata[0]["pdfPreprocessing"]["renderedPageCount"] == 1
    assert files[0].filename.endswith("__pdf-vector-preview.png")
