from copy import deepcopy
import math

import pytest

from app.cad_executor import _build_worker_shape, build_plan_shape
from app.cad_history import TopologyHistory, authorize_bindings
from app.cad_plan import PlanValidationError, validate_plan
from app.cad_topology import export_topology_preview
from app.geometry import get_cadquery

pytestmark = pytest.mark.skipif(get_cadquery() is None, reason="Requires real OCCT")


def base_plan():
    return validate_plan({"version": "cad-plan-v1", "parameters": {"width": {"value": 20}},
                          "features": [{"id": "body", "op": "box", "size": ["width", 16, 10]}], "result": "body"})


def picked_profile(face):
    return {"id": "boss", "op": "profile_extrude", "plane": "custom", "planeSource": deepcopy(face["selector"]),
            "frame": {"origin": face["origin"], "normal": face["normal"], "xDir": face["xAxis"]}, "start": [0, 0],
            "segments": [{"type": "line", "to": [2, 0]}, {"type": "line", "to": [2, 3]}, {"type": "line", "to": [0, 3]}], "distance": 4}


def rebuilt(plan, baseline=None):
    request = {"plan": plan, "persistTopology": True, "bindingBasePlan": baseline}
    shape, _, trace = _build_worker_shape(request, None)
    return request["builtPlan"], shape, trace


@pytest.fixture
def box_preview(tmp_path):
    plan = base_plan(); shape, _, _ = build_plan_shape(plan)
    preview, _ = export_topology_preview(plan, shape, tmp_path)
    return plan, preview


@pytest.mark.parametrize("kind,index", [("edge", i) for i in range(12)] + [("face", i) for i in range(6)])
def test_all_box_picks_follow_width_and_legacy_refs_upgrade_without_weakening_raw_pick(box_preview, tmp_path, kind, index):
    cq = get_cadquery(); plan, preview = box_preview
    feature = ({"id": "round", "op": "fillet", "input": "body", "edges": [preview["edges"][index]["selector"]], "radius": 1}
               if kind == "edge" else picked_profile(preview["faces"][index]))
    original = {**deepcopy(plan), "features": [*deepcopy(plan["features"]), feature], "result": feature["id"]}
    bound, before, _ = rebuilt(original)
    picked = bound["features"][-1]["edges"][0] if kind == "edge" else bound["features"][-1]["planeSource"]
    assert len(picked["binding"]["key"]) == 64
    changed = deepcopy(bound); changed["parameters"]["width"]["value"] = 22
    updated, after, _ = rebuilt(changed, bound)
    assert after.val().isValid()
    path = tmp_path / "history.step"; cq.exporters.export(after.val(), str(path))
    readback = cq.importers.importStep(str(path)).val()
    assert readback.isValid() and readback.Volume() == pytest.approx(after.val().Volume(), rel=1e-7)
    if kind == "edge": assert after.val().Volume()-before.val().Volume() == pytest.approx(320 if index < 8 else 320-2*(1-math.pi/4))
    else:
        assert after.val().Volume() == pytest.approx(24)
        assert updated["features"][-1]["planeAttachment"] == bound["features"][-1]["planeAttachment"]
        normal = preview["faces"][index]["normal"]
        expected = 2 if normal[0] > .9 else 0 if normal[0] < -.9 else 1
        assert after.val().Center().x-before.val().Center().x == pytest.approx(expected)
    legacy_changed = deepcopy(original); legacy_changed["parameters"]["width"]["value"] = 22
    _, upgraded, _ = rebuilt(legacy_changed, original)
    assert upgraded.val().Volume() == pytest.approx(after.val().Volume())
    with pytest.raises(PlanValidationError) as exc: build_plan_shape(legacy_changed)
    assert exc.value.code == "stale_topology"


