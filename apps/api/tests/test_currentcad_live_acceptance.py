"""Pure CLI/HTTP-contract checks: no model/provider, account or live request."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

spec=importlib.util.spec_from_file_location('live_acceptance',Path(__file__).resolve().parents[3]/'deploy/currentcad_live_acceptance.py')
live=importlib.util.module_from_spec(spec);spec.loader.exec_module(live)


def args(tmp_path):
    return SimpleNamespace(deadline_seconds=900,output_dir=str(tmp_path/'output'),base_url='http://127.0.0.1:8010',
        database=str(tmp_path/'accounts.sqlite3'),cad_root=str(tmp_path/'cad-agent'))


def test_default_contract_check_has_no_network_or_database_writes(monkeypatch):
    monkeypatch.setattr(live.Client,'call',lambda *_a,**_k:pytest.fail('unexpected HTTP call'))
    result=live.contract_check()
    assert result['liveCalls']==0 and result['databaseWrites']==0
    assert result['maxJobs']==1 and result['maxClarifications']==1


@pytest.mark.parametrize('url',['http://example.com:8010','http://user:password@localhost:8010','http://127.0.0.1:8010/foreign','http://127.0.0.1:8010?access=secret'])
def test_only_loopback_without_credentials_or_paths_is_allowed(url):
    with pytest.raises(live.AcceptanceFailure):live.loopback_base(url)


def test_box_confirmation_requires_independent_exact_solid_and_faces():
    expected={'valid':True,'solidCount':1,'faceCount':6,'allPlanar':True,'size':[40,20,10],'volumeMm3':8000}
    for changed in ({'valid':False},{'solidCount':2},{'faceCount':7},{'allPlanar':False},{'size':[20,40,10]},{'volumeMm3':7990}):
        with pytest.raises(live.AcceptanceFailure):live.validate_box({**expected,**changed})


def test_sse_first_identity_is_bounded_and_no_signed_url_is_needed():
    value={'runId':'cad_'+'a'*32,'jobId':'job_example','status':'queued','revision':1}
    stream=io.BytesIO(b': keepalive\n\nevent: progress\ndata: '+live.encode(value)+b'\n\n')
    assert live.read_sse_identity(stream)==value
    with pytest.raises(live.AcceptanceFailure):live.read_sse_identity(io.BytesIO(b'data: '+b'a'*256001))


def test_uncertain_submit_queries_request_without_second_post(tmp_path):
    acceptance=live.Acceptance(args(tmp_path));calls=[]
    value={'runId':'cad_'+'a'*32,'jobId':'job_one','revision':1,'status':'running'}
    def call(path,**kwargs):
        calls.append((path,kwargs))
        if path.endswith('/run'):raise live.AcceptanceFailure('http_transport_uncertain')
        return value
    acceptance.client=SimpleNamespace(call=call)
    assert acceptance.submit()==value
    assert len([item for item in calls if item[1].get('method')=='POST'])==1
    assert calls[1][0].endswith(acceptance.report['requests'][0])
    assert 'password' not in (acceptance.output/'report.json').read_text()
    with pytest.raises(live.AcceptanceFailure):acceptance.submit()


def test_one_clarification_preserves_job_and_has_no_plan_override(tmp_path):
    acceptance=live.Acceptance(args(tmp_path));calls=[]
    responses=[{'runId':'cad_'+'a'*32,'jobId':'job_one','revision':1,'status':'needs_input'},
               {'runId':'cad_'+'b'*32,'jobId':'job_one','revision':1,'status':'running'}]
    def call(path,**kwargs):calls.append(kwargs);return responses[len(calls)-1]
    acceptance.client=SimpleNamespace(call=call)
    parent=acceptance.submit();acceptance.submit(parent)
    state=json.loads(calls[1]['form']['modelState'])
    assert state=={'agentRun':{'runId':parent['runId'],'revision':1}}
    assert calls[1]['form']['message']==live.CLARIFICATION
    with pytest.raises(live.AcceptanceFailure):acceptance.submit(parent)
    with pytest.raises(live.AcceptanceFailure):acceptance.remember({**responses[1],'jobId':'another_job'})


def test_candidate_file_check_enforces_owner_and_run_directory(tmp_path):
    root=tmp_path/'cad';run_id='cad_'+'a'*32;(root/run_id).mkdir(parents=True)
    path=root/run_id/'model.step';path.write_bytes(b'fixture only')
    record={'runId':run_id,'owner':'qa','artifacts':{'step':{'path':str(path)}}}
    assert live.owned_candidate_step(record,'qa',root)==path.resolve()
    with pytest.raises(live.AcceptanceFailure):live.owned_candidate_step(record,'other',root)
    outside=tmp_path/'other.step';outside.write_bytes(b'fixture only')
    record['artifacts']['step']['path']=str(outside)
    with pytest.raises(live.AcceptanceFailure):live.owned_candidate_step(record,'qa',root)


def test_failure_queries_and_cancels_only_this_invocations_active_request(tmp_path,monkeypatch):
    acceptance=live.Acceptance(args(tmp_path));acceptance.report.update(status='blocked',requests=['own-request'])
    acceptance.owner='qa';calls=[]
    value={'runId':'cad_'+'a'*32,'jobId':'job_one','revision':1,'status':'running'}
    def call(path,**kwargs):
        calls.append(path)
        return {**value,'status':'cancel_requested' if path.endswith('/cancel') else 'running'}
    acceptance.client=SimpleNamespace(call=call,token='memory-only-token',deadline=time.monotonic()+900)
    monkeypatch.setattr(live,'usage_report',lambda *_:{'calls':[],'supplierCostKnown':False})
    report=acceptance.finish()
    assert calls==['/api/v1/cad-agent/jobs/by-request/own-request','/api/v1/cad-agent/jobs/by-request/own-request/cancel']
    assert report['jobId']=='job_one' and report['runs'][0]['status']=='cancel_requested'
    assert 'memory-only-token' not in (acceptance.output/'report.json').read_text()
