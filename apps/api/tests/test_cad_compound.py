from copy import deepcopy
import math

import pytest

from app.cad_executor import _build_worker_shape, build_plan_shape
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureConflict, FeatureWorkspaceError, compile_manual_plan
from app.cad_history import compound_builder
from app.cad_plan import PlanValidationError, cad_plan_schema, validate_plan
from app.cad_plan_edit import apply_plan_edit
from app.cad_topology import export_topology_preview
from app.geometry import get_cadquery


def plan_at(x=40):
    return {'version':'cad-plan-v1','name':'独立实体','units':'mm','parameters':{'width':{'value':20}},'features':[
        {'id':'a','op':'box','size':['width',16,10]},
        {'id':'b','op':'box','size':[8,6,5],'origin':[x,0,0]},
        {'id':'bodies','op':'compound','inputs':['a','b']}], 'result':'bodies'}


def rebuild(plan, baseline=None):
    request={'plan':plan,'persistTopology':True,'bindingBasePlan':baseline}
    shape,_,_=_build_worker_shape(request,None)
    return request['builtPlan'],shape


@pytest.mark.parametrize('refs',[[],['a'],['a','a'],['a','future'],['a','bodies'],['a',1],['a']*33])
def test_compound_schema_rejects_invalid_or_cyclic_dependencies(refs):
    plan=plan_at();plan['features'][-1]['inputs']=refs;original=deepcopy(plan)
    with pytest.raises(PlanValidationError):validate_plan(plan)
    assert plan==original


def test_compound_strict_schema_edit_and_suppression_contract():
    plan=plan_at();assert validate_plan(plan)==plan
    schema=next(item for item in cad_plan_schema()['properties']['features']['items']['oneOf'] if item['properties']['op']['const']=='compound')
    assert schema['properties']['inputs']['uniqueItems'] is True and schema['additionalProperties'] is False
    invalid=deepcopy(plan);invalid['features'][-1]['fuse']=True
    with pytest.raises(PlanValidationError):validate_plan(invalid)
    with pytest.raises(FeatureWorkspaceError,match='依赖'):compile_manual_plan(plan,['b'])
    with pytest.raises(PlanValidationError):apply_plan_edit(plan,{'removeFeatures':['b']})
    assert apply_plan_edit(plan,{'features':[{'id':'bodies','op':'compound','inputs':['b','a']}]})['features'][-1]['inputs']==['b','a']


real_occt=pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')


@real_occt
@pytest.mark.parametrize('x',[0,10,20,40])
def test_compound_keeps_disjoint_touching_overlapping_and_identical_solids_in_step(tmp_path,x):
    cq=get_cadquery();plan=plan_at(x);plan['features'][1]['size']=[20,16,10]
    original=deepcopy(plan)
    for history in [False,True]:
        shape=rebuild(plan)[1] if history else build_plan_shape(plan)[0]
        value=shape.val()
        assert value.isValid() and len(value.Solids())==2
        assert value.Volume()==pytest.approx(6400)
        assert all(solid.Volume()==pytest.approx(3200) for solid in value.Solids())
        path=tmp_path/f'compound-{history}.step';cq.exporters.export(value,str(path))
        restored=cq.importers.importStep(str(path)).val()
        assert restored.isValid() and len(restored.Solids())==2
        assert restored.Volume()==pytest.approx(6400)
    assert plan==original
    if x==10:
        union=deepcopy(plan);union['features'][-1]['op']='union'
        value=build_plan_shape(union)[0].val()
        assert len(value.Solids())==1 and value.Volume()==pytest.approx(4800)


@real_occt
def test_compound_copy_preserves_duplicate_branches_and_independent_sheets(tmp_path):
    cq=get_cadquery();plan=plan_at(0)
    plan['features'][1]={'id':'b','op':'translate','input':'a','vector':[0,0,0]}
    shape=rebuild(plan)[1].val()
    assert len(shape.Solids())==2 and shape.Volume()==pytest.approx(6400)
    plan['features'] += [{'id':'more','op':'compound','inputs':['bodies','a']}];plan['result']='more'
    assert len(rebuild(plan)[1].val().Solids())==3
    solid=cq.Solid.makeBox(2,2,2);face=solid.Faces()[0]
    shapes={'solid':cq.Workplane('XY').newObject([solid]),'mixed':cq.Workplane('XY').newObject([cq.Compound.makeCompound([solid,face])])}
    mixed, _ = compound_builder(cq,['solid','mixed'],shapes)
    assert len(mixed.val().Solids()) == 2 and len(mixed.val().Faces()) == 13
    assert sum(s.Volume() for s in mixed.val().Solids()) == pytest.approx(16)
    target = tmp_path / 'independent-sheet.step'
    cq.exporters.export(mixed.val(), str(target))
    restored = cq.importers.importStep(str(target)).val()
    assert restored.isValid() and len(restored.Solids()) == 2 and len(restored.Faces()) == 13
    shapes['point'] = cq.Workplane('XY').newObject([cq.Vertex.makeVertex(0,0,0)])
    with pytest.raises(PlanValidationError): compound_builder(cq,['solid','point'],shapes)


