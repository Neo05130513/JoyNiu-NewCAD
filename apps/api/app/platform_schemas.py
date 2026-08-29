"""Dependency-free request/response contracts for platform endpoints.

The geometry API uses Pydantic models in ``schemas.py``.  Platform services
also need to run in workers where Pydantic is not installed, so these compact
dataclasses provide validation and camel-case conversion without coupling the
domain layer to a web framework.  FastAPI callers may pass ordinary dicts to
the adapter; these contracts are useful for typed clients and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .platform import ValidationError


def _required_text(value: Any, field_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValidationError(f"{field_name} is required")
    return clean


@dataclass(frozen=True, slots=True)
class ProjectCreateRequest:
    name: str
    owner_id: str | None = None
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProjectCreateRequest":
        return cls(
            name=_required_text(payload.get("name"), "name"),
            owner_id=(str(payload["ownerId"]).strip() if payload.get("ownerId") else None),
            description=str(payload.get("description", "")),
            metadata=payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ownerId": self.owner_id,
            "description": self.description,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DocumentCreateRequest:
    name: str
    kind: str = "model"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DocumentCreateRequest":
        return cls(
            name=_required_text(payload.get("name"), "name"),
            kind=str(payload.get("kind", "model")),
            metadata=payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "metadata": dict(self.metadata)}


@dataclass(frozen=True, slots=True)
class VersionCreateRequest:
    content: Any
    file_name: str | None = None
    content_type: str = "application/octet-stream"
    note: str = ""
    expected_revision: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VersionCreateRequest":
        if "content" in payload:
            content = payload["content"]
        elif "contentText" in payload:
            content = payload["contentText"]
        elif "contentBase64" in payload:
            content = payload["contentBase64"]
        else:
            raise ValidationError("content, contentText or contentBase64 is required")
        expected = payload.get("expectedRevision", payload.get("expected_current_revision"))
        if expected is not None:
            try:
                expected = int(expected)
            except (TypeError, ValueError) as exc:
                raise ValidationError("expectedRevision must be an integer") from exc
        return cls(
            content=content,
            file_name=payload.get("fileName", payload.get("file_name")),
            content_type=str(payload.get("contentType", "application/octet-stream")),
            note=str(payload.get("note", "")),
            expected_revision=expected,
            metadata=payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "fileName": self.file_name,
            "contentType": self.content_type,
            "note": self.note,
            "expectedRevision": self.expected_revision,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class OCRAnalyzeRequest:
    image_base64: str
    filename: str = "drawing.png"
    fixture_id: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OCRAnalyzeRequest":
        value = payload.get("imageBase64", payload.get("image_base64", payload.get("contentBase64")))
        return cls(
            image_base64=_required_text(value, "imageBase64"),
            filename=str(payload.get("filename", payload.get("sourceFilename", "drawing.png"))),
            fixture_id=payload.get("fixtureId", payload.get("fixture_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "imageBase64": self.image_base64,
            "filename": self.filename,
            "fixtureId": self.fixture_id,
        }


@dataclass(frozen=True, slots=True)
class CAMPlanCreateRequest:
    geometry_hash: str
    stock: Mapping[str, Any]
    machine: str = "3-axis-mill"
    units: str = "mm"
    project_id: str | None = None
    source_document_id: str | None = None
    source_version_id: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CAMPlanCreateRequest":
        geometry_hash = _required_text(payload.get("geometryHash", payload.get("geometry_hash")), "geometryHash")
        stock = payload.get("stock")
        if not isinstance(stock, Mapping):
            raise ValidationError("stock must be an object")
        return cls(
            geometry_hash=geometry_hash,
            stock=stock,
            machine=str(payload.get("machine", "3-axis-mill")),
            units=str(payload.get("units", "mm")),
            project_id=payload.get("projectId", payload.get("project_id")),
            source_document_id=payload.get("sourceDocumentId", payload.get("source_document_id")),
            source_version_id=payload.get("sourceVersionId", payload.get("source_version_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "geometryHash": self.geometry_hash,
            "stock": dict(self.stock),
            "machine": self.machine,
            "units": self.units,
            "projectId": self.project_id,
            "sourceDocumentId": self.source_document_id,
            "sourceVersionId": self.source_version_id,
        }


@dataclass(frozen=True, slots=True)
class CAMOperationCreateRequest:
    operation_type: str
    tool_id: str
    depth: float
    feed_rate: float = 600.0
    spindle_rpm: int = 6000
    retract_height: float = 5.0
    path_length: float = 0.0
    parameters: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CAMOperationCreateRequest":
        try:
            depth = float(payload.get("depth"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("depth is required and must be numeric") from exc
        return cls(
            operation_type=str(payload.get("operationType", payload.get("operation_type", "profile"))),
            tool_id=str(payload.get("toolId", payload.get("tool_id", "T10"))),
            depth=depth,
            feed_rate=float(payload.get("feedRate", payload.get("feed_rate", 600))),
            spindle_rpm=int(payload.get("spindleRpm", payload.get("spindle_rpm", 6000))),
            retract_height=float(payload.get("retractHeight", payload.get("retract_height", 5))),
            path_length=float(payload.get("pathLength", payload.get("path_length", 0))),
            parameters=payload.get("parameters", {}) if isinstance(payload.get("parameters", {}), Mapping) else {},
            enabled=bool(payload.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operationType": self.operation_type,
            "toolId": self.tool_id,
            "depth": self.depth,
            "feedRate": self.feed_rate,
            "spindleRpm": self.spindle_rpm,
            "retractHeight": self.retract_height,
            "pathLength": self.path_length,
            "parameters": dict(self.parameters),
            "enabled": self.enabled,
        }


__all__ = [
    "CAMOperationCreateRequest",
    "CAMPlanCreateRequest",
    "DocumentCreateRequest",
    "OCRAnalyzeRequest",
    "ProjectCreateRequest",
    "VersionCreateRequest",
]
