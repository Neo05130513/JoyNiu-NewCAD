"""Editor admission, evidence and rollback checks; synthetic data and loopback only."""
import copy
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old = load('_currentcad_activation_test_support', Path(__file__).with_name('test_activate_currentcad.py'))
editor = load('_cad_editor_activation_tests', ROOT / 'deploy/activate_cad_editor.py')


def reseal(candidate):
    files = {path.relative_to(candidate).as_posix(): old.digest(path) for path in candidate.rglob('*') if path.is_file()
             and path.name not in {'release-manifest.json', 'candidate-verification.json', 'verification.json'}
             and path.relative_to(candidate).as_posix() != 'dist/release.json'}
    manifest = {'release': editor.RELEASE, 'gitBase': 'a' * 40, 'workingTreeSnapshot': True,
                'sourceAndBuildSha256': hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), 'files': files}
    old.write_json(candidate/'release-manifest.json', manifest)
    old.write_json(candidate/'dist/release.json', {key: value for key, value in manifest.items() if key != 'files'})
    evidence = json.loads((candidate/'candidate-verification.json').read_text())
    evidence['sourceAndBuildSha256'] = manifest['sourceAndBuildSha256']
    evidence['smoke']['sourceAndBuildSha256'] = manifest['sourceAndBuildSha256']
    evidence['smoke']['scriptSha256'] = files['deploy/currentcad_release_smoke.py']
    old.write_json(candidate/'candidate-verification.json', evidence)
    return manifest


def update_smoke(candidate, mutate):
    path = candidate/'verification.json'
    value = json.loads(path.read_text()); mutate(value); old.write_json(path, value)
    evidence = json.loads((candidate/'candidate-verification.json').read_text())
    evidence['smoke']['sha256'] = old.digest(path)
    old.write_json(candidate/'candidate-verification.json', evidence)


