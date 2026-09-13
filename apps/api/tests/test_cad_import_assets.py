"""Real uploaded bodies, authenticated editing, STEP round trips and PDM recovery."""
import copy
import hashlib
import io
import json
import math
import shutil
import struct
import threading
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_design_workspace_api import create_cad_design_workspace_router
from app.cad_feature_workspace_api import create_cad_feature_workspace_router
from app.cad_import_assets import CadImportAssets, build_import_step
from app.cad_plan import PlanValidationError, validate_plan
from app.geometry import get_cadquery
from app.platform import AuthService, PDMRepository, Role

IMPORT = "/api/cad/designs/imports"
FEATURE = "/api/cad/features"
PDM = "/api/cad/designs/pdm"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("actual-body-import")
    auth = AuthService(root / "platform.sqlite3", token_secret="synthetic-import-asset-secret" * 2)
    pdm = PDMRepository(root / "platform.sqlite3")
    actors = {name: auth.create_user(name + "@example.invalid", "Synthetic!Test2026", name, roles=[role])
              for name, role in (("alice", Role.DESIGNER), ("bob", Role.DESIGNER), ("viewer", Role.VIEWER))}
    headers = {name: {"Authorization": "Bearer " + auth.issue_token(actor).token} for name, actor in actors.items()}
    services = SimpleNamespace(auth=auth, pdm=pdm)
    design_router = create_cad_design_workspace_router(services, root=root / "engineering")
    feature_router = create_cad_feature_workspace_router(services, design_store=design_router.design_store)
    app = FastAPI(); app.include_router(design_router); app.include_router(feature_router)
    cq = get_cadquery(); original = cq.Workplane("XY").box(20, 16, 10, centered=False)
    path = root / "unparametric.step"; cq.exporters.export(original, str(path))
    with TestClient(app) as client:
        value = SimpleNamespace(root=root, auth=auth, pdm=pdm, actors=actors, headers=headers, client=client,
                                features=feature_router.feature_store, assets=CadImportAssets(root / "cad-feature-workspace" / "imports"), source=path.read_bytes())
        response = upload(value)
        assert response.status_code == 201, response.text
        value.asset = response.json()
        yield value
    pdm.close(); auth.close()


def upload(env, payload=None, filename="no-history.step", actor="alice", request_id=None, units="mm"):
    return env.client.post(IMPORT, files={"file": (filename, payload if payload is not None else env.source, "application/octet-stream")},
                           data={"name": "原始无历史实体", "units": units, "requestId": request_id or str(uuid4())}, headers=env.headers.get(actor, {}))


def plan_for(asset):
    return {"version": "cad-plan-v1", "units": "mm", "parameters": {},
            "features": [{"id": "source", **asset["feature"]}], "result": "source"}


def post(env, path, value, actor="alice", status=200):
    response = env.client.post(FEATURE + path, json=value, headers=env.headers[actor])
    assert response.status_code == status, response.text
    return response.json()


def append(plan, feature):
    value = copy.deepcopy(plan); value["features"].append(feature); value["result"] = feature["id"]; return value


def commit(env, plan, **extra):
    return post(env, "/commit", {"plan": plan, "name": "导入后直接编辑", "fileId": "imported-file", "requestId": str(uuid4()), "changeNote": "真实导入编辑验收", **extra})


def test_step_upload_is_owned_immutable_idempotent_and_not_a_fabricated_history(env):
    asset = env.asset
    assert asset["inspection"]["valid"] and asset["inspection"]["solidCount"] == 1
    assert asset["inspection"]["volume"] == pytest.approx(3200)
    assert asset["history"] == "imported_body" and asset["originalFormat"] == "step"
    assert "path" not in json.dumps(asset) and "owner" not in asset
    assert env.client.get(IMPORT + "/" + asset["id"]).status_code == 401
    assert env.client.get(IMPORT + "/" + asset["id"], headers=env.headers["bob"]).status_code == 404
    assert upload(env, actor="viewer").status_code == 403
    assert upload(env, actor="unknown").status_code == 401
    ident = str(uuid4()); a = upload(env, request_id=ident); b = upload(env, request_id=ident)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert upload(env, payload=env.source + b"\n", request_id=ident).status_code == 409
    assert upload(env, filename="model.glb").status_code == 422
    assert upload(env, payload=b"fake step").status_code == 422
    with pytest.raises(PlanValidationError) as error: build_import_step(get_cadquery(), asset["feature"])
    assert error.value.code == "untrusted_import_asset"
    with pytest.raises(PlanValidationError) as error: env.assets.archive(env.actors["alice"].id, plan_for(asset), max_bytes=10)
    assert error.value.code == "resource_limit"
    wrong = plan_for(asset); wrong["features"][0]["path"] = "/tmp/arbitrary.step"
    with pytest.raises(PlanValidationError): validate_plan(wrong)
    assert env.client.post(FEATURE + "/preview", json={"plan": plan_for(asset)}, headers=env.headers["bob"]).status_code == 404


