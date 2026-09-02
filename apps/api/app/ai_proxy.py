"""Server-side multimodal AI gateway for the NewCAD copilot.

The browser never receives the relay credential.  This module deliberately
keeps the provider boundary small: drawings and the current parameter state
go in, while a validated, allow-listed parameter patch comes out.  Uploaded
drawing parameters come from the remote multimodal model; local recognition
is retained only as review/audit metadata and never substitutes for that
model response.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4


DEFAULT_BASE_URL = "https://gptx.shop/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_COUNT = 4
_RESPONSE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_KEY_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9._-]{10,}")


class AIProxyError(RuntimeError):
    """A safe, user-facing AI proxy failure without provider payloads."""


class AIProviderNotConfigured(AIProxyError):
    """The server has no configured provider credential."""


class AIProviderHTTPError(AIProxyError):
    """Provider HTTP failure with a status code safe for retry decisions."""

    def __init__(self, status_code: int):
        self.status_code = int(status_code)
        super().__init__(f"AI provider request failed (HTTP {self.status_code})")


class AIProviderTransportError(AIProxyError):
    """Transient provider connection or timeout failure."""


class AIProviderIncompleteError(AIProxyError):
    """Provider stopped before producing the required structured message."""

    def __init__(self, reason: str = "unknown"):
        self.reason = str(reason or "unknown")[:80]
        super().__init__(f"AI provider response incomplete ({self.reason})")


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


def _validated_patch(raw: Any, *, tolerate_invalid: bool = False) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        if tolerate_invalid:
            return {}
        raise AIProxyError("AI provider returned an invalid parameter patch")
    result: dict[str, Any] = {}
    for raw_key, value in raw.items():
        key = str(raw_key)
        if key not in PARAMETER_FIELDS and "_" in key:
            parts = key.split("_")
            key = parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
        if key not in PARAMETER_FIELDS:
            if tolerate_invalid:
                # Vision models occasionally use a descriptive synonym such
                # as ``overall_length``.  It is safer to omit that value than
                # to guess which CAD datum it represents; the message and
                # question list still explain what the customer must confirm.
                continue
            raise AIProxyError("AI provider returned an unsupported parameter")
        if value is None:
            continue
        kind = PARAMETER_FIELDS[key]
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                if tolerate_invalid:
                    continue
                raise AIProxyError("AI provider returned an invalid numeric parameter")
            value = float(value)
            if not math.isfinite(value) or value <= 0 or value > 1_000_000:
                if tolerate_invalid:
                    continue
                raise AIProxyError("AI provider returned an out-of-range parameter")
            value = int(value) if value.is_integer() else value
        elif kind == "boolean":
            if not isinstance(value, bool):
                if tolerate_invalid:
                    continue
                raise AIProxyError("AI provider returned an invalid boolean parameter")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > 80:
                if tolerate_invalid:
                    continue
                raise AIProxyError("AI provider returned an invalid text parameter")
            if key == "units" and value.casefold() != "mm":
                if tolerate_invalid:
                    continue
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


def _parse_result(
    payload: Mapping[str, Any],
    *,
    tolerate_patch_errors: bool = False,
) -> AIConversationResult:
    status = str(payload.get("status") or "").strip().casefold()
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, Mapping) else "unknown"
        raise AIProviderIncompleteError(str(reason or "unknown"))
    if status and status != "completed":
        raise AIProxyError("AI provider returned a non-completed response")
    response_id = _safe_response_id(payload.get("id"))
    raw_text = _output_text(payload)
    if not raw_text:
        raise AIProxyError("AI provider returned no structured result")
    parsed = _json_from_text(raw_text)
    message = parsed.get("message", parsed.get("assistant_message", ""))
    if not isinstance(message, str):
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
        parameter_patch=_validated_patch(raw_patch, tolerate_invalid=tolerate_patch_errors),
        needs_review=bool(parsed.get("needs_review", parsed.get("needsReview", False))),
        questions=tuple(raw_questions),
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
    raw = drawing.get("parameters") or drawing.get("candidateParameters")
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


def _detected_image_mime(item: AIFile) -> str:
    """Identify common image uploads even when browsers use octet-stream."""

    declared = (item.content_type or "").strip().casefold()
    if declared.startswith("image/"):
        return declared
    suffix_mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(Path(item.filename).suffix.casefold(), "")
    data = item.data[:16]
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"RIFF") and item.data[8:12] == b"WEBP":
        return "image/webp"
    return suffix_mime


def _provider_image_bytes(item: AIFile) -> tuple[bytes, str]:
    """Return a relay-friendly image payload without changing audit bytes.

    Some OpenAI-compatible relays accept a PNG data URL but leave the request
    pending while their vision adapter decodes it.  A high-quality RGB JPEG is
    materially smaller and is handled consistently by those adapters.  The
    original upload is still hashed/stored by the platform; this conversion is
    only for the transient provider request.  Invalid or unsupported images
    fall back to their original bytes so the caller receives the provider's
    normal, bounded error path.
    """

    mime = _detected_image_mime(item) or (item.content_type or "").casefold()
    if not mime.startswith("image/"):
        return item.data, mime or "application/octet-stream"
    try:
        # Pillow is optional in the domain package.  Keep the gateway usable on
        # minimal hosts and let the remote endpoint handle the original bytes
        # when the image decoder is not installed.
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(item.data)) as image:
            # Engineering drawings are generally opaque; flatten alpha onto a
            # white canvas so transparent PNG/DWG previews do not become black.
            if image.mode in {"RGBA", "LA", "P"}:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            # Downsample only the transient provider copy.  ``thumbnail``
            # preserves aspect ratio and leaves already-small drawings alone.
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
            image.thumbnail((_image_max_dimension(), _image_max_dimension()), resampling)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True, progressive=True)
            converted = output.getvalue()
        if converted:
            return converted, "image/jpeg"
    except Exception:
        # Do not turn a provider compatibility optimisation into an upload
        # failure.  The original content remains available for a retry.
        pass
    return item.data, mime or "application/octet-stream"


def _image_detail() -> str:
    """Select the provider vision detail level.

    Engineering dimensions need the high-detail path, so it is the default.
    If that provider path fails, ``AIProxy.converse`` retries the same original
    drawing once at low detail.  Both attempts remain multimodal model calls.
    """

    value = os.environ.get("JOYNIU_AI_IMAGE_DETAIL", "high").strip().casefold()
    return value if value in {"low", "high", "auto"} else "high"


def _image_max_dimension() -> int:
    """Return the largest side sent to the vision relay.

    Keeping the original upload untouched while sending a bounded preview
    avoids relay timeouts on very large screenshots and leaves enough pixels
    for ordinary engineering-drawing annotations.  Deployments with a relay
    that supports tiled/high-resolution inputs can raise this value.
    """

    try:
        value = int(os.environ.get("JOYNIU_AI_IMAGE_MAX_DIMENSION", "4096"))
    except (TypeError, ValueError):
        value = 4096
    return max(256, min(4096, value))


def _attachment_content(
    item: AIFile,
    *,
    image_detail: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider_bytes, provider_mime = _provider_image_bytes(item)
    encoded = base64.b64encode(provider_bytes).decode("ascii")
    mime = item.content_type or "application/octet-stream"
    metadata = {
        "filename": Path(item.filename).name[:160] or "drawing",
        "contentType": mime,
        "sizeBytes": len(item.data),
        "sha256": hashlib.sha256(item.data).hexdigest(),
    }
    if provider_mime.startswith("image/"):
        return metadata, {
            "type": "input_image",
            "image_url": f"data:{provider_mime};base64,{encoded}",
            "detail": image_detail or _image_detail(),
        }
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
    history: tuple[dict[str, str], ...] = (),
    include_schema: bool = True,
    force_store: bool | None = None,
    image_detail: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    has_attachments = bool(files)
    if message:
        content.append({"type": "input_text", "text": message})
    if model_state:
        serialized = json.dumps(dict(model_state), ensure_ascii=False, separators=(",", ":"))
        content.append({
            "type": "input_text",
            "text": (
                "Current editable model state (JSON). Treat these values as a "
                "preview scaffold only: when a drawing is attached, copy a value "
                "into parameter_patch only if the drawing or the user's text "
                "justifies it; leave unsupported fields null.\n"
                f"{serialized}"
            ),
        })
    if has_attachments:
        # GPTX's vision adapter is more reliable when the multimodal
        # instruction travels in the user content.  In particular, sending a
        # large top-level ``instructions`` string together with a strict
        # nullable schema can leave the relay request pending even though a
        # normal text request succeeds.  Keep this prompt compact and ask for
        # the same validated JSON envelope; ``_parse_result`` applies the
        # server-side allowlist after the response arrives.
        # Keep the key guide short enough for the relay's vision path while
        # still making the supported bracket semantics unambiguous.  The
        # server-side allowlist below remains the final authority.
        fields = (
            "baseLength=底板长,baseWidth=底板宽,baseThickness=底板厚,"
            "upperLength=上部长,upperWidth=上部全宽,upperHeight=上部高,"
            "totalHeight=总高,notchOpening=鞍槽开口,notchRadius=鞍槽半径,"
            "slotLength=浅槽沿Y长,slotWidth=浅槽宽,pocketDepth=浅槽深,"
            "bossDiameter=圆孔直径,bossCenterDistance=圆孔中心距,units=单位"
        )
        content.insert(
            0,
            {
                "type": "input_text",
                "text": (
                    "请分析附加工程图，检查所有视图、标注和特征关系。"
                    "只返回 JSON（不要 Markdown）：message、parameter_patch、needs_review、questions。"
                    f"parameter_patch 字段含义：{fields}。"
                    "只填图纸明确证明的候选值，未知省略或 null，绝不猜测；"
                    "识别不完整时仍返回已识别值并设 needs_review=true，候选必须人工确认。"
                ),
            },
        )
    for item in files:
        if len(item.data) > MAX_FILE_BYTES:
            raise AIProxyError("drawing file is too large")
        _metadata, attachment = _attachment_content(item, image_detail=image_detail)
        content.append(attachment)
    configured_store = os.environ.get("JOYNIU_AI_STORE_RESPONSES", "0").casefold() not in {"0", "false", "no"}
    store_response = configured_store if force_store is None else bool(force_store)
    # Replay the visible conversation explicitly.  The relay is configured
    # with ``store=false`` by default, so a previous response id alone cannot
    # be treated as durable chat memory.  Easy input messages are accepted by
    # the Responses protocol for both user and assistant roles.
    conversation_input: list[dict[str, Any]] = [
        {"role": item["role"], "content": item["text"]}
        for item in history
    ]
    conversation_input.append({"role": "user", "content": content})
    body: dict[str, Any] = {
        "model": _model(),
        "reasoning": {"effort": _reasoning_effort()},
        # Do not send max_output_tokens. The relay/model owns its native output
        # budget; JoyNiu must not truncate high-effort reasoning or the final
        # structured CAD answer with an additional application-level cap.
        "store": store_response,
        # GPTX declares the Responses wire protocol.  Request its standard
        # server-sent event stream so long high-effort vision turns keep
        # producing progress instead of appearing as one non-streaming call.
        "stream": True,
        "input": conversation_input,
    }
    if not has_attachments:
        body["instructions"] = (
            "You are JoyNiu NewCAD's parameter editor. Interpret the user's text and return a "
            "validated structured parameter patch. Only change values justified by the conversation; "
            "never invent dimensions. Candidate values from a drawing are not final until a human "
            "confirms them. All dimensions are millimetres unless the user explicitly states another unit."
        )
    # The GPTX vision route currently handles a concise JSON contract more
    # reliably than a large strict schema. Text-only turns retain strict
    # structured outputs; attachment turns are still validated immediately by
    # ``_parse_result`` and the allowlist above.
    if include_schema and not has_attachments:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "joyniu_cad_parameter_patch",
                "strict": True,
                "schema": response_schema(),
            }
        }
    if previous_response_id and store_response:
        body["previous_response_id"] = previous_response_id
    return body


def _json_mapping(raw: bytes, *, error_message: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AIProxyError(error_message) from exc
    if not isinstance(payload, Mapping):
        raise AIProxyError(error_message)
    return payload


def _partial_message_text(raw_text: str) -> str:
    """Extract the currently available ``message`` JSON string.

    Structured Responses arrive a few characters at a time.  The browser
    should see the natural-language assistant message, never the surrounding
    JSON envelope or an unvalidated parameter patch.  This small decoder is
    intentionally tolerant of an unfinished escape at the end of a delta.
    """

    match = re.search(r'"(?:message|assistant_message)"\s*:\s*"', str(raw_text or ""))
    if match is None:
        return ""
    source = str(raw_text)[match.end() :]
    decoded: list[str] = []
    index = 0
    escapes = {"\"": "\"", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while index < len(source):
        char = source[index]
        if char == '"':
            break
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(source):
            break
        escaped = source[index + 1]
        if escaped == "u":
            codepoint = source[index + 2 : index + 6]
            if len(codepoint) < 4 or not re.fullmatch(r"[0-9A-Fa-f]{4}", codepoint):
                break
            value = int(codepoint, 16)
            if 0xD800 <= value <= 0xDBFF:
                # JSON represents non-BMP characters as a UTF-16 surrogate
                # pair.  Wait for an unfinished low half, and never expose a
                # lone surrogate that Starlette cannot encode as UTF-8.
                if len(source) < index + 12:
                    break
                low_prefix = source[index + 6 : index + 8]
                low_text = source[index + 8 : index + 12]
                if low_prefix == "\\u" and re.fullmatch(r"[0-9A-Fa-f]{4}", low_text):
                    low = int(low_text, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        decoded.append(chr(0x10000 + ((value - 0xD800) << 10) + (low - 0xDC00)))
                        index += 12
                        continue
                decoded.append("\N{REPLACEMENT CHARACTER}")
                index += 6
                continue
            if 0xDC00 <= value <= 0xDFFF:
                decoded.append("\N{REPLACEMENT CHARACTER}")
                index += 6
                continue
            decoded.append(chr(value))
            index += 6
            continue
        decoded.append(escapes.get(escaped, escaped))
        index += 2
    return "".join(decoded)


def _stream_payload(
    response: Any,
    on_message_update: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Consume a Responses SSE stream and return its completed response.

    GPTX is Responses-compatible, but a few compatible test/local gateways
    still return a normal JSON body even when ``stream=true``.  Supporting
    both forms keeps that compatibility without changing the upstream request
    back to non-streaming.
    """

    completed: Mapping[str, Any] | None = None
    response_id = ""
    deltas: list[str] = []
    done_text = ""
    data_lines: list[str] = []
    emitted_message = ""

    def consume_event() -> bool:
        nonlocal completed, response_id, done_text, data_lines, emitted_message
        if not data_lines:
            return False
        raw_data = "\n".join(data_lines).strip()
        data_lines = []
        if not raw_data:
            return False
        if raw_data == "[DONE]":
            return True
        event = _json_mapping(
            raw_data.encode("utf-8"),
            error_message="AI provider returned an invalid streaming event",
        )
        event_type = str(event.get("type", ""))
        event_response = event.get("response")
        if isinstance(event_response, Mapping):
            candidate_id = event_response.get("id")
            if isinstance(candidate_id, str):
                response_id = candidate_id
        direct_response_id = event.get("response_id")
        if isinstance(direct_response_id, str):
            response_id = direct_response_id
        if event_type in {"response.completed", "response.failed", "response.incomplete"}:
            if not isinstance(event_response, Mapping):
                raise AIProxyError("AI provider returned an invalid terminal streaming event")
            # A non-completed signal in either the event type or payload wins.
            # It must never smuggle a parseable output_text through as a
            # completed CAD patch when a compatible relay contradicts itself.
            event_status = event_type.removeprefix("response.")
            payload_status = str(event_response.get("status") or "").strip().casefold()
            terminal_status = event_status if event_status != "completed" else payload_status or event_status
            completed = {**event_response, "status": terminal_status}
            return True
        if event_type == "error":
            raise AIProxyError("AI provider streaming response failed")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
                visible = _partial_message_text("".join(deltas))
                if on_message_update is not None and visible != emitted_message:
                    emitted_message = visible
                    on_message_update(visible)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str):
                done_text = text
                visible = _partial_message_text(done_text)
                if on_message_update is not None and visible != emitted_message:
                    emitted_message = visible
                    on_message_update(visible)
        return False

    def consume_line(raw_line: bytes) -> bool:
        try:
            line = raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise AIProxyError("AI provider returned an invalid streaming event") from exc
        if not line:
            return consume_event()
        if line.startswith(":") or line.startswith("event:") or line.startswith("id:"):
            return False
        if line.startswith("data:"):
            next_data = line[5:].lstrip()
            if next_data.strip() == "[DONE]":
                if data_lines and consume_event():
                    return True
                return True
            # A few compatible relays omit the blank SSE separator and emit
            # one complete JSON object per ``data:`` line.  Flush a previously
            # complete object before accepting the next line while preserving
            # legitimate multi-line JSON events.
            if data_lines:
                try:
                    json.loads("\n".join(data_lines))
                except ValueError:
                    pass
                else:
                    if consume_event():
                        return True
            data_lines.append(next_data)
        return False

    if hasattr(response, "readline"):
        first_line = response.readline()
        if not first_line:
            raise AIProxyError("AI provider returned an empty streaming response")
        # Compatibility path for relays that ignore ``stream=true`` and return
        # one ordinary Responses JSON document.  Actual SSE always begins with
        # an event/comment/data field, so buffering is confined to this path.
        if first_line.lstrip().startswith((b"{", b"[")):
            if hasattr(response, "read"):
                remainder = response.read()
            else:
                parts: list[bytes] = []
                while True:
                    part = response.readline()
                    if not part:
                        break
                    parts.append(part)
                remainder = b"".join(parts)
            return _json_mapping(
                (first_line + remainder).strip(),
                error_message="AI provider returned invalid JSON",
            )

        terminal = consume_line(first_line)
        while not terminal:
            raw_line = response.readline()
            if not raw_line:
                break
            terminal = consume_line(raw_line)
        if not terminal and data_lines:
            consume_event()
    else:  # small injectable response doubles used by tests
        raw_body = response.read().strip()
        if not raw_body:
            raise AIProxyError("AI provider returned an empty streaming response")
        if raw_body.startswith((b"{", b"[")):
            return _json_mapping(raw_body, error_message="AI provider returned invalid JSON")
        terminal = False
        for raw_line in raw_body.splitlines(keepends=True):
            terminal = consume_line(raw_line)
            if terminal:
                break
        if not terminal and data_lines:
            consume_event()

    if completed is not None:
        return completed
    output_text = done_text or "".join(deltas)
    if output_text:
        # Some compatible relays omit ``response.completed`` but provide all
        # text events followed by [DONE].  Reconstruct only the minimum normal
        # response envelope consumed by the existing strict parser.
        return {
            "id": response_id or f"resp_stream_{uuid4().hex[:16]}",
            "status": "completed",
            "output_text": output_text,
        }
    raise AIProxyError("AI provider returned no completed streaming response")


