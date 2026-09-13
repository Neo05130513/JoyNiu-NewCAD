import copy
import hashlib
import io
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_community_api import create_cad_community_router
from app.cad_feature_workspace import CadFeatureWorkspace
from app.cad_import_assets import CadImportAssets
from app.geometry import get_cadquery
from app.platform import AuthService, Role

BASE = "/api/cad/community"
SECRET = "PRIVATE_DRAWING_4519"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("community-real")
    auth = AuthService(root / "platform.sqlite3", token_secret="synthetic-community-tests" * 2)
    users = {name: auth.create_user(f"{name}@example.invalid", "Synthetic!Pass2026", name, roles=[role])
             for name, role in (("author", Role.DESIGNER), ("other", Role.DESIGNER), ("viewer", Role.VIEWER))}
    headers = {name: {"Authorization": "Bearer " + auth.issue_token(user).token} for name, user in users.items()}
    features = CadFeatureWorkspace(root / "features")
    router = create_cad_community_router(SimpleNamespace(auth=auth), root=root / "resources", features=features)
    app = FastAPI(); app.include_router(router)
    plan = {"version": "cad-plan-v1", "units": "mm", "parameters": {"width": {"value": 20, "source": {"sourceFileId": SECRET}, "question": SECRET}},
            "features": [{"id": "base", "op": "box", "size": ["width", 16, 10]}], "result": "base", "notes": [SECRET]}
    owner = users["author"].id
    preview = features.preview(owner, {"plan": plan})
    plan["features"].append({"id": "round", "op": "fillet", "input": "base", "radius": 1, "edges": [preview["edges"][0]["selector"]]})
    plan["result"] = "round"
    old = features.commit(owner, {"plan": plan, "name": SECRET, "fileId": SECRET, "requestId": str(uuid4()), "changeNote": SECRET})
    newer = copy.deepcopy(old["plan"]); newer["parameters"]["width"]["value"] = 22
    latest = features.commit(owner, {"plan": newer, "name": SECRET, "fileId": SECRET, "requestId": str(uuid4()),
        "changeNote": SECRET, "designId": old["id"], "expectedRevision": 1})
    with TestClient(app) as client:
        yield SimpleNamespace(root=root, auth=auth, users=users, headers=headers, features=features, store=router.community_store,
                              client=client, old=old, latest=latest)
    auth.close()


def publish(env, **changes):
    data = {"featureId": env.old["id"], "revision": 1, "name": "公开圆角件", "description": "真实可编辑示例",
            "requestId": str(uuid4()), "consent": "public-copy-download", **changes}
    response = env.client.post(BASE, json=data, headers=env.headers["author"])
    assert response.status_code == 201, response.text
    return data, response.json()


def no_private(value, env):
    encoded = json.dumps(value, ensure_ascii=False)
    for private in [SECRET, env.old["id"], env.users["author"].id, str(env.root)]: assert private not in encoded
    assert "sourceDocuments" not in encoded and "originalFilename" not in encoded


def test_public_exact_version_has_real_step_glb_and_no_private_record_fields(env, tmp_path):
    data, resource = publish(env)
    for route in (BASE, f"{BASE}?q=圆角", f"{BASE}/{resource['id']}"):
        response = env.client.get(route)
        assert response.status_code == 200, response.text
        no_private(response.json(), env)
        assert response.headers["cache-control"] == "private, no-store"
    assert env.client.get(BASE + "?q=没有此资源").json()["items"] == []
    assert resource["inspection"]["bbox"]["size"][0] == pytest.approx(20)
    assert resource["canWithdraw"] is True
    assert env.client.get(f"{BASE}/{resource['id']}", headers=env.headers["other"]).json()["canWithdraw"] is False
    step = env.client.get(f"{BASE}/{resource['id']}/artifacts/step")
    assert step.status_code == 200 and step.headers["content-type"].startswith("application/step")
    actual = tmp_path / "published.step"; actual.write_bytes(step.content)
    shape = get_cadquery().importers.importStep(str(actual)).val()
    assert shape.isValid() and len(shape.Solids()) == 1 and shape.BoundingBox().xlen == pytest.approx(20)
    assert SECRET.encode() not in step.content and env.old["id"].encode() not in step.content
    assert env.client.get(f"{BASE}/{resource['id']}/artifacts/glb").content[:4] == b"glTF"
    assert env.client.get(f"{BASE}/{resource['id']}/artifacts/plan").status_code == 404
    assert env.client.get(f"{BASE}/{resource['id']}/artifacts/registry.json").status_code == 404
    assert env.client.post(BASE, json=data, headers=env.headers["author"]).json()["id"] == resource["id"]
    assert env.client.post(BASE, json={**data, "revision": 2}, headers=env.headers["author"]).status_code == 409


