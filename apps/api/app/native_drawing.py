"""Account-owned DXF documents. Edits retain the complete ezdxf document.

JSON is an editing projection, never the serialization source. Unsupported
entities, block definitions, layouts and drawing tables stay in every DXF
snapshot. DWG output is served only after a real conversion and read-back.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import ast
import hashlib
import io
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from xml.sax.saxutils import escape
from xml.etree import ElementTree

try:
    import ezdxf
    from ezdxf.math import Matrix44, Vec3
except ImportError:  # The rest of the API can run without the dwg extra.
    ezdxf = None
    Matrix44 = Vec3 = None

from .dwg_preprocessor import (DWGConverterCommand, DWGPreprocessConfig,
                               _resolve_converter_commands, _run_converter,
                               _source_metadata)

MAX_UPLOAD = 20 * 1024 * 1024
MAX_ENTITIES = 50_000
MAX_OPERATIONS = 500
MAX_RENDER_ENTITIES = 50_000
EDITABLE = frozenset({'LINE', 'CIRCLE', 'ARC', 'LWPOLYLINE', 'TEXT', 'MTEXT', 'DIMENSION'})
LINEWEIGHTS = frozenset({-3, -2, -1, 0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211})
DIMENSION_REF_APPID = 'JOYNIU_NATIVE_DIM_REFS'


class DrawingError(ValueError):
    status = 422
    code = 'invalid_drawing'


class DrawingNotFound(DrawingError):
    status = 404
    code = 'drawing_not_found'


class DrawingConflict(DrawingError):
    status = 409
    code = 'drawing_revision_conflict'

    def __init__(self, revision):
        super().__init__('图纸已被其他窗口修改，请重新加载后继续。')
        self.revision = revision


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require_ezdxf():
    if ezdxf is None:
        raise DrawingError('当前服务未安装 ezdxf 图纸依赖，请启用 dwg 扩展后使用。')


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e9 or (positive and value <= 0):
        raise DrawingError('坐标及尺寸必须为有效有限数值，正尺寸须大于零。')
    return float(value)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise DrawingError('坐标须为 [x, y] 或 [x, y, z]。')
    return [_number(item) for item in value] + ([0.0] if len(value) == 2 else [])


def _text(value, maximum=4096):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise DrawingError('文字内容无效或超过长度上限。')
    return value


def _json(value, maximum=65536):
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DrawingError('属性必须是有效 JSON。') from exc
    if len(text.encode()) > maximum:
        raise DrawingError('属性超过大小上限。')
    return text


def _read(payload):
    _require_ezdxf()
    # readfile handles legacy codepages and binary DXF without lossy decoding.
    with tempfile.TemporaryDirectory(prefix='joyniu-native-read-') as directory:
        path = Path(directory) / 'source.dxf'
        path.write_bytes(payload)
        try:
            doc = ezdxf.readfile(path)
        except Exception as exc:
            raise DrawingError('DXF 无法完整解析；未使用会丢弃实体的自动恢复。') from exc
    if len(doc.entitydb) > MAX_ENTITIES:
        raise DrawingError(f'图纸超过 {MAX_ENTITIES} 个实体的编辑上限。')
    return doc


def _write(doc):
    stream = io.StringIO()
    doc.write(stream)
    data = stream.getvalue().encode(doc.output_encoding, errors='dxfreplace')
    if len(data) > 64 * 1024 * 1024:
        raise DrawingError('保存后的图纸超过大小上限。')
    return data


def _convert_dwg(payload):
    _source_metadata(payload, 'source.dwg')
    config = DWGPreprocessConfig(max_input_bytes=MAX_UPLOAD)
    with tempfile.TemporaryDirectory(prefix='joyniu-native-dwg-') as directory:
        source, target = Path(directory) / 'source.dwg', Path(directory) / 'result.dxf'
        source.write_bytes(payload)
        for command in _resolve_converter_commands(config):
            converted, error = _run_converter(command, source, target, config)
            if not error:
                if command.name.startswith('libredwg-'):
                    from .native_dwg import repair_libredwg_dxf
                    return repair_libredwg_dxf(source, converted.read_bytes(), directory, config)
                return converted.read_bytes()
    raise DrawingError('DWG 转换未成功，原文件未修改；请提供 DXF 文件。')


def capabilities():
    try:
        reader = bool(_resolve_converter_commands(DWGPreprocessConfig()))
    except Exception:
        reader = False
    return {'editableTypes': sorted(EDITABLE), 'dwgImport': reader,
            'dwgExport': bool(shutil.which('dxf2dwg')), 'dwgExportValidationRequired': True,
            'maxUploadBytes': MAX_UPLOAD, 'maxEntities': MAX_ENTITIES,
            'dxfAvailable': ezdxf is not None, 'pdfAvailable': importlib.util.find_spec('pymupdf') is not None,
            'exports': ['dxf', 'dwg', 'pdf', 'xlsx', 'json'],
            'scope': '模型空间实体编辑；其他布局、块定义和不支持实体保留在 DXF。DWG 导出须通过转换回读核验。'}


def _dimension(entity):
    try:
        measured = entity.get_measurement()
        value = float(measured) if isinstance(measured, (int, float)) and math.isfinite(measured) else None
    except Exception:
        value = None
    override = entity.override()
    kind = {0: 'linear', 1: 'aligned', 2: 'angular', 3: 'diameter', 4: 'radius', 5: 'angular', 6: 'ordinate'}.get(entity.dimtype, 'other')
    factor = float(override.get('dimlfac', 1)) if kind != 'angular' else 1.0
    return {'id': entity.dxf.handle, 'kind': kind, 'value': value,
            'measurementScale': factor, 'displayValue': value * abs(factor) if value is not None else None,
            'text': entity.dxf.get('text', '<>'),
            'toleranceUpper': float(override.get('dimtp', 0)),
            'toleranceLower': float(override.get('dimtm', 0)),
            'toleranceEnabled': bool(override.get('dimtol', 0)),
            'style': {key: override.get(key, default) for key, default in [('dimtxt', 2.5), ('dimasz', 2.5), ('dimdec', 2), ('dimtdec', 2)]},
            'layer': entity.dxf.layer, 'dimensionType': entity.dimtype,
            'points': {key: list(entity.dxf.get(key)) for key in ('defpoint', 'defpoint2', 'defpoint3', 'defpoint4', 'defpoint5') if entity.dxf.hasattr(key)}}


def _entity(entity):
    kind = entity.dxftype()
    data = {'id': entity.dxf.handle, 'type': kind, 'layer': entity.dxf.get('layer', '0'),
            'color': entity.dxf.get('color', 256), 'editable': kind in EDITABLE,
            'lineweight': entity.dxf.get('lineweight', -1), 'linetype': entity.dxf.get('linetype', 'BYLAYER')}
    if entity.dxf.hasattr('true_color'):
        data['trueColor'] = entity.dxf.true_color
    if kind == 'LINE':
        data.update(start=list(entity.dxf.start), end=list(entity.dxf.end))
    elif kind in ('CIRCLE', 'ARC'):
        data.update(center=list(entity.dxf.center), radius=entity.dxf.radius)
        if kind == 'ARC':
            data.update(startAngle=entity.dxf.start_angle, endAngle=entity.dxf.end_angle)
    elif kind == 'LWPOLYLINE':
        data.update(points=[list(point) for point in entity.get_points('xyb')], closed=entity.closed)
    elif kind in ('TEXT', 'MTEXT'):
        data.update(text=entity.dxf.text if kind == 'TEXT' else entity.text,
                    position=list(entity.dxf.insert), height=entity.dxf.get('height' if kind == 'TEXT' else 'char_height', 2.5),
                    rotation=entity.get_rotation() if kind == 'MTEXT' else entity.dxf.get('rotation', 0))
    elif kind == 'DIMENSION':
        data.update(_dimension(entity))
    elif kind == 'INSERT':
        data.update(block=entity.dxf.name, position=list(entity.dxf.insert), rotation=entity.dxf.get('rotation', 0))
    elif kind in ('SOLID', 'TRACE', '3DFACE'):
        data['vertices'] = [list(entity.dxf.get(f'vtx{index}')) for index in range(4)]
    return data


def _render_entities(doc):
    """Flatten display geometry only; originals remain the editing authority."""
    output = []
    truncated = False
    def visit(entity, parent, depth=0):
        nonlocal truncated
        if depth > 8 or len(output) >= MAX_RENDER_ENTITIES:
            truncated = True
            return
        kind = entity.dxftype()
        if kind in ('DIMENSION', 'INSERT', 'LWPOLYLINE', 'POLYLINE'):
            try:
                for child in entity.virtual_entities():
                    visit(child, parent, depth + 1)
                    if len(output) >= MAX_RENDER_ENTITIES:
                        truncated = True
                        break
            except (ezdxf.DXFError, ValueError, TypeError, AttributeError):
                truncated = True
            return
        if kind in ('SOLID', 'TRACE', '3DFACE'):
            vertices = [list(entity.dxf.get(f'vtx{index}')) for index in (0, 1, 3, 2)]
            data = {'type': 'LWPOLYLINE', 'points': [point[:2] + [0] for point in vertices], 'closed': True, 'filled': True,
                    'layer': entity.dxf.get('layer', '0'), 'color': entity.dxf.get('color', 256)}
        elif kind in ('LINE', 'ARC', 'CIRCLE', 'TEXT', 'MTEXT'):
            data = _entity(entity)
            if kind == 'TEXT':
                horizontal, vertical = entity.dxf.get('halign', 0), entity.dxf.get('valign', 0)
                if (horizontal or vertical) and entity.dxf.hasattr('align_point'):
                    data['position'] = list(entity.dxf.align_point)
                data['anchor'] = 'middle' if horizontal in (1, 4) else 'end' if horizontal == 2 else 'start'
                data['verticalAnchor'] = {1: 'bottom', 2: 'middle', 3: 'top'}.get(vertical, 'baseline')
            if kind == 'MTEXT':
                data['text'] = entity.plain_text()
                # Retain native stacked tolerances as two visual rows rather
                # than exposing the MTEXT caret separator in the editor.
                tolerance = re.fullmatch(r'(.*?)([+-]\d+(?:\.\d+)?)\^([+-]\d+(?:\.\d+)?)', data['text'])
                if tolerance and r'\S' in entity.text:
                    data['stackedTolerance'] = {'base': tolerance[1], 'upper': tolerance[2], 'lower': tolerance[3]}
                data['rotation'] = entity.get_rotation()
                point = entity.dxf.get('attachment_point', 1)
                data['anchor'] = {1: 'start', 2: 'middle', 0: 'end'}[point % 3]
                data['verticalAnchor'] = 'top' if point <= 3 else 'middle' if point <= 6 else 'bottom'
        else:
            return
        data.update(id=f'{parent}:{len(output)}', parentId=parent, editable=False)
        output.append(data)
    for entity in doc.modelspace():
        if entity.dxftype() in ('DIMENSION', 'INSERT', 'LWPOLYLINE', 'POLYLINE', 'SOLID', 'TRACE', '3DFACE'):
            visit(entity, entity.dxf.handle)
    return output, truncated


def _snapshot(doc, record, meta):
    from .native_drawing_bubbles import bubbles
    balloons = bubbles(doc)
    entities = [_entity(entity) for entity in doc.modelspace()]
    for entity in entities:
        for balloon in balloons:
            if entity['id'] == balloon['dimensionId']:
                entity['bubbleNumber'] = balloon['number']
            for key in ('circleId', 'textId', 'leaderId'):
                if entity['id'] == balloon.get(key):
                    entity['bubble'] = {**balloon, 'role': key}
                    if key == 'textId':
                        entity.update(position=list(doc.entitydb[balloon['circleId']].dxf.center), anchor='middle', verticalAnchor='middle')
        if entity['type'] == 'DIMENSION':
            binding = meta.get('dimensionAssociations', {}).get(entity['id'], {})
            entity['sourceRefs'] = binding.get('sourceRefs', {})
            if binding.get('kind'):
                entity['kind'] = binding['kind']
    unsupported = dict(Counter(entity['type'] for entity in entities if not entity['editable']))
    display, truncated = _render_entities(doc)
    return {'id': record['id'], 'name': record['name'], 'revision': record['revision'],
            'updatedAt': record['updated_at'], 'units': int(doc.units),
            'source': meta.get('source', {}), 'customProperties': meta.get('customProperties', {}),
            'entities': entities, 'dimensions': [item for item in entities if item['type'] == 'DIMENSION'],
            'renderEntities': display, 'bubbles': balloons,
            'layers': [{'name': layer.dxf.name, 'color': layer.color, 'visible': not layer.is_off(),
                        'locked': layer.is_locked(), 'frozen': layer.is_frozen(),
                        'linetype': layer.dxf.linetype, 'lineweight': layer.dxf.get('lineweight', -3)} for layer in doc.layers],
            'linetypes': [line.dxf.name for line in doc.linetypes],
            'blocks': [{'name': block.name, 'entityCount': len(block)} for block in doc.blocks if not block.name.startswith('*')],
            'layouts': [layout.name for layout in doc.layouts], 'unsupportedTypes': unsupported,
            'warnings': (['部分实体仅保留和预览，未提供直接修改；导出 DXF 保留其原定义。'] if unsupported else []) + (['编辑画布展开预览达到上限或存在无法展开的定义，请使用整图预览核对。'] if truncated else []) + meta.get('warnings', []),
            'canUndo': bool(meta.get('undo')), 'canRedo': bool(meta.get('redo'))}


def _ensure_layer(doc, name):
    name = _text(name, 255)
    if not name or any(character in name for character in '<>/\\":;?*|='):
        raise DrawingError('图层名称无效。')
    if name not in doc.layers:
        doc.layers.new(name)
    return name


def _attributes(doc, source):
    attrs = {'layer': _ensure_layer(doc, source.get('layer', '0'))}
    if 'color' in source:
        color = source['color']
        if type(color) is not int or not 0 <= color <= 256:
            raise DrawingError('颜色须为 0–256 的 ACI 索引。')
        attrs['color'] = color
    if 'lineweight' in source:
        value = source['lineweight']
        if type(value) is not int or value not in LINEWEIGHTS:
            raise DrawingError('线宽须为有效 DXF 百分之一毫米值，或 -1 随图层、-2 随块、-3 默认。')
        attrs['lineweight'] = value
    if 'linetype' in source:
        value = _text(source['linetype'], 255)
        if value not in doc.linetypes:
            raise DrawingError('当前图纸没有此线型定义，请选择已加载的线型。')
        attrs['linetype'] = value
    return attrs


def _poly_points(value):
    if not isinstance(value, list) or not 2 <= len(value) <= 10_000:
        raise DrawingError('多段线须包含 2–10000 个点。')
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) not in (2, 3):
            raise DrawingError('多段线点格式为 [x, y, bulge]。')
        points.append((_number(point[0]), _number(point[1]), _number(point[2]) if len(point) == 3 else 0))
    return points


def _dimension_override(source):
    result = {}
    style = source.get('style', {})
    if not isinstance(style, dict) or set(style) - {'dimtxt', 'dimasz', 'dimdec', 'dimtdec'}:
        raise DrawingError('不支持该标注样式字段。')
    for key, value in style.items():
        value = _number(value, positive=key in ('dimtxt', 'dimasz'))
        if key in ('dimdec', 'dimtdec') and (value != int(value) or not 0 <= value <= 8):
            raise DrawingError('标注小数位须为 0–8。')
        result[key] = value if key in ('dimtxt', 'dimasz') else int(value)
    for key, destination in [('toleranceUpper', 'dimtp'), ('toleranceLower', 'dimtm')]:
        if key in source:
            result[destination] = _number(source[key])
            result['dimtol'] = 1
    if 'toleranceEnabled' in source:
        if type(source['toleranceEnabled']) is not bool:
            raise DrawingError('公差开关须为布尔值。')
        result['dimtol'] = int(source['toleranceEnabled'])
    return result


def _add_dimension(doc, source):
    msp = doc.modelspace()
    # ezdxf's setup styles use a sample dimlfac of 100. Native dimensions must
    # default to the drawing's actual units, not that demonstration scale.
    common = {'text': _text(source.get('text', '<>')), 'override': {'dimtxt': 2.5, 'dimasz': 2.5, 'dimlfac': 1.0, 'dimscale': 1.0, **_dimension_override(source)}, 'dxfattribs': _attributes(doc, source)}
    kind = source.get('kind', 'linear')
    if kind == 'linear':
        dimension = msp.add_linear_dim(base=_point(source['base']), p1=_point(source['p1']), p2=_point(source['p2']), angle=_number(source.get('angle', 0)), **common)
    elif kind == 'aligned':
        dimension = msp.add_aligned_dim(p1=_point(source['p1']), p2=_point(source['p2']), distance=_number(source.get('distance', 10)), **common)
    elif kind in ('radius', 'diameter'):
        dimension = getattr(msp, f'add_{kind}_dim')(center=_point(source['center']), radius=_number(source['radius'], positive=True), angle=_number(source.get('angle', 45)), **common)
    elif kind == 'angular':
        dimension = msp.add_angular_dim_3p(base=_point(source['base']), center=_point(source['center']), p1=_point(source['p1']), p2=_point(source['p2']), **common)
    else:
        raise DrawingError('不支持此标注类型。')
    dimension.render()
    return dimension.dimension


def _dimension_refs(doc, source):
    refs = source.get('sourceRefs', {})
    if not isinstance(refs, dict) or set(refs) - {'p1', 'p2', 'center', 'circle'}:
        raise DrawingError('标注关联格式无效。')
    entities = {entity.dxf.handle: entity for entity in doc.modelspace()}
    resolved = dict(source)
    for key, reference in refs.items():
        if not isinstance(reference, dict) or not isinstance(reference.get('entityId'), str) or reference['entityId'] not in entities:
            raise DrawingError('关联标注的源实体不存在；请先删除相关标注，再删除源实体。')
        entity = entities[reference['entityId']]
        kind = entity.dxftype()
        if key == 'circle':
            if kind not in ('CIRCLE', 'ARC') or source.get('kind') not in ('radius', 'diameter'):
                raise DrawingError('半径/直径标注关联需要圆或圆弧。')
            resolved.update(center=list(entity.dxf.center), radius=float(entity.dxf.radius))
        else:
            point = reference.get('point')
            if point in ('start', 'end') and kind == 'LINE':
                resolved[key] = list(getattr(entity.dxf, point))
            elif point == 'center' and kind in ('CIRCLE', 'ARC'):
                resolved[key] = list(entity.dxf.center)
            else:
                raise DrawingError('标注关联仅支持直线起止点、圆及圆弧中心。')
    return resolved


def _create_dimension(doc, source, metadata):
    source = _dimension_refs(doc, source)
    entity = _add_dimension(doc, source)
    binding = {'kind': source.get('kind', 'linear'), 'sourceRefs': source.get('sourceRefs', {})}
    metadata.setdefault('dimensionAssociations', {})[entity.dxf.handle] = binding
    if DIMENSION_REF_APPID not in doc.appids:
        doc.appids.new(DIMENSION_REF_APPID)
    encoded = json.dumps(binding, ensure_ascii=True)
    entity.set_xdata(DIMENSION_REF_APPID, [(1000, encoded[index:index + 200]) for index in range(0, len(encoded), 200)])
    return entity


def _restore_dimension_associations(doc, metadata):
    for entity in doc.modelspace().query('DIMENSION'):
        if not entity.has_xdata(DIMENSION_REF_APPID):
            continue
        encoded = ''.join(tag.value for tag in entity.get_xdata(DIMENSION_REF_APPID) if tag.code == 1000)
        if len(encoded) > 4096:
            raise DrawingError('图纸中的关联标注记录超过上限。')
        try:
            binding = json.loads(encoded)
        except (ValueError, RecursionError) as exc:
            raise DrawingError('图纸中的关联标注记录无效。') from exc
        if not isinstance(binding, dict) or binding.get('kind') not in ('linear', 'aligned', 'radius', 'diameter', 'angular'):
            raise DrawingError('图纸中的关联标注类型无效。')
        _dimension_refs(doc, binding)
        metadata.setdefault('dimensionAssociations', {})[entity.dxf.handle] = binding


def _rerender_dimension(doc, entity):
    old_block = entity.dxf.get('geometry', '')
    entity.override().render()
    if old_block and old_block != entity.dxf.get('geometry') and old_block.startswith('*D'):
        referenced = any(other.dxftype() == 'DIMENSION' and other.dxf.get('geometry') == old_block for layout in doc.layouts for other in layout)
        if not referenced and old_block in doc.blocks:
            doc.blocks.delete_block(old_block, safe=False)


def _refresh_dimension_associations(doc, metadata):
    bindings = metadata.get('dimensionAssociations', {})
    current = {entity.dxf.handle: entity for entity in doc.modelspace()}
    for identity, binding in list(bindings.items()):
        entity = current.get(identity)
        if entity is None:
            del bindings[identity]
            continue
        refs = binding.get('sourceRefs', {})
        if not refs:
            continue
        kind = binding['kind']
        resolved = _dimension_refs(doc, {'kind': kind, 'sourceRefs': refs})
        before = {key: list(entity.dxf.get(key)) for key in ('defpoint', 'defpoint2', 'defpoint3', 'defpoint4') if entity.dxf.hasattr(key)}
        if kind in ('linear', 'aligned'):
            old1, old2, base = entity.dxf.defpoint2, entity.dxf.defpoint3, entity.dxf.defpoint
            p1, p2 = Vec3(resolved.get('p1', old1)), Vec3(resolved.get('p2', old2))
            entity.dxf.defpoint2, entity.dxf.defpoint3 = p1, p2
            if kind == 'aligned':
                vector, previous = p2 - p1, old2 - old1
                if vector.magnitude < 1e-9 or previous.magnitude < 1e-9:
                    raise DrawingError('关联对齐标注的定义点不能重合。')
                normal = Vec3(-vector.y, vector.x, 0).normalize()
                old_normal = Vec3(-previous.y, previous.x, 0).normalize()
                distance = (base - old1).dot(old_normal)
                entity.dxf.defpoint = p1 + normal * distance
                entity.dxf.angle = math.degrees(math.atan2(vector.y, vector.x))
            else:
                entity.dxf.defpoint = base + p1 - old1
        elif kind in ('radius', 'diameter'):
            if 'circle' not in refs:
                # Center-only references translate an otherwise explicit size.
                old_center = entity.dxf.defpoint if kind == 'radius' else (entity.dxf.defpoint + entity.dxf.defpoint4) / 2
                new_center = Vec3(resolved.get('center', old_center))
                delta = new_center - old_center
                entity.dxf.defpoint += delta
                entity.dxf.defpoint4 += delta
            else:
                center, radius = Vec3(resolved['center']), resolved['radius']
                direction = entity.dxf.defpoint4 - entity.dxf.defpoint if kind == 'radius' else entity.dxf.defpoint - entity.dxf.defpoint4
                if direction.magnitude < 1e-9:
                    raise DrawingError('关联径向标注的方向无效。')
                radial = direction.normalize(radius)
                entity.dxf.defpoint = center if kind == 'radius' else center + radial
                entity.dxf.defpoint4 = center + radial if kind == 'radius' else center - radial
        elif kind == 'angular':
            old_center = entity.dxf.defpoint4
            center = Vec3(resolved.get('center', old_center))
            p1, p2 = Vec3(resolved.get('p1', entity.dxf.defpoint2)), Vec3(resolved.get('p2', entity.dxf.defpoint3))
            if (p1 - center).magnitude < 1e-9 or (p2 - center).magnitude < 1e-9:
                raise DrawingError('关联角度标注的边点不能与顶点重合。')
            entity.dxf.defpoint2, entity.dxf.defpoint3, entity.dxf.defpoint4 = p1, p2, center
            entity.dxf.defpoint += center - old_center
        after = {key: list(entity.dxf.get(key)) for key in before}
        if before != after:
            _rerender_dimension(doc, entity)


def _add(doc, source):
    if not isinstance(source, dict):
        raise DrawingError('实体定义无效。')
    kind = source.get('type', '').upper()
    attrs = _attributes(doc, source)
    msp = doc.modelspace()
    if kind == 'LINE':
        return msp.add_line(_point(source['start']), _point(source['end']), dxfattribs=attrs)
    if kind in ('CIRCLE', 'ARC'):
        kwargs = {'center': _point(source['center']), 'radius': _number(source['radius'], positive=True), 'dxfattribs': attrs}
        if kind == 'ARC':
            kwargs.update(start_angle=_number(source['startAngle']), end_angle=_number(source['endAngle']))
        return getattr(msp, f'add_{kind.lower()}')(**kwargs)
    if kind == 'LWPOLYLINE':
        return msp.add_lwpolyline(_poly_points(source['points']), format='xyb', close=bool(source.get('closed', False)), dxfattribs=attrs)
    if kind in ('TEXT', 'MTEXT'):
        attrs.update(insert=_point(source['position']), rotation=_number(source.get('rotation', 0)))
        attrs['height' if kind == 'TEXT' else 'char_height'] = _number(source.get('height', 2.5), positive=True)
        return getattr(msp, f'add_{kind.lower()}')(_text(source['text']), dxfattribs=attrs)
    if kind == 'DIMENSION':
        return _add_dimension(doc, source)
    raise DrawingError('此实体类型仅支持原样保留，不能通过 JSON 新建。')


def _selected(doc, ids):
    if not isinstance(ids, list) or not ids or len(ids) > MAX_ENTITIES or len(set(ids)) != len(ids):
        raise DrawingError('请选择不重复的实体编号。')
    entities = {entity.dxf.handle: entity for entity in doc.modelspace()}
    if any(not isinstance(identity, str) or identity not in entities for identity in ids):
        raise DrawingError('实体不在当前图纸模型空间中。')
    selected = [entities[identity] for identity in ids]
    for entity in selected:
        if doc.layers.get(entity.dxf.layer).is_locked():
            raise DrawingError('图层已锁定，请先解锁再编辑。')
    return selected


def _update(doc, entity, changes):
    if not isinstance(changes, dict):
        raise DrawingError('变更格式无效。')
    kind = entity.dxftype()
    if kind not in EDITABLE:
        raise DrawingError('此实体只保留原定义，暂不能直接修改。')
    common = {'layer', 'color', 'lineweight', 'linetype'}
    fields = {'LINE': {'start', 'end'}, 'CIRCLE': {'center', 'radius'}, 'ARC': {'center', 'radius', 'startAngle', 'endAngle'},
              'LWPOLYLINE': {'points', 'closed'}, 'TEXT': {'text', 'position', 'height', 'rotation'},
              'MTEXT': {'text', 'position', 'height', 'rotation'},
              'DIMENSION': {'text', 'toleranceUpper', 'toleranceLower', 'toleranceEnabled', 'style'}}
    if set(changes) - common - fields[kind]:
        raise DrawingError('包含不支持修改的实体字段。')
    for key, value in changes.items():
        if key == 'layer':
            entity.dxf.layer = _ensure_layer(doc, value)
        elif key == 'color':
            entity.dxf.color = _attributes(doc, {'color': value})['color']
        elif key in ('lineweight', 'linetype'):
            setattr(entity.dxf, key, _attributes(doc, {key: value})[key])
        elif key in ('start', 'end', 'center', 'position'):
            setattr(entity.dxf, 'insert' if key == 'position' else key, _point(value))
        elif key in ('radius', 'height'):
            setattr(entity.dxf, 'char_height' if key == 'height' and kind == 'MTEXT' else key, _number(value, positive=True))
        elif key in ('rotation', 'startAngle', 'endAngle'):
            setattr(entity.dxf, {'startAngle': 'start_angle', 'endAngle': 'end_angle'}.get(key, key), _number(value))
        elif key == 'text':
            if kind == 'MTEXT':
                entity.text = _text(value)
            else:
                entity.dxf.text = _text(value)
        elif key == 'points':
            points = _poly_points(value)
            old_points = list(entity.get_points('xyseb'))
            if len(points) != len(old_points) and any(point[2] or point[3] for point in old_points):
                raise DrawingError('此多段线有逐段宽度；改变点数前需用支持段宽的编辑器处理，以免丢失原定义。')
            if len(points) == len(old_points):
                entity.set_points([(point[0], point[1], old[2], old[3], point[2]) for point, old in zip(points, old_points)], format='xyseb')
            else:
                entity.set_points(points, format='xyb')
        elif key == 'closed':
            if type(value) is not bool:
                raise DrawingError('闭合状态须为布尔值。')
            entity.closed = value
    if kind == 'DIMENSION':
        # Definition points and measured geometry stay on the original entity.
        overrides = entity.override()
        overrides.update(_dimension_override(changes))
        overrides.commit()
        _rerender_dimension(doc, entity)


def _operation(doc, operation, meta):
    if not isinstance(operation, dict):
        raise DrawingError('操作格式无效。')
    action = operation.get('action', operation.get('op'))
    if action == 'add':
        if operation.get('entity', {}).get('type', '').upper() == 'DIMENSION':
            _create_dimension(doc, operation['entity'], meta)
        else:
            _add(doc, operation['entity'])
    elif action == 'dimension':
        _create_dimension(doc, operation, meta)
    elif action == 'template':
        from .native_drawing_templates import insert_native_template
        insert_native_template(doc, operation, meta)
    elif action == 'update':
        _update(doc, _selected(doc, [operation['id']])[0], operation['changes'])
    elif action == 'delete':
        from .native_drawing_bubbles import expand_bubble_selection
        for entity in _selected(doc, expand_bubble_selection(doc, operation['ids'], deleting=True)):
            if entity.dxftype() not in EDITABLE:
                raise DrawingError('不支持实体只保留原定义，不能删除。')
            doc.modelspace().delete_entity(entity)
    elif action == 'transform':
        from .native_drawing_bubbles import expand_bubble_selection
        selected = _selected(doc, expand_bubble_selection(doc, operation['ids']))
        if any(entity.dxftype() not in EDITABLE for entity in selected):
            raise DrawingError('所选对象包含暂不支持变换的实体。')
        for entity in selected:
            refs = meta.get('dimensionAssociations', {}).get(entity.dxf.handle, {}).get('sourceRefs', {})
            if refs and any(reference['entityId'] not in operation['ids'] for reference in refs.values()):
                raise DrawingError('关联标注不能脱离其源图元单独变换；请选择源图元，或同时选择源图元和标注。')
        origin = _point(operation.get('origin', [0, 0]))
        offset = _point(operation.get('translation', [0, 0]))
        scale = _number(operation.get('scale', 1), positive=True)
        rotation = math.radians(_number(operation.get('rotation', 0)))
        matrix = Matrix44.chain(Matrix44.translate(*[-item for item in origin]), Matrix44.scale(scale), Matrix44.z_rotate(rotation), Matrix44.translate(*[origin[index] + offset[index] for index in range(3)]))
        for entity in selected:
            entity.transform(matrix)
    elif action == 'offset':
        distance = _number(operation['distance'])
        for entity in _selected(doc, operation['ids']):
            kind = entity.dxftype()
            if kind == 'LINE':
                vector = entity.dxf.end - entity.dxf.start
                length = math.hypot(vector.x, vector.y)
                if length <= 1e-12:
                    raise DrawingError('零长度线不能偏移。')
                shift = Vec3(-vector.y / length * distance, vector.x / length * distance, 0)
                copy = entity.copy()
                copy.dxf.start += shift
                copy.dxf.end += shift
            elif kind in ('CIRCLE', 'ARC'):
                copy = entity.copy()
                copy.dxf.radius = _number(entity.dxf.radius + distance, positive=True)
            else:
                raise DrawingError('偏移目前支持直线、圆及圆弧；其他对象保留不变。')
            doc.modelspace().add_entity(copy)
    elif action == 'trim':
        entity = _selected(doc, [operation['id']])[0]
        if entity.dxftype() != 'LINE':
            raise DrawingError('参数裁剪目前只支持直线。')
        start, end = _number(operation['start']), _number(operation['end'])
        if not 0 <= start < end <= 1:
            raise DrawingError('裁剪范围须满足 0 ≤ start < end ≤ 1。')
        original, direction = entity.dxf.start, entity.dxf.end - entity.dxf.start
        entity.dxf.start, entity.dxf.end = original + direction * start, original + direction * end
    elif action == 'layer':
        name = _ensure_layer(doc, operation['name'])
        layer = doc.layers.get(name)
        if 'color' in operation:
            color = operation['color']
            if type(color) is not int or not 1 <= color <= 255:
                raise DrawingError('图层颜色须为 1–255。')
            layer.color = color
        for field in ('lineweight', 'linetype'):
            if field in operation:
                setattr(layer.dxf, field, _attributes(doc, {field: operation[field]})[field])
        for field, methods in [('visible', ('on', 'off')), ('locked', ('lock', 'unlock')), ('frozen', ('freeze', 'thaw'))]:
            if field in operation:
                if type(operation[field]) is not bool:
                    raise DrawingError('图层状态须为布尔值。')
                getattr(layer, methods[0 if operation[field] else 1])()
    elif action == 'bubble':
        if 'dimensionId' in operation:
            from .native_drawing_bubbles import set_bubble
            set_bubble(doc, operation)
            return
        center = _point(operation['position'])
        radius = _number(operation.get('radius', 3), positive=True)
        attrs = _attributes(doc, operation)
        doc.modelspace().add_circle(center, radius, dxfattribs=attrs)
        from ezdxf.enums import TextEntityAlignment
        text = doc.modelspace().add_text(_text(str(operation['number']), 32), dxfattribs={**attrs, 'height': radius})
        text.set_placement(center, align=TextEntityAlignment.MIDDLE_CENTER)
        if 'target' in operation:
            doc.modelspace().add_line(_point(operation['target']), center, dxfattribs=attrs)
    elif action == 'metadata':
        if 'customProperties' in operation:
            if not isinstance(operation['customProperties'], dict):
                raise DrawingError('customProperties 须为 JSON 对象。')
            _json(operation['customProperties'])
            meta['customProperties'] = operation['customProperties']
        if 'name' in operation:
            meta['name'] = _text(operation['name'], 200).strip()
            if not meta['name']:
                raise DrawingError('名称不能为空。')
    else:
        raise DrawingError('不支持此图纸操作。')


def _formula(expression, parameters, stack=()):
    """Evaluate the UI's bounded arithmetic grammar, without eval or exec."""
    if isinstance(expression, (int, float)) and not isinstance(expression, bool):
        return _number(expression)
    if not isinstance(expression, str) or not 0 < len(expression.strip()) <= 256:
        raise DrawingError('约束公式无效或过长。')
    try:
        tree = ast.parse(expression.replace('^', '**').strip(), mode='eval')
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise DrawingError('约束公式语法无效。') from exc
    if sum(1 for _node in ast.walk(tree)) > 200:
        raise DrawingError('约束公式过于复杂。')
    functions = {'sqrt': math.sqrt, 'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
                 'abs': abs, 'min': min, 'max': max}
    def visit(node):
        if isinstance(node, ast.Constant):
            return _number(node.value)
        if isinstance(node, ast.Name):
            if node.id == 'pi':
                return math.pi
            if node.id not in parameters or node.id in stack or len(stack) >= 24:
                raise DrawingError('约束参数不存在或公式循环引用。')
            return _formula(parameters[node.id], parameters, stack + (node.id,))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return _number(left + right)
            if isinstance(node.op, ast.Sub):
                return _number(left - right)
            if isinstance(node.op, ast.Mult):
                return _number(left * right)
            if isinstance(node.op, ast.Div):
                return _number(left / right)
            if isinstance(node.op, ast.Pow):
                return _number(left ** right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions and not node.keywords and 1 <= len(node.args) <= 8:
            values = [visit(argument) for argument in node.args]
            if node.func.id not in ('min', 'max') and len(values) != 1:
                raise DrawingError('约束函数参数数量无效。')
            return _number(functions[node.func.id](values) if node.func.id in ('min', 'max') else functions[node.func.id](*values))
        raise DrawingError('约束公式含不支持的表达式。')
    try:
        return _number(visit(tree.body))
    except (ArithmeticError, TypeError, ValueError) as exc:
        if isinstance(exc, DrawingError):
            raise
        raise DrawingError('约束公式结果无效。') from exc


def _validate_constraints(doc, metadata):
    properties = metadata.get('customProperties', {})
    constraints, parameters = properties.get('constraints', []), properties.get('parameters', {})
    if not isinstance(constraints, list) or len(constraints) > 80 or not isinstance(parameters, dict) or len(parameters) > 100:
        raise DrawingError('约束或参数超过上限，或格式无效。')
    for name, expression in parameters.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_]\w{0,39}', name):
            raise DrawingError('参数名称无效。')
        _formula(expression, parameters)
    entities = {entity.dxf.handle: _entity(entity) for entity in doc.modelspace()}
    pair_types = {'parallel', 'perpendicular', 'coincident', 'equal', 'concentric', 'distance'}
    def length(entity):
        if entity['type'] == 'LINE':
            return math.hypot(entity['end'][0] - entity['start'][0], entity['end'][1] - entity['start'][1])
        return entity['radius']
    def endpoint(entity, which=0):
        return entity['end' if which else 'start'] if entity['type'] == 'LINE' else entity['center']
    for constraint in constraints:
        if not isinstance(constraint, dict):
            raise DrawingError('约束记录无效。')
        kind, ids = constraint.get('type'), constraint.get('entities')
        if not isinstance(ids, list) or len(ids) != (2 if kind in pair_types else 1) or any(not isinstance(identity, str) or identity not in entities for identity in ids):
            raise DrawingError('约束引用的图元不存在或数量不符。')
        selected = [entities[identity] for identity in ids]
        if any(entity['type'] not in ('LINE', 'CIRCLE', 'ARC') for entity in selected):
            raise DrawingError('几何约束支持直线、圆和圆弧。')
        a, b = selected[0], selected[1] if len(selected) > 1 else None
        numeric = _formula(constraint.get('value'), parameters) if kind in ('length', 'radius', 'angle', 'distance') else 0
        if kind in ('horizontal', 'vertical', 'length', 'angle', 'parallel', 'perpendicular') and a['type'] != 'LINE':
            raise DrawingError('此约束需要直线。')
        if kind in ('parallel', 'perpendicular') and b['type'] != 'LINE':
            raise DrawingError('方向关系需要两条直线。')
        if kind in ('angle', 'parallel', 'perpendicular') and (length(a) < 1e-9 or (b and length(b) < 1e-9)):
            raise DrawingError('零长度直线不能确定方向关系。')
        dx, dy = (a['end'][0] - a['start'][0], a['end'][1] - a['start'][1]) if a['type'] == 'LINE' else (0, 0)
        if kind == 'horizontal':
            residual = [dy]
        elif kind == 'vertical':
            residual = [dx]
        elif kind == 'length':
            if numeric <= 0:
                raise DrawingError('约束长度须大于零。')
            residual = [length(a) - numeric]
        elif kind == 'radius':
            if a['type'] not in ('CIRCLE', 'ARC') or numeric <= 0:
                raise DrawingError('半径约束需要正半径的圆或圆弧。')
            residual = [a['radius'] - numeric]
        elif kind == 'angle':
            difference = math.atan2(dy, dx) - math.radians(numeric)
            residual = [math.atan2(math.sin(difference), math.cos(difference))]
        elif kind in ('parallel', 'perpendicular'):
            bx, by = b['end'][0] - b['start'][0], b['end'][1] - b['start'][1]
            residual = [(dx * by - dy * bx if kind == 'parallel' else dx * bx + dy * by) / max(1e-6, length(a) * length(b))]
        elif kind == 'equal':
            if (a['type'] == 'LINE') != (b['type'] == 'LINE'):
                raise DrawingError('等长需要两条直线，等半径需要两个圆或圆弧。')
            residual = [length(a) - length(b)]
        elif kind in ('coincident', 'distance'):
            ends = constraint.get('ends', [0, 0])
            if not isinstance(ends, list) or len(ends) != 2 or any(type(value) is not int or value not in (0, 1) for value in ends):
                raise DrawingError('约束端点选择无效。')
            p, q = endpoint(a, ends[0]), endpoint(b, ends[1])
            residual = [math.hypot(p[0] - q[0], p[1] - q[1]) - numeric] if kind == 'distance' else [p[0] - q[0], p[1] - q[1]]
        elif kind == 'concentric':
            if a['type'] == 'LINE' or b['type'] == 'LINE':
                raise DrawingError('同心约束需要两个圆或圆弧。')
            residual = [a['center'][index] - b['center'][index] for index in (0, 1)]
        elif kind == 'fixed':
            baseline = constraint.get('geometry')
            if not isinstance(baseline, dict) or baseline.get('type') != a['type']:
                raise DrawingError('固定约束必须保存加入时的几何。')
            fields = ('start', 'end') if a['type'] == 'LINE' else ('center',)
            residual = [a[field][index] - _point(baseline.get(field))[index] for field in fields for index in (0, 1)]
            if a['type'] != 'LINE':
                residual.append(a['radius'] - _number(baseline.get('radius'), positive=True))
        else:
            raise DrawingError('未知约束类型。')
        if any(not math.isfinite(value) or abs(value) > 1e-4 for value in residual):
            raise DrawingError('当前几何不满足已保存约束，请先求解或在同次操作中明确解除约束。')