def test_real_imported_face_move_fillet_hole_save_reopen_and_failed_revision_is_atomic(env):
    plan = plan_for(env.asset); initial = commit(env, plan)
    preview = post(env, "/preview", {"plan": initial["plan"], "designId": initial["id"], "expectedRevision": 1})
    assert len(preview["faces"]) == 6 and len(preview["edges"]) == 12
    top = next(f for f in preview["faces"] if f["normal"][2] > .9)
    plan = append(initial["plan"], {"id": "raised", "op": "move_face", "input": "source", "faces": [top["selector"]], "distance": 2})
    raised = commit(env, plan, designId=initial["id"], expectedRevision=1)
    assert raised["inspection"]["bbox"]["size"] == pytest.approx([20, 16, 12])
    preview = post(env, "/preview", {"plan": raised["plan"], "designId": raised["id"], "expectedRevision": 2})
    edge = next(e for e in preview["edges"] if all(abs(z - 12) < 1e-5 for z in e["points"][2::3]))
    plan = append(raised["plan"], {"id": "rounded", "op": "fillet", "input": "raised", "edges": [edge["selector"]], "radius": .5})
    plan = append(plan, {"id": "drill", "op": "cylinder", "radius": 2, "height": 20, "origin": [10, 8, -2]})
    plan = append(plan, {"id": "bored", "op": "cut", "inputs": ["rounded", "drill"]})
    final = commit(env, plan, designId=raised["id"], expectedRevision=2)
    assert final["revision"] == 3 and final["inspection"]["stepReadback"]["valid"]
    expected = 20*16*12 - 12*math.pi*4 - math.dist(edge["points"][:3], edge["points"][-3:])*(1-math.pi/4)*.5**2
    path, _ = env.features.artifact(env.actors["alice"].id, final["id"], 3, "step")
    shape = get_cadquery().importers.importStep(str(path)).val()
    assert shape.isValid() and shape.Volume() == pytest.approx(expected, rel=1e-6)
    assert len(shape.Solids()) == 1
    opened = env.client.get(FEATURE + "/" + final["id"] + "?revision=3", headers=env.headers["alice"])
    assert opened.status_code == 200 and opened.json()["plan"]["features"][0]["op"] == "import_step"
    invalid = copy.deepcopy(final["plan"]); invalid["features"][2]["radius"] = 1000
    post(env, "/commit", {"plan": invalid, "name": final["name"], "fileId": final["fileId"], "designId": final["id"], "expectedRevision": 3, "requestId": str(uuid4()), "changeNote": "拒绝不可能圆角"}, status=422)
    assert env.features.get(env.actors["alice"].id, final["id"])["revision"] == 3
    env.edited = final


