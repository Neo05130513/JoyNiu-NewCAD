from types import SimpleNamespace

import pytest

pytest.importorskip('ezdxf')
pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.native_drawing import _write
from app.native_drawing_api import create_native_drawing_router
from app.platform import AuthService
import ezdxf


@pytest.fixture
def api(tmp_path):
    auth = AuthService(tmp_path / 'accounts.sqlite', token_secret='native-test-' * 5)
    users = [auth.create_user(f'{name}@example.com', 'a-long-native-password', name, roles=['designer']) for name in ('alice', 'bob')]
    headers = [{'Authorization': f'Bearer {auth.issue_token(user).token}'} for user in users]
    app = FastAPI()
    app.include_router(create_native_drawing_router(SimpleNamespace(auth=auth)))
    try:
        with TestClient(app) as client:
            yield client, headers, auth, users
    finally:
        auth.close()


def test_routes_require_auth_and_isolate_drawings(api):
    client, headers, auth, users = api
    base = '/api/cad/drawings'
    assert client.get(base).status_code == 401
    assert client.get(base + '/capabilities').status_code == 401
    created = client.post(base, headers=headers[0], json={'name': '客户图纸', 'owner': users[1].id})
    assert created.status_code == 201, created.text
    path = base + '/' + created.json()['id']
    assert client.get(path, headers=headers[1]).status_code == 404
    assert client.get(path + '/export', headers=headers[1]).status_code == 404
    assert client.post(path + '/undo', headers=headers[1], json={'expectedRevision': 1}).status_code == 404
    assert client.get(base, headers=headers[1]).json()['items'] == []
    auth.set_active(users[0].id, False, actor_id='test')
    assert client.get(path, headers=headers[0]).status_code == 401


def test_import_edit_export_conflict_and_history(api):
    client, headers, _auth, _users = api
    doc = ezdxf.new('R2010')
    line = doc.modelspace().add_line((0, 0), (20, 0))
    result = client.post('/api/cad/drawings/import', headers=headers[0], files={'file': ('客户.dxf', _write(doc), 'application/dxf')})
    assert result.status_code == 201, result.text
    path = '/api/cad/drawings/' + result.json()['id']
    request = {'expectedRevision': 1, 'operations': [{'action': 'update', 'id': line.dxf.handle, 'changes': {'end': [40, 0]}}]}
    changed = client.post(path + '/operations', headers=headers[0], json=request)
    assert changed.status_code == 200, changed.text
    conflict = client.post(path + '/operations', headers=headers[0], json=request)
    assert conflict.status_code == 409
    assert conflict.json()['detail']['revision'] == 2
    assert client.post(path + '/undo', headers=headers[0], json={'expectedRevision': 2}).json()['entities'][0]['end'] == [20, 0, 0]
    assert client.post(path + '/redo', headers=headers[0], json={'expectedRevision': 3}).json()['entities'][0]['end'] == [40, 0, 0]
    download = client.get(path + '/export?format=dxf', headers=headers[0])
    assert download.status_code == 200
    assert 'filename*=UTF-8' in download.headers['content-disposition']
    assert download.headers['cache-control'] == 'no-store'
    assert client.get(path + '/export?format=json', headers=headers[0]).json()['revision'] == 4
    assert client.get(path + '/preview.svg', headers=headers[0]).status_code == 200


def test_json_and_upload_validation(api):
    client, headers, _auth, _users = api
    base = '/api/cad/drawings'
    assert client.post(base, headers=headers[0], content='{}').status_code == 415
    assert client.post(base, headers={**headers[0], 'Content-Type': 'application/json'}, content='NaN').status_code == 422
    assert client.post(base, headers=headers[0], json={'name': 'x' * 201}).status_code == 422
    assert client.post(base + '/import', headers=headers[0], files={'file': ('../../etc/passwd', b'not-a-drawing')}).status_code == 422
    assert client.post(base + '/import', headers=headers[0], files={'file': ('broken.dxf', b'not-a-drawing')}).status_code == 422
    assert client.post(base + '/import', headers=headers[0], files={'file': ('large.dxf', b'x' * (20 * 1024 * 1024 + 1))}).status_code == 413


def test_viewer_can_read_owned_drawing_but_all_write_routes_require_document_write(api):
    client, headers, auth, users = api
    base = '/api/cad/drawings'
    result = client.post(base, headers=headers[0], json={'name': '权限检查'}).json()
    identity = result['id']
    auth.assign_roles(users[0].id, ['viewer'], actor_id='test')
    viewer = {'Authorization': f'Bearer {auth.issue_token(auth.get_user(users[0].id)).token}'}
    assert client.get(base + '/capabilities', headers=viewer).json()['canWrite'] is False
    assert client.get(base, headers=viewer).status_code == 200
    assert client.get(base + '/' + identity, headers=viewer).status_code == 200
    assert client.get(base + '/' + identity + '/export?format=dxf', headers=viewer).status_code == 200
    assert client.post(base, headers=viewer, json={'name': '拒绝'}).status_code == 403
    assert client.post(base + '/import', headers=viewer, files={'file': ('test.dxf', b'bad')}).status_code == 403
    for action in ['operations', 'undo', 'redo']:
        response = client.post(base + '/' + identity + '/' + action, headers=viewer, json={'expectedRevision': 1, 'operations': []})
        assert response.status_code == 403, response.text
    assert client.get(base + '/' + identity, headers=viewer).json()['revision'] == 1


def test_http_retries_reuse_request_id_for_create_import_edit_and_history(api):
    client, headers, _auth, _users=api
    base='/api/cad/drawings'
    args={'headers':headers[0], 'json':{'name':'断网重试','requestId':'new-once'}}
    first=client.post(base,**args)
    assert first.status_code==201
    assert client.post(base,**args).json()['id']==first.json()['id']
    path=base+'/'+first.json()['id']
    edit={'expectedRevision':1,'requestId':'edit-once','operations':[{'op':'add','entity':{'type':'LINE','start':[0,0],'end':[8,0]}}]}
    assert client.post(path+'/operations',headers=headers[0],json=edit).json()['revision']==2
    retried=client.post(path+'/operations',headers=headers[0],json=edit)
    assert retried.status_code==200 and len(retried.json()['entities'])==1
    undo={'expectedRevision':2,'requestId':'undo-once'}
    assert client.post(path+'/undo',headers=headers[0],json=undo).json()['revision']==3
    assert client.post(path+'/undo',headers=headers[0],json=undo).json()['revision']==3
    redo={'expectedRevision':3,'requestId':'redo-once'}
    assert client.post(path+'/redo',headers=headers[0],json=redo).json()['revision']==4
    assert client.post(path+'/redo',headers=headers[0],json=redo).json()['revision']==4
    dxf=client.get(path+'/export',headers=headers[0]).content
    upload={'headers':headers[0],'files':{'file':('saved.dxf',dxf)},'data':{'requestId':'import-once'}}
    imported=client.post(base+'/import',**upload)
    assert imported.status_code==201, imported.text
    assert client.post(base+'/import',**upload).json()['id']==imported.json()['id']
    assert len(client.get(base,headers=headers[0]).json()['items'])==2
