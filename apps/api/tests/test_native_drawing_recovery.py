"""History and lost-response recovery against real SQLite and DXF documents."""
import pytest

pytest.importorskip('ezdxf')
from app.native_drawing import NativeDrawingStore, DrawingError


@pytest.fixture
def store(tmp_path):
    return NativeDrawingStore(tmp_path / 'native.sqlite')


def test_deleted_dimension_undo_recovers_live_source_binding(store):
    doc=store.create('alice')
    doc=store.operate('alice',doc['id'],1,[{'op':'add','entity':{'type':'LINE','start':[0,0],'end':[20,0]}}])
    line=doc['entities'][0]['id']
    refs={'p1':{'entityId':line,'point':'start'},'p2':{'entityId':line,'point':'end'}}
    doc=store.operate('alice',doc['id'],2,[{'op':'dimension','kind':'linear','base':[0,8],'sourceRefs':refs}])
    dimension=doc['dimensions'][0]['id']
    doc=store.operate('alice',doc['id'],3,[{'op':'delete','ids':[dimension]}])
    assert not doc['dimensions']
    doc=store.operate('alice',doc['id'],4,history='undo')
    assert doc['dimensions'][0]['sourceRefs']==refs
    doc=store.operate('alice',doc['id'],5,[{'op':'update','id':line,'changes':{'end':[45,0]}}])
    assert doc['dimensions'][0]['id']==dimension
    assert doc['dimensions'][0]['value']==45


def test_creation_and_import_retry_return_same_document_without_duplicates(store):
    first=store.create('alice','同一新建',request_id='create-one')
    assert store.create('alice','同一新建',request_id='create-one')['id']==first['id']
    dxf=store.export('alice',first['id'],'dxf')[0]
    imported=store.create('alice','同一导入',dxf,'input.dxf',request_id='import-one')
    assert store.create('alice','同一导入',dxf,'input.dxf',request_id='import-one')['id']==imported['id']
    assert len(store.list('alice')['items'])==2
    with pytest.raises(DrawingError,match='其他修改'):
        store.create('alice','不同内容',request_id='create-one')
    assert store.create('bob','同一新建',request_id='create-one')['id']!=first['id']


def test_edit_and_undo_retry_are_idempotent_and_return_latest_revision(store):
    doc=store.create('alice')
    op=[{'op':'add','entity':{'type':'CIRCLE','center':[0,0],'radius':5}}]
    doc=store.operate('alice',doc['id'],1,op,request_id='edit-one')
    replay=store.operate('alice',doc['id'],1,op,request_id='edit-one')
    assert replay['revision']==2 and len(replay['entities'])==1
    doc=store.operate('alice',doc['id'],2,history='undo',request_id='undo-one')
    assert not doc['entities'] and doc['revision']==3
    replay=store.operate('alice',doc['id'],2,history='undo',request_id='undo-one')
    assert replay['revision']==3 and not replay['entities']
    # A late response replay must not resurrect a snapshot predating the undo.
    assert store.operate('alice',doc['id'],1,op,request_id='edit-one')['revision']==3
    with pytest.raises(DrawingError,match='其他修改'):
        store.operate('alice',doc['id'],3,op,request_id='edit-one')


def test_rejected_command_does_not_consume_request_id_or_revision(store):
    doc=store.create('alice')
    with pytest.raises(DrawingError):
        store.operate('alice',doc['id'],1,[{'op':'add','entity':{'type':'CIRCLE','center':[0,0],'radius':0}}],request_id='recover-validation')
    changed=store.operate('alice',doc['id'],1,[{'op':'add','entity':{'type':'CIRCLE','center':[0,0],'radius':2}}],request_id='recover-validation')
    assert changed['revision']==2