@real_occt
def test_compound_fails_before_copying_unbounded_member_counts():
    cq=get_cadquery();solid=cq.Solid.makeBox(1,1,1)
    # Direct child occurrences must be counted even when an OCCT indexed map
    # would collapse repeated shared identities into one entry.
    crowded=cq.Compound.makeCompound([solid]*1025)
    with pytest.raises(PlanValidationError) as error:
        compound_builder(cq,['a','b'],{'a':cq.Workplane('XY').newObject([crowded]),'b':cq.Workplane('XY').newObject([solid])})
    assert error.value.code=='resource_limit'


@real_occt
def test_copied_branch_member_binding_survives_source_dimension_change_and_reorder(tmp_path):
    plan=plan_at();plan['features'][1]={'id':'b','op':'translate','input':'a','vector':[40,0,0]}
    shape=build_plan_shape(plan)[0];preview,_=export_topology_preview(plan,shape,tmp_path)
    edge=next(item for item in preview['edges'] if min(item['points'][0::3])>=40)
    edge_length=shape.val().Edges()[edge['selector']['index']].Length()
    plan['features'].append({'id':'round','op':'fillet','input':'bodies','radius':1,'edges':[edge['selector']]});plan['result']='round'
    bound,before=rebuild(plan)
    assert 'binding' in bound['features'][-1]['edges'][0]
    changed=deepcopy(bound);changed['parameters']['width']['value']=22;changed['features'][2]['inputs']=['b','a']
    _,after=rebuild(changed,bound)
    assert len(after.val().Solids())==2
    expected_loss=edge_length*(1-math.pi/4)
    assert before.val().Volume()==pytest.approx(6400-expected_loss)
    assert after.val().Volume()>before.val().Volume()
    solids=sorted(after.val().Solids(),key=lambda value:value.Center().x)
    assert solids[0].Volume()==pytest.approx(22*16*10)
    assert solids[1].Volume()<22*16*10
    with pytest.raises(PlanValidationError) as error:build_plan_shape(changed)
    assert error.value.code=='stale_topology'


@real_occt
def test_compound_transaction_preview_fillet_rebuild_step_and_failure_keep_all_bodies(tmp_path):
    cq=get_cadquery();store=CadFeatureWorkspace(tmp_path/'workspace')
    payload={'name':'两个独立实体','fileId':'compound-test','changeNote':'保留原体新建','suppressed':[], 'plan':plan_at(12),'requestId':'compound-create-1'}
    first=store.commit('alice',payload)
    assert first['inspection']['solidCount']==2
    path=store.artifact('alice',first['id'],1,'step')[0];before=path.read_bytes()
    preview=store.preview('alice',{'plan':first['plan'],'designId':first['id'],'expectedRevision':1})
    assert preview['inspection']['solidCount']==2 and len(preview['faces'])==12
    # Select a unique top edge of the smaller overlapping body; the touch/
    # overlap never becomes a Boolean union or silently drops the larger body.
    edge=next(item for item in preview['edges'] if all(abs(z-5)<1e-7 for z in item['points'][2::3]) and min(item['points'][0::3])>=12)
    plan=deepcopy(first['plan']);plan['features'].append({'id':'round','op':'fillet','input':'bodies','radius':.5,'edges':[edge['selector']]});plan['result']='round'
    second=store.commit('alice',{**payload,'designId':first['id'],'expectedRevision':1,'requestId':'compound-fillet-2','plan':plan})
    changed=deepcopy(second['plan']);changed['parameters']['width']['value']=24
    third=store.commit('alice',{**payload,'designId':first['id'],'expectedRevision':2,'requestId':'compound-width-3','plan':changed})
    assert third['inspection']['volumeMm3']-second['inspection']['volumeMm3']==pytest.approx(4*16*10)
    for record in [first,second,third]:
        restored=cq.importers.importStep(record['artifacts']['step']['path']).val()
        assert restored.isValid() and len(restored.Solids())==2
        assert restored.Volume()==pytest.approx(record['inspection']['volumeMm3'],rel=1e-7)
        assert record['inspection']['stepReadback']['solidCount']==2
        assert record['artifacts']['glb']['path']
    assert path.read_bytes()==before
    failed=deepcopy(third['plan']);failed['features'][-1]['radius']=1000
    with pytest.raises(FeatureWorkspaceError):store.commit('alice',{**payload,'designId':first['id'],'expectedRevision':3,'requestId':'compound-failed-4','plan':failed})
    assert store.get('alice',first['id'])==third and len(store.versions('alice',first['id']))==3
    with pytest.raises(FeatureConflict):store.commit('alice',{**payload,'designId':first['id'],'expectedRevision':1,'requestId':'compound-stale-5'})
