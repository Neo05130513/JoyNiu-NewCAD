"""Engineering-drawing OCR and evidence services.

OCR is treated as an evidence-producing step, not as an unreviewable number
generator.  A recognition result carries the source hash, bounding boxes,
confidence and assumptions that led to every dimension.  The included
acceptance drawing is registered as a deterministic fixture so CI and local
demo runs produce the same result even when a machine does not have an OCR
engine installed.  Its recipe follows the four-view C interpretation: the
upper body is full-width, the 30-mm callout is a Y-oriented pocket length, and
the Ø20 circles are subtractive vertical through holes/side notches.  Unknown
drawings can be sent to the optional Tesseract adapter; those results remain
``needs_review`` while the AI candidate is being edited. ``needs_review`` is a
pending-candidate state, not a terminal reviewer-only gate: an explicit
customer/designer acceptance records the parameter snapshot before geometry
generation.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .platform import ValidationError


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# These fields describe the minimum supported bracket recipe.  Pydantic's
# ``BracketParameters`` intentionally has defaults for compatibility with old
# clients, but an unknown drawing must not inherit those defaults at the
# customer-confirmation boundary.
_REQUIRED_BRACKET_CANDIDATE_FIELDS = frozenset(
    {
        "baseLength", "baseWidth", "baseThickness", "upperLength", "upperWidth",
        "upperHeight", "totalHeight", "notchOpening", "notchRadius", "slotLength",
        "slotWidth", "pocketDepth", "bossDiameter", "bossCenterDistance",
    }
)
_REQUIRED_SPLIT_CLAMP_CANDIDATE_FIELDS = frozenset(
    {
        "baseLength", "baseWidth", "baseThickness", "baseMainDepth",
        "frontTongueWidth", "rearBridgeWidth", "totalHeight",
        "pedestalOuterRadius", "pedestalCenterFromRear", "pedestalHeight",
        "rearClampRise", "boreDiameter", "boreFloorZ", "splitWidth",
        "mountHoleCount", "mountHoleDiameter", "mountHoleCenterDistance",
        "mountHoleCenterFromRear",
        "crossHoleDiameter", "crossHoleCenterZ", "ribHeight", "ribThickness",
        "outerCornerRadius", "neckConcaveRadius", "neckConvexRadius",
    }
)
# SHA-256 of the acceptance drawing supplied with the product brief.  The
# registry also stores this value in JSON; keeping the constant here makes it
# easy for callers to identify the canonical fixture without opening the file.
_ACCEPTANCE_SHA256 = "ea337023af0158438f9cea2482e8e2d6d4052fc04e7e7f4265956824478c4366"
ACCEPTANCE_DRAWING_SHA256 = _ACCEPTANCE_SHA256


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _clamp_confidence(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(1.0, number))


def _image_dimensions(payload: bytes) -> tuple[int | None, int | None]:
    """Read image dimensions without PIL (PNG/JPEG/GIF/WebP are supported)."""

    if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
        import struct

        return struct.unpack(">II", payload[16:24])
    if payload.startswith((b"GIF87a", b"GIF89a")) and len(payload) >= 10:
        import struct

        return struct.unpack("<HH", payload[6:10])
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        # VP8X stores dimensions as 24-bit little-endian values + 1.
        if payload[12:16] == b"WEBP" and len(payload) >= 30 and payload[16:20] == b"VP8X":
            width = 1 + int.from_bytes(payload[24:27], "little")
            height = 1 + int.from_bytes(payload[27:30], "little")
            return width, height
    if len(payload) >= 4 and payload[:2] == b"\xff\xd8":
        # Walk JPEG markers until a Start Of Frame marker is found.
        import struct

        offset = 2
        while offset + 4 <= len(payload):
            if payload[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(payload) and payload[offset] == 0xFF:
                offset += 1
            if offset >= len(payload):
                break
            marker = payload[offset]
            offset += 1
            if marker in (0xD8, 0xD9):
                continue
            if offset + 2 > len(payload):
                break
            segment_length = struct.unpack(">H", payload[offset : offset + 2])[0]
            if segment_length < 2 or offset + segment_length > len(payload):
                break
            if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0)):
                if segment_length >= 7:
                    height, width = struct.unpack(">HH", payload[offset + 3 : offset + 7])
                    return width, height
            offset += segment_length
    return None, None


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x: float
    y: float
    width: float
    height: float
    coordinate_space: str = "pixel"
    page: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DimensionEvidence:
    id: str
    field: str
    value: float
    unit: str
    kind: str
    source_text: str
    confidence: float
    bbox: BoundingBox | None = None
    view: str = "unknown"
    tolerance: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bbox is not None:
            data["bbox"] = self.bbox.to_dict()
        return data


@dataclass(frozen=True, slots=True)
class FeatureEvidence:
    id: str
    feature_type: str
    parameters: dict[str, Any]
    confidence: float
    evidence_ids: tuple[str, ...] = ()
    view: str = "unknown"
    verified: bool = False
    # A feature can expose a descriptive semantic name and a compatibility
    # alias without creating two physical features in the recipe.  This is
    # used by the acceptance bracket where the Ø20 cut is both a
    # ``side_notch_cut_pair`` and a ``vertical_through_hole_pair``.  Imported
    # legacy fixtures may still carry an old label for migration, but the
    # calibrated fixture never emits it as its primary feature type.
    feature_type_aliases: tuple[str, ...] = ()
    legacy_feature_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_ids"] = list(self.evidence_ids)
        data["feature_type_aliases"] = list(self.feature_type_aliases)
        if self.legacy_feature_type is None:
            data.pop("legacy_feature_type", None)
        return data


@dataclass(frozen=True, slots=True)
class DrawingRecognition:
    id: str
    status: str
    part_type: str
    source_filename: str
    source_sha256: str
    image_width: int | None
    image_height: int | None
    confidence: float
    dimensions: tuple[DimensionEvidence, ...]
    features: tuple[FeatureEvidence, ...]
    model_recipe: dict[str, Any]
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]
    unresolved: tuple[str, ...]
    ocr_text: str
    engine: str
    fixture_id: str | None = None
    created_at: str = ""
    # Explicit human/customer acceptance audit fields.  These are optional for
    # backwards compatibility with persisted/constructed recognition records.
    confirmation_type: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    # AI may produce a partial candidate before a durable recipe exists. Keep
    # it separate from ``model_recipe.parameters`` so clients can display and
    # edit the proposal without accidentally treating it as production truth.
    candidate_parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Keep the platform evidence envelope compatible with the lightweight
        # drawing endpoint and the browser client.  ``modelRecipe`` remains
        # the auditable source of truth, while a top-level ``parameters``
        # alias makes direct AI-conversation consumers interoperable without
        # knowing which recognition adapter produced the result.
        parameters = self.model_recipe.get("parameters", {})
        return {
            "id": self.id,
            "status": self.status,
            "partType": self.part_type,
            "sourceFilename": self.source_filename,
            "sourceSha256": self.source_sha256,
            "imageWidth": self.image_width,
            "imageHeight": self.image_height,
            "confidence": self.confidence,
            "dimensions": [item.to_dict() for item in self.dimensions],
            "features": [item.to_dict() for item in self.features],
            "modelRecipe": self.model_recipe,
            "parameters": dict(parameters) if isinstance(parameters, Mapping) else {},
            "candidateParameters": dict(self.candidate_parameters),
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
            "unresolved": list(self.unresolved),
            "ocrText": self.ocr_text,
            "engine": self.engine,
            "fixtureId": self.fixture_id,
            "createdAt": self.created_at,
            "confirmationType": self.confirmation_type,
            "confirmedBy": self.confirmed_by,
            "confirmedAt": self.confirmed_at,
            "reviewRequired": self.status == "needs_review",
        }

    # The geometry API consumes the same aliases as the browser prototype.
    def to_geometry_request(self, *, formats: Sequence[str] = ("step", "glb")) -> dict[str, Any]:
        parameters = dict(self.model_recipe.get("parameters", {}))
        return {
            "parameters": parameters,
            "formats": list(formats),
            "sourceDrawingId": self.id,
            "requireCadQuery": True,
        }


@dataclass(frozen=True, slots=True)
class OCRProviderResult:
    text: str
    tokens: tuple[dict[str, Any], ...] = ()
    engine: str = "unknown"
    confidence: float = 0.0
    warnings: tuple[str, ...] = ()


class OCRProvider(Protocol):
    def recognize(self, image_bytes: bytes, filename: str) -> OCRProviderResult:
        ...


class TesseractOCRProvider:
    """Optional local OCR adapter.

    It is intentionally opt-in and never silently upgrades an uncertain result
    to a confirmed CAD model.  Install ``tesseract`` and pass this provider to
    :class:`OCRService` (or set ``JOYNIU_OCR_ENGINE=tesseract``).
    """

    def __init__(self, executable: str | None = None, language: str = "eng") -> None:
        self.executable = executable or shutil.which("tesseract")
        self.language = language
        if not self.executable:
            raise ValidationError("tesseract executable was not found")

    def recognize(self, image_bytes: bytes, filename: str = "drawing.png") -> OCRProviderResult:
        suffix = Path(filename).suffix or ".png"
        with tempfile.TemporaryDirectory(prefix="joyniu-ocr-") as directory:
            image_path = Path(directory) / f"source{suffix}"
            image_path.write_bytes(image_bytes)
            command = [
                self.executable,
                str(image_path),
                "stdout",
                "--psm",
                "11",
                "-l",
                self.language,
                "tsv",
            ]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return OCRProviderResult(
                    text="",
                    engine="tesseract",
                    warnings=(f"OCR provider unavailable: {exc}",),
                )
            if result.returncode != 0:
                return OCRProviderResult(
                    text=result.stderr.strip(),
                    engine="tesseract",
                    warnings=("tesseract returned a non-zero status",),
                )
            tokens: list[dict[str, Any]] = []
            text_parts: list[str] = []
            reader = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
            for row in reader:
                value = (row.get("text") or "").strip()
                if not value:
                    continue
                text_parts.append(value)
                try:
                    confidence = float(row.get("conf") or 0) / 100.0
                    bbox = {
                        "x": float(row.get("left") or 0),
                        "y": float(row.get("top") or 0),
                        "width": float(row.get("width") or 0),
                        "height": float(row.get("height") or 0),
                    }
                except ValueError:
                    confidence, bbox = 0.0, None
                tokens.append({"text": value, "confidence": confidence, "bbox": bbox})
            confidences = [float(token["confidence"]) for token in tokens]
            return OCRProviderResult(
                text=" ".join(text_parts),
                tokens=tuple(tokens),
                engine="tesseract",
                confidence=sum(confidences) / len(confidences) if confidences else 0.0,
            )


def _dimension_field(value: float, marker: str = "") -> tuple[str, str]:
    marker = marker.upper()
    if marker in {"R", "RAD", "RADIUS"}:
        return "notch_radius", "radius"
    if marker in {"Ø", "Φ", "D", "DIA", "DIAMETER"}:
        # Keep the historical field name for API compatibility.  In the
        # calibrated bracket recipe this diameter belongs to subtractive
        # vertical holes/side notches, not additive bosses.
        return "boss_diameter", "diameter"
    # Generic OCR fallback.  The caller can remap repeated values by view.
    return "unclassified", "linear"


_DIMENSION_PATTERN = re.compile(
    r"(?P<marker>[RrØøΦφ]|diam(?:eter)?|dia)?\s*"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>mm|毫米|英寸|in)?",
    re.IGNORECASE,
)


def parse_dimension_text(
    text: str,
    *,
    tokens: Sequence[Mapping[str, Any]] = (),
    default_unit: str = "mm",
) -> tuple[DimensionEvidence, ...]:
    """Parse explicit dimension tokens while preserving low confidence.

    The parser intentionally does not infer a semantic field for unlabelled
    numbers.  Such dimensions are exposed as ``unclassified`` and remain in
    review, preventing a plausible-looking but incorrect solid.
    """

    results: list[DimensionEvidence] = []
    token_index = 0
    for match in _DIMENSION_PATTERN.finditer(text or ""):
        marker = match.group("marker") or ""
        raw_value = match.group("value").replace(",", ".")
        try:
            value = float(raw_value)
        except ValueError:
            continue
        unit = match.group("unit") or default_unit
        if unit in {"毫米"}:
            unit = "mm"
        elif unit in {"英寸", "in"}:
            unit = "in"
        field_name, kind = _dimension_field(value, marker)
        bbox = None
        confidence = 0.45
        if token_index < len(tokens):
            token = tokens[token_index]
            token_index += 1
            confidence = _clamp_confidence(token.get("confidence"), confidence)
            raw_bbox = token.get("bbox")
            if isinstance(raw_bbox, Mapping):
                try:
                    bbox = BoundingBox(
                        float(raw_bbox.get("x", 0)),
                        float(raw_bbox.get("y", 0)),
                        float(raw_bbox.get("width", 0)),
                        float(raw_bbox.get("height", 0)),
                    )
                except (TypeError, ValueError):
                    bbox = None
        results.append(
            DimensionEvidence(
                id=_new_id("dim"),
                field=field_name,
                value=value,
                unit=unit,
                kind=kind,
                source_text=match.group(0).strip(),
                confidence=confidence,
                bbox=bbox,
            )
        )
    return tuple(results)


def _load_fixture_documents(directory: Path = _FIXTURE_DIR) -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return fixtures
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fixture_id = str(data.get("fixture_id") or path.stem)
        fixtures[fixture_id] = data
        for alias in data.get("aliases", []):
            fixtures[str(alias)] = data
    return fixtures


class OCRService:
    """Evidence-first recognizer with deterministic fixtures and live providers."""

    def __init__(
        self,
        *,
        fixture_directory: str | Path | None = None,
        provider: OCRProvider | None = None,
        enable_live_ocr: bool | None = None,
        allow_unverified_fixture: bool | None = None,
    ) -> None:
        self.fixture_directory = Path(fixture_directory) if fixture_directory else _FIXTURE_DIR
        self.fixtures = _load_fixture_documents(self.fixture_directory)
        self.provider = provider
        if enable_live_ocr is None:
            enable_live_ocr = os.getenv("JOYNIU_OCR_ENGINE", "fixture").casefold() == "tesseract"
        self.enable_live_ocr = bool(enable_live_ocr)
        if allow_unverified_fixture is None:
            allow_unverified_fixture = os.getenv(
                "JOYNIU_ALLOW_UNVERIFIED_FIXTURE", "0"
            ).casefold() in {"1", "true", "yes", "on"}
        # This switch is intended only for deterministic fixture-only smoke
        # tests.  Production/API instances leave it disabled so a caller
        # cannot attach a known fixture id to arbitrary bytes.
        self.allow_unverified_fixture = bool(allow_unverified_fixture)
        if self.provider is None and self.enable_live_ocr:
            try:
                self.provider = TesseractOCRProvider()
            except ValidationError:
                self.provider = None

    def list_fixtures(self) -> list[dict[str, Any]]:
        seen: set[int] = set()
        output: list[dict[str, Any]] = []
        for fixture in self.fixtures.values():
            marker = id(fixture)
            if marker in seen:
                continue
            seen.add(marker)
            output.append(
                {
                    "fixtureId": fixture.get("fixture_id"),
                    "description": fixture.get("description", ""),
                    "sourceSha256": fixture.get("source_sha256"),
                    "verified": bool(fixture.get("verified", False)),
                }
            )
        return output

    def _fixture_for(self, source_sha256: str, fixture_id: str | None) -> dict[str, Any] | None:
        if fixture_id:
            fixture = self.fixtures.get(fixture_id)
            if fixture is None:
                return None
            expected = str(fixture.get("source_sha256") or "")
            # A fixture without a registered source digest is not a trusted
            # calibration record.  Requiring the digest here prevents a
            # caller from selecting a verified recipe for arbitrary bytes just
            # because it knows the fixture id.  The explicit unverified switch
            # remains available to deterministic test harnesses.
            if (not expected or expected != source_sha256) and not self.allow_unverified_fixture:
                return None
            return fixture
        for fixture in self.fixtures.values():
            if fixture.get("source_sha256") == source_sha256:
                return fixture
        return None

    @staticmethod
    def _fixture_dimensions(fixture: Mapping[str, Any]) -> tuple[DimensionEvidence, ...]:
        dimensions: list[DimensionEvidence] = []
        for raw in fixture.get("dimensions", []):
            bbox_data = raw.get("bbox")
            bbox = BoundingBox(**bbox_data) if isinstance(bbox_data, Mapping) else None
            dimensions.append(
                DimensionEvidence(
                    id=str(raw.get("id") or _new_id("dim")),
                    field=str(raw.get("field", "unclassified")),
                    value=float(raw["value"]),
                    unit=str(raw.get("unit", "mm")),
                    kind=str(raw.get("kind", "linear")),
                    source_text=str(raw.get("source_text", raw.get("sourceText", raw["value"]))),
                    confidence=_clamp_confidence(raw.get("confidence"), 1.0),
                    bbox=bbox,
                    view=str(raw.get("view", "unknown")),
                    tolerance=raw.get("tolerance"),
                    verified=bool(raw.get("verified", fixture.get("verified", False))),
                )
            )
        return tuple(dimensions)

    @staticmethod
    def _fixture_features(fixture: Mapping[str, Any]) -> tuple[FeatureEvidence, ...]:
        return tuple(
            FeatureEvidence(
                id=str(raw.get("id") or _new_id("feat")),
                feature_type=str(raw.get("feature_type", raw.get("featureType", "unknown"))),
                parameters=dict(raw.get("parameters", {})),
                confidence=_clamp_confidence(raw.get("confidence"), 1.0),
                evidence_ids=tuple(raw.get("evidence_ids", raw.get("evidenceIds", []))),
                view=str(raw.get("view", "unknown")),
                verified=bool(raw.get("verified", fixture.get("verified", False))),
                feature_type_aliases=tuple(
                    str(item)
                    for item in raw.get(
                        "feature_type_aliases",
                        raw.get("featureTypeAliases", []),
                    )
                ),
                legacy_feature_type=(
                    str(raw.get("legacy_feature_type"))
                    if raw.get("legacy_feature_type") is not None
                    else str(raw.get("legacyFeatureType"))
                    if raw.get("legacyFeatureType") is not None
                    else None
                ),
            )
            for raw in fixture.get("features", [])
        )

    def analyze(
        self,
        image_bytes: bytes | bytearray | memoryview,
        *,
        filename: str = "drawing.png",
        fixture_id: str | None = None,
        provider: OCRProvider | None = None,
    ) -> DrawingRecognition:
        payload = bytes(image_bytes)
        if not payload:
            raise ValidationError("drawing image is empty")
        source_sha256 = hashlib.sha256(payload).hexdigest()
        width, height = _image_dimensions(payload)
        fixture = self._fixture_for(source_sha256, fixture_id)
        if fixture is not None:
            fixture_hash_matches = str(fixture.get("source_sha256") or "") == source_sha256
            dimensions = self._fixture_dimensions(fixture)
            features = self._fixture_features(fixture)
            warnings = tuple(str(item) for item in fixture.get("warnings", []))
            unresolved = tuple(str(item) for item in fixture.get("unresolved", []))
            if not fixture_hash_matches:
                warnings += (
                    "Unverified fixture override is enabled; this result is for smoke testing only.",
                )
            if fixture.get("verified", False):
                status = "confirmed" if not unresolved else "needs_review"
            else:
                status = "needs_review"
            return DrawingRecognition(
                id=_new_id("drw"),
                status=status,
                part_type=str(fixture.get("part_type", "bracket")),
                source_filename=filename,
                source_sha256=source_sha256,
                image_width=int(fixture.get("image_width") or width) if (fixture.get("image_width") or width) else None,
                image_height=int(fixture.get("image_height") or height) if (fixture.get("image_height") or height) else None,
                confidence=_clamp_confidence(fixture.get("confidence"), 0.98),
                dimensions=dimensions,
                features=features,
                model_recipe=dict(fixture.get("model_recipe", {})),
                assumptions=tuple(str(item) for item in fixture.get("assumptions", [])),
                warnings=warnings,
                unresolved=unresolved,
                ocr_text=str(fixture.get("ocr_text", "")),
                engine=("deterministic-fixture" if fixture_hash_matches else "deterministic-fixture-unverified"),
                fixture_id=str(fixture.get("fixture_id")),
                created_at=str(fixture.get("created_at", "")),
            )

        selected_provider = provider or self.provider
        if selected_provider is not None and self.enable_live_ocr:
            provider_result = selected_provider.recognize(payload, filename)
            dimensions = parse_dimension_text(
                provider_result.text, tokens=provider_result.tokens, default_unit="mm"
            )
            confidence = _clamp_confidence(provider_result.confidence, 0.0)
            warnings = tuple(provider_result.warnings)
            if not dimensions:
                warnings += ("No explicit engineering dimensions were detected",)
            unresolved = tuple(
                item.field for item in dimensions if item.field == "unclassified"
            )
            return DrawingRecognition(
                id=_new_id("drw"),
                status="needs_review",
                part_type="unknown",
                source_filename=filename,
                source_sha256=source_sha256,
                image_width=width,
                image_height=height,
                confidence=confidence,
                dimensions=dimensions,
                features=(),
                model_recipe={"parameters": {}, "source": "live-ocr"},
                assumptions=("Live OCR output must be mapped to a known part recipe before modeling",),
                warnings=warnings,
                unresolved=unresolved,
                ocr_text=provider_result.text,
                engine=provider_result.engine,
                fixture_id=None,
            )

        return DrawingRecognition(
            id=_new_id("drw"),
            status="needs_review",
            part_type="unknown",
            source_filename=filename,
            source_sha256=source_sha256,
            image_width=width,
            image_height=height,
            confidence=0.0,
            dimensions=(),
            features=(),
            model_recipe={"parameters": {}, "source": "unrecognised"},
            assumptions=(),
            warnings=("No deterministic fixture or live OCR provider was available",),
            unresolved=("part_type", "dimensions", "feature_topology"),
            ocr_text="",
            engine="none",
            fixture_id=None,
        )

    def confirm(
        self,
        recognition: DrawingRecognition,
        *,
        reviewer_id: str,
        parameter_overrides: Mapping[str, Any] | None = None,
        confirmation_type: str = "reviewer",
        confirmed_at: str | None = None,
    ) -> DrawingRecognition:
        """Return a confirmed copy after an explicit human confirmation.

        ``reviewer_id`` is the durable actor id for compatibility; callers may
        identify it as a customer, designer or formal reviewer through
        ``confirmation_type``. Overrides are recorded in ``model_recipe`` and
        do not mutate the original recognition object, preserving the evidence
        audit trail.
        """

        if not reviewer_id.strip():
            raise ValidationError("reviewer_id is required to confirm a drawing")
        recipe = dict(recognition.model_recipe)
        raw_parameters = recipe.get("parameters", {})
        if not isinstance(raw_parameters, Mapping):
            raise ValidationError("drawing model recipe parameters must be an object")
        # A pending AI analysis can carry partial fields outside the durable
        # recipe.  Treat them as the starting candidate for confirmation while
        # preserving the distinction in the response/audit envelope.
        if not raw_parameters and recognition.candidate_parameters:
            raw_parameters = recognition.candidate_parameters
        overrides = parameter_overrides or {}
        if not isinstance(overrides, Mapping):
            raise ValidationError("parameter_overrides must be an object")
        # Unknown/low-confidence candidates are accepted only when the caller
        # supplies an explicit parameter candidate.  A known AI recipe keeps
        # its own topology instead of being coerced into the legacy bracket.
        was_unknown = recognition.part_type == "unknown"
        # An AI conversation may already have merged a candidate patch into
        # the recognition recipe.  Accept can therefore omit overrides only
        # when a non-empty candidate is present; a completely unknown drawing
        # still requires explicit human parameter input.
        if was_unknown and not overrides and not raw_parameters:
            raise ValidationError("unknown drawing requires parameterOverrides before confirmation")
        # Do not silently drop misspelled reviewer overrides.  Pydantic's
        # compatibility models intentionally ignore unknown fields for input
        # forwards-compatibility, but confirmation is an authorization
        # boundary: an operator must know every requested value was actually
        # applied to the recipe.
        recipe_id = str(recipe.get("recipeId", recipe.get("recipe_id", "")) or "")
        if recognition.part_type == "split_clamp_support" or (
            was_unknown and recipe_id == "split_clamp_support_v1"
        ):
            effective_part_type = "split_clamp_support"
            if recipe_id != "split_clamp_support_v1":
                raise ValidationError(
                    "split_clamp_support candidate requires recipeId split_clamp_support_v1"
                )
        elif recognition.part_type in {"unknown", "bracket"}:
            # Preserve the historical unknown → bracket confirmation bridge.
            effective_part_type = "bracket"
            if recipe_id and recipe_id not in {"review-required", "bracket_support_v1"}:
                raise ValidationError("drawing part_type and recipeId do not match")
        else:
            raise ValidationError(f"unsupported drawing part_type: {recognition.part_type}")
        if effective_part_type == "bracket":
            try:
                from .schemas import BracketParameters

                allowed = set(BracketParameters.model_fields)
                allowed.update(
                    field.alias
                    for field in BracketParameters.model_fields.values()
                    if getattr(field, "alias", None)
                )
            except Exception:  # pragma: no cover - dependency-free fallback
                allowed = {
                    "baseLength", "baseWidth", "baseThickness", "upperLength",
                    "upperWidth", "upperHeight", "totalHeight", "notchOpening",
                    "notchRadius", "slotLength", "slotWidth", "pocketDepth",
                    "saddleDepth", "holeDepth", "holeThrough", "bossDiameter",
                    "bossCenterDistance", "bossHeight",
                    "material", "units",
                }
            unknown = sorted(
                str(key)
                for key in set(raw_parameters).union(overrides)
                if str(key) not in allowed
            )
            if unknown:
                raise ValidationError(
                    "unknown parameter override(s): " + ", ".join(unknown)
                )
        else:
            from .schemas import SplitClampSupportParameters

            allowed = set(SplitClampSupportParameters.model_fields)
            allowed.update(
                field.alias
                for field in SplitClampSupportParameters.model_fields.values()
                if getattr(field, "alias", None)
            )
            unknown = sorted(
                str(key)
                for key in set(raw_parameters).union(overrides)
                if str(key) not in allowed
            )
            if unknown:
                raise ValidationError(
                    "unknown parameter override(s): " + ", ".join(unknown)
                )
        parameters = dict(raw_parameters)
        parameters.update(dict(overrides))

        if was_unknown and effective_part_type == "bracket":
            aliases = {
                "base_length": "baseLength", "base_width": "baseWidth", "base_thickness": "baseThickness",
                "upper_length": "upperLength", "upper_width": "upperWidth", "upper_height": "upperHeight",
                "total_height": "totalHeight", "notch_opening": "notchOpening", "notch_radius": "notchRadius",
                "slot_length": "slotLength", "slot_width": "slotWidth", "pocket_depth": "pocketDepth",
                "boss_diameter": "bossDiameter", "boss_center_distance": "bossCenterDistance",
            }
            supplied = {aliases.get(str(key), str(key)) for key, value in parameters.items() if value is not None}
            missing = sorted(_REQUIRED_BRACKET_CANDIDATE_FIELDS - supplied)
            if missing:
                raise ValidationError(
                    "unknown drawing candidate is incomplete; provide: " + ", ".join(missing)
                )
        if effective_part_type == "split_clamp_support":
            from .schemas import SplitClampSupportParameters

            aliases = {
                name: field.alias or name
                for name, field in SplitClampSupportParameters.model_fields.items()
            }
            supplied = {
                aliases.get(str(key), str(key))
                for key, value in parameters.items()
                if value is not None
            }
            missing = sorted(_REQUIRED_SPLIT_CLAMP_CANDIDATE_FIELDS - supplied)
            if missing:
                raise ValidationError(
                    "split clamp candidate is incomplete; provide: " + ", ".join(missing)
                )

        # Bracket confirmations are the hand-off into the geometry kernel.  Do
        # the same schema and non-throwing geometry validation here as the
        # generation endpoint, so a reviewer cannot accidentally approve a
        # malformed recipe and leave a later request to fail with a 500.
        if effective_part_type == "bracket":
            try:
                from .geometry import validate_bracket
                from .schemas import BracketParameters

                validated_parameters = BracketParameters.model_validate(parameters)
                validation = validate_bracket(validated_parameters)
            except Exception as exc:
                raise ValidationError("drawing recipe parameters are invalid") from exc
            if not validation.valid:
                raise ValidationError("drawing recipe failed geometry validation")
            parameters = validated_parameters.model_dump(by_alias=True)
            recipe["recipeId"] = "bracket_support_v1"
        else:
            try:
                from .model_recipes import parse_model_parameters, validate_model_recipe

                validated_parameters = parse_model_parameters(
                    "split_clamp_support_v1",
                    parameters,
                )
                validation = validate_model_recipe(
                    "split_clamp_support_v1",
                    validated_parameters,
                )
            except Exception as exc:
                raise ValidationError("drawing recipe parameters are invalid") from exc
            if not validation.get("valid"):
                raise ValidationError("drawing recipe failed geometry validation")
            parameters = validated_parameters.model_dump(mode="json", by_alias=True)
            recipe["recipeId"] = "split_clamp_support_v1"
        recipe["parameters"] = parameters
        recipe["confirmed_by"] = reviewer_id
        recipe["confirmation_type"] = str(confirmation_type or "reviewer")
        recipe["confirmed_at"] = confirmed_at or datetime.now(timezone.utc).isoformat()
        # Keep camelCase aliases in the durable recipe for browser/PDM
        # consumers while retaining the snake_case keys used by older workers.
        recipe["confirmedBy"] = reviewer_id
        recipe["confirmationType"] = recipe["confirmation_type"]
        recipe["confirmedAt"] = recipe["confirmed_at"]
        # A confirmation never manufactures missing evidence; callers can still
        # inspect warnings/unresolved fields before submitting it for modeling.
        # Explicit parameter overrides resolve the unknown candidate.  Known
        # recognitions retain unresolved evidence so the audit envelope still
        # exposes what the recognizer could not infer.
        unresolved = () if was_unknown else tuple(recognition.unresolved)
        return DrawingRecognition(
            id=recognition.id,
            # The explicit human action is the authorization boundary.  Keep
            # unresolved evidence in the audit envelope for known candidates,
            # but do not require a reviewer role or a second confirmation once
            # the candidate has been accepted.
            status="confirmed",
            part_type=effective_part_type,
            source_filename=recognition.source_filename,
            source_sha256=recognition.source_sha256,
            image_width=recognition.image_width,
            image_height=recognition.image_height,
            confidence=recognition.confidence,
            dimensions=recognition.dimensions,
            features=recognition.features,
            model_recipe=recipe,
            assumptions=recognition.assumptions,
            warnings=recognition.warnings,
            unresolved=unresolved,
            ocr_text=recognition.ocr_text,
            engine=recognition.engine,
            fixture_id=recognition.fixture_id,
            created_at=recognition.created_at,
            confirmation_type=str(confirmation_type or "reviewer"),
            confirmed_by=reviewer_id,
            confirmed_at=confirmed_at or recipe["confirmed_at"],
            candidate_parameters=dict(parameters),
        )


def recognize_drawing_bytes(
    data: bytes,
    *,
    filename: str = "drawing.png",
    fixture_id: str | None = None,
    provider: OCRProvider | None = None,
    enable_live_ocr: bool | None = None,
    hints: Mapping[str, Any] | None = None,
) -> DrawingRecognition:
    """Functional convenience wrapper for scripts and route adapters.

    ``hints`` uses either camelCase or snake_case parameter names.  Hints are
    recorded as explicit reviewer overrides; they never alter the source hash.
    """

    service = OCRService(provider=provider, enable_live_ocr=enable_live_ocr)
    result = service.analyze(data, filename=filename, fixture_id=fixture_id)
    if hints:
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
        overrides = {aliases.get(key, key): value for key, value in hints.items()}
        if result.part_type == "unknown":
            # Hints can make a live/unrecognised profile useful for a reviewer,
            # but they must not be treated as a confirmation of topology.
            recipe = dict(result.model_recipe)
            params = dict(recipe.get("parameters", {}))
            params.update(overrides)
            recipe["parameters"] = params
            result = DrawingRecognition(
                id=result.id,
                status=result.status,
                part_type=result.part_type,
                source_filename=result.source_filename,
                source_sha256=result.source_sha256,
                image_width=result.image_width,
                image_height=result.image_height,
                confidence=result.confidence,
                dimensions=result.dimensions,
                features=result.features,
                model_recipe=recipe,
                assumptions=result.assumptions,
                warnings=result.warnings + ("Client hints supplied; topology still requires human review",),
                unresolved=result.unresolved,
                ocr_text=result.ocr_text,
                engine=result.engine,
                fixture_id=result.fixture_id,
                created_at=result.created_at,
                candidate_parameters=dict(params),
            )
        else:
            result = service.confirm(result, reviewer_id="client-hint", parameter_overrides=overrides)
    return result


__all__ = [
    "BoundingBox",
    "ACCEPTANCE_DRAWING_SHA256",
    "DimensionEvidence",
    "DrawingRecognition",
    "FeatureEvidence",
    "OCRProvider",
    "OCRProviderResult",
    "OCRService",
    "TesseractOCRProvider",
    "recognize_drawing_bytes",
    "parse_dimension_text",
]
