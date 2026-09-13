"""Regression for the actual browser's holed multi-body + sheet PMI r5."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.cad_executor import build_plan_shape, _build_worker_shape
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
from app.cad_history import TopologyHistory
from app.cad_surface_features import topology_surface_bodies
from app.cad_topology import export_topology_preview
from app.geometry import get_cadquery

pytestmark=pytest.mark.skipif(get_cadquery() is None,reason='Requires actual OCCT')


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/cad_pmi_browser_multicontour_sheet.json').read_text())


def widen(p):
    p=deepcopy(p)
    for index in (0,1):p['features'][0]['segments'][index]['to'][0]=45
    return p


def test_real_browser_all_fourteen_cleaned_compound_faces_have_unique_semantics(tmp_path):
    p=fixture();history=TopologyHistory(persist=True,trusted_source=True);out={}
    shape,_,_=build_plan_shape(p,history=history,history_output=out)
    assert len(shape.val().Faces())==14 and len(shape.val().Solids())==2
    keys=[history.entity_key(p['result'],'face',face) for face in shape.val().Faces()]
    assert all(keys) and len(set(keys))==14
    notes=out['plan']['annotations'][0]
    assert all(ref['binding']['key'] for ref in notes['references'])
    assert notes['references'][0]['binding']==notes['references'][1]['binding']
    preview,_=export_topology_preview(out['plan'],shape,tmp_path)
    assert len(preview['bodies'])==2 and len(preview['surfaceBodies'])==1
    surface=preview['surfaceBodies'][0]
    assert surface['areaMm2']>400 and len(surface['faceIds'])==1
    assert not set(surface['faceIds']) & {face for body in preview['bodies'] for face in body['faceIds']}
    bound=out['plan'];changed=widen(bound);changed['features'][1]['points'][1][1][2]+=1
    next_history=TopologyHistory(persist=True,baseline=bound);out2={}
    modified,_,_=build_plan_shape(changed,history=next_history,history_output=out2)
    newkeys=[next_history.entity_key(p['result'],'face',face) for face in modified.val().Faces()]
    assert set(keys)==set(newkeys)
    points=out2['plan']['annotations'][0]['points']
    for old,new in zip(notes['points'],points):
        assert new[0]==pytest.approx(old[0]*45/40)
        assert new[1:]==pytest.approx(old[1:])
    assert topology_surface_bodies(modified)[0]['signature']!=surface['signature']
    target=tmp_path/'changed.step';get_cadquery().exporters.export(modified.val(),str(target))
    restored=get_cadquery().importers.importStep(str(target)).val()
    assert restored.isValid() and len(restored.Solids())==2 and len(restored.Faces())==14
    assert sum(s.Volume() for s in restored.Solids())-sum(s.Volume() for s in shape.val().Solids())==pytest.approx(750)


def test_old_browser_anchor_without_binding_upgrades_atomically_then_rebuilds(tmp_path):
    p=fixture();assert all('binding' not in ref for ref in p['annotations'][0]['references'])
    store=CadFeatureWorkspace(tmp_path/'store')
    # Restore the exact server-saved legacy plan in this isolated test store;
    # the public API still rejects client-supplied first-save anchors.
    clean=deepcopy(p);clean['annotations'][0].pop('anchors')
    record,_=store._prepare_save('alice',{'name':'真实r5兼容','plan':clean,'suppressed':[],'changeNote':'隔离兼容夹具'})
    record['plan']=p;store._save('alice',record,0)
    changed=widen(p)
    preview=store.preview('alice',{'designId':record['id'],'expectedRevision':1,'plan':changed})
    assert all(ref.get('binding') for ref in preview['plan']['annotations'][0]['references'])
    result=store.commit('alice',{'designId':record['id'],'expectedRevision':1,'name':'真实r5兼容','plan':changed,'suppressed':[],'changeNote':'宽40到45'})
    assert result['revision']==2 and result['inspection']['stepReadback']['valid']
    assert all(ref.get('binding') for ref in result['plan']['annotations'][0]['references'])
    assert store.get('alice',record['id'],1)['plan']==p
    invalid=deepcopy(result['plan']);invalid['features'][0]['segments'][0]['to'][0]=10;invalid['features'][0]['segments'][1]['to'][0]=10
    with pytest.raises(FeatureWorkspaceError):
        store.commit('alice',{'designId':record['id'],'expectedRevision':2,'name':'真实r5兼容','plan':invalid,'suppressed':[],'changeNote':'孔洞越界拒绝'})
    assert store.get('alice',record['id'])==result and len(store.versions('alice',record['id']))==2


def test_surface_body_inventory_excludes_solid_faces_and_preserves_shell_grouping():
    cq=get_cadquery();box=cq.Solid.makeBox(2,3,4);sheet=cq.Face.makePlane(5,6,cq.Vector(10,0,0))
    shell=cq.Shell.makeShell([cq.Face.makePlane(2,3,cq.Vector(20,0,0)),cq.Face.makePlane(2,3,cq.Vector(22,0,0))])
    shape=cq.Workplane().newObject([cq.Compound.makeCompound([box,sheet,shell])])
    result=topology_surface_bodies(shape)
    assert len(result)==2 and sorted(len(item['faceIds']) for item in result)==[1,2]
    assert sorted(item['shapeType'] for item in result)==['Face','Shell']
    assert sum(item['areaMm2'] for item in result)==pytest.approx(42)
