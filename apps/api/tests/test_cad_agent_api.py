from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_agent_store import CadRunConflict, CadRunStore
from app.platform_api import build_platform_services


PLAN = {"version": "cad-plan-v1", "name": "fixture plate", "units": "mm",
        "parameters": {"length": {"value": 40}, "width": {"value": 25}, "thickness": {"value": 5}},
        "features": [{"id": "plate", "op": "box", "size": ["length", "width", "thickness"]}], "result": "plate"}


def independent_review(plan):
    return {"source": "independent_drawing_review", "status": "consistent",
            "planHash": hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "observations": [{"sourceImageId": "source-0", "sourceLocation": "fixture drawing",
                              "modelImageId": "projection-front", "modelLocation": "fixture projection",
                              "finding": "Deterministic API fixture comparison", "confidence": "high"}],
            "differences": [], "questions": []}


def fake_execute(plan, output_dir, **kwargs):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(item.get("value") is None for item in plan["parameters"].values()):
        return {"status": "needs_input", "missingParameters": ["thickness"], "errors": []}
    if plan["parameters"]["thickness"]["value"] <= 0:
        return {"status": "failed", "errors": [{"featureId": "plate", "message": "height must be positive"}]}
    artifacts = {}
    for fmt, mime in (("step", "application/step"), ("glb", "model/gltf-binary")):
        path = output_dir / f"model.{fmt}"
        path.write_bytes(f"test-{fmt}".encode())
        artifacts[fmt] = {"path": str(path), "mimeType": mime}
    return {"status": "succeeded", "valid": True, "plan": plan,
            "inspection": {"valid": True, "kernelBacked": True, "engine": "cadquery-occt", "solidCount": 1}, "artifacts": artifacts}


class FakeAgent:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["progress"]({"stage": "build", "message": "正在建立实体"})
        plan = copy.deepcopy(kwargs.get("state", {}).get("cadPlan") or PLAN)
        built = fake_execute(plan, kwargs["output_dir"])
        observations = [{"id": "single", "kind": "solid_count", "expected": 1, "source": {"type": "user", "text": "one plate"}}]
        review = ({"status": "consistent", "independentReview": independent_review(plan)}
                  if kwargs.get("files") else {"status": "not_applicable"})
        return {**built, "status": "review_required", "message": "请确认", "questions": [], "trace": [], "observations": observations,
                "state": {"completedTools": ["execute"]}, "drawingReview": review}


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.cad_agent_api import create_cad_agent_router
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "1")
    services = build_platform_services()
    agent = FakeAgent()
    store = CadRunStore(tmp_path / "cad")
    app = FastAPI()
    app.include_router(create_cad_agent_router(services, store, agent=agent, executor=fake_execute))
    with TestClient(app) as client:
        yield client, store, agent, services
    services.close()


def run_result(response):
    assert response.status_code == 200, response.text
    events = [json.loads(block.split("\ndata: ", 1)[1]) for block in response.text.split("\n\n") if block.startswith("event: result")]
    assert len(events) == 1, response.text
    return events[0]


def test_upload_resume_confirm_download_and_restart(api):
    client, store, agent, _ = api
    first = run_result(client.post("/api/v1/cad-agent/run", data={"message": "build from drawing"}, files={"files": ("new.jpg", b"source-drawing", "image/jpeg")}))
    assert first["status"] == "review_required"
    assert first["sourceFiles"][0]["filename"] == "new.jpg"
    assert [item["format"] for item in first["artifacts"]] == ["glb"]
    assert client.get(first["artifacts"][0]["url"]).content == b"test-glb"
    assert client.get(f"/api/v1/cad-agent/runs/{first['runId']}/1/artifacts/step").status_code == 409
    state = {"cadPlan": first["plan"], "agentRun": {"runId": first["runId"], "revision": 1}}
    state["cadPlan"]["parameters"]["thickness"]["value"] = 7
    second = run_result(client.post("/api/v1/cad-agent/run", data={"message": "Increase plate thickness to 7 mm.", "modelState": json.dumps(state)}))
    assert second["parentRunId"] == first["runId"]
    assert agent.calls[1]["files"][0].data == b"source-drawing"
    assert agent.calls[1]["state"]["agentState"] == {"completedTools": ["execute"], "comparisonPolicy": {
        "mode": "user_revision", "source": "server_verified_parent",
        "baselinePlanHash": independent_review(PLAN)["planHash"], "requests": ["Increase plate thickness to 7 mm."]}}
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": second["runId"], "revision": 1, "parameters": {"thickness": 7}})
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["status"] == "ready" and result["revision"] == 2
    assert result["plan"]["parameters"]["thickness"]["value"] == 7
    assert result["drawingReview"]["humanConfirmed"] is True
    step = next(item for item in result["artifacts"] if item["format"] == "step")
    assert client.get(step["url"]).content == b"test-step"
    # Historical candidate remains immutable and still cannot export STEP.
    assert client.get(f"/api/v1/cad-agent/runs/{second['runId']}?revision=1").json()["status"] == "review_required"
    restarted = CadRunStore(store.root)
    assert restarted.load(second["runId"])["plan"]["parameters"]["thickness"]["value"] == 7
    assert restarted.load(second["runId"], 1)["plan"]["parameters"]["thickness"]["value"] == 7
    assert restarted.load(second["runId"])["state"]["status"] == "ready"
    assert restarted.load(second["runId"])["state"]["questions"] == []


