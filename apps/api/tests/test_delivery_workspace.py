from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_agent_store import CadRunStore
from app.cad_design_workspace import CadDesignWorkspace
from app.cad_feature_workspace import CadFeatureWorkspace
from app.delivery_workspace import DeliveryWorkspace, DeliveryError
from app.delivery_workspace_api import create_delivery_workspace_router
from app.native_drawing import NativeDrawingStore, _write
from app.platform import AuthService

cq = pytest.importorskip('cadquery')
ez = pytest.importorskip('ezdxf')


def request(*refs, key=None):
    return {'requestId': key or uuid4().hex, 'title': '首批客户交付', 'notes': '请检查尺寸后使用', 'sourceRefs': list(refs)}


def stores(root):
    runs = CadRunStore(root/'runs')
    engineering = CadDesignWorkspace(root/'engineering')
    features = CadFeatureWorkspace(root/'features', source_store=runs, design_store=engineering)
    native = NativeDrawingStore(root/'native.sqlite3')
    return DeliveryWorkspace(root/'deliveries', engineering_store=engineering, feature_store=features, native_store=native, cad_store=runs)


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp('delivery-real-cad')
    store = stores(root)
    plan = {'version':'cad-plan-v1', 'name':'实体盒', 'units':'mm', 'parameters':{},
            'features':[{'id':'body','op':'box','size':[10,20,5]}], 'result':'body'}
    created = store.features.save('alice', {'name':'实体盒', 'fileId':'project-file-1', 'plan':plan,
        'suppressed':[], 'changeNote':'已设置 10 × 20 × 5 mm'})
    feature = store.features.build('alice', created['id'], 1)
    part = feature['sharedDesign']
    assembly = store.engineering.assemble('alice', {'name':'双盒装配','fileId':'project-file-1',
        'instances':[{'id':'left','designId':part['id'],'position':[0,0,0]},
                     {'id':'right','designId':part['id'],'position':[30,0,0]}]})
    assembly = store.engineering.drawing('alice', assembly['id'], {'views':[{'view':'top','x':40,'y':40,'scale':1}], 'dimensions':[]})
    run_id = 'cad_' + 'a'*32
    directory = store.cad.directory(run_id); directory.mkdir()
    artifacts = {}
    for fmt in ('step', 'glb'):
        original, mime = store.features.artifact('alice', feature['id'], 2, fmt)
        path = directory/f'model.{fmt}'; shutil.copyfile(original, path)
        artifacts[fmt] = {'path':str(path), 'mimeType':mime}
    run = {'runId':run_id,'owner':'alice','revision':1,'status':'ready','plan':feature['plan'],
           'artifacts':artifacts, 'inspection':feature['inspection'], 'drawingReview':{'status':'not_applicable'},
           'observations':[{'id':'one','kind':'solid_count','expected':1,'source':{'type':'user','text':'一个实体'}}],
           'confirmedAt':'2026-09-12T00:00:00Z','downloadToken':'do-not-leak-this-token', 'files':[]}
    store.cad.save(run)
    return store, feature, assembly, run


def make_native(store, owner='alice'):
    doc = ez.new('R2010'); doc.units=4
    line = doc.modelspace().add_line((0,0),(20,0))
    doc.modelspace().add_circle((5,5),2)
    drawing = store.native.create(owner, '中文零件', _write(doc), '中文.dxf')
    return drawing, line.dxf.handle


def test_native_real_dxf_old_revision_snapshot_replay_and_restart(tmp_path):
    store = stores(tmp_path); drawing, line_id = make_native(store)
    args = request({'kind':'native','id':drawing['id'],'revision':1})
    result = store.create('alice', args)
    assert result['verification']['status'] == 'requires_review'
    assert len(result['files']) == 2
    assert all(not source['provenance']['manufacturingReleased'] for source in result['sources'])
    file = next(item for item in result['files'] if item['name'].endswith('.dxf'))
    path, _ = store.file('alice', result['id'], file['id'])
    assert list(ez.readfile(path).modelspace().query('LINE'))[0].dxf.end.x == 20
    store.native.operate('alice',drawing['id'],1,[{'action':'update','id':line_id,'changes':{'end':[40,0]}}])
    assert store.create('alice', args) == result
    second = store.create('alice', request({'kind':'native','id':drawing['id'],'revision':1}))
    assert next(f['sha256'] for f in second['files'] if f['name'].endswith('.dxf')) == file['sha256']
    assert {f['name']:f['sha256'] for f in second['files']} == {f['name']:f['sha256'] for f in result['files']}
    assert list(ez.readfile(path).modelspace().query('LINE'))[0].dxf.end.x == 20
    restarted = stores(tmp_path)
    assert restarted.get('alice', result['id']) == result
    archive, meta = restarted.archive('alice', result['id'])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == meta['sha256']
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        manifest = zipped.read('manifest.json')
        assert hashlib.sha256(manifest).hexdigest() == result['manifest']['sha256']
        assert str(tmp_path).encode() not in manifest and b'owner' not in manifest
        assert all(not name.startswith('/') and '..' not in name.split('/') for name in zipped.namelist())
    with pytest.raises(DeliveryError) as exc:
        store.create('alice', {**args,'title':'不同内容'})
    assert exc.value.status == 409


