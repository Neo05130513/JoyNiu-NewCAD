import copy
import hashlib
import io
import json
import shutil
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_design_workspace_api import create_cad_design_workspace_router
from app.cad_feature_workspace import CadFeatureWorkspace
from app.geometry import get_cadquery
from app.platform import AuthService, PDMRepository, Role


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("editor-pdm")
    auth = AuthService(root / "platform.sqlite3", token_secret="synthetic-cad-pdm-test" * 2)
    pdm = PDMRepository(root / "platform.sqlite3")
    actors = {name: auth.create_user(f"{name}@example.invalid", "Synthetic!Pass2026", name, roles=[role])
              for name, role in (("alice", Role.DESIGNER), ("bob", Role.DESIGNER), ("reader", Role.VIEWER))}
    headers = {name: {"Authorization": "Bearer " + auth.issue_token(actor).token} for name, actor in actors.items()}
    services = SimpleNamespace(auth=auth, pdm=pdm)
    router = create_cad_design_workspace_router(services, root=root / "engineering")
    app = FastAPI(); app.include_router(router)
    features = CadFeatureWorkspace(root / "cad-feature-workspace")
    plan = {"version": "cad-plan-v1", "units": "mm", "parameters": {"width": {"value": 20}},
            "features": [{"id": "body", "op": "box", "size": ["width", 16, 10]}], "result": "body"}
    preview = features.preview(actors["alice"].id, {"plan": plan})
    plan["features"].append({"id": "round", "op": "fillet", "input": "body", "radius": 1, "edges": [preview["edges"][0]["selector"]]})
    plan["result"] = "round"
    old = features.commit(actors["alice"].id, {"name": "真实圆角件", "fileId": "file-pdm-source", "plan": plan, "requestId": str(uuid4()), "changeNote": "合成 PDM 验收"})
    newer = copy.deepcopy(old["plan"]); newer["parameters"]["width"]["value"] = 22
    latest = features.commit(actors["alice"].id, {"name": "真实圆角件", "fileId": "file-pdm-source", "plan": newer,
                "designId": old["id"], "expectedRevision": 1, "requestId": str(uuid4()), "changeNote": "合成 PDM 验收"})
    with TestClient(app) as client:
        yield SimpleNamespace(root=root, auth=auth, pdm=pdm, actors=actors, headers=headers, features=features,
                              designs=router.design_store, client=client, old=old, latest=latest)
    auth.close(); pdm.close()


def project(env, owner="alice"):
    return env.pdm.create_project("PDM " + uuid4().hex[:8], env.actors[owner].id)


def payload(env, project_id, **updates):
    return {"projectId": project_id, "name": "精确实体", "featureId": env.old["id"], "revision": 1,
            "requestId": str(uuid4()), "expectedCurrentRevision": 0, **updates}


BASE = "/api/cad/designs/pdm"


