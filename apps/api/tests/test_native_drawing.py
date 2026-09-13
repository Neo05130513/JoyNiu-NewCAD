import io
import json
import math
import shutil
import sqlite3
import zipfile
from xml.etree import ElementTree as ET

import pytest

ezdxf = pytest.importorskip('ezdxf')
from app.native_drawing import (DrawingConflict, DrawingError, DrawingNotFound,
                                NativeDrawingStore, _read, _write)


@pytest.fixture
def drawings(tmp_path):
    return NativeDrawingStore(tmp_path / 'native.sqlite')


def add_line(start=(0, 0), end=(100, 0), **attributes):
    return {'action': 'add', 'entity': {'type': 'LINE', 'start': list(start), 'end': list(end), **attributes}}


def test_complete_document_preserves_unsupported_blocks_layouts_and_xdata(drawings):
    original = ezdxf.new('R2010')
    original.layers.new('原图层', dxfattribs={'color': 3})
    original.appids.new('SOURCE_EVIDENCE')
    line = original.modelspace().add_line((0, 0), (20, 0), dxfattribs={'layer': '原图层'})
    line.set_xdata('SOURCE_EVIDENCE', [(1000, '必须保留')])
    hatch = original.modelspace().add_hatch(color=2)
    hatch.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
    block = original.blocks.new('测试块')
    block.add_circle((2, 2), 3)
    insert = original.modelspace().add_blockref('测试块', (30, 30))
    original.layouts.get('Layout1').add_text('图框正文')
    result = drawings.create('alice', '原图', _write(original), 'test.dxf')
    assert result['unsupportedTypes'] == {'HATCH': 1, 'INSERT': 1}
    changed = drawings.operate('alice', result['id'], 1, [{'action': 'update', 'id': line.dxf.handle, 'changes': {'end': [50, 0]}}])
    output, _, _ = drawings.export('alice', result['id'])
    reread = _read(output)
    assert reread.entitydb[line.dxf.handle].dxf.end.x == 50
    assert reread.entitydb[line.dxf.handle].get_xdata('SOURCE_EVIDENCE')[0].value == '必须保留'
    assert reread.entitydb[hatch.dxf.handle].paths[0].vertices == hatch.paths[0].vertices
    assert reread.entitydb[insert.dxf.handle].dxf.name == '测试块'
    assert len(reread.blocks['测试块']) == 1
    assert reread.layouts.get('Layout1').query('TEXT')[0].dxf.text == '图框正文'
    assert changed['revision'] == 2
    assert NativeDrawingStore(drawings.database).get('alice', result['id'])['entities'] == changed['entities']
    with pytest.raises(DrawingError, match='HATCH'):
        drawings.export('alice', result['id'], 'dwg')


def test_revision_conflict_owner_isolation_and_atomic_rollback(drawings):
    result = drawings.create('alice')
    identity = result['id']
    with pytest.raises(DrawingNotFound):
        drawings.get('bob', identity)
    with pytest.raises(DrawingNotFound):
        drawings.export('bob', identity)
    assert drawings.list('bob') == {'items': []}
    drawings.operate('alice', identity, 1, [add_line()])
    with pytest.raises(DrawingConflict) as error:
        drawings.operate('alice', identity, 1, [add_line()])
    assert error.value.revision == 2
    with pytest.raises(DrawingError):
        drawings.operate('alice', identity, 2, [add_line(), {'action': 'add', 'entity': {'type': 'CIRCLE', 'center': [0, 0], 'radius': -1}}])
    assert len(drawings.get('alice', identity)['entities']) == 1
    assert drawings.get('alice', identity)['revision'] == 2


