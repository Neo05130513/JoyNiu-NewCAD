"""Real LibreDWG compatibility tests; no mocked successful conversion."""
import shutil
import pytest

ezdxf = pytest.importorskip('ezdxf')
from app.native_drawing import DrawingError, NativeDrawingStore, _add_dimension, _convert_dwg, _read, _write, export_dwg
from app.native_dwg import _drawing_signature, _first_difference

pytestmark = pytest.mark.skipif(not all(shutil.which(name) for name in ('dxf2dwg', 'dwgread', 'cc')), reason='LibreDWG and C compiler required')

@pytest.mark.parametrize('kind,args,measurement', [
    ('linear', {'p1': [0, 0], 'p2': [40, 0], 'base': [0, 10]}, 40),
    ('aligned', {'p1': [0, 0], 'p2': [3, 4], 'distance': 2}, 5),
    ('radius', {'center': [0, 0], 'radius': 12, 'angle': 45}, 12),
    ('diameter', {'center': [0, 0], 'radius': 12, 'angle': 45}, 24),
    ('angular', {'center': [0, 0], 'p1': [10, 0], 'p2': [0, 10], 'base': [5, 5]}, 90),
])
def test_dimension_survives_real_dwg_without_exploding(kind, args, measurement):
    doc = ezdxf.new('R2010', setup=True)
    doc.units = 4
    dimension = _add_dimension(doc, {'kind': kind, **args, 'toleranceUpper': .02, 'toleranceLower': .01})
    doc = _read(_write(doc))
    payload = export_dwg(doc)
    assert payload[:6] == b'AC1015'
    result = _read(_convert_dwg(payload))
    output = result.modelspace().query('DIMENSION')[0]
    assert output.dxf.handle == dimension.dxf.handle
    assert output.get_measurement() == pytest.approx(measurement)
    assert output.override().get('dimtp') == .02
    assert output.override().get('dimtm') == .01
    assert _first_difference(_drawing_signature(doc), _drawing_signature(result)) == ''
    text = result.blocks[output.dxf.geometry].query('MTEXT')[0]
    assert text.dxf.char_height == 2.5
    for arrow in result.blocks[output.dxf.geometry].query('INSERT'):
        assert arrow.dxf.zscale == 1


def test_chinese_blocks_widths_and_multiple_paperspaces_survive_together(tmp_path):
    doc = ezdxf.new('R2010', setup=True)
    doc.units = 4
    doc.layers.new('VIEWPORTS')
    doc.layers.new('机械轮廓', dxfattribs={'color': 3, 'lineweight': 35, 'linetype': 'DASHED'})
    block = doc.blocks.new('测试零件', base_point=(1, 2, 0))
    block.add_line((1, 2, 0), (11, 12, 3), dxfattribs={'layer': '机械轮廓'})
    block.add_circle((2, 2), 3)
    insert = doc.modelspace().add_blockref('测试零件', (30, 30), dxfattribs={'rotation': 30, 'xscale': 2, 'yscale': 1.5, 'zscale': 3})
    polyline = doc.modelspace().add_lwpolyline([(0, 0, 2, 3, .5), (10, 0, 4, 5, 0)], format='xyseb')
    _add_dimension(doc, {'kind': 'aligned', 'p1': [0, 0], 'p2': [30, 40], 'distance': 10})
    for layout in [doc.layouts.get('Layout1'), doc.layouts.new('中文第二张')]:
        layout.page_setup(size=(297, 210), margins=(10, 10, 10, 10))
        layout.add_text('技术要求：尺寸公差', dxfattribs={'insert': (15, 30)})
        layout.add_circle((20, 20), 7)
        layout.add_viewport(center=(100, 100), size=(100, 80), view_center_point=(20, 20), view_height=50)
    store = NativeDrawingStore(tmp_path / 'drawing.sqlite')
    drawing = store.create('alice', '多布局原图', _write(doc), 'source.dxf')
    payload, _, filename = store.export('alice', drawing['id'], 'dwg')
    assert filename.endswith('.dwg') and payload.startswith(b'AC1015')
    result = _read(_convert_dwg(payload))
    assert result.entitydb[insert.dxf.handle].dxf.name == '测试零件'
    assert result.entitydb[insert.dxf.handle].dxf.zscale == 3
    assert result.entitydb[polyline.dxf.handle].get_points('xyseb')[0] == pytest.approx([0, 0, 2, 3, .5])
    assert result.layouts.names() == doc.layouts.names()
    for layout in result.layouts:
        if layout.name != 'Model':
            assert layout.query('TEXT')[0].dxf.text == '技术要求：尺寸公差'
            assert layout.query('VIEWPORT')[0].dxf.id == 1
    assert _first_difference(_drawing_signature(_read(_write(doc))), _drawing_signature(result)) == ''
    reopened = store.create('alice', 'DWG重新打开', payload, 'export.dwg')
    assert reopened['layouts'] == drawing['layouts']
    assert store.export('alice', reopened['id'], 'pdf', layout='中文第二张')[0].startswith(b'%PDF')