def test_saves_exact_older_version_to_real_pdm_and_engineering_and_reopens_editable_bindings(env):
    target = project(env); data = payload(env, target.id)
    response = env.client.post(BASE + "/versions", json=data, headers=env.headers["alice"])
    assert response.status_code == 201, response.text
    result = response.json(); version_id = result["version"]["id"]
    assert result["source"] == {"featureId": env.old["id"], "revision": 1, "fileId": "file-pdm-source"}
    assert result["version"]["revision"] == 1 and not result["productionReady"]
    original, _ = env.features.artifact(env.actors["alice"].id, env.old["id"], 1, "step")
    blob = env.pdm.get_version_content(version_id)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.read("model.step") == original.read_bytes()
        assert json.loads(archive.read("plan.json"))["parameters"]["width"]["value"] == 20
    actual = env.client.get(f"{BASE}/versions/{version_id}/artifacts/step", headers=env.headers["alice"])
    assert actual.status_code == 200 and actual.content == original.read_bytes()
    engineering = env.designs.get(env.actors["alice"].id, result["engineeringDesignId"])
    assert engineering["sourceFeature"] == {"id": env.old["id"], "revision": 1}
    assert engineering["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert env.client.post(BASE + "/versions", json=data, headers=env.headers["alice"]).json()["version"]["id"] == version_id
    assert len(env.pdm.list_versions(result["document"]["id"])) == 1
    opened = env.client.post(f"{BASE}/versions/{version_id}/open", json={"fileId": "new-pdm-file", "requestId": "open-exact-version"}, headers=env.headers["alice"])
    assert opened.status_code == 201, opened.text
    record = opened.json()["record"]
    assert record["id"] != env.old["id"] and record["revision"] == 1 and record["sourceRun"] is None
    assert record["plan"]["parameters"]["width"]["value"] == 20
    copied, _ = env.features.artifact(env.actors["alice"].id, record["id"], 1, "step")
    assert copied.read_bytes() == original.read_bytes()
    changed = copy.deepcopy(record["plan"]); changed["parameters"]["width"]["value"] = 24
    rebuilt = env.features.commit(env.actors["alice"].id, {"name": record["name"], "fileId": record["fileId"], "plan": changed,
        "designId": record["id"], "expectedRevision": 1, "requestId": str(uuid4()), "changeNote": "合成 PDM 验收"})
    assert rebuilt["inspection"]["stepReadback"]["valid"] and rebuilt["inspection"]["bbox"]["size"][0] == pytest.approx(24)
    assert env.features.get(env.actors["alice"].id, env.old["id"])["revision"] == env.latest["revision"]
    repeated = env.client.post(f"{BASE}/versions/{version_id}/open", json={"fileId": "new-pdm-file", "requestId": "open-exact-version"}, headers=env.headers["alice"])
    assert repeated.json()["record"]["revision"] == 1  # exact PDM snapshot, never silently latest


def test_project_and_feature_ownership_and_readonly_roles_are_enforced(env):
    target = project(env); foreign = project(env, "bob")
    data = payload(env, target.id)
    assert env.client.get(BASE + "/projects").status_code == 401
    assert target.id not in {item["id"] for item in env.client.get(BASE + "/projects", headers=env.headers["bob"]).json()["items"]}
    assert env.client.get(f"{BASE}/projects/{target.id}/documents", headers=env.headers["bob"]).status_code == 403
    assert env.client.post(BASE + "/versions", json=data, headers=env.headers["bob"]).status_code == 403
    assert env.client.post(BASE + "/versions", json={**data, "projectId": foreign.id}, headers=env.headers["bob"]).status_code == 404
    assert env.client.post(BASE + "/versions", json=data, headers=env.headers["reader"]).status_code == 403
    saved = env.client.post(BASE + "/versions", json=data, headers=env.headers["alice"]).json()
    version_id = saved["version"]["id"]
    for path in (f"/versions/{version_id}", f"/versions/{version_id}/artifacts/step"):
        assert env.client.get(BASE + path, headers=env.headers["bob"]).status_code == 403
    assert env.client.post(f"{BASE}/versions/{version_id}/open", json={"fileId": "foreign", "requestId": str(uuid4())}, headers=env.headers["bob"]).status_code == 403


def test_missing_draft_stale_or_tampered_source_never_claims_a_successful_pdm_version(env):
    target = project(env)
    assert env.client.post(BASE + "/versions", json=payload(env, target.id, revision=999), headers=env.headers["alice"]).status_code == 404
    assert env.client.post(BASE + "/versions", json=payload(env, target.id, revision=True), headers=env.headers["alice"]).status_code == 422
    draft = env.features.save(env.actors["alice"].id, {"name": "draft", "fileId": "draft", "changeNote": "保存合成草稿", "plan": {"version": "cad-plan-v1", "features": [{"id": "a", "op": "box", "size": [2, 2, 2]}], "result": "a"}})
    assert env.client.post(BASE + "/versions", json=payload(env, target.id, featureId=draft["id"]), headers=env.headers["alice"]).status_code == 422
    assert env.pdm.list_documents(target.id) == []
    saved = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"]).json()
    stale = payload(env, target.id, revision=2)
    assert env.client.post(BASE + "/versions", json=stale, headers=env.headers["alice"]).status_code == 409
    appended = env.client.post(BASE + "/versions", json={**stale, "expectedCurrentRevision": 1}, headers=env.headers["alice"])
    assert appended.status_code == 201 and appended.json()["version"]["revision"] == 2
    assert len(env.pdm.list_versions(saved["document"]["id"])) == 2


def test_untrusted_upload_cannot_authorize_history_and_owned_archive_survives_source_removal(env):
    target = project(env)
    saved = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"]).json()
    trusted = saved["version"]["id"]
    forged_doc = env.pdm.create_document(target.id, "上传伪装包", "model", env.actors["alice"].id, metadata={"format": "joyniu-cad-pdm-v1"})
    forged = env.pdm.create_version(forged_doc.id, env.pdm.get_version_content(trusted), env.actors["alice"].id, content_type="application/zip", metadata=saved["version"]["metadata"])
    assert env.client.post(f"{BASE}/versions/{forged.id}/open", json={"fileId": "forged", "requestId": str(uuid4())}, headers=env.headers["alice"]).status_code == 404
    first = env.client.post(f"{BASE}/versions/{trusted}/open", json={"fileId": "temporary-copy", "requestId": str(uuid4())}, headers=env.headers["alice"]).json()["record"]
    second = env.client.post(BASE + "/versions", json=payload(env, target.id, name="持久副本", featureId=first["id"]), headers=env.headers["alice"]).json()
    with env.features._db() as db: db.execute("DELETE FROM manual_features WHERE id=?", (first["id"],))
    shutil.rmtree(env.features.root / first["id"])
    reopened = env.client.post(f"{BASE}/versions/{second['version']['id']}/open", json={"fileId": "surviving-copy", "requestId": str(uuid4())}, headers=env.headers["alice"])
    assert reopened.status_code == 201, reopened.text
    record = reopened.json()["record"]
    path, _ = env.features.artifact(env.actors["alice"].id, record["id"], 1, "step")
    assert get_cadquery().importers.importStep(str(path)).val().isValid()


def test_source_artifact_and_saved_archive_checksums_are_required(env):
    target = project(env)
    artifact, _ = env.features.artifact(env.actors["alice"].id, env.old["id"], 1, "glb")
    original = artifact.read_bytes()
    try:
        artifact.write_bytes(original + b"tampered")
        response = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"])
        assert response.status_code == 422 and "校验失败" in response.text
        assert env.pdm.list_documents(target.id) == []
    finally:
        artifact.write_bytes(original)
    response = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"])
    assert response.status_code == 201, response.text
    version = response.json()["version"]["id"]
    original = env.pdm.get_version_content(version)
    with env.pdm._lock, env.pdm._connection:
        env.pdm._connection.execute("UPDATE pdm_versions SET content=? WHERE id=?", (original+b"tampered", version))
    try:
        response = env.client.post(f"{BASE}/versions/{version}/open", json={"fileId": "tampered", "requestId": str(uuid4())}, headers=env.headers["alice"])
        assert response.status_code == 409 and "checksum mismatch" in response.text
    finally:
        with env.pdm._lock, env.pdm._connection:
            env.pdm._connection.execute("UPDATE pdm_versions SET content=? WHERE id=?", (original, version))


def test_pdm_read_member_can_open_owned_copy_without_inheriting_source_owner_authority(env):
    target = project(env)
    saved = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"]).json()
    env.pdm.update_project_metadata(target.id, {"members": {env.actors["bob"].id: "read"}}, actor_id=env.actors["alice"].id)
    read = env.client.get(f"{BASE}/projects/{target.id}/documents", headers=env.headers["bob"])
    assert read.status_code == 200 and read.json()["items"]
    assert env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["bob"]).status_code == 403
    copied = env.client.post(f"{BASE}/versions/{saved['version']['id']}/open", json={"fileId": "member-working-copy", "requestId": str(uuid4())}, headers=env.headers["bob"])
    assert copied.status_code == 201, copied.text
    record = copied.json()["record"]
    assert env.features.get(env.actors["bob"].id, record["id"])["revision"] == 1
    from app.cad_feature_workspace import FeatureNotFound
    with pytest.raises(FeatureNotFound): env.features.get(env.actors["alice"].id, record["id"])
    changed = copy.deepcopy(record["plan"]); changed["parameters"]["width"]["value"] = 21
    rebuilt = env.features.commit(env.actors["bob"].id, {"name": "成员独立编辑", "fileId": record["fileId"], "plan": changed,
        "designId": record["id"], "expectedRevision": 1, "requestId": str(uuid4()), "changeNote": "修改副本参数"})
    assert rebuilt["inspection"]["stepReadback"]["valid"]
    assert env.features.get(env.actors["alice"].id, env.old["id"])["revision"] == 2