def test_native_transform_trim_offset_and_polyline_bulge_roundtrip(drawings):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line(end=(10, 0)), {'action': 'add', 'entity': {'type': 'LWPOLYLINE', 'points': [[0, 0, 0.5], [10, 10, 0]], 'closed': True}}])
    handle = result['entities'][0]['id']
    result = drawings.operate('alice', result['id'], 2, [{'action': 'transform', 'ids': [handle], 'rotation': 90, 'translation': [5, 5], 'scale': 2}, {'action': 'trim', 'id': handle, 'start': 0.25, 'end': 0.75}, {'action': 'offset', 'ids': [handle], 'distance': 3}])
    lines = [entity for entity in result['entities'] if entity['type'] == 'LINE']
    assert lines[0]['start'] == pytest.approx([5, 10, 0])
    assert lines[0]['end'] == pytest.approx([5, 20, 0])
    assert lines[1]['start'] == pytest.approx([2, 10, 0])
    doc = _read(drawings.export('alice', result['id'])[0])
    assert doc.modelspace().query('LWPOLYLINE')[0].get_points('xyb')[0][2] == 0.5


def test_editing_polyline_coordinates_preserves_original_segment_widths(drawings):
    original = ezdxf.new('R2010')
    polyline = original.modelspace().add_lwpolyline([(0, 0, 2, 3, .5), (10, 0, 3, 4, 0)], format='xyseb')
    result = drawings.create('alice', '宽线', _write(original), 'widths.dxf')
    result = drawings.operate('alice', result['id'], 1, [{'op': 'update', 'id': polyline.dxf.handle, 'changes': {'points': [[5, 0, .5], [20, 0, 0]]}}])
    output = _read(drawings.export('alice', result['id'])[0]).modelspace().query('LWPOLYLINE')[0]
    assert output.get_points('xyseb')[0] == pytest.approx([5, 0, 2, 3, .5])
    with pytest.raises(DrawingError, match='逐段宽度'):
        drawings.operate('alice', result['id'], 2, [{'op': 'update', 'id': polyline.dxf.handle, 'changes': {'points': [[5, 0], [20, 0], [30, 10]]}}])


def test_undo_redo_restores_full_dxf_and_metadata_and_invalidates_redo(drawings):
    created = drawings.create('alice', '开始')
    result = drawings.operate('alice', created['id'], 1, [add_line(), {'action': 'metadata', 'name': '修改', 'customProperties': {'paper': 'A3', 'constraints': []}}])
    result = drawings.operate('alice', created['id'], 2, history='undo')
    assert result['revision'] == 3 and result['entities'] == []
    assert result['name'] == '开始' and result['canRedo']
    result = drawings.operate('alice', created['id'], 3, history='redo')
    assert result['name'] == '修改' and len(result['entities']) == 1
    assert result['customProperties']['paper'] == 'A3'
    result = drawings.operate('alice', created['id'], 4, history='undo')
    result = drawings.operate('alice', created['id'], 5, [add_line(end=(50, 0))])
    assert not result['canRedo']


@pytest.mark.parametrize('kind,parameters,expected', [
    ('linear', {'p1': [0, 0], 'p2': [40, 0], 'base': [0, 10]}, 40),
    ('aligned', {'p1': [0, 0], 'p2': [3, 4], 'distance': 2}, 5),
    ('radius', {'center': [0, 0], 'radius': 12, 'angle': 45}, 12),
    ('diameter', {'center': [0, 0], 'radius': 12, 'angle': 45}, 24),
    ('angular', {'center': [0, 0], 'p1': [10, 0], 'p2': [0, 10], 'base': [5, 5]}, 90),
])
def test_dimensions_measurements_tolerances_and_update_preserve_definition(drawings, kind, parameters, expected):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [{'action': 'dimension', 'kind': kind, **parameters, 'toleranceUpper': 0.1, 'toleranceLower': 0.2}])
    dimension = result['dimensions'][0]
    assert dimension['value'] == pytest.approx(expected)
    assert dimension['measurementScale'] == 1
    assert dimension['displayValue'] == pytest.approx(expected)
    result = drawings.operate('alice', created['id'], 2, [{'action': 'update', 'id': dimension['id'], 'changes': {'text': '<> mm', 'toleranceUpper': 0.3, 'style': {'dimtxt': 3}}}])
    updated = result['dimensions'][0]
    assert updated['points'] == dimension['points']
    assert updated['value'] == pytest.approx(expected)
    assert updated['toleranceUpper'] == 0.3 and updated['toleranceLower'] == 0.2
    reread = _read(drawings.export('alice', created['id'])[0])
    assert reread.modelspace().query('DIMENSION')[0].override().get('dimtp') == 0.3


