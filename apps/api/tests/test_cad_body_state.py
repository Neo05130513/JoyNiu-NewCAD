from copy import deepcopy

import pytest

from app.cad_executor import build_plan_shape, _build_worker_shape
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
from app.cad_history import authorize_bindings
from app.cad_plan import PlanValidationError, validate_plan
from app.cad_surface_features import body_signature
from app.geometry import get_cadquery

pytestmark = pytest.mark.skipif(get_cadquery() is None, reason='Real OCCT required')


def scene():
    p={'version':'cad-plan-v1','parameters':{'width':{'value':20}},'features':[
        {'id':'first','op':'box','size':['width',16,10]},
        {'id':'second','op':'box','size':[8,6,5],'origin':[40,0,0]},
        {'id':'parts','op':'compound','inputs':['first','second']}], 'result':'parts'}
    shape=build_plan_shape(p)[0]
    bodies=sorted(shape.val().Solids(),key=lambda body:body.Center().x)
    p['bodyStates']=[{'id':'left','label':'底座','hidden':True,'color':'#123456','signature':body_signature(bodies[0])},
                     {'id':'right','label':'盖板','hidden':False,'color':'#abcdef','signature':body_signature(bodies[1])}]
    return p


def rebuild(p, baseline=None):
    request={'plan':deepcopy(p),'persistTopology':True,'bindingBasePlan':baseline}
    result=_build_worker_shape(request,None)
    return request['builtPlan'], result[0]


def test_body_attributes_follow_true_members_through_dimensions_and_added_compound(tmp_path):
    original=scene();bound,shape=rebuild(original)
    assert all(state['binding']['keys'] and state['binding']['otherKeys'] for state in bound['bodyStates'])
    changed=deepcopy(bound);changed['parameters']['width']['value']=24
    changed['features'].extend([{'id':'third','op':'box','size':[3,4,5],'origin':[70,0,0]},
                                {'id':'all','op':'compound','inputs':['parts','third']}]);changed['result']='all'
    new,shape=rebuild(changed,bound)
    assert len(shape.val().Solids())==3
    bodies=sorted(shape.val().Solids(),key=lambda body:body.Center().x)
    assert new['bodyStates'][0]['signature']==body_signature(bodies[0])
    assert new['bodyStates'][1]['signature']==body_signature(bodies[1])
    assert new['bodyStates'][0]['signature']!=bound['bodyStates'][0]['signature']
    for a,b in zip(new['bodyStates'],bound['bodyStates']):
        assert all(a[k]==b[k] for k in ['label','color','hidden'])
        assert a['binding']['keys'] and a['binding']['otherKeys']
    target=tmp_path/'three-bodies.step';get_cadquery().exporters.export(shape.val(),str(target))
    actual=get_cadquery().importers.importStep(str(target)).val()
    assert actual.isValid() and len(actual.Solids())==3
    assert actual.Volume()==pytest.approx(24*16*10+8*6*5+3*4*5)


def test_body_attribute_commit_reopen_and_legacy_saved_draft_upgrade(tmp_path):
    store=CadFeatureWorkspace(tmp_path/'store')
    initial=store.save('alice',{'name':'两实体显示','plan':scene(),'suppressed':[],'changeNote':'旧保存数据'})
    changed=deepcopy(initial['plan']);changed['parameters']['width']['value']=24
    second=store.commit('alice',{'designId':initial['id'],'expectedRevision':1,'name':'两实体显示','plan':changed,'suppressed':[],'changeNote':'改宽'})
    reread=CadFeatureWorkspace(tmp_path/'store').get('alice',initial['id'])
    assert reread==second and reread['revision']==2 and reread['inspection']['stepReadback']['valid']
    assert reread['plan']['bodyStates'][0]['hidden'] and reread['plan']['bodyStates'][0]['label']=='底座'
    assert reread['plan']['bodyStates'][1]['color']=='#abcdef' and not reread['plan']['bodyStates'][1]['hidden']
    assert all(state['binding']['keys'] for state in reread['plan']['bodyStates'])
    assert len(store.versions('alice',initial['id']))==2
    preview=store.preview('alice',{'designId':initial['id'],'expectedRevision':2,'featureId':'first','plan':reread['plan']})
    assert preview['inspection']['solidCount']==1


def test_body_binding_cannot_be_forged_transplanted_or_laundered_through_save(tmp_path):
    bound,_=rebuild(scene())
    store=CadFeatureWorkspace(tmp_path/'store')
    for value in [bound, {**scene(),'bodyStates':[{'id':'left','signature':'a'*64,'binding':{'version':1,'keys':['a'*64],'otherKeys':[]}}]}]:
        with pytest.raises(PlanValidationError,match='已保存'):store.save('eve',{'name':'伪造','plan':value,'suppressed':[],'changeNote':'伪造'})
    assert store.list('eve')==[]
    bad=deepcopy(bound);bad['bodyStates'][0]['binding']=deepcopy(bound['bodyStates'][1]['binding'])
    with pytest.raises(PlanValidationError):authorize_bindings(bad,bound)
    bad=deepcopy(bound);bad['bodyStates'][0]['binding']['keys'][0]='c'*64
    with pytest.raises(PlanValidationError):rebuild(bad,bound)
    bad=deepcopy(bound);bad['bodyStates'][0]['binding']['guessIndex']=1
    with pytest.raises(PlanValidationError):validate_plan(bad)


@pytest.mark.parametrize('operation',['split','merge','removed'])
def test_ambiguous_or_disappeared_body_identity_is_rejected_and_head_unchanged(tmp_path,operation):
    store=CadFeatureWorkspace(tmp_path/'store')
    initial=store.commit('alice',{'name':'两实体显示','plan':scene(),'suppressed':[],'changeNote':'命名'})
    bad=deepcopy(initial['plan'])
    if operation=='split':
        bad['features'].append({'id':'copies','op':'linear_pattern','input':'parts','vector':[100,0,0],'count':2,'fuse':False});bad['result']='copies'
    elif operation=='merge':
        bad['features'][1]['origin']=[15,0,0]
        bad['features'].append({'id':'joined','op':'union','inputs':['first','second']});bad['result']='joined'
    else: bad['result']='second'
    with pytest.raises(FeatureWorkspaceError) as error:
        store.commit('alice',{'designId':initial['id'],'expectedRevision':1,'name':'两实体显示','plan':bad,'suppressed':[],'changeNote':'有歧义'})
    assert error.value.code=='unresolved_body_binding'
    assert store.get('alice',initial['id'])==initial and len(store.versions('alice',initial['id']))==1
