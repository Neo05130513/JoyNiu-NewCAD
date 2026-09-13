from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_executor import build_plan_shape, execute_cad_plan, preview_cad_plan
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureConflict, FeatureNotFound, FeatureWorkspaceError
from app.cad_plan import PlanValidationError, cad_plan_schema, validate_plan
from app.cad_topology import export_topology_preview, resolve_topology_selection, topology_identity
from app.geometry import get_cadquery
from app.cad_feature_workspace_api import create_cad_feature_workspace_router
from app.platform import AuthService

pytestmark = pytest.mark.skipif(get_cadquery() is None, reason="Requires real CadQuery/OCCT")


def box_plan():
    return validate_plan({"version": "cad-plan-v1", "parameters": {"width": {"value": 20}},
                          "features": [{"id": "block", "op": "box", "size": ["width", 16, 10]}], "result": "block"})


def with_feature(plan, feature):
    return {**deepcopy(plan), "features": [*deepcopy(plan["features"]), feature], "result": feature["id"]}


@pytest.fixture
def box_geometry(tmp_path):
    plan = box_plan()
    shape, _, _ = build_plan_shape(plan)
    payload, _ = export_topology_preview(plan, shape, tmp_path)
    return plan, shape, payload


def test_preview_mesh_triangles_map_to_real_faces_and_edges_follow_actual_curves(box_geometry):
    plan, shape, payload = box_geometry
    mesh = payload["mesh"]
    assert len(mesh["indices"]) == 3 * len(mesh["faceIds"])
    assert len(mesh["positions"]) == len(mesh["normals"])
    assert len(mesh["positions"]) // 3 < len(mesh["indices"])
    for vertex in set(mesh["indices"]):
        normal = mesh["normals"][3*vertex:3*vertex+3]
        assert sum(v*v for v in normal) == pytest.approx(1, abs=1e-9)
    assert len(payload["faces"]) == 6 and len(payload["edges"]) == 12
    for triangle, face_id in zip(zip(*[iter(mesh["indices"])] * 3), mesh["faceIds"]):
        index = int(face_id.split(":")[1])
        face = shape.val().Faces()[index]
        for vertex in triangle:
            point = mesh["positions"][3*vertex:3*vertex+3]
            assert face.distance(get_cadquery().Vertex.makeVertex(*point)) < 1e-6
    for edge in payload["edges"]:
        actual = shape.val().Edges()[edge["selector"]["index"]]
        for point in zip(*[iter(edge["points"])]*3):
            assert actual.distance(get_cadquery().Vertex.makeVertex(*point)) < 1e-6
    assert payload["bounds"] == {"min": [0, 0, 0], "max": [20, 16, 10]}
    assert all(face["planar"] and len(face["normal"]) == 3 for face in payload["faces"])
    # Triangulation/cache activity must not change an entity's version.
    assert topology_identity(plan, "block", shape)["version"] == payload["geometryVersion"]


@pytest.mark.parametrize("op", ["fillet", "chamfer", "shell"])
def test_exact_picked_edge_or_face_builds_and_survives_step_readback(box_geometry, tmp_path, op):
    cq = get_cadquery()
    plan, shape, payload = box_geometry
    if op == "shell":
        picked = next(face for face in payload["faces"] if face["normal"][2] > .9)
        feature = {"id": "edited", "op": op, "input": "block", "faces": [picked["selector"]], "thickness": -1}
        expected = 3200 - 18 * 14 * 9
    else:
        picked = payload["edges"][0]
        feature = {"id": "edited", "op": op, "input": "block", "edges": [picked["selector"]], "radius" if op == "fillet" else "length": 1}
        length = shape.val().Edges()[picked["selector"]["index"]].Length()
        expected = 3200 - length * ((1 - math.pi / 4) if op == "fillet" else .5)
    edited, _, _ = build_plan_shape(with_feature(plan, feature))
    assert edited.val().isValid()
    assert edited.val().Volume() == pytest.approx(expected, rel=1e-7)
    path = tmp_path / f"{op}.step"
    cq.exporters.export(edited.val(), str(path))
    readback = cq.importers.importStep(str(path))
    assert readback.val().isValid() and len(readback.val().Solids()) == 1
    assert readback.val().Volume() == pytest.approx(expected, rel=1e-7)


