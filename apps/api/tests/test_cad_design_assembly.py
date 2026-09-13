"""Real OCCT assembly orchestration; injected provider is a metering fixture only."""
import copy
import json

import pytest

from app.cad_design_assembly import CadAssemblyJobs, AssemblyAdmissionError, validate_assembly_plan, NeedsAssemblyInput
from app.cad_design_workspace import CadDesignWorkspace, DesignNotFound, DesignError
from .test_billing import account
from .test_billing_policy import configured

pytest.importorskip('cadquery')


def box_plan(name='plate'):
    return {'version':'cad-plan-v1','name':name,'units':'mm',
        'parameters':{'W':{'value':10,'source':{'quote':'宽 10'}},'H':{'value':5,'source':{'quote':'高 5'}}},
        'features':[{'id':'block','op':'box','size':['W','W','H'],'origin':['-W/2','-W/2',0]}],'result':'block'}


def assembly_plan():
    return {'name':'Two plates','questions':[],
        'parts':[{'id':'base','name':'Base','plan':box_plan()},{'id':'cap','name':'Cap','plan':box_plan('cap')}],
        'instances':[{'id':'base1','partId':'base','position':[0,0,0],'rotation':[0,0,0],'fixed':True},
                     {'id':'cap1','partId':'cap','position':[0,0,12],'rotation':[0,0,0],'fixed':False}],
        'constraints':[{'kind':'coincident','aInstance':'base1','bInstance':'cap1',
            'a':{'kind':'face','type':'PLANE','normal':[0,0,1],'extremum':'maxZ'},
            'b':{'kind':'face','type':'PLANE','normal':[0,0,-1],'extremum':'minZ'}}]}


def test_structured_parts_real_execution_shared_library_and_semantic_mate(tmp_path):
    store=CadDesignWorkspace(tmp_path/'designs');jobs=CadAssemblyJobs(store)
    request={'message':'两块宽 10、高 5 的方板，第一块固定，第二块底面与第一块顶面重合。','fileId':'f1','requestId':'repeat-safe','plan':assembly_plan()}
    job=jobs.submit('owner',request,from_ai=False,background=False)
    assert job['status']=='review_required',job
    assert job['source']=='structured' and all(x['status']=='ready' for x in job['parts'])
    design=store.get('owner',job['designId'])
    assert design['metrics']['solidCount']==2
    assert design['metrics']['volume']==pytest.approx(1000)
    assert design['metrics']['size']==pytest.approx([10,10,10],abs=.01)
    assert design['constraintResults'][0]['passed']
    assert not any(i['interferes'] for i in design['interference'])
    assert sum(i['quantity'] for i in design['bom'])==2
    assert design['source']['plan']==job['plan']
    assert design['validation']['productionReady'] is False
    assert len(store.list('owner','f1'))==3
    assert jobs.submit('owner',request,from_ai=False)['id']==job['id']
    with pytest.raises(DesignError):jobs.submit('owner',{**request,'message':'different'},from_ai=False)
    with pytest.raises(DesignNotFound):jobs.get('other',job['id'])
    assert jobs.list('other')==[]
    assert jobs.list('owner','different')==[]
    assert 'workerPid' not in job and 'requestHash' not in job
    assert (jobs._path('owner',job['id']).parent/'base-execution.json').is_file()


def test_missing_dimension_is_durable_draft_without_replacement_geometry(tmp_path):
    store=CadDesignWorkspace(tmp_path/'designs');jobs=CadAssemblyJobs(store)
    plan=assembly_plan();plan['parts'][0]['plan']['parameters']['H']={'value':None,'question':'请提供板厚'}
    job=jobs.submit('owner',{'fileId':'f','message':'只知道宽度','plan':plan},from_ai=False,background=False)
    assert job['status']=='needs_input' and job['questions']
    assert job['plan']['parts'][0]['plan']['parameters']['H']['value'] is None
    assert store.list('owner')==[]
    assert job['designId'] is None