def complete_fixture(candidate, script_hash):
    """Fake-host protocol fixtures; native STEP production is tested separately."""
    def artifact(name):
        content=(('synthetic gate fixture '+name+'\n')*8).encode()
        (candidate/name).write_bytes(content)
        return {'name':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
    result={'format':editor.complete.FORMAT,'scriptSha256':script_hash,'isolatedSyntheticData':True,
        'checks':dict.fromkeys(editor.complete.CHECKS,True),
        'geometries':{name:{'valid':True,'solidCount':solids,'faceCount':6 if solids else 1,
            'volumeMm3':100*solids,'areaMm2':80,'sizeMm':[10,8,6 if solids else 0],
            'step':artifact(f'complete-editor-{name}.step')} for name,solids in editor.complete.GEOMETRIES.items()},
        'details':{'g2':{'nonzeroSourceCurvature':True,'edgeSamples':4,'maxCurvatureError':1e-6},
            'pmi':{'measurementBeforeMm':16,'measurementAfterMm':24,'savedRevisions':2,'oldVersionPreserved':True},
            'helix':{'savedRevisions':2,'radiusBefore':5,'radiusAfter':7,'pitchAfter':5,'exactCurvedEdges':1,'previewVertices':240,'previewTriangles':120,'previewPreservedHead':True},
            'pdm':{'idempotent':True,'missingSourceRestored':True,'independentRebuild':True,'manufacturingConfirmed':False,
                   'archive':artifact('complete-editor-pdm.zip')}}}
    for name,volume in (('community-download',600),('community-edited-copy',1000)):
        result['geometries'][name]['volumeMm3']=volume
    glb=artifact('complete-editor-community.glb')
    public={'id':'resource_'+'a'*32,'name':'Synthetic published resource','description':'Fake-host protocol fixture',
        'publishedAt':'2026-09-13T00:00:00Z','status':'published','inspection':{},'canWithdraw':False,
        'sharing':'public-copy-download','drawingAgreement':'not_checked','productionReady':False,'artifacts':{}}
    snapshot={'public.json':json.dumps(public).encode(),
        'model.step':(candidate/'complete-editor-community-download.step').read_bytes(),
        'model.glb':(candidate/glb['name']).read_bytes()}
    manifest={'format':'joyniu-community-smoke-snapshot-v1','files':{name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in snapshot.items()}}
    archive_path=candidate/'complete-editor-community.zip'
    with zipfile.ZipFile(archive_path,'w') as bundle:
        for name,raw in snapshot.items():bundle.writestr(name,raw)
        bundle.writestr('manifest.json',json.dumps(manifest))
    result['details']['community']={**dict.fromkeys(('publicFieldsOnly','publicationIdempotent','copyIdempotent',
        'immutablePublishedRevision','independentCopy','parameterEdit','foreignWithdrawRejected','withdrawalRejectedDownloads',
        'withdrawalRejectedOpen','withdrawalRejectedRetry','existingCopyPreserved'),True),
        'manufacturingConfirmed':False,'sourceRevision':1,'sourceHeadRevision':2,'copyRevision':2,
        'publishedVolumeMm3':600,'editedCopyVolumeMm3':1000,'glb':glb,
        'archive':{'name':archive_path.name,'bytes':archive_path.stat().st_size,'sha256':old.digest(archive_path)}}
    return result


class Host(old.FakeHost):
    def compose_configuration(self):
        values = dict(line.split('=', 1) for line in (Path(self.config['root'])/'.env').read_text().splitlines() if '=' in line)
        image = values['JOYNIU_CURRENTCAD_API_IMAGE']; new = image.startswith('candidate-')
        if new and self.compose_broken: raise RuntimeError('candidate compose failure')
        return {'services': {'api': {'image': image, 'build': {'context': self.config['root'], 'dockerfile': 'deploy/Dockerfile.currentcad',
                    'args': {'CURRENT_CAD_IMAGE': values['JOYNIU_CURRENTCAD_BASE_IMAGE'], 'DWG_TOOLS_IMAGE': values['JOYNIU_CURRENTCAD_TOOLS_IMAGE']}},
                    'environment': {'PRIVATE_TOKEN': 'fixture-never-output', 'BILLING_ENABLED': not (new and self.policy_mutation)}, 'volumes': ['/data:/data']},
                'web': {'image': values['JOYNIU_CURRENTCAD_WEB_IMAGE']}, 'cad-egress': {'image': 'egress-original'}}}


@pytest.fixture
def setup(tmp_path):
    config, candidate, images = old.setup.__wrapped__(tmp_path)
    root = Path(config['root'])
    # Model the actual upgrade from an already activated CurrentCAD overlay.
    (root/'.env').write_text('PRIVATE_TOKEN=fixture-never-output\r\nCOMPOSE_FILE=' + ':'.join((*editor.base.OVERLAYS, 'deploy/compose.currentcad.yaml'))
        + '\r\nBILLING_ENABLED=true\r\nJOYNIU_CURRENTCAD_API_IMAGE=old-api:released\r\nJOYNIU_CURRENTCAD_WEB_IMAGE=old-web:released'
        + '\r\nJOYNIU_CURRENTCAD_BASE_IMAGE=original-base:v1\r\nJOYNIU_CURRENTCAD_TOOLS_IMAGE=candidate-tools:reviewed\r\nOTHER_POLICY="unchanged"\r\n')
    for name in set(editor.base.DEPLOY_FILES) - set(old.release.DEPLOY_FILES):
        (candidate/name).write_text('NEW-' + name)
    manifest = reseal(candidate)
    update_smoke(candidate, lambda value: (
        value['engineering'].update(cadEditor={'topologyFaces': 6, 'topologyEdges': 12, 'triangleMembership': True, 'selectedEdgeFillet': True,
            'atomicFailurePreservedHead': True, 'idempotentRetry': True, 'parameterHistoryRebuilt': True, 'profileHistoryRebuilt': True, 'compoundBodiesRebuilt': True, 'stepReadback': True, 'volumeMm3': 3517.8539816339744}),
        value['http'].update(cadEditor=dict.fromkeys(('anonymousRejected', 'readerRejected', 'ownerOnlyArtifacts', 'staleInteractivePickRejected',
            'atomicHttpCommit', 'httpIdempotency', 'foreignUpdateRejected', 'revisionConflictPreservedHead', 'maintenanceProbe', 'commitWaitedForPreviews'), True))))
    complete_report=complete_fixture(candidate,manifest['files'][editor.COMPLETE_HELPER])
    update_smoke(candidate,lambda value:value['engineering'].update(completeEditor=complete_report))
    evidence = json.loads((candidate/'candidate-verification.json').read_text())
    evidence['smoke'].update(entrypoint=editor.ENTRYPOINT, editorScriptSha256=manifest['files'][editor.ENTRYPOINT])
    old.write_json(candidate/'candidate-verification.json', evidence)
    return config, candidate, images


def test_wrapper_isolated_configuration_and_read_only_plan_preserve_current_release(setup):
    config, candidate, images = setup; host = Host(config, images)
    assert old.release.RELEASE == 'currentcad-delivery-20260912'
    assert old.release.JOURNAL == 'currentcad-activation.json'
    assert editor.JOURNAL == 'cad-editor-activation.json'
    assert editor.FORMAT == editor.base.FORMAT == old.release.FORMAT
    before = old.file_inventory(Path(config['root']))
    result = editor.activate(config, candidate, host)
    assert result['status'] == 'planned' and result['release'] == editor.RELEASE
    assert result['smokeEntrypoint'] == editor.ENTRYPOINT
    assert set(('deploy/Dockerfile.cad-editor', 'deploy/currentcad_release_smoke.py', editor.ENTRYPOINT, editor.COMPLETE_HELPER, 'deploy/activate_cad_editor.py')).issubset(result['files'])
    assert old.file_inventory(Path(config['root'])) == before and host.events == []
    assert not Path(config['reportRoot']).exists()


@pytest.mark.parametrize('field', ['triangleMembership', 'selectedEdgeFillet', 'atomicFailurePreservedHead', 'idempotentRetry', 'parameterHistoryRebuilt', 'profileHistoryRebuilt', 'compoundBodiesRebuilt', 'stepReadback', 'topologyFaces', 'topologyEdges', 'volumeMm3'])
def test_missing_editor_geometry_checks_prevent_activation(setup, field):
    config, candidate, images = setup; host = Host(config, images)
    update_smoke(candidate, lambda value: value['engineering']['cadEditor'].pop(field))
    with pytest.raises(editor.ActivationError, match='editor_geometry_smoke_not_passed'):
        editor.activate(config, candidate, host, execute=True)
    assert host.events == [] and not host.gate()['maintenance']


@pytest.mark.parametrize('field', ['anonymousRejected', 'readerRejected', 'ownerOnlyArtifacts', 'staleInteractivePickRejected', 'atomicHttpCommit', 'httpIdempotency', 'foreignUpdateRejected', 'revisionConflictPreservedHead', 'maintenanceProbe', 'commitWaitedForPreviews'])
def test_missing_editor_http_checks_prevent_activation(setup, field):
    config, candidate, images = setup; host = Host(config, images)
    update_smoke(candidate, lambda value: value['http']['cadEditor'].pop(field))
    with pytest.raises(editor.ActivationError, match='editor_http_smoke_not_passed'):
        editor.validate_candidate(candidate, host)
    assert host.events == []


@pytest.mark.parametrize('damage,code', [
    ('old-smoke', 'editor_smoke_runtime_binding_mismatch'), ('script', 'editor_smoke_runtime_binding_mismatch'),
    ('container', 'smoke_runtime_binding_mismatch'), ('image', 'smoke_runtime_binding_mismatch'),
    ('baseline-script', 'smoke_runtime_binding_mismatch'), ('baseline-native', 'native_smoke_not_passed'),
    ('host-only', 'container_smoke_not_passed'), ('truthy', 'editor_geometry_smoke_not_passed')])
def test_entrypoint_script_container_source_and_baseline_bindings_are_all_required(setup, damage, code):
    config, candidate, images = setup; host = Host(config, images)
    path = candidate/'candidate-verification.json'; evidence = json.loads(path.read_text())
    if damage == 'old-smoke': evidence['smoke'].pop('entrypoint')
    elif damage == 'script':
        (candidate/editor.ENTRYPOINT).write_text('changed-after-smoke')
        reseal(candidate); evidence = json.loads(path.read_text())
    elif damage == 'container': evidence['smoke']['containerId'] = 'bad'
    elif damage == 'image': evidence['smoke']['apiImageId'] = images['baseImage']['id']
    elif damage == 'baseline-script': evidence['smoke']['scriptSha256'] = '0' * 64
    elif damage == 'baseline-native':
        update_smoke(candidate, lambda value: value['native'].update(binaryDwg=False)); evidence = json.loads(path.read_text())
    elif damage == 'host-only':
        update_smoke(candidate, lambda value: value.update(candidateRuntime=False)); evidence = json.loads(path.read_text())
    elif damage == 'truthy':
        update_smoke(candidate, lambda value: value['engineering']['cadEditor'].update(stepReadback=1)); evidence = json.loads(path.read_text())
    old.write_json(path, evidence)
    with pytest.raises(editor.ActivationError, match=code): editor.activate(config, candidate, host, execute=True)
    assert host.events == [] and not host.gate()['maintenance']


def test_existing_overlay_replaces_only_image_variables_and_refuses_ambiguous_policy(setup):
    config, _, images = setup; original = (Path(config['root'])/'.env').read_bytes()
    changed = editor.updated_environment(original, images)
    protected = lambda value: [line for line in value.splitlines(keepends=True) if not line.startswith(b'JOYNIU_CURRENTCAD_')]
    assert protected(changed) == protected(original)
    assert changed.count(b'deploy/compose.currentcad.yaml') == 1 and b'\r\n' in changed
    for variable in editor.base.IMAGE_KEYS.values(): assert changed.count((variable+'=').encode()) == 1
    with pytest.raises(editor.ActivationError, match='candidate_image_setting_ambiguous'):
        editor.updated_environment(original + b'JOYNIU_CURRENTCAD_API_IMAGE=duplicate\n', images)
    with pytest.raises(editor.ActivationError, match='unexpected_existing_compose_overlays'):
        editor.updated_environment(original.replace(b'compose.currentcad.yaml', b'compose.unknown.yaml'), images)


def test_upgrade_uses_independent_journal_and_manages_new_scripts_without_touching_policies(setup):
    config, candidate, images = setup; root = Path(config['root']); host = Host(config, images)
    old.write_json(Path(config['reportRoot'])/old.release.JOURNAL, {'stage': 'completed', 'historical': True})
    original_journal = (Path(config['reportRoot'])/old.release.JOURNAL).read_bytes()
    result = editor.activate(config, candidate, host, execute=True)
    assert result['stage'] == 'completed'
    assert host.events == ['stop', 'up-new', 'verify-new-closed', 'ready']
    assert (Path(config['reportRoot'])/old.release.JOURNAL).read_bytes() == original_journal
    assert json.loads((Path(config['reportRoot'])/editor.JOURNAL).read_text())['release'] == editor.RELEASE
    for name in ('deploy/Dockerfile.cad-editor', editor.ENTRYPOINT, 'deploy/activate_cad_editor.py'):
        assert (root/name).read_bytes() == (candidate/name).read_bytes()
    assert (root/'data/customer.step').read_bytes() == b'customer-original-data'
    assert 'PRIVATE_TOKEN=fixture-never-output' in (root/'.env').read_text()


@pytest.mark.parametrize('failure', ['health', 'bad-compose'])
def test_failed_upgrade_restores_exact_old_env_images_code_and_never_customer_data(setup, failure):
    config, candidate, images = setup; root = Path(config['root']); host = Host(config, images)
    original = (root/'.env').read_bytes()
    host.fail_verify = failure == 'health'; host.compose_broken = failure == 'bad-compose'
    with pytest.raises(editor.ActivationError, match='activation_failed_rolled_back'):
        editor.activate(config, candidate, host, execute=True)
    assert (root/'.env').read_bytes() == original
    assert host.current['api']['id'] == images['baseImage']['id']
    assert (root/'src/old.jsx').exists() and not (root/'src/new.jsx').exists()
    assert not (root/editor.ENTRYPOINT).exists()
    assert json.loads((Path(config['reportRoot'])/editor.JOURNAL).read_text())['stage'] == 'rolled_back'
    assert not host.gate()['maintenance']
    if failure == 'health': assert (root/'data/customer.step').read_bytes() == b'new-runtime-data-must-survive-rollback'


def test_pending_historical_activation_blocks_the_new_release_before_maintenance(setup):
    config, candidate, images = setup; host = Host(config, images)
    old.write_json(Path(config['reportRoot'])/old.release.JOURNAL, {'stage': 'recovery_required'})
    with pytest.raises(editor.ActivationError, match='historical_activation_recovery_pending'):
        editor.activate(config, candidate, host, execute=True)
    assert host.events == [] and not host.gate()['maintenance']


def test_cli_default_targets_new_candidate_and_recovery_journal(monkeypatch):
    calls = []; monkeypatch.setattr(editor.base, 'main', lambda values: calls.append(values) or 0)
    assert editor.main(['--config', '/synthetic/config.json']) == 0
    assert calls[-1][-2:] == ['--candidate', '/opt/joyniu-candidates/' + editor.RELEASE]
    editor.main(['--config', '/synthetic/config.json', '--recover', '--candidate=/synthetic/explicit'])
    assert calls[-1].count('--candidate') == 0


def test_interrupted_recovery_uses_only_its_own_journal_and_preserves_runtime_data(setup):
    config, candidate, images = setup; host = Host(config, images); root = Path(config['root'])
    original = (root/'.env').read_bytes(); host.fail_candidate_up = True; host.fail_recovery = True
    with pytest.raises(editor.ActivationError, match='manual_recovery_required'):
        editor.activate(config, candidate, host, execute=True)
    assert host.gate()['maintenance']
    state = json.loads((Path(config['reportRoot'])/editor.JOURNAL).read_text())
    assert state['format'] == editor.FORMAT and state['stage'] == 'recovery_required'
    assert not (Path(config['reportRoot'])/old.release.JOURNAL).exists()
    (root/'data/customer.step').write_bytes(b'leave-this-runtime-data-alone')
    before = old.file_inventory(root)
    assert editor.recover(config, host)['status'] == 'recovery_planned'
    assert old.file_inventory(root) == before
    host.fail_recovery = False
    assert editor.recover(config, host, execute=True)['stage'] == 'rolled_back'
    assert (root/'.env').read_bytes() == original and not host.gate()['maintenance']
    assert (root/'data/customer.step').read_bytes() == b'leave-this-runtime-data-alone'


def test_report_cannot_change_between_baseline_and_editor_checks(setup, monkeypatch):
    config, candidate, images = setup; host = Host(config, images)
    original = editor.base._json; reads = 0
    def changed_read(path):
        nonlocal reads
        if Path(path).name == 'verification.json':
            reads += 1
            if reads == 2:
                value = json.loads(Path(path).read_text()); value['http']['cadEditor']['ownerOnlyArtifacts'] = False
                old.write_json(Path(path), value)
        return original(path)
    monkeypatch.setattr(editor.base, '_json', changed_read)
    with pytest.raises(editor.ActivationError, match='editor_evidence_changed_during_validation'):
        editor.validate_candidate(candidate, host)
    assert host.events == []


def test_wrapper_marker_passes_real_closed_http_probe_and_preserves_authentication(tmp_path):
    """Exercise the emitted marker, real gate, four auth routers and probe program."""
    import fcntl
    import socket
    import subprocess
    import sys
    import threading
    import time
    from types import SimpleNamespace
    from urllib.error import HTTPError
    from urllib.request import build_opener, ProxyHandler, Request
    from fastapi import FastAPI
    import uvicorn
    from app.platform import AuthService
    from app.operations_gate import OperationsGateMiddleware
    from app.native_drawing_api import create_native_drawing_router
    from app.cad_design_workspace_api import create_cad_design_workspace_router
    from app.cad_feature_workspace_api import create_cad_feature_workspace_router
    from app.delivery_workspace_api import create_delivery_workspace_router

    directory = tmp_path / 'data/operations'
    directory.mkdir(parents=True)
    state = {'token': 'c3' * 16}
    marker = editor.base._marker({'root': str(tmp_path)}, state, create=True)
    assert json.loads(marker.read_text())['format'] == 'joyniu-currentcad-activation-v1'
    auth = AuthService(tmp_path / 'accounts.sqlite3', token_secret='synthetic-closed-probe-test' * 2)
    services = SimpleNamespace(auth=auth)
    app = FastAPI()
    designs = create_cad_design_workspace_router(services, root=tmp_path / 'designs')
    features = create_cad_feature_workspace_router(services, root=tmp_path / 'features', design_store=designs.design_store)
    app.include_router(designs)
    app.include_router(features)
    app.include_router(create_native_drawing_router(services))
    app.include_router(create_delivery_workspace_router(services, root=tmp_path / 'deliveries',
        engineering_store=designs.design_store, feature_store=features.feature_store))
    geometry_available = {'value': True}

    @app.get('/api/v1/health')
    def health():
        return {'geometry': {'available': geometry_available['value']}}

    @app.get('/api/v1/cad-agent/capabilities')
    def capabilities():
        return {'version': 'cad-agent-v1'}

    app.add_middleware(OperationsGateMiddleware, directory=directory)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    opener = build_opener(ProxyHandler({}))
    url = f'http://127.0.0.1:{port}'
    # The production assertions and stdin-only token transport are unchanged;
    # only the test's ephemeral loopback listener differs from container 8010.
    program = editor.base.LOOPBACK_PROBE.replace('http://127.0.0.1:8010', url)

    def probe():
        return subprocess.run([sys.executable, '-c', program],
            input=json.dumps({**state, 'mode': 'candidate'}), capture_output=True, text=True, timeout=15)

    def status(path, headers=None, method='GET'):
        try:
            with opener.open(Request(url + path, headers=headers or {}, method=method), timeout=3) as response:
                return response.status
        except HTTPError as error:
            code = error.code
            error.close()
            return code

    try:
        with (directory / 'admission.lock').open('w') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started
            passed = probe()
            assert passed.returncode == 0 and json.loads(passed.stdout) == {'ok': True, 'mode': 'candidate'}
            headers = {'X-Joyniu-Maintenance-Probe': state['token']}
            for path in editor.base.PROTECTED_ROUTES:
                assert status(path) == 503
                assert status(path, headers) == 401
                assert status(path, headers, 'POST') == 503
            assert status('/api/v1/health', {'X-Joyniu-Maintenance-Probe': 'd4' * 16}) == 503
            assert status('/api/v1/health', {**headers, 'Authorization': 'Bearer synthetic'}) == 503
            assert status('/api/v1/health', {**headers, 'X-Forwarded-For': '127.0.0.1'}) == 503
            # Reproduce the exact failed r2 marker: readiness is insufficient,
            # and the closed probe must fail rather than opening admission.
            marker.write_text(json.dumps({'format': 'joyniu-cad-editor-activation-v1', **state}))
            assert status('/api/v1/operations/readiness') == 200
            denied = probe()
            assert denied.returncode == 1 and json.loads(denied.stdout) == {'ok': False}
            assert status('/api/v1/health', headers) == 503
        # The candidate's smoke runs this same marker/probe contract too. Test
        # its success and failure cleanup against the still-running real gate.
        marker.unlink()
        sys.path.insert(0, str(ROOT / 'deploy'))
        try:
            smoke = load('_editor_maintenance_smoke_test', ROOT / 'deploy/cad_editor_release_smoke.py')
        finally:
            sys.path.pop(0)
        assert smoke.editor_maintenance_probe(url, tmp_path) is True
        assert not marker.exists()
        geometry_available['value'] = False
        with pytest.raises(smoke.baseline.SmokeCheckFailed, match='Release activation probe did not pass'):
            smoke.editor_maintenance_probe(url, tmp_path)
        assert not marker.exists()
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=10)
        listener.close()
        auth.close()
    assert not thread.is_alive()


