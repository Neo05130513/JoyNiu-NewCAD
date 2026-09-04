"""Pydantic contracts shared by the drawing and geometry endpoints.

The browser prototype uses camelCase while Python code uses snake_case.  Every
model therefore accepts both spellings and serializes to camelCase at the API
boundary.  Geometry validity deliberately lives in ``geometry.validate`` so a
bad drawing can return a complete, auditable report instead of stopping at the
first Pydantic error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        serialize_by_alias=True,
    )


class BracketParameters(ApiModel):
    """Dimensions of the acceptance-test bracket, in millimetres."""

    base_length: float = Field(100.0, alias="baseLength")
    base_width: float = Field(50.0, alias="baseWidth")
    base_thickness: float = Field(10.0, alias="baseThickness")
    upper_length: float = Field(70.0, alias="upperLength")
    # The calibrated drawing uses the full 50 mm base width for the upper
    # body.  Earlier revisions treated the right-view 30 mm callout as this
    # field; that value is actually the length of the two shallow pockets.
    upper_width: float = Field(50.0, alias="upperWidth")
    upper_height: float = Field(30.0, alias="upperHeight")
    total_height: float = Field(40.0, alias="totalHeight")
    notch_opening: float = Field(40.0, alias="notchOpening")
    notch_radius: float = Field(15.0, alias="notchRadius")
    boss_diameter: float = Field(20.0, alias="bossDiameter")
    boss_center_distance: float = Field(70.0, alias="bossCenterDistance")
    # ``boss*`` names remain in the wire contract for older clients.  In the
    # current drawing recipe the circles are subtractive side holes, not
    # additive bosses.  The explicit fields below make the new semantics
    # unambiguous while keeping old payloads accepted.
    boss_height: float | None = Field(None, alias="bossHeight")
    slot_length: float = Field(30.0, alias="slotLength")
    slot_width: float = Field(10.0, alias="slotWidth")
    pocket_depth: float = Field(10.0, alias="pocketDepth")
    saddle_depth: float | None = Field(None, alias="saddleDepth")
    hole_depth: float | None = Field(None, alias="holeDepth")
    hole_through: bool = Field(True, alias="holeThrough")
    material: str = "45# 钢"
    units: Literal["mm"] = "mm"

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_right_view_width(cls, value: Any) -> Any:
        """Translate the pre-C recipe wire payload without changing new data.

        Early clients copied the right-view ``30`` callout into
        ``upperWidth``.  In the calibrated drawing that callout is the Y
        length of the shallow pockets; the upper body spans the full 50 mm
        base width.  New clients send explicit slot fields and are left
        untouched.  This small migration keeps saved/API payloads from the
        prototype from regenerating the old, visibly incorrect solid while
        still allowing an explicit custom 30 mm upper width when the new
        recipe fields are present.
        """

        if not isinstance(value, dict):
            return value
        semantic_fields = {
            "slotLength", "slot_length", "slotWidth", "slot_width",
            "pocketDepth", "pocket_depth", "saddleDepth", "saddle_depth",
            "holeDepth", "hole_depth", "holeThrough", "hole_through",
        }
        if semantic_fields.intersection(value):
            return value
        raw_width = value.get("upperWidth", value.get("upper_width"))
        try:
            legacy_width = math.isclose(float(raw_width), 30.0, abs_tol=1e-9)
        except (TypeError, ValueError):
            legacy_width = False
        if not legacy_width:
            return value
        migrated = dict(value)
        if "upperWidth" in migrated:
            migrated["upperWidth"] = 50.0
        else:
            migrated["upper_width"] = 50.0
        return migrated

    @field_validator(
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
        "boss_height",
        "slot_length",
        "slot_width",
        "pocket_depth",
        "saddle_depth",
        "hole_depth",
    )
    @classmethod
    def finite_number(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("dimension must be finite")
        return value

    @property
    def resolved_boss_height(self) -> float:
        """Return the legacy display value for ``bossHeight``.

        ``bossHeight`` was the height of an additive boss in the first API
        revision (30 mm for the acceptance drawing).  It remains exposed for
        old clients and reports, but it must not drive the C-recipe geometry;
        :attr:`resolved_hole_depth` is the authoritative value for the new
        subtractive side holes.
        """

        return float(self.boss_height if self.boss_height is not None else self.upper_height)

    @property
    def resolved_hole_depth(self) -> float:
        """Depth of the subtractive vertical holes, measured from Z=0."""

        if self.hole_through:
            return float(self.total_height)
        if self.hole_depth is not None:
            return float(self.hole_depth)
        # A legacy ``bossHeight`` value is accepted as a blind-hole depth only
        # when the caller explicitly disables through-hole semantics.
        if self.boss_height is not None:
            return float(self.boss_height)
        return float(self.total_height)

    @property
    def resolved_saddle_depth(self) -> float:
        """Y span of the horizontal saddle cutter (defaults to base width)."""

        return float(self.saddle_depth if self.saddle_depth is not None else self.base_width)


class SplitClampSupportParameters(ApiModel):
    """Dimensions of the split cylindrical clamp support shown in drawing 9.

    This is intentionally a separate recipe from :class:`BracketParameters`.
    Although both parts have a base and circular callouts, ``R33`` is the
    clamp body's outside radius, ``Ø36`` is its central bore, and ``12`` is a
    radial split width; treating those values as the legacy bracket's saddle
    and side-hole fields produces a different solid.
    """

    base_length: float = Field(125.0, alias="baseLength")
    base_width: float = Field(95.0, alias="baseWidth")
    base_thickness: float = Field(15.0, alias="baseThickness")
    base_main_depth: float = Field(80.0, alias="baseMainDepth")
    front_tongue_width: float = Field(80.0, alias="frontTongueWidth")
    rear_bridge_width: float = Field(86.0, alias="rearBridgeWidth")
    total_height: float = Field(75.0, alias="totalHeight")
    pedestal_outer_radius: float = Field(33.0, alias="pedestalOuterRadius")
    pedestal_center_from_rear: float = Field(35.0, alias="pedestalCenterFromRear")
    pedestal_height: float = Field(40.0, alias="pedestalHeight")
    rear_clamp_rise: float = Field(20.0, alias="rearClampRise")
    bore_diameter: float = Field(36.0, alias="boreDiameter")
    bore_floor_z: float = Field(40.0, alias="boreFloorZ")
    split_width: float = Field(12.0, alias="splitWidth")
    mount_hole_count: Literal[2] = Field(2, alias="mountHoleCount")
    mount_hole_diameter: float = Field(12.0, alias="mountHoleDiameter")
    mount_hole_center_distance: float = Field(96.0, alias="mountHoleCenterDistance")
    mount_hole_center_from_rear: float = Field(40.0, alias="mountHoleCenterFromRear")
    cross_hole_diameter: float = Field(12.0, alias="crossHoleDiameter")
    cross_hole_center_z: float = Field(55.0, alias="crossHoleCenterZ")
    rib_height: float = Field(20.0, alias="ribHeight")
    rib_thickness: float = Field(10.0, alias="ribThickness")
    outer_corner_radius: float = Field(8.0, alias="outerCornerRadius")
    neck_concave_radius: float = Field(5.0, alias="neckConcaveRadius")
    neck_convex_radius: float = Field(8.0, alias="neckConvexRadius")
    material: str = "45# 钢"
    units: Literal["mm"] = "mm"

    @field_validator(
        "base_length",
        "base_width",
        "base_thickness",
        "base_main_depth",
        "front_tongue_width",
        "rear_bridge_width",
        "total_height",
        "pedestal_outer_radius",
        "pedestal_center_from_rear",
        "pedestal_height",
        "rear_clamp_rise",
        "bore_diameter",
        "bore_floor_z",
        "split_width",
        "mount_hole_diameter",
        "mount_hole_center_distance",
        "mount_hole_center_from_rear",
        "cross_hole_diameter",
        "cross_hole_center_z",
        "rib_height",
        "rib_thickness",
        "outer_corner_radius",
        "neck_concave_radius",
        "neck_convex_radius",
    )
    @classmethod
    def finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("dimension must be finite")
        return value

    @property
    def pedestal_center_y(self) -> float:
        """Y coordinate when the base rear edge is ``+baseWidth / 2``."""

        return float(self.base_width / 2 - self.pedestal_center_from_rear)

    @property
    def mount_hole_center_y(self) -> float:
        return float(self.base_width / 2 - self.mount_hole_center_from_rear)

    @property
    def lower_clamp_top_z(self) -> float:
        return float(self.base_thickness + self.pedestal_height)


class SteppedTaperedNozzleParameters(ApiModel):
    """Two-solid axisymmetric nozzle and removable insert from DWG ``1(1)``.

    All axial positions use the main part's left face as ``x=0``.  The insert
    remains a separate solid in the candidate assembly; ``M12`` is represented
    by its nominal 12 mm straight clearance only and is never presented as a
    generated thread form.
    """

    main_length: float = Field(98.0, alias="mainLength")
    head_length: float = Field(50.0, alias="headLength")
    neck_length: float = Field(20.0, alias="neckLength")
    head_left_diameter: float = Field(54.25449350717895, alias="headLeftDiameter")
    head_right_diameter: float = Field(56.0, alias="headRightDiameter")
    neck_diameter: float = Field(30.0, alias="neckDiameter")
    tip_diameter: float = Field(25.0, alias="tipDiameter")
    counterbore_diameter: float = Field(40.0, alias="counterboreDiameter")
    counterbore_depth: float = Field(40.0, alias="counterboreDepth")
    axial_bore_diameter: float = Field(13.0, alias="axialBoreDiameter")
    outlet_diameter: float = Field(17.0, alias="outletDiameter")
    outlet_taper_half_angle: float = Field(15.0, alias="outletTaperHalfAngle")
    insert_outer_diameter: float = Field(39.4, alias="insertOuterDiameter")
    insert_length: float = Field(40.0, alias="insertLength")
    insert_thread_designation: str = Field("M12", alias="insertThreadDesignation")
    insert_axial_offset: float = Field(0.0, alias="insertAxialOffset")
    material: str = "45# 钢"
    units: Literal["mm"] = "mm"

    @field_validator(
        "main_length",
        "head_length",
        "neck_length",
        "head_left_diameter",
        "head_right_diameter",
        "neck_diameter",
        "tip_diameter",
        "counterbore_diameter",
        "counterbore_depth",
        "axial_bore_diameter",
        "outlet_diameter",
        "outlet_taper_half_angle",
        "insert_outer_diameter",
        "insert_length",
        "insert_axial_offset",
    )
    @classmethod
    def finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("dimension must be finite")
        return value

    @field_validator("insert_thread_designation")
    @classmethod
    def metric_thread_designation(cls, value: str) -> str:
        normalized = value.strip().upper().replace("×", "X")
        if not normalized or len(normalized) > 32:
            raise ValueError("insertThreadDesignation must be a short metric thread designation")
        import re

        if re.fullmatch(r"M\d+(?:\.\d+)?(?:X\d+(?:\.\d+)?)?", normalized) is None:
            raise ValueError("insertThreadDesignation must use metric syntax such as M12")
        return normalized

    @property
    def tip_length(self) -> float:
        return float(self.main_length - self.head_length - self.neck_length)

    @property
    def outlet_taper_length(self) -> float:
        radius_delta = (self.outlet_diameter - self.axial_bore_diameter) / 2
        tangent = math.tan(math.radians(self.outlet_taper_half_angle))
        if tangent <= 0:
            return float("inf")
        return float(radius_delta / tangent)

    @property
    def outlet_taper_start_x(self) -> float:
        return float(self.main_length - self.outlet_taper_length)

    @property
    def radial_clearance(self) -> float:
        return float((self.counterbore_diameter - self.insert_outer_diameter) / 2)

    @property
    def insert_thread_nominal_diameter(self) -> float:
        numeric = self.insert_thread_designation[1:].split("X", 1)[0]
        return float(numeric)


class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class ValidationIssue(ApiModel):
    rule_id: str = Field(alias="ruleId")
    severity: Severity
    passed: bool
    message: str
    actual: Any = None
    expected: Any = None


class ValidationReport(ApiModel):
    valid: bool
    production_ready: bool = Field(alias="productionReady")
    engine: str
    checked_at: datetime = Field(default_factory=utc_now, alias="checkedAt")
    parameters: BracketParameters
    issues: list[ValidationIssue]
    metrics: dict[str, float | int | str | bool]


class DrawingEvidence(ApiModel):
    field: str
    value: float | str
    source: str
    confidence: float = Field(ge=0, le=1)
    # Evidence is intentionally richer than a plain OCR number.  These fields
    # remain optional for backwards-compatible client submissions, while the
    # server recognizer fills them for calibrated drawings.
    unit: str = "mm"
    kind: str = "linear"
    view: str = "unknown"
    bbox: dict[str, float | int | str] | None = None
    verified: bool = False


class DrawingRecognition(ApiModel):
    id: str
    status: Literal["needs_review", "confirmed", "rejected"] = "needs_review"
    part_type: Literal["bracket"] = Field("bracket", alias="partType")
    source_filename: str = Field(alias="sourceFilename")
    source_sha256: str = Field(alias="sourceSha256")
    image_width: int | None = Field(None, alias="imageWidth")
    image_height: int | None = Field(None, alias="imageHeight")
    confidence: float = Field(ge=0, le=1)
    parameters: BracketParameters
    evidence: list[DrawingEvidence] = Field(default_factory=list)
    ocr_text: str = Field("", alias="ocrText")
    warnings: list[str] = Field(default_factory=list)
    validation: ValidationReport
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")
    engine: str = "compatibility-recognizer"
    assumptions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    # Rich OCR clients use these fields; the compatibility route still keeps
    # ``parameters`` and ``evidence`` as the stable minimum contract.
    dimensions: list[dict[str, Any]] = Field(default_factory=list)
    features: list[dict[str, Any]] = Field(default_factory=list)
    model_recipe: dict[str, Any] = Field(default_factory=dict, alias="modelRecipe")
    # AI proposals can be shown/edited before a durable recipe is accepted.
    # Keep this separate from ``parameters`` so an unknown drawing never
    # masquerades as a production-ready model.
    candidate_parameters: dict[str, Any] = Field(default_factory=dict, alias="candidateParameters")
    review_required: bool = Field(default=True, alias="reviewRequired")
    confirmation_type: str | None = Field(None, alias="confirmationType")
    confirmed_by: str | None = Field(None, alias="confirmedBy")
    confirmed_at: datetime | None = Field(None, alias="confirmedAt")


class DrawingResultSubmission(ApiModel):
    source_filename: str = Field("external-recognition.json", alias="sourceFilename")
    source_sha256: str = Field("external", alias="sourceSha256")
    confidence: float = Field(1.0, ge=0, le=1)
    parameters: BracketParameters
    evidence: list[DrawingEvidence] = Field(default_factory=list)
    ocr_text: str = Field("", alias="ocrText")
    confirmed: bool = False


class GeometryRequest(ApiModel):
    parameters: BracketParameters = Field(default_factory=BracketParameters)
    formats: list[Literal["step", "glb"]] = Field(
        default_factory=lambda: ["step", "glb"]
    )
    source_drawing_id: str | None = Field(None, alias="sourceDrawingId")
    require_cadquery: bool = Field(False, alias="requireCadQuery")
    # A drawing result marked ``needs_review`` can only enter generation after
    # the user explicitly confirms the evidence in the UI.  Existing callers
    # that do not attach a drawing remain unaffected.
    confirmed: bool = False

    @field_validator("formats")
    @classmethod
    def unique_non_empty_formats(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one output format is required")
        return list(dict.fromkeys(value))


class ArtifactDescriptor(ApiModel):
    id: str
    format: Literal["step", "glb"]
    filename: str
    media_type: str = Field(alias="mediaType")
    size_bytes: int = Field(alias="sizeBytes")
    sha256: str
    download_url: str = Field(alias="downloadUrl")
    engine: str
    production_ready: bool = Field(alias="productionReady")
    warnings: list[str] = Field(default_factory=list)


class GeometryResponse(ApiModel):
    request_id: str = Field(alias="requestId")
    status: Literal["completed", "failed"]
    engine: str
    parameters: BracketParameters
    validation: ValidationReport
    artifacts: list[ArtifactDescriptor]
    source_drawing_id: str | None = Field(None, alias="sourceDrawingId")
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")


class ModelGeometryRequest(ApiModel):
    """Recipe-dispatched geometry request used by the generic model API."""

    part_type: Literal["bracket", "split_clamp_support", "stepped_tapered_nozzle"] = Field(alias="partType")
    recipe_id: Literal[
        "bracket_support_v1",
        "split_clamp_support_v1",
        "stepped_tapered_nozzle_with_insert_v1",
    ] = Field(alias="recipeId")
    parameters: dict[str, Any]
    formats: list[Literal["step", "glb"]] = Field(default_factory=lambda: ["step", "glb"])
    source_drawing_id: str | None = Field(None, alias="sourceDrawingId")
    require_cadquery: bool = Field(False, alias="requireCadQuery")

    @field_validator("formats")
    @classmethod
    def unique_non_empty_formats(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one output format is required")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def recipe_matches_part_type(self) -> "ModelGeometryRequest":
        expected = {
            "bracket_support_v1": "bracket",
            "split_clamp_support_v1": "split_clamp_support",
            "stepped_tapered_nozzle_with_insert_v1": "stepped_tapered_nozzle",
        }[self.recipe_id]
        if self.part_type != expected:
            raise ValueError(f"recipeId {self.recipe_id!r} requires partType {expected!r}")
        return self


class ModelGeometryResponse(ApiModel):
    request_id: str = Field(alias="requestId")
    status: Literal["completed", "failed"]
    part_type: Literal["bracket", "split_clamp_support", "stepped_tapered_nozzle"] = Field(alias="partType")
    recipe_id: Literal[
        "bracket_support_v1",
        "split_clamp_support_v1",
        "stepped_tapered_nozzle_with_insert_v1",
    ] = Field(alias="recipeId")
    engine: str
    parameters: dict[str, Any]
    validation: dict[str, Any]
    artifacts: list[ArtifactDescriptor]
    source_drawing_id: str | None = Field(None, alias="sourceDrawingId")
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")
