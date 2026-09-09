"""Question rereading provides source candidates, never implicit confirmation."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.cad_agent import CadAgentService, _digest, _source_manifest
from app.cad_executor import execute_cad_plan
from app.cad_source_questions import questions_identity
from tests.test_cad_agent import (
    Executor, Provider, StubSourceReader, StubSpatialInterpreter,
    context, execute, finish, observation, plan, raster, record, run,
)


def ask(questions=("Which dimension gives the overall height?",), *, draft=False):
    value = {"action": "ask_user", "message": "需要核对具体尺寸依据。", "questions": list(questions)}
    if draft:
        value["plan"] = plan(None)
    return value


class Resolver:
    """A fixture for source pixels only; no planning-provider action is consumed."""
    def __init__(self, statuses=None, mutation=None):
        self.calls = []
        self.statuses = statuses
        self.mutation = mutation

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        identity = questions_identity(**{key: kwargs[key] for key in
            ("questions", "source_files", "detail_files", "detail_metadata", "source_manifest")})
        answers = []
        for index, _ in enumerate(kwargs["questions"]):
            status = self.statuses[index] if self.statuses else "answered"
            answers.append({"questionIndex": index, "questionId": f"question-{index}", "status": status,
                            "answer": "The source marks the overall height as 30 mm." if status == "answered" else None,
                            "source": {"imageId": "source-0", "location": "overall dimension at the right of the source view",
                                       "evidence": "The dimension is bounded by the upper and lower outside faces."}
                                      if status == "answered" else None,
                            "confidence": "high" if status == "answered" else "uncertain"})
        result = {**identity, "status": "succeeded", "scope": "source_question_reread",
                  "candidateEvidence": True, "verified": False, "answers": answers,
                  "unresolvedQuestions": [kwargs["questions"][a["questionIndex"]] for a in answers if a["status"] == "unresolved"],
                  "providerMetrics": {"requestCount": 1}}
        if self.mutation:
            result = self.mutation(result)
        return result


def uncertain_comparison(**kwargs):
    return {"status": "uncertain", "scope": "original_drawing", "views": []}


def test_partial_source_answers_require_explicit_ledger_real_execution_and_review(tmp_path):
    resolver = Resolver(["answered", "unresolved"])
    questions = ["Which dimension gives the overall height?", "Is another source view available?"]
    timeline, executions, reviews, checkpoints = [], [], [], []

    def from_candidate(body):
        current = context(body)
        feedback = current["toolFeedback"]
        assert feedback["stage"] == "source_question_review" and feedback["status"] == "candidate_answers"
        assert feedback["answers"][0]["status"] == "answered"
        assert feedback["answers"][1]["status"] == "unresolved"
        assert current["currentPlan"]["parameters"]["height"]["value"] is None
        assert current["sourceObservations"] == []
        assert not current["currentExecutionSummary"]["hasFreshValidGeometry"]
        assert "not user statements or confirmation" in feedback["next"]
        assert "must not override explicit user design revisions" in feedback["next"]
        assert executions == reviews == []
        saved = json.loads((tmp_path / "checkpoint.json").read_text())
        assert saved["sourceQuestionReviews"][0]["answers"] == feedback["answers"]
        assert saved["observations"] == [] and saved["plan"]["parameters"]["height"]["value"] is None
        checkpoints.append(saved)
        timeline.append("planner_checked_candidate")
        return record()

    def actual_executor(value, output_dir, **kwargs):
        executions.append(deepcopy(value))
        timeline.append("occt_execute")
        return execute_cad_plan(value, output_dir, **kwargs)

    def independent_reviewer(**kwargs):
        reviews.append(kwargs)
        timeline.append("independent_review")
        assert kwargs["inspection"]["kernelBacked"] is True
        assert kwargs["inspection"]["bbox"]["size"] == pytest.approx([10, 20, 30])
        assert len(kwargs["projection_files"]) == 3
        assert kwargs["inspection"]["acceptance"]["status"] == "passed"
        return {"status": "consistent", "observations": ["Synthetic block fixture review."], "differences": [], "questions": []}

    def final_step(body):
        assert len(executions) == len(reviews) == 1
        assert context(body)["currentExecutionSummary"]["hasFreshValidGeometry"]
        timeline.append("planner_finish")
        return finish()

    provider = Provider([ask(questions, draft=True), from_candidate, execute(), final_step])
    result = run(provider, actual_executor, tmp_path, files=[raster()], question_resolver=resolver,
                 drawing_reviewer=independent_reviewer, projection_comparer=uncertain_comparison)
    assert result["status"] == "review_required"
    assert len(resolver.calls) == 1 and len(checkpoints) == 1
    assert timeline == ["planner_checked_candidate", "occt_execute", "independent_review", "planner_finish"]
    assert Path(result["artifacts"]["step"]["path"]).is_file()
    assert result["inspection"]["acceptance"]["status"] == "passed"
    assert result["drawingReview"]["humanConfirmed"] is False
    assert result["state"]["sourceQuestionReviews"] == result["sourceQuestionReviews"]
    persisted = json.loads((tmp_path / "agent-state.json").read_text())
    assert persisted["state"]["sourceQuestionReviews"] == result["sourceQuestionReviews"]
    assert result["sourceQuestionReviews"][0]["verified"] is False


def test_answered_candidate_alone_cannot_finish_without_executed_geometry(tmp_path):
    resolver, executor = Resolver(), Executor()
    provider = Provider([ask(draft=True), finish(), ask()])
    result = run(provider, executor, tmp_path, files=[raster()], question_resolver=resolver)
    assert result["status"] == "needs_input"
    assert len(resolver.calls) == 1 and executor.calls == []
    assert result["artifacts"] == {} and result["inspection"] is None
    assert result["drawingReview"]["humanConfirmed"] is False
    assert context(provider.requests[2][0])["toolFeedback"]["errors"]


def test_source_candidate_cannot_replace_explicit_user_revision_or_existing_ledger(tmp_path):
    resolver, executor = Resolver(), Executor()
    revised_plan, revised_observation = plan(21), observation(21)
    request = "Set the height to 21 mm as my explicit revision, even if the original shows another value."
    revised_plan["parameters"]["height"]["source"]["text"] = request
    revised_observation["source"]["text"] = request
    policy = {"mode": "user_revision", "source": "server_verified_parent", "baselinePlanHash": _digest(plan(30)), "requests": [request]}
    state = {"cadPlan": revised_plan, "observations": [revised_observation], "comparisonPolicy": policy}
    files = [raster()]
    source_manifest = _source_manifest(tuple(files))
    reading = StubSourceReader().read(files=files, source_files=source_manifest)
    state["sourceTranscription"] = reading
    state["sourceSpatialContract"] = StubSpatialInterpreter().interpret(
        files=files, source_files=source_manifest, transcription=reading)
    before = deepcopy(state)
    def check_candidate(body):
        current = context(body)
        assert "30 mm" in current["toolFeedback"]["answers"][0]["answer"]
        assert current["currentPlan"] == revised_plan
        assert current["sourceObservations"] == [revised_observation]
        assert current["comparisonPolicy"] == policy
        assert "must not override explicit user design revisions" in current["toolFeedback"]["next"]
        return ask()
    provider = Provider([ask(), check_candidate])
    result = CadAgentService(provider_call=provider, executor=executor, source_reader=StubSourceReader(),
                             spatial_interpreter=StubSpatialInterpreter(), question_resolver=resolver).run(
        message=request, files=files, state=state, output_dir=tmp_path)
    assert result["status"] == "needs_input" and len(resolver.calls) == 1
    assert result["plan"] == revised_plan and result["observations"] == [revised_observation]
    assert result["comparisonPolicy"] == policy and state == before
    assert executor.calls == [] and result["drawingReview"]["humanConfirmed"] is False


def test_identical_question_group_is_not_reread_forever(tmp_path):
    resolver = Resolver()
    provider = Provider([ask(), ask()])
    result = run(provider, Executor(), tmp_path, files=[raster()], question_resolver=resolver, max_turns=12)
    assert result["status"] == "needs_input" and result["questions"] == ask()["questions"]
    assert len(provider.requests) == 2 and len(resolver.calls) == 1
    assert len(result["sourceQuestionReviews"]) == 1


def test_at_most_two_distinct_question_groups_are_reread_per_run(tmp_path):
    resolver = Resolver()
    groups = [["First source issue?"], ["Second source issue?"], ["Third source issue?"]]
    result = run(Provider([ask(group) for group in groups]), Executor(), tmp_path, files=[raster()],
                 question_resolver=resolver, max_turns=12)
    assert result["status"] == "needs_input" and result["questions"] == groups[-1]
    assert len(resolver.calls) == 2
    assert [review["questions"] for review in result["sourceQuestionReviews"]] == groups[:2]


def test_changed_crop_can_be_checked_once_without_reusing_old_source_mapping(tmp_path):
    resolver = Resolver()
    crop = {"action": "inspect_source", "message": "Inspect a different source area.",
            "source": {"fileIndex": 0, "crop": [.1, .1, .5, .5], "rotation": 90, "view": "source detail"}}
    result = run(Provider([ask(), crop, ask(), ask()]), Executor(), tmp_path, files=[raster()], question_resolver=resolver)
    assert result["status"] == "needs_input" and len(resolver.calls) == 2
    first, second = result["sourceQuestionReviews"]
    assert first["questionFingerprint"] == second["questionFingerprint"]
    assert first["inputFingerprint"] != second["inputFingerprint"]
    assert resolver.calls[0]["detail_files"] == ()
    assert len(resolver.calls[1]["detail_files"]) == len(resolver.calls[1]["detail_metadata"]) == 1
    assert second["images"][-1]["sourceMappings"][0]["rotation"] == 90


@pytest.mark.parametrize("failure", ["failed", "timeout", "exception", "null", "unsourced", "null_answer", "unknown_source", "invalid_index", "all_unresolved"])
def test_failed_invalid_or_unanswered_rereads_preserve_original_questions(tmp_path, failure):
    questions = ["Which source dimension applies?", "Where is its reference face?"]
    resolver = Resolver()
    def failed(**kwargs):
        value = resolver(**kwargs)
        if failure == "failed": value.update(status="failed", errorCode="invalid_source_questions")
        elif failure == "timeout": value.update(status="failed", errorCode="timeout")
        elif failure == "exception": raise TimeoutError("Provider unavailable")
        elif failure == "null": return None
        elif failure == "unsourced": value["answers"][0]["source"] = None
        elif failure == "null_answer": value["answers"][0]["answer"] = None
        elif failure == "unknown_source": value["answers"][0]["source"]["imageId"] = "old-source"
        elif failure == "invalid_index": value["answers"][0]["questionIndex"] = 99
        elif failure == "all_unresolved":
            for item in value["answers"]:
                item.update(status="unresolved", answer=None, source=None, confidence="uncertain")
            value["unresolvedQuestions"] = questions
        return value
    executor = Executor()
    provider = Provider([ask(questions)])
    result = run(provider, executor, tmp_path, files=[raster()], question_resolver=failed)
    assert result["status"] == "needs_input" and result["questions"] == questions
    assert len(resolver.calls) == len(provider.requests) == 1
    assert executor.calls == [] and result["inspection"] is None
    assert len(result["sourceQuestionReviews"]) == 1
    assert json.loads((tmp_path / "checkpoint.json").read_text())["sourceQuestionReviews"] == result["sourceQuestionReviews"]


@pytest.mark.parametrize("reason", ["no_image", "last_turn", "insufficient_time"])
def test_no_unusable_question_request_when_no_image_or_continuation_budget(tmp_path, reason):
    resolver = Resolver()
    options = {"files": [raster()]}
    if reason == "no_image": options["files"] = []
    elif reason == "last_turn": options["max_turns"] = 1
    elif reason == "insufficient_time": options["timeout_seconds"] = 25
    provider = Provider([ask()])
    result = run(provider, Executor(), tmp_path, question_resolver=resolver, **options)
    assert result["status"] == "needs_input" and result["questions"] == ask()["questions"]
    assert resolver.calls == [] and len(provider.requests) == 1
    assert result["sourceQuestionReviews"] == []


def saved_candidate(tmp_path):
    resolver = Resolver()
    result = run(Provider([ask(), ask()]), Executor(), tmp_path / "saved", files=[raster()], question_resolver=resolver)
    assert result["status"] == "needs_input" and len(result["sourceQuestionReviews"]) == 1
    return result["state"]


def test_matching_saved_candidate_can_resume_without_calling_reader_again(tmp_path):
    saved = saved_candidate(tmp_path)
    before = deepcopy(saved)
    resolver = Resolver()
    provider = Provider([ask(), ask()])
    result = run(provider, Executor(), tmp_path / "resumed", state=saved, files=[raster()], question_resolver=resolver)
    assert result["status"] == "needs_input" and resolver.calls == []
    assert saved == before
    assert result["sourceQuestionReviews"] == saved["sourceQuestionReviews"]
    event = [item for item in result["trace"] if item["action"] == "source_question_review"][-1]
    assert event["reused"] is True


def test_changed_question_does_not_reuse_previous_candidate(tmp_path):
    saved = saved_candidate(tmp_path)
    resolver = Resolver()
    question = ["Which other source feature is shown?"]
    result = run(Provider([ask(question), ask(question)]), Executor(), tmp_path / "new-question", state=saved,
                 files=[raster()], question_resolver=resolver)
    assert result["status"] == "needs_input" and len(resolver.calls) == 1
    assert result["sourceQuestionReviews"][-1]["questions"] == question
    assert result["sourceQuestionReviews"][-1]["questionFingerprint"] != saved["sourceQuestionReviews"][0]["questionFingerprint"]


def test_changed_source_clears_old_question_candidates_before_next_read(tmp_path):
    saved = saved_candidate(tmp_path)
    old_fingerprint = saved["sourceQuestionReviews"][0]["inputFingerprint"]
    resolver = Resolver()
    def first_step(body):
        checkpoint = json.loads((tmp_path / "new-source" / "checkpoint.json").read_text())
        assert checkpoint["sourceQuestionReviews"] == []
        assert context(body)["sourceObservations"] == []
        return ask()
    result = run(Provider([first_step, ask()]), Executor(), tmp_path / "new-source", state=saved,
                 files=[raster(203)], question_resolver=resolver)
    assert result["status"] == "needs_input" and len(resolver.calls) == 1
    assert len(result["sourceQuestionReviews"]) == 1
    assert result["sourceQuestionReviews"][0]["inputFingerprint"] != old_fingerprint
    assert saved["sourceQuestionReviews"][0]["inputFingerprint"] == old_fingerprint
