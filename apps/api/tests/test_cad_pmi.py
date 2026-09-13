from copy import deepcopy
import pytest
from app.cad_executor import _build_worker_shape, build_plan_shape
from app.cad_topology import export_topology_preview
from app.cad_plan import PlanValidationError
from app.cad_editor_entities import annotation_measurement
from app.cad_feature_workspace import CadFeatureWorkspace


def scene(tmp_path,kind='face'):
    plan={'version':'cad-plan-v1','parameters':{'width':{'value':20}},'features':[{'id':'body','op':'box','size':['width',16,10]}],'result':'body'}
    shape,_,_=build_plan_shape(plan); geometry,_=export_topology_preview(plan,shape,tmp_path)
    if kind=='face':
        face=next(f for f in geometry['faces'] if f.get('normal',[0,0,0])[2]>.9)
        refs=[deepcopy(face['selector']),deepcopy(face['selector'])];points=[[2,8,10],[18,8,10]]
    else:
        edge=next(e for e in geometry['edges'] if abs(e['points'][0]-e['points'][-3])>19)
        refs=[deepcopy(edge['selector']),deepcopy(edge['selector'])];points=[edge['points'][:3],edge['points'][-3:]]
    plan['annotations']=[{'id':'pmi1','kind':'distance','text':'总长','points':points,'position':[20,20,15],'references':refs}]
    return plan


def build(plan,baseline=None):
    request={'plan':plan,'bindingBasePlan':baseline,'persistTopology':True}
    shape,_,_=_build_worker_shape(request,None)
    return request['builtPlan'],shape


@pytest.mark.parametrize('kind',['face','edge'])
def test_measurement_tracks_same_semantic_topology_and_step_roundtrip(tmp_path,kind):
    import cadquery as cq
    bound,shape=build(scene(tmp_path,kind)); note=bound['annotations'][0]
    assert len(note['anchors'])==2 and all(ref['binding']['key'] for ref in note['references'])
    before=annotation_measurement(note,float)
    changed=deepcopy(bound);changed['parameters']['width']['value']=30
    updated,shape=build(changed,bound);after=annotation_measurement(updated['annotations'][0],float)
    assert after==pytest.approx(before*1.5)
    path=tmp_path/'annotated.step';cq.exporters.export(shape.val(),str(path))
    assert cq.importers.importStep(str(path)).val().Volume()==pytest.approx(4800)
    assert updated['annotations'][0]['position']!=note['position']
    assert bound['parameters']['width']['value']==20


def test_pmi_owner_private_anchors_and_forged_topology_refused(tmp_path):
    bound,_=build(scene(tmp_path))
    with pytest.raises(PlanValidationError):build(bound)
    changed=deepcopy(bound);changed['annotations'][0]['anchors'][0]['coordinates'][0]=.9
    with pytest.raises(PlanValidationError,match='已保存'):build(changed,bound)
    changed=deepcopy(bound);changed['annotations'][0]['references'][0]['binding']['key']='a'*64
    with pytest.raises(PlanValidationError):build(changed,bound)
    changed=scene(tmp_path);changed['annotations'][0]['points'][0]=[200,200,10]
    with pytest.raises(PlanValidationError,match='边界'):build(changed)


def test_pmi_saved_reopen_edit_revision_persists_anchors_and_real_geometry(tmp_path):
    store=CadFeatureWorkspace(tmp_path/'store')
    first=store.commit('alice',{'name':'标注件','changeNote':'编辑标注','plan':scene(tmp_path),'suppressed':[]})
    assert first['plan']['annotations'][0]['anchors']
    reread=store.get('alice',first['id'])
    changed=deepcopy(reread['plan']);changed['parameters']['width']['value']=30
    second=store.commit('alice',{'designId':first['id'],'expectedRevision':first['revision'],'name':'标注件','changeNote':'编辑标注','plan':changed,'suppressed':[]})
    assert second['inspection']['stepReadback']['valid']
    assert annotation_measurement(second['plan']['annotations'][0],float)==pytest.approx(24)
    assert store.get('alice',first['id'],first['revision'])['plan']['annotations'][0]['points']==first['plan']['annotations'][0]['points']


def test_untrusted_anchor_cannot_be_laundered_through_draft_save(tmp_path):
    store=CadFeatureWorkspace(tmp_path/'store')
    plan=scene(tmp_path)
    plan['annotations'][0]['anchors']=[{'version':1,'kind':'face','coordinates':[.2,.3]},{'version':1,'kind':'face','coordinates':[.8,.3]}]
    with pytest.raises((PlanValidationError, ValueError),match='锚点|已保存|当前设计'):
        store.save('alice',{'name':'伪造标注','changeNote':'新建','plan':plan,'suppressed':[]})
    assert store.list('alice')==[]


def test_missing_unique_history_never_persists_fake_associative_anchors(tmp_path,monkeypatch):
    from app.cad_history import TopologyHistory
    plan=scene(tmp_path)
    monkeypatch.setattr(TopologyHistory,'entity_key',lambda *args:None)
    def same_process_executor(plan, *args, **kwargs):
        return build(plan)[0]
    store=CadFeatureWorkspace(tmp_path/'store',executor=same_process_executor)
    with pytest.raises((PlanValidationError,ValueError),match='关联'):
        store.commit('alice',{'name':'标注件','changeNote':'保存标注','plan':plan,'suppressed':[]})
    assert store.list('alice')==[]
    independent=deepcopy(plan);independent['annotations'][0]['references']=[]
    store=CadFeatureWorkspace(tmp_path/'store')
    saved=store.commit('alice',{'name':'标注件','changeNote':'明确独立坐标','plan':independent,'suppressed':[]})
    assert 'anchors' not in saved['plan']['annotations'][0]
    assert saved['inspection']['stepReadback']['valid']