def test_real_feature_assembly_parts_drawings_and_reverse_provenance(built):
    store, feature, assembly, _ = built
    result = store.create('alice', request({'kind':'engineering','id':assembly['id']}))
    refs = {(item['kind'], item['id'], item['revision']) for item in result['sources']}
    assert ('engineering', feature['sharedDesign']['id'], None) in refs
    assert ('feature', feature['id'], 2) in refs
    assert len(refs) == 3
    part = next(item for item in result['sources'] if item['id']==feature['sharedDesign']['id'])
    assert part['provenance']['sourceFeature'] == {'id':feature['id'],'revision':2}
    names = [item['name'] for item in result['files']]
    assert 'bom.csv' in names and any(name.endswith('.pdf') for name in names) and any(name.endswith('.dxf') for name in names)
    source = next(item for item in result['sources'] if item['id']==assembly['id'])
    step = next(item for item in result['files'] if item['id'] in source['fileIds'] and item['name'].endswith('.step'))
    path, _ = store.file('alice', result['id'], step['id'])
    assert cq.importers.importStep(str(path)).val().Volume() == pytest.approx(2000)
    assert result['verification']['status']=='requires_review'
    assert str(store.root.parent) not in json.dumps(result)
    # A later sheet must not change the previously frozen package.
    original_archive = store.archive('alice',result['id'])[1]['sha256']
    store.engineering.drawing('alice',assembly['id'],{'views':[{'view':'front','x':60,'y':40,'scale':1}]})
    assert store.get('alice',result['id']) == result
    assert store.archive('alice',result['id'])[1]['sha256'] == original_archive
    store.features.save('alice',{'name':'尚未重建的新名称','expectedRevision':2,'plan':feature['plan'],
        'suppressed':[],'changeNote':'修改名称后待重建'},feature['id'])
    history=[item for item in store.sources('alice') if item['kind']=='feature' and item['id']==feature['id']]
    assert next(item for item in history if item['revision']==2)['name']==feature['name']
    assert not next(item for item in history if item['revision']==3)['eligible']


def test_ai_confirmed_export_policy_and_real_step_readback(built):
    store, _, _, run = built
    result = store.create('alice', request({'kind':'cad_run','id':run['runId'],'revision':1}))
    assert result['verification']['status'] == 'confirmed_source'
    assert 'do-not-leak-this-token' not in json.dumps(result)
    path, _ = store.archive('alice',result['id'])
    with zipfile.ZipFile(path) as archive:
        assert b'do-not-leak-this-token' not in archive.read('manifest.json')
    other = deepcopy(run); other.update(runId='cad_'+'b'*32,status='review_required')
    store.cad.save(other)
    with pytest.raises(DeliveryError) as exc:
        store.create('alice',request({'kind':'cad_run','id':other['runId']}))
    assert exc.value.status==409 and exc.value.code=='source_not_exportable'
    bad_review = deepcopy(run); bad_review.update(runId='cad_'+'c'*32,drawingReview={'status':'mismatch'})
    store.cad.save(bad_review)
    with pytest.raises(DeliveryError) as exc:
        store.create('alice',request({'kind':'cad_run','id':bad_review['runId']}))
    assert exc.value.code=='source_not_exportable'
    listed = {item['id']:item for item in store.sources('alice') if item['kind']=='cad_run'}
    assert listed[run['runId']]['eligible'] and not listed[other['runId']]['eligible'] and not listed[bad_review['runId']]['eligible']


