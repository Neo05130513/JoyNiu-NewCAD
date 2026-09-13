"""Real OCCT preview + HTTP login against an isolated restored synthetic copy."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from app.cad_feature_workspace import CadFeatureWorkspace
from app.geometry import get_cadquery
from app.platform import AuthService, Role


API=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('_restored_preview_real_smoke',API.parents[1]/'deploy/currentcad_release_smoke.py')
smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_restored_preexpired_cache_cleanup_preserves_saved_revisions_active_preview_sessions_and_source(tmp_path):
    source=tmp_path/'backup-data';source.mkdir()
    auth=AuthService(source/'joyniu.sqlite3')
    original_user=auth.create_user('original@example.invalid',secrets.token_urlsafe(32),'原有合成账号',roles=[Role.ADMIN])
    auth.create_session(original_user)
    store=CadFeatureWorkspace(source/'cad-feature-workspace')
    plan={'version':'cad-plan-v1','units':'mm','parameters':{},'features':[{'id':'body','op':'box','size':[20,16,10]}],'result':'body'}
    saved=store.commit(original_user.id,{'name':'不可修改的已保存合成件','plan':plan,
        'changeNote':'独立恢复验收的原有已保存版本','requestId':secrets.token_hex(16)})
    assert saved['inspection']['stepReadback']['valid'] and saved['inspection']['volumeMm3']==pytest.approx(3200)
    expired=store.preview(original_user.id,{'plan':plan})
    active=store.preview(original_user.id,{'plan':plan})
    cutoff=int(time.time())
    with store._db() as db:
        payload=json.loads(db.execute('SELECT payload FROM manual_feature_previews WHERE id=?',(expired['previewId'],)).fetchone()[0])
        payload['expiresAt']=cutoff-1
        db.execute('UPDATE manual_feature_previews SET created=?,payload=? WHERE id=?',
                   (cutoff-1801,json.dumps(payload),expired['previewId']))
    with sqlite3.connect(source/'joyniu.sqlite3') as db:
        db.execute('INSERT INTO auth_rate_limits VALUES (?, ?, ?, ?)',('expired-synthetic-distinct-identity',cutoff-200,1,cutoff-1))
    original_files=smoke.file_hashes(source)
    restored=tmp_path/'restored-data';shutil.copytree(source,restored)
    before={}
    for database in restored.rglob('*.sqlite3'):
        relative=database.relative_to(restored).as_posix()
        before[relative]=smoke.snapshot_database(database,
            expired_auth_rate_limit_before=cutoff if relative=='joyniu.sqlite3' else None,
            expired_manual_preview_before=cutoff if relative==smoke.PREVIEW_CACHE_DATABASE else None)
    password=secrets.token_urlsafe(32);email='new-candidate@example.invalid'
    candidate=AuthService(restored/'joyniu.sqlite3').create_user(email,password,'新合成验收账号',roles=[Role.ADMIN])
    copied_store=CadFeatureWorkspace(restored/'cad-feature-workspace')
    fresh=copied_store.preview(candidate.id,{'plan':plan})
    assert fresh['valid'] and fresh['inspection']['solidCount']==1
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    env={**os.environ,'JOYNIU_DB':str(restored/'joyniu.sqlite3'),'JOYNIU_CAD_AGENT_DIR':str(restored/'cad-agent'),
         'JOYNIU_CAD_DESIGNS_ROOT':str(restored/'cad-designs'),'JOYNIU_OPERATIONS_DIR':str(restored/'operations'),
         'JOYNIU_AUTH_SECRET':secrets.token_hex(32),'JOYNIU_BILLING_ENABLED':'false','JOYNIU_ONLINE_PAYMENTS_ENABLED':'false',
         'JOYNIU_PUBLIC_REGISTRATION':'false','JOYNIU_AI_ALLOW_ANONYMOUS':'0','JOYNIU_ENV':'production'}
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),
                                  '--no-access-log'],cwd=API,env=env,stdout=log,stderr=log)
        try:
            for _ in range(120):
                assert process.poll() is None,'Isolated API startup failed'
                try:smoke.request(base,'/api/v1/health');break
                except OSError:time.sleep(.25)
            else:pytest.fail('Isolated API health timeout')
            session=json.loads(smoke.request(base,'/api/v1/auth/login',payload={'email':email,'password':password}))
            assert session['access_token']
            entries=json.loads(smoke.request(base,'/api/cad/features',token=session['access_token']))
            assert entries['items']==[]  # New actor does not inherit the restored owner's saved model.
        finally:
            process.terminate()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    counts={key:0 for key in ('expiredAuthRateLimitRowsAllowed','expiredAuthRateLimitRowsRemoved',
                             'expiredManualPreviewRowsAllowed','expiredManualPreviewRowsRemoved')}
    missing_tables=[]
    for relative,tables in before.items():
        after=smoke.snapshot_database(restored/relative,{name:row['columns'] for name,row in tables.items()})
        missing_tables.extend((relative,name) for name,row in tables.items() if row['hashes']-after[name]['hashes'])
        for key,value in smoke.verify_restored_database(tables,after).items():counts[key]+=value
    assert set(missing_tables)=={('joyniu.sqlite3','auth_rate_limits'),(smoke.PREVIEW_CACHE_DATABASE,'manual_feature_previews')}
    assert counts==dict.fromkeys(counts,1)
    assert copied_store.get(original_user.id,saved['id'],1)==saved
    with copied_store._db() as db:
        assert db.execute('SELECT 1 FROM manual_feature_previews WHERE id=?',(active['previewId'],)).fetchone()
        assert not db.execute('SELECT 1 FROM manual_feature_previews WHERE id=?',(expired['previewId'],)).fetchone()
    assert smoke.file_hashes(source)==original_files