@pytest.mark.parametrize("op", ["cylinder", "profile_extrude", "profile_revolve"])
def test_generated_profile_and_cylinder_edges_keep_names_across_dimension_change(tmp_path, op):
    if op == "cylinder": feature = {"id": "body", "op": op, "radius": "width", "height": 10}
    else:
        feature = {"id": "body", "op": op, "plane": "XY", "start": [3, 0], "segments": [
            {"type": "line", "to": ["width", 0]}, {"type": "line", "to": ["width", 10]}, {"type": "line", "to": [3, 10]}]}
        feature.update({"distance": 5} if op == "profile_extrude" else {"axisStart": [0, 0], "axisEnd": [0, 1]})
    plan = {**base_plan(), "features": [feature]}; plan["parameters"]["width"]["value"] = 6
    output = {}; shape, _, _ = build_plan_shape(plan, history=TopologyHistory(persist=True), history_output=output)
    preview, _ = export_topology_preview(plan, shape, tmp_path)
    candidates = [edge for edge in preview["edges"] if output["history"].entity_key("body", "edge", shape.val().Edges()[edge["selector"]["index"]])]
    assert candidates
    plan["features"].append({"id": "round", "op": "fillet", "input": "body", "radius": .2, "edges": [candidates[0]["selector"]]}); plan["result"] = "round"
    bound, _, _ = rebuilt(plan)
    changed = deepcopy(bound); changed["parameters"]["width"]["value"] = 7
    _, after, _ = rebuilt(changed, bound)
    cq = get_cadquery(); path = tmp_path / "generated-history.step"; cq.exporters.export(after.val(), str(path))
    assert cq.importers.importStep(str(path)).val().Volume() == pytest.approx(after.val().Volume(), rel=1e-7)


def test_boolean_split_rejects_persistent_face_and_keeps_original(tmp_path):
    plan = base_plan(); plan["parameters"] = {"width": {"value": 100}, "bore": {"value": 5}}
    plan["features"][0]["size"] = ["width", 40, 10]
    plan["features"] += [{"id": "hole", "op": "cylinder", "radius": "bore", "height": 12, "origin": [50, 20, -1]},
                         {"id": "cut", "op": "cut", "inputs": ["body", "hole"]}]; plan["result"] = "cut"
    shape, _, _ = build_plan_shape(plan); preview, _ = export_topology_preview(plan, shape, tmp_path)
    face = next(face for face in preview["faces"] if face.get("normal", [0,0,0])[2] > .9)
    plan["features"].append(picked_profile(face)); plan["result"] = "boss"
    bound, _, _ = rebuilt(plan); before = deepcopy(bound)
    changed = deepcopy(bound); changed["parameters"]["bore"]["value"] = 25
    with pytest.raises(PlanValidationError) as exc: rebuilt(changed, bound)
    assert exc.value.code == "unresolved_topology_binding" and bound == before


@pytest.mark.parametrize("op", ["translate", "rotate", "linear_pattern", "circular_pattern"])
def test_transformed_or_pattern_instances_keep_unique_semantic_face(tmp_path, op):
    plan = base_plan()
    feature = {"id": "moved", "op": op, "input": "body"}
    if op == "translate": feature["vector"] = [30, 0, 0]
    if op == "rotate": feature.update(axisStart=[0,0,0], axisEnd=[0,1,0], angle=30)
    if op == "linear_pattern": feature.update(count=2, vector=[30,0,0])
    if op == "circular_pattern": feature.update(count=2, axisStart=[-30,0,0], axisEnd=[-30,0,1], angle=180)
    plan["features"].append(feature); plan["result"] = "moved"
    shape, _, _ = build_plan_shape(plan); preview, _ = export_topology_preview(plan, shape, tmp_path)
    face = next(face for face in preview["faces"] if face.get("normal", [0,0,0])[2] > .8)
    plan["features"].append(picked_profile(face)); plan["result"] = "boss"
    bound, _, _ = rebuilt(plan); assert bound["features"][-1]["planeSource"].get("binding")
    changed = deepcopy(bound); changed["parameters"]["width"]["value"] = 22
    _, after, _ = rebuilt(changed, bound)
    assert after.val().isValid() and after.val().Volume() == pytest.approx(24)


def test_binding_cannot_be_forged_transplanted_or_used_without_saved_authority(box_preview):
    plan, preview = box_preview
    plan["features"].append({"id": "round", "op": "fillet", "input": "body", "radius": 1, "edges": [preview["edges"][0]["selector"]]}); plan["result"] = "round"
    bound, _, _ = rebuilt(plan)
    with pytest.raises(PlanValidationError) as exc: rebuilt(bound)
    assert exc.value.code == "untrusted_topology_binding"
    forged = deepcopy(bound); forged["features"][-1]["edges"][0]["binding"]["key"] = "a"*64
    with pytest.raises(PlanValidationError): authorize_bindings(forged, bound)
    transplanted = deepcopy(bound); transplanted["features"][-1]["id"] = "another"; transplanted["result"] = "another"
    with pytest.raises(PlanValidationError): authorize_bindings(transplanted, bound)


