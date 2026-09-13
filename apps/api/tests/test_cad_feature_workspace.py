from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_agent_store import CadRunStore
from app.cad_design_workspace import CadDesignWorkspace, DesignNotFound
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureConflict, FeatureNotFound, FeatureWorkspaceError, compile_manual_plan, public_feature_record
from app.cad_feature_workspace_api import create_cad_feature_workspace_router
from app.cad_plan import PlanValidationError
from app.geometry import get_cadquery
from app.platform import AuthService


def draft():
    return {"name": "手工盒体", "changeNote": "将壁厚设为 1 毫米", "suppressed": [], "plan": {
        "version": "cad-plan-v1", "parameters": {"width": {"value": 10}, "halfWidth": {"expression": "width / 2"}},
        "features": [{"id": "block", "op": "box", "size": ["width", 10, 10]},
                     {"id": "cup", "op": "shell", "input": "block", "thickness": -1, "faces": ["maxZ"]}], "result": "cup"}}


def test_owned_immutable_versions_and_conflict_preserve_drafts(tmp_path):
    store = CadFeatureWorkspace(tmp_path)
    created = store.save("alice", draft())
    assert created["revision"] == 1 and not created["productionReady"]
    assert store.list("bob") == []
    with pytest.raises(FeatureNotFound):
        store.get("bob", created["id"])
    data = {**draft(), "expectedRevision": 1, "changeNote": "改宽", "plan": deepcopy(created["plan"])}
    data["plan"]["parameters"]["width"]["value"] = 20
    changed = store.save("alice", data, created["id"])
    assert changed["revision"] == 2
    assert store.get("alice", created["id"], 1)["plan"]["parameters"]["width"]["value"] == 10
    with pytest.raises(FeatureConflict):
        store.save("alice", data, created["id"])
    assert len(store.versions("alice", created["id"])) == 2


def test_delete_reorder_suppress_require_dependency_repair():
    value = draft()["plan"]
    with pytest.raises(FeatureWorkspaceError, match="依赖"):
        compile_manual_plan(value, ["block"])
    with pytest.raises(FeatureWorkspaceError, match="结果"):
        compile_manual_plan(value, ["cup"])
    value["result"] = "block"
    assert [item["id"] for item in compile_manual_plan(value, ["cup"])["features"]] == ["block"]
    value["features"].reverse()
    with pytest.raises(PlanValidationError, match="earlier"):
        compile_manual_plan(value, [])


def test_source_binding_is_server_verified_without_mutating_original_review(tmp_path):
    sources = CadRunStore(tmp_path / "runs")
    run = {"runId": "cad_" + "a" * 32, "revision": 1, "owner": "alice", "status": "review_required", "plan": draft()["plan"]}
    sources.save(run)
    before = sources.load(run["runId"])
    store = CadFeatureWorkspace(tmp_path / "manual", source_store=sources)
    data = {**draft(), "sourceRun": {"runId": run["runId"], "revision": 1, "status": "ready"}, "productionReady": True, "drawingAgreement": "passed"}
    record = store.save("alice", data)
    assert record["sourceRun"]["statusAtFork"] == "review_required"
    assert record["productionReady"] is False and record["drawingAgreement"] == "not_checked"
    assert sources.load(run["runId"]) == before
    with pytest.raises(FeatureNotFound):
        store.save("bob", data)


@pytest.mark.skipif(get_cadquery() is None, reason="CadQuery unavailable")
def test_manual_build_real_step_reimport_and_draft_export_gate(tmp_path):
    store = CadFeatureWorkspace(tmp_path)
    created = store.save("alice", draft())
    with pytest.raises(FeatureNotFound):
        store.artifact("alice", created["id"], 1, "step")
    built = store.build("alice", created["id"], 1)
    assert built["status"] == "built" and built["revision"] == 2
    assert built["drawingAgreement"] == "not_checked" and not built["productionReady"]
    assert built["inspection"]["stepReadback"]["valid"]
    assert built["resolvedParameters"]["halfWidth"] == 5
    path, mime = store.artifact("alice", created["id"], 2, "step")
    cq = get_cadquery()
    assert cq.importers.importStep(str(path)).val().Volume() == pytest.approx(424)
    public = public_feature_record(built)
    assert str(tmp_path) not in str(public)
    assert mime == "application/step"
    with pytest.raises(FeatureNotFound):
        store.artifact("bob", created["id"], 2, "step")
    with pytest.raises(FeatureConflict):
        store.build("alice", created["id"], 1)
    changed = draft()
    changed["plan"]["parameters"]["width"]["value"] = None
    latest = store.save("alice", {**changed, "expectedRevision": 2}, created["id"])
    with pytest.raises(FeatureWorkspaceError, match="请补充参数"):
        store.build("alice", created["id"], latest["revision"])
    assert store.get("alice", created["id"])["status"] == "draft"
    assert store.artifact("alice", created["id"], 2, "step")[0] == path
    assert path.is_file()


