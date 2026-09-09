"""A durable job reports its selected engine, never a later default or secret."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

from app.cad_agent_api import _public_result, create_cad_agent_router
from app.cad_agent_store import CadRunStore
from tests.test_cad_agent_api import fake_execute
from tests.test_cad_agent_job_lifecycle import BlockingAgent, event_data, public, running_record, start, terminal


CODEX = {"mode": "codex", "name": "codex-cli", "model": "gpt-6-astra", "reasoningEffort": "high",
         "configured": True, "streaming": False, "authentication": "cli-managed"}
REMOTE = {"mode": "remote", "model": "gpt-5.6-sol", "reasoningEffort": "medium"}


class BoundAgent(BlockingAgent):
    def __init__(self, metadata, *, fail=False):
        super().__init__(fail=fail)
        self.provider_call = SimpleNamespace(provider_info=copy.deepcopy(metadata))
        self.selected_model = metadata["model"]

    def run(self, **kwargs):
        result = super().run(**kwargs)
        # Even an injected runner must not replace its durable selected engine
        # with a later default or accidentally publish transport configuration.
        reported = {**REMOTE, "baseUrl": "https://PRIVATE.example/v1", "binary": "/PRIVATE/codex",
                    "apiKey": "sk-PRIVATE", "attempts": 3, "retryCount": 1,
                    "lastErrorCode": "timeout", "rawPayload": {"secret": "PRIVATE"}}
        result["provider"] = reported
        result["state"]["provider"] = reported
        return result


def services():
    return SimpleNamespace(ai=SimpleNamespace(allow_anonymous=True))


def test_enqueue_binds_one_service_and_engine_for_started_progress_get_and_final(tmp_path):
    selected = {"metadata": {**CODEX, "baseUrl": "https://PRIVATE.example", "apiKey": "sk-PRIVATE"}}
    made = []

    def factory():
        instance = BoundAgent(selected["metadata"])
        made.append(instance)
        return instance

    store = CadRunStore(tmp_path / "cad")
    router = create_cad_agent_router(services(), store, executor=fake_execute, service_factory=factory)
    assert made == []  # Configuration is selected when queued, not at API startup.

    async def scenario():
        first_response = await start(router)
        assert len(made) == 1 and made[0].calls == []
        # Change the default before the background task can start. The first
        # job must retain its already-created instance and safe metadata.
        selected["metadata"] = dict(REMOTE)
        second_response = await start(router)
        assert len(made) == 2 and made[1].selected_model == REMOTE["model"]
        first, second = first_response.body_iterator, second_response.body_iterator
        try:
            first_started = event_data(await anext(first))
            second_started = event_data(await anext(second))
            assert first_started["provider"] == CODEX
            assert second_started["provider"] == REMOTE
            first_id, second_id = first_started["runId"], second_started["runId"]
            initial = store.load(first_id)
            assert initial["provider"] == initial["progress"]["provider"] == initial["state"]["provider"] == CODEX
            assert "PRIVATE" not in json.dumps(initial)
            assert event_data(await anext(first))["provider"] == CODEX
            first_get = await public(router, first_id)
            assert first_get["provider"] == first_get["progress"]["provider"] == CODEX
            assert first_get["status"] == "running"
            await first.aclose()  # Disconnect does not lose the selected engine.
            await second.aclose()
            for instance in made:
                instance.release.set()
            final = await terminal(store, first_id)
            await terminal(store, second_id)
            expected = {**CODEX, "attempts": 3, "retryCount": 1, "lastErrorCode": "timeout"}
            assert final["provider"] == final["progress"]["provider"] == final["state"]["provider"] == expected
            assert "PRIVATE" not in json.dumps(final)
            assert (await public(router, first_id))["provider"] == expected
            assert CadRunStore(store.root).load(first_id)["provider"] == expected
            assert [len(instance.calls) for instance in made] == [1, 1]
            assert made[0].selected_model == CODEX["model"]
        finally:
            for instance in made:
                instance.release.set()
    asyncio.run(scenario())


def test_progress_cannot_override_selected_provider_or_publish_transport_fields(tmp_path):
    agent = BoundAgent(CODEX)
    original_run = agent.run

    def reporting(**kwargs):
        kwargs["progress"]({"stage": "planning", "message": "Planning",
                            "provider": {**REMOTE, "apiKey": "sk-PRIVATE", "baseUrl": "https://PRIVATE.example"}})
        return original_run(**kwargs)

    agent.run = reporting
    store = CadRunStore(tmp_path / "cad")
    router = create_cad_agent_router(services(), store, agent=agent, executor=fake_execute)

    async def scenario():
        response = await start(router)
        stream = response.body_iterator
        try:
            run_id = event_data(await anext(stream))["runId"]
            frame = event_data(await anext(stream))
            assert frame["stage"] == "planning" and frame["provider"] == CODEX
            assert "PRIVATE" not in json.dumps(frame)
            agent.release.set()
            await stream.aclose()
            await terminal(store, run_id)
        finally:
            agent.release.set()
    asyncio.run(scenario())


def test_injected_legacy_agent_still_wins_over_factory(tmp_path):
    def forbidden_factory():
        raise AssertionError("Explicit agent must take precedence")

    agent = BlockingAgent()
    agent.release.set()
    store = CadRunStore(tmp_path / "cad")
    router = create_cad_agent_router(services(), store, agent=agent, executor=fake_execute,
                                    service_factory=forbidden_factory)

    async def scenario():
        response = await start(router)
        frames = [event_data(frame) async for frame in response.body_iterator]
        assert frames[0]["provider"] == {"mode": "unknown"}
        assert frames[-1]["status"] == "review_required"
        assert frames[-1]["provider"] == {"mode": "unknown"}
    asyncio.run(scenario())
    assert len(agent.calls) == 1


def test_exception_and_restart_keep_bound_identity_without_claiming_a_final_model(tmp_path):
    agent = BoundAgent(CODEX, fail=True)
    agent.release.set()
    store = CadRunStore(tmp_path / "cad")
    router = create_cad_agent_router(services(), store, agent=agent, executor=fake_execute)

    async def scenario():
        response = await start(router)
        frames = [event_data(frame) async for frame in response.body_iterator]
        final = frames[-1]
        assert final["status"] == "failed" and final["artifacts"] == []
        assert final["provider"] == {**CODEX, "lastErrorCode": "task_exception"}
        assert final["progress"]["provider"] == final["provider"]
        assert "PRIVATE" not in json.dumps(final)
        return final["runId"]
    run_id = asyncio.run(scenario())
    assert CadRunStore(store.root).load(run_id)["provider"]["model"] == CODEX["model"]

    abandoned = running_record("c")
    abandoned["workerInstance"] = "previous-worker"
    abandoned["provider"] = dict(CODEX)
    store.save(abandoned)
    restarted = CadRunStore(store.root)
    record = restarted.load(abandoned["runId"])
    result = _public_result(record, "/api/v1")
    assert result["status"] == "interrupted" and result["artifacts"] == []
    assert result["provider"] == {**CODEX, "lastErrorCode": "worker_interrupted"}
    assert result["progress"]["provider"] == result["provider"]


def test_public_legacy_metadata_is_allowlisted_without_reading_current_configuration():
    record = running_record()
    record["provider"] = {**CODEX, "model": "sk-PRIVATE", "name": "/PRIVATE/bin",
                          "lastErrorCode": "PRIVATE secret", "baseUrl": "https://PRIVATE.example",
                          "attempts": True, "retryCount": -1, "sourceReaderAttempts": 1_000_001,
                          "sourceSpatialAttempts": 2, "reasoning": {"content": "PRIVATE"}}
    record["progress"]["provider"] = {"baseUrl": "https://PRIVATE.example"}
    result = _public_result(record, "/api/v1")
    assert result["provider"] == {key: value for key, value in {**CODEX, "sourceSpatialAttempts": 2}.items()
                                   if key not in {"model", "name"}}
    assert result["progress"]["provider"] == result["provider"]
    assert "PRIVATE" not in json.dumps(result)
