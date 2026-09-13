#!/usr/bin/env python3
"""Activate the reviewed CAD editor candidate; default is a read-only plan.

 python3 deploy/activate_cad_editor.py --config /etc/joyniu-cad-operations.json \
   --candidate /opt/joyniu-candidates/cad-editor-parity-20260913
Append --execute to activate, or --recover [--execute] for this release's
independent cad-editor-activation.json journal. Recovery restores code/images
and the exact previous .env; it never restores customer data.

candidate-verification.json retains the baseline smoke bindings and additionally
requires smoke.entrypoint='deploy/cad_editor_release_smoke.py' and
smoke.editorScriptSha256. smoke.scriptSha256 binds the baseline sibling script;
both are executed in the same bound container against the same source snapshot.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import zipfile

sys.dont_write_bytecode = True
# Use a private module instance: importing this wrapper must not reconfigure the
# historical release's public module, constants, journal or recovery behavior.
_directory = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('_joyniu_cad_editor_activation_base', _directory / 'activate_currentcad.py')
base = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(_directory))
try:
    _spec.loader.exec_module(base)
finally:
    sys.path.pop(0)

RELEASE = 'cad-editor-parity-20260913'
JOURNAL = 'cad-editor-activation.json'
ENTRYPOINT = 'deploy/cad_editor_release_smoke.py'
COMPLETE_HELPER = 'deploy/cad_complete_editor_smoke.py'
_complete_spec = importlib.util.spec_from_file_location('_editor_complete_smoke_contract', _directory / 'cad_complete_editor_smoke.py')
complete = importlib.util.module_from_spec(_complete_spec)
_complete_spec.loader.exec_module(complete)
# FORMAT is also the API maintenance-marker wire protocol, not a release name.
# Keep that protocol unchanged; RELEASE and JOURNAL isolate this activation.
# Interrupted r1/r2 journals retain their old wrapper for recovery.
FORMAT = base.FORMAT
HISTORICAL_JOURNAL = base.JOURNAL
base.RELEASE, base.JOURNAL = RELEASE, JOURNAL
base.DEPLOY_FILES = tuple(dict.fromkeys((*base.DEPLOY_FILES, 'deploy/Dockerfile.cad-editor', ENTRYPOINT, COMPLETE_HELPER, 'deploy/activate_cad_editor.py')))
base.COPY_FILES = (*base.DEPLOY_FILES, *base.ROOT_FILES)
base.__doc__ = __doc__
ActivationError, require, Host = base.ActivationError, base.require, base.Host
_original_validate, _original_environment, _original_plan = base.validate_candidate, base.updated_environment, base.plan


def validate_candidate(candidate, host):
    checked = _original_validate(candidate, host)
    directory = Path(checked['candidate'])
    evidence = base._json(directory / 'candidate-verification.json')
    smoke_ref = evidence['smoke']
    script_hash = checked['files'][ENTRYPOINT]
    require(smoke_ref.get('entrypoint') == ENTRYPOINT
            and smoke_ref.get('editorScriptSha256') == script_hash,
            'editor_smoke_runtime_binding_mismatch')
    path = Path(smoke_ref['path'])
    path = path if path.is_absolute() else directory / path
    smoke = base._json(path)
    require(base.backup._digest(path) == checked['smokeSha256']
            and base.backup._digest(directory / 'candidate-verification.json') == checked['evidenceSha256'],
            'editor_evidence_changed_during_validation')
    geometry = smoke.get('engineering', {}).get('cadEditor')
    require(isinstance(geometry, dict)
            and type(geometry.get('topologyFaces')) is int and geometry['topologyFaces'] == 6
            and type(geometry.get('topologyEdges')) is int and geometry['topologyEdges'] == 12
            and all(geometry.get(key) is True for key in (
                'triangleMembership', 'selectedEdgeFillet', 'atomicFailurePreservedHead',
                'idempotentRetry', 'parameterHistoryRebuilt', 'profileHistoryRebuilt', 'compoundBodiesRebuilt', 'stepReadback'))
            and type(geometry.get('volumeMm3')) in (int, float)
            and math.isfinite(geometry['volumeMm3']) and geometry['volumeMm3'] > 0,
            'editor_geometry_smoke_not_passed')
    http = smoke.get('http', {}).get('cadEditor')
    require(isinstance(http, dict) and all(http.get(key) is True for key in (
        'anonymousRejected', 'readerRejected', 'ownerOnlyArtifacts', 'staleInteractivePickRejected',
        'atomicHttpCommit', 'httpIdempotency', 'foreignUpdateRejected', 'revisionConflictPreservedHead',
        'maintenanceProbe', 'commitWaitedForPreviews')),
        'editor_http_smoke_not_passed')
    validate_complete_editor(smoke.get('engineering', {}).get('completeEditor'), path.parent, checked['files'][COMPLETE_HELPER])
    return {**checked, 'editorSmokeScriptSha256': script_hash, 'completeEditorScriptSha256': checked['files'][COMPLETE_HELPER], 'smokeEntrypoint': ENTRYPOINT}



def validate_complete_editor(report, evidence_directory, expected_script):
    """Require expanded native checks AND the actual immutable evidence files."""
    code='complete_editor_smoke_not_passed'
    require(isinstance(report,dict) and report.get('format')==complete.FORMAT
            and report.get('scriptSha256')==expected_script
            and report.get('isolatedSyntheticData') is True,code)
    checks=report.get('checks')
    require(isinstance(checks,dict) and all(checks.get(key) is True for key in complete.CHECKS),code)
    geometries=report.get('geometries')
    require(isinstance(geometries,dict) and set(geometries)==set(complete.GEOMETRIES),code)
    def finite(value):
        return type(value) in (int,float) and math.isfinite(value)
    def artifact(record, name):
        require(isinstance(record,dict) and record.get('name')==name
                and type(record.get('bytes')) is int and 100<record['bytes']<=100*1024**2
                and isinstance(record.get('sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',record['sha256']),code)
        target=base.safe_path(evidence_directory/name,exists=True)
        require(target.is_file() and target.stat().st_size==record['bytes']
                and base.backup._digest(target)==record['sha256'],'complete_editor_artifact_mismatch')
    for name,solids in complete.GEOMETRIES.items():
        row=geometries[name]
        require(isinstance(row,dict) and row.get('valid') is True
                and type(row.get('solidCount')) is int and row['solidCount']==solids
                and type(row.get('faceCount')) is int and row['faceCount']>0
                and finite(row.get('volumeMm3')) and (row['volumeMm3']>0 if solids else abs(row['volumeMm3'])<1e-7)
                and finite(row.get('areaMm2')) and row['areaMm2']>0
                and isinstance(row.get('sizeMm'),list) and len(row['sizeMm'])==3
                and all(finite(v) and v>=0 for v in row['sizeMm'])
                and sum(v>0 for v in row['sizeMm'])>=2,code)
        artifact(row.get('step'),f'complete-editor-{name}.step')
    details=report.get('details')
    require(isinstance(details,dict),code)
    g2,pmi,pdm,helix=(details.get(key,{}) for key in ('g2','pmi','pdm','helix'))
    require(all(isinstance(value,dict) for value in (g2,pmi,pdm,helix)),code)
    require(g2.get('nonzeroSourceCurvature') is True and type(g2.get('edgeSamples')) is int and g2['edgeSamples']>=4
            and finite(g2.get('maxCurvatureError')) and 0<=g2['maxCurvatureError']<=2e-4,code)
    require(pmi.get('measurementBeforeMm')==16 and pmi.get('measurementAfterMm')==24
            and pmi.get('savedRevisions')==2 and pmi.get('oldVersionPreserved') is True,code)
    require(helix.get('savedRevisions')==2 and helix.get('radiusBefore')==5 and helix.get('radiusAfter')==7
            and helix.get('pitchAfter')==5 and helix.get('exactCurvedEdges')==1
            and helix.get('previewPreservedHead') is True
            and type(helix.get('previewVertices')) is int and helix['previewVertices']>0
            and type(helix.get('previewTriangles')) is int and helix['previewTriangles']>0,code)
    require(all(pdm.get(key) is True for key in ('idempotent','missingSourceRestored','independentRebuild'))
            and pdm.get('manufacturingConfirmed') is False,code)
    artifact(pdm.get('archive'),'complete-editor-pdm.zip')
    community=details.get('community')
    require(isinstance(community,dict) and all(community.get(key) is True for key in (
        'publicFieldsOnly','publicationIdempotent','copyIdempotent','immutablePublishedRevision','independentCopy',
        'parameterEdit','foreignWithdrawRejected','withdrawalRejectedDownloads','withdrawalRejectedOpen',
        'withdrawalRejectedRetry','existingCopyPreserved')) and community.get('manufacturingConfirmed') is False,code)
    require(community.get('sourceRevision')==1 and community.get('sourceHeadRevision')==2
            and community.get('copyRevision')==2 and community.get('publishedVolumeMm3')==600
            and community.get('editedCopyVolumeMm3')==1000,code)
    for name,expected in (('community-download',600),('community-edited-copy',1000)):
        require(abs(geometries[name]['volumeMm3']-expected)<1e-5,code)
    artifact(community.get('glb'),'complete-editor-community.glb')
    artifact(community.get('archive'),'complete-editor-community.zip')
    try:
        with zipfile.ZipFile(evidence_directory/'complete-editor-community.zip') as bundle:
            entries=bundle.infolist()
            require(len(entries)==4 and {item.filename for item in entries}=={'manifest.json','public.json','model.step','model.glb'}
                    and all(0<item.file_size<=100*1024**2 for item in entries)
                    and sum(item.file_size for item in entries)<=100*1024**2,code)
            manifest=json.loads(bundle.read('manifest.json'))
            require(manifest.get('format')=='joyniu-community-smoke-snapshot-v1'
                    and set(manifest.get('files',{}))=={'public.json','model.step','model.glb'},code)
            for name,row in manifest['files'].items():
                raw=bundle.read(name)
                require(type(row.get('bytes')) is int and row['bytes']==len(raw)
                        and row.get('sha256')==hashlib.sha256(raw).hexdigest(),code)
            require(manifest['files']['model.step']['sha256']==geometries['community-download']['step']['sha256']
                    and manifest['files']['model.glb']['sha256']==community['glb']['sha256'],code)
            public=json.loads(bundle.read('public.json'))
            require(set(public)=={'id','name','description','publishedAt','status','inspection','canWithdraw','sharing',
                    'drawingAgreement','productionReady','artifacts'} and public.get('canWithdraw') is False
                    and public.get('status')=='published' and public.get('sharing')=='public-copy-download'
                    and public.get('productionReady') is False and public.get('drawingAgreement')=='not_checked',code)
    except (OSError,ValueError,KeyError,TypeError,AttributeError,zipfile.BadZipFile):
        raise ActivationError(code) from None


def updated_environment(original, images):
    """Replace only the existing overlay's release image keys; preserve policies."""
    try:
        text = original.decode('utf-8')
    except UnicodeError:
        raise ActivationError('env_not_utf8') from None
    lines = text.splitlines(keepends=True)
    compose = [line for line in lines if re.match(r'^\s*(?:export\s+)?COMPOSE_FILE\s*=', line)]
    require(len(compose) == 1, 'compose_file_setting_ambiguous')
    value = compose[0].split('=', 1)[1].strip()
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        value = value[1:-1]
    if value.split(':') == list(base.OVERLAYS):
        return _original_environment(original, images)
    require(value.split(':') == [*base.OVERLAYS, 'deploy/compose.currentcad.yaml'], 'unexpected_existing_compose_overlays')
    for key, variable in base.IMAGE_KEYS.items():
        found = [index for index, line in enumerate(lines) if re.match(r'^\s*(?:export\s+)?' + variable + r'\s*=', line)]
        require(len(found) == 1, 'candidate_image_setting_ambiguous')
        index = found[0]
        ending = '\r\n' if lines[index].endswith('\r\n') else '\n' if lines[index].endswith('\n') else ''
        lines[index] = variable + '=' + base._image(images[key])['tag'] + ending
    return ''.join(lines).encode('utf-8')


def plan(config, candidate, host=None):
    previous = Path(config['reportRoot']) / HISTORICAL_JOURNAL
    if previous.exists():
        require(base._json(previous).get('stage') in base.TERMINAL, 'historical_activation_recovery_pending')
    return _original_plan(config, candidate, host)


base.validate_candidate, base.updated_environment, base.plan = validate_candidate, updated_environment, plan
activate, recover = base.activate, base.recover


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if not any(value == '--candidate' or value.startswith('--candidate=') for value in values):
        values += ['--candidate', '/opt/joyniu-candidates/' + RELEASE]
    return base.main(values)


if __name__ == '__main__':
    raise SystemExit(main())
