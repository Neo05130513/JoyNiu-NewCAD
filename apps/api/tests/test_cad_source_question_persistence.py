"""Source rereading is durable candidate evidence, never delivery permission."""
from __future__ import annotations

import asyncio
import copy
import json

from app.cad_agent_api import _public_result
from app.cad_agent_store import CadRunStore, source_question_reviews
from tests.test_cad_agent_api import api, run_result
from tests.test_cad_agent_job_lifecycle import setup, running_record, endpoint, request, event_data, public, terminal


def review(index=0):
    return {"version": "source-question-review-v1", "scope": "source_question_reread", "status": "succeeded",
            "candidateEvidence": True, "verified": False, "inputFingerprint": str(index)*64,
            "questions": ["Which two faces define the thickness?"], "answers": [{"questionIndex": 0,
                "questionId": "question-0", "status": "answered", "answer": "The top and bottom faces.",
                "source": {"imageId": "source-0", "location": "left orthographic view", "evidence": "dimension extension lines touch both faces"},
                "confidence": "high"}], "images": [{"imageId": "source-0", "kind": "source"}], "unresolvedQuestions": []}


def test_source_question_candidate_survives_api_confirmation_and_reload_without_becoming_verified(api, monkeypatch):
    client, store, agent, _ = api
    evidence = [review()]
    original = agent.run
    def with_review(**kwargs):
        result = original(**kwargs)
        result["sourceQuestionReviews"] = copy.deepcopy(evidence)
        result["state"]["sourceQuestionReviews"] = copy.deepcopy(evidence)
        return result
    monkeypatch.setattr(agent, "run", with_review)
    current = run_result(client.post("/api/v1/cad-agent/run", data={"message": "Build the plate"}))
    assert current["sourceQuestionReviews"] == evidence
    assert current["status"] == "review_required"
    assert client.get(f"/api/v1/cad-agent/runs/{current['runId']}/1/artifacts/step").status_code == 409
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": current["runId"], "revision": 1})
    assert confirmed.status_code == 200
    assert confirmed.json()["sourceQuestionReviews"] == evidence
    assert confirmed.json()["sourceQuestionReviews"][0]["verified"] is False
    assert client.get(f"/api/v1/cad-agent/runs/{current['runId']}").json()["sourceQuestionReviews"] == evidence
    assert CadRunStore(store.root).load(current["runId"])["state"]["sourceQuestionReviews"] == evidence


def test_resumed_running_record_and_get_preserve_source_question_candidates(tmp_path):
    router, store, agent, _ = setup(tmp_path)
    parent = running_record("c")
    parent.update(status="failed", sourceQuestionReviews=[review()], state={"sourceQuestionReviews": [review()]})
    store.save(parent)
    async def scenario():
        response = await endpoint(router, "/cad-agent/run")(
            request(), message="Retry the unfinished work", modelState=json.dumps({"agentRun": {"runId": parent["runId"], "revision": 1}}),
            history="[]", files=None, authorization=None)
        stream = response.body_iterator
        try:
            started = event_data(await anext(stream))
            saved = store.load(started["runId"])
            assert saved["sourceQuestionReviews"] == saved["state"]["sourceQuestionReviews"] == [review()]
            visible = await public(router, started["runId"])
            assert visible["status"] == "running" and visible["sourceQuestionReviews"] == [review()]
            assert visible["inspection"] is None and visible["artifacts"] == []
        finally:
            await stream.aclose()
            agent.release.set()
        await terminal(store, started["runId"])
    asyncio.run(scenario())


def test_checkpoint_restores_bounded_candidates_but_drops_geometry_and_passed_claims(tmp_path):
    store = CadRunStore(tmp_path / "cad")
    running = running_record()
    running["workerInstance"] = "previous-process-instance"
    store.save(running)
    directory = store.directory(running["runId"]) / "builds"
    directory.mkdir(parents=True)
    candidates = [review(0), review(1), {**review(2), "passed": True, "productionReady": True}]
    checkpoint = {"sourceQuestionReviews": candidates, "inspection": {"valid": True}, "status": "ready",
                  "artifacts": {"step": {"path": "not-trusted"}}, "drawingReview": {"status": "consistent", "humanConfirmed": True}}
    (directory / "checkpoint.json").write_text(json.dumps(checkpoint))
    recovered = CadRunStore(store.root).load(running["runId"])
    assert recovered["status"] == "interrupted"
    assert recovered["sourceQuestionReviews"] == recovered["state"]["sourceQuestionReviews"] == [review(1), review(2)]
    assert recovered["inspection"] is None and recovered["artifacts"] == {}
    assert recovered["drawingReview"]["status"] == "unverified"
    assert recovered["drawingReview"]["humanConfirmed"] is False
    visible = _public_result(recovered, "/api/v1")
    assert visible["sourceQuestionReviews"] == [review(1), review(2)] and visible["artifacts"] == []


def test_legacy_or_invalid_candidate_claims_never_create_verification():
    assert source_question_reviews(None) == []
    assert source_question_reviews([{**review(), "verified": True}]) == []
    assert source_question_reviews([{**review(), "status": "passed"}]) == []
    assert source_question_reviews([{**review(), "candidateEvidence": False}]) == []
    legacy = running_record()
    assert _public_result(legacy, "/api/v1")["sourceQuestionReviews"] == []
