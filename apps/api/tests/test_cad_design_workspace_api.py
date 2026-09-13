import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cad_design_workspace_api import create_cad_design_workspace_router
from app.platform import AuthService
from app.platform_api import build_platform_services

cq = pytest.importorskip("cadquery")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthService,"_PBKDF2_ITERATIONS",1000)
    services=build_platform_services(":memory:",auth_secret="engineering-test-secret-very-long")
    users=[services.auth.create_user(f"eng-{i}@example.test","strong-password-123",f"User {i}",roles=("designer",)) for i in range(2)]
    headers=[{"Authorization":"Bearer "+services.auth.issue_token(user).token} for user in users]
    app=FastAPI();router=create_cad_design_workspace_router(services,tmp_path/'store');app.include_router(router)
    app.state.platform_services=services;app.state.engineering_router=router
    source=tmp_path/'source.step';cq.exporters.export(cq.Workplane().box(10,15,20),str(source))
    with TestClient(app) as client: yield client, headers, source.read_bytes(), router.design_store
    services.close()


def test_owned_step_http_flow_and_file_ids(api):
    client,headers,payload,store=api
    base='/api/cad/designs'
    assert client.get(base).status_code in (401,403)
    response=client.post(base+'/step',headers=headers[0],files={'file':('part.step',payload)},data={'fileId':'workspace-123'})
    assert response.status_code==201,response.text
    record=response.json();path=base+'/'+record['id']
    assert record['fileId']=='workspace-123'
    assert record['metrics']['size']==pytest.approx([10,15,20])
    assert client.get(base+'?fileId=workspace-123',headers=headers[0]).json()['items'][0]['id']==record['id']
    assert client.get(base+'?fileId=another-file',headers=headers[0]).json()['items']==[]
    assert client.get(base,headers=headers[1]).json()['items']==[]
    for suffix in ('','/artifacts/step','/artifacts/glb'):
        assert client.get(path+suffix,headers=headers[1]).status_code==404
    assert client.post(path+'/measure',headers=headers[1],json={'a':{'kind':'face','index':0}}).status_code==404
    assert client.post(path+'/drawing',headers=headers[1],json={}).status_code==404
    assert client.post(base+'/assemblies',headers=headers[1],json={'fileId':'x','instances':[{'id':'p','designId':record['id']}]}).status_code==404
    download=client.get(path+'/artifacts/step',headers=headers[0])
    assert download.status_code==200 and b'ISO-10303-21' in download.content[:200]
    assert 'no-store' in download.headers['cache-control']
    assert client.get(path+'/artifacts/design.json',headers=headers[0]).status_code==404
    measurement=client.post(path+'/measure',headers=headers[0],json={'a':{'kind':'edge','index':0}})
    assert measurement.status_code==200 and measurement.json()['a']['length']>0


def test_bad_payloads_are_validation_errors(api):
    client,headers,payload,store=api
    base='/api/cad/designs'
    response=client.post(base+'/step',headers=headers[0],files={'file':('evil.stp',b'bad')},data={'fileId':'file'})
    assert response.status_code==422
    assert client.get(base,headers=headers[0]).json()['items']==[]
    assert client.post(base+'/assemblies',headers=headers[0],json=[]).status_code==422
    assert client.post(base+'/assemblies',headers=headers[0],content='{"instances": NaN}').status_code==422
    assert client.post(base+'/assemblies',headers=headers[0],content='x'*256001).status_code==413


def test_viewer_cannot_write_and_assembly_job_owner_isolation(api):
    import time
    client,headers,payload,store=api
    services=client.app.state.platform_services
    viewer=services.auth.create_user('engineering-viewer@example.test','strong-password-123','Viewer',roles=('viewer',))
    read_only={'Authorization':'Bearer '+services.auth.issue_token(viewer).token}
    base='/api/cad/designs'
    assert client.get(base,headers=read_only).status_code==200
    assert client.post(base+'/step',headers=read_only,files={'file':('part.step',payload)},data={'fileId':'f'}).status_code==403
    for route in ('/assemblies','/ai-assemblies','/assembly-plans','/design_'+'0'*32+'/drawing'):
        assert client.post(base+route,headers=read_only,json={}).status_code==403
    response=client.post(base+'/assembly-plans',headers=headers[0],json={'fileId':'f','message':'missing sizes','plan':{'questions':['请提供尺寸']}})
    assert response.status_code==202,response.text
    job=response.json()
    assert client.get(base+'/assembly-jobs/'+job['id'],headers=headers[1]).status_code==404
    deadline=time.monotonic()+10
    while job['status']=='queued' and time.monotonic()<deadline:
        time.sleep(.03);job=client.get(base+'/assembly-jobs/'+job['id'],headers=headers[0]).json()
    assert job['status']=='needs_input'
    assert client.get(base+'/assembly-jobs?fileId=f',headers=headers[0]).json()['items'][0]['id']==job['id']
    assert client.get(base+'/assembly-jobs',headers=headers[1]).json()['items']==[]
    assert client.post(base+'/ai-assemblies',headers=headers[0],json={'fileId':'f','message':'example'}).status_code==409