def test_invalid_saved_non_topological_draft_can_still_be_repaired():
    baseline = base_plan(); baseline["parameters"]["width"]["value"] = -1
    valid = base_plan(); _, shape, _ = rebuilt(valid, baseline)
    assert shape.val().Volume() == pytest.approx(3200)


def test_saved_boss_boolean_fillet_history_is_atomic_private_and_downloadable(tmp_path):
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError, FeatureNotFound, FeatureConflict
    from app.cad_topology import feature_prefix
    cq = get_cadquery(); plan = base_plan()
    plan['features'] += [{'id':'hole','op':'cylinder','radius':2,'height':12,'origin':[5,8,-1]},
                         {'id':'cut','op':'cut','inputs':['body','hole']}]; plan['result']='cut'
    shape,_,_=build_plan_shape(plan); preview,_=export_topology_preview(plan,shape,tmp_path)
    face=next(item for item in preview['faces'] if item.get('normal',[0,0,0])[2]>.9)
    plan['features'] += [picked_profile(face),{'id':'joined','op':'union','inputs':['cut','boss']}];plan['result']='joined'
    shape,_,_=build_plan_shape(plan);preview,_=export_topology_preview(plan,shape,tmp_path)
    picks=[item['selector'] for item in preview['edges'] if all(abs(z-14)<1e-6 for z in item['points'][2::3])][:2]
    assert len(picks)==2
    plan['features'].append({'id':'round','op':'fillet','input':'joined','radius':.3,'edges':picks});plan['result']='round'
    store=CadFeatureWorkspace(tmp_path/'store')
    old=store.save('alice',{'name':'历史底板','changeNote':'旧版本','plan':plan})
    beforePlan=deepcopy(old['plan']);data={'designId':old['id'],'expectedRevision':1,'name':'历史底板','changeNote':'宽度改变','requestId':'history-commit-0001','plan':deepcopy(plan)}
    data['plan']['parameters']['width']['value']=22
    assert store.preview('alice',data)['valid']
    assert store.get('alice',old['id'])['plan']==beforePlan and len(store.versions('alice',old['id']))==1
    built=store.commit('alice',data)
    assert built['revision']==2 and built['status']=='built' and store.commit('alice',data)==built
    assert len(store.versions('alice',old['id']))==2
    stepPath,_=store.artifact('alice',built['id'],2,'step');beforeBytes=stepPath.read_bytes()
    shape=cq.importers.importStep(str(stepPath)).val()
    assert shape.isValid() and shape.BoundingBox().xmax==pytest.approx(22)
    assert all('binding' in item for item in built['plan']['features'][-1]['edges'])
    bossBefore=feature_prefix(plan,'boss');_,a,_=rebuilt(bossBefore,plan)
    bossAfter=feature_prefix(built['plan'],'boss');_,b,_=rebuilt(bossAfter,built['plan'])
    assert b.val().Center().x-a.val().Center().x==pytest.approx(1)
    with pytest.raises(FeatureNotFound):store.preview('bob',{**data,'expectedRevision':2})
    with pytest.raises(FeatureConflict):store.commit('alice',{**data,'requestId':'another-request-01'})
    with pytest.raises(PlanValidationError):store.save('bob',{'name':'伪造','changeNote':'复制绑定','plan':built['plan']})
    bad={**data,'expectedRevision':2,'requestId':'invalid-radius-0001','plan':deepcopy(built['plan'])};bad['plan']['features'][-1]['radius']=1000
    with pytest.raises(FeatureWorkspaceError):store.commit('alice',bad)
    assert store.get('alice',old['id'])==built and stepPath.read_bytes()==beforeBytes
    assert len(store.versions('alice',old['id']))==2


