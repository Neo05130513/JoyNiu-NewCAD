#!/usr/bin/env python3
"""Candidate-only CAD editor smoke. Never run against a live/customer API.

Run alongside currentcad_release_smoke.py inside the pinned candidate runtime.
The existing harness creates isolated data, synthetic credentials and optionally
checks a copied read-only backup. This extension requires the new editor paths,
exact picking, atomic saves and parameter history in addition to its DWG checks.
All output is synthetic geometry or fixed diagnostics; passwords are not logged.

Mount the reviewed deploy directory (both smokes, activation wrappers and their
commercial operations modules are required siblings for the real gate probe):
 docker run --rm --network none --read-only --user 10001:10001 \
   --tmpfs /tmp:rw,exec,nosuid,size=2g,uid=10001,gid=10001 \
   -v "$PWD/deploy:/release:ro" -v "/private/new-evidence:/evidence" \
   --entrypoint python CANDIDATE /release/cad_editor_release_smoke.py \
   --output-dir /evidence
Use an independently created evidence directory writable by UID 10001. Add
only a read-only verified backup mount and --restore-data /restore/data when
testing restoration; do not mount live data, Docker sockets, tokens or env files.
For a development-only host check, run from the repository root:
 PYTHONPATH=apps/api:deploy apps/api/.venv/bin/python \
   deploy/cad_editor_release_smoke.py --allow-host-runtime \
   --output-dir /tmp/new-editor-smoke-evidence
"""
from __future__ import annotations
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request
from uuid import uuid4

try:
    import currentcad_release_smoke as baseline
except ModuleNotFoundError as exc:
    if exc.name != 'currentcad_release_smoke':
        raise
    raise SystemExit('Mount cad_editor_release_smoke.py and currentcad_release_smoke.py in the same directory.') from None

require, close = baseline.require, baseline.close
original_engineering = baseline.engineering_smoke
original_http = baseline.http_smoke


def box_plan():
    return {'version':'cad-plan-v1','units':'mm','name':'编辑器候选验收',
            'parameters':{'width':{'value':20,'source':{'type':'user','text':'合成验收'}}},
            'features':[{'id':'body','op':'box','size':['width',16,10],'origin':[0,0,0]}], 'result':'body'}