def test_feature_from_candidate_references_source_without_exporting_candidate(built, tmp_path):
    # Build a real manual derivative; the original candidate retains its gate.
    store, feature, _, run = built
    candidate = deepcopy(run); candidate.update(runId='cad_'+'d'*32,status='review_required')
    store.cad.save(candidate)
    draft = store.features.save('alice', {'name':'手动修改','fileId':'manual-candidate','plan':feature['plan'],
        'suppressed':[],'changeNote':'按自己的尺寸重建','sourceRun':{'runId':candidate['runId'],'revision':1}})
    derived = store.features.build('alice',draft['id'],1)
    result = store.create('alice',request({'kind':'feature','id':derived['id'],'revision':2}))
    source = next(item for item in result['sources'] if item['kind']=='feature')
    assert source['provenance']['sourceRun']['artifactsIncluded'] is False
    assert not any(item['kind']=='cad_run' for item in result['sources'])
    assert store.cad.load(candidate['runId'])['status']=='review_required'
    assert result['verification']['status']=='requires_review'


def test_source_tamper_missing_and_cross_owner_paths_rejected(built):
    store, feature, _, _ = built
    path, _ = store.features.artifact('alice',feature['id'],2,'step')
    original = path.read_bytes()
    try:
        path.write_bytes(original+b'\nTAMPERED')
        with pytest.raises(DeliveryError) as exc:
            store.create('alice',request({'kind':'feature','id':feature['id'],'revision':2}))
        assert exc.value.code=='source_integrity'
        path.unlink()
        with pytest.raises(DeliveryError):
            store.create('alice',request({'kind':'feature','id':feature['id'],'revision':2}))
    finally:
        path.write_bytes(original)
    # A valid STEP of another size is rejected even for legacy AI records without hashes.
    run = deepcopy(store.cad.load('cad_'+'a'*32)); run['runId']='cad_'+'e'*32
    folder=store.cad.directory(run['runId']);folder.mkdir()
    wrong=folder/'model.step';cq.exporters.export(cq.Workplane().box(3,4,5),str(wrong))
    run['artifacts']['step']['path']=str(wrong)
    glb=folder/'model.glb';glb.write_bytes(Path(run['artifacts']['glb']['path']).read_bytes());run['artifacts']['glb']['path']=str(glb)
    store.cad.save(run)
    with pytest.raises(DeliveryError) as exc:
        store.create('alice',request({'kind':'cad_run','id':run['runId']}))
    assert exc.value.code=='source_integrity'


def test_identity_revision_size_and_account_isolation(tmp_path, monkeypatch):
    store=stores(tmp_path);drawing,_=make_native(store)
    ref={'kind':'native','id':drawing['id'],'revision':1}
    result=store.create('alice',request(ref))
    assert store.list('bob')==[] and store.sources('bob')==[]
    for operation in (lambda:store.get('bob',result['id']),lambda:store.archive('bob',result['id']),
                      lambda:store.file('bob',result['id'],result['files'][0]['id']),lambda:store.create('bob',request(ref))):
        with pytest.raises(DeliveryError) as exc:operation()
        assert exc.value.status==404
    for bad in ({**ref,'id':'../../accounts.sqlite3'},{**ref,'kind':[]},{**ref,'revision':True},{**ref,'revision':0},{**ref,'owner':'alice'}, {'kind':'engineering','id':'x','revision':1}):
        with pytest.raises(DeliveryError):store.create('alice',request(bad))
    with pytest.raises(DeliveryError):store.create('alice',request(ref,ref))
    with pytest.raises(DeliveryError):store.create('alice',request(*[{'kind':'native','id':str(i)} for i in range(21)]))
    with pytest.raises(DeliveryError):store.create('alice',request({**ref,'revision':999}))
    monkeypatch.setattr('app.delivery_workspace.MAX_TOTAL_BYTES',100)
    with pytest.raises(DeliveryError) as exc:store.create('alice',request(ref))
    assert exc.value.code=='delivery_size'
    assert not list((store._owner('alice')).glob('.building-*'))