def test_xlsx_is_real_ooxml_measures_not_inferred_text_and_no_formula_execution(drawings):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [{'action': 'dimension', 'kind': 'linear', 'p1': [0, 0], 'p2': [25, 0], 'base': [0, 10], 'text': '=HYPERLINK("http://invalid")', 'toleranceUpper': 0.02, 'toleranceLower': 0.01}, {'action': 'bubble', 'position': [10, 10], 'number': 1, 'target': [0, 0]}])
    data, content_type, filename = drawings.export('alice', result['id'], 'xlsx')
    assert filename.endswith('.xlsx') and 'spreadsheetml' in content_type
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert 'xl/workbook.xml' in archive.namelist()
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        assert sheet.find('.//m:c[@r="C2"]/m:v', ns).text == '25.0'
        assert sheet.find('.//m:c[@r="E2"]/m:v', ns).text == '0.02'
        assert sheet.find('.//m:c[@r="D2"]', ns).attrib['t'] == 'inlineStr'
        assert sheet.findall('.//m:f', ns) == []
        for path in archive.namelist():
            ET.fromstring(archive.read(path))
    reread = _read(drawings.export('alice', result['id'])[0])
    assert len(reread.modelspace().query('CIRCLE')) == 1
    assert len(reread.modelspace().query('TEXT')) == 1


def test_pdf_paper_size_explicit_scale_and_monochrome(drawings):
    fitz = pytest.importorskip('fitz')
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line(color=1)])
    pdf, content_type, _filename = drawings.export('alice', result['id'], 'pdf', paper='A4', scale='1:1', color='monochrome')
    assert pdf.startswith(b'%PDF-') and content_type == 'application/pdf'
    with fitz.open(stream=pdf, filetype='pdf') as document:
        page = document[0]
        assert page.rect.width == pytest.approx(210 / 25.4 * 72, abs=0.1)
        assert page.rect.height == pytest.approx(297 / 25.4 * 72, abs=0.1)
        lines = [item for drawing in page.get_drawings() for item in drawing['items'] if item[0] == 'l']
        assert len(lines) >= 1
        length = math.hypot(lines[0][2].x - lines[0][1].x, lines[0][2].y - lines[0][1].y)
        assert length == pytest.approx(100 / 25.4 * 72, abs=0.5)
        assert next(drawing for drawing in page.get_drawings() if drawing['type'] == 's')['color'] == (0, 0, 0)
    svg, _, _ = drawings.export('alice', result['id'], 'svg')
    assert ET.fromstring(svg).tag.endswith('svg')


@pytest.mark.skipif(not shutil.which('dxf2dwg') or not shutil.which('dwg2dxf'), reason='LibreDWG not installed')
def test_real_dwg_binary_export_then_import_preserves_modified_geometry(drawings):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line(end=(125, 0)), {'action': 'add', 'entity': {'type': 'CIRCLE', 'center': [50, 40], 'radius': 10}}])
    data, _, filename = drawings.export('alice', result['id'], 'dwg')
    assert data.startswith(b'AC1015')
    reopened = drawings.create('alice', 'DWG 回读', data, filename)
    assert reopened['source']['format'] == 'dwg'
    assert reopened['entities'][0]['end'] == [125, 0, 0]
    assert reopened['entities'][1]['radius'] == 10
    assert drawings.export('alice', reopened['id'], 'original')[0] == data


