"""Durable background-job regressions with local threads, never a real model."""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.cad_agent_api import create_cad_agent_router, _public_result
from app.cad_agent_store import CadRunConflict, CadRunStore, PROCESS_INSTANCE
from tests.test_cad_agent_api import PLAN, fake_execute


def request():
    return Request({"type": "http", "method": "POST", "path": "/api/v1/cad-agent/run",
                    "headers": [], "client": ("127.0.0.1", 3210)})


def endpoint(router, suffix):
    return next(route.endpoint for route in router.routes if route.path.endswith(suffix))


async def start(router, **kwargs):
    return await endpoint(router, "/cad-agent/run")(
        request(), message="Build a plate", modelState="{}", history="[]", files=None,
        authorization=None, **kwargs,
    )


async def public(router, run_id):
    return await endpoint(router, "/cad-agent/runs/{run_id}")(run_id, request(), revision=None, authorization=None)


def event_data(frame):
    return json.loads(frame.split("\ndata: ", 1)[1])


async def terminal(store, run_id):
    for _ in range(200):
        value = store.load(run_id)
        if value["status"] != "running":
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("local job did not terminate")


class BlockingAgent:
    def __init__(self, *, fail=False):
        self.release = threading.Event()
        self.fail = fail
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        directory = kwargs["output_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        observations = [{"id": "one_plate", "kind": "solid_count", "expected": 1,
                         "source": {"type": "user", "text": "one connected plate"}}]
        checkpoint = {"status": "running", "plan": PLAN, "observations": observations,
                      "trace": [{"action": "edit_plan", "message": "saved one plate"}]}
        (directory / "checkpoint.json").write_text(json.dumps(checkpoint))
        kwargs["progress"]({"stage": "plan_saved", "message": "Draft saved"})
        if not self.release.wait(3):
            raise AssertionError("test did not release local agent")
        if self.fail:
            raise RuntimeError("PRIVATE provider message must not be exposed")
        kwargs["progress"]({"stage": "inspect_geometry", "message": "Local checks complete"})
        built = fake_execute(PLAN, directory)
        return {**built, "status": "review_required", "message": "Awaiting confirmation", "questions": [],
                "observations": observations, "trace": checkpoint["trace"],
                "drawingReview": {"status": "not_applicable"},
                "state": {"cadPlan": PLAN, "observations": observations, "trace": checkpoint["trace"]}}


def setup(tmp_path, agent=None):
    agent = agent or BlockingAgent()
    store = CadRunStore(tmp_path / "cad")
    services = SimpleNamespace(ai=SimpleNamespace(allow_anonymous=True))
    router = create_cad_agent_router(services, store, agent=agent, executor=fake_execute)
    return router, store, agent, services


def test_started_identity_is_durable_and_disconnect_keeps_background_job_and_get_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("JOYNIU_CAD_AGENT_TIMEOUT_SECONDS", "600")
    router, store, agent, _ = setup(tmp_path)
    async def scenario():
        response = await start(router)
        stream = response.body_iterator
        started = event_data(await anext(stream))
        assert started["status"] == "running" and started["revision"] == 1
        run_id = started["runId"]
        assert store.load(run_id)["status"] == "running"
        progress = event_data(await anext(stream))
        assert progress["runId"] == run_id and progress["stage"] == "plan_saved"
        running = await public(router, run_id)
        assert running["status"] == "running" and running["artifacts"] == [] and running["inspection"] is None
        assert running["progress"]["stage"] == "plan_saved"
        await stream.aclose()  # Browser/network disconnect; do not cancel work.
        assert store.load(run_id)["status"] == "running"
        agent.release.set()
        saved = await terminal(store, run_id)
        recovered = await public(router, run_id)
        assert saved["revision"] == recovered["revision"] == 1
        assert recovered["status"] == "review_required"
        assert [artifact["format"] for artifact in recovered["artifacts"]] == ["glb"]
        assert len(agent.calls) == 1 and agent.calls[0]["timeout_seconds"] == 600
        assert agent.calls[0]["max_turns"] == 20
        return run_id
    run_id = asyncio.run(scenario())
    assert CadRunStore(store.root).load(run_id)["status"] == "review_required"


def test_running_job_cannot_be_resubmitted_confirmed_or_read_by_another_owner(tmp_path):
    router, store, agent, _ = setup(tmp_path)
    async def scenario():
        response = await start(router)
        stream = response.body_iterator
        run_id = event_data(await anext(stream))["runId"]
        await anext(stream)
        with pytest.raises(HTTPException) as repeated:
            await endpoint(router, "/cad-agent/run")(
                request(), message="retry", modelState=json.dumps({"agentRun": {"runId": run_id, "revision": 1}}),
                history="[]", files=None, authorization=None,
            )
        assert repeated.value.status_code == 409 and repeated.value.detail["runId"] == run_id
        with pytest.raises(HTTPException) as confirm:
            await endpoint(router, "/cad-agent/confirm")(
                request(), payload={"runId": run_id, "revision": 1}, authorization=None,
            )
        assert confirm.value.status_code == 422
        other = running_record("b")
        other["owner"] = "another-user"
        store.save(other)
        with pytest.raises(HTTPException) as forbidden:
            await public(router, other["runId"])
        assert forbidden.value.status_code == 403
        await stream.aclose()
        agent.release.set()
        await terminal(store, run_id)
    asyncio.run(scenario())
    assert len(agent.calls) == 1