def editor_geometry(root, owner, output):
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
    from app.cad_plan import PlanValidationError
    store = CadFeatureWorkspace(root/'cad-feature-workspace')
    plan = box_plan()
    preview = store.preview(owner, {'plan':plan,'suppressed':[]})
    require(preview['valid'] and len(preview['faces'])==6 and len(preview['edges'])==12,'Editor topology missing')
    require(len(preview['mesh']['faceIds'])==len(preview['mesh']['indices'])//3,'Triangle face membership missing')
    pick=preview['edges'][0]['selector']
    plan['features'].append({'id':'round','op':'fillet','input':'body','radius':1,'edges':[pick]});plan['result']='round'
    payload={'name':'编辑器候选验收','fileId':'editor-smoke-'+uuid4().hex,'plan':plan,'suppressed':[],
             'changeNote':'合成几何选边验收','requestId':str(uuid4())}
    built=store.commit(owner,payload)
    require(built['status']=='built' and built['revision']==1,'Editor first atomic commit failed')
    require(built['inspection']['stepReadback']['valid'],'Editor STEP readback failed')
    repeated=store.commit(owner,payload)
    require(repeated['id']==built['id'] and repeated['revision']==1,'Editor retry created a duplicate revision')
    failed={**copy.deepcopy(payload),'designId':built['id'],'expectedRevision':1,'requestId':str(uuid4())}
    failed['plan']=copy.deepcopy(built['plan']);failed['plan']['features'][-1]['radius']=1000
    try:store.commit(owner,failed)
    except (FeatureWorkspaceError,PlanValidationError):pass
    else:require(False,'Invalid fillet was published')
    require(store.get(owner,built['id'])==built,'Failed operation changed saved head')
    changed={**copy.deepcopy(payload),'designId':built['id'],'expectedRevision':1,'requestId':str(uuid4())}
    changed['plan']=copy.deepcopy(built['plan']);changed['plan']['parameters']['width']['value']=22
    rebuilt=store.commit(owner,changed)
    require(rebuilt['revision']==2 and rebuilt['inspection']['stepReadback']['valid'],'Parametric history did not rebuild')
    close(rebuilt['inspection']['bbox']['size'][0],22)
    require(store.get(owner,built['id'],1)==built,'Parameter edit altered the prior saved version')
    require(rebuilt['plan']['features'][-1]['edges'][0].get('binding',{}).get('version')==1,
            'Saved selected edge has no persistent history binding')
    for fmt in ('step','glb'):
        path,_=store.artifact(owner,built['id'],rebuilt['revision'],fmt)
        (output/f'editor-fixture.{fmt}').write_bytes(path.read_bytes())
    (output/'editor-fixture-plan.json').write_text(json.dumps(rebuilt['plan'],ensure_ascii=False,indent=2)+'\n')
    profile_history=editor_profile_history(root,owner,output)
    compound_bodies=editor_compound_bodies(root,owner,output)
    return {'topologyFaces':6,'topologyEdges':12,'triangleMembership':True,'selectedEdgeFillet':True,
            'atomicFailurePreservedHead':True,'idempotentRetry':True,'parameterHistoryRebuilt':True,
            'stepReadback':True,'volumeMm3':rebuilt['inspection']['volumeMm3'],
            'profileHistoryRebuilt':True,'profileHistory':profile_history,
            'compoundBodiesRebuilt':True,'compoundBodies':compound_bodies}


def editor_profile_history(root, owner, output):
    """Follow real owner-bound picks through a box-to-sketch feature edit.

    The three initial commits deliberately use one design. Preview responses
    provide fresh public selectors; only saved server results supply bindings.
    """
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
    from app.cad_plan import PlanValidationError
    store=CadFeatureWorkspace(root/'cad-feature-workspace')
    file_id='editor-profile-'+uuid4().hex
    def payload(plan, previous=None):
        return {'name':'底板草图历史验收','fileId':file_id,'plan':copy.deepcopy(plan),'suppressed':[],
                'changeNote':'合成顶面凸台及圆角历史验收','requestId':str(uuid4()),
                **({'designId':previous['id'],'expectedRevision':previous['revision']} if previous else {})}
    def preview(record, plan=None, feature_id=None):
        data=payload(plan if plan is not None else record['plan'],record)
        if feature_id:data['featureId']=feature_id
        return store.preview(owner,data)
    original=store.commit(owner,payload(box_plan()))
    first=preview(original)
    faces=[face for face in first['faces'] if face.get('planar') and face.get('normal',[0,0,0])[2]>.999 and abs(face['origin'][2]-10)<1e-6]
    require(len(faces)==1,'Profile history top face is not unique')
    face=faces[0]
    plan=copy.deepcopy(original['plan'])
    plan['features'] += [
        {'id':'boss','op':'profile_extrude','plane':'custom','planeSource':copy.deepcopy(face['selector']),
         'frame':{'origin':face['origin'],'normal':face['normal'],'xDir':face['xAxis']},'start':[-5,-4],
         'segments':[{'type':'line','to':[5,-4]},{'type':'line','to':[5,4]},
                     {'type':'line','to':[-5,4]},{'type':'line','to':[-5,-4]}],'distance':5},
        {'id':'joined','op':'union','inputs':['body','boss']}]
    plan['result']='joined'
    joined=store.commit(owner,payload(plan,original))
    live=preview(joined)
    # Pick the two opposite 10-mm top edges, without hard-coded edge indices.
    picks=[]
    for edge in live['edges']:
        points=edge['points']
        if len(points)==6 and all(abs(z-15)<1e-6 for z in points[2::3]) and abs(math.dist(points[:3],points[3:])-10)<1e-6:
            picks.append(copy.deepcopy(edge['selector']))
    require(len(picks)==2,'Profile history boss top edges are not unique')
    plan=copy.deepcopy(joined['plan'])
    plan['features'].append({'id':'round','op':'fillet','input':'joined','radius':1,'edges':picks});plan['result']='round'
    rounded=store.commit(owner,payload(plan,joined))
    require(rounded['revision']==3 and rounded['inspection']['stepReadback']['valid'],'Profile history initial STEP is invalid')
    bound_boss=next(feature for feature in rounded['plan']['features'] if feature['id']=='boss')
    require(bound_boss['planeSource'].get('binding',{}).get('version')==1
            and all(edge.get('binding',{}).get('version')==1 for edge in rounded['plan']['features'][-1]['edges']),
            'Profile history did not receive saved server bindings')
    expected_before=20*16*10+10*8*5-20*(1-math.pi/4)
    close(rounded['inspection']['volumeMm3'],expected_before,1e-5)
    boss_before=preview(rounded,feature_id='boss')
    old_step,_=store.artifact(owner,rounded['id'],3,'step');old_bytes=old_step.read_bytes()
    changed=copy.deepcopy(rounded['plan'])
    changed['features'][0]={'id':'body','op':'profile_extrude','plane':'XY','origin':[0,0,0],
        'start':[0,0],'segments':[{'type':'line','to':['width',0]},{'type':'line','to':['width',16]},
        {'type':'line','to':[0,16]},{'type':'line','to':[0,0]}],'distance':10}
    changed['parameters']['width']['value']=22
    try:
        boss_after=preview(rounded,changed,'boss')
        require(store.get(owner,rounded['id'])==rounded,'Profile preview changed the saved head')
        converted=store.commit(owner,payload(changed,rounded))
    except (FeatureWorkspaceError,PlanValidationError):
        require(store.get(owner,rounded['id'])==rounded and old_step.read_bytes()==old_bytes,
                'Failed profile conversion altered the saved design')
        raise baseline.SmokeCheckFailed('Box-to-profile attached sketch and fillet history did not rebuild') from None
    require(converted['revision']==4 and converted['inspection']['stepReadback']['valid'],
            'Profile history conversion did not produce a valid saved STEP')
    for actual,expected in zip(converted['inspection']['bbox']['size'],(22,16,15)):close(actual,expected)
    close(converted['inspection']['volumeMm3'],expected_before+320,1e-5)
    delta=[(boss_after['bounds']['min'][axis]+boss_after['bounds']['max'][axis]
            -boss_before['bounds']['min'][axis]-boss_before['bounds']['max'][axis])/2 for axis in range(3)]
    for actual,expected in zip(delta,(1,0,0)):close(actual,expected)
    new_boss=next(feature for feature in converted['plan']['features'] if feature['id']=='boss')
    require(new_boss['planeAttachment']==bound_boss['planeAttachment'],'Profile conversion changed the saved local attachment')
    require(store.get(owner,rounded['id'],3)==rounded and old_step.read_bytes()==old_bytes,
            'Profile conversion changed an immutable previous revision')
    invalid=copy.deepcopy(converted['plan']);invalid['features'][-1]['radius']=1000
    try:store.commit(owner,payload(invalid,converted))
    except (FeatureWorkspaceError,PlanValidationError):pass
    else:require(False,'Invalid profile fillet was published')
    # A saved binding cannot be transplanted into a new design, even by owner.
    try:store.commit(owner,payload(converted['plan']))
    except (FeatureWorkspaceError,PlanValidationError):pass
    else:require(False,'Profile bindings were accepted without saved design authority')
    require(store.get(owner,converted['id'])==converted and len(store.versions(owner,converted['id']))==4,
            'Rejected profile operations changed saved history')
    for fmt in ('step','glb'):
        path,_=store.artifact(owner,converted['id'],4,fmt)
        (output/f'editor-profile-history.{fmt}').write_bytes(path.read_bytes())
    (output/'editor-profile-history-plan.json').write_text(json.dumps(converted['plan'],ensure_ascii=False,indent=2)+'\n')
    return {'sourceWidthMm':20,'finalWidthMm':22,'sizeMm':[22,16,15],'selectedBossEdges':2,
            'bossAttachmentMovementMm':delta,'volumeBeforeMm3':expected_before,'volumeAfterMm3':converted['inspection']['volumeMm3'],
            'savedRevisions':4,'stepReadback':True,'previousVersionPreserved':True,'failurePreservedHead':True,
            'foreignDesignBindingRejected':True}


def editor_compound_bodies(root, owner, output):
    """Keep intersecting bodies independent through a selected-edge edit."""
    from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
    from app.cad_plan import PlanValidationError
    from app.geometry import get_cadquery
    store=CadFeatureWorkspace(root/'cad-feature-workspace')
    file_id='editor-compound-'+uuid4().hex

    def payload(plan, previous=None):
        return {'name':'新建独立实体验收','fileId':file_id,'plan':copy.deepcopy(plan),'suppressed':[],
                'changeNote':'合成多实体、选边及参数修改验收','requestId':str(uuid4()),
                **({'designId':previous['id'],'expectedRevision':previous['revision']} if previous else {})}

    def readback(record, width, second_volume):
        require(record['inspection']['solidCount']==2 and record['inspection']['stepReadback']['valid']
                and record['inspection']['stepReadback']['solidCount']==2,'Compound STEP lost an independent body')
        path,_=store.artifact(owner,record['id'],record['revision'],'step')
        imported=get_cadquery().importers.importStep(str(path)).val()
        solids=imported.Solids()
        require(imported.isValid() and len(solids)==2 and all(solid.isValid() for solid in solids),
                'Compound STEP does not contain two valid solids')
        solids=sorted(solids,key=lambda solid:solid.BoundingBox().xmin)
        for actual,expected in zip((solids[0].BoundingBox().xlen,solids[0].BoundingBox().ylen,solids[0].BoundingBox().zlen),(width,16,10)):
            close(actual,expected)
        close(solids[0].Volume(),width*16*10,1e-5)
        close(solids[1].Volume(),second_volume,1e-5)
        close(imported.Volume(),width*16*10+second_volume,1e-5)
        # The bodies deliberately overlap: positive common volume plus separate
        # STEP solids proves this result is not a fuse/union masquerading as new.
        overlap=solids[0].intersect(solids[1]).Volume()
        close(overlap,(width-15)*8*5,1e-5)
        return path,overlap

    original=store.commit(owner,payload(box_plan()))
    plan=copy.deepcopy(original['plan'])
    plan['features'] += [
        {'id':'second','op':'profile_extrude','plane':'XY','origin':[15,4,5],'start':[0,0],
         'segments':[{'type':'line','to':[10,0]},{'type':'line','to':[10,8]},
                     {'type':'line','to':[0,8]},{'type':'line','to':[0,0]}],'distance':10},
        {'id':'bodies','op':'compound','inputs':['body','second']}]
    plan['result']='bodies'
    compound=store.commit(owner,payload(plan,original))
    original_step,overlap_before=readback(compound,20,800)
    original_bytes=original_step.read_bytes()
    live=store.preview(owner,payload(compound['plan'],compound))
    require(live['inspection']['solidCount']==2,'Compound preview lost an independent body')
    picks=[]
    for edge in live['edges']:
        points=edge['points']
        if (len(points)==6 and all(abs(z-15)<1e-6 for z in points[2::3])
                and all(abs(y-4)<1e-6 for y in points[1::3])
                and abs(math.dist(points[:3],points[3:])-10)<1e-6):
            picks.append(copy.deepcopy(edge['selector']))
    require(len(picks)==1,'Independent body edge cannot be picked uniquely')
    plan=copy.deepcopy(compound['plan'])
    plan['features'].append({'id':'rounded','op':'fillet','input':'bodies','radius':1,'edges':picks})
    plan['result']='rounded'
    rounded=store.commit(owner,payload(plan,compound))
    second_volume=800-10*(1-math.pi/4)
    readback(rounded,20,second_volume)
    require(rounded['plan']['features'][-1]['edges'][0].get('binding',{}).get('version')==1,
            'Independent body edit has no saved topology binding')
    changed=copy.deepcopy(rounded['plan']);changed['parameters']['width']['value']=22
    rebuilt=store.commit(owner,payload(changed,rounded))
    final_step,overlap_after=readback(rebuilt,22,second_volume)
    close(rebuilt['inspection']['volumeMm3']-rounded['inspection']['volumeMm3'],320,1e-5)
    require(rebuilt['revision']==4 and store.get(owner,compound['id'],2)==compound
            and original_step.read_bytes()==original_bytes,'Independent body edit changed its prior version')
    invalid=copy.deepcopy(rebuilt['plan']);invalid['features'][-1]['radius']=1000
    try:store.commit(owner,payload(invalid,rebuilt))
    except (FeatureWorkspaceError,PlanValidationError):pass
    else:require(False,'Invalid independent body fillet was published')
    require(store.get(owner,rebuilt['id'])==rebuilt and len(store.versions(owner,rebuilt['id']))==4,
            'Failed independent body edit changed saved history')
    (output/'editor-compound-bodies.step').write_bytes(final_step.read_bytes())
    glb,_=store.artifact(owner,rebuilt['id'],4,'glb')
    (output/'editor-compound-bodies.glb').write_bytes(glb.read_bytes())
    (output/'editor-compound-bodies-plan.json').write_text(json.dumps(rebuilt['plan'],ensure_ascii=False,indent=2)+'\n')
    return {'solidCount':2,'savedRevisions':4,'sourceWidthMm':20,'finalWidthMm':22,
            'initialVolumeMm3':4000,'finalVolumeMm3':rebuilt['inspection']['volumeMm3'],
            'independentSecondVolumeMm3':second_volume,'overlapBeforeMm3':overlap_before,'overlapAfterMm3':overlap_after,
            'selectedEdgeFillet':True,'stepReadback':True,'parameterHistoryRebuilt':True,
            'previousVersionPreserved':True,'failurePreservedHead':True}


def extended_engineering(root, owner, output):
    result=original_engineering(root,owner,output)
    result['cadEditor']=editor_geometry(root,owner,output)
    from cad_complete_editor_smoke import complete_editor_smoke
    result['completeEditor']=complete_editor_smoke(root,owner,output)
    return result


def editor_maintenance_probe(base_url, activation_root):
    """Use this release's actual marker writer and probe on its private API."""
    directory=Path(__file__).resolve().parent
    spec=importlib.util.spec_from_file_location('_editor_smoke_activation',directory/'activate_cad_editor.py')
    activation=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(activation)
    operations=activation_root/'data/operations'
    marker=operations/'maintenance.json'
    require(operations.is_dir() and not marker.exists(),'Private maintenance directory is not clear')
    state={'token':secrets.token_hex(16)}
    marker=activation.base._marker({'root':str(activation_root)},state,create=True)
    opener=build_opener(ProxyHandler({}))

    def request(path, expected, headers=None):
        try:response=opener.open(Request(base_url+path,headers=headers or {}),timeout=5)
        except HTTPError as error:response=error
        with response:
            require(response.status==expected,'Candidate maintenance probe HTTP contract failed')
            return json.loads(response.read(1048576))

    try:
        with (operations/'admission.lock').open('a+') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
            require(json.loads(marker.read_text()).get('format')=='joyniu-currentcad-activation-v1',
                    'Activation marker differs from the API maintenance protocol')
            require(request('/api/v1/operations/readiness',200)=={'gateVersion':'v1','maintenance':True},
                    'Candidate did not observe the private maintenance marker')
            program=activation.base.LOOPBACK_PROBE.replace('http://127.0.0.1:8010',base_url)
            result=subprocess.run([sys.executable,'-c',program],input=json.dumps({**state,'mode':'candidate'}),
                capture_output=True,text=True,timeout=40)
            require(result.returncode==0 and json.loads(result.stdout)=={'ok':True,'mode':'candidate'},
                    'Release activation probe did not pass against the candidate API')
            headers={'X-Joyniu-Maintenance-Probe':state['token']}
            require(request('/api/v1/health',200,headers).get('geometry',{}).get('available') is True,
                    'Candidate geometry unavailable under maintenance')
            request('/api/v1/health',503,{'X-Joyniu-Maintenance-Probe':secrets.token_hex(16)})
            request('/api/v1/health',503,{**headers,'Authorization':'Bearer synthetic-probe-must-stay-anonymous'})
            for path in activation.base.PROTECTED_ROUTES:
                request(path,503)
                request(path,401,headers)
    finally:
        # This file was created exclusively by this smoke in its own temporary
        # directory. Never remove a marker whose ownership has changed.
        if marker.exists() and json.loads(marker.read_text()).get('token')==state['token']:
            marker.unlink()
    require(request('/api/v1/operations/readiness',200)=={'gateVersion':'v1','maintenance':False},
            'Private maintenance marker was not removed')
    request('/api/v1/health',200)
    return True


def editor_concurrent_commit(base_url, token, root):
    """Two real HTTP previews occupy the same API's two native worker slots."""
    from app.geometry import get_cadquery
    plan=box_plan()
    preview_root=root/'cad-feature-workspace/previews'
    before={path.name for path in preview_root.iterdir() if path.is_dir()}
    start=threading.Event()

    def preview(payload, event):
        event.wait(timeout=5)
        return json.loads(baseline.request(base_url,'/api/cad/features/preview',payload=payload,token=token))

    with ThreadPoolExecutor(max_workers=3) as pool:
        previews=[pool.submit(preview,{'plan':copy.deepcopy(plan),'suppressed':[]},start) for _ in range(2)]
        start.set()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            current={path.name for path in preview_root.iterdir() if path.is_dir()}
            # These fresh private directories are created only after admission
            # to _SLOTS and before launching the real isolated OCCT workers.
            if len(current-before)==2 and not any(future.done() for future in previews):break
            require(not any(future.done() for future in previews),'Concurrent preview finished before both workers were observed')
            time.sleep(.01)
        else:require(False,'Two concurrent preview workers were not observed')
        started=time.monotonic()
        limited=json.loads(baseline.request(base_url,'/api/cad/features/preview',expected=422,
            payload={'plan':box_plan(),'suppressed':[]},token=token))
        admission_ms=round((time.monotonic()-started)*1000)
        require(limited.get('detail',{}).get('code')=='resource_limit'
                and not any(future.done() for future in previews),'Preview did not reject immediately while both workers were busy')
        commit={'name':'并发预览后提交验收','fileId':'editor-concurrent-'+uuid4().hex,'plan':box_plan(),'suppressed':[],
                'changeNote':'真实两个预览占槽后的原子提交','requestId':str(uuid4())}
        started=time.monotonic()
        saved=json.loads(baseline.request(base_url,'/api/cad/features/commit',payload=commit,token=token))
        commit_ms=round((time.monotonic()-started)*1000)
        for future in previews:
            result=future.result(timeout=30)
            require(result.get('valid') is True and result['inspection']['solidCount']==1,
                    'An occupied preview did not complete real geometry')
        require(saved['revision']==1 and saved['status']=='built' and saved['inspection']['stepReadback']['valid'],
                'Commit did not wait for preview admission and publish valid geometry')
        step=baseline.request(base_url,f"/api/cad/features/{saved['id']}/versions/1/artifacts/step",token=token)
        path=root/'editor-concurrent-http.step';path.write_bytes(step)
        value=get_cadquery().importers.importStep(str(path)).val()
        require(value.isValid() and len(value.Solids())==1,'Queued HTTP commit STEP readback failed')
        close(value.Volume(),3200)
        # Both original permits must be reusable after the queued commit.
        retry_start=threading.Event()
        retries=[pool.submit(preview,{'plan':box_plan(),'suppressed':[]},retry_start) for _ in range(2)]
        retry_start.set()
        require(all(future.result(timeout=30).get('valid') is True for future in retries),
                'Worker admission capacity did not recover after concurrent work')
    return {'occupiedPreviewWorkers':2,'previewSolidsEach':1,'previewRejectedImmediately':True,
            'previewAdmissionMs':admission_ms,'commitElapsedMs':commit_ms,'stepReadback':True,'capacityReusable':True}


def editor_http(root, username, password):
    # This helper must never accidentally start an API using an inherited live
    # database when called separately from the baseline's private-temp setup.
    require(Path(os.environ.get('JOYNIU_DB','')).resolve()==(root/'joyniu.sqlite3').resolve(),
            'Editor HTTP smoke requires the isolated database environment')
    require(Path(os.environ.get('JOYNIU_CAD_AGENT_DIR','')).resolve()==(root/'cad-agent').resolve(),
            'Editor HTTP smoke requires the isolated task environment')
    from app.platform import AuthService, Role
    auth=AuthService(root/'joyniu.sqlite3')
    viewer_email='editor-viewer-'+uuid4().hex+'@example.invalid';viewer_password=secrets.token_urlsafe(32)
    auth.create_user(viewer_email,viewer_password,'候选只读验收',roles=[Role.VIEWER])
    other_email='editor-other-'+uuid4().hex+'@example.invalid';other_password=secrets.token_urlsafe(32)
    auth.create_user(other_email,other_password,'候选隔离验收',roles=[Role.ADMIN])
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='editor-maintenance-',dir=root) as gate_root, tempfile.TemporaryFile() as log:
        # macOS tempfile paths can pass through /var -> /private/var. Resolve
        # our new private directory before the activation writer's symlink gate.
        gate_root=Path(gate_root).resolve()
        operations=gate_root/'data/operations';operations.mkdir(parents=True)
        environment={**os.environ,'JOYNIU_OPERATIONS_DIR':str(operations)}
        server=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],stdout=log,stderr=log,env=environment)
        try:
            for _ in range(120):
                require(server.poll() is None,'Editor candidate process exited')
                try:baseline.request(base,'/api/v1/health');break
                except (OSError,URLError):time.sleep(.5)
            else:require(False,'Editor candidate did not start')
            maintenance_probe=editor_maintenance_probe(base,gate_root)
            def login(email,pwd):return json.loads(baseline.request(base,'/api/v1/auth/login',payload={'email':email,'password':pwd}))['access_token']
            token=login(username,password);viewer=login(viewer_email,viewer_password);other=login(other_email,other_password)
            payload={'plan':box_plan(),'suppressed':[]}
            for route in ('/api/cad/features/preview','/api/cad/features/commit'):
                baseline.request(base,route,expected=401,payload=payload)
                baseline.request(base,route,expected=403,payload=payload,token=viewer)
            preview=json.loads(baseline.request(base,'/api/cad/features/preview',payload=payload,token=token))
            glb=baseline.request(base,preview['artifacts']['glb']['url'],token=token)
            require(glb[:4]==b'glTF','Authenticated editor GLB invalid')
            baseline.request(base,preview['artifacts']['glb']['url'],expected=404,token=other)
            bad=copy.deepcopy(payload);pick=copy.deepcopy(preview['edges'][0]['selector']);pick['geometryVersion']='0'*64
            bad['plan']['features'].append({'id':'bad','op':'fillet','input':'body','radius':1,'edges':[pick]});bad['plan']['result']='bad'
            baseline.request(base,'/api/cad/features/preview',expected=409,payload=bad,token=token)
            commit={**payload,'name':'HTTP原子提交','fileId':'editor-http-'+uuid4().hex,'changeNote':'候选验收','requestId':str(uuid4())}
            saved=json.loads(baseline.request(base,'/api/cad/features/commit',payload=commit,token=token))
            repeated=json.loads(baseline.request(base,'/api/cad/features/commit',payload=commit,token=token))
            require(saved['id']==repeated['id'] and saved['revision']==repeated['revision']==1,'HTTP commit duplicated on retry')
            # Existing versions and revisions are ownership-bound too, not just
            # downloadable files. A forged update must leave the real head.
            foreign={**commit,'designId':saved['id'],'expectedRevision':1,'requestId':str(uuid4())}
            baseline.request(base,'/api/cad/features/commit',expected=404,payload=foreign,token=other)
            conflict={**foreign,'expectedRevision':99,'requestId':str(uuid4())}
            baseline.request(base,'/api/cad/features/commit',expected=409,payload=conflict,token=token)
            head=json.loads(baseline.request(base,f"/api/cad/features/{saved['id']}",token=token))
            require(head['revision']==1,'Rejected HTTP update changed the saved head')
            private=f"/api/cad/features/{saved['id']}/versions/{saved['revision']}/artifacts/step"
            baseline.request(base,private,expected=404,token=other)
            require(b'ISO-10303-21' in baseline.request(base,private,token=token)[:100],'HTTP STEP artifact invalid')
            concurrency=editor_concurrent_commit(base,token,root)
            return {'anonymousRejected':True,'readerRejected':True,'ownerOnlyArtifacts':True,
                    'staleInteractivePickRejected':True,'atomicHttpCommit':True,'httpIdempotency':True,
                    'foreignUpdateRejected':True,'revisionConflictPreservedHead':True,
                    'maintenanceProbe':maintenance_probe,'commitWaitedForPreviews':True,'concurrency':concurrency}
        finally:
            server.terminate()
            try:server.wait(timeout=20)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)


def extended_http(root,username,password,drawing):
    result=original_http(root,username,password,drawing)
    result['cadEditor']=editor_http(root,username,password)
    return result


if __name__=='__main__':
    baseline.engineering_smoke=extended_engineering
    baseline.http_smoke=extended_http
    raise SystemExit(baseline.main())
