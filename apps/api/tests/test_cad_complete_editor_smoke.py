"""Expanded release evidence comes from the real isolated OCCT runtime."""
import hashlib
import importlib.util
import json
import math
import zipfile
from pathlib import Path

import pytest
from app.geometry import get_cadquery

DEPLOY=Path(__file__).resolve().parents[3]/'deploy'
spec=importlib.util.spec_from_file_location('_complete_editor_candidate_smoke',DEPLOY/'cad_complete_editor_smoke.py')
smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)


@pytest.fixture(scope='module')
def complete(tmp_path_factory):
    root=tmp_path_factory.mktemp('complete-editor-smoke')
    output=root/'evidence';output.mkdir()
    sentinel=root/'customer-must-not-change.step';sentinel.write_bytes(b'read-only-test-sentinel')
    result=smoke.complete_editor_smoke(root,'synthetic-candidate-owner',output)
    assert sentinel.read_bytes()==b'read-only-test-sentinel'
    assert not list(root.glob('complete-editor-*'))
    (output/'complete-editor-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result,output


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_complete_editor_runtime_exchanges_every_declared_geometry_and_rebuilds_archived_dependencies(complete):
    result,output=complete
    assert result['format']==smoke.FORMAT and result['isolatedSyntheticData'] is True
    assert result['scriptSha256']==hashlib.sha256((DEPLOY/'cad_complete_editor_smoke.py').read_bytes()).hexdigest()
    assert set(result['checks'])==set(smoke.CHECKS) and all(v is True for v in result['checks'].values())
    assert set(result['geometries'])==set(smoke.GEOMETRIES)
    for name,expected in smoke.GEOMETRIES.items():
        row=result['geometries'][name];path=output/row['step']['name'];raw=path.read_bytes()
        assert len(raw)==row['step']['bytes'] and hashlib.sha256(raw).hexdigest()==row['step']['sha256']
        shape=get_cadquery().importers.importStep(str(path)).val()
        assert shape.isValid() and len(shape.Solids())==expected
        assert sum(s.Volume() for s in shape.Solids())==pytest.approx(row['volumeMm3'],rel=1e-6,abs=1e-5)
        assert shape.Area()==pytest.approx(row['areaMm2'],rel=1e-6)
    assert result['geometries']['spline']['volumeMm3']==pytest.approx(1450/3,rel=1e-8)
    assert result['geometries']['multi-contour']['volumeMm3']==pytest.approx((300-4*math.pi)*5,rel=1e-8)
    assert result['details']['g2']['nonzeroSourceCurvature'] and result['details']['g2']['edgeSamples']==4
    assert result['details']['g2']['maxCurvatureError']<2e-4
    assert result['details']['pmi']['measurementAfterMm']==24
    assert result['details']['pdm']['missingSourceRestored'] and result['details']['pdm']['independentRebuild']
    assert result['details']['pdm']['manufacturingConfirmed'] is False
    archive=result['details']['pdm']['archive'];assert hashlib.sha256((output/archive['name']).read_bytes()).hexdigest()==archive['sha256']
    community=result['details']['community']
    assert community['sourceRevision']==1 and community['sourceHeadRevision']==2 and community['copyRevision']==2
    assert community['withdrawalRejectedDownloads'] and community['withdrawalRejectedOpen'] and community['withdrawalRejectedRetry']
    assert community['publicFieldsOnly'] and community['existingCopyPreserved'] and not community['manufacturingConfirmed']
    assert result['geometries']['community-download']['volumeMm3']==pytest.approx(600)
    assert result['geometries']['community-edited-copy']['volumeMm3']==pytest.approx(1000)
    archive=community['archive'];assert hashlib.sha256((output/archive['name']).read_bytes()).hexdigest()==archive['sha256']
    with zipfile.ZipFile(output/archive['name']) as bundle:
        assert set(bundle.namelist())=={'manifest.json','public.json','model.step','model.glb'}
        assert bundle.read('model.step')==(output/'complete-editor-community-download.step').read_bytes()
        assert bundle.read('model.glb')==(output/'complete-editor-community.glb').read_bytes()
        assert bundle.read('model.glb')[:4]==b'glTF'
        public=json.loads(bundle.read('public.json'))
        assert public['canWithdraw'] is False and public['productionReady'] is False
        assert not {'owner','source','fileId','sourceRun','plan','changeNote'}&set(public)
    text=json.dumps(result)
    assert str(output) not in text and 'synthetic-candidate-owner' not in text


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_actual_complete_runtime_evidence_passes_the_activation_gate_and_changed_file_is_refused(complete):
    result,output=complete
    gate_spec=importlib.util.spec_from_file_location('_complete_runtime_actual_gate',DEPLOY/'activate_cad_editor.py')
    gate=importlib.util.module_from_spec(gate_spec);gate_spec.loader.exec_module(gate)
    expected=hashlib.sha256((DEPLOY/'cad_complete_editor_smoke.py').read_bytes()).hexdigest()
    gate.validate_complete_editor(result,output,expected)
    path=output/result['geometries']['style']['step']['name'];original=path.read_bytes()
    try:
        path.write_bytes(original+b'changed-after-evidence')
        with pytest.raises(gate.ActivationError,match='complete_editor_artifact_mismatch'):
            gate.validate_complete_editor(result,output,expected)
    finally:path.write_bytes(original)


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_actual_community_download_is_required_by_activation_after_service_withdrawal(complete):
    result,output=complete
    gate_spec=importlib.util.spec_from_file_location('_community_runtime_actual_gate',DEPLOY/'activate_cad_editor.py')
    gate=importlib.util.module_from_spec(gate_spec);gate_spec.loader.exec_module(gate)
    expected=hashlib.sha256((DEPLOY/'cad_complete_editor_smoke.py').read_bytes()).hexdigest()
    path=output/'complete-editor-community.glb';original=path.read_bytes()
    try:
        path.write_bytes(original+b'changed-community-model')
        with pytest.raises(gate.ActivationError,match='complete_editor_artifact_mismatch'):
            gate.validate_complete_editor(result,output,expected)
    finally:path.write_bytes(original)
