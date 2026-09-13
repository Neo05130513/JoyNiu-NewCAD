"""Durable request recovery, owner scope, dispatch capacity and real cancel."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.cad_agent_api import create_cad_agent_router
from app.cad_agent_store import CadRunStore
from app.platform_api import build_platform_services
from tests.test_cad_agent_job_lifecycle import BlockingAgent, endpoint, request, event_data
from tests.test_cad_agent_api import fake_execute


def setup(tmp_path):
    services = SimpleNamespace(ai=SimpleNamespace(allow_anonymous=True))
    store = CadRunStore(tmp_path / "cad")
    agent = BlockingAgent()
    return create_cad_agent_router(services, store, agent=agent, executor=fake_execute), store, agent


async def start(router, request_id="req-one", *, message="Build a plate", parent=None, token=None):
    return await endpoint(router, "/cad-agent/run")(request(), message=message,
        modelState=json.dumps({"agentRun": {"runId": parent, "revision": 1}}) if parent else "{}",
        history="[]", files=None, authorization=token, requestId=request_id)


async def get(router, run_id, token=None):
    return await endpoint(router, "/cad-agent/runs/{run_id}")(run_id, request(), revision=None, authorization=token)


async def wait_terminal(router, run_id):
    for _ in range(200):
        result = await get(router, run_id)
        if result["status"] not in {"queued", "running", "cancel_requested"}:
            return result
        await asyncio.sleep(.01)
    raise AssertionError("test task did not terminate")


def test_duplicate_request_returns_same_run_and_can_recover_before_sse_id(tmp_path):
    router, store, agent = setup(tmp_path)
    async def scenario():
        response = await start(router)
        # No SSE frame has been consumed by the client yet.
        recovered = await endpoint(router, "/cad-agent/jobs/by-request/{request_id}")("req-one", request(), authorization=None)
        identity = json.loads(recovered.body)
        duplicate = await start(router)
        assert json.loads(duplicate.body)["runId"] == identity["runId"]
        assert len(list(store.root.glob("cad_*"))) == 1
        with pytest.raises(HTTPException) as conflict:
            await start(router, message="different content")
        assert conflict.value.status_code == 409
        await asyncio.sleep(.01)
        assert len(agent.calls) == 1
        agent.release.set()
        result = await wait_terminal(router, identity["runId"])
        assert result["status"] == "review_required"
        assert json.loads((await start(router)).body)["runId"] == identity["runId"]
        await response.body_iterator.aclose()
    asyncio.run(scenario())


def test_unknown_request_cancellation_is_durable_and_blocks_late_upload_start(tmp_path):
    router, store, agent = setup(tmp_path)
    async def scenario():
        cancelled = await endpoint(router, "/cad-agent/jobs/by-request/{request_id}/cancel")("late-upload", request(), authorization=None)
        assert json.loads(cancelled.body) == {"requestId": "late-upload", "runId": None, "status": "cancelled"}
        # A new router/registry sees the same tombstone after process recovery.
        other = create_cad_agent_router(SimpleNamespace(ai=SimpleNamespace(allow_anonymous=True)), store, agent=agent)
        late = await start(other, "late-upload")
        assert json.loads(late.body)["status"] == "cancelled"
        assert not agent.calls
        found = await endpoint(other, "/cad-agent/jobs/by-request/{request_id}")("late-upload", request(), authorization=None)
        assert json.loads(found.body)["runId"] is None
    asyncio.run(scenario())


def test_registered_request_cancel_waits_for_actual_worker_stop(tmp_path):
    router, _, agent = setup(tmp_path)
    async def scenario():
        response = await start(router, "in-flight")
        run_id = event_data(await anext(response.body_iterator))["runId"]
        await anext(response.body_iterator)
        cancelled = await endpoint(router, "/cad-agent/jobs/by-request/{request_id}/cancel")("in-flight", request(), authorization=None)
        assert json.loads(cancelled.body)["status"] == "cancel_requested"
        assert (await get(router, run_id))["status"] == "cancel_requested"
        agent.release.set()
        assert (await wait_terminal(router, run_id))["status"] == "cancelled"
        await response.body_iterator.aclose()
    asyncio.run(scenario())


def test_claim_and_request_tombstone_race_cannot_leave_future_work_startable(tmp_path):
    from app.cad_job_registry import CadJobRegistry, JobRequestCancelled
    registry = CadJobRegistry(CadRunStore(tmp_path / "race"))
    for index in range(8):
        request_id, run_id = f"race-{index}", f"cad_{index:032x}"
        def claim():
            try:
                return registry.claim(run_id=run_id, owner="owner", request_id=request_id, request_hash="same")
            except JobRequestCancelled:
                return None
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(claim)
            cancellation = pool.submit(registry.cancel_request, "owner", request_id)
            result, cancelled = first.result(), cancellation.result()
        assert registry.try_start(run_id) is False
        if result:
            assert registry.get(run_id)["cancelRequested"] is True
            assert cancelled["status"] == "cancel_requested"
            registry.finish(run_id, "cancelled")
        else:
            assert cancelled["status"] == "cancelled" and cancelled["runId"] is None
        assert registry.by_request("other-owner", request_id) is None


def test_queue_cancel_never_starts_second_agent_and_running_cancel_waits_for_stop(tmp_path):
    router, store, agent = setup(tmp_path)
    async def scenario():
        first = await start(router, "first")
        run1 = event_data(await anext(first.body_iterator))["runId"]
        await anext(first.body_iterator)
        second = await start(router, "second")
        start2 = event_data(await anext(second.body_iterator))
        run2 = start2["runId"]
        assert start2["status"] == "queued"
        await endpoint(router, "/cad-agent/runs/{run_id}/cancel")(run2, request(), authorization=None)
        assert (await wait_terminal(router, run2))["status"] == "cancelled"
        assert len(agent.calls) == 1
        response = await endpoint(router, "/cad-agent/runs/{run_id}/cancel")(run1, request(), authorization=None)
        assert json.loads(response.body)["status"] == "cancel_requested"
        await asyncio.sleep(.03)
        assert (await get(router, run1))["status"] == "cancel_requested"
        agent.release.set()  # The uncancellable test node really stops here.
        result = await wait_terminal(router, run1)
        assert result["status"] == "cancelled" and result["artifacts"] == []
        assert store.load(run1)["status"] == "cancelled"
        with pytest.raises(HTTPException):
            await endpoint(router, "/cad-agent/confirm")(request(), payload={"runId": run1, "revision": 1}, authorization=None)
        await first.body_iterator.aclose()
        await second.body_iterator.aclose()
    asyncio.run(scenario())


def test_capacity_is_bounded_and_queue_starts_when_slot_is_released(tmp_path, monkeypatch):
    monkeypatch.setenv("JOYNIU_CAD_MAX_QUEUED_RUNS", "1")
    router, _, agent = setup(tmp_path)
    async def scenario():
        first = await start(router, "first")
        run1 = event_data(await anext(first.body_iterator))["runId"]
        await anext(first.body_iterator)
        second = await start(router, "second")
        run2 = event_data(await anext(second.body_iterator))["runId"]
        with pytest.raises(HTTPException) as full:
            await start(router, "third")
        assert full.value.status_code == 429
        agent.release.set()
        assert (await wait_terminal(router, run1))["status"] == "review_required"
        assert (await wait_terminal(router, run2))["status"] == "review_required"
        assert len(agent.calls) == 2
        await first.body_iterator.aclose()
        await second.body_iterator.aclose()
    asyncio.run(scenario())


def test_parent_attempts_keep_job_identity_without_promising_free_retries(tmp_path):
    router, _, agent = setup(tmp_path)
    agent.release.set()
    async def scenario():
        first = await start(router, "first")
        frames = [event_data(frame) async for frame in first.body_iterator if frame.startswith("event: result")]
        original = frames[0]
        next_response = await start(router, "next", parent=original["runId"], message="continue")
        next_frames = [event_data(frame) async for frame in next_response.body_iterator if frame.startswith("event: result")]
        revised = next_frames[0]
        assert revised["jobId"] == original["jobId"]
        assert revised["runId"] != original["runId"] and revised["parentRunId"] == original["runId"]
        assert revised["changeKind"] == "continuation"
        listed = json.loads((await endpoint(router, "/cad-agent/jobs")(request(), limit=50, offset=0, authorization=None)).body)
        assert listed["total"] == 2 and {item["jobId"] for item in listed["items"]} == {original["jobId"]}
        assert all("downloadToken" not in item for item in listed["items"])
    asyncio.run(scenario())


def test_provider_configuration_failure_releases_capacity_and_is_not_endless_202(tmp_path):
    from app.cad_job_registry import CadJobRegistry
    store = CadRunStore(tmp_path / "cad")
    services = SimpleNamespace(ai=SimpleNamespace(allow_anonymous=True))
    def unavailable():
        raise RuntimeError("PRIVATE configuration")
    router = create_cad_agent_router(services, store, service_factory=unavailable)
    async def scenario():
        with pytest.raises(HTTPException) as failed:
            await start(router)
        assert failed.value.status_code == 503 and "PRIVATE" not in str(failed.value.detail)
        recovered = await endpoint(router, "/cad-agent/jobs/by-request/{request_id}")("req-one", request(), authorization=None)
        assert recovered.status_code == 200 and json.loads(recovered.body)["status"] == "failed"
        repeated = await start(router)
        assert repeated.status_code == 200 and json.loads(repeated.body)["status"] == "failed"
    asyncio.run(scenario())
    identity, created = CadJobRegistry(store).claim(run_id="cad_" + "d" * 32, owner="local-anonymous", request_id="fresh", request_hash="hash")
    assert created and identity["status"] == "running"


def test_job_list_request_lookup_and_cancel_are_owner_scoped(tmp_path):
    services = build_platform_services(tmp_path / "accounts.db", auth_secret="test-jobs-secret-32-bytes-long-value")
    first = services.auth.create_user("first@example.test", "test-password", "First", roles=["designer"])
    second = services.auth.create_user("second@example.test", "test-password", "Second", roles=["designer"])
    token1, token2 = ("Bearer " + services.auth.issue_token(user).token for user in (first, second))
    store = CadRunStore(tmp_path / "cad")
    agent = BlockingAgent()
    router = create_cad_agent_router(services, store, agent=agent, executor=fake_execute)
    async def scenario():
        response = await start(router, token=token1)
        run_id = event_data(await anext(response.body_iterator))["runId"]
        await anext(response.body_iterator)
        listed = await endpoint(router, "/cad-agent/jobs")(request(), limit=50, offset=0, authorization=token2)
        assert json.loads(listed.body)["items"] == []
        with pytest.raises(HTTPException) as absent:
            await endpoint(router, "/cad-agent/jobs/by-request/{request_id}")("req-one", request(), authorization=token2)
        assert absent.value.status_code == 404
        with pytest.raises(HTTPException) as forbidden:
            await endpoint(router, "/cad-agent/runs/{run_id}/cancel")(run_id, request(), authorization=token2)
        assert forbidden.value.status_code == 403
        agent.release.set()
        async for _ in response.body_iterator:
            pass
    try:
        asyncio.run(scenario())
    finally:
        agent.release.set()
        services.close()
