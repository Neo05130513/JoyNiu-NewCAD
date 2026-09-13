"""Pure accounting tests; no model, provider, credentials or payment access."""
from __future__ import annotations

import copy
import json

import pytest

from app.cad_usage_accounting import aggregate_usage_events, collect_run_usage_events, normalize_usage


PRICE = {"example": {"currency": "CNY", "input_per_million": "2",
                     "cached_input_per_million": "0.2", "output_per_million": "8"}}


def event(event_id="request-1", **usage):
    return {"event_id": event_id, "model": "example", "usage": usage}


def test_codex_and_responses_use_the_same_cache_and_reasoning_subsets():
    codex = normalize_usage({"input_tokens": 1000, "cached_input_tokens": 400,
                             "output_tokens": 100, "reasoning_output_tokens": 80})
    api = normalize_usage({"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                           "input_tokens_details": {"cached_tokens": 400},
                           "output_tokens_details": {"reasoning_tokens": 80}})
    assert codex == api
    assert codex["uncached_input_tokens"] == 600
    assert codex["total_tokens"] == 1100
    summary = aggregate_usage_events([event(**codex)], prices=PRICE)
    assert summary["estimated_cost"] == "0.00208"
    assert summary["cost_complete"] is True


def test_unknown_cache_is_not_zero_and_preserves_known_input_output():
    summary = aggregate_usage_events([event(input_tokens=100, output_tokens=20)], prices=PRICE)
    assert summary["totals"]["input_tokens"] == 100
    assert summary["known_subtotal"]["output_tokens"] == 20
    assert summary["totals"]["cached_input_tokens"] is None
    assert summary["totals"]["uncached_input_tokens"] is None
    assert summary["totals"]["total_tokens"] == 120
    assert summary["estimated_cost"] is None
    assert summary["events"][0]["cost_unknown_reasons"] == ["cached_input_usage_unknown"]
    assert normalize_usage({"cached_input_tokens": 0})["cached_input_tokens"] == 0


def test_failed_request_is_unknown_and_not_free():
    summary = aggregate_usage_events([
        event("ok", input_tokens=100, cached_tokens=0, output_tokens=20),
        {"event_id": "fail", "model": "example", "status": "failed", "elapsed_seconds": 17.5},
    ], prices=PRICE)
    assert summary["event_count"] == 2
    assert summary["totals"]["input_tokens"] is None
    assert summary["known_subtotal"]["input_tokens"] == 100
    assert summary["unknown_event_counts"]["input_tokens"] == 1
    assert summary["estimated_cost"] is None
    assert summary["known_cost_by_currency"] == {"CNY": "0.00036"}
    assert summary["events"][1]["status"] == "failed"
    assert summary["events"][1]["elapsed_seconds"] == 17.5


def test_payload_diagnostics_and_duplicate_event_ids_are_one_call():
    first = event(input_tokens=100, output_tokens=20)
    first["diagnostics"] = {"usage": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 40}}
    first["transport"] = copy.deepcopy(first["diagnostics"])
    second = {"event_id": "request-1", "stage": "source_reading", "status": "completed",
              "response": {"usage": {"input_tokens": 100, "output_tokens": 20}}}
    summary = aggregate_usage_events([first, second], prices=PRICE)
    assert summary["event_count"] == 1
    assert summary["deduplicated_event_count"] == 1
    assert summary["totals"]["input_tokens"] == 100
    assert summary["totals"]["cached_input_tokens"] == 40
    assert summary["events"][0]["stages"] == ["source_reading"]
    assert summary["events"][0]["status"] == "completed"


def test_equal_usage_is_not_an_identity_for_distinct_calls():
    repeated = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}}
    summary = aggregate_usage_events([repeated, repeated], default_model="example")
    assert summary["event_count"] == 2
    assert summary["totals"]["input_tokens"] == 20
    assert summary["estimated_cost"] is None
    assert summary["events"][0]["model"] == "example"


def test_conflicting_duplicate_is_unknown_instead_of_arbitrarily_chosen():
    summary = aggregate_usage_events([event(input_tokens=20), event(input_tokens=30)])
    assert summary["totals"]["input_tokens"] is None
    assert "conflicting_input_tokens" in summary["events"][0]["issues"]
    assert summary["events"][0]["estimated_cost"] is None


@pytest.mark.parametrize("bad", [-1, True, 1.5, "100", float("nan"), 10**13])
def test_invalid_counters_remain_unknown(bad):
    result = normalize_usage({"input_tokens": bad})
    assert result["input_tokens"] is None
    assert "invalid_input_tokens" in result["issues"]