def test_old_editor_journal_requires_its_frozen_recovery_wrapper(setup):
    config, candidate, images = setup
    host = Host(config, images)
    state = {'format': 'joyniu-cad-editor-activation-v1', 'deployment': config['root'],
        'release': editor.RELEASE, 'token': 'a1' * 16, 'stage': 'recovery_required',
        'stopRequested': True, 'codeChangeStarted': True, 'envChangeStarted': True}
    path = Path(config['reportRoot']) / editor.JOURNAL
    old.write_json(path, state)
    before = old.file_inventory(Path(config['root']))
    with pytest.raises(editor.ActivationError, match='invalid_activation_journal'):
        editor.recover(config, host)
    assert json.loads(path.read_text()) == state
    assert old.file_inventory(Path(config['root'])) == before and host.events == []


def test_completed_same_release_journal_does_not_reuse_a_previous_candidates_result(setup):
    config, candidate, images = setup
    host = Host(config, images)
    path = Path(config['reportRoot']) / editor.JOURNAL
    previous = {'stage': 'completed', 'format': editor.FORMAT, 'release': editor.RELEASE,
        'deployment': config['root'], 'token': 'a1' * 16, 'candidate': '/synthetic/previous-r4',
        'sourceAndBuildSha256': '0' * 64}
    old.write_json(path, previous)
    planned = editor.activate(config, candidate, host)
    assert planned['status'] == 'planned' and planned['release'] == previous['release']
    assert planned['sourceAndBuildSha256'] != previous['sourceAndBuildSha256']
    assert json.loads(path.read_text()) == previous and host.events == []
    result = editor.activate(config, candidate, host, execute=True)
    assert result['stage'] == 'completed' and result['sourceAndBuildSha256'] == planned['sourceAndBuildSha256']
    assert result['token'] != previous['token'] and result['candidate'] == str(candidate)
    assert host.events == ['stop', 'up-new', 'verify-new-closed', 'ready']
    assert json.loads(path.read_text()) == result