@pytest.mark.parametrize('primitive', ['box','cylinder'])
@pytest.mark.parametrize('picked_kind', ['edge','face'])
def test_equivalent_primitive_to_editable_sketch_preserves_persistent_history(tmp_path, primitive, picked_kind):
    plan=base_plan()
    if primitive=='cylinder':plan['features'][0]={'id':'body','op':'cylinder','radius':'width','height':10};plan['parameters']['width']['value']=6
    shape,_,_=build_plan_shape(plan);preview,_=export_topology_preview(plan,shape,tmp_path)
    if picked_kind=='face':feature=picked_profile(next(item for item in preview['faces'] if item.get('normal',[0,0,0])[2]>.9))
    else:
        edge=next(item for item in preview['edges'] if shape.val().Edges()[item['selector']['index']].geomType()==('CIRCLE' if primitive=='cylinder' else 'LINE'))
        feature={'id':'round','op':'fillet','input':'body','radius':.2,'edges':[edge['selector']]}
    plan['features'].append(feature);plan['result']=feature['id'];bound,before,_=rebuilt(plan)
    changed=deepcopy(bound)
    profile={'id':'body','op':'profile_extrude','plane':'XY','distance':10}
    if primitive=='box':profile.update(start=[0,0],segments=[{'type':'line','to':['width',0]},{'type':'line','to':['width',16]},{'type':'line','to':[0,16]},{'type':'line','to':[0,0]}])
    else:profile.update(start=['width',0],segments=[{'type':'arc','through':[0,'width'],'to':['-width',0]},{'type':'arc','through':[0,'-width'],'to':['width',0]}])
    changed['features'][0]=profile
    _,equal,_=rebuilt(changed,bound)
    assert equal.val().Volume()==pytest.approx(before.val().Volume())
    assert equal.val().Center().toTuple()==pytest.approx(before.val().Center().toTuple())
    changed['parameters']['width']['value']+=2
    _,after,_=rebuilt(changed,bound)
    assert after.val().isValid()
    cq=get_cadquery();path=tmp_path/'sketch-history.step';cq.exporters.export(after.val(),str(path))
    assert cq.importers.importStep(str(path)).val().Volume()==pytest.approx(after.val().Volume(),rel=1e-7)


def test_bound_reference_does_not_reexecute_invalid_or_suppressed_saved_tail(box_preview):
    plan,preview=box_preview
    plan['features'].append({'id':'round','op':'fillet','input':'body','radius':1,'edges':[preview['edges'][0]['selector']]});plan['result']='round'
    bound,_,_=rebuilt(plan)
    # Saving a draft while a later feature is suppressed must not revoke the
    # server-owned binding, nor require executing that inactive feature.
    baseline=deepcopy(bound);baseline['features'].append({'id':'inactive','op':'cylinder','radius':-1,'height':2})
    changed=deepcopy(bound);changed['parameters']['width']['value']=22
    _,shape,_=rebuilt(changed,baseline)
    assert shape.val().isValid() and shape.val().BoundingBox().xmax==pytest.approx(22)


