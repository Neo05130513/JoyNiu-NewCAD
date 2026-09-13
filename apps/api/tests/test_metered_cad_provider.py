"""Actual call boundaries, snapshots, cancellation and pending usage delivery."""
from concurrent.futures import ThreadPoolExecutor
import os
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest

from app.cad_agent_store import CadRunStore
from app.cad_job_registry import CadJobRegistry
from app.metered_cad_provider import CadOperationCancelled, MeteredCadProvider, RunCallContext, instrument_runner
from .test_cad_agent_api import api


class BillingRecorder:
    def __init__(self):
        self.calls = {}
        self.fail = False

    def record_usage(self, **kwargs):
        if self.fail:
            raise RuntimeError("temporary billing failure")
        if kwargs["call_id"] in self.calls:
            assert self.calls[kwargs["call_id"]] == kwargs
        self.calls[kwargs["call_id"]] = kwargs


def context(tmp_path):
    registry = CadJobRegistry(CadRunStore(tmp_path / "runs"))
    billing = BillingRecorder()
    return RunCallContext(registry=registry, billing=billing, owner="test-owner", job_id="job-test", attempt_id="attempt-test"), billing


class SnapshotProvider:
    supports_diagnostics = True
    provider_info = {"mode": "codex", "model": "test-model"}

    def __call__(self, body, timeout, on_diagnostics):
        on_diagnostics({"usage": {"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 800, "reasoning_tokens": 20}})
        on_diagnostics({"usage": {"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 800, "reasoning_tokens": 50}})
        return {"usage": {"input_tokens": 1000, "output_tokens": 100}, "output": []}


def test_snapshots_are_one_call_and_cache_reasoning_subsets_are_not_added(tmp_path):
    ctx, billing = context(tmp_path)
    wrapped = MeteredCadProvider(SnapshotProvider(), ctx)
    body = {"model": "test-model"}
    wrapped(body, 10)
    wrapped(body, 10)  # A real retry is a distinct call even with identical input.
    assert len(billing.calls) == 2
    for usage in billing.calls.values():
        assert usage["input_tokens"] == 1000 and usage["output_tokens"] == 100
        assert usage["cached_input_tokens"] == 800 and usage["reasoning_output_tokens"] == 50
        assert usage["cost_micro_usd"] is None
    ctx.registry.deliver_usage(billing)
    assert len(billing.calls) == 2


def test_invalid_request_does_not_leave_an_active_call_and_null_does_not_erase_counts(tmp_path):
    from app.metered_cad_provider import normalized_usage
    ctx, billing = context(tmp_path)
    with pytest.raises(TypeError):
        MeteredCadProvider(SnapshotProvider(), ctx)(None, 1)
    assert ctx.is_idle() and not billing.calls
    usage = normalized_usage({"usage": {"input_tokens": None, "input_tokens_details": {"cached_tokens": None}}},
                             {"usage": {"input_tokens": 9, "input_tokens_details": {"cached_tokens": 2}}}, "failed")
    assert usage["input_tokens"] == 9 and usage["output_tokens"] is None
    assert usage["cached_input_tokens"] == 2


def test_admission_runs_once_before_first_actual_call_without_mid_task_balance_checks(tmp_path):
    ctx, billing = context(tmp_path)
    checks = []
    def admission():
        checks.append("first")
        assert not billing.calls and ctx.is_idle()
    ctx.admission_check = admission
    wrapped = MeteredCadProvider(SnapshotProvider(), ctx)
    wrapped({"model": "test-model"}, 1)
    wrapped({"model": "test-model"}, 1)
    assert checks == ["first"] and len(billing.calls) == 2


def test_unknown_usage_on_error_is_journalled_once_and_delivery_retries(tmp_path):
    ctx, billing = context(tmp_path)
    billing.fail = True
    def failing(body, timeout):
        raise RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        MeteredCadProvider(failing, ctx)({}, 1)
    assert ctx.is_idle()
    with sqlite3.connect(ctx.registry.database) as db:
        assert db.execute("SELECT count(*) FROM cad_provider_calls WHERE completed_at IS NOT NULL AND delivered=0").fetchone()[0] == 1
    billing.fail = False
    ctx.registry.deliver_usage(billing)
    usage = next(iter(billing.calls.values()))
    assert usage["input_tokens"] is None and usage["output_tokens"] is None
    assert usage["outcome"] == "failed"


def test_crashed_provider_start_is_recovered_as_unknown_not_zero(tmp_path):
    ctx, billing = context(tmp_path)
    ctx.registry.begin_call(call_id="crashed-call", owner="test-owner", job_id="job-test", attempt_id="attempt-test", stage="planner", identity={"provider": "codex", "model": "test-model"})
    with sqlite3.connect(ctx.registry.database) as db:
        db.execute("UPDATE cad_provider_calls SET worker_instance='old-process-instance'")
    ctx.registry.reconcile()
    ctx.registry.deliver_usage(billing)
    record = billing.calls["crashed-call"]
    assert record["input_tokens"] is None and record["output_tokens"] is None
    assert record["cost_micro_usd"] is None and record["outcome"] == "interrupted"
    ctx.registry.deliver_usage(billing)
    assert len(billing.calls) == 1


def test_cancellation_prevents_next_call_and_inflight_provider_is_not_declared_idle(tmp_path):
    ctx, billing = context(tmp_path)
    started, release = threading.Event(), threading.Event()
    def uncancellable(body, timeout):
        started.set()
        release.wait(2)
        return {"usage": {"input_tokens": 4, "output_tokens": 2}}
    provider = MeteredCadProvider(uncancellable, ctx)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(provider, {}, 5)
        assert started.wait(1)
        ctx.cancel_event.set()
        assert not ctx.is_idle()
        with pytest.raises(CadOperationCancelled):
            provider({}, 1)
        release.set()
        with pytest.raises(CadOperationCancelled):
            pending.result(timeout=1)
    assert ctx.is_idle() and len(billing.calls) == 1
    assert next(iter(billing.calls.values()))["input_tokens"] == 4


def test_runner_wiring_is_per_instance_and_covers_readers_reviews_questions(tmp_path):
    from app.cad_agent import CadAgentService
    from app.cad_source_questions import QUESTION_READING_PROMPT
    ctx, billing = context(tmp_path)
    raw = SnapshotProvider()
    runner = CadAgentService(provider_call=raw)
    wrapped = instrument_runner(runner, ctx)
    assert runner.provider_call is raw
    assert wrapped.source_reader is not runner.source_reader
    wrapped.source_reader.provider_call({}, 1)
    wrapped.spatial_interpreter.provider_call({}, 1)
    wrapped.provider_call({"text": {"format": {"name": "joyniu_independent_drawing_review"}}}, 1)
    wrapped.provider_call({"instructions": QUESTION_READING_PROMPT}, 1)
    assert {row["stage"] for row in billing.calls.values()} == {"source_reader", "source_spatial", "drawing_review", "source_question_review"}


@pytest.mark.parametrize("with_image", [False, True])
def test_unconfigured_default_runner_stops_before_instrumented_call_or_source_reading(tmp_path, monkeypatch, with_image):
    from app import ai_proxy
    from app.cad_agent import CadAgentService
    from tests.test_cad_agent import raster
    monkeypatch.setenv("JOYNIU_CAD_PROVIDER", "relay")
    monkeypatch.setattr(ai_proxy, "_provider_configured", lambda: False)
    monkeypatch.setattr(ai_proxy, "_provider_key", lambda: pytest.fail("unconfigured provider must not be called"))
    ctx, billing = context(tmp_path)
    runner = instrument_runner(CadAgentService(), ctx)
    assert isinstance(runner.provider_call, MeteredCadProvider)
    result = runner.run(message="创建 10×20×30 毫米方块", files=[raster()] if with_image else [], output_dir=tmp_path / "build")
    assert result["status"] == "failed"
    assert result["provider"]["lastErrorCode"] == "not_configured"
    assert result["provider"]["attempts"] == 0
    assert "未调用模型" in result["message"]
    assert result["sourceTranscription"] is None and result["trace"] == []
    assert ctx.is_idle() and not billing.calls
    with sqlite3.connect(ctx.registry.database) as db:
        assert db.execute("SELECT count(*) FROM cad_provider_calls").fetchone()[0] == 0


def test_instrumented_custom_provider_does_not_require_remote_credentials(tmp_path, monkeypatch):
    from app import ai_proxy
    from app.cad_agent import CadAgentService
    from app.cad_provider import provider_is_configured
    monkeypatch.setattr(ai_proxy, "_provider_configured", lambda: False)
    ctx, billing = context(tmp_path)
    wrapped = instrument_runner(CadAgentService(provider_call=SnapshotProvider()), ctx)
    assert provider_is_configured(wrapped.provider_call)
    wrapped.provider_call({"model": "test-model"}, 1)
    assert len(billing.calls) == 1
    assert ai_proxy._provider_error_code(ai_proxy.AIProviderNotConfigured("no secret here")) == "not_configured"


def test_source_location_route_records_actual_usage_once_and_cache_does_not_rebill(api, monkeypatch):
    from .test_cad_source_locations import with_source, output, located
    client, store, agent, _ = api
    run = with_source(api, monkeypatch)
    def provider(body, timeout):
        return {**output(located()), "usage": {"input_tokens": 40, "output_tokens": 8}}
    monkeypatch.setattr(agent, "provider_call", provider, raising=False)
    url = f"/api/v1/cad-agent/runs/{run['runId']}/source-locations"
    assert client.post(url, json={}).json()["status"] == "succeeded"
    assert client.post(url, json={}).json()["status"] == "succeeded"
    with sqlite3.connect(store.database) as db:
        rows = db.execute("SELECT stage,usage_json,attempt_id FROM cad_provider_calls WHERE stage='source_locations'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "source_locations"
    assert json.loads(rows[0][1])["input_tokens"] == 40
    assert rows[0][2].startswith("location_") and rows[0][2] != run["runId"]


def test_codex_cancellation_reaps_a_real_child_process_without_calling_ai(tmp_path):
    from app.cad_codex_provider import _run
    event = threading.Event()
    marker = tmp_path / "pid.txt"
    script = "from pathlib import Path; import os,time; Path('pid.txt').write_text(str(os.getpid())); time.sleep(30)"
    diagnostics = {"eventCount": 0, "responseBytes": 0}
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(_run, [sys.executable, "-c", script], "", tmp_path, time.monotonic() + 10, diagnostics, lambda: None, cancel_event=event)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.01)
        assert marker.exists()
        pid = int(marker.read_text())
        event.set()
        with pytest.raises(CadOperationCancelled):
            future.result(timeout=2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_geometry_cancellation_reaps_a_real_child_without_building_a_model(tmp_path, monkeypatch):
    from app import cad_executor
    from tests.test_cad_agent_api import PLAN
    real_popen = cad_executor.subprocess.Popen
    marker = tmp_path / "geometry-pid.txt"
    script = f"from pathlib import Path; import os,time; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    def popen(args, *positional, **kwargs):
        if "app.cad_executor" in args:
            args = [sys.executable, "-c", script]
        return real_popen(args, *positional, **kwargs)
    monkeypatch.setattr(cad_executor.subprocess, "Popen", popen)
    event = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(cad_executor.execute_cad_plan, PLAN, tmp_path / "output", cancel_event=event)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(.01)
        assert marker.exists()
        pid = int(marker.read_text())
        event.set()
        result = pending.result(timeout=3)
    assert result["status"] == "failed" and result["errors"][0]["code"] == "user_cancelled"
    assert result["artifacts"] == {}
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
