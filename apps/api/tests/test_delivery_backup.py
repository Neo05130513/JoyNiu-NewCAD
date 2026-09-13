"""Offline restore drill for an actual CAD delivery with nested SQLite stores."""
import hashlib
import importlib.util
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
from app.delivery_workspace_api import create_delivery_workspace_router
from app.native_drawing import NativeDrawingStore, _write
from app.platform import AuthService

cq = pytest.importorskip('cadquery')
ez = pytest.importorskip('ezdxf')
SPEC = importlib.util.spec_from_file_location('delivery_backup_tool', Path(__file__).resolve().parents[3]/'deploy/commercial_backup.py')
backup_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_tool)


def test_real_delivery_offline_backup_restore_reopens_zip_without_original_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthService,'_PBKDF2_ITERATIONS',1000)
    deployment=tmp_path/'deployment'; data=deployment/'data';data.mkdir(parents=True)
    secret='test-only-backup-secret-'*3
    auth=AuthService(data/'accounts.sqlite3',token_secret=secret)
    user=auth.create_user('customer@example.test','safe-test-password','验收账号',roles=['designer'])
    services=SimpleNamespace(auth=auth)
    runs=CadRunStore(data/'cad-agent')
    engineering=CadDesignWorkspace(data/'cad-designs')
    features=CadFeatureWorkspace(data/'cad-feature-workspace',source_store=runs,design_store=engineering)
    native=NativeDrawingStore(auth.database)
    plan={'version':'cad-plan-v1','name':'恢复演练盒体','units':'mm','parameters':{},
          'features':[{'id':'body','op':'box','size':[10,5,3]}],'result':'body'}
    draft=features.save(user.id,{'name':'恢复演练盒体','plan':plan,'suppressed':[],'fileId':'project-restore-test','changeNote':'设定真实尺寸'})
    built=features.build(user.id,draft['id'],1)
    doc=ez.new('R2010');doc.modelspace().add_circle((5,5),2)
    drawing=native.create(user.id,'恢复演练二维图',_write(doc),'drawing.dxf')
    router=create_delivery_workspace_router(services,cad_store=runs,engineering_store=engineering,feature_store=features,native_store=native)
    store=router.delivery_store
    record=store.create(user.id,{'requestId':uuid4().hex,'title':'可恢复的真实交付','notes':'备份演练',
        'sourceRefs':[{'kind':'feature','id':built['id'],'revision':2},{'kind':'native','id':drawing['id'],'revision':1}]})
    original_zip=store.archive(user.id,record['id'])[0].read_bytes()
    auth.close()  # All writers in this isolated deployment are stopped.
    destination,restored=tmp_path/'private-backup',tmp_path/'restored'
    backup_tool.backup(deployment,destination,quiescent=True)
    manifest=backup_tool.verify(destination)
    assert manifest['databases']['data/cad-feature-workspace/features.sqlite3']['manual_features']==2
    assert manifest['databases']['data/cad-deliveries/deliveries.sqlite3']['deliveries']==1
    assert manifest['databases']['data/accounts.sqlite3']['native_drawing_documents']==1
    assert 'data/cad-agent/runs.sqlite3' in manifest['databases']
    assert any(name.startswith('data/cad-designs/') and name.endswith('model.step') for name in manifest['files'])
    assert any(name.startswith('data/cad-feature-workspace/') and name.endswith('model.step') for name in manifest['files'])
    assert any(name.startswith('data/cad-deliveries/') and name.endswith('archive.zip') for name in manifest['files'])
    backup_tool.restore(destination,restored)
    shutil.rmtree(deployment)  # Ensure downloads cannot accidentally read old absolute source paths.
    restored_auth=AuthService(restored/'data/accounts.sqlite3',token_secret=secret)
    restored_runs=CadRunStore(restored/'data/cad-agent')
    reopened=create_delivery_workspace_router(SimpleNamespace(auth=restored_auth),cad_store=restored_runs)
    assert reopened.delivery_store.get(user.id,record['id'])==record
    assert len(reopened.delivery_store.features.versions(user.id,built['id']))==2
    assert reopened.delivery_store.engineering.get(user.id,built['sharedDesign']['id'])['metrics']['volume']==pytest.approx(150)
    app=FastAPI();app.include_router(reopened)
    header={'Authorization':'Bearer '+restored_auth.issue_token(user).token}
    try:
        with TestClient(app) as client:
            base='/api/cad/deliveries/'+record['id']
            assert client.get(base,headers=header).json()==record
            download=client.get(base+'/archive',headers=header)
            assert download.status_code==200 and download.content==original_zip
            assert hashlib.sha256(download.content).hexdigest()==record['archive']['sha256']
            with zipfile.ZipFile(io.BytesIO(download.content)) as zipped:
                assert zipped.testzip() is None
                assert str(deployment).encode() not in zipped.read('manifest.json')
            step=next(item for item in record['files'] if item['name'].endswith('.step'))
            response=client.get(base+'/files/'+step['id'],headers=header)
            assert response.status_code==200 and hashlib.sha256(response.content).hexdigest()==step['sha256']
            reread=tmp_path/'restored-delivery.step';reread.write_bytes(response.content)
            assert cq.importers.importStep(str(reread)).val().Volume()==pytest.approx(150)
    finally:restored_auth.close()
