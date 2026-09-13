"""Real filesystem/SQLite backups with a fake Docker host; never SSH/production."""
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import multiprocessing
import os
import io
from pathlib import Path
import sqlite3
import sys

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / 'deploy'
sys.path.insert(0, str(DEPLOY))
try:
    spec = importlib.util.spec_from_file_location('activate_currentcad', DEPLOY / 'activate_currentcad.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
finally:
    sys.path.remove(str(DEPLOY))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def manifest_for(candidate):
    files = {p.relative_to(candidate).as_posix(): digest(p) for p in candidate.rglob('*') if p.is_file()
             and p.name not in {'release-manifest.json', 'candidate-verification.json', 'verification.json'} and p.relative_to(candidate).as_posix() != 'dist/release.json'}
    manifest = {'release': release.RELEASE, 'gitBase': 'a' * 40, 'workingTreeSnapshot': True,
                'sourceAndBuildSha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), 'files': files}
    write_json(candidate / 'release-manifest.json', manifest)
    write_json(candidate / 'dist/release.json', {key: value for key, value in manifest.items() if key != 'files'})
    return manifest


@pytest.fixture
def setup(tmp_path):
    root, candidate = tmp_path/'production', tmp_path/'candidate'
    (root/'data/operations').mkdir(parents=True)
    (root/'data/operations/admission.lock').touch()
    (root/'data/cad-agent').mkdir()
    with closing(sqlite3.connect(root/'data/cad-agent/runs.sqlite3')) as db:
        db.executescript('CREATE TABLE cad_job_attempts(status,created_at,updated_at); CREATE TABLE cad_billing_operations(status); CREATE TABLE cad_provider_calls(completed_at,started_at,usage_json); CREATE TABLE cad_runs(run_id,revision,payload);')
    with closing(sqlite3.connect(root/'data/joyniu.sqlite3')) as db:
        db.execute('CREATE TABLE ledger(id INTEGER, amount INTEGER)'); db.execute('INSERT INTO ledger VALUES(1,42)'); db.commit()
    (root/'data/customer.step').write_bytes(b'customer-original-data')
    for name in ('apps/api/app/old.py', 'src/old.jsx', 'dist/index.html', 'public/logo.svg', 'deploy/nginx.conf',
                 'deploy/Dockerfile.commercial', 'deploy/commercial_operations.py', 'deploy/compose.operations.yaml', 'deploy/.secrets/secret-key',
                 'release-manifest.json', 'package.json', 'apps/api/pyproject.toml'):
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('ORIGINAL-'+name)
    (root/'compose.yaml').write_text('original-base')
    (root/'.env').write_text('PRIVATE_TOKEN=fixture-never-output\nCOMPOSE_FILE='+':'.join(release.OVERLAYS)+'\nBILLING_ENABLED=true\n')
    os.chmod(root/'.env',0o600)
    for name in ('apps/api/app/new.py','src/new.jsx','dist/index.html', *release.DEPLOY_FILES, *(name for name in release.ROOT_FILES if name != 'release-manifest.json')):
        path=candidate/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('NEW-'+name)
    manifest=manifest_for(candidate)
    images={key:{'tag':tag,'id':'sha256:'+char*64,'architecture':'amd64'} for key,tag,char in [
        ('apiImage','candidate-api:reviewed','a'),('webImage','candidate-web:reviewed','b'),('baseImage','old-api:released','c'),('toolsImage','candidate-tools:reviewed','d')]}
    smoke={'ok':True,'candidateRuntime':True,'architecture':'x86_64','compilerAbsent':True,'usesCustomerCredentials':False,
           'native':dict.fromkeys(('binaryDwg','fullDrawingSignatureMatch','dimensionsRemainNative','blocksAndPaperLayoutsPreserved'),True),
           'engineering':{'featureWorker':True,'stepValid':True,'pdfChineseGlyphsRendered':True,'pdfEmbeddedFontBytes':[407397]},
           'http':{'syntheticLogin':True,'authenticatedLists':True,'deliveryArchiveVerified':True,'anonymousStatus':dict.fromkeys(release.PROTECTED_ROUTES,401)},
           'tools':{name:{'versionChecked':True,'staticLibreDwgLinkVerified':True,'sha256':'f'*64} for name in ('dwgread','dwg2dxf','dxf2dwg','joyniu-native-dwg-adapter')},
           'restore':{'sourceUnchanged':True,'originalDatabaseRowsPreserved':True,'checkedDatabases':3}}
    write_json(candidate/'verification.json',smoke)
    write_json(candidate/'candidate-verification.json',{'sourceAndBuildSha256':manifest['sourceAndBuildSha256'],**images,
        'smoke':{'path':'verification.json','sha256':digest(candidate/'verification.json'),
                 'apiImageId':images['apiImage']['id'],'sourceAndBuildSha256':manifest['sourceAndBuildSha256'],
                 'scriptSha256':manifest['files']['deploy/currentcad_release_smoke.py'],'containerId':'9'*64}})
    config={**release.ops.DEFAULTS,'root':str(root),'backupRoot':str(tmp_path/'backups'),'reportRoot':str(tmp_path/'reports'),'minimumFreeBytes':0}
    return config,candidate,images


class FakeHost:
    def __init__(self,config,images):
        self.config=config; self.images={item['tag']:{'id':item['id'],'os':'linux','architecture':'amd64'} for item in images.values()}
        self.images['old-web:released']={'id':'sha256:'+'e'*64,'os':'linux','architecture':'amd64'}
        self.current={'api':{'tag':'old-api:released',**self.images['old-api:released']},'web':{'tag':'old-web:released',**self.images['old-web:released']}}
        self.events=[];self.state='running';self.web_state='running';self.extra=[];self.active=0;self.jobs_reads=0
        self.race=False;self.fail_verify=False;self.fail_candidate_up=False;self.fail_recovery=False;self.policy_mutation=False;self.compose_broken=False
    def image(self,tag):return self.images[tag]
    def capture_containers(self):return {'project':'joyniu-cad','containers':[{'id':'1'*64,'service':'api','workdir':self.config['root'],'project':'joyniu-cad'}]}
    def stop_for_recovery(self,state):
        assert state['oldContainers']['project']=='joyniu-cad'
        self.stop()
    def compose_configuration(self):
        env=(Path(self.config['root'])/'.env').read_text();new='JOYNIU_CURRENTCAD_API_IMAGE=' in env
        if new and self.compose_broken:raise RuntimeError('new compose parse failed')
        return {'services':{'api':{'image':'candidate-api:reviewed' if new else 'old-api:released',
            'build':{'context':self.config['root'],'dockerfile':'deploy/Dockerfile.currentcad' if new else 'deploy/Dockerfile.commercial',
                     'args':{'CURRENT_CAD_IMAGE':'old-api:released',**({'DWG_TOOLS_IMAGE':'candidate-tools:reviewed'} if new else {})}},
            'environment':{'PRIVATE_TOKEN':'fixture-never-output','BILLING_ENABLED':not(new and self.policy_mutation)},'volumes':['/data:/data']},
            'web':{'image':'candidate-web:reviewed' if new else 'old-web:released'},'cad-egress':{'image':'egress-original'}}}
    def running_images(self):return self.current
    def services(self):return [{'service':'api','state':self.state,'health':'healthy'},{'service':'web','state':self.web_state,'health':''},{'service':'cad-egress','state':'running','health':''},*self.extra]
    def gate(self):return {'gateVersion':'v1','maintenance':(Path(self.config['root'])/'data/operations/maintenance.json').exists()}
    def jobs(self):
        self.jobs_reads+=1
        return {'activeTotal':1 if self.race and self.jobs_reads>1 else self.active}
    def stop(self):
        self.events.append('stop');assert self.gate()['maintenance']
        with pytest.raises(BlockingIOError), release.ops.exclusive(Path(self.config['root'])/'data/operations/admission.lock',create=False):pass
        self.state='exited'
    def up(self):
        configuration=self.compose_configuration();new=configuration['services']['api']['image'].startswith('candidate-')
        self.events.append('up-new' if new else 'up-old')
        if new and self.fail_candidate_up:
            self.web_state='exited';raise RuntimeError('new web failed')
        if not new and self.fail_recovery:raise RuntimeError('old API failed')
        for service in ('api','web'):
            tag=configuration['services'][service]['image'];self.current[service]={'tag':tag,**self.images[tag]}
        self.state='running';self.web_state='running'
    def wait_ready(self):
        self.events.append('ready');assert not self.gate()['maintenance']
    def verify_gated(self,state,expected):
        self.events.append('verify-new-closed');assert self.gate()['maintenance']
        with pytest.raises(BlockingIOError), release.ops.exclusive(Path(self.config['root'])/'data/operations/admission.lock',create=False):pass
        assert all(self.current[key]['id']==expected[key]['id'] for key in expected)
        if self.fail_verify:
            (Path(self.config['root'])/'data/customer.step').write_bytes(b'new-runtime-data-must-survive-rollback')
            raise release.ActivationError('protected_route_not_unauthorized')
    def verify_recovery_gated(self,state):
        self.events.append('verify-old-closed');assert self.gate()['maintenance']
        with pytest.raises(BlockingIOError), release.ops.exclusive(Path(self.config['root'])/'data/operations/admission.lock',create=False):pass
        assert digest(Path(self.config['root'])/'.env')==state['oldEnvSha256']
        assert all(self.current[key]['id']==state['oldImages'][key]['id'] for key in ('api','web'))


def file_inventory(path):return {p.relative_to(path).as_posix():digest(p) for p in path.rglob('*') if p.is_file()}
def journal(config):return json.loads((Path(config['reportRoot'])/release.JOURNAL).read_text())


def test_default_plan_is_read_only_and_binds_all_release_evidence(setup):
    config,candidate,images=setup;host=FakeHost(config,images)
    before=file_inventory(Path(config['root']));candidate_before=file_inventory(candidate)
    result=release.activate(config,candidate,host)
    assert result['status']=='planned' and result['oldImages']['api']['id']==images['baseImage']['id']
    assert host.events==[] and not Path(config['reportRoot']).exists() and not Path(config['backupRoot']).exists()
    assert file_inventory(Path(config['root']))==before and file_inventory(candidate)==candidate_before


@pytest.mark.parametrize('damage,code',[('file','candidate_file_checksum_mismatch'),('aggregate','manifest_aggregate_checksum_mismatch'),('extra','unlisted_candidate_code'),('image','candidate_image_id_or_platform_mismatch'),('arm64','candidate_image_id_or_platform_mismatch'),('host-smoke','container_smoke_not_passed'),('smoke-hash','smoke_checksum_mismatch'),('release','dist_release_identity_mismatch')])
def test_invalid_candidate_never_sets_maintenance_or_stops(setup,damage,code):
    config,candidate,images=setup;host=FakeHost(config,images)
    if damage=='file':(candidate/'src/new.jsx').write_text('tampered')
    elif damage=='aggregate':
        m=json.loads((candidate/'release-manifest.json').read_text());m['sourceAndBuildSha256']='1'*64;write_json(candidate/'release-manifest.json',m)
    elif damage=='extra':(candidate/'src/unlisted.jsx').write_text('unlisted')
    elif damage in {'image','arm64'}:host.images[images['apiImage']['tag']]['id' if damage=='image' else 'architecture']='sha256:'+'f'*64 if damage=='image' else 'arm64'
    elif damage=='host-smoke':
        s=json.loads((candidate/'verification.json').read_text());s['candidateRuntime']=False;write_json(candidate/'verification.json',s)
        e=json.loads((candidate/'candidate-verification.json').read_text());e['smoke']['sha256']=digest(candidate/'verification.json');write_json(candidate/'candidate-verification.json',e)
    elif damage=='smoke-hash':(candidate/'verification.json').write_text('{}')
    else:write_json(candidate/'dist/release.json',{})
    with pytest.raises(release.ActivationError,match=code):release.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']


def test_activation_snapshots_offline_and_changes_only_allowed_code_env(setup):
    config,candidate,images=setup;host=FakeHost(config,images);root=Path(config['root'])
    protected={name:(root/name).read_bytes() for name in ('compose.yaml','deploy/nginx.conf','deploy/Dockerfile.commercial','deploy/commercial_operations.py','deploy/compose.operations.yaml','deploy/.secrets/secret-key','data/customer.step','public/logo.svg')}
    result=release.activate(config,candidate,host,execute=True)
    assert result['stage']=='completed' and host.events==['stop','up-new','verify-new-closed','ready']
    assert (root/'src/new.jsx').is_file() and not(root/'src/old.jsx').exists()
    assert not(root/'data/operations/maintenance.json').exists()
    assert not(Path(config['reportRoot'])/'recovery.json').exists()
    assert {name:(root/name).read_bytes() for name in protected}==protected
    saved=release.backup.verify(result['backup'])
    assert saved['quiescent'] and len(saved['databases'])==2
    assert (Path(result['backup'])/'snapshot/release-manifest.json').read_text() == 'ORIGINAL-release-manifest.json'
    assert (root/'release-manifest.json').read_bytes() == (candidate/'release-manifest.json').read_bytes()
    assert (root/'package.json').read_bytes() == (candidate/'package.json').read_bytes()
    assert (root/'deploy/verify.py').read_bytes() == (candidate/'deploy/verify.py').read_bytes()
    assert (Path(result['backup'])/'snapshot/.env').read_text().count('COMPOSE_FILE=')==1
    text=(root/'.env').read_text();assert text.startswith('PRIVATE_TOKEN=fixture-never-output\n')
    assert text.count('deploy/compose.currentcad.yaml')==1 and 'JOYNIU_CURRENTCAD_API_IMAGE=candidate-api:reviewed' in text
    assert (root/'.env').stat().st_mode & 0o777 == 0o600
    assert 'fixture-never-output' not in json.dumps(journal(config))


@pytest.mark.parametrize('reason',['active','race','inflight','extra-service','maintenance','commercial-recovery'])
def test_existing_work_requests_and_operations_prevent_stop(setup,reason):
    config,candidate,images=setup;host=FakeHost(config,images);root=Path(config['root'])
    if reason=='active':host.active=1
    elif reason=='race':host.race=True
    elif reason=='extra-service':host.extra=[{'service':'extra-writer','state':'running'}]
    elif reason=='maintenance':write_json(root/'data/operations/maintenance.json',{'token':'another-operation'})
    elif reason=='commercial-recovery':write_json(Path(config['reportRoot'])/'recovery.json',{'format':'existing-contract','private':'do-not-rewrite'})
    if reason=='inflight':
        with (root/'data/operations/admission.lock').open('r+') as handle:
            release.ops.fcntl.flock(handle,release.ops.fcntl.LOCK_SH)
            with pytest.raises(release.ActivationError):release.activate(config,candidate,host,execute=True)
    else:
        with pytest.raises(release.ActivationError):release.activate(config,candidate,host,execute=True)
    assert host.events==[] and not(root/'src/new.jsx').exists()
    if reason=='maintenance':assert json.loads((root/'data/operations/maintenance.json').read_text())['token']=='another-operation'
    else:assert not host.gate()['maintenance']
    if reason=='commercial-recovery':assert json.loads((Path(config['reportRoot'])/'recovery.json').read_text())['private']=='do-not-rewrite'


@pytest.mark.parametrize('failure',['up','health','policy','bad-compose'])
def test_failed_candidate_rolls_code_env_images_back_without_restoring_data(setup,failure):
    config,candidate,images=setup;host=FakeHost(config,images);root=Path(config['root']);env=(root/'.env').read_bytes()
    host.fail_candidate_up=failure=='up';host.fail_verify=failure=='health';host.policy_mutation=failure=='policy';host.compose_broken=failure=='bad-compose'
    with pytest.raises(release.ActivationError,match='activation_failed_rolled_back'):release.activate(config,candidate,host,execute=True)
    assert journal(config)['stage']=='rolled_back'
    assert (root/'src/old.jsx').is_file() and not(root/'src/new.jsx').exists() and (root/'.env').read_bytes()==env
    assert not(root/'deploy/compose.currentcad.yaml').exists()
    assert (root/'release-manifest.json').read_text() == 'ORIGINAL-release-manifest.json'
    assert (root/'package.json').read_text() == 'ORIGINAL-package.json'
    assert host.current['api']['id']==images['baseImage']['id'] and host.web_state=='running'
    assert not host.gate()['maintenance']
    if failure=='health':assert (root/'data/customer.step').read_bytes()==b'new-runtime-data-must-survive-rollback'
    assert host.events[-2:] == ['verify-old-closed', 'ready']


def test_failed_recovery_keeps_durable_journal_and_owned_marker_for_manual_retry(setup):
    config,candidate,images=setup;host=FakeHost(config,images)
    host.fail_candidate_up=True;host.fail_recovery=True
    with pytest.raises(release.ActivationError,match='manual_recovery_required'):release.activate(config,candidate,host,execute=True)
    assert journal(config)['stage']=='recovery_required' and host.gate()['maintenance']
    before=file_inventory(Path(config['root']))
    assert release.recover(config,host)['status']=='recovery_planned'
    assert file_inventory(Path(config['root']))==before
    host.fail_recovery=False
    assert release.recover(config,host,execute=True)['stage']=='rolled_back'
    assert not host.gate()['maintenance']
    assert release.recover(config,host,execute=True)['status']=='no_recovery_needed'


@pytest.mark.parametrize('phase',['code','new-environment'])
def test_process_death_during_code_swap_can_recover_from_verified_backup(setup,phase):
    config,candidate,images=setup
    def killed():
        host=FakeHost(config,images)
        def crash(root,candidate,files,token):
            (root/'src/old.jsx').unlink()
            (root/'src/new.jsx').write_text('partially copied')
            os._exit(99)
        if phase == 'code':release._install=crash
        else:host.up=lambda:os._exit(99)
        release.activate(config,candidate,host,execute=True)
    process=multiprocessing.get_context('fork').Process(target=killed);process.start();process.join(timeout=10)
    assert not process.is_alive() and process.exitcode==99
    state=journal(config);assert state['stage']==('installing_code' if phase=='code' else 'starting_candidate') and state['codeChangeStarted']
    if phase=='new-environment':assert state['envChangeStarted'] and 'JOYNIU_CURRENTCAD_API_IMAGE=' in (Path(config['root'])/'.env').read_text()
    host=FakeHost(config,images);host.state='exited'
    result=release.recover(config,host,execute=True)
    assert result['stage']=='rolled_back' and (Path(config['root'])/'src/old.jsx').is_file()
    assert not(Path(config['root'])/'src/new.jsx').exists() and not host.gate()['maintenance']


@pytest.mark.parametrize('field', ['apiImageId', 'sourceAndBuildSha256', 'scriptSha256', 'containerId'])
def test_smoke_requires_the_exact_candidate_container_source_and_script(setup, field):
    config,candidate,images=setup;host=FakeHost(config,images)
    path=candidate/'candidate-verification.json';evidence=json.loads(path.read_text())
    evidence['smoke'][field]='wrong-image-or-source'
    write_json(path,evidence)
    with pytest.raises(release.ActivationError,match='smoke_runtime_binding_mismatch'):
        release.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']


def test_recovery_rereads_terminal_journal_after_acquiring_action_lock(setup, monkeypatch):
    config,candidate,images=setup;host=FakeHost(config,images)
    host.fail_candidate_up=True;host.fail_recovery=True
    with pytest.raises(release.ActivationError,match='manual_recovery_required'):
        release.activate(config,candidate,host,execute=True)
    events=list(host.events)
    @contextmanager
    def concurrent_completion(path):
        state=journal(config);release._save(config,state,'rolled_back')
        yield
    monkeypatch.setattr(release.ops,'action_lock',concurrent_completion)
    assert release.recover(config,host,execute=True)=={'status':'no_recovery_needed','stage':'rolled_back'}
    assert host.events==events


def test_gated_probe_passes_secret_only_on_stdin_and_uses_recorded_running_container(setup, monkeypatch):
    config,candidate,images=setup;host=release.Host(config);token='7'*32
    state={'token':token};release._marker(config,state,create=True)
    expected={key:images[key+'Image'] for key in ('api','web')}
    monkeypatch.setattr(host,'_closed_images',lambda state,images:{'api':{'id':'6'*64}})
    def run(argv, **kwargs):
        assert argv==['docker','exec','-i','6'*64,'python','-c',release.LOOPBACK_PROBE]
        assert token not in ' '.join(argv)
        assert json.loads(kwargs['input'])=={'token':token,'mode':'candidate'}
        assert kwargs['capture_output'] and kwargs['check']
        return type('Result',(),{'stdout':'{"ok": true, "mode": "candidate"}'})()
    monkeypatch.setattr(release.subprocess,'run',run)
    host.verify_gated(state,expected)
    assert (Path(config['root'])/'data/operations/maintenance.json').exists()


@pytest.mark.parametrize('failure', [None, 'geometry', 'version', 'unauthorized', 'gate'])
def test_loopback_program_checks_real_process_geometry_capabilities_and_auth(monkeypatch, capsys, failure):
    from urllib import request as urllib_request
    from urllib.error import HTTPError
    routes=[];token='7'*32
    class Response:
        status=200
        def __init__(self,value):self.value=value
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,limit):return json.dumps(self.value).encode()
    class Opener:
        def open(self,request,timeout):
            path=request.full_url.removeprefix('http://127.0.0.1:8010');routes.append(path)
            assert request.get_method()=='GET' and request.full_url.startswith('http://127.0.0.1:8010/')
            assert request.get_header('Authorization') is None and request.get_header('Cookie') is None
            if path=='/api/v1/operations/readiness':return Response({'gateVersion':'v1','maintenance':failure!='gate'})
            assert request.get_header('X-joyniu-maintenance-probe')==token
            if path=='/api/v1/health':return Response({'geometry':{'available':failure!='geometry'}})
            if path=='/api/v1/cad-agent/capabilities':return Response({'version':'wrong' if failure=='version' else 'cad-agent-v1'})
            if failure=='unauthorized':return Response({'items':[]})
            raise HTTPError(request.full_url,401,'Unauthorized',{},None)
    monkeypatch.setattr(urllib_request,'build_opener',lambda *args:Opener())
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps({'token':token,'mode':'candidate'})))
    if failure:
        with pytest.raises(SystemExit) as caught:exec(release.LOOPBACK_PROBE,{})
        assert caught.value.code==1
    else:
        exec(release.LOOPBACK_PROBE,{})
        assert routes==['/api/v1/operations/readiness','/api/v1/health','/api/v1/cad-agent/capabilities',*release.PROTECTED_ROUTES]
    assert token not in capsys.readouterr().out