def test_stale_signature_changed_parameter_and_ambiguous_geometry_are_rejected(box_geometry):
    cq = get_cadquery()
    plan, shape, payload = box_geometry
    selector = payload["edges"][0]["selector"]
    changed = with_feature(plan, {"id": "round", "op": "fillet", "input": "block", "edges": [selector], "radius": 1})
    changed["parameters"]["width"]["value"] = 22
    before = deepcopy(changed)
    with pytest.raises(PlanValidationError) as error: build_plan_shape(changed)
    assert error.value.code == "stale_topology" and changed == before
    wrong = {**selector, "signature": "a" * 64}
    with pytest.raises(PlanValidationError) as error: resolve_topology_selection(plan, "block", shape, [wrong], "edge")
    assert error.value.code == "stale_topology"
    duplicate_shape = cq.Workplane("XY").newObject([cq.Compound.makeCompound([shape.val(), shape.val().copy()])])
    identity = topology_identity(plan, "block", duplicate_shape)
    duplicate_selector = {**selector, "geometryVersion": identity["version"], "signature": identity["edgeSignatures"][0]}
    with pytest.raises(PlanValidationError) as error: resolve_topology_selection(plan, "block", duplicate_shape, [duplicate_selector], "edge")
    assert error.value.code == "ambiguous_topology"


def test_custom_plane_attaches_to_rotated_real_face_and_rejects_stale_or_detached_frame(tmp_path):
    plan = with_feature(box_plan(), {"id": "tilted", "op": "rotate", "input": "block", "axisStart": [0, 0, 0], "axisEnd": [0, 1, 0], "angle": 30})
    shape, _, _ = build_plan_shape(plan)
    payload, _ = export_topology_preview(plan, shape, tmp_path)
    face = next(face for face in payload["faces"] if abs(face["normal"][0]) > .4 and abs(face["normal"][2]) > .4)
    feature = {"id": "boss", "op": "profile_extrude", "plane": "custom", "frame": {"origin": face["origin"], "normal": face["normal"], "xDir": face["xAxis"]},
               "planeSource": face["selector"], "start": [0, 0], "segments": [{"type": "line", "to": [2, 0]}, {"type": "line", "to": [2, 3]}, {"type": "line", "to": [0, 3]}], "distance": 4}
    complete = with_feature(plan, feature)
    result, _, _ = build_plan_shape(complete)
    assert result.val().isValid() and result.val().Volume() == pytest.approx(24)
    cq = get_cadquery(); path = tmp_path / "attached-profile.step"
    cq.exporters.export(result.val(), str(path))
    assert cq.importers.importStep(str(path)).val().Volume() == pytest.approx(24)
    shifted = deepcopy(complete)
    shifted["features"][-1]["frame"]["origin"] = [x + 2*n for x, n in zip(face["origin"], face["normal"])]
    with pytest.raises(PlanValidationError, match="不在拾取"): build_plan_shape(shifted)
    stale = deepcopy(complete); stale["features"][1]["angle"] = 35
    with pytest.raises(PlanValidationError) as error: build_plan_shape(stale)
    assert error.value.code == "stale_topology"
    invalid = deepcopy(complete); invalid["features"][-1]["frame"]["xDir"] = face["normal"]
    with pytest.raises(PlanValidationError, match="垂直"): build_plan_shape(invalid)


@pytest.mark.parametrize("keep", [False, True])
def test_mirror_uses_occt_and_explicit_custom_frame(keep, tmp_path):
    plan = box_plan(); plan["features"][0]["origin"] = [2, 3, 4]
    feature = {"id": "reflected", "op": "mirror", "input": "block", "plane": "custom", "frame": {"origin": [0, 0, 0], "normal": [1, 0, 0], "xDir": [0, 1, 0]}, "keepOriginal": keep}
    shape, _, _ = build_plan_shape(with_feature(plan, feature))
    assert len(shape.val().Solids()) == (2 if keep else 1)
    assert shape.val().Volume() == pytest.approx(6400 if keep else 3200)
    bounds = shape.val().BoundingBox()
    assert bounds.xmin == pytest.approx(-22)
    assert bounds.xmax == pytest.approx(22 if keep else -2)
    cq = get_cadquery(); path = tmp_path / "mirror.step"
    cq.exporters.export(shape.val(), str(path))
    restored = cq.importers.importStep(str(path)).val()
    assert restored.isValid() and len(restored.Solids()) == (2 if keep else 1)
    assert restored.Volume() == pytest.approx(shape.val().Volume())
    standard = with_feature(plan, {"id": "reflected", "op": "mirror", "input": "block", "plane": "YZ", "keepOriginal": keep})
    standard_shape, _, _ = build_plan_shape(standard)
    assert standard_shape.val().Volume() == pytest.approx(shape.val().Volume())


def constrained_profile():
    return {"id": "plate", "op": "profile_extrude", "plane": "XY", "distance": 3, "start": [0, 0],
            "segments": [{"type": "line", "to": ["width", 0]}, {"type": "line", "to": ["width", 5]}, {"type": "line", "to": [0, 5]}, {"type": "line", "to": [0, 0]}],
            "sketchConstraints": [{"id": "c1", "type": "horizontal", "edge": 0}, {"id": "c2", "type": "vertical", "edge": 1},
                                  {"id": "c3", "type": "length", "edge": 0, "value": "width"}, {"id": "c4", "type": "coincident", "points": ["start", "3:to"]},
                                  {"id": "c5", "type": "fixed", "points": ["start"], "position": [0, 0]}]}