def test_real_browser_box_to_trapezoid_caps_keep_attachment_and_fillet_through_commits(tmp_path):
    import json
    from pathlib import Path
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
    from app.cad_history import strip_history_metadata
    from app.cad_topology import _bounds
    fixture=json.loads((Path(__file__).parent/'fixtures/cad_history_browser_box_to_trapezoid.json').read_text())
    store=CadFeatureWorkspace(tmp_path/'store')
    # Recreate the original in this owner/design through the real commit API;
    # do not transplant another design's trusted references.
    first=store.commit('alice',{'name':'浏览器真实回归','changeNote':'原始80毫米底板','plan':strip_history_metadata(fixture['baselinePlan']),'requestId':'browser-history-original'})
    assert first['inspection']['volumeMm3']==pytest.approx(fixture['baselineVolumeMm3'],abs=1e-5)
    assert first['plan']['features'][1]['planeSource']==fixture['baselinePlan']['features'][1]['planeSource']
    originalPath=Path(first['artifacts']['step']['path']);originalBytes=originalPath.read_bytes()
    payload={'designId':first['id'],'expectedRevision':first['revision'],'name':'浏览器真实回归','changeNote':'底边85、上边80的梯形','plan':deepcopy(fixture['candidatePlan']),'requestId':'browser-history-trapezoid'}
    preview=store.preview('alice',payload)
    assert preview['valid'] and len(store.versions('alice',first['id']))==1
    second=store.commit('alice',payload)
    assert store.commit('alice',payload)==second
    assert second['inspection']['volumeMm3']==pytest.approx(50099.053693835885,abs=1e-5)
    assert second['plan']['features'][1]['planeAttachment']==first['plan']['features'][1]['planeAttachment']
    assert second['plan']['features'][1]['planeSource']['binding']!=first['plan']['features'][1]['planeSource']['binding']
    # Verify normalization remains editable: enlarge the same trapezoid, then
    # return to a rectangle, both through immutable saved versions.
    thirdPlan=deepcopy(second['plan']);base=thirdPlan['features'][0]
    base['start']=[-5,0];base['segments'][0]['to']=[85,0];base['segments'][-1]['to']=[-5,0];base['sketchConstraints'][0]['value']=90
    third=store.commit('alice',{**payload,'plan':thirdPlan,'expectedRevision':second['revision'],'requestId':'browser-history-trapezoid-again'})
    fourthPlan=deepcopy(third['plan']);base=fourthPlan['features'][0]
    base['start']=[0,0];base['segments'][0]['to']=[85,0];base['segments'][1]['to']=[85,50];base['segments'][2]['to']=[0,50];base['segments'][3]['to']=[0,0];base['sketchConstraints'][0]['value']=85
    fourth=store.commit('alice',{**payload,'plan':fourthPlan,'expectedRevision':third['revision'],'requestId':'browser-history-rectangle-again'})
    assert fourth['inspection']['volumeMm3']==pytest.approx(fixture['baselineVolumeMm3']+3000,abs=1e-5)
    assert fourth['plan']['features'][1]['planeSource']['binding']==first['plan']['features'][1]['planeSource']['binding']
    cq=get_cadquery()
    for record in [first,second,third,fourth]:
        restored=cq.importers.importStep(record['artifacts']['step']['path']).val()
        assert restored.isValid() and len(restored.Solids())==1
        assert restored.Volume()==pytest.approx(record['inspection']['volumeMm3'],rel=1e-7)
        assert _bounds(restored)['max'][2]==pytest.approx(17)
    assert originalPath.read_bytes()==originalBytes and len(store.versions('alice',first['id']))==4
    failed=deepcopy(fourth['plan']);failed['features'][-1]['radius']=1000
    with pytest.raises(FeatureWorkspaceError):store.commit('alice',{**payload,'plan':failed,'expectedRevision':fourth['revision'],'requestId':'browser-history-radius-fail'})
    assert store.get('alice',first['id'])==fourth


@pytest.mark.parametrize('normal_sign',[-1,1])
def test_box_extrusion_cap_aliases_preserve_both_outward_planes(tmp_path,normal_sign):
    plan=base_plan();shape,_,_=build_plan_shape(plan);preview,_=export_topology_preview(plan,shape,tmp_path)
    face=next(item for item in preview['faces'] if item.get('normal',[0,0,0])[2]*normal_sign>.9)
    plan['features'].append(picked_profile(face));plan['result']='boss'
    bound,before,_=rebuilt(plan)
    changed=deepcopy(bound);changed['features'][0]={'id':'body','op':'profile_extrude','plane':'XY','start':[-1,0],
        'segments':[{'type':'line','to':[21,0]},{'type':'line','to':[20,16]},{'type':'line','to':[0,16]},{'type':'line','to':[-1,0]}],'distance':10}
    rebound,after,_=rebuilt(changed,bound)
    assert after.val().Volume()==pytest.approx(24)
    assert after.val().Center().z==pytest.approx(12 if normal_sign==1 else -2)
    # The generic prism's start face uses the sketch's +Z reference frame;
    # the box bottom uses outward -Z. Re-express the attachment in that new
    # local basis while retaining its physical outward extrusion direction.
    attachment=rebound['features'][-1]['planeAttachment']
    assert attachment['origin']==pytest.approx(bound['features'][-1]['planeAttachment']['origin'])
    assert attachment['xDir']==pytest.approx([1,0,0])
    assert attachment['normal']==pytest.approx([0,0,normal_sign])
    # A fresh worker has no transient alias state; the normalized saved plan
    # must rebuild on its own with only the saved owner's authorization.
    _,again,_=rebuilt(rebound,rebound)
    assert again.val().Center().toTuple()==pytest.approx(after.val().Center().toTuple())
    assert again.val().Volume()==pytest.approx(after.val().Volume())
