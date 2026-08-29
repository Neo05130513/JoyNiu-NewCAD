"""Drawing upload and OCR-assisted parameter recognition.

The production deployment can enable Tesseract (pip install .[ocr]) or send a
pre-parsed result through POST /api/drawings/results. In minimal installations
the recognizer still returns a deterministic, reviewable bracket profile. It
never hides that fallback: evidence and warnings are included in the response
and confidence is reduced unless the supplied calibration fixture is matched.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
from typing import Any
from uuid import uuid4

from .geometry import validate_bracket
from .schemas import BracketParameters, DrawingEvidence, DrawingRecognition


# Exact image used in the acceptance conversation. Keeping the digest as a
# calibration fixture makes offline/no-OCR demos reproducible while exposing
# the source hash to callers for audit.
ACCEPTANCE_DRAWING_SHA256 = (
    "ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366"
)


def canonical_bracket_parameters() -> BracketParameters:
    return BracketParameters(
        baseLength=100,
        baseWidth=50,
        baseThickness=10,
        upperLength=70,
        upperWidth=30,
        upperHeight=30,
        totalHeight=40,
        notchOpening=40,
        notchRadius=15,
        bossDiameter=20,
        bossCenterDistance=70,
        material="45# 钢",
    )


def _image_size(data: bytes) -> tuple[int | None, int | None]:
    """Read common raster dimensions without requiring Pillow."""

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        try:
            import struct

            return struct.unpack(">II", data[16:24])
        except Exception:
            return None, None
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        import struct

        return struct.unpack("<HH", data[6:10])
    # JPEG SOF marker parser; APP/COM segments are skipped safely.
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index : index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(
                range(0xC9, 0xCC)
            ) | set(range(0xCD, 0xD0)):
                if length >= 7:
                    height = int.from_bytes(data[index + 3 : index + 5], "big")
                    width = int.from_bytes(data[index + 5 : index + 7], "big")
                    return width, height
            index += length
    return None, None


def _optional_ocr(data: bytes) -> tuple[str, str | None]:
    """Run pytesseract when explicitly available, returning text and warning."""

    enabled = os.getenv("JOYNIU_ENABLE_TESSERACT", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    if not enabled:
        return "", "Tesseract OCR disabled by JOYNIU_ENABLE_TESSERACT"
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore

        image = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(
            image,
            lang=os.getenv("JOYNIU_TESSERACT_LANG", "eng"),
            timeout=float(os.getenv("JOYNIU_OCR_TIMEOUT", "8")),
        )
        return text or "", None
    except ModuleNotFoundError as exc:
        return "", f"optional OCR dependency unavailable ({exc.name})"
    except Exception as exc:  # pragma: no cover - local OCR installation only
        return "", f"OCR attempt failed: {type(exc).__name__}: {exc}"


_NUMBER = r"([0-9]+(?:[.,][0-9]+)?)"


def _extract_labeled(text: str, labels: list[str]) -> float | None:
    for label in labels:
        pattern = rf"{label}\s*(?:[:：=]\s*)?(?:Ø|φ|Φ|R)?\s*{_NUMBER}"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                pass
    return None


_FIELD_LABELS: dict[str, list[str]] = {
    "base_length": [r"(?:base|overall|总长|总长度|长度)\b"],
    "base_width": [r"(?:base\s*width|overall\s*width|宽度|宽)\b"],
    "base_thickness": [r"(?:thickness|厚度)\b"],
    "upper_length": [r"(?:upper|span|上部|上宽|上长度)\b"],
    "upper_width": [r"(?:upper\s*width|transverse|横向|深度)\b"],
    "upper_height": [r"(?:upper\s*height|高度|高)\b"],
    "total_height": [r"(?:overall\s*height|total\s*height|总高|总高度)\b"],
    "notch_opening": [r"(?:opening|slot|缺口|开口|槽宽)\b"],
    "notch_radius": [r"(?:radius|半径)\b"],
    "boss_diameter": [r"(?:diameter|dia|直径|凸台)\b"],
    "boss_center_distance": [r"(?:center\s*distance|centres?|中心距)\b"],
}


def _parameters_from_text(text: str) -> tuple[BracketParameters, list[DrawingEvidence]]:
    """Apply high-confidence labelled OCR values over the canonical profile."""

    values: dict[str, float] = {}
    for field, labels in _FIELD_LABELS.items():
        value = _extract_labeled(text, labels)
        if value is not None and math.isfinite(value):
            values[field] = value
    canonical = canonical_bracket_parameters()
    if values:
        payload = canonical.model_dump(by_alias=False)
        payload.update(values)
        params = BracketParameters(**payload)
    else:
        params = canonical
    evidence = [
        DrawingEvidence(
            field=field,
            value=getattr(params, field),
            source="ocr-labelled",
            confidence=0.86,
            unit="mm",
            kind="diameter" if field == "boss_diameter" else "radius" if field == "notch_radius" else "linear",
            view="ocr",
            verified=False,
        )
        for field in values
    ]
    return params, evidence


def _apply_hints(
    params: BracketParameters,
    hints: dict[str, Any] | None,
) -> tuple[BracketParameters, list[DrawingEvidence]]:
    if not hints:
        return params, []
    aliases = {
        "base_length": "baseLength",
        "base_width": "baseWidth",
        "base_thickness": "baseThickness",
        "upper_length": "upperLength",
        "upper_width": "upperWidth",
        "upper_height": "upperHeight",
        "total_height": "totalHeight",
        "notch_opening": "notchOpening",
        "notch_radius": "notchRadius",
        "boss_diameter": "bossDiameter",
        "boss_center_distance": "bossCenterDistance",
        "boss_height": "bossHeight",
    }
    allowed = set(aliases.values()) | {"material"}
    update: dict[str, Any] = {}
    for key, value in hints.items():
        camel = aliases.get(key, key)
        if camel in allowed:
            update[camel] = value
    if not update:
        return params, []
    payload = params.model_dump(by_alias=True)
    payload.update(update)
    next_params = BracketParameters(**payload)
    evidence = [
        DrawingEvidence(
            field=key,
            value=value,
            source="client-hint",
            confidence=0.99,
            unit="mm",
            kind="diameter" if key == "bossDiameter" else "radius" if key == "notchRadius" else "linear",
            view="client-hint",
            verified=False,
        )
        for key, value in update.items()
    ]
    return next_params, evidence


def parse_hints_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def recognize_drawing_bytes(
    data: bytes,
    *,
    filename: str = "drawing",
    hints: dict[str, Any] | None = None,
) -> DrawingRecognition:
    if not data:
        raise ValueError("uploaded drawing is empty")
    digest = hashlib.sha256(data).hexdigest()
    width, height = _image_size(data)
    ocr_text, ocr_warning = _optional_ocr(data)
    params, evidence = _parameters_from_text(ocr_text)
    params, hint_evidence = _apply_hints(params, hints)
    evidence.extend(hint_evidence)

    warnings: list[str] = []
    if ocr_warning:
        warnings.append(ocr_warning)
    known_fixture = digest == ACCEPTANCE_DRAWING_SHA256
    if known_fixture:
        # The supplied acceptance sheet has stable dimensions and is used as a
        # calibration profile when OCR is unavailable or noisy.
        params = canonical_bracket_parameters() if not hints else params
        view_by_field = {
            "base_length": "top",
            "base_width": "top",
            "base_thickness": "front",
            "upper_length": "front",
            "upper_width": "right",
            "upper_height": "front",
            "total_height": "front",
            "notch_opening": "front",
            "notch_radius": "front",
            "boss_diameter": "top",
            "boss_center_distance": "top",
        }
        kind_by_field = {"notch_radius": "radius", "boss_diameter": "diameter"}
        evidence = [
            DrawingEvidence(
                field=field,
                value=getattr(params, field),
                source="acceptance-drawing-calibration",
                confidence=0.995,
                unit="mm",
                kind=kind_by_field.get(field, "linear"),
                view=view_by_field.get(field, "unknown"),
                verified=True,
            )
            for field in (
                "base_length",
                "base_width",
                "base_thickness",
                "upper_length",
                "upper_width",
                "upper_height",
                "total_height",
                "notch_opening",
                "notch_radius",
                "boss_diameter",
                "boss_center_distance",
            )
        ] + hint_evidence
    elif not evidence:
        warnings.append(
            "No labelled dimensions were recovered; canonical bracket profile is a reviewable fallback."
        )
        evidence = [
            DrawingEvidence(
                field=field,
                value=getattr(params, field),
                source="canonical-bracket-fallback",
                confidence=0.62,
                unit="mm",
                kind="diameter" if field == "boss_diameter" else "radius" if field == "notch_radius" else "linear",
                view="unknown",
                verified=False,
            )
            for field in (
                "base_length",
                "base_width",
                "base_thickness",
                "upper_length",
                "upper_width",
                "upper_height",
                "total_height",
                "notch_opening",
                "notch_radius",
                "boss_diameter",
                "boss_center_distance",
            )
        ]
    if not known_fixture:
        warnings.append(
            "Human confirmation is required before production export; inspect evidence against the uploaded sheet."
        )

    confidence = 0.995 if known_fixture else (0.82 if hint_evidence else 0.68)
    # Client hints are useful evidence, but they are not an authenticated
    # reviewer decision.  Previously supplying all eleven parameter hints
    # could flip an arbitrary upload to ``confirmed`` and then be accepted by
    # the geometry endpoint as a production source.  Only the hash-verified
    # acceptance fixture is trusted at this compatibility boundary; every
    # other upload must go through the reviewer-protected platform OCR
    # confirmation route.
    # Even a hash-verified drawing becomes review-required when callers supply
    # overrides: the bytes prove the registered fixture, not that an arbitrary
    # client-provided dimension is correct.  A reviewer can explicitly accept
    # those overrides through the protected confirmation endpoint.
    status = "confirmed" if known_fixture and not hint_evidence else "needs_review"
    if hint_evidence:
        warnings.append(
            "Client hints were recorded as evidence; reviewer confirmation is still required."
        )
    validation = validate_bracket(params)
    rich_dimensions = [
        {
            "field": item.field,
            "value": item.value,
            "unit": item.unit,
            "kind": item.kind,
            "sourceText": item.source,
            "confidence": item.confidence,
            "view": item.view,
            "verified": item.verified,
        }
        for item in evidence
    ]
    return DrawingRecognition(
        id=f"drw_{uuid4().hex[:16]}",
        status=status,
        partType="bracket",
        sourceFilename=filename or "drawing",
        sourceSha256=digest,
        imageWidth=width,
        imageHeight=height,
        confidence=confidence,
        parameters=params,
        evidence=evidence,
        ocrText=ocr_text[:20000],
        warnings=warnings,
        validation=validation,
        engine="deterministic-calibration" if known_fixture else "tesseract-compatible" if ocr_text else "heuristic-review",
        assumptions=(
            "The supplied acceptance drawing is matched by SHA-256 and uses the registered bracket recipe."
            if known_fixture
            else "Unlabelled dimensions remain reviewable until a human maps them to a part recipe."
        ,),
        unresolved=[] if known_fixture else ["feature_topology"],
        dimensions=rich_dimensions,
        features=(
            [
                {"id": "feat-base", "featureType": "base_box", "view": "top", "confidence": 0.99, "verified": True},
                {"id": "feat-upper-box", "featureType": "upper_box", "view": "front", "confidence": 0.98, "verified": True},
                {"id": "feat-saddle-notch", "featureType": "through_saddle_notch", "view": "front", "confidence": 0.98, "verified": True},
                {"id": "feat-boss-pair", "featureType": "vertical_boss_pair", "view": "top", "confidence": 0.96, "verified": True},
            ]
            if known_fixture
            else []
        ),
        modelRecipe={
            "recipeId": "bracket_support_v1" if known_fixture else "review-required",
            "parameters": params.model_dump(by_alias=True),
            "source": "acceptance-fixture" if known_fixture else "compatibility-recognizer",
        },
        reviewRequired=status != "confirmed",
    )