def test_impossible_cache_reasoning_and_totals_are_reported():
    result = normalize_usage({"input_tokens": 10, "cached_tokens": 15, "output_tokens": 5,
                              "reasoning_tokens": 9, "total_tokens": 99})
    assert result["cached_input_tokens"] is None
    assert result["reasoning_tokens"] is None
    assert result["total_tokens"] is None
    assert set(result["issues"]) == {"cached_input_exceeds_input", "reasoning_exceeds_output",
                                     "inconsistent_total_tokens"}


def test_equal_input_rates_allow_price_without_guessing_cache():
    rates = copy.deepcopy(PRICE)
    rates["example"]["cached_input_per_million"] = "2"
    summary = aggregate_usage_events([event(input_tokens=100, output_tokens=20)], prices=rates)
    assert summary["estimated_cost"] == "0.00036"
    assert summary["totals"]["cached_input_tokens"] is None


def test_no_price_is_invented_for_codex_or_unknown_models():
    summary = aggregate_usage_events([
        {"model": "gpt-6-astra", "provider": "codex", "usage": {"input_tokens": 100, "output_tokens": 20}},
    ])
    assert summary["estimated_cost"] is None
    assert summary["events"][0]["cost_unknown_reasons"] == ["model_price_missing"]
    assert summary["cost_basis"] == "simulated_model_token_rates"


@pytest.mark.parametrize("bad", ["NaN", "-1", "Infinity", True, None])
def test_invalid_price_rejected(bad):
    rates = copy.deepcopy(PRICE)
    rates["example"]["input_per_million"] = bad
    with pytest.raises(ValueError, match="Invalid price"):
        aggregate_usage_events([event(input_tokens=100, cached_tokens=0, output_tokens=20)], prices=rates)


def test_mixed_currencies_have_separate_subtotals():
    rates = copy.deepcopy(PRICE)
    rates["other"] = {**rates["example"], "currency": "USD"}
    first = event("a", input_tokens=100, cached_tokens=0, output_tokens=20)
    second = {**event("b", input_tokens=100, cached_tokens=0, output_tokens=20), "model": "other"}
    summary = aggregate_usage_events([first, second], prices=rates)
    assert summary["estimated_cost"] is None
    assert summary["currency"] is None
    assert summary["known_cost_by_currency"] == {"CNY": "0.00036", "USD": "0.00036"}


def test_collector_counts_request_leaves_without_state_or_review_mirrors():
    usage = {"input_tokens": 100, "cached_tokens": 0, "output_tokens": 20}
    attempt = {"usage": usage, "diagnostics": {"usage": usage}}
    independent = {"providerMetrics": {"requestCount": 2, "attempts": [attempt, attempt], "usage": usage}}
    run = {"provider": {"mode": "codex", "model": "example"},
           "trace": [{"action": "edit_plan", "providerCall": attempt},
                     {"action": "independent_drawing_review", "result": independent}],
           "sourceTranscription": {"provider": {"requestCount": 2},
                                   "regionReadings": [{"attempts": [attempt, attempt], "usage": usage}]},
           "sourceSpatialContract": {"provider": {"requestCount": 1, "diagnostics": {"usage": usage}}},
           "drawingReview": {"independentReview": independent}}
    run["state"] = copy.deepcopy(run)
    before = copy.deepcopy(run)
    events = collect_run_usage_events(run, run_id="demo")
    summary = aggregate_usage_events(events, prices=PRICE)
    assert run == before
    assert summary["event_count"] == 6
    assert summary["totals"]["input_tokens"] == 600
    assert len({item["event_id"] for item in events}) == 6
    assert all(item["event_id"].startswith("demo:") for item in events)
    json.dumps(summary, allow_nan=False)


def test_collector_skips_explicitly_reused_sources_and_marks_missing_requests():
    run = {"trace": [{"action": "read_source", "reused": True},
                     {"action": "interpret_source_spatial", "reused": True},
                     {"action": "independent_drawing_review", "result": {"providerMetrics": {"requestCount": 2}}}],
           "sourceTranscription": {"provider": {"requestCount": 3}},
           "sourceSpatialContract": {"provider": {"requestCount": 1}}}
    summary = aggregate_usage_events(collect_run_usage_events(run))
    assert summary["event_count"] == 2
    assert summary["totals"]["input_tokens"] is None
    assert summary["unknown_event_counts"]["input_tokens"] == 2


def test_chat_completion_usage_and_zero_request_are_supported():
    usage = normalize_usage({"prompt_tokens": 40, "completion_tokens": 12,
                             "prompt_tokens_details": {"cached_tokens": 30},
                             "completion_tokens_details": {"reasoning_tokens": 6}})
    assert usage["uncached_input_tokens"] == 10
    assert usage["total_tokens"] == 52
    assert collect_run_usage_events({"sourceSpatialContract": {"provider": {"requestCount": 0}}}) == []
