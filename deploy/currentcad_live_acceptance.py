#!/usr/bin/env python3
"""One synthetic live-Codex acceptance job, run explicitly inside the API container.

Default: pure contract check, no network/database writes/provider calls.
Live example (a NEW output directory is required):
  python /tmp/currentcad_live_acceptance.py --execute \
    --output-dir /tmp/currentcad-live-20260912 --deadline-seconds 900

Uses existing JOYNIU_DB / JOYNIU_CAD_AGENT_DIR and HTTP localhost:8010. Never
changes provider, wallet, pricing, terms, credentials or another user's data.
A needs_input continuation is limited to one, bound to the same jobId. Run POSTs
are never blindly retried. Synthetic engineering confirmation is not customer
manufacturing release. Password and bearer token exist only in process memory.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4
import zipfile

PROMPT = ("这是独立合成工程验收件，不是客户制造任务。请用当前真实 CAD Agent 创建一个单一、封闭、有效的长方体。"
          "所有尺寸已经确定：单位毫米，X 方向长度 40，Y 方向宽度 20，Z 方向高度 10。"
          "没有孔、圆角、倒角、空腔或其他特征。目标体积 8000 mm³。"
          "请直接完成真实建模、尺寸检查和三维预览，保留人工确认步骤；不要使用示例/替代模型。")
CLARIFICATION = ("补充并确认：只需一个完整实心长方体，X=40 mm、Y=20 mm、Z=10 mm，"
                 "无孔、无圆角、无倒角、无空腔，体积8000 mm³。位置可取中心原点。"
                 "无需其他结构或尺寸。请基于本任务继续真实建模并完成尺寸检查，等待工程测试确认。")
ACTIVE = {"running", "queued", "cancel_requested"}
ID = re.compile(r"cad_[a-f0-9]{32}\Z")


class AcceptanceFailure(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def api_root():
    for candidate in (Path(__file__).resolve().parents[1]/'apps/api', Path.cwd()):
        if (candidate/'app/platform.py').is_file():
            sys.path.insert(0, str(candidate))
            return candidate.resolve()
    spec = importlib.util.find_spec('app')
    if not spec or not spec.origin:
        raise AcceptanceFailure('api_runtime_not_found')
    return Path(spec.origin).resolve().parents[1]


def loopback_base(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1','localhost','::1'}
            or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment):
        raise AcceptanceFailure('only_loopback_api_is_allowed')
    return value.rstrip('/')


def validate_box(value):
    if (value.get('valid') is not True or value.get('solidCount') != 1 or value.get('faceCount') != 6
            or value.get('allPlanar') is not True or len(value.get('size', [])) != 3
            or any(not math.isclose(actual, target, rel_tol=1e-6, abs_tol=1e-5) for actual, target in zip(value['size'],[40,20,10]))
            or not math.isclose(value.get('volumeMm3',0),8000,rel_tol=1e-6,abs_tol=1e-4)):
        raise AcceptanceFailure('synthetic_box_geometry_mismatch')
    return value


def read_sse_identity(response):
    size = 0
    for _ in range(100):
        line = response.readline(256_001)
        size += len(line)
        if not line or size > 1_000_000 or len(line) > 256_000:
            break
        if line.startswith(b'data:'):
            value = json.loads(line[5:].strip())
            if isinstance(value, dict) and ID.fullmatch(str(value.get('runId',''))):
                return value
    raise AcceptanceFailure('run_submission_identity_unknown')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base, deadline, token=''):
        self.base, self.deadline, self.token = loopback_base(base), deadline, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, *, method='GET', value=None, form=None, stream=False, binary=False, timeout=30):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise AcceptanceFailure('deadline_reached')
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise AcceptanceFailure('invalid_api_path')
        headers = {'Accept':'text/event-stream' if stream else 'application/octet-stream' if binary else 'application/json'}
        if self.token:
            headers['Authorization']='Bearer '+self.token
        payload = None
        if value is not None:
            payload=encode(value);headers['Content-Type']='application/json'
        elif form is not None:
            payload=urllib.parse.urlencode(form).encode();headers['Content-Type']='application/x-www-form-urlencoded'
        request=urllib.request.Request(self.base+path,data=payload,headers=headers,method=method)
        try:
            with self.opener.open(request,timeout=max(.1,min(timeout,remaining))) as response:
                if stream and 'text/event-stream' in response.headers.get('Content-Type',''):
                    return read_sse_identity(response)
                limit=50*1024*1024 if binary else 2*1024*1024
                data=response.read(limit+1)
                if len(data)>limit:
                    raise AcceptanceFailure('http_response_too_large')
                return data if binary else json.loads(data)
        except urllib.error.HTTPError as exc:
            # Do not print upstream bodies, URLs (possibly signed), passwords or tokens.
            raise AcceptanceFailure('http_'+str(exc.code)) from None
        except (urllib.error.URLError,TimeoutError,ConnectionError,OSError):
            raise AcceptanceFailure('http_transport_uncertain') from None


def read_run(database,owner,run_id,revision=None):
    if not ID.fullmatch(str(run_id)):
        raise AcceptanceFailure('invalid_run_id')
    with closing(sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        row=(db.execute('SELECT payload FROM cad_runs WHERE owner=? AND run_id=? ORDER BY revision DESC LIMIT 1',(owner,run_id)).fetchone()
             if revision is None else db.execute('SELECT payload FROM cad_runs WHERE owner=? AND run_id=? AND revision=?',(owner,run_id,revision)).fetchone())
    if not row:
        raise AcceptanceFailure('owned_run_not_persisted')
    return json.loads(row[0])


def owned_candidate_step(record,owner,cad_root):
    if record.get('owner')!=owner or not ID.fullmatch(str(record.get('runId'))):
        raise AcceptanceFailure('candidate_owner_mismatch')
    path=Path((record.get('artifacts') or {}).get('step',{}).get('path','')).resolve()
    boundary=Path(cad_root).resolve()/record['runId']
    if not path.is_relative_to(boundary) or not path.is_file() or not 0<path.stat().st_size<=32*1024*1024:
        raise AcceptanceFailure('owned_candidate_step_unavailable')
    return path


def inspect_step(path,deadline):
    seconds=min(40,deadline-time.monotonic())
    if seconds<1:
        raise AcceptanceFailure('deadline_reached')
    environment={key:value for key,value in os.environ.items() if key in {'PATH','TMPDIR','TEMP','TMP','LANG','LC_ALL','SYSTEMROOT'}}
    environment.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--inspect-step',str(path)],
            cwd=str(api_root()),env=environment,capture_output=True,timeout=seconds)
    except subprocess.TimeoutExpired:
        raise AcceptanceFailure('geometry_check_timeout') from None
    if result.returncode:
        raise AcceptanceFailure('geometry_check_failed')
    try:
        return validate_box(json.loads(result.stdout))
    except (ValueError,TypeError,KeyError):
        raise AcceptanceFailure('geometry_check_invalid_response') from None


def inspect_child(path):
    api_root()
    from app.cad_executor import _apply_worker_limits
    _apply_worker_limits(35,4096*1024*1024)
    import cadquery as cq
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    shape=cq.importers.importStep(str(path)).val()
    bounds=Bnd_Box();BRepBndLib.AddOptimal_s(shape.wrapped,bounds,False,False)
    values=bounds.Get()
    return {'valid':shape.isValid(),'solidCount':len(shape.Solids()),'faceCount':len(shape.Faces()),
            'allPlanar':all(face.geomType()=='PLANE' for face in shape.Faces()),
            'size':[values[i+3]-values[i] for i in range(3)],'volumeMm3':shape.Volume()}


def usage_report(cad_database,platform_database,owner,job_id):
    result={'calls':[],'chargedCreditUnits':None,'scope':'synthetic QA account and one job only'}
    with closing(sqlite3.connect(Path(cad_database).as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT call_id,stage,identity_json,completed_at,usage_json FROM cad_provider_calls WHERE owner=? AND job_id=? ORDER BY started_at',(owner,job_id)).fetchall()
    for row in rows:
        identity=json.loads(row['identity_json']);usage=json.loads(row['usage_json'] or '{}')
        result['calls'].append({'callId':row['call_id'],'stage':row['stage'],'complete':row['completed_at'] is not None,
            'provider':identity.get('provider',identity.get('mode')),'model':identity.get('model'),
            **{key:usage.get(key) for key in ('input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens','cost_micro_usd','outcome')}})
    for field in ('input_tokens','output_tokens','cost_micro_usd'):
        values=[item[field] for item in result['calls']]
        result[field]=sum(values) if values and all(isinstance(value,int) for value in values) else None
    result['supplierCostKnown']=bool(result['calls']) and result['cost_micro_usd'] is not None
    try:
        with closing(sqlite3.connect(Path(platform_database).as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            row=db.execute("SELECT -COALESCE(SUM(delta_units),0) FROM billing_ledger WHERE owner_id=? AND reference_id=? AND kind='usage'",(owner,job_id)).fetchone()
            result['chargedCreditUnits']=row[0]
    except sqlite3.OperationalError:
        pass
    return result


class Acceptance:
    def __init__(self,args):
        self.args=args;self.started=time.monotonic();self.hard_deadline=self.started+args.deadline_seconds
        self.deadline=self.hard_deadline-15
        self.output=Path(args.output_dir).resolve();self.output.mkdir(parents=True,exist_ok=False,mode=0o700)
        self.client=Client(args.base_url,self.deadline)
        self.report={'scope':'synthetic live-model engineering acceptance; NOT customer manufacturing release',
            'startedAt':datetime.now(timezone.utc).isoformat(),'status':'running','requests':[],'runs':[],
            'providerConfigurationChanged':False,'billingConfigurationChanged':False,'customerAccountsUsed':False}
        self.owner=None;self.job_id=None;self.latest=None
        self.report['limits']={'jobs':1,'initialSubmissions':1,'clarifications':1,'deadlineSeconds':args.deadline_seconds}
        self.database=Path(args.database).resolve();self.cad_root=Path(args.cad_root).resolve()
        self.cad_database=self.cad_root/'runs.sqlite3'
        self.checkpoint()

    def checkpoint(self):
        self.report['elapsedSeconds']=round(time.monotonic()-self.started,3)
        path=self.output/'report.json';temporary=self.output/'report.tmp'
        temporary.write_bytes(encode(self.report)+b'\n');temporary.chmod(0o600);temporary.replace(path)

    def remember(self,value):
        if not ID.fullmatch(str(value.get('runId',''))):
            raise AcceptanceFailure('invalid_run_identity')
        job_id=value.get('jobId')
        if self.job_id and job_id and self.job_id!=job_id:
            raise AcceptanceFailure('continuation_created_another_job')
        self.job_id=self.job_id or job_id
        self.latest=value
        safe={key:value.get(key) for key in ('runId','jobId','revision','status')}
        old=next((item for item in self.report['runs'] if item['runId']==safe['runId']),None)
        if old:old.update(safe)
        else:self.report['runs'].append(safe)
        self.checkpoint()
        return value

    def submit(self,parent=None):
        if len(self.report['requests'])>=2 or (self.report['requests'] and parent is None):
            raise AcceptanceFailure('task_limit_reached')
        request_id='liveqa_'+uuid4().hex;self.report['requests'].append(request_id);self.checkpoint()
        form={'requestId':request_id,'message':CLARIFICATION if parent else PROMPT,'history':'[]',
              'modelState':json.dumps({'agentRun':{'runId':parent['runId'],'revision':parent['revision']}} if parent else {})}
        try:
            value=self.client.call('/api/v1/cad-agent/run',method='POST',form=form,stream=True)
        except AcceptanceFailure as exc:
            if str(exc) not in {'http_transport_uncertain','run_submission_identity_unknown'}:
                raise
            # Query the original request; never repeat a chargeable POST.
            until=min(self.deadline,time.monotonic()+30)
            while True:
                try:
                    value=self.client.call('/api/v1/cad-agent/jobs/by-request/'+request_id)
                    if value.get('runId'):break
                except AcceptanceFailure as query_error:
                    if str(query_error) not in {'http_404','http_transport_uncertain'}:raise
                if time.monotonic()>=until:raise AcceptanceFailure('submission_uncertain_no_resubmit')
                time.sleep(min(2,max(0,until-time.monotonic())))
        return self.remember(value)

    def wait(self,value):
        while value.get('status') in ACTIVE:
            if time.monotonic()>=self.deadline-100:
                raise AcceptanceFailure('model_deadline_reached')
            time.sleep(min(2,max(.1,self.deadline-time.monotonic())))
            try:
                value=self.client.call('/api/v1/cad-agent/runs/'+value['runId'])
            except AcceptanceFailure as exc:
                if str(exc)=='http_transport_uncertain':continue
                raise
            self.remember(value)
        return value

    def run(self):
        if not self.database.is_file() or not self.cad_database.is_file():
            raise AcceptanceFailure('existing_api_databases_required')
        provider=(self.client.call('/api/v1/ai/status').get('cadProvider') or {})
        self.report['provider']={key:provider.get(key) for key in ('mode','model','configured','binaryAvailable')}
        if provider.get('mode')!='codex' or provider.get('configured') is not True or provider.get('binaryAvailable') is not True:
            raise AcceptanceFailure('existing_codex_provider_not_ready')
        # The only direct application mutation is creation of a fresh synthetic user.
        from app.platform import AuthService
        auth=AuthService(self.database)
        password=secrets.token_urlsafe(32)
        try:
            email='qa-currentcad-'+uuid4().hex+'@qa.invalid'
            user=auth.create_user(email,password,'发布合成模型验收',roles=['designer'],actor_id='synthetic-live-acceptance')
            self.owner=user.id;self.report['qaAccountId']=user.id
        finally:auth.close()
        login=self.client.call('/api/v1/auth/login',method='POST',value={'email':email,'password':password})
        password=None
        self.client.token=login['access_token'];login=None
        if self.client.call('/api/v1/auth/me').get('id')!=self.owner:
            raise AcceptanceFailure('qa_login_identity_mismatch')
        value=self.wait(self.submit())
        if value.get('status')=='needs_input':
            value=self.wait(self.submit(value))
        if value.get('status') not in {'review_required','ready'}:
            self.report['terminalStatus']=value.get('status')
            self.report['questionCount']=len(value.get('questions') or [])
            raise AcceptanceFailure('model_not_ready_for_engineering_confirmation')
        candidate=read_run(self.cad_database,self.owner,value['runId'],value['revision'])
        candidate_path=owned_candidate_step(candidate,self.owner,self.cad_root)
        self.report['beforeConfirmation']=inspect_step(candidate_path,self.deadline)
        self.report['candidateSha256']=sha(candidate_path.read_bytes());self.checkpoint()
        if value.get('status')!='ready':
            # No parameter overrides: only the independently measured synthetic box is confirmed.
            try:
                value=self.client.call('/api/v1/cad-agent/confirm',method='POST',
                    value={'runId':value['runId'],'revision':value['revision'],'parameters':{}},timeout=90)
            except AcceptanceFailure as exc:
                if str(exc)!='http_transport_uncertain':raise
                value=self.client.call('/api/v1/cad-agent/runs/'+value['runId'])
                if value.get('status')!='ready':raise AcceptanceFailure('confirmation_uncertain_no_repeat')
            self.remember(value)
        if value.get('status')!='ready':raise AcceptanceFailure('confirmation_not_ready')
        artifact=next((item for item in value.get('artifacts',[]) if item.get('format')=='step'),None)
        if not artifact or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',artifact.get('id','')):
            raise AcceptanceFailure('confirmed_step_unavailable')
        # Construct a bearer-authenticated path, discarding signed URLs and query tokens.
        download_path=f"/api/v1/cad-agent/runs/{value['runId']}/{value['revision']}/artifacts/{artifact['id']}"
        payload=self.client.call(download_path,binary=True)
        step=self.output/'synthetic-box.step';step.write_bytes(payload)
        self.report['step']={'name':step.name,'sha256':sha(payload),'bytes':len(payload),'geometry':inspect_step(step,self.deadline)}
        delivery_request={'requestId':'liveqa_delivery_'+uuid4().hex,'title':'合成验收 40×20×10 mm 长方体',
            'notes':'仅对已测量的合成件作工程测试确认，不代表任何客户制造放行。',
            'sourceRefs':[{'kind':'cad_run','id':value['runId'],'revision':value['revision']}]}
        delivery=self.client.call('/api/cad/deliveries',method='POST',value=delivery_request,timeout=100)
        archive=self.client.call('/api/cad/deliveries/'+delivery['id']+'/archive',binary=True)
        if sha(archive)!=delivery['archive']['sha256']:raise AcceptanceFailure('delivery_zip_hash_mismatch')
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            manifest=zipped.read('manifest.json')
            if zipped.testzip() or sha(manifest)!=delivery['manifest']['sha256']:raise AcceptanceFailure('delivery_manifest_mismatch')
            if any(item['kind']!='cad_run' or item['id']!=value['runId'] for item in delivery['sources']):
                raise AcceptanceFailure('unexpected_delivery_source')
            if not any(item['sha256']==sha(payload) and item['name'].endswith('.step') for item in delivery['files']):
                raise AcceptanceFailure('delivery_does_not_contain_confirmed_step')
        (self.output/'synthetic-delivery.zip').write_bytes(archive)
        self.report['delivery']={key:delivery[key] for key in ('id','manifest','archive','verification')}
        # Fresh DB connection and a new HTTP client verify persisted state independently.
        persisted=read_run(self.cad_database,self.owner,value['runId'])
        verifier=Client(self.args.base_url,self.deadline,self.client.token)
        reread=verifier.call('/api/cad/deliveries/'+delivery['id'])
        persisted_zip=verifier.call('/api/cad/deliveries/'+delivery['id']+'/archive',binary=True)
        if persisted['status']!='ready' or persisted['revision']!=value['revision'] or reread!=delivery or sha(persisted_zip)!=sha(archive):
            raise AcceptanceFailure('persistence_recheck_failed')
        self.report['persistence']={'runRevision':persisted['revision'],'runStatus':persisted['status'],
            'planSha256':sha(encode(persisted['plan'])),'deliveryReopened':True,'zipHashMatches':True}
        self.report['status']='passed';self.checkpoint()

    def finish(self):
        if self.report['status']!='passed' and self.client.token:
            # Cleanup is restricted to this invocation's own request IDs and reserved time.
            self.client.deadline=self.hard_deadline-8
            for request_id in self.report['requests']:
                if time.monotonic()>=self.client.deadline:break
                current=None
                try:
                    current=self.client.call('/api/v1/cad-agent/jobs/by-request/'+request_id,timeout=3)
                    if current.get('runId'):self.remember(current)
                except AcceptanceFailure:pass
                if current is None or current.get('status') in ACTIVE:
                    try:
                        cancelled=self.client.call('/api/v1/cad-agent/jobs/by-request/'+request_id+'/cancel',method='POST',value={},timeout=3)
                        if cancelled.get('runId'):self.remember(cancelled)
                    except AcceptanceFailure:pass
        if self.owner and self.job_id:
            try:self.report['usage']=usage_report(self.cad_database,self.database,self.owner,self.job_id)
            except (sqlite3.Error,ValueError):self.report['usage']={'available':False,'supplierCostKnown':False}
        self.report['jobId']=self.job_id;self.checkpoint()
        self.client.token=''
        return self.report


def contract_check():
    assert loopback_base('http://127.0.0.1:8010')=='http://127.0.0.1:8010'
    try:loopback_base('https://example.com')
    except AcceptanceFailure:pass
    else:raise AssertionError('external host accepted')
    expected={'valid':True,'solidCount':1,'faceCount':6,'allPlanar':True,'size':[40,20,10],'volumeMm3':8000}
    validate_box(expected)
    try:validate_box({**expected,'volumeMm3':7999})
    except AcceptanceFailure:pass
    else:raise AssertionError('bad box accepted')
    run={'runId':'cad_'+'a'*32,'revision':1,'status':'running'}
    assert read_sse_identity(io.BytesIO(b'event: progress\ndata: '+encode(run)+b'\n\n'))==run
    return {'contractVerified':True,'liveCalls':0,'databaseWrites':0,'maxJobs':1,'maxInitialSubmissions':1,'maxClarifications':1}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Explicitly run the live synthetic acceptance; may consume existing upstream quota.')
    parser.add_argument('--contract-check',action='store_true')
    parser.add_argument('--inspect-step',help=argparse.SUPPRESS)
    parser.add_argument('--base-url',default='http://127.0.0.1:8010')
    parser.add_argument('--database')
    parser.add_argument('--cad-root')
    parser.add_argument('--output-dir')
    parser.add_argument('--deadline-seconds',type=int,default=900)
    args=parser.parse_args()
    if args.inspect_step:
        try:print(json.dumps(inspect_child(args.inspect_step)));return 0
        except Exception:print('{"valid":false}');return 2
    if not args.execute:
        print(json.dumps(contract_check(),ensure_ascii=False));return 0
    if args.contract_check or not args.output_dir or not 150<=args.deadline_seconds<=900:
        parser.error('--execute requires a NEW --output-dir and a deadline of 150–900 seconds; do not combine with --contract-check')
    root=api_root()
    args.database=args.database or os.getenv('JOYNIU_DB',str(root/'data/joyniu.sqlite3'))
    args.cad_root=args.cad_root or os.getenv('JOYNIU_CAD_AGENT_DIR',str(root/'data/cad-agent'))
    def interrupted(_signum,_frame):raise AcceptanceFailure('operator_interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    try:acceptance=Acceptance(args)
    except FileExistsError:
        print(json.dumps({'status':'blocked','code':'output_directory_exists_no_new_task_started'}));return 2
    try:acceptance.run()
    except AcceptanceFailure as exc:
        acceptance.report.update(status='blocked',code=str(exc))
    except Exception as exc:
        acceptance.report.update(status='failed',code='local_'+type(exc).__name__)
    finally:report=acceptance.finish()
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='passed' else 2


if __name__=='__main__':
    raise SystemExit(main())