@pytest.mark.parametrize('field', editor.complete.CHECKS)
def test_each_expanded_native_capability_is_required_before_admission_opens(setup, field):
    config,candidate,images=setup;host=Host(config,images)
    update_smoke(candidate,lambda value:value['engineering']['completeEditor']['checks'].pop(field))
    with pytest.raises(editor.ActivationError,match='complete_editor_smoke_not_passed'):
        editor.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']


@pytest.mark.parametrize('damage', ['old-core-only','helper-hash','truthy','missing-case','wrong-solid','nonfinite','flat-g2','stale-pmi','confirmed-pdm','path-traversal','missing-community','private-community','withdraw-open','drifted-publication','confirmed-community'])
def test_complete_evidence_cannot_be_replaced_by_old_or_plausible_metadata(setup,damage):
    config,candidate,images=setup;host=Host(config,images)
    def change(value):
        report=value['engineering']['completeEditor']
        if damage=='old-core-only':value['engineering'].pop('completeEditor')
        elif damage=='helper-hash':report['scriptSha256']='0'*64
        elif damage=='truthy':report['checks']['referenceHelix']=1
        elif damage=='missing-case':report['geometries'].pop('ellipse')
        elif damage=='wrong-solid':report['geometries']['style']['solidCount']=1
        elif damage=='nonfinite':report['geometries']['spline']['areaMm2']=float('inf')
        elif damage=='flat-g2':report['details']['g2']['nonzeroSourceCurvature']=False
        elif damage=='stale-pmi':report['details']['pmi']['measurementAfterMm']=16
        elif damage=='confirmed-pdm':report['details']['pdm']['manufacturingConfirmed']=True
        elif damage=='path-traversal':report['geometries']['ellipse']['step']['name']='../outside.step'
        elif damage=='missing-community':report['details'].pop('community')
        elif damage=='private-community':report['details']['community']['publicFieldsOnly']=False
        elif damage=='withdraw-open':report['details']['community']['withdrawalRejectedRetry']=False
        elif damage=='drifted-publication':report['geometries']['community-download']['volumeMm3']=900
        elif damage=='confirmed-community':report['details']['community']['manufacturingConfirmed']=True
    update_smoke(candidate,change)
    with pytest.raises(editor.ActivationError,match='complete_editor_smoke_not_passed'):
        editor.validate_candidate(candidate,host)
    assert host.events==[]