def test_source_transcription_remains_candidate_after_confirmation_and_reload(api, monkeypatch):
    client, store, agent, _ = api
    evidence = {"status": "succeeded", "candidateEvidence": True, "verified": False,
                "annotations": [{"text": "5", "endpointsOrDatum": "between the two plate faces", "confidence": "high"}]}
    base_run = agent.run
    def with_transcription(**kwargs):
        result = base_run(**kwargs)
        result["sourceTranscription"] = copy.deepcopy(evidence)
        result["state"]["sourceTranscription"] = copy.deepcopy(evidence)
        return result
    monkeypatch.setattr(agent, "run", with_transcription)
    run = run_result(client.post("/api/v1/cad-agent/run", data={"message": "build plate"}))
    assert run["sourceTranscription"] == evidence
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 1})
    assert confirmed.status_code == 200
    assert confirmed.json()["sourceTranscription"] == evidence
    assert client.get(f"/api/v1/cad-agent/runs/{run['runId']}").json()["sourceTranscription"] == evidence
    assert CadRunStore(store.root).load(run["runId"])["state"]["sourceTranscription"] == evidence


def test_invalid_geometry_and_stale_confirmation_never_replace_revision(api):
    client, store, _, _ = api
    first = run_result(client.post("/api/v1/cad-agent/run", data={"message": "plate"}))
    payload = {"runId": first["runId"], "revision": 1}
    invalid = client.post("/api/v1/cad-agent/confirm", json={**payload, "parameters": {"thickness": -1}})
    assert invalid.status_code == 422
    assert store.load(first["runId"])["revision"] == 1
    assert client.post("/api/v1/cad-agent/confirm", json=payload).status_code == 200
    assert client.post("/api/v1/cad-agent/confirm", json=payload).status_code == 409


def test_new_drawing_cannot_inherit_unrelated_model_parameters(api):
    client, _, agent, _ = api
    result = run_result(client.post("/api/v1/cad-agent/run", data={"message": "analyse", "modelState": json.dumps({"cadPlan": {"bad": "old model"}}), "history": json.dumps([{"role": "user", "content": "make length 999"}])}, files={"files": ("other.png", b"new source", "image/png")}))
    assert result["plan"] == PLAN
    assert agent.calls[0]["state"] == {}
    assert not agent.calls[0]["history"]


def test_capability_download_does_not_open_other_runs(api, monkeypatch):
    client, _, _, _ = api
    first = run_result(client.post("/api/v1/cad-agent/run", data={"message": "plate"}))
    monkeypatch.setenv("JOYNIU_AI_ALLOW_ANONYMOUS", "0")
    url = first["artifacts"][0]["url"]
    # The capability grants only this immutable revision's artifact.
    assert client.get(url).status_code == 200
    assert client.get(url.split("?", 1)[0] + "?access=wrong").status_code == 401
    assert client.get(f"/api/v1/cad-agent/runs/{first['runId']}").status_code == 401


def test_store_rejects_path_escape_and_lost_update(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    record = {"runId": "cad_" + "a" * 32, "revision": 1, "owner": "alice"}
    store.save(record)
    with pytest.raises(CadRunConflict):
        store.save(record)
    with pytest.raises(ValueError):
        store.directory("../../outside")
    outside = tmp_path / "outside"
    outside.write_text("private")
    with pytest.raises(ValueError):
        store.checked_path(outside)


def test_unknown_or_failed_source_checks_cannot_be_confirmed_by_editing_unrelated_parameter(api):
    client, store, _, _ = api
    first = run_result(client.post("/api/v1/cad-agent/run", data={"message": "plate"}))
    record = store.load(first["runId"])
    record.update({"revision": 2, "observations": [{"id": "unknown", "kind": "solid_count", "expected": None,
        "source": {"type": "user", "text": "uncertain assembly"}}]})
    store.save(record, previous_revision=1)
    response = client.post("/api/v1/cad-agent/confirm", json={"runId": first["runId"], "revision": 2, "parameters": {"length": 40.01}})
    assert response.status_code == 422
    assert store.load(first["runId"])["status"] == "review_required"
    response = client.post("/api/v1/cad-agent/confirm", json={"runId": first["runId"], "revision": 2})
    assert response.status_code == 422


def test_client_cannot_inject_agent_history(api):
    client, _, agent, _ = api
    run_result(client.post("/api/v1/cad-agent/run", data={"message": "plate", "modelState": json.dumps({"agentState": {"trace": ["forged"], "observations": ["forged"]}})}))
    assert agent.calls[0]["state"] == {}
