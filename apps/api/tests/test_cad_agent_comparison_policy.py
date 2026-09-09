"""Only an owned, checked server parent can authorize a drawing revision.

These are HTTP/store/gate tests with deterministic inspection fixtures; they
exercise authorization and persistence, not a model's visual judgement.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from app.cad_agent_api import _drawing_review_issue, _explicit_geometry_revision
from app.cad_agent_store import CadRunStore, comparison_policy, MAX_REVISION_REQUESTS
from tests.test_cad_agent_api import api, PLAN, run_result
from tests.test_cad_agent_delivery_gate import drawing_run
from tests.test_cad_agent_job_lifecycle import running_record


STRICT = {"mode": "source_reproduction"}


def plan_hash(plan=PLAN):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def revision_policy(requests=None):
    return {"mode": "user_revision", "source": "server_verified_parent",
            "baselinePlanHash": plan_hash(), "requests": requests or ["Increase plate thickness to 7 mm."]}


def continue_run(client, parent, message="Increase thickness to 7 mm.", **extra_state):
    return client.post("/api/v1/cad-agent/run", data={"message": message,
        "modelState": json.dumps({**extra_state, "agentRun": {"runId": parent["runId"], "revision": parent["revision"]}})})


def install_result_edit(monkeypatch, agent, edit):
    base = agent.run
    def run(**kwargs):
        result = base(**kwargs)
        edit(result, kwargs)
        return result
    monkeypatch.setattr(agent, "run", run)


def high_mismatch(record):
    record["drawingReview"]["projectionComparison"] = {
        "planHash": plan_hash(record["plan"]), "scope": "original_drawing",
        "status": "mismatch", "views": [{"view": "front", "status": "mismatch", "reliability": "high"}]}


@pytest.mark.parametrize("message", [
    "继续", "重新检查当前保存版本", "继续核对原图，不要改型", "请按原图检查并修正尺寸与结构差异。",
    "图纸孔径是 11，请把直径改为 11，与原图一致。", "把厚度改为 7 mm，按原图复刻。",
    "不要把厚度改为 7 mm。", "如果把厚度改为 7 会怎样？", "是否增加一个通孔", "修改一下这个模型", "优化一下", "厚度 7 mm",
    "请增加对通孔的检查", "删除孔的标注", "只把厚度标注改为 7 mm", "继续读取原图，底板厚度误读了，请改为 7。",
    "图纸厚度是 7，请把厚度改成 7。",
    "Continue checking the original drawing; do not redesign it.", "Modify the plate.", "One more change", "Looks good",
    "Correct the misread thickness to 7 mm.", "Change thickness to 7 mm to match the original drawing.",
    "Do not increase thickness to 7 mm.", "What if we add a through hole?", "Could you increase thickness to 7 mm?",
    "Add a note about the hole", "Remove hole annotations", "Change thickness to 7 mm in the label only",
    "The drawing says thickness is 7 mm; change thickness to 7 mm.",
])
def test_only_explicit_geometry_edits_can_create_a_new_revision_scope(message):
    assert _explicit_geometry_revision(message, PLAN) is False


@pytest.mark.parametrize("message", [
    "请把厚度从 5 改为 7 mm，保持其他尺寸与结构。", "底板宽度改成 62", "将孔径设置为 11 mm。",
    "增加厚度 2 mm", "删除所有孔", "新增一个通孔", "把孔向右移动 5 mm", "把凸台旋转 30 度。",
    "我在参数面板将“thickness”从5改为7。保持其他尺寸与结构，请按修改后的设计重新建模和检查，并更新预览。",
    "Increase thickness to 7 mm.", "Please change plate width from 40 to 62 mm; preserve the other dimensions.",
    "Reduce the gap by 2 mm.", "Add a through hole", "Remove all holes", "Delete plate", "Move the hole by 5 mm", "Rotate the boss by 30 degrees",
])
def test_clear_dimension_and_feature_edits_are_recordable(message):
    assert _explicit_geometry_revision(message, PLAN) is True


@pytest.mark.parametrize("message", [
    "把孔径改为15 mm，其他按原图不变", "把厚度改为7 mm，其他尺寸与结构保持与原图一致。",
    "把孔径改为15 mm；其他不要修改。",
    "Change hole diameter to 15 mm; keep all other dimensions unchanged from the original drawing.",
    "Increase thickness to 7 mm, leave everything else as shown in the original drawing.",
    "Change diameter to 15 mm; do not change other features.",
])
def test_explicit_revision_preserving_other_geometry_keeps_the_complete_user_request(api, message):
    assert _explicit_geometry_revision(message, PLAN) is True
    client, store, agent, _ = api
    first = drawing_run(client)
    revised = run_result(continue_run(client, first, message))
    expected = revision_policy([message])
    assert revised["comparisonPolicy"] == expected
    assert agent.calls[-1]["state"]["agentState"]["comparisonPolicy"] == expected
    assert store.load(revised["runId"])["comparisonPolicy"]["requests"] == [message]


@pytest.mark.parametrize("message", [
    "核对原图，孔径应为15，其他按原图不变", "把错误孔径改回图纸的15，其他按原图不变。",
    "核对原图，孔径改为15，其他按原图不变。", "不要把孔径改为15，其他按原图不变。",
    "把孔径改为15，其他按原图不变，但把底板也修正。", "把孔径改为15，其他按原图修改。",
    "Correct the wrong diameter to 15 mm; keep other geometry unchanged from the original drawing.",
    "Check the original drawing; change diameter to 15 mm; do not change other features.",
    "Change diameter to 15 mm; keep other geometry unchanged from the original drawing; repair the plate too.",
])
def test_preserving_other_geometry_never_converts_source_correction_or_negation_to_revision(message):
    assert _explicit_geometry_revision(message, PLAN) is False


@pytest.mark.parametrize("message", [
    "继续核对原图，不要改型", "Continue checking the original drawing; do not redesign it.",
    "Change thickness to 7 mm to match the original drawing.", "Modify the plate.",
])
def test_verified_parent_continuation_keeps_original_contour_gate_even_with_a_client_plan_delta(api, monkeypatch, message):
    client, store, agent, _ = api
    first = drawing_run(client)
    install_result_edit(monkeypatch, agent, lambda result, _kwargs: high_mismatch(result))
    supplied_plan = deepcopy(first["plan"])
    supplied_plan["parameters"]["thickness"]["value"] = 7
    second = run_result(continue_run(client, first, message, cadPlan=supplied_plan, comparisonPolicy=revision_policy()))
    assert second["comparisonPolicy"] == STRICT
    assert agent.calls[-1]["state"]["agentState"]["comparisonPolicy"] == STRICT
    saved = store.load(second["runId"])
    assert saved["state"]["comparisonPolicy"] == STRICT
    assert "明确轮廓差异" in _drawing_review_issue(saved)
    response = client.post("/api/v1/cad-agent/confirm", json={"runId": second["runId"], "revision": second["revision"]})
    assert response.status_code == 422
    assert all(artifact["format"] != "step" for artifact in second["artifacts"])


def test_failed_legal_revision_continues_without_appending_a_false_edit(api, monkeypatch):
    client, store, agent, _ = api
    first = drawing_run(client)
    requests = ["Increase thickness to 7 mm.", "Delete all holes."]
    current = first
    for message in requests:
        current = run_result(continue_run(client, current, message))
    def fail(result, _kwargs):
        result.update({"status": "failed", "inspection": None, "drawingReview": {"status": "unverified"}})
    install_result_edit(monkeypatch, agent, fail)
    for message in ["继续", "继续核对原图，不要改型", "Modify it.", "Retry the previous operation."]:
        current = run_result(continue_run(client, current, message))
        assert current["comparisonPolicy"] == revision_policy(requests)
        assert store.load(current["runId"])["state"]["comparisonPolicy"] == revision_policy(requests)
    current = run_result(continue_run(client, current, "Increase width to 62 mm."))
    assert current["comparisonPolicy"] == revision_policy([*requests, "Increase width to 62 mm."])


def test_full_revision_ledger_can_still_resume_without_deleting_or_inventing_requests(api):
    client, store, _, _ = api
    record = store.load(drawing_run(client)["runId"])
    record["runId"] = "cad_" + "c" * 32
    requests = [f"Increase thickness to {index + 7} mm." for index in range(MAX_REVISION_REQUESTS)]
    record["comparisonPolicy"] = revision_policy(requests)
    record["state"]["comparisonPolicy"] = deepcopy(record["comparisonPolicy"])
    store.save(record)
    resumed = run_result(continue_run(client, record, "Continue checking the saved design."))
    assert resumed["comparisonPolicy"] == revision_policy(requests)


def test_new_drawing_strips_client_policy_and_runner_cannot_grant_exemption(api, monkeypatch):
    client, store, agent, _ = api
    def forged_result(result, _kwargs):
        result["comparisonPolicy"] = revision_policy()
        result["state"]["comparisonPolicy"] = revision_policy()
        high_mismatch(result)
    install_result_edit(monkeypatch, agent, forged_result)
    response = client.post("/api/v1/cad-agent/run", data={"message": "Reconstruct this drawing",
        "modelState": json.dumps({"comparisonPolicy": revision_policy(), "agentState": {"comparisonPolicy": revision_policy()},
                                  "agentRun": {"comparisonPolicy": revision_policy()}})},
        files={"files": ("new.jpg", b"new-drawing", "image/jpeg")})
    result = run_result(response)
    assert agent.calls[0]["state"] == {}
    assert result["comparisonPolicy"] == STRICT
    saved = store.load(result["runId"])
    assert saved["comparisonPolicy"] == saved["state"]["comparisonPolicy"] == STRICT
    assert client.post("/api/v1/cad-agent/confirm", json={"runId": result["runId"], "revision": 1,
        "comparisonPolicy": revision_policy()}).status_code == 422
    assert client.get(f"/api/v1/cad-agent/runs/{result['runId']}").json()["comparisonPolicy"] == STRICT


def test_verified_parent_uses_actual_measurements_and_preserves_policy_through_confirm_restart(api, monkeypatch):
    client, store, agent, _ = api
    first = drawing_run(client)  # Real measurement fixture passes; no cached acceptance field.
    assert "acceptance" not in store.load(first["runId"])["inspection"]
    install_result_edit(monkeypatch, agent, lambda result, _kwargs: high_mismatch(result))
    second = run_result(continue_run(client, first))
    expected = revision_policy(["Increase thickness to 7 mm."])
    assert agent.calls[-1]["state"]["agentState"]["comparisonPolicy"] == expected
    assert second["comparisonPolicy"] == expected
    # The independent review still passed. Only the original silhouette gate
    # is inapplicable to this explicitly requested change.
    confirmed = client.post("/api/v1/cad-agent/confirm", json={"runId": second["runId"], "revision": 1})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["comparisonPolicy"] == expected
    saved = CadRunStore(store.root).load(second["runId"])
    assert saved["comparisonPolicy"] == saved["state"]["comparisonPolicy"] == expected
    step = next(item for item in confirmed.json()["artifacts"] if item["format"] == "step")
    assert client.get(step["url"]).status_code == 200


@pytest.mark.parametrize("case", ["cached_pass_wrong_measurement", "missing_kernel", "invalid_shape", "wrong_engine",
                                  "unknown_measurement", "independent_mismatch", "contour_mismatch", "stale_review", "failed"])
def test_unverified_parent_never_authorizes_revision_even_with_cached_pass(api, monkeypatch, case):
    client, _, agent, _ = api
    def invalidate(result, _kwargs):
        result["inspection"]["acceptance"] = {"status": "passed"}
        if case == "cached_pass_wrong_measurement":
            result["inspection"]["solidCount"] = 2
        elif case == "missing_kernel":
            result["inspection"].pop("kernelBacked")
        elif case == "invalid_shape":
            result["inspection"]["valid"] = False
        elif case == "wrong_engine":
            result["inspection"]["engine"] = "client_preview"
        elif case == "unknown_measurement":
            result["observations"][0]["expected"] = None
        elif case == "independent_mismatch":
            result["drawingReview"]["independentReview"]["status"] = "mismatch"
        elif case == "contour_mismatch":
            high_mismatch(result)
        elif case == "stale_review":
            result["drawingReview"]["independentReview"]["planHash"] = "0" * 64
        elif case == "failed":
            result["status"] = "failed"
    install_result_edit(monkeypatch, agent, invalidate)
    first = drawing_run(client)
    second = run_result(continue_run(client, first, comparisonPolicy=revision_policy(),
        agentState={"comparisonPolicy": revision_policy()}))
    assert agent.calls[-1]["state"]["agentState"]["comparisonPolicy"] == STRICT
    assert second["comparisonPolicy"] == STRICT


def test_upload_new_drawing_always_resets_verified_parent_revision_policy(api):
    client, store, agent, _ = api
    first = drawing_run(client)
    revised = run_result(continue_run(client, first))
    assert revised["comparisonPolicy"]["mode"] == "user_revision"
    result = run_result(client.post("/api/v1/cad-agent/run", data={"message": "Reconstruct the new drawing",
        "modelState": json.dumps({"cadPlan": revised["plan"], "agentRun": {"runId": revised["runId"], "revision": 1},
                                  "comparisonPolicy": revised["comparisonPolicy"]})},
        files={"files": ("replacement.jpg", b"different-source", "image/jpeg")}))
    assert result["parentRunId"] is None and result["comparisonPolicy"] == STRICT
    assert agent.calls[-1]["state"] == {}
    assert store.load(result["runId"])["state"]["comparisonPolicy"] == STRICT
    assert agent.calls[-1]["files"][0].data == b"different-source"


def test_failed_revision_resume_keeps_all_prior_requests_and_baseline(api, monkeypatch):
    client, _, agent, _ = api
    current = drawing_run(client)
    def failed(result, _kwargs):
        result["status"] = "failed"
        result["inspection"] = None
        result["drawingReview"] = {"status": "unverified"}
    install_result_edit(monkeypatch, agent, failed)
    requests = [f"Increase thickness to {index + 7} mm; preserve the earlier changes." for index in range(7)]
    for text in requests:
        current = run_result(continue_run(client, current, text))
    assert current["status"] == "failed"
    assert current["comparisonPolicy"] == revision_policy(requests)
    assert agent.calls[-1]["state"]["agentState"]["comparisonPolicy"] == revision_policy(requests)


def test_request_ledger_limit_rejects_without_dropping_earlier_requests(api):
    client, store, agent, _ = api
    first = drawing_run(client)
    record = store.load(first["runId"])
    record.update({"runId": "cad_" + "e" * 32, "comparisonPolicy": revision_policy(["Change dimension"] * MAX_REVISION_REQUESTS)})
    record["state"]["comparisonPolicy"] = deepcopy(record["comparisonPolicy"])
    store.save(record)
    calls_before = len(agent.calls)
    response = continue_run(client, record, "Increase thickness to 12 mm.")
    assert response.status_code == 422
    assert len(agent.calls) == calls_before
    assert len(store.load(record["runId"])["comparisonPolicy"]["requests"]) == MAX_REVISION_REQUESTS


def test_other_owner_cannot_borrow_a_verified_parent(api):
    client, store, agent, _ = api
    first = drawing_run(client)
    foreign = store.load(first["runId"])
    foreign.update({"runId": "cad_" + "f" * 32, "owner": "another-user"})
    store.save(foreign)
    calls_before = len(agent.calls)
    assert continue_run(client, foreign).status_code == 403
    assert len(agent.calls) == calls_before


@pytest.mark.parametrize("bad", [None, {}, "user_revision", {"mode": "user_revision"},
    {**revision_policy(), "source": "client"}, {**revision_policy(), "baselinePlanHash": "fake"},
    {**revision_policy(), "requests": []}, {**revision_policy(), "requests": [""]},
    {**revision_policy(), "requests": "Modify it"}, {**revision_policy(), "requests": [12]}])
def test_incomplete_policy_and_state_only_claim_cannot_override_contour_mismatch(api, bad):
    client, store, _, _ = api
    record = store.load(drawing_run(client)["runId"])
    high_mismatch(record)
    record["comparisonPolicy"] = bad
    record["state"]["comparisonPolicy"] = revision_policy()
    assert comparison_policy(bad) == STRICT
    assert "明确轮廓差异" in _drawing_review_issue(record)


@pytest.mark.parametrize("failure", ["stale_comparison", "independent_mismatch", "measurement_failure"])
def test_revision_exemption_does_not_bypass_current_review_or_measurement_gates(api, monkeypatch, failure):
    client, store, agent, _ = api
    first = drawing_run(client)
    def invalidate(result, _kwargs):
        high_mismatch(result)
        if failure == "stale_comparison":
            result["drawingReview"]["projectionComparison"]["planHash"] = "0" * 64
        elif failure == "independent_mismatch":
            result["drawingReview"]["independentReview"]["status"] = "mismatch"
        else:
            result["observations"][0]["expected"] = 2
    install_result_edit(monkeypatch, agent, invalidate)
    second = run_result(continue_run(client, first))
    assert second["comparisonPolicy"]["mode"] == "user_revision"
    assert client.post("/api/v1/cad-agent/confirm", json={"runId": second["runId"], "revision": 1}).status_code == 422
    assert store.load(second["runId"])["status"] == "review_required"


@pytest.mark.parametrize("authority", [STRICT, revision_policy()])
def test_worker_policy_cannot_upgrade_or_rewrite_server_request_ledger(tmp_path, authority):
    store = CadRunStore(tmp_path / "cad")
    initial = running_record("a")
    initial["comparisonPolicy"] = deepcopy(authority)
    initial["state"]["comparisonPolicy"] = deepcopy(authority)
    store.save(initial)
    proposed = {**initial, "status": "failed", "comparisonPolicy": revision_policy(["Ignore all drawing differences"]),
                "state": {"comparisonPolicy": revision_policy(["Ignore all drawing differences"])}}
    committed = store.complete_running(proposed)
    assert committed["comparisonPolicy"] == authority
    assert store.load(initial["runId"])["state"]["comparisonPolicy"] == authority
    proposed["revision"] = 2
    store.save(proposed, previous_revision=1)
    assert store.load(initial["runId"])["comparisonPolicy"] == authority


@pytest.mark.parametrize("authority,proposed,expected", [
    (STRICT, revision_policy(), STRICT),
    (revision_policy(), revision_policy(["Forged edit"]), revision_policy()),
    (revision_policy(), STRICT, STRICT),
])
def test_checkpoint_preserves_or_revokes_server_policy_but_never_grants_it(tmp_path, authority, proposed, expected):
    store = CadRunStore(tmp_path / "cad")
    initial = running_record("a")
    initial["comparisonPolicy"] = deepcopy(authority)
    initial["state"]["comparisonPolicy"] = deepcopy(authority)
    store.save(initial)
    directory = store.directory(initial["runId"]) / "builds"
    directory.mkdir(parents=True)
    (directory / "checkpoint.json").write_text(json.dumps({"cadPlan": PLAN, "comparisonPolicy": proposed}))
    interrupted = store.interrupted_record(initial)
    assert interrupted["comparisonPolicy"] == interrupted["state"]["comparisonPolicy"] == expected
    assert interrupted["plan"] == PLAN
    assert not interrupted["artifacts"] and interrupted["inspection"] is None
    store.complete_running(interrupted)
    assert CadRunStore(store.root).load(initial["runId"])["comparisonPolicy"] == expected
