"""The candidate history gate follows real preview picks in one saved design."""
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest
from app.geometry import get_cadquery

DEPLOY=Path(__file__).resolve().parents[3]/'deploy'
sys.path.insert(0,str(DEPLOY))
try:
    spec=importlib.util.spec_from_file_location('_editor_history_release_smoke',DEPLOY/'cad_editor_release_smoke.py')
    smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
finally:
    sys.path.remove(str(DEPLOY))


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_box_sketch_boss_and_two_fillet_edges_follow_profile_conversion_with_real_step(tmp_path):
    output=tmp_path/'evidence';output.mkdir()
    result=smoke.editor_profile_history(tmp_path,'synthetic-release-owner',output)
    assert result['selectedBossEdges']==2 and result['savedRevisions']==4
    assert result['bossAttachmentMovementMm']==pytest.approx([1,0,0])
    assert result['volumeAfterMm3']-result['volumeBeforeMm3']==pytest.approx(320)
    assert result['previousVersionPreserved'] and result['failurePreservedHead'] and result['foreignDesignBindingRejected']
    plan=json.loads((output/'editor-profile-history-plan.json').read_text())
    assert plan['features'][0]['op']=='profile_extrude' and len(plan['features'][0]['segments'])==4
    boss=next(feature for feature in plan['features'] if feature['id']=='boss')
    assert boss['planeSource']['binding']['version']==1 and boss['planeAttachment']['version']==1
    assert all(edge['binding']['version']==1 for edge in plan['features'][-1]['edges'])
    actual=get_cadquery().importers.importStep(str(output/'editor-profile-history.step')).val()
    assert actual.isValid() and len(actual.Solids())==1
    assert actual.Volume()==pytest.approx(22*16*10+10*8*5-20*(1-math.pi/4),abs=1e-5)
    bounds=actual.BoundingBox()
    assert [bounds.xlen,bounds.ylen,bounds.zlen]==pytest.approx([22,16,15])
    assert (output/'editor-profile-history.glb').read_bytes()[:4]==b'glTF'


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_new_body_stays_independent_in_step_after_fillet_and_old_body_parameter_edit(tmp_path):
    output=tmp_path/'evidence';output.mkdir()
    result=smoke.editor_compound_bodies(tmp_path,'synthetic-multibody-owner',output)
    assert result['solidCount']==2 and result['savedRevisions']==4
    assert result['selectedEdgeFillet'] and result['parameterHistoryRebuilt']
    assert result['stepReadback'] and result['previousVersionPreserved'] and result['failurePreservedHead']
    assert result['overlapBeforeMm3']==pytest.approx(200)
    assert result['overlapAfterMm3']==pytest.approx(280)
    plan=json.loads((output/'editor-compound-bodies-plan.json').read_text())
    assert plan['features'][-2]=={'id':'bodies','op':'compound','inputs':['body','second']}
    assert plan['features'][-1]['input']=='bodies' and plan['features'][-1]['edges'][0]['binding']['version']==1
    shape=get_cadquery().importers.importStep(str(output/'editor-compound-bodies.step')).val()
    solids=sorted(shape.Solids(),key=lambda solid:solid.BoundingBox().xmin)
    assert shape.isValid() and len(solids)==2
    assert solids[0].Volume()==pytest.approx(22*16*10,abs=1e-5)
    assert solids[1].Volume()==pytest.approx(800-10*(1-math.pi/4),abs=1e-5)
    assert shape.Volume()==pytest.approx(result['finalVolumeMm3'],abs=1e-5)
    assert solids[0].intersect(solids[1]).Volume()==pytest.approx(280,abs=1e-5)
    assert (output/'editor-compound-bodies.glb').read_bytes()[:4]==b'glTF'


def test_expanded_engineering_runs_core_then_complete_native_checks_in_the_same_private_root(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(DEPLOY))
    import cad_complete_editor_smoke as complete
    events=[];output=tmp_path/'evidence';output.mkdir()
    def core(root,owner,destination):
        assert (root,owner,destination)==(tmp_path,'synthetic-owner',output)
        events.append('core');return {'baselineProof':True}
    def editor(root,owner,destination):
        assert (root,owner,destination)==(tmp_path,'synthetic-owner',output)
        events.append('editor');return {'coreEditorProof':True}
    def expanded(root,owner,destination):
        assert (root,owner,destination)==(tmp_path,'synthetic-owner',output)
        events.append('complete');return {'format':complete.FORMAT}
    monkeypatch.setattr(smoke,'original_engineering',core)
    monkeypatch.setattr(smoke,'editor_geometry',editor)
    monkeypatch.setattr(complete,'complete_editor_smoke',expanded)
    assert smoke.extended_engineering(tmp_path,'synthetic-owner',output)=={
        'baselineProof':True,'cadEditor':{'coreEditorProof':True},'completeEditor':{'format':complete.FORMAT}}
    assert events==['core','editor','complete']