def test_ai_explicit_sources_required_and_mates_not_guessed():
    plan=assembly_plan()
    assert validate_assembly_plan(plan,message='宽 10、高 5',from_ai=True)
    with pytest.raises(NeedsAssemblyInput,match='缺少用户原文'):
        validate_assembly_plan(plan,message='随便做两块板',from_ai=True)
    plan['parts'][0]['plan']['features'][0]['size']=[10,10,5]
    with pytest.raises(NeedsAssemblyInput,match='直接尺寸'):
        validate_assembly_plan(plan,message='宽 10、高 5',from_ai=True)
    plan=assembly_plan();plan['constraints'][0]['a']={'kind':'face','index':0}
    with pytest.raises(NeedsAssemblyInput,match='猜测拓扑'):
        validate_assembly_plan(plan,message='宽 10、高 5',from_ai=True)


class FixtureProvider:
    provider_info={'mode':'test-provider','model':'test-model','reasoningEffort':'low'}
    def __init__(self,plan):self.plan=plan;self.calls=0
    def __call__(self,body,timeout):
        self.calls+=1
        assert 'cad-plan-v1' in body['instructions'] and body['store'] is False
        return {'output_text':json.dumps(self.plan),'usage':{'input_tokens':500000,'output_tokens':200000,
            'input_tokens_details':{'cached_tokens':100000},'output_tokens_details':{'reasoning_tokens':100000}}}


def test_real_metering_journal_policy_settlement_and_ai_questions(configured,tmp_path):
    billing,policy,admin,user,other=configured
    provider=FixtureProvider({'name':'Pending','questions':['请明确板厚和配合位置'],'parts':[]})
    jobs=CadAssemblyJobs(CadDesignWorkspace(tmp_path/'designs'),billing=billing,billing_policy=policy,provider=provider)
    data={'fileId':'f','message':'生成两块板并装配','requestId':'metered-once'}
    job=jobs.submit(user.id,data,background=False)
    assert job['status']=='needs_input' and provider.calls==1
    assert job['questions']==['请明确板厚和配合位置']
    assert jobs.submit(user.id,data)['id']==job['id'] and provider.calls==1
    usage=billing.list_records('usage',actor_id=admin.id)['items']
    assert len(usage)==1 and usage[0]['stage']=='assembly_planner'
    assert usage[0]['inputTokens']==500000 and usage[0]['cachedInputTokens']==100000
    assert billing.wallet(user.id)['creditUnits']==991
    with pytest.raises(DesignNotFound):jobs.get(other.id,job['id'])
    ledger=billing.list_records('ledger',owner_id=user.id)['items']
    assert len([r for r in ledger if r['kind']=='usage'])==1


def test_ai_actual_plan_drives_real_parts_and_preserves_user_source(configured,tmp_path):
    billing,policy,admin,user,other=configured
    provider=FixtureProvider(assembly_plan())
    store=CadDesignWorkspace(tmp_path/'designs')
    jobs=CadAssemblyJobs(store,billing=billing,billing_policy=policy,provider=provider)
    job=jobs.submit(user.id,{'fileId':'f','message':'两块宽 10、高 5 的方板。第一块固定，第二块的底面与第一块顶面重合。'},background=False)
    assert job['status']=='review_required',job
    assert provider.calls==1 and job['source']=='ai'
    assert store.get(user.id,job['designId'])['metrics']['volume']==pytest.approx(1000)
    assert len(billing.list_records('usage',actor_id=admin.id)['items'])==1
    assert billing.wallet(user.id)['creditUnits']==991


def test_ai_fails_closed_without_billing_or_funds(configured,tmp_path):
    billing,policy,admin,user,other=configured
    provider=FixtureProvider(assembly_plan());store=CadDesignWorkspace(tmp_path/'designs')
    with pytest.raises(AssemblyAdmissionError,match='尚未初始化'):
        CadAssemblyJobs(store,provider=provider).submit(user.id,{'fileId':'f','message':'example'})
    billing.adjust(admin.id,owner_id=user.id,credit_units=-1000,reason='TEST empty account',idempotency_key='empty')
    with pytest.raises(AssemblyAdmissionError):
        CadAssemblyJobs(store,billing=billing,billing_policy=policy,provider=provider).submit(user.id,{'fileId':'f','message':'example'})
    assert provider.calls==0
