"""Lazy source previews in the same pixel frame as the CAD source reader."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import re
import threading
from typing import Any, Mapping
from uuid import uuid4

from .cad_agent_store import CadRunStore

_LOCKS = [threading.Lock() for _ in range(16)]
_VERSION = "v1"
_MAX_PIXELS = 80_000_000


class SourcePreviewError(ValueError):
    pass


def _source(store: CadRunStore, record: Mapping[str, Any], index: int):
    files = record.get("files") or []
    if type(index) is not int or not 0 <= index < len(files):
        raise SourcePreviewError("Source drawing not found")
    item = files[index]
    try:
        path = store.checked_path(item["path"])
    except (ValueError, KeyError, TypeError):
        raise SourcePreviewError("Source drawing is missing") from None
    digest = item.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise SourcePreviewError("Source drawing identity is missing")
    directory = store.directory(record["runId"]) / "source-previews" / f"{_VERSION}-{digest}"
    if not directory.resolve().is_relative_to(store.root):
        raise SourcePreviewError("Source preview is outside its workspace")
    return item, path, digest, directory


def source_download(store: CadRunStore, record: Mapping[str, Any], index: int):
    item, path, _, _ = _source(store, record, index)
    return path, Path(item.get("filename") or path.name).name


def _atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _prepared_hash(record: Mapping[str, Any], index: int) -> str | None:
    transcription = record.get("sourceTranscription") or (record.get("state") or {}).get("sourceTranscription") or {}
    return next((item.get("sha256") for item in transcription.get("preparedFiles", [])
                 if isinstance(item, Mapping) and item.get("fileIndex") == index), None)


def _metadata(path: Path, suffix: str) -> dict[str, Any]:
    from PIL import Image
    if suffix == ".pdf":
        import fitz
        from .pdf_preprocessor import PDFPreprocessConfig
        config = PDFPreprocessConfig()
        with fitz.open(path) as document:
            if document.needs_pass:
                raise SourcePreviewError("PDF is password protected")
            pages, included, pixels = [], [], 0
            for index in range(min(document.page_count, config.max_page_count)):
                rect = document.load_page(index).rect
                width, height = (max(1, math.ceil(value * config.render_dpi / 72)) for value in (rect.width, rect.height))
                accepted = width * height <= config.max_page_pixels and pixels + width * height <= config.max_total_pixels
                if accepted:
                    included.append((index, width, height))
                    pixels += width * height
                preview_width = min(1600, width)
                pages.append({"page": index + 1, "width": preview_width,
                              "height": max(1, int(height * preview_width / width)), "preparedRegion": None})
            # Match preprocess_pdf's page selection, thumbnail sizes and 24 px gaps.
            if included:
                sheet_width = min(1600, max(width for _, width, _ in included))
                sizes = [(index, max(1, int(height * sheet_width / width))) for index, width, height in included]
                sheet_height = sum(height for _, height in sizes) + 24 * (len(sizes) - 1)
                top = 0
                for index, height in sizes:
                    pages[index]["preparedRegion"] = [0, top / sheet_height, 1, height / sheet_height]
                    top += height + 24
            return {"pages": pages, "pageCount": document.page_count,
                    "omittedPageCount": max(0, document.page_count - len(pages))}
    if suffix in {".dwg", ".dxf"}:
        return {"pages": [{"page": 1, "width": None, "height": None, "preparedRegion": [0, 0, 1, 1]}]}
    with Image.open(path) as image:
        if image.width * image.height > _MAX_PIXELS:
            raise SourcePreviewError("Source image exceeds the preview pixel limit")
        return {"pages": [{"page": 1, "width": image.width, "height": image.height, "preparedRegion": [0, 0, 1, 1]}]}


def _cached_metadata(store: CadRunStore, record: Mapping[str, Any], index: int):
    item, path, digest, directory = _source(store, record, index)
    manifest = directory / "manifest.json"
    with _LOCKS[int(digest[:2], 16) % len(_LOCKS)]:
        if manifest.exists():
            cached = json.loads(store.checked_path(manifest).read_text())
            # Metadata failures can recover when a parser is restored. Reading
            # image/PDF headers is cheap; successful metadata stays cached.
            if cached.get("pages") or not cached.get("error"):
                return cached, item, path, digest, directory
        try:
            metadata = _metadata(path, Path(item.get("filename") or path.name).suffix.lower())
        except Exception:
            metadata = {"pages": [], "error": "source_preview_unavailable"}
        _atomic(manifest, json.dumps(metadata, allow_nan=False).encode())
        return metadata, item, path, digest, directory


def source_documents(store: CadRunStore, record: Mapping[str, Any], prefix: str) -> list[dict[str, Any]]:
    documents = []
    for index, item in enumerate(record.get("files") or []):
        base = f"{prefix}/cad-agent/runs/{record['runId']}/{record['revision']}/sources/{index}"
        access = f"?access={record['downloadToken']}"
        document = {"id": f"source-{index}", "filename": item.get("filename"), "sha256": item.get("sha256"),
                    "pages": [], "downloadUrl": f"{base}/download{access}"}
        try:
            metadata, _, path, digest, _ = _cached_metadata(store, record, index)
            prepared_hash = _prepared_hash(record, index)
            if path.suffix.lower() not in {".pdf", ".dwg", ".dxf"}:
                prepared_hash = digest
            for entry in metadata["pages"]:
                page = {**entry, "id": f"source-{index}-page-{entry['page']}",
                        "url": f"{base}/pages/{entry['page']}{access}", "preparedFileIndex": index,
                        "preparedSha256": prepared_hash}
                # DXF currently reaches the agent as a native file, not this
                # rendered PNG. Its original hash cannot identify an image
                # coordinate frame, even if preparedFiles contains that hash.
                if path.suffix.lower() == ".dxf":
                    page.update({"preparedFileIndex": None, "preparedSha256": None, "preparedRegion": None})
                document["pages"].append(page)
            for key in ("pageCount", "omittedPageCount", "error"):
                if key in metadata:
                    document[key] = metadata[key]
        except (ValueError, OSError, KeyError, TypeError):
            document["error"] = "source_preview_unavailable"
        documents.append(document)
    return documents


def source_preview(store: CadRunStore, record: Mapping[str, Any], index: int, page: int) -> Path:
    metadata, item, path, digest, directory = _cached_metadata(store, record, index)
    if type(page) is not int or not any(entry["page"] == page for entry in metadata["pages"]):
        raise SourcePreviewError("Source drawing page not found")
    target = directory / f"page-{page}.png"
    with _LOCKS[int(digest[:2], 16) % len(_LOCKS)]:
        if target.exists():
            return store.checked_path(target)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise SourcePreviewError("Source drawing no longer matches its saved identity")
        suffix = Path(item.get("filename") or path.name).suffix.lower()
        try:
            from PIL import Image
            if suffix == ".pdf":
                from .pdf_preprocessor import preprocess_pdf, PDFPreprocessConfig
                prepared = preprocess_pdf(payload, item["filename"], config=PDFPreprocessConfig(
                    render_page_strategy="pages", selected_pages=(page - 1,)))
                png = prepared.png_bytes
            elif suffix == ".dwg":
                from .dwg_preprocessor import preprocess_dwg
                png = preprocess_dwg(payload, item["filename"]).png_bytes
            elif suffix == ".dxf":
                import ezdxf
                from .dwg_preprocessor import _render_document, DWGPreprocessConfig
                directory.mkdir(parents=True, exist_ok=True)
                drawing = ezdxf.readfile(path)
                png, _, _ = _render_document(drawing, directory / "layout.png", DWGPreprocessConfig())
            else:
                # Reader uses the stored pixels as-is; EXIF autorotation would
                # move annotation boxes away from their corresponding text.
                with Image.open(io.BytesIO(payload)) as opened:
                    if opened.width * opened.height > _MAX_PIXELS:
                        raise SourcePreviewError("Source image exceeds the preview pixel limit")
                    output = io.BytesIO()
                    rgba = opened.convert("RGBA")
                    canvas = Image.new("RGB", rgba.size, "white")
                    canvas.paste(rgba, mask=rgba.getchannel("A"))
                    canvas.save(output, format="PNG")
                    png = output.getvalue()
            _atomic(target, png)
            with Image.open(io.BytesIO(png)) as rendered:
                for entry in metadata["pages"]:
                    if entry["page"] == page:
                        entry.update({"width": rendered.width, "height": rendered.height})
            metadata.pop("error", None)
            _atomic(directory / "manifest.json", json.dumps(metadata, allow_nan=False).encode())
        except Exception as exc:
            # Preserve the known page and URL so an explicit image retry can
            # recover a transient renderer/converter failure. Do not retry
            # expensive rendering from the job polling endpoint.
            metadata["error"] = "source_preview_unavailable"
            _atomic(directory / "manifest.json", json.dumps(metadata, allow_nan=False).encode())
            raise SourcePreviewError("Source drawing preview is unavailable") from exc
    return store.checked_path(target)