def test_actual_asgi_disconnect_ends_only_the_stream_and_final_result_remains_queryable(tmp_path):
    router, store, agent, _ = setup(tmp_path)
    app = FastAPI()
    app.include_router(router)
    async def scenario():
        disconnect = asyncio.Event()
        sent = []
        received_body = False
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": b"message=Build+plate", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and b'"plan_saved"' in message.get("body", b""):
                disconnect.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "method": "POST",
                 "scheme": "http", "path": "/api/v1/cad-agent/run", "raw_path": b"/api/v1/cad-agent/run",
                 "query_string": b"", "http_version": "1.1", "root_path": "",
                 "headers": [(b"host", b"localhost"), (b"content-type", b"application/x-www-form-urlencoded")],
                 "server": ("localhost", 80), "client": ("127.0.0.1", 3210)}
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=2)
            first = next(item["body"] for item in sent if item["type"] == "http.response.body" and b'"started"' in item.get("body", b""))
            run_id = event_data(first.decode())["runId"]
            assert store.load(run_id)["status"] == "running"
            agent.release.set()
            await terminal(store, run_id)
            assert (await public(router, run_id))["status"] == "review_required"
            assert len(agent.calls) == 1
        finally:
            agent.release.set()
    asyncio.run(scenario())


def test_unexpected_background_exception_persists_failed_checkpoint_and_is_resumable(tmp_path):
    router, store, agent, _ = setup(tmp_path, BlockingAgent(fail=True))
    async def scenario():
        response = await start(router)
        stream = response.body_iterator
        run_id = event_data(await anext(stream))["runId"]
        await anext(stream)
        await stream.aclose()
        agent.release.set()
        await terminal(store, run_id)
        result = await public(router, run_id)
        assert result["status"] == "failed" and result["plan"] == PLAN
        assert result["artifacts"] == [] and result["inspection"] is None
        assert result["provider"]["lastErrorCode"] == "task_exception"
        assert "PRIVATE" not in json.dumps(result)
        agent.fail = False
        resumed = await endpoint(router, "/cad-agent/run")(
            request(), message="continue saved draft", modelState=json.dumps({"agentRun": {"runId": run_id, "revision": 1}}),
            history="[]", files=None, authorization=None,
        )
        frames = [frame async for frame in resumed.body_iterator]
        final = event_data(next(frame for frame in frames if frame.startswith("event: result")))
        assert final["status"] == "review_required" and final["parentRunId"] == run_id
        assert agent.calls[1]["state"]["agentState"]["cadPlan"] == PLAN
        assert agent.calls[1]["state"]["agentState"]["observations"][0]["id"] == "one_plate"
    asyncio.run(scenario())


def test_api_shutdown_marks_job_interrupted_with_checkpoint_not_success(tmp_path):
    router, store, agent, _ = setup(tmp_path)
    async def scenario():
        try:
            async with router.lifespan_context(None):
                response = await start(router)
                stream = response.body_iterator
                run_id = event_data(await anext(stream))["runId"]
                await anext(stream)
                await stream.aclose()
            interrupted = store.load(run_id)
            assert interrupted["status"] == "interrupted"
            assert interrupted["plan"] == PLAN
            assert interrupted["artifacts"] == {} and interrupted["inspection"] is None
            assert interrupted["state"]["drawingReview"]["humanConfirmed"] is False
        finally:
            agent.release.set()
    asyncio.run(scenario())


def running_record(letter="a"):
    return {"runId": "cad_" + letter * 32, "revision": 1, "owner": "local-anonymous", "status": "running",
            "workerPid": os.getpid(), "workerInstance": PROCESS_INSTANCE, "downloadToken": "local-test-capability",
            "plan": None, "state": {}, "files": [], "artifacts": {}, "trace": [], "questions": [],
            "observations": [], "inspection": None, "progress": {"stage": "started", "message": "working"}}


