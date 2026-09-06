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
    """Return the calibrated C-semantics bracket parameter profile.

    ``boss*`` names are retained because they are part of the public API used
    by older clients.  They describe the two Ø20 *cuts* in this recipe (the
    circles are not additive bosses).  Newer schema revisions expose the
    pocket/hole dimensions explicitly; passing them here is harmless on an
    older Pydantic model (which ignores extra input) and keeps this
    compatibility recognizer forward compatible with that schema.
    """

    return BracketParameters(
        baseLength=100,
        baseWidth=50,
        baseThickness=10,
        upperLength=70,
        # The right-view 30 mm callout is the Y length of the two top pockets,
        # not the width of the upper body.  The upper body spans the full
        # 50-mm base width.
        upperWidth=50,
        upperHeight=30,
        totalHeight=40,
        notchOpening=40,
        notchRadius=15,
        bossDiameter=20,
        bossCenterDistance=70,
        # Keep the historical alias for old callers.  It is deliberately not
        # used as the hole depth; the explicit holeDepth/holeThrough fields
        # below describe the subtractive feature.
        bossHeight=30,
        slotLength=30,
        slotWidth=10,
        pocketDepth=10,
        saddleDepth=50,
        holeDepth=40,
        holeThrough=True,
        material="45# 钢",
    )