def test_cross_account_copy_keeps_bindings_and_source_is_independent(env):
    _, resource = publish(env)
    request = {"fileId": "independent-user-file", "requestId": "independent-copy-request"}
    response = env.client.post(f"{BASE}/{resource['id']}/open", json=request, headers=env.headers["other"])
    assert response.status_code == 201, response.text
    copied = response.json()["record"]; no_private(copied, env)
    assert copied["plan"]["parameters"]["width"] == {"value": 20}
    assert copied["sourceRun"] is None and copied["revision"] == 1 and not copied["productionReady"]
    assert copied["plan"]["features"][-1]["edges"][0]["binding"]  # real server topology binding survived
    plan = copy.deepcopy(copied["plan"]); plan["parameters"]["width"]["value"] = 24
    changed = env.features.commit(env.users["other"].id, {"plan": plan, "name": "用户副本", "fileId": request["fileId"],
        "designId": copied["id"], "expectedRevision": 1, "requestId": str(uuid4()), "changeNote": "改变公开模型参数"})
    assert changed["inspection"]["stepReadback"]["valid"] and changed["inspection"]["bbox"]["size"][0] == pytest.approx(24)
    repeated = env.client.post(f"{BASE}/{resource['id']}/open", json=request, headers=env.headers["other"])
    assert repeated.json()["record"]["revision"] == 1
    assert env.features.get(env.users["author"].id, env.old["id"])["revision"] == 2
    assert env.client.get(f"{BASE}/{resource['id']}").json()["inspection"]["bbox"]["size"][0] == pytest.approx(20)
    assert env.client.post(f"{BASE}/{resource['id']}/open", json={**request, "fileId": "other-file"}, headers=env.headers["other"]).status_code == 409


def test_explicit_consent_owner_permissions_and_exact_built_revision_required(env):
    data = {"featureId": env.old["id"], "revision": 1, "name": "公开", "description": "", "requestId": str(uuid4()), "consent": "public-copy-download"}
    assert env.client.post(BASE, json=data).status_code == 401
    assert env.client.get(BASE + "?mine=true").status_code == 401
    assert env.client.post(BASE, json=data, headers=env.headers["viewer"]).status_code == 403
    assert env.client.post(BASE, json=data, headers=env.headers["other"]).status_code == 404
    for update in ({"consent": False}, {"revision": 0}, {"revision": True}, {"sourceDocuments": [SECRET]}):
        assert env.client.post(BASE, json={**data, **update}, headers=env.headers["author"]).status_code == 422
    assert env.client.post(BASE, json={**data, "revision": 99}, headers=env.headers["author"]).status_code == 404
    draft_plan = {"version": "cad-plan-v1", "parameters": {}, "features": [{"id": "box", "op": "box", "size": [10,10,10]}], "result": "box"}
    draft = env.features.save(env.users["author"].id, {"name": "草稿", "fileId": "draft", "plan": draft_plan, "changeNote": "草稿"})
    assert env.client.post(BASE, json={**data, "featureId": draft["id"], "revision": draft["revision"]}, headers=env.headers["author"]).status_code == 422


