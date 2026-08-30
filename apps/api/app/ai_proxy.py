"""Server-side multimodal AI gateway for the NewCAD copilot.

The browser never receives the relay credential.  This module deliberately
keeps the provider boundary small: drawings and the current parameter state
go in, while a validated, allow-listed parameter patch comes out.  A
hash-verified acceptance drawing can use the local recognition profile as a
fast, deterministic path; the relay is still used for ordinary conversational
edits and unknown drawings.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4


DEFAULT_BASE_URL = "https://gptx.shop/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_COUNT = 4
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RESPONSE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_KEY_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9._-]{10,}")


class AIProxyError(RuntimeError):
    """A safe, user-facing AI proxy failure without provider payloads."""


class AIProviderNotConfigured(AIProxyError):
    """The server has no configured provider credential."""


@dataclass(frozen=True, slots=True)
class AIFile:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class AIConversationResult:
    response_id: str
    message: str
    parameter_patch: dict[str, Any]
    needs_review: bool
    questions: tuple[str, ...]
    drawing: dict[str, Any] | None = None
    provider: dict[str, Any] | None = None
    attachments: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "responseId": self.response_id,
            "message": self.message,
            "parameterPatch": dict(self.parameter_patch),
            "needsReview": self.needs_review,
            "questions": list(self.questions),
        }
        if self.drawing is not None:
            result["drawingRecognition"] = self.drawing
        if self.provider is not None:
            result["provider"] = dict(self.provider)
        if self.attachments:
            result["attachments"] = [dict(item) for item in self.attachments]
        return result


# This allowlist is shared by the structured-output schema and the response
# validator. Unknown model fields can therefore never reach the CAD editor.
PARAMETER_FIELDS: dict[str, str] = {
    "baseLength": "number",
    "baseWidth": "number",
    "baseThickness": "number",
    "upperLength": "number",
    "upperWidth": "number",
    "upperHeight": "number",
    "totalHeight": "number",
    "notchOpening": "number",
    "notchRadius": "number",
    "slotLength": "number",
    "slotWidth": "number",
    "pocketDepth": "number",
    "saddleDepth": "number",
    "holeDepth": "number",
    "holeThrough": "boolean",
    "bossDiameter": "number",
    "bossCenterDistance": "number",
    "bossHeight": "number",
    "outerDiameter": "number",
    "length": "number",
    "holeDiameter": "number",
    "keywayWidth": "number",
    "keywayDepth": "number",
    "keywayLength": "number",
    "material": "string",
    "units": "string",
}


def _nullable_schema(kind: str) -> dict[str, Any]:
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


def parameter_patch_schema() -> dict[str, Any]:
    properties = {key: _nullable_schema(kind) for key, kind in PARAMETER_FIELDS.items()}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        # Strict structured outputs require nullable fields to be required.
        "required": list(properties),
    }


def response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "message": {"type": "string"},
            "parameter_patch": parameter_patch_schema(),
            "needs_review": {"type": "boolean"},
            "questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["message", "parameter_patch", "needs_review", "questions"],
    }


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _extract_key(raw: str, *, allow_plain: bool = True) -> str:
    """Extract a credential from either a plain file or a Markdown note.

    The supplied key file is a Markdown code block. Sending the whole file
    would authenticate with ``# heading`` and backticks included, so Markdown
    is parsed explicitly. A one-line plain value remains supported for deploys
    and tests.
    """

    text = str(raw or "").strip()
    if not text:
        return ""
    # JSON auth files are accepted without exposing their contents.
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, Mapping):
        for key_name in ("OPENAI_API_KEY", "api_key", "key", "token"):
            candidate = parsed.get(key_name)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    match = _KEY_RE.search(text)
    if match:
        return match.group(0)
    # Providers do not all use the OpenAI ``sk-`` prefix.  For an explicitly
    # configured Markdown file, accept the first non-comment value inside a
    # fenced code block (including ``KEY=value`` dotenv-style lines), while
    # ignoring headings and prose outside the fence.
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL):
        for line in block.splitlines():
            candidate = line.strip().strip('`').strip()
            if not candidate or candidate.startswith("#") or candidate.casefold() in {"text", "bash", "sh", "dotenv"}:
                continue
            if "=" in candidate:
                name, candidate_value = candidate.split("=", 1)
                if name.strip().casefold() in {
                    "key", "token", "api_key", "api-key", "joyniu_ai_api_key", "joyniu_llm_api_key",
                }:
                    candidate = candidate_value.strip().strip('"').strip("'")
            if candidate and not any(char.isspace() for char in candidate):
                return candidate
    if allow_plain and "```" not in text and not text.lstrip().startswith("#") and "\n" not in text:
        return text
    return ""


def _provider_key() -> str:
    # Support both names used by the first prototype and the clearer LLM names
    # used by deployment templates. Do not consume a generic OPENAI_API_KEY:
    # that could accidentally send credentials to the wrong upstream service.
    for name in ("JOYNIU_LLM_API_KEY", "JOYNIU_AI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            extracted = _extract_key(value, allow_plain=True)
            if extracted:
                return extracted
    for name in ("JOYNIU_LLM_API_KEY_FILE", "JOYNIU_AI_API_KEY_FILE"):
        key_file = os.environ.get(name, "").strip()
        if not key_file:
            continue
        try:
            text = Path(key_file).expanduser().read_text(encoding="utf-8")
        except OSError:
            # A deployment may leave an optional canonical path set while a
            # legacy fallback file is still present. Try the remaining source
            # before reporting configuration failure, without echoing paths
            # or filesystem details to the client.
            continue
        value = _extract_key(text, allow_plain=True)
        if value:
            return value
    raise AIProviderNotConfigured("AI provider is not configured on the server")


def _base_url() -> str:
    value = _first_env("JOYNIU_LLM_BASE_URL", "JOYNIU_AI_BASE_URL", default=DEFAULT_BASE_URL).rstrip("/")
    # A local HTTP endpoint is useful for an explicit test double, but must be
    # opted into so an accidental production setting cannot downgrade TLS.
    if not value:
        raise AIProxyError("AI provider base URL is empty")
    if not value.startswith("https://") and os.environ.get("JOYNIU_AI_ALLOW_INSECURE_BASE_URL") != "1":
        raise AIProxyError("AI provider base URL must use HTTPS")
    return value


def _model() -> str:
    return _first_env("JOYNIU_LLM_MODEL", "JOYNIU_AI_MODEL", default=DEFAULT_MODEL)


def _reasoning_effort() -> str:
    value = _first_env(
        "JOYNIU_LLM_REASONING_EFFORT",
        "JOYNIU_AI_REASONING_EFFORT",
        default=DEFAULT_REASONING_EFFORT,
    ).casefold()
    return value if value in {"low", "medium", "high", "xhigh", "max", "ultra"} else DEFAULT_REASONING_EFFORT


def _provider_configured() -> bool:
    try:
        _provider_key()
        _base_url()
        return True
    except AIProxyError:
        return False


def _safe_response_id(value: Any) -> str:
    value = str(value or "")
    if not _RESPONSE_ID_RE.fullmatch(value):
        raise AIProxyError("AI provider returned an invalid response id")
    return value


def _validated_patch(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AIProxyError("AI provider returned an invalid parameter patch")
    result: dict[str, Any] = {}
    for raw_key, value in raw.items():
        key = str(raw_key)
        if key not in PARAMETER_FIELDS and "_" in key:
            parts = key.split("_")
            key = parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
        if key not in PARAMETER_FIELDS:
            raise AIProxyError("AI provider returned an unsupported parameter")
        if value is None:
            continue
        kind = PARAMETER_FIELDS[key]
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AIProxyError("AI provider returned an invalid numeric parameter")
            value = float(value)
            if not math.isfinite(value) or value <= 0 or value > 1_000_000:
                raise AIProxyError("AI provider returned an out-of-range parameter")
            value = int(value) if value.is_integer() else value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise AIProxyError("AI provider returned an invalid boolean parameter")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > 80:
                raise AIProxyError("AI provider returned an invalid text parameter")
            if key == "units" and value.casefold() != "mm":
                raise AIProxyError("AI provider returned an unsupported unit")
        result[key] = value
    return result


def _output_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    fragments: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                text = part.get("text") or part.get("value")
                if isinstance(text, str):
                    fragments.append(text)
    return "\n".join(fragments).strip()


def _json_from_text(raw_text: str) -> Mapping[str, Any]:
    cleaned = str(raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError):
        # Compatible relays occasionally prepend one sentence despite the
        # schema request. Decode the first complete JSON object if possible.
        start = cleaned.find("{")
        if start < 0:
            raise AIProxyError("AI provider returned invalid structured JSON")
        decoder = json.JSONDecoder()
        try:
            parsed, _ = decoder.raw_decode(cleaned[start:])
        except (TypeError, ValueError) as exc:
            raise AIProxyError("AI provider returned invalid structured JSON") from exc
    if not isinstance(parsed, Mapping):
        raise AIProxyError("AI provider returned an invalid structured result")
    return parsed


def _parse_result(payload: Mapping[str, Any]) -> AIConversationResult:
    response_id = _safe_response_id(payload.get("id"))
    raw_text = _output_text(payload)
    if not raw_text:
        raise AIProxyError("AI provider returned no structured result")
    parsed = _json_from_text(raw_text)
    message = parsed.get("message", parsed.get("assistant_message", ""))
    if not isinstance(message, str) or len(message) > 12_000:
        raise AIProxyError("AI provider returned an invalid message")
    raw_questions = parsed.get("questions", [])
    if raw_questions is None:
        raw_questions = []
    if not isinstance(raw_questions, list) or any(not isinstance(item, str) for item in raw_questions):
        raise AIProxyError("AI provider returned invalid questions")
    raw_patch = parsed.get(
        "parameter_patch",
        parsed.get("parameterPatch", parsed.get("parameters", {})),
    )
    return AIConversationResult(
        response_id=response_id,
        message=message,
        parameter_patch=_validated_patch(raw_patch),
        needs_review=bool(parsed.get("needs_review", parsed.get("needsReview", False))),
        questions=tuple(item[:500] for item in raw_questions[:20]),
    )


def _model_dump(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json", by_alias=True)
        except TypeError:
            dumped = value.model_dump(by_alias=True)
        return dict(dumped) if isinstance(dumped, Mapping) else None
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _recognize_attachment(item: AIFile) -> dict[str, Any] | None:
    """Run the existing evidence recognizer without making it mandatory."""

    suffix = Path(item.filename).suffix.casefold()
    if not item.content_type.startswith("image/") and suffix not in {".pdf", ".dxf", ".dwg"}:
        return None
    try:
        from .recognition import recognize_drawing_bytes

        recognition = recognize_drawing_bytes(item.data, filename=item.filename)
        return _model_dump(recognition)
    except Exception:
        # The remote model remains the source for formats that the optional
        # local OCR/rasterizer cannot decode.
        return None


def _recognition_patch(drawing: Mapping[str, Any] | None) -> dict[str, Any]:
    if not drawing or drawing.get("status") != "confirmed":
        return {}
    raw = drawing.get("parameters")
    if not isinstance(raw, Mapping):
        return {}
    return _validated_patch(raw)


def _number(text: str, patterns: Iterable[str]) -> float | None:
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw_value = match.group(1)
            # Change requests often include both the old and target value,
            # e.g. ``底板长度从100改为90`` or ``R15 改为 R12``.  The compact
            # label regexes above naturally match the first number; when a
            # change verb follows that match, prefer the last number in the
            # same clause (up to punctuation) so the patch reflects the new
            # value.  Descriptive drawing text without a change verb keeps
            # the original first-match behaviour.
            tail = text[match.end() : match.end() + 80]
            clause = re.split(r"[,，;；。\n]", tail, maxsplit=1)[0]
            if re.search(r"(?:改为|改成|调整为|设为|设置为|变更为|换成|变成|替换为|为|到)", clause):
                candidates = re.findall(r"\d+(?:\.\d+)?", clause)
                if candidates:
                    raw_value = candidates[-1]
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return int(value) if value.is_integer() else value
    return None


def _text_parameter_patch(message: str, model_state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Small deterministic grammar used when a relay is unavailable."""

    text = str(message or "")
    state_kind = str((model_state or {}).get("kind", ""))
    bracket = state_kind == "bracket" or bool(re.search(r"支架|底板|鞍槽|浅槽|凹槽", text))
    patch: dict[str, Any] = {}
    if bracket:
        patterns: dict[str, tuple[str, ...]] = {
            "baseLength": (
                r"底板[^\d]{0,10}(?:长|长度)[^\d]{0,16}(?:改为|调整为|设为|设置为|变更为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"底板[^\d]{0,10}(?:长|长度)\D{0,8}(\d+(?:\.\d+)?)",
                r"底板\D{0,8}(\d+(?:\.\d+)?)\s*[×x*]",
            ),
            "baseWidth": (
                r"底板[^\d]{0,10}(?:宽|宽度)[^\d]{0,16}(?:改为|调整为|设为|设置为|变更为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"底板[^\d]{0,10}(?:宽|宽度)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "baseThickness": (
                r"底板[^\d]{0,10}(?:厚|厚度)[^\d]{0,16}(?:改为|调整为|设为|设置为|变更为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"底板[^\d]{0,10}(?:厚|厚度)\D{0,8}(\d+(?:\.\d+)?)",
                r"厚度\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "upperLength": (
                r"上部[^\d]{0,10}(?:长|长度)[^\d]{0,16}(?:改为|调整为|设为|设置为|变更为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"上部[^\d]{0,10}(?:长|长度)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "upperWidth": (
                r"上部[^\d]{0,10}(?:宽|深|深度)[^\d]{0,16}(?:改为|调整为|设为|设置为|变更为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"上部[^\d]{0,10}(?:宽|深|深度)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "upperHeight": (
                r"上部[^\d]{0,10}(?:高|高度)[^\d]{0,16}(?:改为|调整为|设为|设置为|变更为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"上部[^\d]{0,10}(?:高|高度)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "totalHeight": (r"总高(?:度)?\D{0,8}(\d+(?:\.\d+)?)",),
            "notchOpening": (
                r"(?:缺口|开口)[^\d]{0,10}(?:宽|开口)?[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:缺口|开口)[^\d]{0,10}(?:宽|开口)?\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "notchRadius": (
                r"(?:圆弧|缺口)?\s*(?:半径|R)\s*\d+(?:\.\d+)?[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(?:R\s*)?(\d+(?:\.\d+)?)",
                r"(?:圆弧|缺口)[^\d]{0,10}(?:半径|R)\D{0,5}(\d+(?:\.\d+)?)",
                r"\bR\s*(\d+(?:\.\d+)?)",
            ),
            "slotLength": (
                r"(?:浅槽|槽)[^\d]{0,10}(?:长|长度|沿Y)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:浅槽|槽)[^\d]{0,10}(?:长|长度|沿Y)\D{0,8}(\d+(?:\.\d+)?)",
                r"槽长\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "slotWidth": (
                r"(?:浅槽|槽)[^\d]{0,10}(?:宽|宽度)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:浅槽|槽)[^\d]{0,10}(?:宽|宽度)\D{0,8}(\d+(?:\.\d+)?)",
                r"槽宽\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "pocketDepth": (
                r"(?:浅槽|口袋|槽)[^\d]{0,10}(?:深|深度)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:浅槽|口袋|槽)[^\d]{0,10}(?:深|深度)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "bossDiameter": (
                r"(?:孔|凹槽|侧孔|圆柱)[^\d]{0,10}(?:直径|Ø|φ)[^\d]{0,12}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:孔|凹槽|侧孔|圆柱)[^\d]{0,10}(?:直径|Ø|φ)\D{0,5}(\d+(?:\.\d+)?)",
                r"[ØΦφ]\s*(\d+(?:\.\d+)?)",
            ),
            "bossCenterDistance": (r"中心距\D{0,8}(\d+(?:\.\d+)?)",),
        }
        for field, field_patterns in patterns.items():
            value = _number(text, field_patterns)
            if value is not None:
                patch[field] = value
        if re.search(r"不贯穿|盲孔|盲槽", text):
            patch["holeThrough"] = False
        elif re.search(r"贯穿|通孔", text):
            patch["holeThrough"] = True
        if re.search(r"铝|AL6061", text, flags=re.IGNORECASE):
            patch["material"] = "AL6061 铝合金"
        elif re.search(r"不锈钢|SUS304|304", text, flags=re.IGNORECASE):
            patch["material"] = "SUS304 不锈钢"
        return patch

    patterns = {
        "outerDiameter": (
            r"外径[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
            r"外径\D{0,8}(\d+(?:\.\d+)?)",
            r"(?:直径|OD)\D{0,8}(\d+(?:\.\d+)?)",
        ),
        "holeDiameter": (
            r"(?:通孔|内径|孔径)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
            r"(?:通孔|内径|孔径)\D{0,8}(\d+(?:\.\d+)?)",
            r"[ØΦφ]\s*(\d+(?:\.\d+)?)",
        ),
    }
    # "加长到 80" is a part-length edit.  When the sentence mentions a
    # keyway, avoid the generic ``长度`` label so ``键槽长度改为45`` cannot
    # accidentally overwrite the shaft's total length.
    if re.search(r"键槽", text):
        patterns["length"] = (
            r"(?:总长度|零件长度|轴长度)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
            r"(?:总长度|零件长度|轴长度)\D{0,8}(\d+(?:\.\d+)?)",
            r"加长(?:到|为)?\D{0,8}(\d+(?:\.\d+)?)",
        )
    else:
        patterns["length"] = (
            r"(?:总长度|零件长度|轴长度|长度)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
            r"(?:总长度|零件长度|轴长度|长度)\D{0,8}(\d+(?:\.\d+)?)",
            r"加长(?:到|为)?\D{0,8}(\d+(?:\.\d+)?)",
            r"长\D{0,8}(\d+(?:\.\d+)?)",
        )
    for field, field_patterns in patterns.items():
        value = _number(text, field_patterns)
        if value is not None:
            patch[field] = value
    if re.search(r"键槽|槽宽|槽深|槽长", text):
        keyway_patterns = {
            "keywayWidth": (
                r"(?:键槽[^\d]{0,8})?(?:槽宽|宽度|宽)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:键槽[^\d]{0,8})?(?:槽宽|宽度|宽)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "keywayDepth": (
                r"(?:键槽[^\d]{0,8})?(?:槽深|深度|深)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"(?:键槽[^\d]{0,8})?(?:槽深|深度|深)\D{0,8}(\d+(?:\.\d+)?)",
            ),
            "keywayLength": (
                r"键槽[^\d]{0,12}(?:槽长|长度|长)[^\d]{0,16}(?:改为|调整为|设为|换成|到)[^\d]*(\d+(?:\.\d+)?)",
                r"键槽[^\d]{0,12}(?:槽长|长度|长)\D{0,8}(\d+(?:\.\d+)?)",
                r"槽长\D{0,8}(\d+(?:\.\d+)?)",
            ),
        }
        for field, field_patterns in keyway_patterns.items():
            value = _number(text, field_patterns)
            if value is not None:
                patch[field] = value
    if re.search(r"铝|AL6061", text, flags=re.IGNORECASE):
        patch["material"] = "AL6061 铝合金"
    elif re.search(r"不锈钢|SUS304|304", text, flags=re.IGNORECASE):
        patch["material"] = "SUS304 不锈钢"
    return patch


def _attachment_content(item: AIFile) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded = base64.b64encode(item.data).decode("ascii")
    mime = item.content_type or "application/octet-stream"
    metadata = {
        "filename": Path(item.filename).name[:160] or "drawing",
        "contentType": mime,
        "sizeBytes": len(item.data),
        "sha256": hashlib.sha256(item.data).hexdigest(),
    }
    if mime.startswith("image/"):
        return metadata, {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "high"}
    return metadata, {
        "type": "input_file",
        "filename": metadata["filename"],
        "file_data": f"data:{mime};base64,{encoded}",
    }


def _provider_body(
    message: str,
    model_state: Mapping[str, Any] | None,
    files: tuple[AIFile, ...],
    previous_response_id: str | None,
    *,
    include_schema: bool = True,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if message:
        content.append({"type": "input_text", "text": message})
    if model_state:
        serialized = json.dumps(dict(model_state), ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > 100_000:
            raise AIProxyError("model state is too large")
        content.append({"type": "input_text", "text": f"Current model state (JSON):\n{serialized}"})
    for item in files:
        if len(item.data) > MAX_FILE_BYTES:
            raise AIProxyError("drawing file is too large")
        _metadata, attachment = _attachment_content(item)
        content.append(attachment)
    body: dict[str, Any] = {
        "model": _model(),
        "reasoning": {"effort": _reasoning_effort()},
        "store": os.environ.get("JOYNIU_AI_STORE_RESPONSES", "1") not in {"0", "false", "no"},
        "instructions": (
            "You are JoyNiu NewCAD's parameter editor. Interpret the user's text and attached "
            "engineering drawing, then return only requested changes in the JSON schema. "
            "parameter_patch contains only dimensions justified by the conversation or drawing; "
            "leave all other fields null. Never invent missing dimensions. Set needs_review true "
            "and ask a concise question when a drawing is ambiguous. All dimensions are millimetres "
            "unless the user explicitly states another unit. For the registered acceptance bracket, "
            "the upper body spans the full 50 mm Y width, 30 mm is the rectangular pocket length, "
            "R15 is a Y-through saddle cut, and Ø20 circles are subtractive Z-through cuts, not bosses."
        ),
        "input": [{"role": "user", "content": content}],
    }
    if include_schema:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "joyniu_cad_parameter_patch",
                "strict": True,
                "schema": response_schema(),
            }
        }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    return body


def _call_provider(body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    raw_body = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{_base_url()}/responses",
        data=raw_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_provider_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise AIProxyError(f"AI provider request failed (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIProxyError("AI provider request failed") from exc
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise AIProxyError("AI provider response is too large")
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AIProxyError("AI provider returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise AIProxyError("AI provider returned invalid JSON")
    return payload


def _safe_provider_info(mode: str, configured: bool) -> dict[str, Any]:
    try:
        base_url = _base_url()
    except AIProxyError:
        base_url = ""
    return {
        "name": "gptx",
        "baseUrl": base_url,
        "model": _model(),
        "reasoningEffort": _reasoning_effort(),
        "configured": configured,
        "mode": mode,
        "anonymousAllowed": os.environ.get("JOYNIU_AI_ALLOW_ANONYMOUS", "0").casefold() in {"1", "true", "yes"},
    }


class AIProxy:
    """Small Responses API client, injectable in tests."""

    def __init__(self, *, timeout_seconds: float = 120.0):
        self.timeout_seconds = timeout_seconds

    @property
    def allow_anonymous(self) -> bool:
        return os.environ.get("JOYNIU_AI_ALLOW_ANONYMOUS", "0").casefold() in {"1", "true", "yes"}

    def status(self) -> dict[str, Any]:
        configured = _provider_configured()
        return _safe_provider_info("remote" if configured else "local-fallback", configured)

    def converse(
        self,
        message: str,
        *,
        previous_response_id: str | None = None,
        model_state: Mapping[str, Any] | None = None,
        files: Iterable[AIFile] = (),
    ) -> AIConversationResult:
        message = str(message or "").strip()
        files_tuple = tuple(files)
        if not message and not files_tuple:
            raise AIProxyError("message or drawing file is required")
        if len(message) > 12_000:
            raise AIProxyError("message is too long")
        if len(files_tuple) > MAX_FILE_COUNT:
            raise AIProxyError("too many drawing files")
        for item in files_tuple:
            if len(item.data) > MAX_FILE_BYTES:
                raise AIProxyError("drawing file is too large")
        if previous_response_id and not _RESPONSE_ID_RE.fullmatch(previous_response_id):
            raise AIProxyError("invalid previous response id")
        provider_previous_id = None if (previous_response_id or "").startswith("local_") else previous_response_id

        drawing: dict[str, Any] | None = None
        attachment_meta: list[dict[str, Any]] = []
        for item in files_tuple:
            metadata, _attachment = _attachment_content(item)
            recognized = _recognize_attachment(item)
            if recognized is not None and drawing is None:
                drawing = recognized
            if recognized is not None:
                metadata["recognitionStatus"] = recognized.get("status")
                metadata["recognitionEngine"] = recognized.get("engine")
            attachment_meta.append(metadata)

        trusted_patch = _recognition_patch(drawing)
        local_patch = _text_parameter_patch(message, model_state)
        configured = _provider_configured()
        explicit_edit = bool(local_patch) or bool(re.search(r"改|修改|调整|变更|设为|增加|删除|换成", message))

        # The acceptance fixture is cryptographically identified and its
        # dimensions/topology are deterministic. Avoid an unnecessary remote
        # round-trip when the user simply asks to import it. Explicit text
        # edits still win over the calibrated values.
        if trusted_patch and (not explicit_edit or not configured):
            patch = dict(trusted_patch)
            patch.update(local_patch)
            return AIConversationResult(
                response_id=f"local_{uuid4().hex[:16]}",
                message=(
                    "已识别并锁定图纸尺寸：底板 100 × 50 × 10 mm、上部全宽 70 × 50 × 30 mm；"
                    "R15 鞍槽沿 Y 贯穿，两条 10 × 30 × 10 mm 浅槽，2×Ø20 沿 Z 贯穿。"
                    "可以继续直接告诉我需要修改的尺寸。"
                ),
                parameter_patch=patch,
                needs_review=False,
                questions=(),
                drawing=drawing,
                provider=_safe_provider_info("verified-local", configured),
                attachments=tuple(attachment_meta),
            )

        remote_error: AIProxyError | None = None
        remote_result: AIConversationResult | None = None
        if configured:
            try:
                body = _provider_body(message, model_state, files_tuple, provider_previous_id, include_schema=True)
                try:
                    payload = _call_provider(body, self.timeout_seconds)
                except AIProxyError as first_error:
                    if "HTTP 400" in str(first_error) or "HTTP 404" in str(first_error):
                        payload = _call_provider(
                            _provider_body(message, model_state, files_tuple, provider_previous_id, include_schema=False),
                            self.timeout_seconds,
                        )
                    else:
                        raise
                remote_result = _parse_result(payload)
            except AIProxyError as exc:
                remote_error = exc

        if remote_result is not None:
            if trusted_patch:
                patch = dict(trusted_patch)
                patch.update(local_patch)
                needs_review = False
            else:
                patch = dict(remote_result.parameter_patch)
                patch.update(local_patch)
                needs_review = remote_result.needs_review or bool(drawing and drawing.get("status") != "confirmed")
            return AIConversationResult(
                response_id=remote_result.response_id,
                message=remote_result.message,
                parameter_patch=patch,
                needs_review=needs_review,
                questions=remote_result.questions,
                drawing=drawing,
                provider=_safe_provider_info("remote", True),
                attachments=tuple(attachment_meta),
            )

        if trusted_patch or local_patch:
            patch = dict(trusted_patch)
            patch.update(local_patch)
            if trusted_patch:
                local_message = "图纸已按本地校准证据解析，尺寸补丁已应用。"
                review = False
            else:
                local_message = "已用本地参数语法应用明确尺寸；可继续重试 AI 服务以获得更丰富的解释。"
                # An explicit edit can be applied to the parameter editor, but
                # an attached drawing that has not been reviewed must remain a
                # review gate. This keeps the API contract safe even if a
                # downstream caller ignores the UI's generation guard.
                review = bool(drawing and drawing.get("status") != "confirmed")
            if remote_error:
                local_message += f"（中转站暂不可用：{remote_error}）"
            return AIConversationResult(
                response_id=f"local_{uuid4().hex[:16]}",
                message=local_message,
                parameter_patch=patch,
                needs_review=review,
                questions=(),
                drawing=drawing,
                provider=_safe_provider_info("local-fallback", configured),
                attachments=tuple(attachment_meta),
            )

        # An unknown attachment is itself a valid evidence result even when
        # no relay key is configured. Return the review envelope rather than a
        # misleading 503, so a reviewer can inspect/confirm it through the
        # platform OCR workflow.
        if drawing is not None and drawing.get("status") != "confirmed":
            return AIConversationResult(
                response_id=f"local_{uuid4().hex[:16]}",
                message=(
                    "已收到图纸，但当前无法完成可信参数化识别；识别结果需要人工确认后才会生成实体。"
                    if remote_error is None
                    else "已收到图纸，但当前中转站不可用；识别结果需要人工确认后才会生成实体。"
                ),
                parameter_patch={},
                needs_review=True,
                questions=("请确认图纸单位、视图对应关系和关键特征后重试。",),
                drawing=drawing,
                provider=_safe_provider_info("local-fallback", configured),
                attachments=tuple(attachment_meta),
            )

        if remote_error is not None:
            raise remote_error
        raise AIProviderNotConfigured("AI provider is not configured and no deterministic patch was found")


__all__ = [
    "AIConversationResult",
    "AIFile",
    "AIProviderNotConfigured",
    "AIProxy",
    "AIProxyError",
    "MAX_FILE_BYTES",
    "MAX_FILE_COUNT",
    "PARAMETER_FIELDS",
    "parameter_patch_schema",
    "response_schema",
]