def _call_provider(
    body: Mapping[str, Any],
    timeout: float,
    on_message_update: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    raw_body = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{_base_url()}/responses",
        data=raw_body,
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_provider_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _stream_payload(response, on_message_update=on_message_update)
    except urllib.error.HTTPError as exc:
        raise AIProviderHTTPError(exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIProviderTransportError("AI provider request failed") from exc


def _provider_error_is_retryable(error: AIProxyError) -> bool:
    """Return whether one clean, stateless relay retry is worthwhile.

    GPTX-compatible relays can reject a strict schema or a stale
    ``previous_response_id`` even though the same model request succeeds
    without those optional fields.  Transport failures, throttling/server
    failures and malformed/empty model envelopes are also safe to retry once.
    Authentication and permission errors deliberately fail immediately.
    """

    if isinstance(error, AIProviderHTTPError):
        return error.status_code in {400, 404, 408, 409, 425, 429, 500, 502, 503, 504}
    if isinstance(error, AIProviderTransportError):
        return True
    if isinstance(error, AIProviderIncompleteError):
        return True
    return str(error).startswith("AI provider returned")


def _validated_history(history: Iterable[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    """Reduce browser chat history to safe user/assistant text messages.

    No application token or character cap is applied.  Unsupported message
    kinds, attachment metadata and UI-only status records are ignored rather
    than becoming model instructions.
    """

    result: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, Mapping):
            raise AIProxyError("conversation history contains an invalid message")
        role = str(item.get("role", "")).casefold()
        text = item.get("text", item.get("content", ""))
        if role not in {"user", "assistant"} or not isinstance(text, str):
            raise AIProxyError("conversation history contains an invalid message")
        if text.strip():
            result.append({"role": role, "text": text})
    return tuple(result)


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
        "streaming": True,
        "configured": configured,
        "mode": mode,
        "anonymousAllowed": os.environ.get("JOYNIU_AI_ALLOW_ANONYMOUS", "0").casefold() in {"1", "true", "yes"},
    }


class AIProxy:
    """Small Responses API client, injectable in tests."""

    def __init__(self, *, timeout_seconds: float = 60.0):
        # Keep an unavailable relay from holding the customer's workbench in
        # a spinner indefinitely.  Vision requests can legitimately take
        # several dozen seconds on a relay, so allow one bounded minute before
        # returning the editable local candidate envelope.
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
        history: Iterable[Mapping[str, Any]] = (),
        on_message_update: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> AIConversationResult:
        message = str(message or "").strip()
        files_tuple = tuple(files)
        history_tuple = _validated_history(history)
        if not message and not files_tuple:
            raise AIProxyError("message or drawing file is required")
        if len(files_tuple) > MAX_FILE_COUNT:
            raise AIProxyError("too many drawing files")
        for item in files_tuple:
            if len(item.data) > MAX_FILE_BYTES:
                raise AIProxyError("drawing file is too large")
        if previous_response_id and not _RESPONSE_ID_RE.fullmatch(previous_response_id):
            raise AIProxyError("invalid previous response id")
        # Explicit replay is authoritative and works with ``store=false``.
        # Combining it with a stored predecessor would duplicate every turn.
        provider_previous_id = None if history_tuple or (previous_response_id or "").startswith("local_") else previous_response_id

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

        configured = _provider_configured()

        remote_error: AIProxyError | None = None
        remote_result: AIConversationResult | None = None
        if configured:
            # Both attempts ask the multimodal model to inspect the original
            # upload.  The first uses engineering-friendly high detail; the
            # second is a stateless low-detail compatibility retry.  Give each
            # attempt its own inactivity timeout so a real first timeout does
            # not result in a misleading "retried" message without a second
            # network call.
            attempt_specs = (
                (True, provider_previous_id, None, _image_detail() if files_tuple else None),
                (False, None, False, "low" if files_tuple else None),
            )
            for attempt_index, (include_schema, attempt_previous_id, force_store, image_detail) in enumerate(attempt_specs):
                try:
                    if on_status is not None:
                        on_status("正在连接远程大模型…" if attempt_index == 0 else "正在使用兼容视觉模式重试…")
                    body = _provider_body(
                        message,
                        model_state,
                        files_tuple,
                        attempt_previous_id,
                        history=history_tuple,
                        include_schema=include_schema,
                        force_store=force_store,
                        image_detail=image_detail,
                    )
                    payload = _call_provider(
                        body,
                        self.timeout_seconds,
                        on_message_update=on_message_update,
                    )
                    # A vision model may include descriptive, non-CAD keys
                    # (for example ``overall_length``) alongside allow-listed
                    # fields. Drop those keys for attachment turns so useful
                    # candidates and questions still reach the customer.
                    remote_result = _parse_result(
                        payload,
                        tolerate_patch_errors=bool(files_tuple),
                    )
                    if on_message_update is not None:
                        on_message_update(remote_result.message)
                    remote_error = None
                    break
                except AIProxyError as exc:
                    remote_error = exc
                    if attempt_index + 1 >= len(attempt_specs) or not _provider_error_is_retryable(exc):
                        break

        if remote_result is not None:
            # For an uploaded drawing, the model patch is the only automatic
            # parameter source.  Local OCR/calibration remains visible as
            # audit metadata but cannot replace or override model values.
            patch = dict(remote_result.parameter_patch)
            needs_review = remote_result.needs_review or bool(files_tuple)
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

        # Preserve the source attachment and its audit metadata after a remote
        # failure, but return no parameter patch.  This prevents OCR geometry
        # or a fixture profile from masquerading as multimodal-model output.
        if files_tuple:
            return AIConversationResult(
                response_id=f"local_{uuid4().hex[:16]}",
                message=(
                    "已收到并保留原图；当前未配置远程大模型，因此没有写入任何自动候选参数。"
                    if remote_error is None
                    else "已收到并保留原图；远程大模型已分别用高清与兼容视觉模式分析，"
                    "但本次仍未返回可靠参数。系统没有用本地 OCR 或模板值替代，可直接再次分析。"
                ),
                parameter_patch={},
                needs_review=True,
                questions=(),
                drawing=drawing,
                provider=_safe_provider_info("local-fallback", configured),
                attachments=tuple(attachment_meta),
            )

        if remote_error is not None:
            raise remote_error
        raise AIProviderNotConfigured("AI provider is not configured")


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