def test_failed_execution_cannot_create_built_revision_or_artifact(tmp_path):
    def fails(*_args, **_kwargs):
        return {"status": "failed", "errors": [{"message": "invalid geometry", "featureId": "cup"}], "artifacts": {}}
    store = CadFeatureWorkspace(tmp_path, executor=fails)
    created = store.save("alice", draft())
    with pytest.raises(FeatureWorkspaceError, match="特征 cup.*invalid geometry") as error:
        store.build("alice", created["id"], 1)
    assert error.value.feature_id == "cup"
    assert store.get("alice", created["id"])["revision"] == 1
    assert store.get("alice", created["id"])["artifacts"] == {}


def test_missing_dimensions_preserve_editable_draft_and_explain_which_to_fill(tmp_path):
    store = CadFeatureWorkspace(tmp_path)
    data = draft()
    data["plan"]["parameters"]["width"]["value"] = None
    created = store.save("alice", data)
    with pytest.raises(FeatureWorkspaceError, match="请补充参数：width"):
        store.build("alice", created["id"], 1)
    assert store.get("alice", created["id"])["status"] == "draft"
    assert len(store.versions("alice", created["id"])) == 1


def test_manual_api_auth_body_limits_and_cross_account_version_access(tmp_path):
    auth = AuthService(tmp_path / "accounts.sqlite3", token_secret="feature-test-" * 4)
    users = [auth.create_user(f"{name}@example.com", "a-long-test-password", name, roles=["designer"]) for name in ["a", "b"]]
    viewer = auth.create_user("viewer@example.com", "a-long-test-password", "viewer", roles=["viewer"])
    viewer_header = {"Authorization": "Bearer " + auth.issue_token(viewer).token}
    headers = [{"Authorization": "Bearer " + auth.issue_token(user).token} for user in users]
    app = FastAPI()
    app.include_router(create_cad_feature_workspace_router(SimpleNamespace(auth=auth), root=tmp_path / "manual"))
    try:
        with TestClient(app) as client:
            base = "/api/cad/features"
            assert client.get(base).status_code == 401
            assert client.get(base, headers=viewer_header).status_code == 200
            assert client.post(base, headers=viewer_header, json=draft()).status_code == 403
            response = client.post(base, headers=headers[0], json=draft())
            assert response.status_code == 201, response.text
            design_id = response.json()["id"]
            assert client.get(f"{base}/{design_id}", headers=headers[1]).status_code == 404
            assert client.get(f"{base}/{design_id}/versions", headers=headers[1]).status_code == 404
            assert client.get(f"{base}/{design_id}/versions/1/artifacts/step", headers=headers[0]).status_code == 404
            assert client.post(base, headers=headers[0], content='{"name":"' + 'x' * 300_000 + '"}').status_code == 413
            assert client.put(f"{base}/{design_id}", headers=headers[0], json={**draft(), "expectedRevision": 0}).status_code == 409
            assert client.put(f"{base}/{design_id}", headers=viewer_header, json={**draft(), "expectedRevision": 1}).status_code == 403
            assert client.post(f"{base}/{design_id}/build", headers=viewer_header, json={"expectedRevision": 1}).status_code == 403
            attack = draft()
            attack["plan"]["parameters"]["width"] = {"expression": "__import__('os').system('touch /tmp/forged-feature')"}
            assert client.post(base, headers=headers[0], json=attack).status_code == 422
            assert client.get(f"{base}/{design_id}?revision=-1", headers=headers[0]).status_code == 422
            auth.set_active(users[0].id, False, actor_id="test-admin")
            assert client.get(f"{base}/{design_id}", headers=headers[0]).status_code == 401
            assert client.post(f"{base}/{design_id}/build", headers=headers[0], json={"expectedRevision": 1}).status_code == 401
            assert client.get("/openapi.json").status_code == 200
    finally:
        auth.close()


@pytest.mark.skipif(get_cadquery() is None, reason="CadQuery unavailable")
def test_feature_step_links_to_owned_shared_engineering_design(tmp_path):
    engineering = CadDesignWorkspace(tmp_path / "engineering")
    store = CadFeatureWorkspace(tmp_path / "manual", design_store=engineering)
    created = store.save("alice", {**draft(), "fileId": "active-part-a"})
    built = store.build("alice", created["id"], 1)
    shared = built["sharedDesign"]
    assert shared["fileId"] == "active-part-a"
    assert shared["metrics"]["solidCount"] == 1
    step, _ = engineering.artifact("alice", shared["id"], "step")
    assert get_cadquery().importers.importStep(str(step)).val().Volume() == pytest.approx(424)
    with pytest.raises(DesignNotFound):
        engineering.get("bob", shared["id"])
    assert str(tmp_path) not in str(public_feature_record(built))