def test_locked_layers_invalid_operations_and_limits_leave_document_unchanged(drawings):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line(layer='锁定'), {'action': 'layer', 'name': '锁定', 'locked': True}])
    handle = result['entities'][0]['id']
    for operation in [{'action': 'delete', 'ids': [handle]}, {'action': 'update', 'id': handle, 'changes': {'end': [2, 2]}}, {'action': 'metadata', 'customProperties': {'large': 'x' * 70000}}, {'action': 'add', 'entity': {'type': 'LINE', 'start': [float('nan'), 0], 'end': [1, 1]}}]:
        with pytest.raises(DrawingError):
            drawings.operate('alice', result['id'], 2, [operation])
    assert drawings.get('alice', result['id'])['revision'] == 2


def test_lineweight_linetype_persist_and_reject_unknown_definitions(drawings):
    created = drawings.create('alice')
    dashed = next(name for name in created['linetypes'] if name.casefold() == 'dashed')
    result = drawings.operate('alice', created['id'], 1, [add_line(lineweight=35, linetype=dashed), {'action': 'layer', 'name': '边界', 'lineweight': 50, 'linetype': dashed}])
    line = result['entities'][0]
    assert line['lineweight'] == 35 and line['linetype'] == dashed
    assert next(layer for layer in result['layers'] if layer['name'] == '边界')['lineweight'] == 50
    result = drawings.operate('alice', created['id'], 2, [{'action': 'update', 'id': line['id'], 'changes': {'lineweight': 70, 'linetype': 'CONTINUOUS'}}])
    entity = _read(drawings.export('alice', created['id'])[0]).modelspace().query('LINE')[0]
    assert entity.dxf.lineweight == 70 and entity.dxf.linetype == 'CONTINUOUS'
    with pytest.raises(DrawingError, match='线型'):
        drawings.operate('alice', created['id'], 3, [{'action': 'update', 'id': line['id'], 'changes': {'linetype': 'missing-definition'}}])


def test_display_geometry_expands_dimension_arcs_transformed_blocks_and_bulges(drawings):
    doc = ezdxf.new('R2010', setup=True)
    block = doc.blocks.new('Rotated')
    block.add_line((0, 0), (10, 0))
    insert = doc.modelspace().add_blockref('Rotated', (20, 30), dxfattribs={'rotation': 90})
    polyline = doc.modelspace().add_lwpolyline([(0, 0, 1), (10, 0, 0)], format='xyb')
    result = drawings.create('alice', '展开', _write(doc), 'display.dxf')
    result = drawings.operate('alice', result['id'], 1, [{'action': 'dimension', 'kind': 'angular', 'center': [0, 0], 'p1': [10, 0], 'p2': [0, 10], 'base': [7, 7]}])
    block_display = [item for item in result['renderEntities'] if item['parentId'] == insert.dxf.handle]
    assert block_display[0]['start'] == pytest.approx([20, 30, 0])
    assert block_display[0]['end'] == pytest.approx([20, 40, 0])
    assert next(item for item in result['renderEntities'] if item['parentId'] == polyline.dxf.handle)['type'] == 'ARC'
    dimension = result['dimensions'][0]
    display = [item for item in result['renderEntities'] if item['parentId'] == dimension['id']]
    assert any(item['type'] == 'ARC' for item in display)
    assert any(item['type'] in ('TEXT', 'MTEXT') and '90' in item['text'] for item in display)
    assert all(not item['editable'] for item in display)


