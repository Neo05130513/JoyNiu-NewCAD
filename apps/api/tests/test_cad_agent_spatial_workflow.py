"""Auxiliary source reasoning never blocks planning or overwrites final state."""
import copy
import json
import threading

from tests.test_cad_agent import (
    Executor, Provider, StubSourceReader, StubSpatialInterpreter, context,
    execute, finish, plan, raster, record, run,
)


class ObservedSpatial:
    """Join only fast fixture work inside a provider callback, avoiding races."""
    def __init__(self, implementation=None):
        self.implementation = implementation or StubSpatialInterpreter().interpret
        self.calls = []
        self.completed = 0
        self.condition = threading.Condition()
        self.thread = None

    def interpret(self, **kwargs):
        with self.condition:
            self.thread = threading.current_thread()
            self.calls.append(kwargs)
        try:
            return self.implementation(**kwargs)
        finally:
            with self.condition:
                self.completed += 1
                self.condition.notify_all()

    def settle(self, count=1):
        with self.condition:
            assert self.condition.wait_for(lambda: self.completed >= count, timeout=2)
            worker = self.thread
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_spatial_inputs_are_independent_and_finished_candidate_reaches_next_operation(tmp_path):
    reader = StubSourceReader()
    original_interpret = StubSpatialInterpreter().interpret

    def interpret(**kwargs):
        assert set(kwargs) == {"files", "source_files", "transcription", "output_dir", "timeout_seconds", "progress"}
        assert kwargs["progress"] is None
        assert "987654" not in json.dumps(kwargs["transcription"])
        checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
        assert checkpoint["sourceTranscription"]["status"] == "succeeded"
        assert checkpoint["observations"] == []
        return original_interpret(**kwargs)
    interpreter = ObservedSpatial(interpret)

    def observe(body):
        assert context(body)["currentPlan"]["parameters"]["height"]["value"] == 987654
        assert any(item["type"] == "input_image" for item in body["input"][0]["content"])
        interpreter.settle()
        return record()

    def build(body):
        spatial = context(body)["sourceSpatialContract"]
        assert spatial["candidateEvidence"] is True and spatial["verified"] is False
        assert spatial["contract"]["features"]
        assert "provider" not in spatial
        return execute()

    result = run(Provider([observe, build, finish()]), Executor(), tmp_path,
                 files=[raster()], source_reader=reader, spatial_interpreter=interpreter,
                 state={"cadPlan": plan(987654), "observations": record()["observations"]})
    assert result["status"] == "review_required"
    assert len(interpreter.calls) == 1
    assert result["state"]["sourceSpatialContract"] == result["sourceSpatialContract"]


def test_identical_sources_reuse_contract_and_replaced_sources_reinterpret(tmp_path):
    reader, interpreter = StubSourceReader(), ObservedSpatial()
    ask = {"action": "ask_user", "message": "需要局部尺寸", "questions": ["槽深是多少？"]}
    def settle_then_ask(count):
        def reply(body):
            interpreter.settle(count)
            return ask
        return reply
    files = [raster()]
    first = run(Provider([settle_then_ask(1)]), Executor(), tmp_path / "first", files=files,
                source_reader=reader, spatial_interpreter=interpreter)
    assert len(interpreter.calls) == 1
    again = run(Provider([ask]), Executor(), tmp_path / "again", files=files, state=first["state"],
                source_reader=reader, spatial_interpreter=interpreter)
    assert len(interpreter.calls) == 1
    assert again["sourceSpatialContract"] == first["sourceSpatialContract"]
    replaced = run(Provider([settle_then_ask(2)]), Executor(), tmp_path / "replaced", files=[raster(201)], state=first["state"],
                   source_reader=reader, spatial_interpreter=interpreter)
    assert len(interpreter.calls) == 2
    assert replaced["sourceSpatialContract"]["sourceFingerprint"] != first["sourceSpatialContract"]["sourceFingerprint"]


def test_failed_auxiliary_pass_keeps_pixels_available_for_explicit_planning_and_comparison(tmp_path):
    interpreter = ObservedSpatial(lambda **_: {"status": "failed", "errorCode": "timeout", "contract": None})
    compared = []
    def observe(body):
        interpreter.settle()
        assert context(body)["sourceTranscription"]["status"] == "succeeded"
        assert any(item["type"] == "input_image" for item in body["input"][0]["content"])
        return record()
    def build(body):
        spatial = context(body)["sourceSpatialContract"]
        assert spatial["status"] == "failed" and spatial["contract"] is None
        return execute()
    def compare(**kwargs):
        compared.append(kwargs)
        assert kwargs["source_files"] and kwargs["projection_files"]
        assert kwargs["source_views"] == []
        return {"status": "uncertain", "scope": "original_drawing", "views": []}
    executor = Executor()
    result = run(Provider([observe, build, finish()]), executor, tmp_path, files=[raster()],
                 spatial_interpreter=interpreter, projection_comparer=compare)
    assert result["status"] == "review_required"
    assert result["sourceSpatialContract"]["errorCode"] == "timeout"
    assert result["plan"]["features"] == plan()["features"]
    assert len(executor.calls) == 1 and len(compared) == 1
    assert result["projectionComparison"].get("errorCode") is None


