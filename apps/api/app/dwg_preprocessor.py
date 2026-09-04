"""Safe DWG preprocessing for vector-aware AI drawing analysis.

DWG is a binary container and is not sent directly to a multimodal model.  This
module converts it to ASCII DXF with a separately installed converter, extracts
the vector geometry and explicit dimensions with :mod:`ezdxf`, and renders a
white-background PNG for the visual analysis pass.

The converter is always executed without a shell and inside a private temporary
directory.  The default commands prefer GNU LibreDWG's ``dwgread`` and fall
back to ``dwg2dxf``.  A deployment can provide an explicit argv array through
``JOYNIU_DWG_CONVERTER_COMMAND_JSON``; the only recognised placeholders are
``{input_dwg}`` and ``{output_dxf}``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from typing import Any, Iterable, Mapping, Sequence


_DWG_VERSION_NAMES: Mapping[str, str] = {
    "AC1009": "R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "AutoCAD 2000/2002",
    "AC1018": "AutoCAD 2004/2006",
    "AC1021": "AutoCAD 2007/2009",
    "AC1024": "AutoCAD 2010/2012",
    "AC1027": "AutoCAD 2013/2017",
    "AC1032": "AutoCAD 2018+",
}
_POINT_ATTRS = tuple(f"defpoint{suffix}" for suffix in ("", "2", "3", "4", "5", "6"))
_GEOMETRY_TYPES = frozenset(
    {"LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "SPLINE", "POINT"}
)


class DWGPreprocessError(RuntimeError):
    """Base class with a stable machine-readable error code."""

    code = "dwg_preprocess_failed"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)

    @property
    def error_type(self) -> str:
        return self.code

    def to_dict(self) -> dict[str, str]:
        return {"errorType": self.code, "message": str(self)}


class DWGInputError(DWGPreprocessError):
    code = "invalid_dwg_input"


class DWGInputTooLargeError(DWGPreprocessError):
    code = "dwg_input_too_large"


class DWGConverterUnavailableError(DWGPreprocessError):
    code = "dwg_converter_unavailable"


class DWGConversionTimeoutError(DWGPreprocessError):
    code = "dwg_conversion_timeout"


class DWGConversionError(DWGPreprocessError):
    code = "dwg_conversion_failed"


class DWGParserUnavailableError(DWGPreprocessError):
    code = "dxf_parser_unavailable"


class DWGParseError(DWGPreprocessError):
    code = "dxf_parse_failed"


class DWGRenderError(DWGPreprocessError):
    code = "dxf_render_failed"


class DWGResourceLimitError(DWGPreprocessError):
    code = "dwg_resource_limit_exceeded"


@dataclass(frozen=True, slots=True)
class DWGConverterCommand:
    """One trusted converter invocation template.

    ``arguments`` is an argv tuple, never a command line string.  ``output_path``
    identifies the DXF produced by the command.  This supports ``dwg2dxf``,
    which derives ``source.dxf`` from ``source.dwg`` rather than accepting an
    explicit destination on older releases.
    """

    name: str
    arguments: tuple[str, ...]
    output_path: str = "{output_dxf}"


@dataclass(frozen=True, slots=True)
class DWGPreprocessConfig:
    timeout_seconds: float = 45.0
    max_input_bytes: int = 64 * 1024 * 1024
    max_dxf_bytes: int = 128 * 1024 * 1024
    max_png_bytes: int = 12 * 1024 * 1024
    max_summary_bytes: int = 512 * 1024
    max_entities: int = 100_000
    max_expanded_entities: int = 200_000
    max_layout_records: int = 256
    max_geometry_items: int = 10_000
    max_geometry_points: int = 500_000
    max_dimension_items: int = 5_000
    max_insert_depth: int = 8
    max_render_layouts: int = 12
    max_render_pixels: int = 24_000_000
    max_converter_memory_bytes: int = 1536 * 1024 * 1024
    render_dpi: int = 240
    render_size_inches: float = 12.0
    converter_commands: tuple[DWGConverterCommand, ...] | None = None

    def __post_init__(self) -> None:
        positive_values = {
            "timeout_seconds": self.timeout_seconds,
            "max_input_bytes": self.max_input_bytes,
            "max_dxf_bytes": self.max_dxf_bytes,
            "max_png_bytes": self.max_png_bytes,
            "max_summary_bytes": self.max_summary_bytes,
            "max_entities": self.max_entities,
            "max_expanded_entities": self.max_expanded_entities,
            "max_layout_records": self.max_layout_records,
            "max_geometry_items": self.max_geometry_items,
            "max_geometry_points": self.max_geometry_points,
            "max_dimension_items": self.max_dimension_items,
            "max_insert_depth": self.max_insert_depth,
            "max_render_layouts": self.max_render_layouts,
            "max_render_pixels": self.max_render_pixels,
            "max_converter_memory_bytes": self.max_converter_memory_bytes,
            "render_dpi": self.render_dpi,
            "render_size_inches": self.render_size_inches,
        }
        if any(value <= 0 for value in positive_values.values()):
            raise ValueError("DWG preprocessing limits must be positive")


@dataclass(frozen=True, slots=True)
class DWGSourceMetadata:
    filename: str
    size_bytes: int
    sha256: str
    signature: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "filename": data["filename"],
            "sizeBytes": data["size_bytes"],
            "sha256": data["sha256"],
            "signature": data["signature"],
            "version": data["version"],
        }


@dataclass(frozen=True, slots=True)
class DWGPreprocessResult:
    original_metadata: DWGSourceMetadata
    converter: str
    dxf_bytes: bytes
    png_bytes: bytes
    # ``summary`` is deliberately safe to serialize into a remote-model
    # request: it contains generated IDs, numeric values and fixed enums only.
    summary: dict[str, Any]
    # User-authored layer/layout names and unrestricted dimension text remain
    # local.  Keeping them separate makes an accidental prompt injection much
    # harder during later API integration.
    audit_summary: dict[str, Any]


def dwg_preprocessor_status() -> dict[str, Any]:
    """Report whether this host can decode, inspect and render DWG files."""

    try:
        commands = _resolve_converter_commands(DWGPreprocessConfig())
        converter_names = [command.name for command in commands]
        converter_error = ""
    except DWGPreprocessError as exc:
        converter_names = []
        converter_error = str(exc)
    dependencies = {
        "ezdxf": importlib.util.find_spec("ezdxf") is not None,
        "matplotlib": importlib.util.find_spec("matplotlib") is not None,
        "Pillow": importlib.util.find_spec("PIL") is not None,
    }
    available = bool(converter_names and all(dependencies.values()))
    return {
        "available": available,
        "engine": converter_names[0] if converter_names else "unavailable",
        "converterCandidates": converter_names,
        "dependencies": dependencies,
        "error": converter_error or None,
        "pipeline": "DWG -> DXF -> vector/dimension evidence + PNG",
        "rawDwgSentToAI": False,
    }


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _point(value: Any) -> list[float] | None:
    if value is None:
        return None
    try:
        components = list(value)
    except (TypeError, ValueError):
        return None
    result = [_safe_number(component) for component in components[:3]]
    if not result or any(component is None for component in result):
        return None
    while len(result) < 3:
        result.append(0.0)
    return [float(component) for component in result]


def _safe_text(value: Any, maximum: int = 512) -> str:
    text = str(value or "").replace("\x00", "")
    return text[:maximum]


def _source_metadata(payload: bytes, filename: str) -> DWGSourceMetadata:
    signature = payload[:6].decode("ascii", errors="replace")
    if len(payload) < 6 or not re.fullmatch(r"AC\d{4}", signature):
        raise DWGInputError("file does not have a supported DWG signature")
    return DWGSourceMetadata(
        filename=Path(filename or "drawing.dwg").name[:255] or "drawing.dwg",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        signature=signature,
        version=_DWG_VERSION_NAMES.get(signature, f"DWG {signature}"),
    )


def _configured_converter_from_environment() -> tuple[DWGConverterCommand, ...] | None:
    raw = os.getenv("JOYNIU_DWG_CONVERTER_COMMAND_JSON", "").strip()
    if not raw:
        return None
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DWGConverterUnavailableError("invalid DWG converter command JSON") from exc
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(item, str) and item for item in arguments)
    ):
        raise DWGConverterUnavailableError("DWG converter command must be a JSON argv array")
    if not any("{input_dwg}" in item for item in arguments):
        raise DWGConverterUnavailableError("DWG converter command is missing {input_dwg}")
    return (DWGConverterCommand("configured", tuple(arguments)),)


def _default_converter_commands() -> tuple[DWGConverterCommand, ...]:
    commands: list[DWGConverterCommand] = []
    dwgread = shutil.which("dwgread")
    if dwgread:
        commands.append(
            DWGConverterCommand(
                "libredwg-dwgread",
                (dwgread, "-O", "DXF", "-o", "{output_dxf}", "{input_dwg}"),
            )
        )
    dwg2dxf = shutil.which("dwg2dxf")
    if dwg2dxf:
        commands.append(
            DWGConverterCommand(
                "libredwg-dwg2dxf",
                (dwg2dxf, "--overwrite", "{input_dwg}"),
                output_path="{input_stem}.dxf",
            )
        )
    return tuple(commands)


def _resolve_converter_commands(config: DWGPreprocessConfig) -> tuple[DWGConverterCommand, ...]:
    if config.converter_commands is not None:
        commands = config.converter_commands
    else:
        commands = _configured_converter_from_environment() or _default_converter_commands()
    if not commands:
        raise DWGConverterUnavailableError(
            "no DWG converter found; install LibreDWG dwgread/dwg2dxf or configure an argv array"
        )
    return commands


def _expand_arguments(
    command: DWGConverterCommand,
    input_path: Path,
    output_path: Path,
) -> tuple[list[str], Path]:
    replacements = {
        "{input_dwg}": str(input_path),
        "{output_dxf}": str(output_path),
        "{input_stem}": str(input_path.with_suffix("")),
    }

    def expand(value: str) -> str:
        result = value
        for placeholder, replacement in replacements.items():
            result = result.replace(placeholder, replacement)
        return result

    arguments = [expand(value) for value in command.arguments]
    expected = Path(expand(command.output_path))
    if not expected.is_absolute():
        expected = input_path.parent / expected
    try:
        expected.resolve().relative_to(input_path.parent.resolve())
    except ValueError as exc:
        raise DWGConversionError("converter output must stay inside its temporary directory") from exc
    return arguments, expected


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows deployment only
            process.kill()
    except (OSError, ProcessLookupError):
        process.kill()
    process.wait(timeout=5)


def _converter_resource_limiter(config: DWGPreprocessConfig) -> Any:
    """Return a minimal POSIX pre-exec limiter for the untrusted conversion.

    stdout/stderr are discarded, so ``RLIMIT_FSIZE`` applies to converter
    artifacts such as the DXF itself rather than log files.  The wall-clock
    timeout in the parent remains authoritative; the CPU cap prevents a child
    that forks or spins from consuming CPU indefinitely before that timeout.
    """

    if os.name != "posix":
        return None

    def apply_limits() -> None:
        import resource

        limits = (
            (resource.RLIMIT_FSIZE, int(config.max_dxf_bytes)),
            (resource.RLIMIT_AS, int(config.max_converter_memory_bytes)),
            (resource.RLIMIT_CPU, max(1, int(math.ceil(config.timeout_seconds)) + 1)),
            (resource.RLIMIT_NOFILE, 64),
        )
        for resource_id, requested in limits:
            try:
                _soft, hard = resource.getrlimit(resource_id)
                target = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
                resource.setrlimit(resource_id, (target, target))
            except (OSError, ValueError):
                # Some platforms expose a resource constant without allowing
                # an unprivileged child to lower it. Other available limits
                # and the parent wall-clock timeout still apply.
                continue

    return apply_limits


def _run_converter(
    command: DWGConverterCommand,
    input_path: Path,
    output_path: Path,
    config: DWGPreprocessConfig,
) -> tuple[Path, str | None]:
    arguments, expected_path = _expand_arguments(command, input_path, output_path)
    if not arguments:
        return expected_path, "empty converter argv"
    try:
        process = subprocess.Popen(
            arguments,
            cwd=input_path.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=os.name == "posix",
            close_fds=True,
            preexec_fn=_converter_resource_limiter(config),
        )
        try:
            return_code = process.wait(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            raise DWGConversionTimeoutError(
                f"DWG converter {command.name} exceeded {config.timeout_seconds:g} seconds"
            ) from exc
    except DWGPreprocessError:
        raise
    except OSError as exc:
        return expected_path, f"converter could not start ({type(exc).__name__})"

    if return_code != 0:
        try:
            partial_size = expected_path.stat().st_size
        except OSError:
            partial_size = 0
        if partial_size >= config.max_dxf_bytes or return_code == -getattr(signal, "SIGXFSZ", 25):
            raise DWGResourceLimitError("converted DXF exceeds the configured size limit")
        return expected_path, f"exit status {return_code}"
    try:
        metadata = expected_path.lstat()
    except OSError:
        return expected_path, "converter did not create a DXF file"
    if not stat.S_ISREG(metadata.st_mode) or expected_path.is_symlink():
        return expected_path, "converter output is not a regular DXF file"
    if metadata.st_size <= 0:
        return expected_path, "converter created an empty DXF file"
    if metadata.st_size > config.max_dxf_bytes:
        raise DWGResourceLimitError("converted DXF exceeds the configured size limit")
    return expected_path, None


def _import_ezdxf() -> Any:
    try:
        import ezdxf  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        raise DWGParserUnavailableError("ezdxf is required to inspect converted DWG files") from exc
    return ezdxf


def _read_document(path: Path) -> tuple[Any, list[str]]:
    ezdxf = _import_ezdxf()
    warnings: list[str] = []
    try:
        return ezdxf.readfile(path), warnings
    except Exception as first_error:
        try:
            from ezdxf import recover  # type: ignore

            document, auditor = recover.readfile(path)
            if getattr(auditor, "has_errors", False):
                warnings.append("DXF required recovery and still has audit errors")
            else:
                warnings.append("DXF required tolerant recovery")
            return document, warnings
        except Exception as recovery_error:
            raise DWGParseError(
                f"converted DXF could not be parsed ({type(first_error).__name__}/{type(recovery_error).__name__})"
            ) from recovery_error


def _bbox_dict(entities: Iterable[Any]) -> dict[str, list[float]] | None:
    try:
        from ezdxf import bbox  # type: ignore

        box = bbox.extents(entities, fast=True)
    except Exception:
        return None
    if not getattr(box, "has_data", False):
        return None
    minimum = _point(box.extmin)
    maximum = _point(box.extmax)
    size = _point(box.size)
    if minimum is None or maximum is None or size is None:
        return None
    return {"min": minimum, "max": maximum, "size": size}


def _dimension_record(entity: Any, layout_name: str, layer_name: str | None = None) -> dict[str, Any]:
    try:
        measurement = _safe_number(entity.get_measurement())
    except Exception:
        measurement = None
    defpoints: dict[str, list[float]] = {}
    for name in _POINT_ATTRS:
        if entity.dxf.hasattr(name):
            value = _point(entity.dxf.get(name))
            if value is not None:
                defpoints[name] = value
    dimtype = int(entity.dxf.get("dimtype", 0))
    return {
        "handle": _safe_text(entity.dxf.get("handle", ""), 64),
        "layout": layout_name,
        "layer": layer_name or _safe_text(entity.dxf.get("layer", "0"), 255),
        "dimensionType": dimtype & 15,
        "measurement": measurement,
        "text": _safe_text(entity.dxf.get("text", "")),
        "defpoints": defpoints,
    }


def _geometry_record(
    entity: Any,
    layout_name: str,
    layer_name: str | None = None,
) -> dict[str, Any] | None:
    entity_type = entity.dxftype()
    record: dict[str, Any] = {
        "type": entity_type,
        "layout": layout_name,
        "layer": layer_name or _safe_text(entity.dxf.get("layer", "0"), 255),
    }
    try:
        if entity_type == "LINE":
            record.update(start=_point(entity.dxf.start), end=_point(entity.dxf.end))
        elif entity_type in {"CIRCLE", "ARC"}:
            record.update(center=_point(entity.dxf.center), radius=_safe_number(entity.dxf.radius))
            if entity_type == "ARC":
                record.update(
                    startAngle=_safe_number(entity.dxf.start_angle),
                    endAngle=_safe_number(entity.dxf.end_angle),
                )
        elif entity_type == "LWPOLYLINE":
            points = []
            source_point_count = 0
            for x, y, *_ in entity.get_points("xy"):
                source_point_count += 1
                point = _point((x, y, 0))
                if point is not None and len(points) < 20_000:
                    points.append(point[:2])
            record.update(points=points, pointCount=source_point_count, closed=bool(entity.closed))
            if source_point_count > len(points):
                record["pointsTruncated"] = source_point_count - len(points)
        elif entity_type == "POLYLINE":
            clean_points: list[list[float]] = []
            source_point_count = 0
            for vertex in entity.vertices:
                source_point_count += 1
                point = _point(vertex.dxf.location)
                if point is not None and len(clean_points) < 20_000:
                    clean_points.append(point)
            record.update(
                points=clean_points,
                pointCount=source_point_count,
                closed=bool(entity.is_closed),
            )
            if source_point_count > len(clean_points):
                record["pointsTruncated"] = source_point_count - len(clean_points)
        elif entity_type == "ELLIPSE":
            record.update(
                center=_point(entity.dxf.center),
                majorAxis=_point(entity.dxf.major_axis),
                ratio=_safe_number(entity.dxf.ratio),
                startParameter=_safe_number(entity.dxf.start_param),
                endParameter=_safe_number(entity.dxf.end_param),
            )
        elif entity_type == "SPLINE":
            clean_points = []
            source_point_count = 0
            for raw_point in entity.control_points:
                source_point_count += 1
                point = _point(raw_point)
                if point is not None and len(clean_points) < 20_000:
                    clean_points.append(point)
            record.update(controlPoints=clean_points, pointCount=source_point_count)
            if source_point_count > len(clean_points):
                record["pointsTruncated"] = source_point_count - len(clean_points)
        elif entity_type == "POINT":
            record["location"] = _point(entity.dxf.location)
        else:
            return None
    except Exception:
        record["detailUnavailable"] = True
    return record


def _iter_expanded_entity(
    entity: Any,
    *,
    depth: int,
    inherited_layer: str | None,
    state: dict[str, int],
    config: DWGPreprocessConfig,
) -> Iterable[tuple[Any, str]]:
    entity_layer = _safe_text(entity.dxf.get("layer", "0"), 255)
    effective_layer = inherited_layer if entity_layer == "0" and inherited_layer else entity_layer
    if entity.dxftype() != "INSERT":
        state["count"] += 1
        if state["count"] > config.max_expanded_entities:
            raise DWGResourceLimitError("expanded DXF entity count exceeds the configured limit")
        yield entity, effective_layer
        return
    if depth >= config.max_insert_depth:
        raise DWGResourceLimitError("nested DXF block depth exceeds the configured limit")

    try:
        insertions = list(entity.multi_insert()) if int(getattr(entity, "mcount", 1)) > 1 else [entity]
        for insertion in insertions:
            for child in insertion.virtual_entities():
                yield from _iter_expanded_entity(
                    child,
                    depth=depth + 1,
                    inherited_layer=effective_layer,
                    state=state,
                    config=config,
                )
    except DWGPreprocessError:
        raise
    except Exception as exc:
        raise DWGParseError("DXF block reference could not be expanded safely") from exc


def _expand_layout_entities(
    entities: Sequence[Any],
    config: DWGPreprocessConfig,
    state: dict[str, int],
) -> list[tuple[Any, str]]:
    expanded: list[tuple[Any, str]] = []
    for entity in entities:
        expanded.extend(
            _iter_expanded_entity(
                entity,
                depth=0,
                inherited_layer=None,
                state=state,
                config=config,
            )
        )
    return expanded


def _summary_size(summary: Mapping[str, Any]) -> int:
    return len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit_summary(summary: dict[str, Any], maximum_bytes: int) -> dict[str, Any]:
    truncated = summary.setdefault("truncated", {})
    for key, omitted_key in (("geometry", "geometryItems"), ("dimensions", "dimensions")):
        values = summary[key]
        while values and _summary_size(summary) > maximum_bytes:
            remove_count = max(1, len(values) // 2)
            del values[-remove_count:]
            truncated[omitted_key] = int(truncated.get(omitted_key, 0)) + remove_count
    vector_candidates = summary.get("vectorParameterCandidates")
    if isinstance(vector_candidates, Mapping):
        available_source_ids = {
            str(item.get("id"))
            for key in ("dimensions", "geometry")
            for item in summary.get(key, [])
            if isinstance(item, Mapping) and item.get("id")
        }
        referenced_source_ids = {
            str(source_id)
            for candidate in vector_candidates.get("fieldCandidates", {}).values()
            if isinstance(candidate, Mapping)
            for source_id in candidate.get("sourceIds", [])
        }
        if (
            not referenced_source_ids.issubset(available_source_ids)
            or _summary_size(summary) > maximum_bytes
        ):
            summary.pop("vectorParameterCandidates", None)
            truncated["vectorParameterCandidates"] = 1
    if _summary_size(summary) > maximum_bytes:
        # Extremely long/corrupt table names can otherwise defeat the output
        # bound even after all entity details have been removed.
        summary["layers"] = summary["layers"][:64]
        truncated["layerRecords"] = max(0, summary["layerCount"] - len(summary["layers"]))
    if _summary_size(summary) > maximum_bytes:
        raise DWGResourceLimitError("DXF summary cannot fit within the configured size limit")
    if not truncated:
        summary.pop("truncated", None)
    return summary


_DIMENSION_LITERAL = re.compile(
    r"^(?:<>|Ø<>|[ØRM]?[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
    r"(?:\s*(?:MM|CM|M|IN|DEG|°))?"
    r"(?:\s*(?:±|\+/-)\s*(?:\d+(?:\.\d+)?|\.\d+))?)$",
    flags=re.IGNORECASE,
)
_SAFE_ENTITY_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")

_STEPPED_NOZZLE_RECIPE_FIELDS = (
    "mainLength",
    "headLength",
    "neckLength",
    "headLeftDiameter",
    "headRightDiameter",
    "neckDiameter",
    "tipDiameter",
    "counterboreDiameter",
    "counterboreDepth",
    "axialBoreDiameter",
    "outletDiameter",
    "outletTaperHalfAngle",
    "insertOuterDiameter",
    "insertLength",
    "insertThreadDesignation",
    "insertAxialOffset",
)


def _safe_dimension_literal(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().split())
    if not text:
        # An omitted DXF group 1 has the same semantics as the standard <>
        # placeholder: display the measured value.
        return "<>"
    diameter_placeholder = re.fullmatch(r"\{%%[cC]<>\}(?:\{\})*", text)
    if diameter_placeholder:
        return "Ø<>"
    formatted_literal = re.fullmatch(
        r"\{([ØRM]?[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+))\}(?:\{\})*",
        text,
        flags=re.IGNORECASE,
    )
    if formatted_literal:
        text = formatted_literal.group(1)
    text = text.replace(",", ".").replace("Φ", "Ø").replace("φ", "Ø").replace("⌀", "Ø")
    if len(text) > 64 or not _DIMENSION_LITERAL.fullmatch(text):
        return None
    return text


def _entity_type(value: Any) -> str:
    candidate = str(value or "").upper()
    return candidate if _SAFE_ENTITY_TYPE.fullmatch(candidate) else "UNKNOWN"


def _safe_count_map(values: Mapping[str, Any]) -> dict[str, int]:
    result: Counter[str] = Counter()
    for key, value in values.items():
        try:
            count = max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            continue
        result[_entity_type(key)] += count
    return dict(sorted(result.items()))


def _canonical_vector_value(value: Any) -> float | None:
    """Remove converter floating-point noise without changing drawing precision."""

    number = _safe_number(value)
    if number is None:
        return None
    rounded = round(number, 12)
    nearest_integer = round(rounded)
    if math.isclose(rounded, nearest_integer, rel_tol=0.0, abs_tol=1e-10):
        return float(nearest_integer)
    return rounded


def _dimension_geometry(dimension: Mapping[str, Any]) -> dict[str, Any] | None:
    points = dimension.get("defpoints")
    if not isinstance(points, Mapping):
        return None
    first = _point(points.get("defpoint2"))
    second = _point(points.get("defpoint3"))
    if first is None or second is None:
        return None
    delta_x = abs(second[0] - first[0])
    delta_y = abs(second[1] - first[1])
    orientation = "axial" if delta_x > delta_y else "transverse"
    return {
        "orientation": orientation,
        "xInterval": [min(first[0], second[0]), max(first[0], second[0])],
        "xStation": (first[0] + second[0]) / 2,
        "yMidpoint": (first[1] + second[1]) / 2,
    }


def _near(left: float, right: float, tolerance: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)


def _interval_endpoint_distance(interval: Sequence[float], station: float) -> float:
    return min(abs(float(interval[0]) - station), abs(float(interval[1]) - station))


def _dimension_candidate(
    dimension: Mapping[str, Any],
    *,
    value: float | str,
    unit: str | None,
) -> dict[str, Any]:
    geometry = _dimension_geometry(dimension) or {}
    return {
        "status": "resolved",
        "value": value,
        "unit": unit,
        "sourceType": "direct_dimension",
        "sourceIds": [str(dimension.get("id", "dimension_unknown"))],
        "measurementOrientation": geometry.get("orientation", "unknown"),
        "evidenceStrength": "exact",
        "requiresModelReview": True,
        "requiresHumanConfirmation": True,
    }


def _unresolved_vector_candidate(reason_code: str) -> dict[str, Any]:
    return {
        "status": "unresolved",
        "value": None,
        "unit": None,
        "sourceType": "unresolved",
        "sourceIds": [],
        "reasonCode": reason_code,
        "evidenceStrength": "missing",
        "requiresModelReview": True,
        "requiresHumanConfirmation": True,
    }


def _build_stepped_nozzle_vector_candidates(
    dimensions: Sequence[Mapping[str, Any]],
    geometry: Sequence[Mapping[str, Any]],
    units: Any,
) -> dict[str, Any] | None:
    """Map an unambiguous two-profile DWG topology to recipe evidence.

    This is deliberately a deterministic vector recognizer, not an AI result
    and not a source of production-authoritative parameters.  It only runs
    when the source contains the complete, mutually consistent dimension
    pattern used by ``stepped_tapered_nozzle_with_insert_v1``.  Values always
    come from DIMENSION measurements or an explicitly identified sloped pair;
    no OCR text, default recipe value or filename is consulted.
    """

    if not isinstance(units, Mapping) or int(units.get("code", 0) or 0) != 4:
        return None

    records: list[dict[str, Any]] = []
    for source in dimensions:
        measurement = _canonical_vector_value(source.get("measurement"))
        shape = _dimension_geometry(source)
        if measurement is None or measurement <= 0 or shape is None:
            continue
        records.append({"source": source, "measurement": measurement, **shape})

    thread_records = [
        record
        for record in records
        if isinstance(record["source"].get("text"), str)
        and re.fullmatch(r"M\d+(?:\.\d+)?", str(record["source"]["text"]), re.IGNORECASE)
        and record["orientation"] == "transverse"
    ]
    diameter_records = [
        record
        for record in records
        if record["source"].get("textMode") == "diameter-measurement"
        and record["orientation"] == "transverse"
    ]
    axial_records = [record for record in records if record["orientation"] == "axial"]
    if len(thread_records) != 1 or len(diameter_records) < 8 or len(axial_records) < 6:
        return None

    thread = thread_records[0]
    thread_station = float(thread["xStation"])
    drawing_scale = max(record["measurement"] for record in axial_records)
    station_tolerance = max(1e-6, drawing_scale * 1e-5)

    insert_length_options = sorted(
        axial_records,
        key=lambda record: _interval_endpoint_distance(record["xInterval"], thread_station),
    )
    if not insert_length_options:
        return None
    insert_length = insert_length_options[0]
    if _interval_endpoint_distance(insert_length["xInterval"], thread_station) > station_tolerance:
        return None
    insert_start, insert_end = (float(value) for value in insert_length["xInterval"])
    insert_diameters = [
        record
        for record in diameter_records
        if _near(float(record["xStation"]), thread_station, station_tolerance)
        and record["measurement"] > thread["measurement"]
    ]
    if not insert_diameters:
        return None
    insert_outer = max(insert_diameters, key=lambda record: record["measurement"])

    def belongs_to_insert(record: Mapping[str, Any]) -> bool:
        if record["orientation"] == "transverse":
            return insert_start - station_tolerance <= float(record["xStation"]) <= insert_end + station_tolerance
        interval = record["xInterval"]
        overlap = min(float(interval[1]), insert_end) - max(float(interval[0]), insert_start)
        return overlap >= -station_tolerance

    main_axials = [record for record in axial_records if not belongs_to_insert(record)]
    if not main_axials:
        return None
    main_length = max(main_axials, key=lambda record: record["measurement"])
    main_start, main_end = (float(value) for value in main_length["xInterval"])
    if main_end >= insert_start - station_tolerance:
        return None
    main_diameters = [
        record
        for record in diameter_records
        if main_start - station_tolerance <= float(record["xStation"]) <= main_end + station_tolerance
    ]

    def at_station(source: Sequence[Mapping[str, Any]], station: float) -> list[Mapping[str, Any]]:
        return [
            record
            for record in source
            if _near(float(record["xStation"]), station, station_tolerance)
        ]

    start_diameters = at_station(main_diameters, main_start)
    start_axials = [
        record
        for record in main_axials
        if _interval_endpoint_distance(record["xInterval"], main_start) <= station_tolerance
        and record is not main_length
    ]
    if len(start_diameters) < 2 or len(start_axials) < 2:
        return None
    head_left = max(start_diameters, key=lambda record: record["measurement"])
    counterbore = min(start_diameters, key=lambda record: record["measurement"])

    head_options: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
    for record in start_axials:
        interval = record["xInterval"]
        far_station = (
            float(interval[1])
            if _near(float(interval[0]), main_start, station_tolerance)
            else float(interval[0])
        )
        station_diameters = at_station(main_diameters, far_station)
        if station_diameters:
            head_options.append(
                (record, max(station_diameters, key=lambda item: item["measurement"]), far_station)
            )
    if not head_options:
        return None
    head_length, head_right, head_end = max(
        head_options,
        key=lambda item: item[1]["measurement"],
    )

    neck_options: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
    for record in main_axials:
        if record is main_length or record is head_length:
            continue
        interval = record["xInterval"]
        if _interval_endpoint_distance(interval, head_end) > station_tolerance:
            continue
        far_station = (
            float(interval[1])
            if _near(float(interval[0]), head_end, station_tolerance)
            else float(interval[0])
        )
        if far_station <= head_end + station_tolerance or far_station >= main_end - station_tolerance:
            continue
        station_diameters = at_station(main_diameters, far_station)
        if station_diameters:
            neck_options.append(
                (record, max(station_diameters, key=lambda item: item["measurement"]), far_station)
            )
    if len(neck_options) != 1:
        return None
    neck_length, neck_diameter, neck_end = neck_options[0]

    end_diameters = sorted(
        at_station(main_diameters, main_end),
        key=lambda record: record["measurement"],
        reverse=True,
    )
    if len(end_diameters) < 2:
        return None
    tip_diameter, outlet_diameter = end_diameters[:2]

    taper_options: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
    for record in main_axials:
        if record is main_length:
            continue
        interval = record["xInterval"]
        if _interval_endpoint_distance(interval, main_end) > station_tolerance:
            continue
        other_station = (
            float(interval[0])
            if _near(float(interval[1]), main_end, station_tolerance)
            else float(interval[1])
        )
        bore_options = at_station(main_diameters, other_station)
        for bore in bore_options:
            if bore["measurement"] < outlet_diameter["measurement"]:
                taper_options.append((record, bore, other_station))
    if len(taper_options) != 1:
        return None
    taper_length, axial_bore, taper_start = taper_options[0]

    counterbore_depth_options = [
        record
        for record in start_axials
        if record is not head_length
        and record["measurement"] < head_length["measurement"]
    ]
    if len(counterbore_depth_options) != 1:
        return None
    counterbore_depth = counterbore_depth_options[0]

    radial_change = (outlet_diameter["measurement"] - axial_bore["measurement"]) / 2
    if radial_change <= 0 or taper_length["measurement"] <= 0:
        return None
    diameter_angle = math.degrees(math.atan2(radial_change, taper_length["measurement"]))
    taper_lines: list[Mapping[str, Any]] = []
    taper_line_angles: list[float] = []
    for item in geometry:
        if item.get("type") != "LINE":
            continue
        start = _point(item.get("start"))
        end = _point(item.get("end"))
        if start is None or end is None:
            continue
        x_interval = [min(start[0], end[0]), max(start[0], end[0])]
        if not (
            _near(x_interval[0], taper_start, station_tolerance)
            and _near(x_interval[1], main_end, station_tolerance)
        ):
            continue
        delta_x = abs(end[0] - start[0])
        delta_y = abs(end[1] - start[1])
        if delta_x <= station_tolerance or delta_y <= station_tolerance:
            continue
        line_angle = math.degrees(math.atan2(delta_y, delta_x))
        if math.isclose(line_angle, diameter_angle, rel_tol=0.0, abs_tol=0.05):
            taper_lines.append(item)
            taper_line_angles.append(line_angle)
    if len(taper_lines) != 2:
        return None
    outlet_taper_angle = _canonical_vector_value(sum(taper_line_angles) / len(taper_line_angles))
    if outlet_taper_angle is None:
        return None

    direct_dimensions: dict[str, tuple[Mapping[str, Any], float | str, str | None]] = {
        "mainLength": (main_length["source"], main_length["measurement"], "mm"),
        "headLength": (head_length["source"], head_length["measurement"], "mm"),
        "neckLength": (neck_length["source"], neck_length["measurement"], "mm"),
        "headLeftDiameter": (head_left["source"], head_left["measurement"], "mm"),
        "headRightDiameter": (head_right["source"], head_right["measurement"], "mm"),
        "neckDiameter": (neck_diameter["source"], neck_diameter["measurement"], "mm"),
        "tipDiameter": (tip_diameter["source"], tip_diameter["measurement"], "mm"),
        "counterboreDiameter": (counterbore["source"], counterbore["measurement"], "mm"),
        "counterboreDepth": (
            counterbore_depth["source"],
            counterbore_depth["measurement"],
            "mm",
        ),
        "axialBoreDiameter": (axial_bore["source"], axial_bore["measurement"], "mm"),
        "outletDiameter": (outlet_diameter["source"], outlet_diameter["measurement"], "mm"),
        "insertOuterDiameter": (insert_outer["source"], insert_outer["measurement"], "mm"),
        "insertLength": (insert_length["source"], insert_length["measurement"], "mm"),
        "insertThreadDesignation": (
            thread["source"],
            str(thread["source"]["text"]).upper(),
            None,
        ),
    }
    field_candidates = {
        field: _dimension_candidate(source, value=value, unit=unit)
        for field, (source, value, unit) in direct_dimensions.items()
    }
    field_candidates["outletTaperHalfAngle"] = {
        "status": "resolved",
        "value": outlet_taper_angle,
        "unit": "deg",
        "sourceType": "vector_derived",
        "sourceIds": [
            str(taper_length["source"].get("id", "dimension_unknown")),
            str(axial_bore["source"].get("id", "dimension_unknown")),
            str(outlet_diameter["source"].get("id", "dimension_unknown")),
            *(str(item.get("id", "geometry_unknown")) for item in taper_lines),
        ],
        "formula": "atan((outletDiameter-axialBoreDiameter)/(2*taperAxialLength))",
        "formulaInputs": {
            "taperAxialLength": taper_length["measurement"],
            "axialBoreDiameter": axial_bore["measurement"],
            "outletDiameter": outlet_diameter["measurement"],
        },
        "evidenceStrength": "geometry_cross_checked",
        "requiresModelReview": True,
        "requiresHumanConfirmation": True,
    }
    field_candidates["insertAxialOffset"] = _unresolved_vector_candidate(
        "assembly_datum_not_encoded_in_part_drawing"
    )
    field_candidates = {
        field: field_candidates[field]
        for field in _STEPPED_NOZZLE_RECIPE_FIELDS
    }

    resolved_values = {
        field: candidate["value"]
        for field, candidate in field_candidates.items()
        if candidate["status"] == "resolved"
    }
    resolved_values["units"] = "mm"
    tip_length = _canonical_vector_value(
        main_length["measurement"] - head_length["measurement"] - neck_length["measurement"]
    )
    return {
        "schemaVersion": "joyniu.dwg-vector-parameter-candidates.v1",
        "method": "deterministic_vector_topology",
        "authority": "evidence_only",
        "isAIResult": False,
        "usesOCR": False,
        "usesTemplateDefaults": False,
        "canPopulateProductionParameters": False,
        "requiresRemoteModelReview": True,
        "requiresHumanConfirmation": True,
        "recipeHint": {
            "partType": "stepped_tapered_nozzle",
            "recipeId": "stepped_tapered_nozzle_with_insert_v1",
            "sourceType": "vector_topology_match",
            "status": "candidate",
        },
        "fieldCandidates": field_candidates,
        "resolvedValues": resolved_values,
        "resolvedFieldCount": len(resolved_values) - 1,
        "unresolvedFields": ["insertAxialOffset"],
        "derivedMeasurements": {
            "tipLength": {
                "value": tip_length,
                "unit": "mm",
                "sourceType": "vector_derived",
                "sourceIds": [
                    str(main_length["source"].get("id", "dimension_unknown")),
                    str(head_length["source"].get("id", "dimension_unknown")),
                    str(neck_length["source"].get("id", "dimension_unknown")),
                ],
                "formula": "mainLength-headLength-neckLength",
            },
            "outletTaperAxialLength": {
                "value": taper_length["measurement"],
                "unit": "mm",
                "sourceType": "direct_dimension",
                "sourceIds": [str(taper_length["source"].get("id", "dimension_unknown"))],
            },
        },
        "topologyEvidence": {
            "independentAxialProfileCount": 2,
            "mainAxialInterval": [
                _canonical_vector_value(main_start),
                _canonical_vector_value(main_end),
            ],
            "insertAxialInterval": [
                _canonical_vector_value(insert_start),
                _canonical_vector_value(insert_end),
            ],
            "mirroredOutletTaperLineCount": len(taper_lines),
        },
    }


def _sanitize_remote_summary(
    audit: Mapping[str, Any],
    metadata: DWGSourceMetadata,
    converter_name: str,
) -> dict[str, Any]:
    """Remove every user-authored string from the model-facing summary."""

    layers = list(audit.get("layers", []))
    layer_ids = {
        str(layer.get("name", "")): f"layer_{index:04d}"
        for index, layer in enumerate(layers, start=1)
    }
    layouts = list(audit.get("layouts", []))
    layout_ids = {
        str(layout.get("name", "")): f"layout_{index:04d}"
        for index, layout in enumerate(layouts, start=1)
    }

    safe_dimensions: list[dict[str, Any]] = []
    for index, dimension in enumerate(audit.get("dimensions", []), start=1):
        literal = _safe_dimension_literal(dimension.get("text"))
        safe_dimensions.append(
            {
                "id": f"dimension_{index:05d}",
                "layoutId": layout_ids.get(str(dimension.get("layout", "")), "layout_unknown"),
                "layerId": layer_ids.get(str(dimension.get("layer", "")), "layer_unknown"),
                "dimensionType": max(0, min(15, int(dimension.get("dimensionType", 0)))),
                "measurement": _safe_number(dimension.get("measurement")),
                "text": literal,
                "textMode": (
                    "measurement-placeholder"
                    if literal == "<>"
                    else "diameter-measurement"
                    if literal == "Ø<>"
                    else "literal"
                    if literal is not None
                    else "omitted-untrusted"
                ),
                "defpoints": {
                    key: _point(point)
                    for key, point in dimension.get("defpoints", {}).items()
                    if key in _POINT_ATTRS and _point(point) is not None
                },
            }
        )

    safe_geometry: list[dict[str, Any]] = []
    permitted_geometry_fields = {
        "start", "end", "center", "radius", "startAngle", "endAngle", "points",
        "closed", "pointCount", "pointsTruncated", "majorAxis", "ratio", "startParameter",
        "endParameter", "controlPoints", "location", "detailUnavailable",
    }
    for index, item in enumerate(audit.get("geometry", []), start=1):
        record = {
            "id": f"geometry_{index:05d}",
            "type": _entity_type(item.get("type")),
            "layoutId": layout_ids.get(str(item.get("layout", "")), "layout_unknown"),
            "layerId": layer_ids.get(str(item.get("layer", "")), "layer_unknown"),
        }
        record.update({key: item[key] for key in permitted_geometry_fields if key in item})
        safe_geometry.append(record)

    converter_family = (
        converter_name
        if converter_name in {"libredwg-dwgread", "libredwg-dwg2dxf", "configured"}
        else "custom"
    )
    dxf_version = str(audit.get("dxfVersion", ""))
    if not re.fullmatch(r"AC\d{4}", dxf_version):
        dxf_version = "unknown"
    safe: dict[str, Any] = {
        "schemaVersion": "joyniu.dwg-vector-summary.v1",
        "source": {
            "sizeBytes": metadata.size_bytes,
            "sha256": metadata.sha256,
            "signature": metadata.signature,
            "version": metadata.version,
        },
        "converterFamily": converter_family,
        "dxfVersion": dxf_version,
        "units": audit.get("units"),
        "sourceEntityCount": int(audit.get("sourceEntityCount", 0)),
        "entityCount": int(audit.get("entityCount", 0)),
        "sourceEntityTypeCounts": _safe_count_map(audit.get("sourceEntityTypeCounts", {})),
        "entityTypeCounts": _safe_count_map(audit.get("entityTypeCounts", {})),
        "layerCount": len(layers),
        "layers": [
            {
                "id": layer_ids[str(layer.get("name", ""))],
                "entityCount": max(0, int(layer.get("entityCount", 0))),
            }
            for layer in layers
        ],
        "modelspaceBoundingBox": audit.get("modelspaceBoundingBox"),
        "layouts": [
            {
                "id": layout_ids[str(layout.get("name", ""))],
                "kind": layout.get("kind")
                if layout.get("kind") in {"modelspace", "paperspace"}
                else "unknown",
                "sourceEntityCount": max(0, int(layout.get("sourceEntityCount", 0))),
                "entityCount": max(0, int(layout.get("entityCount", 0))),
                "entityTypeCounts": _safe_count_map(layout.get("entityTypeCounts", {})),
                "boundingBox": layout.get("boundingBox"),
            }
            for layout in layouts
        ],
        "dimensions": safe_dimensions,
        "geometry": safe_geometry,
        "warnings": list(audit.get("warningCodes", [])),
    }
    vector_candidates = _build_stepped_nozzle_vector_candidates(
        safe_dimensions,
        safe_geometry,
        safe.get("units"),
    )
    if vector_candidates is not None:
        safe["vectorParameterCandidates"] = vector_candidates
    if "truncated" in audit:
        safe["truncated"] = dict(audit["truncated"])
    return safe


def _build_summary(document: Any, config: DWGPreprocessConfig, warnings: Sequence[str]) -> dict[str, Any]:
    try:
        from ezdxf import units  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - guarded by _import_ezdxf
        raise DWGParserUnavailableError("ezdxf unit support is unavailable") from exc

    source_entity_counts: Counter[str] = Counter()
    entity_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    dimensions: list[dict[str, Any]] = []
    geometry: list[dict[str, Any]] = []
    layout_summaries: list[dict[str, Any]] = []
    modelspace_entities: list[Any] = []
    source_entity_total = 0
    expanded_state = {"count": 0}

    try:
        layouts = list(document.layouts)
    except Exception as exc:
        raise DWGParseError("DXF layout table is invalid") from exc
    if len(layouts) > config.max_layout_records:
        raise DWGResourceLimitError("DXF layout count exceeds the configured limit")

    geometry_point_total = 0
    for layout in layouts:
        layout_name = _safe_text(getattr(layout, "name", "unknown"), 255)
        source_entities = list(layout)
        source_entity_total += len(source_entities)
        if source_entity_total > config.max_entities:
            raise DWGResourceLimitError("DXF entity count exceeds the configured limit")
        for source_entity in source_entities:
            source_entity_counts[source_entity.dxftype()] += 1
        expanded = _expand_layout_entities(source_entities, config, expanded_state)
        entities = [entity for entity, _layer in expanded]
        local_counts: Counter[str] = Counter()
        for entity, effective_layer in expanded:
            entity_type = entity.dxftype()
            entity_counts[entity_type] += 1
            local_counts[entity_type] += 1
            layer_counts[effective_layer] += 1
            if entity_type == "DIMENSION" and len(dimensions) < config.max_dimension_items:
                dimensions.append(_dimension_record(entity, layout_name, effective_layer))
            if entity_type in _GEOMETRY_TYPES and len(geometry) < config.max_geometry_items:
                record = _geometry_record(entity, layout_name, effective_layer)
                if record is not None:
                    geometry_point_total += int(record.get("pointCount", 0))
                    if geometry_point_total > config.max_geometry_points:
                        raise DWGResourceLimitError(
                            "DXF geometry point count exceeds the configured limit"
                        )
                    geometry.append(record)
        is_modelspace = bool(getattr(layout, "is_modelspace", False))
        if is_modelspace:
            modelspace_entities = entities
        layout_summaries.append(
            {
                "name": layout_name,
                "kind": "modelspace" if is_modelspace else "paperspace",
                "sourceEntityCount": len(source_entities),
                "entityCount": len(expanded),
                "entityTypeCounts": dict(sorted(local_counts.items())),
                "boundingBox": _bbox_dict(entities),
            }
        )

    dimension_omitted = max(0, entity_counts["DIMENSION"] - len(dimensions))
    geometry_count = sum(entity_counts[entity_type] for entity_type in _GEOMETRY_TYPES)
    geometry_omitted = max(0, geometry_count - len(geometry))
    try:
        unit_code = int(document.units)
        unit_name = units.unit_name(unit_code)
    except Exception:
        unit_code, unit_name = 0, "Unitless"
    warning_codes: list[str] = []
    for warning in warnings:
        if warning == "DXF required tolerant recovery":
            warning_codes.append("dxf_recovered")
        elif warning == "DXF required recovery and still has audit errors":
            warning_codes.append("dxf_recovered_with_audit_errors")
    summary: dict[str, Any] = {
        "schemaVersion": "joyniu.dwg-vector-summary.v1",
        "dxfVersion": _safe_text(getattr(document, "dxfversion", ""), 32),
        "units": {"code": unit_code, "name": unit_name},
        "sourceEntityCount": source_entity_total,
        "entityCount": expanded_state["count"],
        "sourceEntityTypeCounts": dict(sorted(source_entity_counts.items())),
        "entityTypeCounts": dict(sorted(entity_counts.items())),
        "layerCount": len(layer_counts),
        "layers": [
            {"name": name, "entityCount": count}
            for name, count in sorted(layer_counts.items(), key=lambda item: item[0].casefold())
        ],
        "modelspaceBoundingBox": _bbox_dict(modelspace_entities),
        "layouts": layout_summaries,
        "dimensions": dimensions,
        "geometry": geometry,
        "warnings": [_safe_text(warning, 512) for warning in warnings],
        "warningCodes": warning_codes,
    }
    if dimension_omitted or geometry_omitted:
        summary["truncated"] = {
            "dimensions": dimension_omitted,
            "geometryItems": geometry_omitted,
        }
    return _fit_summary(summary, config.max_summary_bytes)


def _bound_png(path: Path, config: DWGPreprocessConfig) -> bytes:
    try:
        from PIL import Image  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        raise DWGRenderError("Pillow is required to bound the rendered PNG") from exc
    try:
        with Image.open(path) as image:
            image.load()
            working = image.convert("RGB")
    except Exception as exc:
        raise DWGRenderError("rendered drawing is not a valid image") from exc

    for _ in range(9):
        output = io.BytesIO()
        working.save(output, format="PNG", optimize=True)
        payload = output.getvalue()
        if len(payload) <= config.max_png_bytes:
            return payload
        width = max(256, int(working.width * 0.75))
        height = max(256, int(working.height * 0.75))
        if (width, height) == working.size:
            break
        working = working.resize((width, height), Image.Resampling.LANCZOS)
    raise DWGResourceLimitError("rendered PNG exceeds the configured size limit")


def _render_document(
    document: Any,
    output_path: Path,
    config: DWGPreprocessConfig,
) -> tuple[bytes, int, int]:
    try:
        from ezdxf.addons.drawing import matplotlib as ezdxf_matplotlib  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        raise DWGRenderError("ezdxf matplotlib rendering support is unavailable") from exc
    try:
        from PIL import Image  # type: ignore
    except (ImportError, ModuleNotFoundError) as exc:
        raise DWGRenderError("Pillow is required to compose DXF layouts") from exc

    layouts = [layout for layout in document.layouts if len(layout)]
    if not layouts:
        raise DWGRenderError("DXF has no drawable entities")
    omitted = max(0, len(layouts) - config.max_render_layouts)
    layouts = layouts[: config.max_render_layouts]
    # qsave may include tight-bbox padding. Keep each tile below a calculated
    # pixel budget, then constrain it again before the contact sheet is built.
    tile_area = config.max_render_pixels / len(layouts)
    maximum_edge = int(config.render_size_inches * config.render_dpi)
    rendered: list[Any] = []
    try:
        for index, layout in enumerate(layouts, start=1):
            bounds = _bbox_dict(list(layout))
            size = bounds.get("size", [1.0, 1.0]) if bounds else [1.0, 1.0]
            width, height = max(float(size[0]), 1e-6), max(float(size[1]), 1e-6)
            aspect = max(0.2, min(5.0, width / height))
            if aspect >= 1:
                pixel_width = min(maximum_edge, int(math.sqrt(tile_area * aspect)))
                pixel_height = max(256, int(pixel_width / aspect))
            else:
                pixel_height = min(maximum_edge, int(math.sqrt(tile_area / aspect)))
                pixel_width = max(256, int(pixel_height * aspect))
            pixel_width, pixel_height = max(256, pixel_width), max(256, pixel_height)
            tile_path = output_path.with_name(f"preview-{index:03d}.png")
            ezdxf_matplotlib.qsave(
                layout,
                tile_path,
                bg="#FFFFFF",
                fg="#000000",
                dpi=config.render_dpi,
                backend="agg",
                size_inches=(pixel_width / config.render_dpi, pixel_height / config.render_dpi),
            )
            with Image.open(tile_path) as image:
                image.load()
                tile = image.convert("RGB")
            tile.thumbnail((pixel_width, pixel_height), Image.Resampling.LANCZOS)
            rendered.append(tile)
    except Exception as exc:
        raise DWGRenderError(f"DXF preview rendering failed ({type(exc).__name__})") from exc

    columns = max(1, int(math.ceil(math.sqrt(len(rendered)))))
    rows = int(math.ceil(len(rendered) / columns))
    cell_width = max(tile.width for tile in rendered)
    cell_height = max(tile.height for tile in rendered)
    canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    for index, tile in enumerate(rendered):
        column, row = index % columns, index // columns
        x = column * cell_width + (cell_width - tile.width) // 2
        y = row * cell_height + (cell_height - tile.height) // 2
        canvas.paste(tile, (x, y))
    if canvas.width * canvas.height > config.max_render_pixels:
        scale = math.sqrt(config.max_render_pixels / (canvas.width * canvas.height))
        canvas = canvas.resize(
            (max(256, int(canvas.width * scale)), max(256, int(canvas.height * scale))),
            Image.Resampling.LANCZOS,
        )
    canvas.save(output_path, format="PNG", optimize=True)
    return _bound_png(output_path, config), len(rendered), omitted


def preprocess_dwg(
    payload: bytes,
    filename: str = "drawing.dwg",
    *,
    config: DWGPreprocessConfig | None = None,
) -> DWGPreprocessResult:
    """Convert and inspect one DWG payload.

    The result owns byte copies only.  All source, converter output and render
    files have already been deleted when this function returns.
    """

    active_config = config or DWGPreprocessConfig()
    if not isinstance(payload, bytes) or not payload:
        raise DWGInputError("DWG payload is empty")
    if len(payload) > active_config.max_input_bytes:
        raise DWGInputTooLargeError("DWG payload exceeds the configured size limit")
    metadata = _source_metadata(payload, filename)
    commands = _resolve_converter_commands(active_config)
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="joyniu-dwg-") as directory:
        workspace = Path(directory)
        input_path = workspace / "source.dwg"
        output_path = workspace / "converted.dxf"
        preview_path = workspace / "preview.png"
        input_path.write_bytes(payload)

        converted_path: Path | None = None
        converter_name = ""
        for command in commands:
            candidate, failure = _run_converter(command, input_path, output_path, active_config)
            if failure is None:
                converted_path = candidate
                converter_name = command.name
                break
            failures.append(f"{command.name}: {failure}")
        if converted_path is None:
            raise DWGConversionError("; ".join(failures) or "all DWG converters failed")

        try:
            dxf_bytes = converted_path.read_bytes()
        except OSError as exc:
            raise DWGConversionError("converted DXF could not be read") from exc
        if len(dxf_bytes) > active_config.max_dxf_bytes:
            raise DWGResourceLimitError("converted DXF exceeds the configured size limit")

        document, warnings = _read_document(converted_path)
        audit_summary = _build_summary(document, active_config, warnings)
        png_bytes, rendered_layouts, omitted_layouts = _render_document(
            document,
            preview_path,
            active_config,
        )
        audit_summary["source"] = metadata.to_dict()
        audit_summary["converter"] = converter_name
        audit_summary["renderedLayoutCount"] = rendered_layouts
        audit_summary["omittedRenderLayoutCount"] = omitted_layouts
        if omitted_layouts:
            audit_summary.setdefault("warnings", []).append(
                "layout preview count exceeded the configured render limit"
            )
            audit_summary.setdefault("warningCodes", []).append("render_layouts_omitted")
        audit_summary = _fit_summary(audit_summary, active_config.max_summary_bytes)
        summary = _sanitize_remote_summary(audit_summary, metadata, converter_name)
        summary["renderedLayoutCount"] = rendered_layouts
        summary["omittedRenderLayoutCount"] = omitted_layouts
        summary = _fit_summary(summary, active_config.max_summary_bytes)
        return DWGPreprocessResult(
            original_metadata=metadata,
            converter=converter_name,
            dxf_bytes=dxf_bytes,
            png_bytes=png_bytes,
            summary=summary,
            audit_summary=audit_summary,
        )


__all__ = [
    "DWGConversionError",
    "DWGConversionTimeoutError",
    "DWGConverterCommand",
    "DWGConverterUnavailableError",
    "DWGInputError",
    "DWGInputTooLargeError",
    "DWGParseError",
    "DWGParserUnavailableError",
    "DWGPreprocessConfig",
    "DWGPreprocessError",
    "DWGPreprocessResult",
    "DWGRenderError",
    "DWGResourceLimitError",
    "DWGSourceMetadata",
    "dwg_preprocessor_status",
    "preprocess_dwg",
]