def test_persisted_constraints_reject_inconsistent_direct_geometry_and_parameter_changes(drawings):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line()])
    identity = result['entities'][0]['id']
    props = {'constraints': [{'type': 'length', 'entities': [identity], 'value': 'width * 2'}, {'type': 'horizontal', 'entities': [identity]}], 'parameters': {'width': '50'}}
    result = drawings.operate('alice', created['id'], 2, [{'op': 'metadata', 'customProperties': props}])
    with pytest.raises(DrawingError, match='不满足'):
        drawings.operate('alice', created['id'], 3, [{'op': 'update', 'id': identity, 'changes': {'end': [120, 0]}}])
    with pytest.raises(DrawingError, match='不满足'):
        drawings.operate('alice', created['id'], 3, [{'op': 'metadata', 'customProperties': {**props, 'parameters': {'width': '60'}}}])
    # The UI may submit solved geometry and parameters atomically.
    result = drawings.operate('alice', created['id'], 3, [{'op': 'update', 'id': identity, 'changes': {'end': [120, 0]}}, {'op': 'metadata', 'customProperties': {**props, 'parameters': {'width': '60'}}}])
    assert result['entities'][0]['end'][0] == 120
    # Removing a reference and its constraint in the same edit is valid.
    result = drawings.operate('alice', created['id'], 4, [{'op': 'delete', 'ids': [identity]}, {'op': 'metadata', 'customProperties': {'constraints': [], 'parameters': {}}}])
    assert result['entities'] == []


def test_constraint_formula_grammar_precedence_and_fixed_baseline(drawings):
    from app.native_drawing import _formula
    assert _formula('-2^2', {}) == -4
    assert _formula('2^-2', {}) == 0.25
    assert _formula('max(2) + min(4, 5)', {}) == 6
    for expression in ['__import__("os")', 'a.x', '(lambda: 1)()', '1/0']:
        with pytest.raises(DrawingError):
            _formula(expression, {})
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line()])
    line = result['entities'][0]
    result = drawings.operate('alice', created['id'], 2, [{'op': 'metadata', 'customProperties': {'constraints': [{'type': 'fixed', 'entities': [line['id']], 'geometry': line}]}}])
    with pytest.raises(DrawingError, match='不满足'):
        drawings.operate('alice', created['id'], 3, [{'op': 'transform', 'ids': [line['id']], 'translation': [5, 0]}])


@pytest.mark.parametrize('kind', ['linear', 'aligned'])
def test_associated_linear_dimensions_follow_source_keep_id_style_tolerance_and_roundtrip(drawings, kind):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line(end=(40, 0))])
    line_id = result['entities'][0]['id']
    refs = {'p1': {'entityId': line_id, 'point': 'start'}, 'p2': {'entityId': line_id, 'point': 'end'}}
    result = drawings.operate('alice', created['id'], 2, [{'op': 'dimension', 'kind': kind, 'base': [0, 10], 'distance': 10, 'sourceRefs': refs, 'text': '<> mm', 'toleranceUpper': .02, 'toleranceLower': .01, 'style': {'dimtxt': 3}}])
    dimension_id = result['dimensions'][0]['id']
    assert result['dimensions'][0]['value'] == 40
    result = drawings.operate('alice', created['id'], 3, [{'op': 'update', 'id': line_id, 'changes': {'end': [80, 0]}}])
    dimension = result['dimensions'][0]
    assert dimension['id'] == dimension_id and dimension['sourceRefs'] == refs
    assert dimension['value'] == 80 and dimension['kind'] == kind
    assert dimension['toleranceUpper'] == .02 and dimension['toleranceLower'] == .01
    assert dimension['text'] == '<> mm' and dimension['style']['dimtxt'] == 3
    exported = drawings.export('alice', result['id'])[0]
    reopened = drawings.create('alice', '重新导入', exported, 'linked.dxf')
    assert reopened['dimensions'][0]['sourceRefs'] == refs
    reopened = drawings.operate('alice', reopened['id'], 1, [{'op': 'update', 'id': line_id, 'changes': {'end': [90, 0]}}])
    assert reopened['dimensions'][0]['value'] == 90


