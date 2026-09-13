"""Keep legacy, non-metered AI HTTP paths out of commercial deployments.

AIProxy.converse may perform several hidden provider calls and retries and its
legacy stream does not await all supplier work after disconnect. Counting one
HTTP turn or a cumulative trace would not be accurate supplier accounting.
Commercial customers must use the durable, per-call metered CAD job pipeline.
"""
from __future__ import annotations

import os
from typing import Callable, Mapping

from .platform import ConflictError


LEGACY_AI_NOTICE = "此旧版 AI 对话入口未接入商用任务计量，已暂停使用。请到任务中心或 2D 转 3D 工作台创建任务，原项目数据不受影响。"


def legacy_ai_available(billing_provider: Callable | None = None, *, environ: Mapping[str, str] | None = None):
    env = os.environ if environ is None else environ
    if env.get("JOYNIU_BILLING_ENABLED", "").strip().casefold() in {"1", "true", "yes"}:
        return False
    if billing_provider is None:
        return True
    try:
        billing = billing_provider()
        if billing is None:
            return True
        # An explicit commercial intent also closes legacy access while its
        # pricing setup is incomplete; do not provide an unmetered fallback.
        if billing.enabled is True:
            return False
        status = billing.status()
        if not isinstance(status, Mapping):
            return False
        return (status.get("enabled") is not True and status.get("commercialMode") is not True
                and status.get("policyAvailable") is not False)
    except Exception:
        return False


def require_legacy_ai(billing_provider: Callable | None = None):
    if not legacy_ai_available(billing_provider):
        raise ConflictError(LEGACY_AI_NOTICE)