def test_wrong_source_candidate_is_rejected_but_original_source_planning_continues(tmp_path):
    base = StubSpatialInterpreter()
    def wrong(**kwargs):
        candidate = base.interpret(**kwargs)
        candidate["sourceFingerprint"] = "not-the-current-image"
        candidate["contract"]["features"][0]["description"] = "WRONG_SOURCE_CONTENT"
        return candidate
    interpreter = ObservedSpatial(wrong)
    def observe(body):
        interpreter.settle()
        return record()
    def build(body):
        current = context(body)
        assert current["sourceSpatialContract"]["contract"] is None
        assert "WRONG_SOURCE_CONTENT" not in json.dumps(body)
        return execute()
    executor = Executor()
    result = run(Provider([observe, build, finish()]), executor, tmp_path, files=[raster()], spatial_interpreter=interpreter)
    assert result["status"] == "review_required"
    assert result["sourceSpatialContract"]["status"] == "failed"
    assert result["sourceSpatialContract"]["contract"] is None and len(executor.calls) == 1


def test_slow_auxiliary_pass_does_not_block_planner_and_late_result_cannot_mutate_finished_state(tmp_path):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    base = StubSpatialInterpreter()
    def slow(**kwargs):
        entered.set()
        assert release.wait(3)
        candidate = base.interpret(**kwargs)
        returned.set()
        return candidate
    interpreter = ObservedSpatial(slow)
    def ask(body):
        assert entered.wait(1)
        assert not returned.is_set(), "Planning must begin while auxiliary reasoning is still waiting"
        assert context(body)["sourceSpatialContract"]["contract"] is None
        return {"action": "ask_user", "message": "需要具体尺寸", "questions": ["槽深是多少？"]}
    events = []
    try:
        result = run(Provider([ask]), Executor(), tmp_path, files=[raster()], spatial_interpreter=interpreter, progress=events.append)
        assert result["status"] == "needs_input"
        assert result["sourceSpatialContract"]["status"] == "not_completed"
        assert not returned.is_set()
        before = copy.deepcopy(result)
        state_before = (tmp_path / "agent-state.json").read_bytes()
        checkpoint_before = (tmp_path / "checkpoint.json").read_bytes()
        events_before = copy.deepcopy(events)
    finally:
        release.set()
    interpreter.settle()
    assert returned.is_set()
    assert result == before
    assert (tmp_path / "agent-state.json").read_bytes() == state_before
    assert (tmp_path / "checkpoint.json").read_bytes() == checkpoint_before
    assert events == events_before


def test_text_only_requests_do_not_use_spatial_vision(tmp_path):
    class Forbidden:
        def interpret(self, **kwargs):
            raise AssertionError("No source image exists")
    result = run(Provider([record(), execute(), finish()]), Executor(), tmp_path, spatial_interpreter=Forbidden())
    assert result["status"] == "review_required"
    assert result["sourceSpatialContract"] is None


def test_source_spatial_version_change_invalidates_ledger_but_preserves_editable_plan(tmp_path):
    interpreter = ObservedSpatial()
    def observe(body):
        interpreter.settle()
        return record()
    first = run(Provider([observe, execute(), finish()]), Executor(), tmp_path / "first",
                files=[raster()], spatial_interpreter=interpreter)
    old = copy.deepcopy(first["state"])
    old["sourceSpatialContract"]["version"] = "obsolete-spatial-reader"
    def check(body):
        current = context(body)
        assert current["sourceObservations"] == []
        assert current["currentPlan"]["features"] == first["plan"]["features"]
        interpreter.settle(2)
        return {"action": "ask_user", "message": "请说明要求", "questions": ["需要改什么？"]}
    result = run(Provider([check]), Executor(), tmp_path / "changed", files=[raster()],
                 state=old, spatial_interpreter=interpreter)
    assert len(interpreter.calls) == 2
    assert result["status"] == "needs_input"
    assert result["plan"] == first["plan"]
    assert result["observations"] == []