def test_withdraw_blocks_new_download_and_open_including_old_request_replay(env):
    _, resource = publish(env)
    path = f"{BASE}/{resource['id']}"
    request = {"fileId": "withdraw-copy", "requestId": "withdraw-copy-request"}
    copied = env.client.post(path + "/open", json=request, headers=env.headers["other"]).json()["record"]
    assert env.client.post(path + "/withdraw", json={}, headers=env.headers["other"]).status_code == 403
    assert env.client.post(path + "/withdraw", json={}, headers=env.headers["author"]).json()["status"] == "withdrawn"
    for route in (path, path + "/artifacts/step", path + "/artifacts/glb"):
        assert env.client.get(route).status_code == 404
    assert env.client.post(path + "/open", json=request, headers=env.headers["other"]).status_code == 404
    assert env.client.post(path + "/open", json={**request, "requestId": str(uuid4())}, headers=env.headers["other"]).status_code == 404
    assert resource["id"] not in [item["id"] for item in env.client.get(BASE).json()["items"]]
    own = env.client.get(BASE + "?mine=true", headers=env.headers["author"]).json()["items"]
    assert next(item for item in own if item["id"] == resource["id"])["status"] == "withdrawn"
    other = env.client.get(BASE + "?mine=true", headers=env.headers["other"]).json()["items"]
    assert resource["id"] not in [item["id"] for item in other]
    path, _ = env.features.artifact(env.users["other"].id, copied["id"], 1, "step")
    assert path.is_file()  # no destructive recall of an already-owned copy


def test_imported_step_dependency_is_renamed_private_metadata_removed_and_real_edit_still_works(env, tmp_path):
    cq = get_cadquery(); source = tmp_path / (SECRET + ".step")
    cq.exporters.export(cq.Workplane("XY").box(20, 16, 10, centered=False), str(source))
    owner = env.users["author"].id
    asset = CadImportAssets(env.features.root / "imports").upload(owner, source.name, source.read_bytes(), SECRET, "mm", str(uuid4()))
    plan = {"version": "cad-plan-v1", "units": "mm", "parameters": {"r": {"value": 1}},
        "features": [{"id": "imported", "op": "import_step", "assetId": asset["id"], "sha256": asset["sha256"]}], "result": "imported"}
    preview = env.features.preview(owner, {"plan": plan})
    plan["features"].append({"id": "round", "op": "fillet", "input": "imported", "radius": "r", "edges": [preview["edges"][0]["selector"]]}); plan["result"] = "round"
    built = env.features.commit(owner, {"plan": plan, "name": SECRET, "fileId": SECRET, "requestId": str(uuid4()), "changeNote": SECRET})
    _, resource = publish(env, featureId=built["id"])
    copied = env.client.post(f"{BASE}/{resource['id']}/open", json={"fileId": "import-copy", "requestId": str(uuid4())}, headers=env.headers["other"])
    assert copied.status_code == 201, copied.text
    record = copied.json()["record"]; no_private(record, env)
    assert asset["id"] not in json.dumps(record) and built["id"] not in json.dumps(record)
    dependency = record["plan"]["features"][0]
    restored = CadImportAssets(env.features.root / "imports").get(env.users["other"].id, dependency["assetId"])
    assert restored["sha256"] == asset["sha256"] and "originalFilename" not in restored
    assert SECRET not in json.dumps(restored)
    updated = copy.deepcopy(record["plan"]); updated["parameters"]["r"]["value"] = .5
    rebuilt = env.features.commit(env.users["other"].id, {"plan": updated, "name": "重新编辑导入件", "fileId": record["fileId"],
        "designId": record["id"], "expectedRevision": 1, "requestId": str(uuid4()), "changeNote": "实际圆角修改"})
    assert rebuilt["inspection"]["stepReadback"]["valid"] and rebuilt["inspection"]["solidCount"] == 1
    assert rebuilt["inspection"]["volumeMm3"] > record["inspection"]["volumeMm3"]


def test_tampered_community_geometry_cannot_be_downloaded_or_create_document(env):
    _, resource = publish(env)
    path = env.store.root / resource["id"] / "model.step"; path.write_bytes(path.read_bytes() + b"tamper")
    before = env.features.list(env.users["other"].id)
    assert env.client.get(f"{BASE}/{resource['id']}/artifacts/step").status_code == 422
    assert env.client.post(f"{BASE}/{resource['id']}/open", json={"fileId": "tamper", "requestId": str(uuid4())}, headers=env.headers["other"]).status_code == 422
    assert env.features.list(env.users["other"].id) == before