def test_restart_recovers_abandoned_checkpoint_without_inheriting_artifacts_or_passed_flags(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    running = running_record()
    running["workerInstance"] = "previous-process-instance"
    store.save(running)
    build = store.directory(running["runId"]) / "builds"
    build.mkdir(parents=True)
    checkpoint = {"plan": PLAN, "observations": [{"id": "one", "kind": "solid_count", "expected": 1,
                   "source": {"type": "user", "text": "one plate"}}], "trace": [{"action": "edit_plan"}],
                  "inspection": {"valid": True}, "artifacts": {"step": {"path": "untrusted"}},
                  "sourceTranscription": {"candidateEvidence": True, "verified": False},
                  "retainedSourceDetails": [{"sha256": "test-source-hash"}]}
    (build / "checkpoint.json").write_text(json.dumps(checkpoint))
    restarted = CadRunStore(store.root)
    restored = restarted.load(running["runId"])
    assert restored["status"] == "interrupted" and restored["revision"] == 1
    assert restored["plan"] == PLAN
    assert restored["state"]["retainedSourceDetails"] == checkpoint["retainedSourceDetails"]
    assert restored["observations"] == checkpoint["observations"]
    assert restored["inspection"] is None and restored["artifacts"] == {}
    assert restored["drawingReview"]["humanConfirmed"] is False
    assert restored["trace"][-1]["code"] == "worker_interrupted"
    assert restarted.recover_interrupted() == 0


def test_new_store_does_not_interrupt_current_or_other_live_worker(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    current = running_record()
    other = running_record("b")
    other.update({"workerPid": os.getppid(), "workerInstance": "another-live-process"})
    store.save(current)
    store.save(other)
    restarted = CadRunStore(store.root)
    assert restarted.load(current["runId"])["status"] == "running"
    assert restarted.load(other["runId"])["status"] == "running"


def test_terminal_commit_is_once_only_owner_bound_and_progress_cannot_change_it(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    running = running_record()
    store.save(running)
    assert store.update_progress(running["runId"], running["owner"], {"stage": "plan_saved", "message": "saved"})
    completed = {**running, "status": "review_required", "plan": PLAN}
    with pytest.raises(CadRunConflict):
        store.complete_running({**completed, "owner": "different-user"})
    assert store.load(running["runId"])["status"] == "running"
    store.complete_running(completed)
    with pytest.raises(CadRunConflict):
        store.complete_running({**completed, "status": "failed"})
    assert store.update_progress(running["runId"], running["owner"], {"stage": "late", "message": "late"}) is False
    with pytest.raises(CadRunConflict):
        store.save({**completed, "revision": 2, "owner": "different-user"}, previous_revision=1)
    store.save({**completed, "revision": 2, "status": "ready"}, previous_revision=1)
    assert store.load(running["runId"], 1)["status"] == "review_required"
    assert store.load(running["runId"])["status"] == "ready"


def test_concurrent_terminal_writers_cannot_replace_each_others_result(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    running = running_record()
    store.save(running)
    barrier = threading.Barrier(2)
    def complete(status):
        barrier.wait(timeout=2)
        try:
            store.complete_running({**running, "status": status})
            return status
        except CadRunConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, ["review_required", "failed"]))
    assert results.count("conflict") == 1
    assert store.load(running["runId"])["status"] == next(value for value in results if value != "conflict")


def test_recovery_never_reads_another_runs_checkpoint_via_symlink(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    running = running_record()
    running["workerInstance"] = "previous-process-instance"
    store.save(running)
    build = store.directory(running["runId"]) / "builds"
    build.mkdir(parents=True)
    outside = tmp_path / "other-project-checkpoint.json"
    outside.write_text(json.dumps({"plan": {"private": "other model"}}))
    (build / "checkpoint.json").symlink_to(outside)
    assert CadRunStore(store.root).load(running["runId"])["plan"] is None


@pytest.mark.parametrize("matches_plan", [True, False])
def test_recovered_draft_diagnostics_are_public_but_never_delivery_evidence(tmp_path, matches_plan):
    store = CadRunStore(tmp_path / "cad")
    running = running_record()
    running["workerInstance"] = "previous-process-instance"
    store.save(running)
    build = store.directory(running["runId"]) / "builds"
    build.mkdir(parents=True)
    plan_hash = hashlib.sha256(json.dumps(PLAN, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    draft = {"scope": "draft_construction", "planHash": plan_hash if matches_plan else "old-plan-hash",
             "status": "failed", "valid": False, "errors": [{"featureId": "plate", "message": "Invalid profile"}],
             "drawingAgreement": "not_checked", "deliveryArtifactsAvailable": False}
    (build / "checkpoint.json").write_text(json.dumps({"plan": PLAN, "draftInspection": draft}))
    restored = CadRunStore(store.root).load(running["runId"])
    result = _public_result(restored, "/api/v1")
    assert result["draftInspection"] == (draft if matches_plan else None)
    assert restored["state"]["draftInspection"] == result["draftInspection"]
    assert result["status"] == "interrupted" and result["inspection"] is None and result["artifacts"] == []
    assert result["drawingReview"]["humanConfirmed"] is False