def test_complete_evidence_files_are_hashed_at_activation_and_never_copied_as_application_code(setup):
    config,candidate,images=setup;host=Host(config,images)
    checked=editor.validate_candidate(candidate,host)
    assert editor.COMPLETE_HELPER in checked['files']
    assert not any(name.startswith('complete-editor-') for name in checked['files'])
    path=candidate/'complete-editor-spline.step';path.write_bytes(path.read_bytes()+b'tamper')
    with pytest.raises(editor.ActivationError,match='complete_editor_artifact_mismatch'):
        editor.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']


def test_changed_complete_helper_is_not_authorized_by_resealing_only_the_old_smoke(setup):
    config,candidate,images=setup;host=Host(config,images)
    (candidate/editor.COMPLETE_HELPER).write_text('changed complete helper after runtime validation')
    reseal(candidate)
    with pytest.raises(editor.ActivationError,match='complete_editor_smoke_not_passed'):
        editor.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']


def test_missing_complete_step_evidence_refuses_release_before_maintenance(setup):
    config,candidate,images=setup;host=Host(config,images)
    (candidate/'complete-editor-ellipse.step').unlink()
    with pytest.raises(editor.ActivationError,match='required_path_missing'):
        editor.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']


@pytest.mark.parametrize('damage',['different-download','private-public-field','duplicate-entry','manufacturing-confirmed'])
def test_rehashed_community_archive_must_match_downloads_and_public_contract(setup,damage):
    config,candidate,images=setup;host=Host(config,images)
    path=candidate/'complete-editor-community.zip'
    with zipfile.ZipFile(path) as bundle:parts={name:bundle.read(name) for name in bundle.namelist()}
    if damage=='different-download':parts['model.step']+=b'not-the-downloaded-revision'
    elif damage in ('private-public-field','manufacturing-confirmed'):
        public=json.loads(parts['public.json'])
        if damage=='private-public-field':public['sourceRun']='private-source'
        else:public['productionReady']=True
        parts['public.json']=json.dumps(public).encode()
    manifest=json.loads(parts['manifest.json'])
    manifest['files']={name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in parts.items() if name!='manifest.json'}
    parts['manifest.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(path,'w') as bundle:
        for name,raw in parts.items():bundle.writestr(name,raw)
        if damage=='duplicate-entry':
            with pytest.warns(UserWarning,match='Duplicate name'):bundle.writestr('model.step',parts['model.step'])
    update_smoke(candidate,lambda value:value['engineering']['completeEditor']['details']['community']['archive'].update(bytes=path.stat().st_size,sha256=old.digest(path)))
    with pytest.raises(editor.ActivationError,match='complete_editor_smoke_not_passed'):
        editor.activate(config,candidate,host,execute=True)
    assert host.events==[] and not host.gate()['maintenance']