class NativeDrawingStore:
    def __init__(self, database):
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS native_drawing_documents (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, original BLOB, metadata TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS native_drawing_owner ON native_drawing_documents(owner);
                CREATE TABLE IF NOT EXISTS native_drawing_revisions (
                document_id TEXT NOT NULL, revision INTEGER NOT NULL, dxf BLOB NOT NULL, metadata TEXT NOT NULL,
                PRIMARY KEY(document_id, revision));
                CREATE TABLE IF NOT EXISTS native_drawing_requests (
                owner TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, document_id TEXT NOT NULL,
                PRIMARY KEY(owner, request_id));''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _record(self, db, owner, identity):
        row = db.execute('SELECT * FROM native_drawing_documents WHERE id=? AND owner=?', (identity, owner)).fetchone()
        if row is None:
            raise DrawingNotFound('未找到此图纸。')
        return dict(row)

    def _load(self, db, owner, identity):
        record = self._record(db, owner, identity)
        revision = db.execute('SELECT dxf FROM native_drawing_revisions WHERE document_id=? AND revision=?', (identity, record['revision'])).fetchone()
        return record, _read(revision['dxf']), json.loads(record['metadata'])

    def _request_fingerprint(self, request_id, command):
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id):
            raise DrawingError('requestId 格式无效。')
        _json(command, 2 * 1024 * 1024)
        return hashlib.sha256(json.dumps(command, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _replayed(self, db, owner, request_id, fingerprint):
        if request_id is None:
            return None
        row = db.execute('SELECT * FROM native_drawing_requests WHERE owner=? AND request_id=?', (owner, request_id)).fetchone()
        if row is None:
            return None
        if row['fingerprint'] != fingerprint:
            raise DrawingError('该请求标识已用于其他修改，请重新操作。')
        # Return the current revision if later edits exist; a recovered response
        # must never roll the UI back to an old snapshot.
        record, doc, metadata = self._load(db, owner, row['document_id'])
        return _snapshot(doc, record, metadata)

    def _remember_request(self, db, owner, request_id, fingerprint, identity):
        if request_id is not None:
            db.execute('INSERT INTO native_drawing_requests VALUES (?,?,?,?)', (owner, request_id, fingerprint, identity))

    def list(self, owner):
        with self._db() as db:
            rows = db.execute('SELECT id,name,revision,updated_at FROM native_drawing_documents WHERE owner=? ORDER BY updated_at DESC LIMIT 500', (owner,)).fetchall()
        return {'items': [{'id': row['id'], 'name': row['name'], 'revision': row['revision'], 'updatedAt': row['updated_at']} for row in rows]}

    def create(self, owner, name='未命名图纸', payload=None, filename=None, request_id=None):
        _require_ezdxf()
        name = _text(name, 200).strip()
        if not name:
            raise DrawingError('名称不能为空。')
        fingerprint = self._request_fingerprint(request_id, {'create': name, 'filename': filename, 'payload': hashlib.sha256(payload).hexdigest() if isinstance(payload, bytes) else None})
        with self._db() as db:
            replay = self._replayed(db, owner, request_id, fingerprint)
            if replay is not None:
                return replay
        metadata = {'name': name, 'source': {}, 'undo': [], 'redo': []}
        if payload is None:
            doc = ezdxf.new('R2010', setup=True)
            doc.units = 4
        else:
            if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_UPLOAD:
                raise DrawingError('上传文件为空或超过 20 MiB。')
            filename = Path(_text(filename or '', 255)).name
            suffix = Path(filename).suffix.lower()
            if suffix not in ('.dwg', '.dxf'):
                raise DrawingError('仅支持 DWG 和 DXF 文件。')
            data = _convert_dwg(payload) if suffix == '.dwg' else payload
            doc = _read(data)
            _restore_dimension_associations(doc, metadata)
            _refresh_dimension_associations(doc, metadata)
            metadata['source'] = {'filename': filename, 'format': suffix[1:]}
            if suffix == '.dwg':
                metadata['warnings'] = ['DWG 已通过本机转换器导入；原 DWG 二进制另行保留。专有代理对象的转换兼容性需核对原软件。']
        identity, at = f'nd_{uuid.uuid4().hex}', _now()
        record = {'id': identity, 'name': name, 'revision': 1, 'updated_at': at}
        data = _write(doc)
        snapshot = _snapshot(doc, record, metadata)
        _json(snapshot, 32 * 1024 * 1024)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replayed(db, owner, request_id, fingerprint)
            if replay is not None:
                return replay
            db.execute('INSERT INTO native_drawing_documents VALUES (?,?,?,?,?,?,?,?)', (identity, owner, name, 1, at, at, payload, _json(metadata, 200000)))
            db.execute('INSERT INTO native_drawing_revisions VALUES (?,?,?,?)', (identity, 1, data, _json(metadata, 200000)))
            self._remember_request(db, owner, request_id, fingerprint, identity)
        return snapshot

    def get(self, owner, identity):
        with self._db() as db:
            record, doc, metadata = self._load(db, owner, identity)
        return _snapshot(doc, record, metadata)

    def operate(self, owner, identity, expected_revision, operations=None, history=None, request_id=None):
        if type(expected_revision) is not int or expected_revision < 1:
            raise DrawingError('必须提供有效的 expectedRevision。')
        if history not in (None, 'undo', 'redo'):
            raise DrawingError('未知历史操作。')
        if history is None and (not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS):
            raise DrawingError(f'每次须提交 1–{MAX_OPERATIONS} 个操作。')
        fingerprint = self._request_fingerprint(request_id, {'identity': identity, 'revision': expected_revision, 'history': history, 'operations': operations})
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replayed(db, owner, request_id, fingerprint)
            if replay is not None:
                return replay
            record, doc, metadata = self._load(db, owner, identity)
            if expected_revision != record['revision']:
                raise DrawingConflict(record['revision'])
            if history:
                chain = metadata.get(history, [])
                if not chain:
                    raise DrawingError('没有可撤销或重做的记录。')
                target = chain.pop()
                previous = db.execute('SELECT dxf,metadata FROM native_drawing_revisions WHERE document_id=? AND revision=?', (identity, target)).fetchone()
                doc = _read(previous['dxf'])
                restored = json.loads(previous['metadata'])
                metadata[history] = chain
                other = 'redo' if history == 'undo' else 'undo'
                metadata[other] = metadata.get(other, []) + [record['revision']]
                for key in ('customProperties', 'name', 'dimensionAssociations'):
                    if key in restored:
                        metadata[key] = restored[key]
                    else:
                        metadata.pop(key, None)
            else:
                for operation in operations:
                    try:
                        _operation(doc, operation, metadata)
                    except DrawingError:
                        raise
                    except (KeyError, TypeError, ValueError, AttributeError, ezdxf.DXFError) as exc:
                        raise DrawingError('操作参数不完整、实体定义不支持或几何无效。') from exc
                metadata['undo'] = metadata.get('undo', [])[-99:] + [record['revision']]
                metadata['redo'] = []
            if len(doc.entitydb) > MAX_ENTITIES:
                raise DrawingError('编辑结果超过实体数量上限。')
            _refresh_dimension_associations(doc, metadata)
            from .native_drawing_bubbles import sync_bubbles
            sync_bubbles(doc)
            _validate_constraints(doc, metadata)
            data = _write(doc)
            _read(data)  # A saved revision must be reopenable before commit.
            record.update(revision=record['revision'] + 1, updated_at=_now(), name=metadata.get('name', record['name']))
            encoded = _json(metadata, 200000)
            snapshot = _snapshot(doc, record, metadata)
            _json(snapshot, 32 * 1024 * 1024)
            db.execute('INSERT INTO native_drawing_revisions VALUES (?,?,?,?)', (identity, record['revision'], data, encoded))
            db.execute('UPDATE native_drawing_documents SET name=?,revision=?,updated_at=?,metadata=? WHERE id=? AND owner=?', (record['name'], record['revision'], record['updated_at'], encoded, identity, owner))
            self._remember_request(db, owner, request_id, fingerprint, identity)
        return snapshot

    def export(self, owner, identity, format='dxf', *, paper='A4', scale='fit', color='original', landscape=False, layout='Model'):
        with self._db() as db:
            record, doc, metadata = self._load(db, owner, identity)
            if format == 'original':
                if not record['original']:
                    raise DrawingError('此图纸没有上传原文件。')
                return bytes(record['original']), 'application/octet-stream', metadata['source']['filename']
        name = record['name']
        if format == 'dxf':
            return _write(doc), 'application/dxf', f'{name}.dxf'
        if format == 'json':
            return _json(_snapshot(doc, record, metadata), 32 * 1024 * 1024).encode(), 'application/json', f'{name}.json'
        if format == 'xlsx':
            return dimension_xlsx(doc), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', f'{name}-尺寸清单.xlsx'
        if format in ('pdf', 'svg'):
            data = render_document(doc, format, paper=paper, scale=scale, color=color, landscape=landscape, layout_name=layout)
            return data, 'application/pdf' if format == 'pdf' else 'image/svg+xml', f'{name}.{format}'
        if format == 'dwg':
            return export_dwg(doc), 'application/acad', f'{name}.dwg'
        raise DrawingError('不支持此导出格式。')


def dimension_xlsx(doc):
    from .native_drawing_bubbles import bubbles
    balloon_numbers = {item['dimensionId']: item['number'] for item in bubbles(doc)}
    rows = [['实体编号', '标注类型', '几何测量值', '显示文字', '上公差', '下公差', '图层', '单位代码', '标注数值（比例后）', '标注比例', '气泡编号', '尺寸上限', '尺寸下限', '检验编号状态']]
    for entity in doc.modelspace().query('DIMENSION'):
        data = _dimension(entity)
        upper = data['displayValue'] + data['toleranceUpper'] if data['displayValue'] is not None and data['toleranceEnabled'] else ''
        lower = data['displayValue'] - data['toleranceLower'] if data['displayValue'] is not None and data['toleranceEnabled'] else ''
        rows.append([data['id'], data['kind'], data['value'], data['text'], data['toleranceUpper'], data['toleranceLower'], data['layer'], int(doc.units), data['displayValue'], data['measurementScale'], balloon_numbers.get(data['id'], ''), upper, lower, '已编号' if data['id'] in balloon_numbers else '待编号'])
    def cell(value, reference):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{reference}"><v>{value}</v></c>'
        text = str(value) if value is not None else '未取得实测值'
        text = ''.join(character if ord(character) >= 32 or character in '\t\n\r' else f'[U+{ord(character):04X}]' for character in text)
        return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    content = ''.join(f'<row r="{index}">' + ''.join(cell(value, f'{chr(65 + column)}{index}') for column, value in enumerate(row)) + '</row>' for index, row in enumerate(rows, 1))
    files = {
        '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="尺寸清单" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cols><col min="1" max="14" width="20" customWidth="1"/></cols><sheetData>{content}</sheetData><autoFilter ref="A1:N{len(rows)}"/></worksheet>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, value in files.items():
            archive.writestr(path, value.encode())
    return output.getvalue()


def render_document(doc, format='pdf', *, paper='A4', scale='fit', color='original', landscape=False, layout_name='Model'):
    from ezdxf.addons.drawing import Frontend, RenderContext, layout, svg
    from ezdxf.addons.drawing.config import Configuration, ColorPolicy, BackgroundPolicy, ImagePolicy
    papers = {'A0': (841, 1189), 'A1': (594, 841), 'A2': (420, 594), 'A3': (297, 420), 'A4': (210, 297)}
    if paper not in papers or color not in ('original', 'monochrome'):
        raise DrawingError('纸张或颜色策略无效。')
    ratio = 1.0
    if scale != 'fit':
        try:
            numerator, denominator = str(scale).split(':')
            ratio = float(numerator) / float(denominator)
            if not math.isfinite(ratio) or not 0 < ratio <= 1000:
                raise ValueError()
        except (ValueError, ZeroDivisionError):
            raise DrawingError('比例须为 fit 或正比例，如 1:1、1:2。') from None
    # Drawing units -> millimetres for explicit paper scale.
    unit_factors = {0: 1, 1: 25.4, 2: 304.8, 4: 1, 5: 10, 6: 1000}
    if scale != 'fit' and int(doc.units) not in unit_factors:
        raise DrawingError('当前单位尚不支持固定比例输出，请使用适合纸张或先转换单位。')
    width, height = papers[paper]
    if landscape:
        width, height = height, width
    # SVG retains fractional PDF points; ezdxf's direct PDF backend truncates
    # A-series page dimensions to integers, also perturbing a requested 1:1.
    backend = svg.SVGBackend()
    try:
        selected_layout = doc.layouts.get(layout_name)
    except (KeyError, ezdxf.DXFError):
        raise DrawingError('所选打印布局不存在。') from None
    unit_factor = unit_factors.get(int(doc.units), 1) if selected_layout.name == 'Model' else (25.4 if selected_layout.dxf.get('plot_paper_units', 1) == 0 else 1)
    external_types = {'IMAGE', 'PDFUNDERLAY', 'DWFUNDERLAY', 'DGNUNDERLAY', 'OLE2FRAME'}
    inspected_blocks = set()
    def has_external(entities):
        for entity in entities:
            if doc.layers.get(entity.dxf.get('layer', '0')).is_off():
                continue
            if entity.dxftype() in external_types:
                return True
            if entity.dxftype() == 'INSERT' and entity.dxf.name in doc.blocks and entity.dxf.name not in inspected_blocks:
                inspected_blocks.add(entity.dxf.name)
                block = doc.blocks[entity.dxf.name]
                if block.block.dxf.flags & 4 or has_external(block):
                    return True
        return False
    if has_external(selected_layout):
        raise DrawingError('所选布局含外部图片、底图或参照；为避免遗漏，当前不导出不完整预览/PDF。请用原 CAD 连同依赖文件出图，DXF 原定义仍可下载。')
    config = Configuration(color_policy=ColorPolicy.BLACK if color == 'monochrome' else ColorPolicy.COLOR,
                           background_policy=BackgroundPolicy.WHITE, image_policy=ImagePolicy.IGNORE,
                           hatching_timeout=5.0)
    # Raster references are not fetched from arbitrary local paths or network.
    try:
        Frontend(RenderContext(doc), backend, config=config).draw_layout(selected_layout, finalize=True)
        page = layout.Page(width, height, margins=layout.Margins.all(10))
        settings = layout.Settings(fit_page=scale == 'fit', scale=ratio * unit_factor, crop_at_margins=True)
        vector = backend.get_string(page, settings=settings).encode()
        if format == 'pdf':
            import pymupdf
            # MuPDF's SVG converter does not resolve stylesheet classes.
            # Materialize ezdxf's generated classes as presentation attributes
            # so thin lines and text outlines survive as vector paths.
            root = ElementTree.fromstring(vector)
            rules = {}
            for node in root.iter():
                if node.tag.endswith('}style'):
                    for name, properties in re.findall(r'\.([\w-]+)\s*\{([^}]*)\}', node.text or ''):
                        rules[name] = dict(tuple(part.strip() for part in declaration.split(':', 1)) for declaration in properties.split(';') if ':' in declaration)
            for node in root.iter():
                for name in node.attrib.get('class', '').split():
                    node.attrib.update(rules.get(name, {}))
            vector = ElementTree.tostring(root)
            with pymupdf.open(stream=vector, filetype='svg') as drawing:
                return drawing.convert_to_pdf()
        return vector
    except Exception as exc:
        raise DrawingError('图纸渲染未成功；DXF 原实体仍保留，可导出后用 CAD 打开。') from exc


def _geometry_signature(doc, entity_source=None):
    records = []
    for entity in doc.modelspace() if entity_source is None else entity_source:
        data = _entity(entity)
        if 'linetype' in data:
            data['linetype'] = data['linetype'].upper()
        for key in ('id', 'editable'):
            data.pop(key, None)
        if entity.xdata:
            data['xdata'] = {name: [(tag.code, str(tag.value)) for tag in tags] for name, tags in entity.xdata.data.items()}
        def normalized(value):
            if isinstance(value, float):
                return round(value, 7)
            if isinstance(value, list):
                return [normalized(item) for item in value]
            if isinstance(value, dict):
                return {key: normalized(item) for key, item in value.items()}
            return value
        records.append(json.dumps(normalized(data), sort_keys=True, ensure_ascii=False))
    return sorted(records)


def export_dwg(doc):
    from .native_dwg import export_verified_dwg
    return export_verified_dwg(doc)