@pytest.mark.parametrize('kind,multiplier', [('radius', 1), ('diameter', 2)])
def test_associated_radial_dimensions_follow_radius_and_center(drawings, kind, multiplier):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [{'op': 'add', 'entity': {'type': 'CIRCLE', 'center': [10, 20], 'radius': 5}}])
    circle_id = result['entities'][0]['id']
    result = drawings.operate('alice', created['id'], 2, [{'op': 'dimension', 'kind': kind, 'sourceRefs': {'circle': {'entityId': circle_id}}, 'angle': 45}])
    dimension_id = result['dimensions'][0]['id']
    result = drawings.operate('alice', created['id'], 3, [{'op': 'update', 'id': circle_id, 'changes': {'center': [30, 40], 'radius': 12}}])
    assert result['dimensions'][0]['id'] == dimension_id
    assert result['dimensions'][0]['value'] == pytest.approx(12 * multiplier)
    dimension = _read(drawings.export('alice', result['id'])[0]).entitydb[dimension_id]
    center = dimension.dxf.defpoint if kind == 'radius' else (dimension.dxf.defpoint + dimension.dxf.defpoint4) / 2
    assert list(center) == pytest.approx([30, 40, 0])


def test_associated_angular_dimension_and_source_deletion_policy(drawings):
    created = drawings.create('alice')
    result = drawings.operate('alice', created['id'], 1, [add_line(end=(10, 0)), add_line(end=(0, 10))])
    a, b = [entity['id'] for entity in result['entities']]
    refs = {'center': {'entityId': a, 'point': 'start'}, 'p1': {'entityId': a, 'point': 'end'}, 'p2': {'entityId': b, 'point': 'end'}}
    result = drawings.operate('alice', created['id'], 2, [{'op': 'dimension', 'kind': 'angular', 'base': [7, 7], 'sourceRefs': refs}])
    dimension_id = result['dimensions'][0]['id']
    result = drawings.operate('alice', created['id'], 3, [{'op': 'update', 'id': b, 'changes': {'end': [10, 10]}}])
    assert result['dimensions'][0]['value'] == pytest.approx(45)
    with pytest.raises(DrawingError, match='先删除相关标注'):
        drawings.operate('alice', created['id'], 4, [{'op': 'delete', 'ids': [a]}])
    assert drawings.get('alice', created['id'])['revision'] == 4
    result = drawings.operate('alice', created['id'], 4, [{'op': 'delete', 'ids': [a, dimension_id]}])
    assert result['dimensions'] == []


def test_paper_space_pdf_prints_selected_layout_at_physical_scale(drawings):
    fitz = pytest.importorskip('pymupdf')
    doc = ezdxf.new('R2010')
    doc.units = 4
    doc.modelspace().add_circle((0, 0), 3)
    doc.layouts.get('Layout1').add_circle((30, 40), 7)
    result = drawings.create('alice', '布局图', _write(doc), 'layouts.dxf')
    assert 'Layout1' in result['layouts']
    data = drawings.export('alice', result['id'], 'pdf', layout='Layout1', scale='1:1')[0]
    with fitz.open(stream=data, filetype='pdf') as pdf:
        circles = [drawing for drawing in pdf[0].get_drawings() if drawing['type'] == 's']
        assert len(circles) == 1
        assert circles[0]['rect'].width == pytest.approx(14 / 25.4 * 72, abs=.1)
    with pytest.raises(DrawingError, match='布局不存在'):
        drawings.export('alice', result['id'], 'pdf', layout='不存在')


def test_external_images_are_preserved_but_not_silently_omitted_from_pdf(drawings):
    doc = ezdxf.new('R2010')
    image = doc.add_image_def(filename='/untrusted-local-path/private.png', size_in_pixel=(100, 100))
    doc.modelspace().add_image(image, insert=(0, 0), size_in_units=(10, 10))
    result = drawings.create('alice', '引用图', _write(doc), 'external.dxf')
    assert len(_read(drawings.export('alice', result['id'])[0]).modelspace().query('IMAGE')) == 1
    with pytest.raises(DrawingError, match='外部图片'):
        drawings.export('alice', result['id'], 'pdf')