def test_pdm_keeps_effective_plan_artifact_and_full_suppression_history_separately(env):
    target = project(env)
    value = {"version": "cad-plan-v1", "units": "mm", "features": [{"id": "body", "op": "box", "size": [3,4,5]}, {"id": "hidden", "op": "box", "size": [2,2,2]}], "result": "body"}
    committed = env.features.commit(env.actors["alice"].id, {"name": "抑制记录", "fileId": "suppressed", "plan": value, "suppressed": ["hidden"], "requestId": str(uuid4()), "changeNote": "保存抑制版本"})
    saved = env.client.post(BASE + "/versions", json=payload(env, target.id, featureId=committed["id"]), headers=env.headers["alice"])
    assert saved.status_code == 201, saved.text
    version = saved.json()["version"]["id"]
    bundle = env.pdm.get_version_content(version)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        effective = json.loads(archive.read("plan.json"))
        full = json.loads(archive.read("record.json"))
        assert [f["id"] for f in effective["features"]] == ["body"]
        assert [f["id"] for f in full["plan"]["features"]] == ["body", "hidden"]
        assert full["suppressed"] == ["hidden"]
    reopened = env.client.post(f"{BASE}/versions/{version}/open", json={"fileId": "restored-suppressed", "requestId": str(uuid4())}, headers=env.headers["alice"])
    assert reopened.status_code == 201
    assert reopened.json()["record"]["suppressed"] == ["hidden"]