def test_all_sketch_constraints_are_checked_after_expression_resolution():
    plan = {**box_plan(), "features": [constrained_profile()], "result": "plate"}
    shape, _, _ = build_plan_shape(plan)
    assert shape.val().Volume() == pytest.approx(300)
    for item in [{"id": "c6", "type": "vertical", "edge": 0}, {"id": "c6", "type": "fixed", "points": ["1:to"], "position": [0, 0]}]:
        wrong = deepcopy(plan); wrong["features"][0]["sketchConstraints"].append(item)
        with pytest.raises(PlanValidationError) as error: build_plan_shape(wrong)
        assert error.value.code == "invalid_sketch_constraint"
    duplicate = deepcopy(plan); duplicate["features"][0]["sketchConstraints"].append({"id": "other", "type": "horizontal", "edge": 0})
    with pytest.raises(PlanValidationError, match="重复"): validate_plan(duplicate)
    invalid = deepcopy(plan); invalid["features"][0]["sketchConstraints"][0]["type"] = []
    with pytest.raises(PlanValidationError): validate_plan(invalid)
    schema = cad_plan_schema()
    assert any(item["properties"]["op"]["const"] == "mirror" for item in schema["properties"]["features"]["items"]["oneOf"])


def test_isolated_prefix_preview_ignores_unrelated_unresolved_tail_and_never_saves_versions(tmp_path):
    store = CadFeatureWorkspace(tmp_path)
    plan = with_feature(box_plan(), {"id": "later", "op": "cylinder", "radius": 2, "height": "unknown"})
    plan["parameters"]["unknown"] = {"value": None}
    # IDs and labels that happen to match parameter names are not dimensions.
    plan["parameters"]["block"] = {"value": None}
    preview = store.preview("alice", {"plan": plan, "featureId": "block"})
    assert preview["sourceFeatureId"] == "block"
    assert preview["execution"]["isolatedProcess"] is True
    assert preview["inspection"]["volumeMm3"] == pytest.approx(3200)
    assert preview["scope"] == "transaction_preview" and preview["productionReady"] is False
    assert str(tmp_path) not in json.dumps(preview)
    assert store.list("alice") == []
    path, _ = store.preview_artifact("alice", preview["previewId"], "glb")
    assert path.read_bytes()[:4] == b"glTF"
    with pytest.raises(FeatureNotFound): store.preview_artifact("bob", preview["previewId"], "glb")
    with pytest.raises(FeatureWorkspaceError, match="unknown"): store.preview("alice", {"plan": plan})
    assert path.is_file() and store.list("alice") == []


def test_commit_is_atomic_idempotent_and_failure_preserves_original_artifacts(tmp_path):
    store = CadFeatureWorkspace(tmp_path)
    data = {"name": "事务盒体", "plan": box_plan(), "suppressed": [], "changeNote": "新建实体", "requestId": "commit-first-001"}
    built = store.commit("alice", data)
    assert built["status"] == "built" and built["revision"] == 1
    assert built["inspection"]["stepReadback"]["valid"]
    assert store.commit("alice", data)["id"] == built["id"]
    assert len(store.versions("alice", built["id"])) == 1
    before = Path(built["artifacts"]["step"]["path"]).read_bytes()
    invalid = {**data, "requestId": "commit-next-002", "designId": built["id"], "expectedRevision": 1, "plan": deepcopy(data["plan"])}
    invalid["plan"]["parameters"]["width"]["value"] = -1
    with pytest.raises(FeatureWorkspaceError): store.commit("alice", invalid)
    assert store.get("alice", built["id"]) == built
    assert Path(built["artifacts"]["step"]["path"]).read_bytes() == before
    assert len(list((tmp_path / built["id"]).glob("commit-*"))) == 1
    with pytest.raises(FeatureConflict): store.commit("alice", {**data, "name": "不相同的请求"})
    with pytest.raises(FeatureNotFound): store.commit("bob", {**data, "requestId": "foreign-commit-001", "designId": built["id"], "expectedRevision": 1})


