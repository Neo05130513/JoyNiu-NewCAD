"""Opaque sources are bound by immutable content/exact geometry, never index."""
from copy import deepcopy

import pytest

from app.cad_executor import build_plan_shape
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
from app.cad_history import TopologyHistory
from app.cad_import_assets import CadImportAssets
from app.cad_standard_parts import CATALOG
from app.cad_topology import export_topology_preview
from app.geometry import get_cadquery

pytestmark=pytest.mark.skipif(get_cadquery() is None,reason='Requires actual OCCT')


def opaque_scene(tmp_path,kind):
    cq=get_cadquery();store=CadFeatureWorkspace(tmp_path/'store')
    if kind=='import':
        source=tmp_path/'source.step';cq.exporters.export(cq.Solid.makeBox(20,16,10),str(source))
        asset=CadImportAssets(store.root/'imports').upload('alice','original.step',source.read_bytes(),'来源实体','mm','exact-source-test')
        feature={'id':'source',**asset['feature']};points=[[3,4,10],[12,4,10]]
    else:
        feature={'id':'source','op':'standard_part','catalogId':'washer-m8','dimensions':deepcopy(CATALOG['washer-m8']['dimensions'])};points=[[6,0,1.6],[0,6,1.6]]
    drill_origin=[17,12,-1] if kind=='import' else [-6,0,-1]
    p={'version':'cad-plan-v1','parameters':{},'features':[feature,
       {'id':'drill','op':'cylinder','radius':.5,'height':30,'origin':drill_origin},
       {'id':'cut','op':'cut','inputs':['source','drill']},
       {'id':'moved','op':'translate','input':'cut','vector':[0,0,0]},
       {'id':'turned','op':'rotate','input':'moved','axisStart':[0,0,0],'axisEnd':[0,0,1],'angle':0}], 'result':'turned'}
    shape=build_plan_shape(p,imported_assets=store._import_assets('alice',p))[0]
    (tmp_path/'pick').mkdir();preview,_=export_topology_preview(p,shape,tmp_path/'pick')
    face=next(face for face in preview['faces'] if face.get('normal',[0,0,0])[2]>.9)
    p['annotations']=[{'id':'dimension','kind':'distance','text':'实际来源标注','points':points,'references':[deepcopy(face['selector']),deepcopy(face['selector'])],'position':[points[-1][0],points[-1][1],points[-1][2]+2]}]
    return store,p


@pytest.mark.parametrize('kind',['import','standard'])
def test_opaque_source_pmi_tracks_true_translation_rotation_and_step(tmp_path,kind):
    store,p=opaque_scene(tmp_path,kind)
    first=store.commit('alice',{'name':'来源关联','plan':p,'suppressed':[],'changeNote':'真实PMI'})
    note=first['plan']['annotations'][0]
    assert all(ref.get('binding') for ref in note['references'])
    changed=deepcopy(first['plan']);changed['features'][-2]['vector']=[10,2,3];changed['features'][-1]['angle']=90
    second=store.commit('alice',{'designId':first['id'],'expectedRevision':1,'name':'来源关联','plan':changed,'suppressed':[],'changeNote':'移动并旋转'})
    for before,after in zip(note['points'],second['plan']['annotations'][0]['points']):
        assert after==pytest.approx([-(before[1]+2),before[0]+10,before[2]+3],abs=1e-6)
    assert second['inspection']['stepReadback']['valid']
    body=get_cadquery().importers.importStep(second['artifacts']['step']['path']).val()
    assert body.isValid() and len(body.Solids())==1
    assert store.get('alice',first['id'],1)['plan']==first['plan']
    if kind=='standard':
        invalid=deepcopy(second['plan']);invalid['features'][0]['dimensions']['length']=2
        with pytest.raises(FeatureWorkspaceError) as error:
            store.commit('alice',{'designId':first['id'],'expectedRevision':2,'name':'来源关联','plan':invalid,'suppressed':[],'changeNote':'不能证明的尺寸变更'})
        assert error.value.code=='unresolved_pmi_anchor'
        assert store.get('alice',first['id'])==second and len(store.versions('alice',first['id']))==2


def test_exact_registration_never_assigns_coincident_faces_by_index_or_revives_ambiguity():
    cq=get_cadquery();shape=cq.Workplane().newObject([cq.Compound.makeCompound([cq.Solid.makeBox(2,3,4),cq.Solid.makeBox(2,3,4)])])
    history=TopologyHistory(persist=True)
    feature={'id':'opaque','op':'standard_part','catalogId':'synthetic-opaque'}
    history.register_exact(feature,shape)
    assert history.maps['opaque']['face']=={}
    single=cq.Workplane().newObject([cq.Solid.makeBox(2,3,4)])
    history.maps['opaque']={'face':{'a'*64:[]},'edge':{}}
    history.register_exact(feature,single)
    assert history.maps['opaque']['face']=={'a'*64:[]}


def test_exact_registration_preserves_existing_semantic_role_names():
    cq=get_cadquery();p={'version':'cad-plan-v1','parameters':{},'features':[{'id':'box','op':'box','size':[20,16,10]}],'result':'box'}
    history=TopologyHistory(persist=True);shape,_,_=build_plan_shape(p,history=history)
    existing=set(history.maps['box']['face'])
    assert len(existing)==6
    history.register_exact(p['features'][0],shape)
    assert set(history.maps['box']['face'])==existing
