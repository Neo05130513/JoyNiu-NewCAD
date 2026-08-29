"""安全的 OpenAI Responses API 兼容代理。

The browser only receives the normalized conversation result.  Provider
credentials are read on the API process from ``JOYNIU_AI_API_KEY`` or an
explicit ``JOYNIU_AI_API_KEY_FILE`` and are never included in responses,
exceptions, or logs.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_BASE_URL = "https://gptx.shop/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RESPONSE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "responseId": self.response_id,
            "message": self.message,
            "parameterPatch": dict(self.parameter_patch),
            "needsReview": self.needs_review,
            "questions": list(self.questions),
        }


# The allowlist is deliberately shared by the structured-output schema and
# the response validator.  Unknown model fields must never be applied by an
# AI response, even if a provider ignores the JSON schema instruction.
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
    if kind == "number":
        return {"anyOf": [{"type": "number"}, {"type": "null"}]}
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


def parameter_patch_schema() -> dict[str, Any]:
    properties = {key: _nullable_schema(kind) for key, kind in PARAMETER_FIELDS.items()}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        # Strict structured outputs require optional values to be represented
        # as nullable required properties.  The parser removes nulls.
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


def _provider_key() -> str:
    value = os.environ.get("JOYNIU_AI_API_KEY", "").strip()
    if value:
        return value
    key_file = os.environ.get("JOYNIU_AI_API_KEY_FILE", "").strip()
    if key_file:
        try:
            value = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AIProviderNotConfigured("AI provider credential file is unavailable") from exc
        if value:
            return value
    raise AIProviderNotConfigured("AI provider is not configured on the server")


def _base_url() -> str:
    value = os.environ.get("JOYNIU_AI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    # A local HTTP endpoint is useful for an explicit test double, but must be
    # opted into so an accidental production setting cannot downgrade TLS.
    if not value.startswith("https://") and os.environ.get("JOYNIU_AI_ALLOW_INSECURE_BASE_URL") != "1":
        raise AIProxyError("AI provider base URL must use HTTPS")
    if not value:
        raise AIProxyError("AI provider base URL is empty")
    return value


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
    for key, value in raw.items():
        key = str(key)
        # Accept snake_case aliases from non-strict compatible gateways while
        # still applying the same allowlist.
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
            # Preserve integer-looking dimensions as numbers while avoiding
            # NaN/Infinity and surprising string coercion in the UI.
            value = int(value) if value.is_integer() else value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise AIProxyError("AI provider returned an invalid boolean parameter")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > 80:
                raise AIProxyError("AI provider returned an invalid text parameter")
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
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    fragments.append(part["text"])
    return "\n".join(fragments).strip()


def _parse_result(payload: Mapping[str, Any]) -> AIConversationResult:
    response_id = _safe_response_id(payload.get("id"))
    raw_text = _output_text(payload)
    if not raw_text:
        raise AIProxyError("AI provider returned no structured result")
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except (TypeError, ValueError) as exc:
        raise AIProxyError("AI provider returned invalid structured JSON") from exc
    if not isinstance(parsed, Mapping):
        raise AIProxyError("AI provider returned an invalid structured result")
    message = parsed.get("message", "")
    if not isinstance(message, str) or len(message) > 12_000:
        raise AIProxyError("AI provider returned an invalid message")
    raw_questions = parsed.get("questions", [])
    if not isinstance(raw_questions, list) or any(not isinstance(item, str) for item in raw_questions):
        raise AIProxyError("AI provider returned invalid questions")
    return AIConversationResult(
        response_id=response_id,
        message=message,
        parameter_patch=_validated_patch(parsed.get("parameter_patch", {})),
        needs_review=bool(parsed.get("needs_review", False)),
        questions=tuple(item[:500] for item in raw_questions[:20]),
    )


class AIProxy:
    """Small stdlib-only Responses API client, injectable in tests."""

    def __init__(self, *, timeout_seconds: float = 120.0):
        self.timeout_seconds = timeout_seconds

    def converse(
        self,
        message: str,
        *,
        previous_response_id: str | None = None,
        model_state: Mapping[str, Any] | None = None,
        files: Iterable[AIFile] = (),
    ) -> AIConversationResult:
        message = str(message or "").strip()
        files = tuple(files)
        if not message and not files:
            raise AIProxyError("message or drawing file is required")
        if len(message) > 12_000:
            raise AIProxyError("message is too long")
        if previous_response_id is not None and not _RESPONSE_ID_RE.fullmatch(previous_response_id):
            raise AIProxyError("invalid previous response id")
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
            encoded = base64.b64encode(item.data).decode("ascii")
            mime = item.content_type or "application/octet-stream"
            if mime.startswith("image/"):
                content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
            else:
                content.append({
                    "type": "input_file",
                    "filename": Path(item.filename).name[:160] or "drawing",
                    "file_data": f"data:{mime};base64,{encoded}",
                })
        body: dict[str, Any] = {
            "model": DEFAULT_MODEL,
            "reasoning": {"effort": DEFAULT_REASONING_EFFORT},
            "store": True,
            "instructions": (
                "You are JoyNiu NewCAD's parameter editor. Interpret the user's text and attached "
                "engineering drawing, then return only the requested changes in the supplied JSON "
                "schema. parameter_patch must contain only dimensions you can justify from the "
                "conversation/drawing; leave other fields null. Never invent missing dimensions. "
                "Set needs_review true and ask a concise question when the drawing is ambiguous. "
                "All dimensions are millimetres unless the user explicitly states another unit."
            ),
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "joyniu_cad_parameter_patch",
                    "strict": True,
                    "schema": response_schema(),
                }
            },
        }
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Do not relay provider response text: gateways occasionally echo
            # request headers or diagnostic data into an error body.
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
        return _parse_result(payload)


__all__ = [
    "AIConversationResult",
    "AIFile",
    "AIProviderNotConfigured",
    "AIProxy",
    "AIProxyError",
    "MAX_FILE_BYTES",
    "PARAMETER_FIELDS",
]