def test_start_services_independently_while_healthcheck_is_blocked(setup, monkeypatch):
    config,candidate,images=setup;host=release.Host(config);calls=[]
    def compose(*args,**kwargs):
        services=[value for value in args if value in ('api','web')]
        # Matches real Compose: explicitly selecting web and api together can
        # retain web's condition:service_healthy ordering despite --no-deps.
        if services==['api','web']:
            raise release.subprocess.TimeoutExpired('docker compose',180)
        assert '--no-deps' in args and '--no-build' in args
        calls.append(services)
        return ''
    monkeypatch.setattr(host,'compose',compose)
    host.up()
    assert calls==[['api'],['web']]


def test_compose_pins_production_project_directory_even_when_started_from_candidate(setup, monkeypatch):
    config,candidate,images=setup;host=release.Host(config)
    def run(argv,**kwargs):
        assert argv[:6]==['docker','compose','--project-directory',config['root'],'--env-file',str(Path(config['root'])/'.env')]
        assert kwargs['cwd']==config['root']
        return type('Result',(),{'stdout':''})()
    monkeypatch.setattr(release.subprocess,'run',run)
    host.compose('ps','--all','--quiet')


@pytest.mark.parametrize('foreign_state', ['exited','dead','running','created'])
def test_recovery_ignores_only_stopped_standalone_inherited_compose_labels(setup,monkeypatch,foreign_state):
    config,candidate,images=setup;host=release.Host(config)
    owned=[{'id':str(i)*64,'project':'joyniu-cad','service':service,'workdir':config['root'],
            'oneoff':'False','state':'running','image':images['apiImage']['id']} for i,service in enumerate(('api','web','cad-egress'),1)]
    inherited={'id':'8'*64,'project':'joyniu-cad','service':'api','workdir':'','oneoff':'','state':foreign_state}
    values=[*owned,inherited];stopped=[]
    monkeypatch.setattr(host,'_inspect_containers',lambda ids:values)
    def run(argv,**kwargs):
        if argv[1]=='stop':
            stopped.append(argv[-1]);owned[0]['state']='exited'
            return type('Result',(),{'stdout':''})()
        assert 'label=com.docker.compose.project=joyniu-cad' in argv
        return type('Result',(),{'stdout':'\n'.join(item['id'] for item in values)})()
    monkeypatch.setattr(release.subprocess,'run',run)
    state={'oldContainers':{'project':'joyniu-cad','containers':[{'id':'9'*64}]}}
    if foreign_state in {'exited','dead'}:
        assert host.project_containers(state)==owned
        host.stop_for_recovery(state)
        assert stopped==[owned[0]['id']]  # New actual ID, not the old recorded ID.
    else:
        with pytest.raises(release.ActivationError,match='recovery_container_scope_mismatch'):
            host.stop_for_recovery(state)
        assert stopped==[]

