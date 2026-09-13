"""The cost probe must meter calls without changing results or leaking content."""
import importlib.util
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest


SPEC = importlib.util.spec_from_file_location("cad_cost_probe", Path(__file__).parents[1] / "scripts" / "cad_cost_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class FakeProvider:
    provider_info = {"mode": "codex", "model": "test-model"}

    def __call__(self, body, timeout, on_diagnostics=None):
        usage = {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 40, "reasoning_tokens": 5}
        for _ in range(3):
            on_diagnostics({"usage": usage, "terminalStatus": "completed", "secret": "never-store-this"})
        return {"usage": usage, "output": body["private_output"]}


def test_many_diagnostic_snapshots_are_one_call_and_do_not_copy_output(tmp_path):
    metered = probe.MeteredProvider(FakeProvider(), tmp_path, "one")
    result = metered({"instructions": "只转录图片", "private_output": "private-model-text"}, 30)
    assert result["output"] == "private-model-text"
    text = (tmp_path / "usage-events.json").read_text()
    events = json.loads(text)
    assert len(events) == 1
    assert events[0]["status"] == "completed"
    assert events[0]["usage"]["input_tokens"] == 100
    assert events[0]["stage"] == "source_reading"
    assert "private-model-text" not in text and "never-store-this" not in text


def test_concurrent_calls_and_equal_usage_are_distinct(tmp_path):
    metered = probe.MeteredProvider(FakeProvider(), tmp_path, "many")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: metered({"private_output": "ok"}, 30), range(8)))
    events = json.loads((tmp_path / "usage-events.json").read_text())
    assert len(events) == len({e["event_id"] for e in events}) == 8
    from app.cad_usage_accounting import aggregate_usage_events
    summary = aggregate_usage_events(events)
    assert summary["totals"]["input_tokens"] == 800
    assert summary["totals"]["output_tokens"] == 160


def test_failure_preserves_known_usage_without_error_body(tmp_path):
    class Failing(FakeProvider):
        def __call__(self, body, timeout, on_diagnostics=None):
            error = ValueError("private-provider-error")
            error.diagnostics = {"usage": {"input_tokens": 25}, "errorCategory": "timeout"}
            raise error
    metered = probe.MeteredProvider(Failing(), tmp_path, "failure")
    with pytest.raises(ValueError, match="private-provider-error"):
        metered({}, 30)
    text = (tmp_path / "usage-events.json").read_text()
    event = json.loads(text)[0]
    assert event["status"] == "failed"
    assert event["usage"] == {"input_tokens": 25}
    assert "private-provider-error" not in text
    from app.cad_usage_accounting import aggregate_usage_events
    summary = aggregate_usage_events([event])
    assert summary["totals"]["output_tokens"] is None
    assert summary["estimated_cost"] is None
