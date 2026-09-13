"""Read-only usage accounting and explicitly priced simulations.

One event represents one provider request, including a failed request or retry.
Give repeated snapshots of that request the same ``event_id``; give retries new
IDs. Do not mix live events with extracted run events: persisted records lack
provider request IDs, so the two sources cannot reliably be deduplicated.

Prices are supplied by the caller, in currency units per million tokens::

    {"model-name": {"currency": "CNY", "input_per_million": "1.5",
                    "cached_input_per_million": "0.15", "output_per_million": "6"}}

This module neither infers API charges from Codex subscription usage nor embeds
model prices. Input includes cached input; output includes reasoning tokens.
Only uncached input, cached input and output are priced. Missing usage remains
unknown, including failed requests which may have consumed upstream resources.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens")
_ALIASES = {
    "input_tokens": (("input_tokens",), ("prompt_tokens",)),
    "cached_input_tokens": (("cached_input_tokens",), ("cached_tokens",),
                            ("input_tokens_details", "cached_tokens"),
                            ("prompt_tokens_details", "cached_tokens")),
    "output_tokens": (("output_tokens",), ("completion_tokens",)),
    "reasoning_tokens": (("reasoning_tokens",), ("reasoning_output_tokens",),
                         ("output_tokens_details", "reasoning_tokens"),
                         ("completion_tokens_details", "reasoning_tokens")),
    "total_tokens": (("total_tokens",),),
}


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _at(value: Mapping, path: tuple[str, ...]) -> Any:
    for key in path:
        value = _mapping(value).get(key)
    return value


def _normalize_sources(sources: Iterable[Mapping]) -> dict[str, Any]:
    sources = list(sources)
    result: dict[str, Any] = {}
    issues = []
    for field, aliases in _ALIASES.items():
        values = [value for source in sources for path in aliases
                  if (value := _at(source, path)) is not None]
        valid = all(type(value) is int and 0 <= value <= 10**12 for value in values)
        if not valid:
            result[field] = None
            issues.append(f"invalid_{field}")
        elif len(set(values)) > 1:
            result[field] = None
            issues.append(f"conflicting_{field}")
        else:
            result[field] = values[0] if values else None
    incoming, cached, outgoing = (result[key] for key in
                                  ("input_tokens", "cached_input_tokens", "output_tokens"))
    if incoming is not None and cached is not None and cached > incoming:
        result["cached_input_tokens"] = cached = None
        issues.append("cached_input_exceeds_input")
    result["uncached_input_tokens"] = incoming-cached if incoming is not None and cached is not None else None
    reasoning = result["reasoning_tokens"]
    if reasoning is not None and outgoing is not None and reasoning > outgoing:
        result["reasoning_tokens"] = None
        issues.append("reasoning_exceeds_output")
    if incoming is not None and outgoing is not None:
        calculated = incoming+outgoing
        if result["total_tokens"] is None and not any("total_tokens" in issue for issue in issues):
            result["total_tokens"] = calculated
        elif result["total_tokens"] != calculated:
            result["total_tokens"] = None
            issues.append("inconsistent_total_tokens")
    result["unknown_fields"] = [key for key in TOKEN_FIELDS if result[key] is None]
    result["issues"] = sorted(set(issues))
    return result


def normalize_usage(usage: Mapping | None) -> dict[str, Any]:
    """Normalize a Responses, Chat Completions or raw Codex usage dictionary."""
    return _normalize_sources([_mapping(usage)])


def _usage_sources(event: Mapping) -> list[Mapping]:
    # These are mirrors of ONE request, not independently chargeable requests.
    sources = [_mapping(event.get("usage"))]
    for name in ("diagnostics", "transport", "response", "payload"):
        sources.append(_mapping(_mapping(event.get(name)).get("usage")))
    return sources


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"Invalid price: {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid price: {field}") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"Invalid price: {field}")
    return result


def _price_event(usage: Mapping, price: Mapping | None) -> tuple[Decimal | None, str | None, list[str]]:
    if price is None:
        return None, None, ["model_price_missing"]
    currency = price.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise ValueError("An explicit price currency is required")
    rates = {key: _decimal(price[key], key) if key in price else None for key in
             ("input_per_million", "cached_input_per_million", "output_per_million")}
    incoming, cached, outgoing = (usage[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
    reasons = []
    if incoming is None or outgoing is None:
        reasons.append("input_or_output_usage_unknown")
    if rates["input_per_million"] is None or rates["output_per_million"] is None:
        reasons.append("input_or_output_price_missing")
    if reasons:
        return None, currency, reasons
    # Equal input rates make the cache split immaterial; no invented zeroes.
    if cached is None:
        if incoming == 0 or rates["cached_input_per_million"] == rates["input_per_million"]:
            input_cost = incoming*rates["input_per_million"]
        else:
            return None, currency, ["cached_input_usage_unknown"]
    elif cached and rates["cached_input_per_million"] is None:
        return None, currency, ["cached_input_price_missing"]
    else:
        input_cost = (incoming-cached)*rates["input_per_million"]
        if cached:
            input_cost += cached*rates["cached_input_per_million"]
    return (input_cost + outgoing*rates["output_per_million"])/Decimal(1_000_000), currency, []


def aggregate_usage_events(events: Iterable[Mapping], *, prices: Mapping | None = None,
                           default_model: str | None = None) -> dict[str, Any]:
    """Deduplicate explicit request IDs and return JSON-safe totals and costs.

    ``totals`` is null per unknown field; ``known_subtotal`` is explicitly only
    the observed subset. ``estimated_cost`` is null unless every event can be
    priced in one currency. Money uses decimal strings, never binary floats.
    Events without IDs remain distinct, even if their token counts are equal.
    Conflicting duplicate counters become unknown instead of choosing one.
    """
    grouped: dict[tuple, dict] = {}
    supplied_count = 0
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ValueError("Usage events must be mappings")
        supplied_count += 1
        identity = event.get("event_id")
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise ValueError("event_id must be a nonempty string")
        key = ("id", identity) if identity else ("anonymous", index)
        group = grouped.setdefault(key, {"event_id": identity, "sources": [], "models": set(),
                                         "providers": set(), "stages": set(), "status": None,
                                         "elapsed_seconds": None})
        group["sources"].extend(_usage_sources(event))
        if event.get("status") in {"completed", "failed", "pending", "cancelled", "incomplete"}:
            group["status"] = event["status"]
        elapsed = event.get("elapsed_seconds")
        if type(elapsed) in (int, float) and 0 <= elapsed < 10**12:
            group["elapsed_seconds"] = elapsed
        for plural, singular in (("models", "model"), ("providers", "provider"), ("stages", "stage")):
            value = event.get(singular)
            if isinstance(value, str) and value:
                group[plural].add(value)
    normalized = []
    costs: dict[str, Decimal] = {}
    currencies = set()
    unpriced_count = 0
    for group in grouped.values():
        usage = _normalize_sources(group.pop("sources"))
        models = group.pop("models")
        model = next(iter(models)) if len(models) == 1 else default_model if not models else None
        issues = list(usage["issues"])
        if len(models) > 1:
            issues.append("conflicting_model")
        providers = group.pop("providers")
        if len(providers) > 1:
            issues.append("conflicting_provider")
        price = _mapping(prices).get(model) if model else None
        if price is not None and not isinstance(price, Mapping):
            raise ValueError("Model prices must be mappings")
        amount, currency, reasons = _price_event(usage, price)
        if issues:
            amount = None
            reasons.append("usage_or_identity_conflict")
        if currency:
            currencies.add(currency)
        if amount is None:
            unpriced_count += 1
        else:
            costs[currency] = costs.get(currency, Decimal(0))+amount
        normalized.append({"event_id": group["event_id"], "model": model,
                           "provider": next(iter(providers)) if len(providers) == 1 else None,
                           "stages": sorted(group["stages"]), "usage": usage,
                           "status": group["status"], "elapsed_seconds": group["elapsed_seconds"],
                           "issues": issues, "estimated_cost": str(amount) if amount is not None else None,
                           "currency": currency, "cost_unknown_reasons": reasons})
    subtotal = {key: sum(item["usage"][key] or 0 for item in normalized) for key in TOKEN_FIELDS}
    unknown = {key: sum(item["usage"][key] is None for item in normalized) for key in TOKEN_FIELDS}
    complete_cost = bool(normalized) and unpriced_count == 0 and len(currencies) == 1
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    return {"event_count": len(normalized), "supplied_event_count": supplied_count,
            "deduplicated_event_count": supplied_count-len(normalized),
            "totals": {key: subtotal[key] if unknown[key] == 0 else None for key in TOKEN_FIELDS},
            "known_subtotal": subtotal, "unknown_event_counts": unknown,
            "unknown_fields": [key for key in TOKEN_FIELDS if unknown[key]],
            "estimated_cost": str(costs[currency]) if complete_cost else None,
            "currency": currency, "cost_complete": complete_cost,
            "cost_basis": "simulated_model_token_rates",
            "unpriced_event_count": unpriced_count,
            "known_cost_by_currency": {key: str(value) for key, value in sorted(costs.items())},
            "events": normalized}


def collect_run_usage_events(run: Mapping, *, run_id: str | None = None) -> list[dict[str, Any]]:
    """Extract one persisted run/revision, without recursively counting mirrors.

    Prefer live per-request events for benchmarks. Older snapshots may omit
    interrupted requests and retain only two question reviews. This collector
    cannot establish complete coverage or reconcile different revisions. Do
    not sum overlapping snapshots/revisions as though each were new work.
    Explicit reused source stages are skipped. Reported request counts create
    unknown placeholders when detailed records are absent.
    """
    if not isinstance(run, Mapping):
        raise ValueError("run must be a mapping")
    record = _mapping(run.get("result")) or run
    state = _mapping(record.get("state"))
    prefix = run_id or str(run.get("runId") or run.get("run_id") or "run")
    provider = _mapping(record.get("provider"))
    trace = record.get("trace", state.get("trace", []))
    trace = [item for item in trace if isinstance(item, Mapping)] if isinstance(trace, list) else []
    events: list[dict[str, Any]] = []

    def add(metrics: Mapping, path: str, stage: str, info: Mapping | None = None) -> None:
        selected = info or provider
        events.append({"event_id": f"{prefix}:{path}", "stage": stage,
                       "model": selected.get("model", provider.get("model")),
                       "provider": selected.get("mode", provider.get("mode")),
                       "usage": dict(_mapping(metrics.get("usage"))),
                       "diagnostics": {"usage": dict(_mapping(_mapping(metrics.get("diagnostics")).get("usage")))},
                       "transport": {"usage": dict(_mapping(_mapping(metrics.get("transport")).get("usage")))},
                       "coverage": "persisted_snapshot_partial_possible"})

    def attempts(metrics: Mapping, path: str, stage: str, info: Mapping | None = None,
                 default_count: int = 1) -> None:
        detailed = metrics.get("attempts")
        detailed = [item for item in detailed if isinstance(item, Mapping)] if isinstance(detailed, list) else []
        requested = metrics.get("requestCount", default_count)
        count = requested if type(requested) is int and 0 <= requested <= 10000 else default_count
        if detailed:
            for index, attempt in enumerate(detailed):
                add(attempt, f"{path}:attempt:{index}", stage, info)
            count -= len(detailed)
        elif count == 1:
            add(metrics, path, stage, info)
            count = 0
        # With >1 unitemized requests, a parent usage value may describe only
        # the last request. Its allocation is unknown; never multiply it.
        for index in range(max(0, count)):
            add({}, f"{path}:unrecorded:{index}", stage, info)

    def stage_reused(action: str) -> bool:
        matches = [item for item in trace if item.get("action") == action]
        return bool(matches) and matches[-1].get("reused") is True

    review_trace = []
    question_trace = []
    for index, item in enumerate(trace):
        if isinstance(item.get("providerCall"), Mapping):
            add(item["providerCall"], f"trace:{index}:provider", "planning")
        if item.get("action") == "independent_drawing_review":
            review_trace.append(item)
            result = _mapping(item.get("result"))
            attempts(_mapping(result.get("providerMetrics")), f"trace:{index}:review", "drawing_review")
        if item.get("action") == "source_question_review":
            question_trace.append(item)

    source = _mapping(record.get("sourceTranscription", state.get("sourceTranscription")))
    if source and not stage_reused("read_source"):
        info = _mapping(source.get("provider"))
        regions = source.get("regionReadings")
        regions = [item for item in regions if isinstance(item, Mapping)] if isinstance(regions, list) else []
        before = len(events)
        for index, region in enumerate(regions):
            attempts(region, f"source:region:{index}", "source_reading", info)
        count = info.get("requestCount")
        if type(count) is int and 0 <= count <= 10000:
            for index in range(max(0, count-(len(events)-before))):
                add({}, f"source:unrecorded:{index}", "source_reading", info)
        elif not regions:
            add(info, "source:unknown", "source_reading", info)

    spatial = _mapping(record.get("sourceSpatialContract", state.get("sourceSpatialContract")))
    if spatial and not stage_reused("interpret_source_spatial"):
        info = _mapping(spatial.get("provider"))
        attempts(info, "spatial", "source_spatial", info, default_count=0)

    reviews = record.get("sourceQuestionReviews", state.get("sourceQuestionReviews", []))
    reviews = [item for item in reviews if isinstance(item, Mapping)] if isinstance(reviews, list) else []
    if question_trace:
        for index, item in enumerate(question_trace):
            if item.get("reused") is True:
                continue
            fingerprint = item.get("inputFingerprint")
            match = next((review for review in reviews if fingerprint and review.get("inputFingerprint") == fingerprint), {})
            attempts(_mapping(match.get("providerMetrics")), f"question:{index}", "source_questions")
    else:
        for index, review in enumerate(reviews):
            attempts(_mapping(review.get("providerMetrics")), f"question:{index}", "source_questions", default_count=0)

    if not review_trace:
        review = _mapping(record.get("drawingReview", state.get("drawingReview")))
        independent = _mapping(review.get("independentReview")) or review
        if isinstance(independent.get("providerMetrics"), Mapping):
            attempts(independent["providerMetrics"], "review", "drawing_review", default_count=0)
    return events