@pytest.mark.parametrize('source_ok,required_ok,business_ok,accepted', [
    (True,True,True,True),(True,False,True,False),(True,True,False,False),(False,True,True,False),
])
@pytest.mark.parametrize('cleanup',['auth','preview','both'])
def test_expired_cache_cleanup_report_still_requires_all_business_rows_and_source_files(setup, source_ok, required_ok, business_ok, accepted, cleanup):
    config,candidate,images=setup;host=FakeHost(config,images)
    report=json.loads((candidate/'verification.json').read_text())
    report['restore']={'sourceUnchanged':source_ok,'originalDatabaseRowsPreserved':False,
        'requiredOriginalRowsPreserved':required_ok,'businessRowsPreserved':business_ok,
        'expiredAuthRateLimitRowsAllowed':3 if cleanup in ('auth','both') else 0,
        'expiredAuthRateLimitRowsRemoved':3 if cleanup in ('auth','both') else 0,
        'expiredManualPreviewRowsAllowed':8 if cleanup in ('preview','both') else 0,
        'expiredManualPreviewRowsRemoved':8 if cleanup in ('preview','both') else 0,
        'previewCacheTtlSeconds':1800}
    write_json(candidate/'verification.json',report)
    evidence=json.loads((candidate/'candidate-verification.json').read_text())
    evidence['smoke']['sha256']=digest(candidate/'verification.json')
    write_json(candidate/'candidate-verification.json',evidence)
    if accepted:
        assert release.activate(config,candidate,host)['status']=='planned'
    else:
        with pytest.raises(release.ActivationError,match='restore_smoke_not_passed'):
            release.activate(config,candidate,host,execute=True)
    assert host.events==[]