def test_preview_cancellation_reaps_worker_and_releases_concurrency_slot(tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(.15, cancel.set)
    started = time.monotonic(); timer.start()
    try: result = preview_cad_plan(box_plan(), tmp_path / "cancelled", cancel_event=cancel)
    finally: timer.cancel()
    assert result["status"] == "failed" and result["errors"][0]["code"] == "user_cancelled"
    assert result["artifacts"] == {} and time.monotonic() - started < 5
    store = CadFeatureWorkspace(tmp_path / "store")
    with pytest.raises(FeatureWorkspaceError): store.preview("alice", {"plan": box_plan()}, cancel)
    assert store.preview("alice", {"plan": box_plan()})["valid"] is True


def test_topology_resource_limits_fail_without_partial_glb(box_geometry, tmp_path, monkeypatch):
    from app import cad_topology
    plan, shape, _ = box_geometry
    monkeypatch.setattr(cad_topology, "MAX_PICK_TRIANGLES", 1)
    output = tmp_path / "limited"; output.mkdir()
    with pytest.raises(PlanValidationError) as error: export_topology_preview(plan, shape, output)
    assert error.value.code == "resource_limit"
    assert list(output.iterdir()) == []


def test_preview_and_commit_routes_enforce_auth_owner_revision_and_return_private_artifacts(tmp_path):
    auth = AuthService(tmp_path / "accounts.sqlite3", token_secret="topology-test-" * 4)
    users = [auth.create_user(name + "@test.example", "a-long-test-password", name, roles=[role]) for name, role in [("alice", "designer"), ("bob", "designer"), ("viewer", "viewer")]]
    headers = [{"Authorization": "Bearer " + auth.issue_token(user).token} for user in users]
    app = FastAPI(); app.include_router(create_cad_feature_workspace_router(SimpleNamespace(auth=auth), root=tmp_path / "features"))
    base = "/api/cad/features"
    data = {"plan": box_plan(), "name": "直接编辑测试", "changeNote": "创建原始盒体"}
    try:
        with TestClient(app) as client:
            for route in ["preview", "commit"]:
                assert client.post(f"{base}/{route}", json=data).status_code == 401
                assert client.post(f"{base}/{route}", headers=headers[2], json=data).status_code == 403
            saved = client.post(base, headers=headers[0], json=data).json()
            request = {**data, "designId": saved["id"], "expectedRevision": 1}
            assert client.post(f"{base}/preview", headers=headers[1], json=request).status_code == 404
            assert client.post(f"{base}/preview", headers=headers[0], json={**request, "expectedRevision": 0}).status_code == 409
            preview = client.post(f"{base}/preview", headers=headers[0], json=request)
            assert preview.status_code == 200, preview.text
            assert preview.headers["cache-control"] == "private, no-store"
            assert str(tmp_path) not in preview.text
            url = preview.json()["artifacts"]["glb"]["url"]
            assert client.get(url, headers=headers[1]).status_code == 404
            assert client.get(url, headers=headers[0]).content[:4] == b"glTF"
            request["requestId"] = "api-commit-0001"
            committed = client.post(f"{base}/commit", headers=headers[0], json=request)
            assert committed.status_code == 200, committed.text
            record = committed.json()
            assert record["status"] == "built" and record["revision"] == 2
            assert record["sharedDesign"] is None and record["productionReady"] is False
            assert client.get(record["artifacts"]["glb"]["url"], headers=headers[0]).content[:4] == b"glTF"
            assert client.post(f"{base}/commit", headers=headers[0], json=request).json()["revision"] == 2
            assert client.post(f"{base}/commit", headers=headers[0], json={**request, "requestId": "api-commit-0002"}).status_code == 409
            stale = with_feature(box_plan(), {"id": "round", "op": "fillet", "input": "block", "radius": 1, "edges": [preview.json()["edges"][0]["selector"]]})
            stale["parameters"]["width"]["value"] = 25
            refused = client.post(f"{base}/preview", headers=headers[0], json={"plan": stale})
            assert refused.status_code == 409 and refused.json()["detail"]["code"] == "stale_topology"
            assert client.get(f"{base}/{saved['id']}", headers=headers[0]).json()["revision"] == 2
    finally:
        auth.close()


def test_commit_conflict_during_worker_never_publishes_its_artifacts(tmp_path):
    store = CadFeatureWorkspace(tmp_path)
    data = {"plan": box_plan(), "name": "并发测试", "changeNote": "原始草稿"}
    original = store.save("alice", data)
    def concurrent_executor(plan, destination, **kwargs):
        result = execute_cad_plan(plan, destination, **kwargs)
        store.save("alice", {**data, "expectedRevision": 1, "changeNote": "其他窗口的有效保存"}, original["id"])
        return result
    store.executor = concurrent_executor
    with pytest.raises(FeatureConflict):
        store.commit("alice", {**data, "designId": original["id"], "expectedRevision": 1, "requestId": "concurrent-edit-001"})
    latest = store.get("alice", original["id"])
    assert latest["revision"] == 2 and latest["changeNote"] == "其他窗口的有效保存"
    assert latest["status"] == "draft" and latest["artifacts"] == {}
    assert list((tmp_path / original["id"]).glob("commit-*")) == []
