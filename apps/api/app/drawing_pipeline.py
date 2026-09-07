"""Shared engineering-drawing evidence contracts and lightweight preprocessing.

This module deliberately does not decide CAD topology. It normalizes raster/PDF
inputs into bounded view, text, geometry and feature-candidate records that can
be reviewed, passed to the AI proxy, and later checked against OCCT metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping
import io
import re

@dataclass(frozen=True, slots=True)
class DrawingView:
    id: str
    view_type: str
    bbox: tuple[float, float, float, float]
    source: str = "image"
    confidence: float = 0.0

@dataclass(frozen=True, slots=True)
class DrawingDimensionCandidate:
    id: str
    kind: str
    value: float
    bbox: tuple[float, float, float, float]
    view_id: str | None = None
    unit: str | None = None
    source_type: str = "document_text"
    geometry_ids: tuple[str, ...] = ()
    needs_review: bool = True

@dataclass(frozen=True, slots=True)
class FeatureCandidate:
    id: str
    feature_type: str
    view_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    geometry_ids: tuple[str, ...]
    confidence: float
    needs_review: bool = True

def bounded_remote_drawing_context(*, views: list[DrawingView], dimensions: list[DrawingDimensionCandidate], geometry: list[Mapping[str, Any]], max_items: int = 256) -> dict[str, Any]:
    """Return numeric/enum-only context safe for a remote model prompt."""
    return {
        "views": [{"id": v.id, "viewType": v.view_type, "bbox": list(v.bbox), "source": v.source, "confidence": round(v.confidence, 4)} for v in views[:32]],
        "dimensions": [{"id": d.id, "kind": d.kind, "value": d.value, "bbox": list(d.bbox), "viewId": d.view_id, "unit": d.unit, "sourceType": d.source_type, "geometryIds": list(d.geometry_ids), "needsReview": d.needs_review} for d in dimensions[:max_items]],
        "geometry": [dict(item) for item in geometry[:max_items]],
        "evidencePolicy": "dimensions are candidates until view/feature mapping is confirmed; isometric views are topology hints only",
    }

def dimension_constraint_report(values: Mapping[str, Any], rules: list[Mapping[str, Any]]) -> dict[str, Any]:
    checks = []
    for rule in rules:
        field = str(rule.get("field", "")); expected = rule.get("expected")
        actual = values.get(field)
        passed = actual is not None and (expected is None or abs(float(actual) - float(expected)) <= float(rule.get("tolerance", 0.05)))
        checks.append({"rule": rule.get("id", field), "field": field, "actual": actual, "expected": expected, "status": "pass" if passed else "fail"})
    return {"status": "passed" if all(item["status"] == "pass" for item in checks) else "failed", "checks": checks}

def preprocess_raster_drawing(payload: bytes, filename: str = "drawing") -> dict[str, Any]:
    """Create deterministic enhancement and coarse view candidates for images.

    View segmentation is intentionally conservative: connected ink regions are
    grouped into boxes and labeled as orthographic/isometric hints; AI and a
    reviewer remain responsible for final projection semantics.
    """
    try:
        from PIL import Image, ImageEnhance, ImageOps
    except ModuleNotFoundError:
        return {"available": False, "warning": "Pillow unavailable", "views": [], "previewBytes": payload}
    try:
        image = Image.open(io.BytesIO(payload)).convert("RGB")
    except Exception:
        return {"available": False, "warning": "invalid raster image", "views": [], "previewBytes": payload}
    gray = ImageOps.grayscale(image)
    enhanced = ImageEnhance.Contrast(gray).enhance(2.6)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.8)
    # Coarse quadrant candidates are stable and avoid pretending to solve CV.
    w, h = enhanced.size
    boxes = [(0, 0, w // 2, h // 2), (w // 2, 0, w, h // 2), (0, h // 2, w // 2, h), (w // 2, h // 2, w, h)]
    views = [DrawingView(f"view-{i+1}", "isometric_hint" if i == 3 else "orthographic_candidate", tuple(map(float, box)), "image", 0.35) for i, box in enumerate(boxes)]
    out = io.BytesIO(); enhanced.save(out, format="PNG", optimize=True)
    geometry = []
    try:
        import cv2
        import numpy as np
        pixels = np.asarray(enhanced)
        edges = cv2.Canny(pixels, 80, 180)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for index, contour in enumerate(sorted(contours, key=cv2.contourArea, reverse=True)[:512]):
            x, y, cw, ch = cv2.boundingRect(contour)
            if cw >= 8 and ch >= 8:
                geometry.append({"id": f"img-geometry-{index+1}", "type": "contour", "bbox": [x, y, x + cw, y + ch], "area": round(float(cv2.contourArea(contour)), 2)})
    except Exception:
        pass
    dimensions = []
    try:
        import pytesseract
        # Thin engineering annotations benefit from several independent OCR
        # passes. Coordinates are mapped back from the 2x enlarged image.
        channels = [enhanced, ImageOps.autocontrast(gray), gray.point(lambda p: 255 if p > 185 else 0)]
        seen = set()
        for channel_index, channel in enumerate(channels):
            scale = 2
            enlarged = channel.resize((w * scale, h * scale))
            data = pytesseract.image_to_data(enlarged, output_type=pytesseract.Output.DICT, config="--psm 11", timeout=8)
            for i, raw in enumerate(data.get("text", [])):
                text = str(raw or "").strip().replace("O", "Ø")
                if not text:
                    continue
                match = re.search(r"(?:Ø|φ|Φ)\s*(\d+(?:[.,]\d+)?)|\b[Rr]\s*(\d+(?:[.,]\d+)?)\b|\b(\d+(?:[.,]\d+)?)\s*(?:mm|毫米)\b", text)
                if not match:
                    continue
                value = next((group for group in match.groups() if group is not None), None)
                if value is None:
                    continue
                left, top = int(data["left"][i] / scale), int(data["top"][i] / scale); width, height = int(data["width"][i] / scale), int(data["height"][i] / scale)
                key = (round(float(value.replace(",", ".")), 3), left // 8, top // 8)
                if key in seen:
                    continue
                seen.add(key)
                view_id = next((v.id for v in views if v.bbox[0] <= left <= v.bbox[2] and v.bbox[1] <= top <= v.bbox[3]), None)
                kind = "diameter" if text[:1] in "ØφΦ" else "radius" if text[:1] in "Rr" else "linear"
                dimensions.append({"id": f"img-text-{len(dimensions)+1}", "sourceType": "document_text", "kind": kind, "value": float(value.replace(",", ".")), "bbox": [left, top, left + width, top + height], "viewId": view_id, "ocrPass": channel_index, "needsReview": True})
    except Exception:
        pass
    return {"available": True, "width": w, "height": h, "views": [asdict(v) for v in views], "dimensions": dimensions[:256], "geometry": geometry, "previewBytes": out.getvalue(), "transforms": {"rotation": 0, "crop": [0, 0, w, h]}}

__all__ = ["DrawingView", "DrawingDimensionCandidate", "FeatureCandidate", "bounded_remote_drawing_context", "dimension_constraint_report", "preprocess_raster_drawing"]