def test_package_tamper_and_concurrent_admission(tmp_path):
    store=stores(tmp_path);drawing,_=make_native(store)
    ref={'kind':'native','id':drawing['id'],'revision':1}
    with store._lock('alice'):
        with pytest.raises(DeliveryError) as exc:store.create('alice',request(ref))
        assert exc.value.code=='delivery_busy'
    result=store.create('alice',request(ref))
    file=result['files'][0];path,_=store.file('alice',result['id'],file['id'])
    original=path.read_bytes();path.write_bytes(original+b'tampered')
    with pytest.raises(DeliveryError):store.file('alice',result['id'],file['id'])
    path.write_bytes(original)
    archive,_=store.archive('alice',result['id']);archive.write_bytes(b'bad')
    with pytest.raises(DeliveryError) as exc:store.archive('alice',result['id'])
    assert exc.value.code=='delivery_integrity'


def test_long_native_name_keeps_extension_and_manifest_tamper_is_rejected(tmp_path):
    store=stores(tmp_path)
    drawing=store.native.create('alice','零'*180)
    result=store.create('alice',request({'kind':'native','id':drawing['id'],'revision':1}))
    assert any(item['name'].endswith('.dxf') and len(item['name'])==180 for item in result['files'])
    folder=store._owner('alice')/result['id']
    manifest=folder/'manifest.json'
    manifest.write_bytes(manifest.read_bytes()+b' ')
    with pytest.raises(DeliveryError) as exc:store.get('alice',result['id'])
    assert exc.value.code=='source_integrity'


def test_delivery_directory_symlink_cannot_cross_owner_boundary(tmp_path):
    store=stores(tmp_path); drawing,_=make_native(store)
    result=store.create('alice',request({'kind':'native','id':drawing['id'],'revision':1}))
    folder=store._owner('alice')/result['id'];other=tmp_path/'elsewhere'
    folder.rename(other);folder.symlink_to(other,target_is_directory=True)
    with pytest.raises(DeliveryError) as exc:store.get('alice',result['id'])
    assert exc.value.code=='source_integrity'


def test_http_auth_owner_permissions_idempotency_and_real_files(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthService,'_PBKDF2_ITERATIONS',1000)
    auth=AuthService(tmp_path/'accounts.sqlite3',token_secret='delivery-tests-'*5)
    users=[auth.create_user(f'{name}@example.test','safe-test-password',name,roles=[role]) for name,role in [('alice','designer'),('bob','designer'),('viewer','viewer')]]
    headers=[{'Authorization':'Bearer '+auth.issue_token(user).token} for user in users]
    store=stores(tmp_path)
    drawing,_=make_native(store,users[0].id)
    router=create_delivery_workspace_router(SimpleNamespace(auth=auth),cad_store=store.cad,root=store.root,
        engineering_store=store.engineering,feature_store=store.features,native_store=store.native)
    app=FastAPI();app.include_router(router)
    args=request({'kind':'native','id':drawing['id'],'revision':1})
    try:
        with TestClient(app) as client:
            base='/api/cad/deliveries'
            assert client.get(base).status_code==401
            assert client.get(base+'/sources').status_code==401
            assert client.post(base,headers=headers[2],json=args).status_code==403
            assert client.get(base+'/sources',headers=headers[0]).json()['items'][0]['eligible']
            created=client.post(base,headers=headers[0],json=args)
            assert created.status_code==201,created.text
            record=created.json();url=base+'/'+record['id']
            assert client.post(base,headers=headers[0],json=args).json()==record
            assert client.get(url,headers=headers[1]).status_code==404
            assert client.get(url+'/archive',headers=headers[1]).status_code==404
            assert client.get(url+'/files/'+record['files'][0]['id'],headers=headers[1]).status_code==404
            assert client.get(url+'/files/'+record['files'][0]['id'],headers=headers[0]).content
            archive=client.get(url+'/archive',headers=headers[0])
            assert archive.status_code==200 and archive.headers['content-type']=='application/zip'
            assert 'no-store' in archive.headers['cache-control']
            assert zipfile.ZipFile(io.BytesIO(archive.content)).testzip() is None
            assert client.post(base,headers=headers[0],json={**args,'title':'changed'}).status_code==409
            assert client.post(base,headers=headers[0],content='x'*64001).status_code==413
            assert client.post(base,headers=headers[0],content='{"notes":NaN}').status_code==422
            assert client.get('/openapi.json').status_code==200
            auth.set_active(users[0].id,False,actor_id='test')
            assert client.get(url,headers=headers[0]).status_code==401
    finally:auth.close()
