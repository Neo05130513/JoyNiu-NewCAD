"""Portable HTTP attachment names for browser and API downloads."""

from __future__ import annotations

import re
from urllib.parse import quote


def attachment_content_disposition(filename: str) -> str:
    """Keep HTTP headers ASCII while preserving the UTF-8 download filename."""

    # A download name is a basename, never a path or a second HTTP header.
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(filename or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip() or "download"
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "download"
    encoded = quote(name, safe="", encoding="utf-8", errors="replace")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
