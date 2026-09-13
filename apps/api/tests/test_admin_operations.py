"""Operations isolation with actual local stores; no model or payment requests."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
import json
import sqlite3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.admin_operations import AdminOperationsService
from app.admin_operations_api import create_admin_operations_router
from app.billing import BillingService
from app.billing_policy import BillingPolicyService
from app.cad_agent_store import CadRunStore
from app.platform import AuthService, AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.support import SupportService

SECRET = 'PRIVATE_PROMPT_SOURCE_PATH_SIGNED_URL'

@pytest.fixture
def setup(tmp_path):
    auth = AuthService(tmp_path/'platform.sqlite3',token_secret='operations-test-secret-'*3)
    users = {role:auth.create_user(role+'@example.test','test-password',role,roles=[role]) for role in ('admin','ops','finance','support','auditor','viewer')}
    users['other'] = auth.create_user('other@example.test','test-password','other',roles=['viewer'])
    services = SimpleNamespace(auth=auth)
    store = CadRunStore(tmp_path/'cad')
    billing = BillingService(auth.database,auth=auth)
    policy = BillingPolicyService(billing)
    support = SupportService(auth,cad_store=store)
    operations = AdminOperationsService(services,cad_store=store,billing=billing,billing_policy=policy)
    value = SimpleNamespace(auth=auth,users=users,services=services,store=store,billing=billing,policy=policy,support=support,ops=operations)
    yield value
    operations.close(); support.close(); billing.close(); auth.close()


def run(env,*,owner='viewer',status='failed',registered=False,revision=1,run_id=None,unsafe=False):
    run_id = run_id or 'cad_'+uuid4().hex
    identity = None
    if registered:
        identity,_ = env.ops.registry.claim(run_id=run_id,owner=env.users[owner].id,request_id='req_'+uuid4().hex,request_hash='hash')
    record = {'runId':run_id,'owner':env.users[owner].id,'revision':revision,'status':status,
              'createdAt':'2026-09-10T01:00:00Z','updatedAt':'2026-09-10T01:00:05Z',
              'completedAt':'2026-09-10T01:00:05Z' if status not in {'running','queued'} else None,
              'progress':{'stage':SECRET if unsafe else 'planning','errorCode':SECRET if unsafe else 'timeout'},
              'provider':{'name':SECRET if unsafe else 'codex-cli','model':'gpt-'+SECRET if unsafe else 'gpt-6-astra'},
              'plan':{'name':SECRET,'code':SECRET},'sourceFiles':[{'url':SECRET,'path':SECRET}],
              'errors':[{'message':SECRET}],'trace':[{'text':SECRET}], 'session':{'access_token':SECRET}}
    env.store.save(record,previous_revision=revision-1)
    if registered and status not in {'running','queued'}:
        env.ops.registry.finish(run_id,status)
    return run_id,identity


def test_legacy_latest_revision_union_and_customer_count(setup):
    e=setup
    legacy,_=run(e,status='failed')
    run(e,status='review_required',revision=2,run_id=legacy)
    active,_=run(e,status='running',registered=True)
    # A claimed task without a CAD record is also visible.
    claimed='cad_'+uuid4().hex
    e.ops.registry.claim(run_id=claimed,owner=e.users['viewer'].id,request_id='queued-only',request_hash='x')
    listed=e.ops.tasks(e.users['ops'].id)
    assert listed['total']==3
    assert {row['runId'] for row in listed['items']}=={legacy,active,claimed}
    latest=next(row for row in listed['items'] if row['runId']==legacy)
    assert latest['revision']==2 and latest['status']=='review_required' and not latest['canCancel']
    assert e.ops.customer(e.users['support'].id,e.users['viewer'].id)['customer']['taskCount']==3
    overview=e.ops.overview(e.users['ops'].id)
    assert overview['tasks']['total']==3 and overview['financial'] is None
    assert overview['tasks']['byStatus']['review_required']==1


def test_permissions_financial_masking_and_fresh_user_status(setup):
    e=setup
    rid,_=run(e)
    e.billing.adjust(e.users['admin'].id,owner_id=e.users['viewer'].id,credit_units=100,reason='TEST '+SECRET,idempotency_key='fund')
    for role in ('ops','support'):
        result=e.ops.customer(e.users[role].id,e.users['viewer'].id)
        assert result['financial'] is None and result['customer']['financial'] is None
        task=e.ops.task(e.users[role].id,rid)
        assert task['settlements'] is None and 'costMicroUsd' not in task['usage']
    finance=e.ops.customer(e.users['finance'].id,e.users['viewer'].id)
    assert finance['financial']['wallet']=={'balanceUnits':100,'dueUnits':0}
    assert finance['financial']['ledger'][0]['deltaUnits']==100
    assert SECRET not in json.dumps(finance)
    with pytest.raises(AuthorizationError): e.ops.task(e.users['finance'].id,rid)
    for method,args in [(e.ops.overview,()),(e.ops.customers,()),(e.ops.tasks,()),(e.ops.system,()),(e.ops.audit,())]:
        with pytest.raises(AuthorizationError): method(e.users['viewer'].id,*args)
    e.auth.set_active(e.users['ops'].id,False,actor_id=e.users['admin'].id)
    with pytest.raises(AuthorizationError): e.ops.tasks(e.users['ops'].id)


def test_safe_diagnostics_never_return_customer_content_or_arbitrary_codes(setup):
    e=setup
    rid,_=run(e,unsafe=True)
    ticket=e.support.create(e.users['viewer'].id,{'subject':SECRET,'category':'modeling','body':SECRET,'runId':rid,'drawingConsent':True,'idempotencyKey':'support-one'})
    results=[e.ops.task(e.users['admin'].id,rid),e.ops.customer(e.users['support'].id,e.users['viewer'].id)]
    assert SECRET not in json.dumps(results)
    assert results[0]['diagnostics']=={'phase':None,'errorCode':'unknown_error','errorCategory':'unknown','provider':None,'model':None,'retryCount':None,'providerAttempts':None,'traceEvents':1}
    assert results[1]['tickets'][0]['number']==ticket['number']
    assert e.ops.tasks(e.users['ops'].id,q=SECRET)['total']==0
    assert e.store.load(rid)['plan']['name']==SECRET


def test_owner_status_search_and_literal_wildcard_pagination(setup):
    e=setup
    first,_=run(e,status='failed'); second,_=run(e,status='ready'); run(e,owner='other',status='failed')
    for filters in ({'q':'viewer@example.test'},{'owner':e.users['viewer'].id}):
        result=e.ops.tasks(e.users['ops'].id,**filters,limit=1,offset=1)
        assert result['total']==2 and len(result['items'])==1
    assert e.ops.tasks(e.users['ops'].id,status='ready')['items'][0]['runId']==second
    assert e.ops.tasks(e.users['ops'].id,q=first)['total']==1
    assert e.ops.tasks(e.users['ops'].id,q='%')['total']==0
    assert e.ops.customers(e.users['support'].id,q='%')['total']==0
    assert e.ops.customers(e.users['support'].id,active=True,limit=1,offset=1)['total']==7
    assert e.ops.tasks(e.users['ops'].id,owner='missing')['total']==0
    with pytest.raises(ValidationError): e.ops.tasks(e.users['ops'].id,status='PRIVATE')
    with pytest.raises(ValidationError): e.ops.customers(e.users['support'].id,limit=0)
    with pytest.raises(NotFoundError): e.ops.customer(e.users['support'].id,'missing')


def test_attempt_usage_deduplicates_and_preserves_unknown_values(setup):
    e=setup
    rid,identity=run(e,registered=True,status='failed')
    job=identity['jobId']
    e.ops.registry.begin_call(call_id='call-known',owner=e.users['viewer'].id,job_id=job,attempt_id=rid,stage='planner',identity={'provider':'codex-cli','model':'gpt-6-astra'})
    e.ops.registry.finish_call('call-known',{'input_tokens':100,'cached_input_tokens':20,'output_tokens':40,'reasoning_output_tokens':10,'cost_micro_usd':500})
    e.billing.record_usage(owner_id=e.users['viewer'].id,job_id=job,attempt_id=rid,call_id='call-known',stage='planner',provider='codex-cli',model='gpt-6-astra',input_tokens=100,cached_input_tokens=20,output_tokens=40,reasoning_output_tokens=10,cost_micro_usd=500,cost_source='provider_reported')
    result=e.ops.task(e.users['auditor'].id,rid)['usage']
    assert result=={'scope':'attempt','calls':1,'unknownUsageCalls':0,'inputTokens':100,'cachedInputTokens':20,'outputTokens':40,'reasoningOutputTokens':10,'costMicroUsd':500,'unknownCostCalls':0}
    e.ops.registry.begin_call(call_id='call-unknown',owner=e.users['viewer'].id,job_id=job,attempt_id=rid,stage='planner',identity={})
    result=e.ops.task(e.users['auditor'].id,rid)['usage']
    assert result['calls']==2 and result['inputTokens'] is None and result['unknownUsageCalls']==1 and result['costMicroUsd'] is None
    assert 'costMicroUsd' not in e.ops.task(e.users['support'].id,rid)['usage']


def test_cancel_requires_reason_is_idempotent_and_never_fakes_terminal(setup):
    e=setup
    rid,_=run(e,status='running',registered=True)
    values={'reason':'客户要求暂停核查','idempotencyKey':'cancel-once'}
    first=e.ops.cancel(e.users['ops'].id,rid,values)
    replay=e.ops.cancel(e.users['ops'].id,rid,values)
    assert first['accepted'] and first['task']['status']=='cancel_requested' and not first['replayed']
    assert replay['replayed'] and replay['auditId']==first['auditId']
    assert e.store.load(rid)['status']=='running' and e.ops.registry.get(rid)['cancelRequested']
    with pytest.raises(ConflictError): e.ops.cancel(e.users['ops'].id,rid,{**values,'reason':'另外一个不同原因'})
    with pytest.raises(ValidationError): e.ops.cancel(e.users['ops'].id,rid,{**values,'ownerId':'forged'})
    with pytest.raises(ValidationError): e.ops.cancel(e.users['ops'].id,rid,{**values,'reason':'短'})
    with pytest.raises(AuthorizationError): e.ops.cancel(e.users['support'].id,rid,values)
    assert e.ops._cad.execute('SELECT count(*) FROM admin_operations_audit').fetchone()[0]==1
    with pytest.raises(sqlite3.IntegrityError): e.ops._cad.execute('DELETE FROM admin_operations_audit')
    e.ops._cad.rollback()


def test_cancel_concurrent_connections_one_audit_and_queued_capacity_preserved(setup):
    e=setup
    run(e,status='running',registered=True)
    rid,_=run(e,status='running',registered=True,owner='other')
    second=AdminOperationsService(e.services,cad_store=e.store,billing=e.billing,billing_policy=e.policy)
    barrier=Barrier(2)
    def stop(service):
        barrier.wait()
        return service.cancel(e.users['ops'].id,rid,{'reason':'重复请求正在停止','idempotencyKey':'same'})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(stop,[e.ops,second]))
        assert len({r['auditId'] for r in results})==1
        assert sum(r['replayed'] for r in results)==1
        assert e.ops.registry.get(rid)['status']=='queued'
        assert e.ops.tasks(e.users['ops'].id,status='cancel_requested')['total']==1
    finally: second.close()


def test_terminal_cancel_is_audited_noop_and_legacy_active_is_not_falsely_stopped(setup):
    e=setup
    terminal,_=run(e,status='failed')
    result=e.ops.cancel(e.users['ops'].id,terminal,{'reason':'核对结束任务状态','idempotencyKey':'ended'})
    assert not result['accepted'] and result['task']['status']=='failed'
    legacy,_=run(e,status='running')
    assert not e.ops.task(e.users['ops'].id,legacy)['task']['canCancel']
    with pytest.raises(ConflictError): e.ops.cancel(e.users['ops'].id,legacy,{'reason':'历史任务不能假停','idempotencyKey':'legacy'})


def test_system_reports_real_capabilities_and_missing_monitoring_without_paths(setup,monkeypatch):
    e=setup
    monkeypatch.setattr('app.admin_operations.kernel_status',lambda:{'available':True,'engine':'cadquery-occt','version':'test','executionProbe':False})
    monkeypatch.setattr('app.admin_operations.shutil.disk_usage',lambda path:SimpleNamespace(free=5,total=100))
    monkeypatch.setenv('JOYNIU_CAD_PROVIDER','codex')
    monkeypatch.setenv('JOYNIU_CAD_CODEX_MODEL','gpt-6-astra')
    monkeypatch.setenv('JOYNIU_LLM_REASONING_EFFORT','high')
    result=e.ops.system(e.users['ops'].id)
    assert result['configuration']=={'provider':'codex','model':'gpt-6-astra','reasoningEffort':'high','source':'runtime_configuration','externalProbe':False}
    assert result['databases']=={'platformReadable':True,'cadReadable':True}
    assert result['billing']['chargingEnabled'] is False
    assert result['disk']['freeBytes']==5 and result['disk']['scope']=='cad_workspace_filesystem'
    assert {a['code'] for a in result['alerts']}=={'low_disk','monitoring_unconfigured'}
    assert all(result[name]['available'] is False for name in ('backup','monitoring','externalNotifications'))
    assert str(e.store.root) not in json.dumps(result)
    monkeypatch.setenv('JOYNIU_CAD_CODEX_MODEL','gpt-'+SECRET)
    assert e.ops.system(e.users['ops'].id)['configuration']['model'] is None


def test_audit_merges_all_real_sources_masks_details_and_pages(setup):
    e=setup
    rid,_=run(e,status='running',registered=True)
    e.ops.cancel(e.users['ops'].id,rid,{'reason':SECRET,'idempotencyKey':'cancel-audit'})
    e.billing.adjust(e.users['admin'].id,owner_id=e.users['viewer'].id,credit_units=10,reason=SECRET,idempotency_key='audit-fund')
    e.support.create(e.users['viewer'].id,{'subject':SECRET,'category':'other','body':SECRET,'runId':None,'drawingConsent':False,'idempotencyKey':'support-audit'})
    result=e.ops.audit(e.users['auditor'].id,limit=100)
    assert {row['source'] for row in result['items']}=={'operations','accounts','billing','support'}
    assert SECRET not in json.dumps(result)
    assert e.ops.audit(e.users['auditor'].id,limit=2,offset=2)['items']==result['items'][2:4]
    filtered=e.ops.audit(e.users['auditor'].id,source='operations',actor_filter=e.users['ops'].id)
    assert filtered['total']==1 and filtered['items'][0]['reasonRecorded']
    with pytest.raises(AuthorizationError): e.ops.audit(e.users['ops'].id)
    with pytest.raises(ValidationError): e.ops.audit(e.users['auditor'].id,source='workspace')


@pytest.fixture
def client(setup):
    app=FastAPI()
    app.include_router(create_admin_operations_router(setup.services,cad_store=setup.store,service=setup.ops),prefix='/api/v1')
    with TestClient(app) as client:
        yield client


def headers(e,role):
    return {'Authorization':'Bearer '+e.auth.issue_token(e.users[role]).token}


@pytest.mark.parametrize('path',['overview','customers','tasks','tasks/cad_missing','system','audit'])
def test_each_get_route_requires_auth_and_capability(client,setup,path):
    assert client.get('/api/v1/admin/'+path).status_code==401
    assert client.get('/api/v1/admin/'+path,headers=headers(setup,'viewer')).status_code==403


def test_api_safe_shapes_filter_validation_and_body_limits(client,setup):
    e=setup
    rid,_=run(e,status='running',registered=True)
    base='/api/v1/admin'
    response=client.get(base+'/tasks',headers=headers(e,'ops'),params={'ownerId':e.users['viewer'].id,'limit':1})
    assert response.status_code==200 and response.headers['cache-control']=='no-store' and response.json()['total']==1
    assert client.get(base+'/tasks',headers=headers(e,'ops'),params={'limit':101}).status_code==422
    assert client.get(base+'/tasks',headers=headers(e,'ops'),params={'status':'unknown'}).status_code==422
    target=base+'/tasks/'+rid+'/cancel'
    assert client.post(target,json={'reason':'测试停止真实任务','idempotencyKey':'api'},headers=headers(e,'support')).status_code==403
    assert client.post(target,content='bad',headers=headers(e,'ops')).status_code==415
    assert client.post(target,content='x'*8193,headers={**headers(e,'ops'),'Content-Type':'application/json'}).status_code==413
    assert client.post(target,json={'reason':'测试停止真实任务','idempotencyKey':'api','ownerId':'forged'},headers=headers(e,'ops')).status_code==422
    first=client.post(target,json={'reason':'测试停止真实任务','idempotencyKey':'api'},headers=headers(e,'ops'))
    assert first.status_code==200 and first.json()['task']['status']=='cancel_requested'
    replay=client.post(target,json={'reason':'测试停止真实任务','idempotencyKey':'api'},headers=headers(e,'ops'))
    assert replay.json()['replayed'] and replay.json()['auditId']==first.json()['auditId']
    assert client.get(base+'/customers/'+e.users['viewer'].id,headers=headers(e,'finance')).json()['financialVisible']
    assert client.get(base+'/tasks/'+rid,headers=headers(e,'finance')).status_code==403


def test_historical_missing_usage_is_unknown_and_terminal_elapsed_does_not_grow(setup):
    e=setup
    rid,_=run(e,status='failed')
    record=e.store.load(rid)
    record.update(revision=2,completedAt=None)
    e.store.save(record,previous_revision=1)
    result=e.ops.task(e.users['auditor'].id,rid)
    assert result['usage']['calls'] is None and result['usage']['inputTokens'] is None and result['usage']['costMicroUsd'] is None
    assert result['task']['elapsedSeconds'] is None
    summary=e.ops.customer(e.users['finance'].id,e.users['viewer'].id)['financial']['usage']
    assert summary['calls'] is None and summary['costMicroUsd'] is None
    assert summary['scope']=='recorded_calls' and summary['historicalTasksWithoutJournal']==1


def test_customer_financial_records_are_owner_scoped_and_no_hidden_content(setup):
    e=setup
    for owner,amount in [('viewer',10),('other',35)]:
        e.billing.adjust(e.users['admin'].id,owner_id=e.users[owner].id,credit_units=amount,reason=SECRET,idempotency_key=owner)
    customer=e.ops.customer(e.users['finance'].id,e.users['viewer'].id)
    assert customer['financial']['wallet']['balanceUnits']==10
    assert [item['deltaUnits'] for item in customer['financial']['ledger']]==[10]
    assert SECRET not in json.dumps(customer)
    assert e.ops.overview(e.users['finance'].id)['financial']['balanceUnits']==45


def test_custom_auditor_without_finance_cannot_read_billing_source(setup,monkeypatch):
    from app.platform import User
    original=User.permissions.fget
    monkeypatch.setattr(User,'permissions',property(lambda user: frozenset({'admin:audit'}) if user.id==setup.users['auditor'].id else original(user)))
    result=setup.ops.audit(setup.users['auditor'].id)
    assert all(item['source']!='billing' for item in result['items'])
    with pytest.raises(AuthorizationError): setup.ops.audit(setup.users['auditor'].id,source='billing')


def test_disabled_token_and_role_downgrade_cannot_keep_operations_access(client,setup):
    e=setup
    token=headers(e,'ops')
    e.auth.assign_roles(e.users['ops'].id,['viewer'],actor_id=e.users['admin'].id)
    assert client.get('/api/v1/admin/tasks',headers=token).status_code==403
    support_token=headers(e,'support')
    e.auth.set_active(e.users['support'].id,False,actor_id=e.users['admin'].id)
    assert client.get('/api/v1/admin/customers',headers=support_token).status_code in (401,403)
    assert client.get('/api/v1/admin/tasks').headers['cache-control']=='no-store'


@pytest.mark.parametrize('content',['[]','{"reason":NaN,"idempotencyKey":"x"}','{"reason":'])
def test_malformed_cancel_json_cannot_create_audit(client,setup,content):
    rid,_=run(setup,status='running',registered=True)
    response=client.post('/api/v1/admin/tasks/'+rid+'/cancel',content=content,headers={**headers(setup,'ops'),'Content-Type':'application/json'})
    assert response.status_code==422 and response.headers['cache-control']=='no-store'
    assert setup.ops._cad.execute('SELECT count(*) FROM admin_operations_audit').fetchone()[0]==0


def test_detail_read_audits_only_safe_access_metadata_and_denials_write_nothing(setup):
    e=setup
    rid,_=run(e)
    e.ops.customer(e.users['support'].id,e.users['viewer'].id)
    e.ops.task(e.users['auditor'].id,rid)
    with pytest.raises(AuthorizationError): e.ops.task(e.users['finance'].id,rid)
    rows=e.ops._cad.execute('SELECT actor_id,action,target_id,reason,financial_visible FROM admin_operations_audit ORDER BY created_at').fetchall()
    assert [tuple(row) for row in rows]==[(e.users['support'].id,'customer.detail.read',e.users['viewer'].id,'',0),(e.users['auditor'].id,'task.detail.read',rid,'',1)]
    assert SECRET not in str([tuple(row) for row in rows])


def revised_diagnostics_record(e, **changes):
    rid,_=run(e,status='failed')
    record=e.store.load(rid)
    record.update(revision=2,progress={},errors=[],trace=[],provider={})
    record.update(changes)
    e.store.save(record,previous_revision=1)
    return rid


def test_legacy_metrics_report_actual_retries_attempts_and_last_known_error(setup):
    e=setup
    rid=revised_diagnostics_record(e,providerMetrics={'retryCount':2,'attempts':8,'lastErrorKind':'timeout','requestBody':SECRET},
        trace=[{'stage':'planning','message':SECRET},{'action':'execute_plan','message':SECRET},{'message':SECRET}])
    result=e.ops.task(e.users['ops'].id,rid)
    assert result['diagnostics']['retryCount']==2 and result['diagnostics']['providerAttempts']==8
    assert result['diagnostics']['errorCode']=='timeout' and result['diagnostics']['errorCategory']=='timeout'
    assert result['diagnostics']['phase']=='execute_plan' and result['task']['stage']=='execute_plan'
    assert result['task']['status']=='failed' and SECRET not in json.dumps(result)


def test_current_provider_and_legacy_history_errors_have_explicit_safe_fallbacks(setup):
    e=setup
    current=revised_diagnostics_record(e,provider={'name':'codex-cli','model':'gpt-6-astra','retryCount':0,'attempts':5,'lastErrorCode':'http_429'},trace=[{'stage':'provider_retry'}])
    result=e.ops.task(e.users['ops'].id,current)
    assert result['diagnostics']['retryCount']==0 and result['diagnostics']['providerAttempts']==5
    assert result['diagnostics']['errorCode']=='http_429' and result['diagnostics']['errorCategory']=='provider'
    historical=revised_diagnostics_record(e,providerMetrics={'history':[{'errorKind':'timeout','message':SECRET},{'code':'incomplete','body':SECRET}]},trace=[{'action':'provider_error','code':'incomplete'}])
    result=e.ops.task(e.users['ops'].id,historical)
    assert result['diagnostics']['errorCode']=='incomplete' and result['diagnostics']['phase']=='provider_error'
    assert result['diagnostics']['retryCount'] is None and result['diagnostics']['providerAttempts'] is None
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('bad',[True,-1,1_000_001,1.5,'2',{},[]])
def test_diagnostic_counters_never_coerce_invalid_values_or_infer_retries(setup,bad):
    e=setup
    rid=revised_diagnostics_record(e,providerMetrics={'retryCount':bad,'attempts':bad,'lastErrorKind':SECRET},provider={'retryCount':bad,'attempts':bad,'lastErrorCode':SECRET},trace=[SECRET,{'stage':SECRET,'action':SECRET,'code':SECRET}])
    result=e.ops.task(e.users['ops'].id,rid)
    assert result['diagnostics']['retryCount'] is None and result['diagnostics']['providerAttempts'] is None
    assert result['diagnostics']['phase'] is None and result['diagnostics']['errorCode']=='unknown_error'
    assert SECRET not in json.dumps(result)


def test_journal_call_count_does_not_claim_retry_count(setup):
    e=setup
    rid,identity=run(e,registered=True,status='failed')
    for index in range(3):
        e.ops.registry.begin_call(call_id='independent-'+str(index),owner=e.users['viewer'].id,job_id=identity['jobId'],attempt_id=rid,stage='planner',identity={})
    result=e.ops.task(e.users['ops'].id,rid)
    assert result['usage']['calls']==3 and result['diagnostics']['retryCount'] is None
    assert result['diagnostics']['providerAttempts'] is None


def test_real_legacy_provider_mode_attempts_progress_error_and_elapsed_fallback(setup):
    e=setup
    rid=revised_diagnostics_record(e,completedAt=None,elapsedSeconds=367.25,
        provider={'mode':'remote','attempts':13,'model':'gpt-6-astra'},
        progress={'provider':{'lastErrorKind':'timeout','message':SECRET},'message':SECRET},
        trace=[{'status':'failed','message':SECRET}])
    result=e.ops.task(e.users['ops'].id,rid)
    assert result['diagnostics']['provider']=='remote' and result['diagnostics']['providerAttempts']==13
    assert result['diagnostics']['retryCount'] is None and result['diagnostics']['errorCode']=='timeout'
    assert result['task']['elapsedSeconds']==367.25 and result['diagnostics']['phase']=='failed'
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('value',[True,-1,'10',1_000_000_001])
def test_elapsed_fallback_requires_a_bounded_json_number(setup,value):
    rid=revised_diagnostics_record(setup,completedAt=None,elapsedSeconds=value)
    assert setup.ops.task(setup.users['ops'].id,rid)['task']['elapsedSeconds'] is None
