from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from app import cad_feature_workspace as module
from app.cad_feature_workspace import CadFeatureWorkspace, FeatureWorkspaceError
from app.geometry import get_cadquery


def payload():
    return {'name':'等待后提交','changeNote':'完成用户尺寸','requestId':'admission-commit-001','suppressed':[],
            'plan':{'version':'cad-plan-v1','parameters':{},'features':[{'id':'body','op':'box','size':[20,16,10]}],'result':'body'}}


@pytest.fixture
def slots(monkeypatch):
    value=threading.BoundedSemaphore(2)
    monkeypatch.setattr(module,'_SLOTS',value)
    return value


def assert_two_free(slots):
    assert slots.acquire(blocking=False)
    try:
        assert slots.acquire(blocking=False)
        try: assert not slots.acquire(blocking=False)
        finally: slots.release()
    finally: slots.release()


@pytest.mark.skipif(get_cadquery() is None,reason='Requires real OCCT')
def test_commit_waits_for_release_then_builds_real_step_without_exceeding_two_slots(tmp_path,slots):
    store=CadFeatureWorkspace(tmp_path)
    assert module._COMMIT_ADMISSION_TIMEOUT_SECONDS==5.0
    slots.acquire();slots.acquire()
    timer=threading.Timer(.2,slots.release)
    started=time.monotonic();timer.start()
    try:
        result=store.commit('alice',payload())
        assert time.monotonic()-started>=.18
        assert result['status']=='built' and result['revision']==1
        shape=get_cadquery().importers.importStep(result['artifacts']['step']['path']).val()
        assert shape.isValid() and len(shape.Solids())==1 and shape.Volume()==pytest.approx(3200)
        # One unrelated operation remains admitted; commit must release exactly
        # its own slot, neither leak it nor free the other operation's slot.
        assert slots.acquire(blocking=False)
        try: assert not slots.acquire(blocking=False)
        finally: slots.release()
    finally:
        timer.join();slots.release()
    assert_two_free(slots)


@pytest.mark.parametrize('existing',[False,True])
@pytest.mark.parametrize('cancelled',[False,True])
def test_wait_cancel_or_timeout_preserves_versions_and_slots(tmp_path,monkeypatch,slots,existing,cancelled):
    called=[]
    def forbidden(*args,**kwargs): called.append(True);raise AssertionError('No worker may start without admission')
    store=CadFeatureWorkspace(tmp_path,executor=forbidden,preview_executor=forbidden)
    data=payload();original=store.save('alice',data) if existing else None
    if original:data={**data,'designId':original['id'],'expectedRevision':original['revision']}
    monkeypatch.setattr(module,'_COMMIT_ADMISSION_TIMEOUT_SECONDS',.3)
    cancel=threading.Event();slots.acquire();slots.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            started=time.monotonic();future=pool.submit(store.commit,'alice',data,cancel)
            time.sleep(.08)
            assert not future.done() and not called
            if cancelled:cancel.set()
            with pytest.raises(FeatureWorkspaceError) as error:future.result(timeout=2)
            assert error.value.code==('user_cancelled' if cancelled else 'resource_limit')
            assert time.monotonic()-started<1
            if not cancelled:assert time.monotonic()-started>=.28
        assert not called and not slots.acquire(blocking=False)
        if original:
            assert store.get('alice',original['id'])==original
            assert len(store.versions('alice',original['id']))==1
        else:assert store.list('alice')==[]
        assert list(tmp_path.glob('feature_*/commit-*'))==[]
        with store._db() as db:assert db.execute('SELECT COUNT(*) FROM manual_feature_commits').fetchone()[0]==0
        # Previews do not join the commit wait queue even when an admission
        # deadline is configured; they still reject without invoking a worker.
        started=time.monotonic()
        with pytest.raises(FeatureWorkspaceError) as error:store.preview('alice',{'plan':payload()['plan']})
        assert error.value.code=='resource_limit' and time.monotonic()-started<.2
    finally:slots.release();slots.release()
    assert_two_free(slots)


def test_cancel_at_slot_handoff_returns_admission_without_starting_worker(tmp_path,monkeypatch):
    cancel=threading.Event()
    class Handoff:
        released=0
        def acquire(self,**kwargs):cancel.set();return True
        def release(self):self.released+=1
    slots=Handoff();monkeypatch.setattr(module,'_SLOTS',slots)
    store=CadFeatureWorkspace(tmp_path,executor=lambda *args,**kwargs:pytest.fail('Cancelled work launched'))
    with pytest.raises(FeatureWorkspaceError) as error:store.commit('alice',payload(),cancel)
    assert error.value.code=='user_cancelled' and slots.released==1
    assert store.list('alice')==[] and list(tmp_path.glob('feature_*/commit-*'))==[]


def test_already_cancelled_commit_does_not_acquire_a_free_slot(tmp_path,slots):
    cancel=threading.Event();cancel.set();store=CadFeatureWorkspace(tmp_path)
    with pytest.raises(FeatureWorkspaceError) as error:store.commit('alice',payload(),cancel)
    assert error.value.code=='user_cancelled' and store.list('alice')==[]
    assert_two_free(slots)
