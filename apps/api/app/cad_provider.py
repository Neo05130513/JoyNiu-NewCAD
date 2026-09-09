"""Select the CAD reasoning engine without changing legacy chat transport."""

from __future__ import annotations

import os


def uses_codex() -> bool:
    return os.environ.get("JOYNIU_CAD_PROVIDER", "relay").strip().casefold() == "codex"


def configured_cad_provider():
    if uses_codex():
        from .cad_codex_provider import CodexCadProvider
        return CodexCadProvider()
    return None


def cad_provider_status():
    provider = configured_cad_provider()
    return dict(provider.provider_info) if provider is not None else None


def provider_details(provider=None):
    from . import ai_proxy
    return dict(getattr(provider, "provider_info", {
        "mode": "remote", "model": ai_proxy._model(), "reasoningEffort": ai_proxy._reasoning_effort(),
    }))


def same_reading_engine(reading, current):
    previous = reading.get("provider") if isinstance(reading, dict) else None
    if not isinstance(previous, dict) or not previous.get("model"):
        # Legacy injected readers had no engine metadata. They cannot stand
        # in for a fresh reading after explicitly selecting the Codex engine.
        return current.get("mode") != "codex"
    return (previous.get("mode", "remote") == current.get("mode", "remote")
            and previous.get("model") == current.get("model"))
