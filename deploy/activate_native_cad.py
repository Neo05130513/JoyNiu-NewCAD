#!/usr/bin/env python3
"""Native 2D release using the existing backup, admission and rollback gates.

Run with --config /etc/joyniu-cad/operations.json --candidate PATH.
Default is read-only; --execute activates; --recover recovers this journal.
"""
import importlib.util
from pathlib import Path
import sys
import re

sys.dont_write_bytecode = True
directory = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_native_activation_base', directory / 'activate_currentcad.py')
base = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(directory))
try:
    spec.loader.exec_module(base)
finally:
    sys.path.pop(0)
base.RELEASE = 'native-cad-ribbon-20260913'
base.JOURNAL = 'native-cad-activation.json'
base.DEPLOY_FILES = (*base.DEPLOY_FILES, 'deploy/activate_native_cad.py', 'deploy/native_cad_release_smoke.py', 'deploy/Dockerfile.cad-editor')
base.COPY_FILES = (*base.DEPLOY_FILES, *base.ROOT_FILES)
base.__doc__ = __doc__
original_validate = base.validate_candidate
original_plan = base.plan


def updated_environment(original, images):
    """Update only the four established image settings, retaining all policies."""
    lines = original.decode('utf-8').splitlines(keepends=True)
    compose = [line for line in lines if re.match(r'^\s*(?:export\s+)?COMPOSE_FILE\s*=', line)]
    base.require(len(compose) == 1, 'compose_file_setting_ambiguous')
    value = compose[0].split('=', 1)[1].strip().strip('\"\'')
    base.require(value.split(':') == [*base.OVERLAYS, 'deploy/compose.currentcad.yaml'], 'unexpected_existing_compose_overlays')
    for key, variable in base.IMAGE_KEYS.items():
        found = [i for i, line in enumerate(lines) if re.match(r'^\s*(?:export\s+)?' + variable + r'\s*=', line)]
        base.require(len(found) == 1, 'candidate_image_setting_ambiguous')
        i = found[0]
        ending = '\r\n' if lines[i].endswith('\r\n') else '\n' if lines[i].endswith('\n') else ''
        lines[i] = variable + '=' + base._image(images[key])['tag'] + ending
    return ''.join(lines).encode('utf-8')


def plan(config, candidate, host=None):
    for name in ('currentcad-activation.json', 'cad-editor-activation.json'):
        path = Path(config['reportRoot']) / name
        if path.exists():
            base.require(base._json(path).get('stage') in base.TERMINAL, 'prior_release_recovery_pending')
    return original_plan(config, candidate, host)


def validate_candidate(candidate, host):
    checked = original_validate(candidate, host)
    directory = Path(checked['candidate'])
    evidence = base._json(directory / 'candidate-verification.json')
    reference = evidence['smoke']
    base.require(reference.get('entrypoint') == 'deploy/native_cad_release_smoke.py'
                 and reference.get('nativeScriptSha256') == checked['files']['deploy/native_cad_release_smoke.py'],
                 'native_smoke_binding_mismatch')
    report_path = Path(reference['path'])
    if not report_path.is_absolute():
        report_path = directory / report_path
    report = base._json(report_path)
    base.require(base.backup._digest(report_path) == checked['smokeSha256'], 'native_smoke_changed')
    base.require(all(report.get('native', {}).get('workspace', {}).get(key) is True
                     for key in ('ellipse', 'groups', 'layoutViewport', 'dxfReadback', 'svgPreview', 'reopen', 'undoRedo')),
                 'native_workspace_smoke_not_passed')
    return checked


base.validate_candidate, base.updated_environment, base.plan = validate_candidate, updated_environment, plan
if __name__ == '__main__':
    raise SystemExit(base.main())
