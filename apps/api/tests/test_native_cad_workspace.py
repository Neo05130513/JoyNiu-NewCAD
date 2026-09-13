import pytest
from app.native_drawing import NativeDrawingStore, DrawingError, _read

def test_ellipse_group_layout_roundtrip_history_and_isolation(tmp_path):
    store=NativeDrawingStore(tmp_path/'native.sqlite')
    doc=store.create('alice','Ribbon acceptance')
    doc=store.operate('alice',doc['id'],doc['revision'],[
        {'op':'add','entity':{'type':'ELLIPSE','center':[20,20],'majorAxis':[10,0],'ratio':.5}},
        {'op':'add','entity':{'type':'LINE','start':[0,0],'end':[50,0]}},
    ])
    assert doc['entities'][0]['type']=='ELLIPSE'
    ids=[e['id'] for e in doc['entities']]
    doc=store.operate('alice',doc['id'],doc['revision'],[{'op':'group','ids':ids},{'op':'layout','name':'Layout2'}])
    assert doc['groups'][0]['ids']==ids
    reopened=NativeDrawingStore(store.database).get('alice',doc['id'])
    assert reopened['groups']==doc['groups'] and 'Layout2' in reopened['layouts']
    raw,_,_=store.export('alice',doc['id']);drawing=_read(raw)
    ellipse=drawing.modelspace().query('ELLIPSE')[0]
    assert ellipse.dxf.ratio==.5 and ellipse.dxf.major_axis.x==10
    assert len(drawing.layouts.get('Layout2').query('VIEWPORT'))>=1
    svg,_,_=store.export('alice',doc['id'],'svg',layout='Layout2')
    assert b'<svg' in svg
    undone=store.operate('alice',doc['id'],doc['revision'],history='undo')
    assert undone['groups']==[] and 'Layout2' not in undone['layouts']
    doc=store.operate('alice',doc['id'],undone['revision'],history='redo')
    doc=store.operate('alice',doc['id'],doc['revision'],[{'op':'ungroup','name':doc['groups'][0]['name']}])
    assert doc['groups']==[] and len(doc['entities'])==2
    with pytest.raises(DrawingError):
        store.operate('alice',doc['id'],doc['revision'],[{'op':'layout','name':'Model'}])
    with pytest.raises(DrawingError):
        store.operate('alice',doc['id'],doc['revision'],[{'op':'add','entity':{'type':'ELLIPSE','center':[0,0],'majorAxis':[0,0],'ratio':.5}}])
    assert store.get('alice',doc['id'])['revision']==doc['revision']