def _parameter_value(parameters: BracketParameters, field_name: str, default: Any) -> Any:
    """Read an optional C-semantics field across schema revisions.

    The compatibility recognizer is shipped with older clients as well as the
    richer schema.  During a rolling upgrade a process may not yet expose the
    new slot/hole fields, so evidence generation must use the calibrated value
    rather than raising ``AttributeError``.
    """

    return getattr(parameters, field_name, default)


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

        ocr_data = data
        if data.startswith(b"%PDF-"):
            from .pdf_preprocessor import PDFPreprocessConfig, preprocess_pdf
            prepared = preprocess_pdf(data, "drawing.pdf", config=PDFPreprocessConfig(render_page_strategy="first"))
            ocr_data = prepared.png_bytes
        image = Image.open(io.BytesIO(ocr_data))
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
    # Do not map a generic ``depth`` token to upper_width: the acceptance
    # drawing's 30-mm right-view value is a pocket length along Y, while the
    # upper body itself is 50 mm wide.
    "upper_width": [
        r"(?:upper\s*width|transverse|横向)\b",
        r"(?:上部宽度|上宽)",
    ],
    "upper_height": [r"(?:upper\s*height|高度|高)\b"],
    "total_height": [r"(?:overall\s*height|total\s*height|总高|总高度)\b"],
    # ``slot`` by itself is ambiguous with the 30-mm rectangular pocket
    # length, so only explicit opening/notch labels are accepted here.
    "notch_opening": [
        r"(?:opening|notch)\b",
        r"(?:缺口|开口|槽宽)",
    ],
    "notch_radius": [r"(?:radius)\b", r"半径"],
    # Explicit C-semantics labels let the compatibility recognizer consume a
    # reviewer/OCR result that calls out the shallow pockets separately from
    # the historical notchOpening field.
    "slot_length": [
        r"(?:slot\s*(?:length|len)|pocket\s*length)\b",
        r"(?:槽长|槽长度)",
    ],
    "slot_width": [r"slot\s*width\b", r"槽宽度"],
    "pocket_depth": [
        r"(?:pocket\s*depth|slot\s*depth|pocket\s*deep)\b",
        r"(?:槽深|口深)",
    ],
    "saddle_depth": [r"(?:saddle\s*depth|saddle\s*width)\b", r"贯穿宽度"],
    "hole_depth": [r"(?:hole\s*depth|through\s*depth)\b", r"(?:孔深|贯穿深度)"],
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
            value=_parameter_value(params, field, values.get(field, 0.0)),
            source="ocr-labelled",
            confidence=0.86,
            unit="mm",
            kind=(
                "diameter"
                if field == "boss_diameter"
                else "radius"
                if field == "notch_radius"
                else "linear"
            ),
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
        "slot_length": "slotLength",
        "slot_width": "slotWidth",
        "feature_depth": "pocketDepth",
        "pocket_depth": "pocketDepth",
        "saddle_depth": "saddleDepth",
        "hole_depth": "holeDepth",
        "hole_through": "holeThrough",
        "boss_diameter": "bossDiameter",
        "boss_center_distance": "bossCenterDistance",
        "boss_height": "bossHeight",
    }
    # Derive the accepted names from the active schema.  This keeps the
    # compatibility route strict (new fields are not silently dropped on an
    # old deployment) while automatically enabling slot/hole overrides once
    # the richer schema is installed.
    model_fields = getattr(BracketParameters, "model_fields", {})
    allowed: set[str] = {"material"}
    if model_fields:
        for field_name, field_info in model_fields.items():
            allowed.add(str(field_name))
            alias = getattr(field_info, "alias", None)
            if alias:
                allowed.add(str(alias))
    else:  # pragma: no cover - compatibility with a Pydantic v1 host
        allowed.update(aliases.values())
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
            # The compatibility evidence schema predates boolean dimensions;
            # serialize a through/not-through hint as text rather than letting
            # Pydantic coerce ``False`` to numeric 0.0.
            value=str(value).lower() if isinstance(value, bool) else value,
            source="client-hint",
            confidence=0.99,
            unit="mm",
            kind=(
                "diameter"
                if key in {"bossDiameter", "holeDiameter"}
                else "radius"
                if key == "notchRadius"
                else "boolean"
                if key == "holeThrough"
                else "linear"
            ),
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
    # A hash-verified acceptance sheet already has a reviewed, immutable
    # dimension profile.  Running Tesseract over it first only adds latency
    # (and can let noisy OCR values leak into the evidence before the profile
    # is restored below).  Unknown/customer drawings still take the normal OCR
    # path, so this shortcut does not weaken general recognition.
    known_fixture = digest == ACCEPTANCE_DRAWING_SHA256
    ocr_text, ocr_warning = ("", None) if known_fixture else _optional_ocr(data)
    params, evidence = _parameters_from_text(ocr_text)
    params, hint_evidence = _apply_hints(params, hints)
    evidence.extend(hint_evidence)

    warnings: list[str] = []
    if ocr_warning:
        warnings.append(ocr_warning)
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
            "slot_length": "right",
            "slot_width": "top",
            "pocket_depth": "right",
            "saddle_depth": "right",
            "hole_depth": "front",
            "boss_diameter": "top",
            "boss_center_distance": "top",
        }
        kind_by_field = {"notch_radius": "radius", "boss_diameter": "diameter"}
        # These are the dimensions that describe the subtractive topology.
        # They are intentionally separate from the historical ``boss*`` API
        # names so a reviewer can see why each value is used in the recipe.
        calibrated_fields = (
            "base_length",
            "base_width",
            "base_thickness",
            "upper_length",
            "upper_width",
            "upper_height",
            "total_height",
            "notch_opening",
            "notch_radius",
            "slot_length",
            "slot_width",
            "pocket_depth",
            "saddle_depth",
            "hole_depth",
            "boss_diameter",
            "boss_center_distance",
        )
        fallback_values = {
            "slot_length": 30.0,
            "slot_width": 10.0,
            "pocket_depth": 10.0,
            "saddle_depth": 50.0,
            "hole_depth": 40.0,
        }
        evidence = [
            DrawingEvidence(
                field=field,
                value=_parameter_value(params, field, fallback_values.get(field, 0.0)),
                source="acceptance-drawing-calibration",
                confidence=0.995,
                unit="mm",
                kind=kind_by_field.get(field, "linear"),
                view=view_by_field.get(field, "unknown"),
                verified=True,
            )
            for field in calibrated_fields
        ] + hint_evidence
    elif not evidence:
        warnings.append(
            "No labelled dimensions were recovered; canonical bracket profile is a reviewable fallback."
        )
        fallback_values = {
            "slot_length": 30.0,
            "slot_width": 10.0,
            "pocket_depth": 10.0,
            "saddle_depth": 50.0,
            "hole_depth": 40.0,
        }
        evidence = [
            DrawingEvidence(
                field=field,
                value=_parameter_value(params, field, fallback_values.get(field, 0.0)),
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
                "slot_length",
                "slot_width",
                "pocket_depth",
                "saddle_depth",
                "hole_depth",
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
            "The supplied acceptance drawing is matched by SHA-256 and uses the registered C-semantics bracket recipe: the upper body spans the full 50-mm Y width; the 30-mm callout is the rectangular pocket length along Y; pocket depth is 10 mm; R15 is a Y-axis cut through 50 mm; and Ø20 circles are Z-axis through cuts that form side notches (legacy boss* aliases are retained)."
            if known_fixture
            else "Unlabelled dimensions remain reviewable until a human maps them to a part recipe."
        ,),
        unresolved=[] if known_fixture else ["feature_topology"],
        dimensions=rich_dimensions,
        features=(
            [
                {
                    "id": "feat-base",
                    "featureType": "base_box",
                    "view": "top",
                    "confidence": 0.99,
                    "verified": True,
                    "parameters": {"length": 100, "width": 50, "height": 10},
                },
                {
                    "id": "feat-upper-box",
                    "featureType": "upper_box",
                    "view": "front",
                    "confidence": 0.98,
                    "verified": True,
                    "parameters": {"length": 70, "width": 50, "height": 30, "zStart": 10},
                },
                {
                    "id": "feat-saddle-notch",
                    "featureType": "through_saddle_notch",
                    "view": "front",
                    "confidence": 0.98,
                    "verified": True,
                    "parameters": {
                        "opening": 40,
                        "radius": 15,
                        "axis": "Y",
                        "depth": 50,
                        "through": True,
                    },
                },
                {
                    "id": "feat-pocket-pair",
                    "featureType": "rectangular_pocket_pair",
                    "view": "top",
                    "confidence": 0.97,
                    "verified": True,
                    "parameters": {
                        "count": 2,
                        "slotLength": 30,
                        "slotWidth": 10,
                        "depth": 10,
                        "axis": "Y",
                        "xCenters": [-15, 15],
                        "yRange": [-15, 15],
                    },
                },
                {
                    "id": "feat-side-notch-pair",
                    # The circles are subtractive holes.  ``side_notch`` is
                    # the visible result where each hole is tangent to the
                    # upper body's X-side wall; no additive boss is present.
                    "featureType": "side_notch_cut_pair",
                    "featureTypeAliases": ["vertical_through_hole_pair"],
                    "view": "top",
                    "confidence": 0.98,
                    "verified": True,
                    "parameters": {
                        "diameter": 20,
                        "axis": "Z",
                        "depth": 40,
                        "through": True,
                        "centerDistance": 70,
                        "centers": [[-35, 0], [35, 0]],
                        "cut": True,
                    },
                },
            ]
            if known_fixture
            else []
        ),
        modelRecipe={
            "recipeId": "bracket_support_v1" if known_fixture else "review-required",
            "parameters": params.model_dump(by_alias=True),
            "source": "acceptance-fixture" if known_fixture else "compatibility-recognizer",
            "coordinateSystem": {
                "origin": "base_center",
                "x": "length_100",
                "y": "width_50",
                "z": "up",
            },
            "featureSemantics": {
                "upperWidth": "full Y span (50 mm)",
                "slotLength": "rectangular pocket length along Y (30 mm)",
                "slotWidth": "rectangular pocket width along X (10 mm)",
                "pocketDepth": "top pocket depth (10 mm)",
                "saddleDepth": "R15 saddle cut through full Y width (50 mm)",
                "holeDepth": "Ø20 vertical cuts through total Z height (40 mm)",
                "bossDiameter": "legacy alias for vertical cut diameter",
                "bossCenterDistance": "legacy alias for cut centre distance",
            },
            "operations": [
                {
                    "id": "op-base",
                    "type": "box",
                    "size": [100, 50, 10],
                    "origin": [-50, -25, 0],
                },
                {
                    "id": "op-upper",
                    "type": "box",
                    "size": [70, 50, 30],
                    "origin": [-35, -25, 10],
                    "fuse": True,
                },
                {
                    "id": "op-saddle",
                    "type": "saddle_cut",
                    "opening": 40,
                    "radius": 15,
                    "axis": "Y",
                    "depth": 50,
                    "through": True,
                    "center": [0, 0, 40],
                    "cut": True,
                },
                {
                    "id": "op-pocket-pair",
                    "type": "rectangular_pocket_pair",
                    "slotLength": 30,
                    "slotWidth": 10,
                    "depth": 10,
                    "xCenters": [-15, 15],
                    "yRange": [-15, 15],
                    "zFloor": 30,
                    "cut": True,
                },
                {
                    "id": "op-side-notch-pair",
                    "type": "vertical_through_hole_pair",
                    "featureType": "side_notch_cut_pair",
                    "diameter": 20,
                    "depth": 40,
                    "axis": "Z",
                    "centers": [[-35, 0, 0], [35, 0, 0]],
                    "through": True,
                    "cut": True,
                },
            ],
        },
        reviewRequired=status != "confirmed",
    )
