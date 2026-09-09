"""Independent drawing evidence gates confirmation and historical downloads."""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_agent_api import create_cad_agent_router
from tests.test_cad_agent_api import api, fake_execute, independent_review, run_result


def drawing_run(client):
    return run_result(client.post("/api/v1/cad-agent/run", data={"message": "build drawing"},
                                  files={"files": ("source.jpg", b"fixture-source", "image/jpeg")}))


def bad_review(record, case):
    review = record["drawingReview"]
    independent = review["independentReview"]
    if case == "missing":
        del review["independentReview"]
    elif case == "self_review":
        independent["source"] = "ai_visual_review"
    elif case == "stale_plan":
        independent["planHash"] = "0" * 64
    elif case in {"mismatch", "uncertain", "failed"}:
        independent["status"] = case
    elif case == "no_observations":
        independent["observations"] = []
    elif case == "legacy_observations":
        independent["observations"] = ["The planner says it matches"]
    elif case == "invalid_observations":
        independent["observations"] = [{}]
    elif case == "uncertain_observations":
        independent["observations"][0]["confidence"] = "low"
    elif case == "differences":
        independent["differences"] = [{"finding": "Missing opening"}]
    elif case == "questions":
        independent["questions"] = ["Which datum applies?"]
    elif case == "error":
        independent["errorCode"] = "timeout"
    elif case in {"missing_differences", "missing_questions"}:
        del independent[case.removeprefix("missing_")]
    elif case == "not_applicable":
        review["status"] = "not_applicable"
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case", ["missing", "self_review", "stale_plan", "mismatch", "uncertain", "failed",
                                   "no_observations", "legacy_observations", "invalid_observations", "uncertain_observations",
                                   "differences", "questions", "error", "missing_differences", "missing_questions", "not_applicable"])
def test_drawing_confirmation_requires_complete_current_independent_evidence(api, case):
    client, store, _, _ = api
    run = drawing_run(client)
    record = store.load(run["runId"])
    bad_review(record, case)
    record["revision"] = 2
    store.save(record, previous_revision=1)
    response = client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 2,
                           "drawingReview": {"status": "consistent", "independentReview": independent_review(run["plan"])}})
    assert response.status_code == 422
    assert "独立图纸复核" in response.text
    assert store.load(run["runId"])["revision"] == 2
    assert not list(store.directory(run["runId"]).glob("confirmed-*"))


@pytest.mark.parametrize("case", ["missing", "stale_plan", "mismatch", "questions", "error", "legacy_observations"])
def test_historical_ready_token_cannot_bypass_new_review_gate(api, case):
    client, store, _, _ = api
    run = drawing_run(client)
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 1})
    assert confirmed.status_code == 200
    record = store.load(run["runId"])
    bad_review(record, case)
    record["revision"] = 3
    store.save(record, previous_revision=2)
    visible = client.get(f"/api/v1/cad-agent/runs/{run['runId']}").json()
    assert visible["deliveryBlockedReason"]
    assert [item["format"] for item in visible["artifacts"]] == ["glb"]
    assert client.get(visible["artifacts"][0]["url"]).status_code == 200
    url = f"/api/v1/cad-agent/runs/{run['runId']}/3/artifacts/step?access={record['downloadToken']}"
    response = client.get(url)
    assert response.status_code == 409 and "独立图纸复核" in response.text


@pytest.mark.parametrize("source_field", ["files", "sourceFiles", "state_sourceFiles"])
def test_drawing_detection_uses_durable_source_metadata(api, source_field):
    client, store, _, _ = api
    run = drawing_run(client)
    record = store.load(run["runId"])
    sources = record.pop("files")
    if source_field == "state_sourceFiles":
        record["state"]["sourceFiles"] = sources
    else:
        record[source_field] = sources
    record["drawingReview"] = {"status": "human_confirmed", "humanConfirmed": True}
    record["revision"] = 2
    store.save(record, previous_revision=1)
    response = client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 2})
    assert response.status_code == 422


def test_confirmed_text_only_task_still_exports_without_independent_drawing_review(api):
    client, _, _, _ = api
    run = run_result(client.post("/api/v1/cad-agent/run", data={"message": "build a plate"}))
    response = client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 1})
    assert response.status_code == 200
    step = next(item for item in response.json()["artifacts"] if item["format"] == "step")
    download = client.get(step["url"])
    assert download.content == b"test-step"
    assert download.headers["cache-control"] == "private, no-store"


def test_confirmation_cannot_apply_a_review_to_a_different_executed_plan(api):
    client, store, agent, services = api
    run = drawing_run(client)

    def changed_execute(plan, output_dir, **kwargs):
        changed = deepcopy(plan)
        changed["parameters"]["thickness"]["value"] += 1
        return fake_execute(changed, output_dir, **kwargs)

    app = FastAPI()
    app.include_router(create_cad_agent_router(services, store, agent=agent, executor=changed_execute))
    with TestClient(app) as changed_client:
        response = changed_client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 1})
    assert response.status_code == 422
    assert store.load(run["runId"])["revision"] == 1


def test_ready_download_requires_actual_current_measurement_evidence(api):
    client, store, _, _ = api
    run = run_result(client.post("/api/v1/cad-agent/run", data={"message": "plate"}))
    assert client.post("/api/v1/cad-agent/confirm", json={"runId": run["runId"], "revision": 1}).status_code == 200
    record = store.load(run["runId"])
    record["observations"] = [{"id": "bore", "kind": "ray_intervals", "expected": [],
                               "source": {"type": "user", "text": "through hole"},
                               "probe": {"origin": [0, 0, 0], "direction": [0, 0, 1], "start": 0, "end": 1}}]
    record["inspection"]["raySections"] = {"bore": {"intervals": []}}
    record["inspection"]["acceptance"] = {"status": "passed"}
    record["revision"] = 3
    store.save(record, previous_revision=2)
    response = client.get(f"/api/v1/cad-agent/runs/{run['runId']}/3/artifacts/step?access={record['downloadToken']}")
    assert response.status_code == 409 and "完整尺寸检查" in response.text