def test_project_archived_during_import_rolls_back_version_and_new_engineering_file(env, monkeypatch):
    target = project(env); created=[]
    original = env.designs.import_step
    def archive_after_import(*args, **kwargs):
        value = original(*args, **kwargs); created.append(value["id"])
        # Simulate an independent project mutation between initial validation
        # and the final version transaction, without touching existing assets.
        with env.pdm._connection:
            env.pdm._connection.execute("UPDATE pdm_projects SET status='archived' WHERE id=?", (target.id,))
        return value
    monkeypatch.setattr(env.designs, "import_step", archive_after_import)
    response = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"])
    assert response.status_code == 409
    assert env.pdm.list_documents(target.id) == []
    assert created and all(not (env.designs._owner(env.actors["alice"].id) / ident).exists() for ident in created)


def test_open_retry_recovers_partial_copy_without_source_or_version_duplication(env):
    target = project(env)
    saved = env.client.post(BASE + "/versions", json=payload(env, target.id), headers=env.headers["alice"]).json()
    version = saved["version"]["id"]
    request_id = "recover-interrupted-open"
    record_id = "feature_" + hashlib.sha256(f"{env.actors['alice'].id}\0{request_id}".encode()).hexdigest()[:32]
    interrupted = env.features.root / record_id / "pdm-r1"; interrupted.mkdir(parents=True)
    (interrupted / "model.step").write_bytes(b"interrupted incomplete file")
    response = env.client.post(f"{BASE}/versions/{version}/open", json={"fileId": "recovered", "requestId": request_id}, headers=env.headers["alice"])
    assert response.status_code == 201, response.text
    assert response.json()["record"]["id"] == record_id
    assert get_cadquery().importers.importStep(str(interrupted / "model.step")).val().isValid()
    with env.features._db() as db:
        assert db.execute("SELECT COUNT(*) FROM manual_features WHERE id=?", (record_id,)).fetchone()[0] == 1


def test_real_threaded_standard_part_can_save_to_engineering_pdm_and_reopen_editable(env):
    from app.cad_standard_parts import CATALOG
    target = project(env)
    value = {"version": "cad-plan-v1", "units": "mm", "parameters": {"length": {"value": 20}},
             "features": [{"id": "screw", "op": "standard_part", "catalogId": "socket-m6-20", "dimensions": {**CATALOG["socket-m6-20"]["dimensions"], "length": "length"}}], "result": "screw"}
    built = env.features.commit(env.actors["alice"].id, {"name": "螺钉真实归档", "fileId": "real-screw", "plan": value, "requestId": str(uuid4()), "changeNote": "真实螺纹入库验收"})
    saved = env.client.post(BASE + "/versions", json=payload(env, target.id, featureId=built["id"]), headers=env.headers["alice"])
    assert saved.status_code == 201, saved.text
    response = env.client.post(f"{BASE}/versions/{saved.json()['version']['id']}/open", json={"fileId": "screw-copy", "requestId": str(uuid4())}, headers=env.headers["alice"])
    assert response.status_code == 201, response.text
    record = response.json()["record"]
    changed = copy.deepcopy(record["plan"]); changed["parameters"]["length"]["value"] = 22
    updated = env.features.commit(env.actors["alice"].id, {"name": "螺钉可编辑副本", "fileId": record["fileId"], "plan": changed,
        "designId": record["id"], "expectedRevision": 1, "requestId": str(uuid4()), "changeNote": "螺钉长度增加 2 mm"})
    assert updated["inspection"]["stepReadback"]["valid"]
    assert updated["inspection"]["stepReadback"]["solidCount"] == 1
    assert updated["inspection"]["stepReadback"]["bbox"]["size"][2] == pytest.approx(28, abs=1e-4)