def stl_box(binary=True, close=True):
    points = [(0,0,0),(2,0,0),(2,1.6,0),(0,1.6,0),(0,0,1),(2,0,1),(2,1.6,1),(0,1.6,1)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    if not close: faces = faces[:-1]
    if binary:
        return b"Synthetic closed box".ljust(80, b"\0") + struct.pack("<I",len(faces)) + b"".join(struct.pack("<12fH",0,0,0,*points[a],*points[b],*points[c],0) for a,b,c in faces)
    rows = ["solid box"]
    for face in faces:
        rows += ["facet normal 0 0 0", "outer loop", *["vertex " + " ".join(map(str,points[i])) for i in face], "endloop", "endfacet"]
    return ("\n".join([*rows, "endsolid box"]) + "\n").encode()


@pytest.mark.parametrize("binary", [True, False])
def test_real_uploaded_stl_units_closed_solid_and_step_editability(env, binary):
    result = upload(env, stl_box(binary), filename="body.stl", units="cm")
    assert result.status_code == 201, result.text
    asset = result.json(); assert asset["inspection"]["facetedSource"] and asset["originalUnits"] == "cm"
    path = env.assets.path(env.actors["alice"].id, asset["id"], asset["sha256"])
    shape = get_cadquery().importers.importStep(str(path)).val()
    assert shape.isValid() and shape.Volume() == pytest.approx(3200, rel=1e-6)
    preview = post(env, "/preview", {"plan": plan_for(asset)})
    assert preview["faces"] and preview["edges"]
    top = next(face for face in preview["faces"] if face["normal"][2] > .9)
    moved = append(plan_for(asset), {"id": "move", "op": "move_face", "input": "source", "faces": [top["selector"]], "distance": 1})
    built = commit(env, moved)
    assert built["inspection"]["bbox"]["size"] == pytest.approx([20,16,11], rel=1e-6)


def test_invalid_open_mesh_and_changed_or_missing_assets_cannot_build(env):
    response = upload(env, stl_box(close=False), filename="open.stl")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "open_mesh"
    asset = upload(env).json(); path = env.assets.path(env.actors["alice"].id, asset["id"], asset["sha256"])
    original = path.read_bytes()
    path.write_bytes(original + b"tampered")
    assert env.client.get(IMPORT + "/" + asset["id"], headers=env.headers["alice"]).status_code == 422
    assert env.client.post(FEATURE + "/preview", json={"plan": plan_for(asset)}, headers=env.headers["alice"]).status_code == 422
    path.unlink()
    assert env.client.post(FEATURE + "/preview", json={"plan": plan_for(asset)}, headers=env.headers["alice"]).status_code == 404
    wrong = plan_for(env.asset); wrong["features"][0]["sha256"] = "a"*64
    assert env.client.post(FEATURE + "/preview", json={"plan":wrong}, headers=env.headers["alice"]).status_code == 422


def test_pdm_archives_import_dependencies_and_shared_member_reopens_independent_editable_body(env):
    preview = post(env,"/preview",{"plan":plan_for(env.asset)})
    source_plan = append(plan_for(env.asset),{"id":"round","op":"fillet","input":"source","edges":[preview["edges"][0]["selector"]],"radius":1})
    original = commit(env, source_plan)
    project = env.pdm.create_project("导入实体归档", env.actors["alice"].id)
    saved = env.client.post(PDM + "/versions", json={"projectId":project.id,"name":"真实导入体","featureId":original["id"],"revision":1,"requestId":str(uuid4()),"expectedCurrentRevision":0},headers=env.headers["alice"])
    assert saved.status_code == 201,saved.text
    version = saved.json()["version"]["id"]
    with zipfile.ZipFile(io.BytesIO(env.pdm.get_version_content(version))) as archive:
        raw = archive.read("assets/" + env.asset["id"] + ".step")
        assert hashlib.sha256(raw).hexdigest() == env.asset["sha256"]
        assert json.loads(archive.read("assets/" + env.asset["id"] + ".json"))["history"] == "imported_body"
    shutil.rmtree(env.assets.path(env.actors["alice"].id,env.asset["id"],env.asset["sha256"]).parent)
    env.pdm.update_project_metadata(project.id,{"members":{env.actors["bob"].id:"read"}},actor_id=env.actors["alice"].id)
    for actor in ("alice","bob"):
        opened=env.client.post(PDM+f"/versions/{version}/open",json={"fileId":actor+"-restored-import","requestId":str(uuid4())},headers=env.headers[actor])
        assert opened.status_code==201,opened.text
        record=opened.json()["record"]
        assert env.assets.path(env.actors[actor].id,env.asset["id"],env.asset["sha256"]).read_bytes()==raw
        preview=post(env,"/preview",{"plan":record["plan"],"designId":record["id"],"expectedRevision":1},actor=actor)
        assert preview["faces"] and preview["edges"]
        plan=copy.deepcopy(record["plan"]);plan["features"][1]["radius"] = .5
        rebuilt=post(env,"/commit",{"plan":plan,"designId":record["id"],"expectedRevision":1,"requestId":str(uuid4()),"fileId":record["fileId"],"name":"归档实体继续编辑","changeNote":"恢复源实体后真实圆角"},actor=actor)
        assert rebuilt["revision"]==2 and rebuilt["inspection"]["stepReadback"]["valid"]
        foreign = "bob" if actor == "alice" else "alice"
        assert env.client.get(FEATURE+"/"+record["id"],headers=env.headers[foreign]).status_code==404


def test_cancelled_import_never_publishes_asset(env):
    cancelled=threading.Event();cancelled.set();request_id=str(uuid4())
    with pytest.raises(PlanValidationError) as error:
        env.assets.upload(env.actors["alice"].id,"cancel.step",env.source,"取消导入","mm",request_id,cancelled)
    assert error.value.code=="user_cancelled"
    asset_id="asset_"+hashlib.sha256(f"{env.actors['alice'].id}\0{request_id}".encode()).hexdigest()[:32]
    assert not (env.assets._owner(env.actors["alice"].id)/asset_id).exists()


def test_step_file_declared_inch_units_normalize_to_actual_millimetres(env):
    cq=get_cadquery();path=env.root/"inch-source.step"
    cq.exporters.export(cq.Workplane("XY").box(1,2,.5,centered=False),str(path),unit="INCH",outputUnit="INCH")
    result=upload(env,path.read_bytes(),filename="inch-source.step")
    assert result.status_code==201,result.text
    assert result.json()["inspection"]["size"]==pytest.approx([25.4,50.8,12.7],rel=1e-6)
    assert result.json()["inspection"]["volume"]==pytest.approx(25.4**3,rel=1e-6)


def test_missing_pdm_source_dependency_rejects_save_and_creates_no_document(env):
    asset=upload(env).json();record=commit(env,plan_for(asset))
    path=env.assets.path(env.actors["alice"].id,asset["id"],asset["sha256"])
    path.write_bytes(path.read_bytes()+b"changed")
    project=env.pdm.create_project("缺失依赖拒绝归档",env.actors["alice"].id)
    result=env.client.post(PDM+"/versions",json={"projectId":project.id,"name":"不得成功","featureId":record["id"],"revision":1,"requestId":str(uuid4()),"expectedCurrentRevision":0},headers=env.headers["alice"])
    assert result.status_code==422 and "校验" in result.text
    assert env.pdm.list_documents(project.id)==[]
