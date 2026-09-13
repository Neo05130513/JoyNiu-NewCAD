import io
import zipfile
from xml.etree import ElementTree as ET

import pytest
from app.native_drawing import NativeDrawingStore, DrawingError


def setup(store):
    doc = store.create('owner', '检验图')
    doc = store.operate('owner', doc['id'], doc['revision'], [
        {'op': 'dimension', 'kind': 'linear', 'p1': [0, 0], 'p2': [100, 0], 'base': [0, 12], 'toleranceUpper': .2, 'toleranceLower': .1},
        {'op': 'dimension', 'kind': 'radius', 'center': [0, 0], 'radius': 10, 'location': [20, 20]},
    ])
    return doc


def balloon(dim, number=1):
    return {'op': 'bubble', 'dimensionId': dim['id'], 'position': [50, 25], 'radius': 3, 'number': number}


def test_linked_bubbles_upsert_reopen_relabel_and_inspection_limits(tmp_path):
    store = NativeDrawingStore(tmp_path / 'drawings.sqlite')
    doc = setup(store)
    source = doc['dimensions'][0]
    doc = store.operate('owner', doc['id'], doc['revision'], [balloon(source)])
    first = doc['bubbles'][0]
    doc = store.operate('owner', doc['id'], doc['revision'], [balloon(source, 7)])
    assert len(doc['bubbles']) == 1
    assert doc['bubbles'][0]['circleId'] == first['circleId']
    assert doc['dimensions'][0]['bubbleNumber'] == 7
    payload = store.export('owner', doc['id'], 'dxf')[0]
    reopened = store.create('owner', '重新导入', payload, 'inspection.dxf')
    assert reopened['bubbles'][0]['number'] == 7
    doc = store.operate('owner', doc['id'], doc['revision'], [{'op': 'update', 'id': first['textId'], 'changes': {'text': '8'}}])
    assert doc['dimensions'][0]['bubbleNumber'] == 8
    xlsx = store.export('owner', doc['id'], 'xlsx')[0]
    with zipfile.ZipFile(io.BytesIO(xlsx)) as archive:
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    assert sheet.find('.//m:c[@r="K2"]/m:v', ns).text == '8'
    assert float(sheet.find('.//m:c[@r="L2"]/m:v', ns).text) == pytest.approx(100.2)
    assert float(sheet.find('.//m:c[@r="M2"]/m:v', ns).text) == pytest.approx(99.9)


def test_bubble_group_move_delete_undo_and_duplicate_rollback(tmp_path):
    store = NativeDrawingStore(tmp_path / 'drawings.sqlite')
    doc = setup(store)
    doc = store.operate('owner', doc['id'], doc['revision'], [balloon(doc['dimensions'][0], 1), balloon(doc['dimensions'][1], 2)])
    first, second = doc['bubbles']
    with pytest.raises(DrawingError, match='重复'):
        store.operate('owner', doc['id'], doc['revision'], [{'op': 'update', 'id': second['textId'], 'changes': {'text': '1'}}])
    assert store.get('owner', doc['id'])['revision'] == doc['revision']
    doc = store.operate('owner', doc['id'], doc['revision'], [{'op': 'transform', 'ids': [first['circleId']], 'translation': [10, 20]}])
    text = next(e for e in doc['entities'] if e['id'] == first['textId'])
    assert text['position'] == [60, 45, 0]
    doc = store.operate('owner', doc['id'], doc['revision'], [{'op': 'delete', 'ids': [first['dimensionId']]}])
    assert len(doc['bubbles']) == 1

    assert not any(e['id'] in {first['circleId'], first['textId'], first['leaderId']} for e in doc['entities'])
    doc = store.operate('owner', doc['id'], doc['revision'], history='undo')
    assert len(doc['bubbles']) == 2
    doc = store.operate('owner', doc['id'], doc['revision'], [{'op': 'delete', 'ids': [first['textId']]}])
    assert len(doc['dimensions']) == 2
    assert len(doc['bubbles']) == 1


def test_malformed_foreign_balloon_xdata_is_preserved_without_trusting_it(tmp_path):
    import ezdxf
    from app.native_drawing import _write
    from app.native_drawing_bubbles import APPID
    source = ezdxf.new('R2010')
    source.appids.new(APPID)
    circle = source.modelspace().add_circle((0, 0), 2)
    circle.set_xdata(APPID, [(1000, '{"circleId":"' + circle.dxf.handle + '","number":1}')])
    store = NativeDrawingStore(tmp_path / 'drawings.sqlite')
    result = store.create('owner', '外部XDATA', _write(source), 'source.dxf')
    assert result['bubbles'] == []
    assert len(result['entities']) == 1
