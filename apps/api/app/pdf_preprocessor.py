"""Safe local preprocessing for PDF engineering drawings."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import importlib.util
import io
import math
from pathlib import Path
import re
from typing import Any

class PDFPreprocessError(RuntimeError):
    code = "pdf_preprocess_failed"
    def __init__(self, message: str | None = None): super().__init__(message or self.code)
    def to_dict(self) -> dict[str, str]: return {"errorType": self.code, "message": str(self)}
class PDFInputError(PDFPreprocessError): code = "invalid_pdf_input"
class PDFInputTooLargeError(PDFPreprocessError): code = "pdf_input_too_large"
class PDFPasswordError(PDFPreprocessError): code = "pdf_password_protected"
class PDFParserUnavailableError(PDFPreprocessError): code = "pdf_parser_unavailable"
class PDFRenderError(PDFPreprocessError): code = "pdf_render_failed"
class PDFResourceLimitError(PDFPreprocessError): code = "pdf_resource_limit_exceeded"

@dataclass(frozen=True, slots=True)
class PDFPreprocessConfig:
    timeout_seconds: float = 45.0
    max_input_bytes: int = 64 * 1024 * 1024
    max_page_count: int = 20
    max_total_pixels: int = 24_000_000
    max_page_pixels: int = 8_000_000
    max_png_bytes: int = 12 * 1024 * 1024
    max_summary_bytes: int = 512 * 1024
    render_dpi: int = 240
    render_page_strategy: str = "all"
    selected_pages: tuple[int, ...] = ()
    def __post_init__(self):
        if self.render_page_strategy not in {"all", "first", "pages"}: raise ValueError("invalid PDF page strategy")
        if any(float(v) <= 0 for v in (self.timeout_seconds, self.max_input_bytes, self.max_page_count, self.max_total_pixels, self.max_page_pixels, self.max_png_bytes, self.max_summary_bytes, self.render_dpi)): raise ValueError("PDF limits must be positive")

@dataclass(frozen=True, slots=True)
class PDFPreprocessResult:
    original_metadata: dict[str, Any]
    page_count_rendered: int
    page_count_omitted: int
    png_bytes: bytes
    summary: dict[str, Any]
    audit_summary: dict[str, Any]
    page_evidence: tuple[dict[str, Any], ...]

_MEASUREMENT = re.compile(r"(?P<prefix>[ØφΦRrMm])?\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>mm|毫米)?", re.I)

def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None

def _drawing_summary(page: Any, max_items: int = 5000) -> tuple[list[dict[str, Any]], int]:
    """Extract bounded, non-semantic PDF path geometry for audit/mapping."""
    items: list[dict[str, Any]] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return items, 0
    total = len(drawings)
    for index, drawing in enumerate(drawings[:max_items]):
        rect = drawing.get("rect")
        bbox = [float(v) for v in (rect or (0, 0, 0, 0)) if _safe_number(v) is not None]
        if len(bbox) != 4:
            continue
        paths = []
        for op in drawing.get("items", ())[:64]:
            if not op:
                continue
            kind = str(op[0])
            values = []
            for value in op[1:]:
                if hasattr(value, "x") and hasattr(value, "y"):
                    values.extend([float(value.x), float(value.y)])
                elif isinstance(value, (tuple, list)):
                    values.extend(float(v) for v in value[:4] if _safe_number(v) is not None)
            paths.append({"kind": kind, "values": values[:8]})
        items.append({"id": f"pdf-path-{index + 1}", "bbox": bbox, "pathCount": len(paths), "paths": paths})
    return items, total

def pdf_preprocessor_status() -> dict[str, Any]:
    available = importlib.util.find_spec("fitz") is not None and importlib.util.find_spec("PIL") is not None
    return {"available": available, "engine": "PyMuPDF" if available else "unavailable", "dependencies": {"PyMuPDF": importlib.util.find_spec("fitz") is not None, "Pillow": importlib.util.find_spec("PIL") is not None}, "pipeline": "PDF -> page text evidence + PNG previews", "rawPdfSentToAI": False}

def _require_deps():
    try:
        import fitz  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
        return fitz, Image, ImageDraw
    except ModuleNotFoundError as exc: raise PDFParserUnavailableError("PyMuPDF and Pillow are required") from exc

def preprocess_pdf(payload: bytes, filename: str = "drawing.pdf", *, config: PDFPreprocessConfig | None = None) -> PDFPreprocessResult:
    config = config or PDFPreprocessConfig()
    if not payload: raise PDFInputError("PDF file is empty")
    if len(payload) > config.max_input_bytes: raise PDFInputTooLargeError("PDF exceeds local upload limit")
    if not payload.startswith(b"%PDF-"): raise PDFInputError("file does not have a PDF signature")
    fitz, Image, ImageDraw = _require_deps()
    try: doc = fitz.open(stream=payload, filetype="pdf")
    except Exception as exc: raise PDFInputError("PDF cannot be opened") from exc
    if doc.needs_pass: doc.close(); raise PDFPasswordError("PDF is password protected")
    total = doc.page_count
    pages = [0] if config.render_page_strategy == "first" else list(config.selected_pages) if config.render_page_strategy == "pages" else list(range(total))
    pages = [p for p in pages if 0 <= p < total][:config.max_page_count]
    omitted = max(0, total - len(pages))
    rendered: list[Image.Image] = []; evidence=[]; total_pixels=0
    try:
        for pno in pages:
            page = doc.load_page(pno); rect = page.rect
            scale = config.render_dpi / 72.0
            width, height = max(1, math.ceil(rect.width*scale)), max(1, math.ceil(rect.height*scale))
            pixels = width * height
            if pixels > config.max_page_pixels or total_pixels + pixels > config.max_total_pixels: omitted += 1; continue
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            rendered.append(image); total_pixels += pixels
            blocks=[]; candidates=[]
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if not text: continue
                    bbox = [float(x) for x in line.get("bbox", [0,0,0,0])]
                    blocks.append({"bbox": bbox, "text": text[:512]})
                    for match in _MEASUREMENT.finditer(text):
                        candidates.append({"sourceType":"document_text", "page":pno+1, "bbox":bbox, "rawText":match.group(0)[:64], "normalizedValue":float(match.group("value").replace(",", ".")), "dimensionKind": {"Ø":"diameter","φ":"diameter","Φ":"diameter","R":"radius","r":"radius"}.get(match.group("prefix") or "length","length"), "unit":match.group("unit"), "needsReview":True})
            drawings, drawing_count = _drawing_summary(page)
            # Text alone is not a CAD dimension. Geometry is retained as a
            # bounded, coordinate-bearing mapping aid and remains untrusted.
            for candidate in candidates:
                candidate["nearbyGeometryIds"] = [item["id"] for item in drawings if not (candidate["bbox"][2] < item["bbox"][0] - 48 or candidate["bbox"][0] > item["bbox"][2] + 48 or candidate["bbox"][3] < item["bbox"][1] - 48 or candidate["bbox"][1] > item["bbox"][3] + 48)][:8]
            evidence.append({"page":pno+1,"width":float(rect.width),"height":float(rect.height),"rotation":int(page.rotation),"textBlocks":blocks,"measurementTextCandidates":candidates,"geometry":drawings,"geometryCount":drawing_count,"warnings":[]})
    except PDFPreprocessError: raise
    except Exception as exc: raise PDFRenderError("PDF page rendering failed") from exc
    finally: doc.close()
    if not rendered: raise PDFRenderError("no PDF pages could be rendered")
    sheet_w = max(i.width for i in rendered); thumb_w = min(1600, sheet_w); thumbs=[]
    for i, img in enumerate(rendered):
        ratio=thumb_w/img.width; thumbs.append(img.resize((thumb_w, max(1, int(img.height*ratio)))))
    gap=24; sheet_h=sum(i.height for i in thumbs)+gap*(len(thumbs)-1); sheet=Image.new("RGB", (thumb_w, sheet_h), "white"); y=0
    for img in thumbs: sheet.paste(img,(0,y)); y += img.height+gap
    out=io.BytesIO(); sheet.save(out, format="PNG", optimize=True)
    png=out.getvalue()
    if len(png)>config.max_png_bytes: raise PDFResourceLimitError("PDF preview exceeds size limit")
    summary={"pageCount":total,"renderedPageCount":len(rendered),"omittedPageCount":omitted,"pages":[{"page":e["page"],"width":e["width"],"height":e["height"],"rotation":e["rotation"],"textBlockCount":len(e["textBlocks"]),"measurementCandidateCount":len(e["measurementTextCandidates"]),"geometryCount":e["geometryCount"],"geometry":e["geometry"][:256]} for e in evidence], "evidenceType": "pdf_text_and_geometry_candidates"}
    if len(str(summary).encode())>config.max_summary_bytes: raise PDFResourceLimitError("PDF summary exceeds size limit")
    metadata={"filename":Path(filename).name[:255] or "drawing.pdf","sizeBytes":len(payload),"sha256":hashlib.sha256(payload).hexdigest(),"pageCount":total,"encrypted":False,"pdfVersion":None}
    return PDFPreprocessResult(metadata,len(rendered),omitted,png,summary,{"pages":evidence},tuple(evidence))

__all__=["PDFPreprocessError","PDFInputError","PDFInputTooLargeError","PDFPasswordError","PDFParserUnavailableError","PDFRenderError","PDFResourceLimitError","PDFPreprocessConfig","PDFPreprocessResult","preprocess_pdf","pdf_preprocessor_status"]