def test_associated_dimension_remains_editable_after_dwg_reimport(tmp_path):
    store = NativeDrawingStore(tmp_path / 'drawing.sqlite')
    drawing = store.create('alice')
    drawing = store.operate('alice', drawing['id'], 1, [{'op': 'add', 'entity': {'type': 'LINE', 'start': [0, 0], 'end': [40, 0]}}])
    line = drawing['entities'][0]['id']
    drawing = store.operate('alice', drawing['id'], 2, [{'op': 'dimension', 'kind': 'linear', 'base': [0, 10], 'sourceRefs': {'p1': {'entityId': line, 'point': 'start'}, 'p2': {'entityId': line, 'point': 'end'}}}])
    reopened = store.create('alice', 'reopened', store.export('alice', drawing['id'], 'dwg')[0], 'reopened.dwg')
    edited = store.operate('alice', reopened['id'], 1, [{'op': 'update', 'id': line, 'changes': {'end': [60, 0]}}])
    assert edited['dimensions'][0]['value'] == 60
    assert edited['dimensions'][0]['sourceRefs']['p2']['entityId'] == line
    output = _read(_convert_dwg(store.export('alice', reopened['id'], 'dwg')[0]))
    assert output.modelspace().query('DIMENSION')[0].get_measurement() == 60


def test_changed_real_dwg_readback_is_blocked(monkeypatch):
    import app.native_drawing as native
    doc = ezdxf.new('R2010', setup=True)
    doc.modelspace().add_circle((10, 10), 5)
    doc = _read(_write(doc))
    converter = native._convert_dwg
    def damaged(payload):
        result = _read(converter(payload))
        result.modelspace().query('CIRCLE')[0].dxf.radius = 7
        return _write(result)
    monkeypatch.setattr(native, '_convert_dwg', damaged)
    with pytest.raises(DrawingError, match='radius'):
        export_dwg(doc)


def test_unverified_types_are_specific_not_all_blocks():
    doc = ezdxf.new('R2010', setup=True)
    block = doc.blocks.new('带属性图框')
    block.add_attdef('NUMBER', (0, 0), text='P001')
    doc.modelspace().add_blockref('带属性图框', (0, 0))
    with pytest.raises(DrawingError, match='ATTDEF'):
        export_dwg(_read(_write(doc)))


def test_real_dwg_preserves_dimension_source_and_balloon_xdata(tmp_path):
    import json
    from app.native_drawing import DIMENSION_REF_APPID
    from app.native_drawing_bubbles import APPID
    store = NativeDrawingStore(tmp_path / 'inspection.sqlite')
    drawing = store.create('owner')
    drawing = store.operate('owner', drawing['id'], 1, [{'op': 'add', 'entity': {'type': 'LINE', 'start': [0, 0], 'end': [150, 0]}}])
    line = drawing['entities'][0]['id']
    drawing = store.operate('owner', drawing['id'], 2, [{'op': 'dimension', 'kind': 'linear', 'base': [0, 15], 'toleranceUpper': .2, 'toleranceLower': .1, 'sourceRefs': {'p1': {'entityId': line, 'point': 'start'}, 'p2': {'entityId': line, 'point': 'end'}}}])
    dimension_id = drawing['dimensions'][0]['id']
    drawing = store.operate('owner', drawing['id'], 3, [{'op': 'bubble', 'dimensionId': dimension_id, 'number': 7, 'position': [-25, 40], 'radius': 4}])
    payload = store.export('owner', drawing['id'], 'dwg')[0]
    doc = _read(_convert_dwg(payload))
    dimension = doc.entitydb[dimension_id]
    assert dimension.get_measurement() == 150
    assert dimension.override().get('dimtp') == .2 and dimension.override().get('dimtm') == .1
    refs = json.loads(''.join(tag.value for tag in dimension.get_xdata(DIMENSION_REF_APPID) if tag.code == 1000))
    assert refs['sourceRefs']['p2']['entityId'] == line
    bubble = drawing['bubbles'][0]
    assert doc.entitydb[bubble['circleId']].has_xdata(APPID)
    text = doc.entitydb[bubble['textId']]
    assert text.dxf.text == '7' and list(text.dxf.align_point) == [-25, 40, 0]
    reopened = store.create('owner', 'DWG检验图', payload, 'inspection.dwg')
    assert reopened['bubbles'][0]['number'] == 7
    assert reopened['dimensions'][0]['bubbleNumber'] == 7
