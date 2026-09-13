#!/usr/bin/env python3
"""One reviewed CurrentCAD candidate activation; default is a read-only plan.

Run from the candidate, with the existing commercial operations configuration:
  python3 deploy/activate_currentcad.py --config /etc/joyniu-cad-operations.json \
    --candidate /opt/joyniu-candidates/currentcad-delivery-20260912
Append --execute only to activate. After an interrupted attempt, use the same
script with --config ... --recover [--execute]. Recovery rolls code/images back,
never customer data. This journal is separate from operations recovery.json.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

# A plan must not even leave imported deployment __pycache__ files behind.
sys.dont_write_bytecode = True
try:
    from . import commercial_backup as backup
    from . import commercial_operations as ops
except ImportError:
    import commercial_backup as backup
    import commercial_operations as ops

FORMAT = 'joyniu-currentcad-activation-v1'
RELEASE = 'currentcad-delivery-20260912'
JOURNAL = 'currentcad-activation.json'
TREES = ('apps/api/app', 'src', 'dist', 'public')
DEPLOY_FILES = ('deploy/compose.currentcad.yaml', 'deploy/Dockerfile.currentcad',
                'deploy/build_currentcad_tools.py', 'deploy/currentcad_release_smoke.py',
                'deploy/currentcad_live_acceptance.py', 'deploy/activate_currentcad.py', 'deploy/verify.py')
ROOT_FILES = ('apps/api/pyproject.toml', 'apps/api/README.md', 'package.json', 'package-lock.json',
              'index.html', '.dockerignore', 'release-manifest.json')
COPY_FILES = (*DEPLOY_FILES, *ROOT_FILES)
OVERLAYS = ('compose.yaml', 'deploy/compose.codex.yaml', 'deploy/compose.commercial.yaml', 'deploy/compose.operations.yaml')
IMAGE_KEYS = {'apiImage': 'JOYNIU_CURRENTCAD_API_IMAGE', 'webImage': 'JOYNIU_CURRENTCAD_WEB_IMAGE',
              'baseImage': 'JOYNIU_CURRENTCAD_BASE_IMAGE', 'toolsImage': 'JOYNIU_CURRENTCAD_TOOLS_IMAGE'}
PROTECTED_ROUTES = ('/api/cad/drawings', '/api/cad/features', '/api/cad/designs', '/api/cad/deliveries')
TERMINAL = {'completed', 'rolled_back', 'aborted_no_change'}

# Executes inside the already running API container, never imports a second app.
# Only the non-secret program goes into argv. The maintenance token uses stdin.
LOOPBACK_PROBE = '''import json, sys
from urllib.request import build_opener, ProxyHandler, Request
from urllib.error import HTTPError
data = json.load(sys.stdin)
opener = build_opener(ProxyHandler({}))
def request(path, probe=False):
    headers = {"X-Joyniu-Maintenance-Probe": data["token"]} if probe else {}
    try:
        with opener.open(Request("http://127.0.0.1:8010" + path, headers=headers), timeout=4) as response:
            return response.status, json.loads(response.read(1048576))
    except HTTPError as error:
        status = error.code
        error.close()
        return status, {}
try:
    status, ready = request("/api/v1/operations/readiness")
    assert status == 200 and ready.get("gateVersion") == "v1" and ready.get("maintenance") is True
    if data["mode"] == "candidate":
        status, health = request("/api/v1/health", True)
        assert status == 200 and health.get("geometry", {}).get("available") is True
        status, capabilities = request("/api/v1/cad-agent/capabilities", True)
        assert status == 200 and capabilities.get("version") == "cad-agent-v1"
        for path in ("/api/cad/drawings", "/api/cad/features", "/api/cad/designs", "/api/cad/deliveries"):
            assert request(path, True)[0] == 401
    print(json.dumps({"ok": True, "mode": data["mode"]}))
except Exception:
    print(json.dumps({"ok": False}))
    sys.exit(1)
'''


class ActivationError(RuntimeError):
    """Fixed diagnostic codes only; no command output, environment or customer data."""


def require(condition, code):
    if not condition:
        raise ActivationError(code)


def safe_path(value, *, exists=False):
    path = Path(value).absolute()
    require(not any(item.is_symlink() for item in (path, *path.parents)), 'symlink_path_refused')
    if exists:
        require(path.exists(), 'required_path_missing')
    return path


def _relative(value):
    require(isinstance(value, str), 'invalid_manifest_path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts and str(path) == value and '\\' not in value,
            'invalid_manifest_path')
    return value


def _image(value):
    require(isinstance(value, dict) and isinstance(value.get('tag'), str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}', value['tag'])
            and isinstance(value.get('id'), str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value['id']), 'invalid_image_evidence')
    return value


def _json(path):
    path = safe_path(path, exists=True)
    require(path.is_file() and path.stat().st_size <= 8 * 1024**2, 'invalid_evidence_file')
    try:
        result = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise ActivationError('invalid_evidence_json') from None
    require(isinstance(result, dict), 'invalid_evidence_json')
    return result


def _managed(name):
    return any(name.startswith(tree + '/') for tree in TREES) or name in COPY_FILES


def validate_candidate(candidate, host):
    candidate = safe_path(candidate, exists=True)
    manifest_path = candidate / 'release-manifest.json'
    manifest = _json(manifest_path)
    require(manifest.get('release') == RELEASE and manifest.get('workingTreeSnapshot') is True
            and isinstance(manifest.get('gitBase'), str)
            and re.fullmatch(r'[a-f0-9]{64}', manifest.get('sourceAndBuildSha256', '')), 'wrong_release_manifest')
    files = manifest.get('files')
    require(isinstance(files, dict) and files, 'empty_release_inventory')
    require(hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == manifest['sourceAndBuildSha256'],
            'manifest_aggregate_checksum_mismatch')
    for name, digest in files.items():
        _relative(name)
        require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest), 'invalid_manifest_checksum')
        path = safe_path(candidate / name, exists=True)
        require(path.is_file() and backup._digest(path) == digest, 'candidate_file_checksum_mismatch')
    release = _json(candidate / 'dist/release.json')
    require(release == {key: manifest[key] for key in ('release', 'gitBase', 'workingTreeSnapshot', 'sourceAndBuildSha256')},
            'dist_release_identity_mismatch')
    selected = {name: digest for name, digest in files.items() if _managed(name)}
    selected['dist/release.json'] = backup._digest(candidate / 'dist/release.json')
    selected['release-manifest.json'] = backup._digest(manifest_path)
    for tree in TREES:
        path = candidate / tree
        if not path.exists():
            require(tree == 'public' and not any(name.startswith(tree + '/') for name in selected), 'candidate_tree_missing')
            continue
        actual = {item.relative_to(candidate).as_posix() for item in backup._files(path)}
        require(actual == {name for name in selected if name.startswith(tree + '/')}, 'unlisted_candidate_code')
    require(all(name in selected for name in COPY_FILES), 'required_deploy_file_missing')
    evidence_path = candidate / 'candidate-verification.json'
    evidence = _json(evidence_path)
    require(evidence.get('sourceAndBuildSha256') == manifest['sourceAndBuildSha256'], 'image_source_binding_mismatch')
    for key in IMAGE_KEYS:
        image = _image(evidence.get(key))
        actual = host.image(image['tag'])
        require(actual['id'] == image['id'] and actual.get('os') == 'linux' and actual.get('architecture') == 'amd64',
                'candidate_image_id_or_platform_mismatch')
        if 'architecture' in image:
            require(image['architecture'] == 'amd64', 'candidate_evidence_platform_mismatch')
    smoke_ref = evidence.get('smoke')
    require(isinstance(smoke_ref, dict) and isinstance(smoke_ref.get('path'), str), 'smoke_evidence_missing')
    require(smoke_ref.get('apiImageId') == evidence['apiImage']['id']
            and smoke_ref.get('sourceAndBuildSha256') == manifest['sourceAndBuildSha256']
            and smoke_ref.get('scriptSha256') == files.get('deploy/currentcad_release_smoke.py')
            and isinstance(smoke_ref.get('containerId'), str)
            and re.fullmatch(r'[a-f0-9]{64}', smoke_ref['containerId']), 'smoke_runtime_binding_mismatch')
    smoke_path = Path(smoke_ref['path'])
    if not smoke_path.is_absolute():
        _relative(smoke_ref['path']); smoke_path = candidate / smoke_path
    smoke_path = safe_path(smoke_path, exists=True)
    require(backup._digest(smoke_path) == smoke_ref.get('sha256'), 'smoke_checksum_mismatch')
    smoke = _json(smoke_path)
    require(smoke.get('ok') is True and smoke.get('candidateRuntime') is True
            and smoke.get('compilerAbsent') is True and smoke.get('architecture') in {'x86_64', 'amd64'}
            and smoke.get('usesCustomerCredentials') is False, 'container_smoke_not_passed')
    require(all(smoke.get('native', {}).get(key) is True for key in ('binaryDwg', 'fullDrawingSignatureMatch', 'dimensionsRemainNative', 'blocksAndPaperLayoutsPreserved')),
            'native_smoke_not_passed')
    require(all(smoke.get('engineering', {}).get(key) is True for key in ('featureWorker', 'stepValid', 'pdfChineseGlyphsRendered'))
            and bool(smoke.get('engineering', {}).get('pdfEmbeddedFontBytes')), 'engineering_smoke_not_passed')
    require(all(smoke.get('http', {}).get(key) is True for key in ('syntheticLogin', 'authenticatedLists', 'deliveryArchiveVerified'))
            and smoke.get('http', {}).get('anonymousStatus') == dict.fromkeys(PROTECTED_ROUTES, 401), 'http_smoke_not_passed')
    for name in ('dwgread', 'dwg2dxf', 'dxf2dwg', 'joyniu-native-dwg-adapter'):
        tool = smoke.get('tools', {}).get(name, {})
        require(tool.get('versionChecked') is True and tool.get('staticLibreDwgLinkVerified') is True
                and re.fullmatch(r'[a-f0-9]{64}', tool.get('sha256', '')), 'tools_smoke_not_passed')
    if 'restore' in smoke:
        require(smoke['restore'].get('sourceUnchanged') is True
                and (smoke['restore'].get('originalDatabaseRowsPreserved') is True
                     or (smoke['restore'].get('requiredOriginalRowsPreserved') is True
                         and smoke['restore'].get('businessRowsPreserved') is True)),
                'restore_smoke_not_passed')
    return {'candidate': str(candidate), 'release': RELEASE, 'sourceAndBuildSha256': manifest['sourceAndBuildSha256'],
            'manifestSha256': backup._digest(manifest_path), 'evidenceSha256': backup._digest(evidence_path),
            'files': selected, 'images': {key: evidence[key] for key in IMAGE_KEYS}, 'smokeSha256': smoke_ref['sha256']}


class Host(ops.Host):
    def compose(self, *args, timeout=30):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {*IMAGE_KEYS.values(), 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_PROFILES'}}
        return subprocess.run(['docker', 'compose', '--project-directory', self.config['root'],
            '--env-file', str(Path(self.config['root']) / '.env'), *args],
            cwd=self.config['root'], env=environment, check=True, capture_output=True, text=True, timeout=timeout).stdout

    def image(self, reference):
        output = subprocess.run(['docker', 'image', 'inspect', '--format',
            '{{json .Id}} {{json .Os}} {{json .Architecture}}', reference], check=True, capture_output=True, text=True, timeout=20).stdout
        values = [json.loads(item) for item in output.strip().split()]
        require(len(values) == 3, 'image_inspection_failed')
        return dict(zip(('id', 'os', 'architecture'), values))

    def compose_configuration(self):
        return json.loads(self.compose('config', '--format', 'json'))

    def running_images(self):
        configuration = self.compose_configuration()
        result = {}
        for service in ('api', 'web'):
            identities = self.compose('ps', '--all', '--quiet', service).split()
            require(len(identities) == 1, 'unexpected_container_count')
            image_id = subprocess.run(['docker', 'inspect', '--format', '{{.Image}}', identities[0]],
                check=True, capture_output=True, text=True, timeout=20).stdout.strip()
            tag = configuration['services'][service]['image']
            actual = self.image(tag)
            require(actual['id'] == image_id, 'running_image_tag_has_changed')
            result[service] = {'tag': tag, **actual}
        return result

    def _inspect_containers(self, identities):
        if not identities:
            return []
        require(all(re.fullmatch(r'[a-f0-9]{12,64}', value) for value in identities), 'invalid_container_identity')
        template = '{"id":{{json .Id}},"image":{{json .Image}},"state":{{json .State.Status}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"workdir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}}}'
        output = subprocess.run(['docker', 'inspect', '--format', template, *identities], check=True,
                                capture_output=True, text=True, timeout=20).stdout
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def capture_containers(self):
        values = self._inspect_containers(self.compose('ps', '--all', '--quiet').split())
        require(values and len({item['project'] for item in values}) == 1, 'ambiguous_compose_project')
        require(all(item['workdir'] == self.config['root'] for item in values), 'container_working_directory_mismatch')
        return {'project': values[0]['project'], 'containers': values}

    def project_containers(self, state):
        project = state['oldContainers']['project']
        require(isinstance(project, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', project), 'invalid_recorded_compose_project')
        result = subprocess.run(['docker', 'ps', '--all', '--filter', 'label=com.docker.compose.project=' + project,
                                 '--format', '{{.ID}}'], check=True, capture_output=True, text=True, timeout=20)
        values = self._inspect_containers(result.stdout.split())
        require(all(item['project'] == project for item in values), 'recovery_container_scope_mismatch')
        # docker commit-derived images can retain project/service labels. An
        # exited standalone smoke container inherits those labels but has no
        # Compose workdir or oneoff ownership. It is not a deployment container
        # and must neither block recovery nor be stopped/restarted by it.
        values = [item for item in values if not (item['state'] in {'exited', 'dead'}
                  and not item.get('workdir') and not item.get('oneoff'))]
        require(all(item['workdir'] == self.config['root']
                    and str(item.get('oneoff', 'False')).lower() != 'true' for item in values), 'recovery_container_scope_mismatch')
        require(not any(item['service'] not in {'api', 'web', 'cad-egress'} and item['state'] == 'running' for item in values), 'unknown_running_service')
        return values

    def stop_for_recovery(self, state):
        # A bad new .env/overlay must never be consulted to stop the new API.
        # The persisted project/workdir identifies replacement containers too.
        values = self.project_containers(state)
        api = [item for item in values if item['service'] == 'api']
        require(len(api) <= 1, 'multiple_recovery_api_containers')
        if api:
            subprocess.run(['docker', 'stop', '--time', '60', api[0]['id']], check=True,
                           capture_output=True, text=True, timeout=90)
        require(not any(item['service'] == 'api' and item['state'] not in {'exited', 'created', 'dead'}
                        for item in self.project_containers(state)), 'rollback_api_still_running')

    def up(self):
        # Even --no-deps may retain healthy ordering between two explicitly
        # selected services. The closed maintenance gate intentionally blocks
        # API healthchecks, so each service must be started independently.
        for service in ('api', 'web'):
            self.compose('up', '-d', '--no-build', '--no-deps', '--pull', 'never', service, timeout=180)

    def _closed_images(self, state, expected):
        values = self.project_containers(state)
        selected = {}
        for service in ('api', 'web'):
            rows = [item for item in values if item['service'] == service]
            require(len(rows) == 1 and rows[0]['state'] == 'running'
                    and rows[0]['image'] == expected[service]['id'], 'closed_service_image_mismatch')
            selected[service] = rows[0]
        return selected

    def _verify_closed(self, state, expected, mode):
        require(_marker(self.config, state) is not None, 'maintenance_marker_missing')
        deadline = time.monotonic() + self.config['restartTimeoutSeconds']
        while time.monotonic() < deadline:
            try:
                containers = self._closed_images(state, expected)
                result = subprocess.run(['docker', 'exec', '-i', containers['api']['id'], 'python', '-c', LOOPBACK_PROBE],
                    input=json.dumps({'token': state['token'], 'mode': mode}), check=True,
                    capture_output=True, text=True, timeout=min(40, max(1, deadline - time.monotonic())))
                require(json.loads(result.stdout) == {'ok': True, 'mode': mode}, 'invalid_loopback_probe_result')
                self._closed_images(state, expected)
                return
            except (ActivationError, subprocess.SubprocessError, ValueError):
                time.sleep(1)
        raise ActivationError('candidate_closed_verification_failed' if mode == 'candidate' else 'rollback_closed_readiness_failed')

    def verify_gated(self, state, images):
        # Docker's ordinary healthcheck is blocked while the gate is closed.
        # Check the actual process and pinned images before opening admission.
        self._verify_closed(state, images, 'candidate')

    def verify_recovery_gated(self, state):
        # Old releases do not support the new maintenance probe. Their original
        # readiness endpoint still proves that Uvicorn has started while closed.
        require(backup._digest(Path(self.config['root']) / '.env') == state['oldEnvSha256'], 'rollback_environment_mismatch')
        self._verify_closed(state, state['oldImages'], 'recovery')


def _env_bytes(root):
    path = safe_path(Path(root) / '.env', exists=True)
    require(path.is_file(), 'production_env_missing')
    return path.read_bytes()


def updated_environment(original, images):
    try:
        text = original.decode('utf-8')
    except UnicodeError:
        raise ActivationError('env_not_utf8') from None
    lines = text.splitlines(keepends=True)
    indices = [i for i, line in enumerate(lines) if re.match(r'^\s*(?:export\s+)?COMPOSE_FILE\s*=', line)]
    require(len(indices) == 1, 'compose_file_setting_ambiguous')
    value = lines[indices[0]].split('=', 1)[1].strip()
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        value = value[1:-1]
    require(value.split(':') == list(OVERLAYS), 'unexpected_existing_compose_overlays')
    require(not any(re.match(r'^\s*(?:export\s+)?' + key + r'\s*=', line) for key in IMAGE_KEYS.values() for line in lines),
            'candidate_image_variables_already_present')
    newline = '\r\n' if '\r\n' in text else '\n'
    lines[indices[0]] = 'COMPOSE_FILE=' + ':'.join((*OVERLAYS, 'deploy/compose.currentcad.yaml')) + newline
    result = ''.join(lines)
    if result and not result.endswith(('\n', '\r')):
        result += newline
    result += '# Verified CurrentCAD candidate; prior policies remain above.' + newline
    for key, variable in IMAGE_KEYS.items():
        result += variable + '=' + _image(images[key])['tag'] + newline
    return result.encode('utf-8')


def _atomic_bytes(path, payload, mode):
    path = safe_path(path)
    temporary = path.with_name('.' + path.name + '.activation-' + uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _policy_digest(configuration):
    value = copy.deepcopy(configuration)
    for service in ('api', 'web'):
        value['services'][service].pop('image', None)
    api_build = value['services']['api'].get('build', {})
    api_build.pop('dockerfile', None)
    for key in ('CURRENT_CAD_IMAGE', 'DWG_TOOLS_IMAGE'):
        api_build.get('args', {}).pop(key, None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _topology(host, *, stopped=False):
    services = host.services()
    for service in ('api', 'web', 'cad-egress'):
        rows = [row for row in services if row['service'] == service]
        require(len(rows) == 1, 'unexpected_service_topology')
        if service == 'api' and stopped:
            require(rows[0]['state'] in {'exited', 'stopped'}, 'api_did_not_stop')
        else:
            require(rows[0]['state'] == 'running', 'required_service_not_running')
            if service == 'api':
                require(rows[0]['health'] == 'healthy', 'api_not_healthy')
    require(not any(row['service'] not in {'api', 'web', 'cad-egress'} and row['state'] == 'running' for row in services),
            'unknown_running_service')


def _no_work(host):
    require(host.jobs().get('activeTotal') == 0, 'active_or_queued_work')


def _layout(config, candidate=None):
    paths = [safe_path(config[key], exists=(key == 'root')) for key in ('root', 'backupRoot', 'reportRoot')]
    if candidate is not None:
        paths.append(safe_path(candidate, exists=True))
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            require(left != right and left not in right.parents and right not in left.parents, 'activation_trees_must_be_disjoint')
    root = paths[0]
    for name in (*TREES, *COPY_FILES, 'data/operations', 'data/operations/admission.lock'):
        safe_path(root / name)
    require((root / 'data/operations/admission.lock').is_file(), 'admission_lock_missing')


def plan(config, candidate, host=None):
    host = host or Host(config)
    _layout(config, candidate)
    artifact = validate_candidate(candidate, host)
    require(not (Path(config['reportRoot']) / 'recovery.json').exists(), 'commercial_recovery_pending')
    journal = Path(config['reportRoot']) / JOURNAL
    if journal.exists():
        require(_json(journal).get('stage') in TERMINAL, 'activation_recovery_pending')
    _topology(host)
    require(host.gate().get('maintenance') is False, 'maintenance_already_active')
    _no_work(host)
    original = _env_bytes(config['root'])
    updated_environment(original, artifact['images'])
    previous = host.running_images()
    require(all(artifact['images'][key + 'Image']['id'] != previous[key]['id'] for key in ('api', 'web')), 'candidate_reuses_running_image')
    require(artifact['images']['baseImage']['id'] == previous['api']['id'], 'candidate_base_not_current_api')
    return {**artifact, 'status': 'planned', 'deployment': config['root'], 'oldImages': previous,
            'oldEnvSha256': hashlib.sha256(original).hexdigest(), 'policySha256': _policy_digest(host.compose_configuration()),
            'oldContainers': host.capture_containers()}


def _marker(config, state, *, create=False):
    path = safe_path(Path(config['root']) / 'data/operations/maintenance.json')
    if path.exists():
        require(_json(path).get('token') == state['token'], 'maintenance_owned_by_another_operation')
        return path
    if not create:
        return None
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as handle:
        json.dump({'format': FORMAT, 'token': state['token'], 'createdAt': ops.stamp()}, handle)
        handle.flush(); os.fsync(handle.fileno())
    return path


def _save(config, state, stage):
    state.update(stage=stage, updatedAt=ops.stamp())
    ops.atomic_json(Path(config['reportRoot']) / JOURNAL, state, 0o600)


def _replace_tree(source, target, token, names):
    safe_path(target)
    staging = target.with_name('.' + target.name + '.currentcad-stage-' + token)
    old = target.with_name('.' + target.name + '.currentcad-old-' + token)
    require(not staging.exists() and not old.exists(), 'leftover_code_swap_requires_recovery')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o755)
    for relative in names:
        original = safe_path(source / relative, exists=True)
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
    if target.exists():
        target.rename(old)
    staging.rename(target)
    if old.exists():
        shutil.rmtree(old)


def _install(root, candidate, files, token):
    for tree in TREES:
        names = [name[len(tree) + 1:] for name in files if name.startswith(tree + '/')]
        if not names and not (candidate / tree).exists():
            continue
        _replace_tree(candidate / tree, root / tree, token, names)
    for name in COPY_FILES:
        original = candidate / name
        _atomic_bytes(root / name, original.read_bytes(), stat.S_IMODE(original.stat().st_mode))


def _restore_code(config, state):
    directory = safe_path(state['backup'], exists=True)
    require(directory.parent == safe_path(config['backupRoot']), 'recovery_backup_outside_configured_root')
    manifest = backup.verify(directory)
    require(backup._digest(directory / 'manifest.json') == state.get('backupManifestSha256'), 'recovery_backup_manifest_changed')
    root, source = Path(config['root']), directory / 'snapshot'
    for tree in TREES:
        # Clean only this journal's exact swap paths, including a crash between renames.
        target = root / tree
        for suffix in ('stage', 'old'):
            leftover = target.with_name('.' + target.name + '.currentcad-' + suffix + '-' + state['token'])
            safe_path(leftover)
            if leftover.exists():
                shutil.rmtree(leftover)
        if (source / tree).is_dir():
            names = [name[len(tree) + 1:] for name in manifest['files'] if name.startswith(tree + '/')]
            _replace_tree(source / tree, target, state['token'], names)
        elif target.exists() and tree in state['installedTrees']:
            shutil.rmtree(target)
    for name in COPY_FILES:
        saved, target = source / name, safe_path(root / name)
        if saved.is_file():
            _atomic_bytes(target, saved.read_bytes(), stat.S_IMODE(saved.stat().st_mode))
        elif target.exists():
            require(target.is_file(), 'unexpected_rollback_deploy_path')
            target.unlink()
    saved_env = source / '.env'
    require(backup._digest(saved_env) == state['oldEnvSha256'], 'old_environment_backup_mismatch')
    _atomic_bytes(root / '.env', saved_env.read_bytes(), stat.S_IMODE(saved_env.stat().st_mode))


def _check_old_images(host, state):
    for item in state['oldImages'].values():
        require(host.image(item['tag'])['id'] == item['id'], 'rollback_image_tag_changed')


def _rollback(config, state, host, *, admission_owned=False):
    if not state['stopRequested']:
        marker = _marker(config, state)
        if marker:
            marker.unlink()
        _save(config, state, 'aborted_no_change')
        return
    _marker(config, state, create=True)
    @contextmanager
    def admission():
        if admission_owned:
            yield
        else:
            with ops.exclusive(Path(config['root']) / 'data/operations/admission.lock', create=False):
                yield
    with admission():
        _no_work(host)
        host.stop_for_recovery(state)
        _no_work(host)
        if state['codeChangeStarted'] or state['envChangeStarted']:
            _restore_code(config, state)
        _check_old_images(host, state)
        host.up()
        host.verify_recovery_gated(state)
    # Release admission before health requests; only remove our own marker.
    marker = _marker(config, state)
    if marker:
        marker.unlink()
    host.wait_ready()
    actual = host.running_images()
    require(all(actual[key]['id'] == state['oldImages'][key]['id'] for key in ('api', 'web')), 'rollback_running_image_mismatch')
    _save(config, state, 'rolled_back')


def activate(config, candidate, host=None, *, execute=False):
    host = host or Host(config)
    if not execute:
        return plan(config, candidate, host)
    reports = safe_path(config['reportRoot'])
    reports.mkdir(parents=True, exist_ok=True, mode=0o750)
    with ops.action_lock(reports / 'action.lock'):
        checked = plan(config, candidate, host)
        root, candidate = Path(config['root']), Path(checked['candidate'])
        token = uuid4().hex
        backup_root = safe_path(config['backupRoot']); backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        size = sum(path.stat().st_size for name in backup.INCLUDE for path in
                   ([root / name] if (root / name).is_file() else (root / name).rglob('*')) if path.is_file() and not path.is_symlink())
        require(ops.disk_report(backup_root)['freeBytes'] >= size * 2 + config['minimumFreeBytes'], 'insufficient_backup_space')
        state = {'format': FORMAT, 'deployment': config['root'], 'release': RELEASE, 'token': token,
                 'candidate': checked['candidate'], 'sourceAndBuildSha256': checked['sourceAndBuildSha256'],
                 'manifestSha256': checked['manifestSha256'], 'evidenceSha256': checked['evidenceSha256'],
                 'oldImages': checked['oldImages'], 'oldContainers': checked['oldContainers'], 'newImages': checked['images'], 'oldEnvSha256': checked['oldEnvSha256'],
                 'backup': str(backup_root / ('currentcad-' + token)), 'backupManifestSha256': None,
                 'stopRequested': False, 'codeChangeStarted': False, 'envChangeStarted': False,
                 'installedTrees': [tree for tree in TREES if (candidate / tree).is_dir()], 'createdAt': ops.stamp()}
        _save(config, state, 'maintenance_pending')
        try:
            _marker(config, state, create=True)
            with ops.exclusive(root / 'data/operations/admission.lock', create=False):
                require(host.gate().get('maintenance') is True, 'maintenance_gate_not_observed')
                _no_work(host)
                state['stopRequested'] = True; _save(config, state, 'stop_requested')
                host.stop(); _topology(host, stopped=True); _no_work(host)
                _save(config, state, 'api_stopped')
                # The existing backup format already supports arbitrary manifest files.
                # Extend this invocation's inventory only; do not rewrite commercial tooling.
                previous_include = backup.INCLUDE
                try:
                    backup.INCLUDE = tuple(dict.fromkeys((*previous_include, *ROOT_FILES)))
                    backup.backup(root, state['backup'], quiescent=True)
                finally:
                    backup.INCLUDE = previous_include
                backup.verify(state['backup'])
                state['backupManifestSha256'] = backup._digest(Path(state['backup']) / 'manifest.json')
                require(backup._digest(Path(state['backup']) / 'snapshot/.env') == state['oldEnvSha256'], 'environment_changed_during_activation')
                _save(config, state, 'backup_verified')
                # Recheck the complete candidate after waiting for quiescence and backup.
                again = validate_candidate(candidate, host)
                require(again == {key: checked[key] for key in again}, 'candidate_changed_during_activation')
                state['codeChangeStarted'] = True; _save(config, state, 'installing_code')
                _install(root, candidate, checked['files'], token)
                require(all(backup._digest(safe_path(root / name, exists=True)) == digest for name, digest in checked['files'].items()),
                        'installed_code_checksum_mismatch')
                _save(config, state, 'code_installed')
                state['envChangeStarted'] = True; _save(config, state, 'updating_environment')
                current_env = _env_bytes(root)
                require(hashlib.sha256(current_env).hexdigest() == state['oldEnvSha256'], 'environment_changed_during_activation')
                _atomic_bytes(root / '.env', updated_environment(current_env, checked['images']), stat.S_IMODE((root / '.env').stat().st_mode))
                effective = host.compose_configuration()
                require(_policy_digest(effective) == checked['policySha256'], 'compose_policy_changed')
                require(all(effective['services'][key]['image'] == checked['images'][key + 'Image']['tag'] for key in ('api', 'web')), 'compose_image_selection_mismatch')
                _check_old_images(host, state)
                _save(config, state, 'starting_candidate')
                host.up()
                _save(config, state, 'checking_candidate_closed')
                host.verify_gated(state, {key: checked['images'][key + 'Image'] for key in ('api', 'web')})
                _no_work(host)
            marker = _marker(config, state)
            if marker:
                marker.unlink()
            _save(config, state, 'checking_candidate')
            host.wait_ready()
            _save(config, state, 'completed')
            return state
        except BaseException as error:
            state['failure'] = str(error) if isinstance(error, ActivationError) else type(error).__name__
            try:
                _save(config, state, 'rollback_pending')
            except OSError:
                pass  # A report-disk error must not prevent the service recovery attempt.
            try:
                _rollback(config, state, host)
            except BaseException as recovery_error:
                state['recoveryFailure'] = str(recovery_error) if isinstance(recovery_error, ActivationError) else type(recovery_error).__name__
                _save(config, state, 'recovery_required')
                raise ActivationError('activation_failed_manual_recovery_required') from None
            raise ActivationError('activation_failed_rolled_back' if state['stopRequested'] else 'activation_refused_no_change') from None


def _recovery_state(config):
    journal = safe_path(Path(config['reportRoot']) / JOURNAL, exists=True)
    state = _json(journal)
    require(state.get('format') == FORMAT and state.get('deployment') == config['root'] and state.get('release') == RELEASE
            and isinstance(state.get('token'), str) and re.fullmatch(r'[a-f0-9]{32}', state['token'])
            and all(type(state.get(key)) is bool for key in ('stopRequested', 'codeChangeStarted', 'envChangeStarted')),
            'invalid_activation_journal')
    require(not (Path(config['reportRoot']) / 'recovery.json').exists(), 'commercial_recovery_pending')
    return state


def recover(config, host=None, *, execute=False):
    host = host or Host(config)
    _layout(config)
    if not execute:
        state = _recovery_state(config)
        return {'status': 'no_recovery_needed', 'stage': state['stage']} if state['stage'] in TERMINAL else {
            'status': 'recovery_planned', 'stage': state['stage'], 'backup': state['backup'], 'oldImages': state['oldImages']}
    with ops.action_lock(Path(config['reportRoot']) / 'action.lock'):
        state = _recovery_state(config)
        if state['stage'] in TERMINAL:
            return {'status': 'no_recovery_needed', 'stage': state['stage']}
        _rollback(config, state, host)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Existing commercial_operations JSON configuration')
    parser.add_argument('--candidate', type=Path, default=Path('/opt/joyniu-candidates/currentcad-delivery-20260912'))
    parser.add_argument('--execute', action='store_true', help='Explicitly perform the reviewed stop/backup/activate or recover sequence')
    parser.add_argument('--recover', action='store_true', help='Recover this script\'s pending activation journal; never restore data')
    args = parser.parse_args(argv)
    def interrupted(signum, frame):
        raise ActivationError('activation_interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        config = ops.configuration(args.config)
        result = recover(config, execute=args.execute) if args.recover else activate(config, args.candidate, execute=args.execute)
        # Public output contains identifiers/hashes only, not source inventories, config or customer rows.
        allowed = ('status', 'stage', 'release', 'sourceAndBuildSha256', 'backup', 'oldImages', 'newImages', 'images', 'failure')
        print(json.dumps({key: result[key] for key in allowed if key in result}))
        return 0
    except ops.OperationInProgress:
        print(json.dumps({'status': 'refused', 'reason': 'operation_in_progress'})); return 2
    except Exception as error:
        print(json.dumps({'status': 'failed', 'reason': str(error) if isinstance(error, ActivationError) else type(error).__name__,
                          'journal': JOURNAL})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
