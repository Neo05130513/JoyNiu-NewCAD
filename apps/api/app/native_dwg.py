"""Verified native DWG writing, including ordinary dimensions and paper layouts.

LibreDWG 0.14's DXF parser loses several numeric fields. A separate bounded CLI
restores those fields from the original DXF before the final DWG is read back.
The adapter never replaces DIMENSION/INSERT objects with flattened geometry.
"""
from functools import lru_cache
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

from .dwg_preprocessor import DWGConverterCommand, DWGPreprocessConfig, _run_converter

# All geometry-affecting DXF attributes of these types are compared, together
# with variable vertex arrays, block attributes, tables and paper layouts.
VERIFIED_TYPES = {'LINE', 'CIRCLE', 'ARC', 'LWPOLYLINE', 'TEXT', 'MTEXT',
                  'DIMENSION', 'INSERT', 'SOLID', 'TRACE', '3DFACE', 'POINT',
                  'ELLIPSE', 'VIEWPORT'}
VIEWPORT_APPID = 'JOYNIU_NATIVE_VIEWPORT_STATE'
_ADAPTER_DIRECTORIES = []


def repair_libredwg_dxf(source, converted, directory, config):
    """Recover values omitted by LibreDWG's DXF emitter from its actual DWG.

    This reader uses no original DXF or expected geometry. Zero dimension-style
    values and anonymous-block flags exist in the DWG but the CLI's DXF output
    omits them, causing ezdxf to substitute different defaults.
    """
    from .native_drawing import DrawingError, _read, _write
    reader = shutil.which('dwgread')
    if not reader:
        raise DrawingError('DWG 完整回读需要 dwgread，以核对原标注样式和匿名块定义。')
    target = Path(directory) / 'actual-dwg.json'
    command = DWGConverterCommand('libredwg-actual-dwg', (reader, '-O', 'JSON', '-o', '{output_dxf}', '{input_dwg}'))
    output, error = _run_converter(command, source, target, config)
    if error:
        raise DrawingError('DWG 标注样式回读失败，未将可能缺少样式的转换结果作为完整图纸。')
    try:
        actual = json.loads(output.read_text())
        doc = _read(converted)
        for layout in doc.layouts:
            for viewport in layout.query('VIEWPORT'):
                if viewport.has_xdata(VIEWPORT_APPID):
                    state = viewport.get_xdata(VIEWPORT_APPID)
                    if len(state) == 2 and all(tag.code == 1071 for tag in state):
                        viewport.dxf.id, viewport.dxf.status = (tag.value for tag in state)
        for record in actual.get('OBJECTS', []):
            if record.get('entity') == 'TEXT' and (record.get('horiz_alignment') or record.get('vert_alignment')):
                handle = record.get('handle', [])
                entity = doc.entitydb.get(format(handle[2], 'X')) if len(handle) >= 3 else None
                # DWG text dataflag bit 2 means alignment == insertion, not
                # the origin. LibreDWG's DXF emitter loses that implicit point.
                point = record.get('ins_pt') if record.get('dataflags', 0) & 2 else record.get('alignment_pt')
                if entity is not None and entity.dxftype() == 'TEXT' and isinstance(point, list) and len(point) == 2:
                    entity.dxf.align_point = (*point, record.get('elevation', 0))
            elif record.get('object') == 'DIMSTYLE' and record.get('name') in doc.dimstyles:
                style = doc.dimstyles.get(record['name'])
                for field in ('DIMCEN', 'DIMTOFL', 'DIMTAD', 'DIMATFIT'):
                    value = record.get(field)
                    if isinstance(value, (float, int)) and math.isfinite(value):
                        style.dxf.set(field.lower(), value)
            elif record.get('object') == 'BLOCK_HEADER' and record.get('name') in doc.blocks:
                block = doc.blocks[record['name']]
                flags = block.block.dxf.flags & ~15
                flags |= int(bool(record.get('anonymous'))) | (int(bool(record.get('hasattrs'))) << 1)
                flags |= (int(bool(record.get('blkisxref'))) << 2) | (int(bool(record.get('xrefoverlaid'))) << 3)
                block.block.dxf.flags = flags
                if isinstance(record.get('base_pt'), list) and len(record['base_pt']) == 3:
                    block.base_point = record['base_pt']
        return _write(doc)
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise DrawingError('DWG 回读的实体结构或编码无效。') from exc


@lru_cache(maxsize=4)
def _adapter(writer):
    from .native_drawing import DrawingError
    installed = shutil.which('joyniu-native-dwg-adapter')
    if installed:
        return installed
    prefix = Path(writer).resolve().parent.parent
    compiler = shutil.which('cc')
    if not compiler or not (prefix / 'include/dwg.h').is_file():
        raise DrawingError('此 DWG 含标注或块，需要 LibreDWG 兼容适配器；服务端缺少 cc 或 libredwg 开发头文件。请安装后重试，DXF 完整导出仍可用。')
    directory = tempfile.TemporaryDirectory(prefix='joyniu-libredwg-adapter-')
    executable = Path(directory.name) / 'native-dwg-adapter'
    source = Path(__file__).with_name('native_drawing_libredwg.c')
    try:
        result = subprocess.run([compiler, '-O2', '-I' + str(prefix / 'include'), str(source),
                                 '-L' + str(prefix / 'lib'), '-lredwg', '-o', str(executable)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=30, check=False)
        if result.returncode:
            raise DrawingError('LibreDWG 兼容适配器编译失败；此服务当前不能可靠写入含标注的 DWG，请使用完整 DXF。')
    except (OSError, subprocess.TimeoutExpired) as exc:
        directory.cleanup()
        raise DrawingError('LibreDWG 兼容适配器未能启动。') from exc
    _ADAPTER_DIRECTORIES.append(directory)
    return str(executable)


def _normal(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 7)
    if isinstance(value, dict):
        return {key: _normal(item) for key, item in value.items()}
    if value is not None and hasattr(value, '__iter__'):
        return [_normal(item) for item in value]
    return value


def _attributes(entity):
    kind = entity.dxftype()
    ignored = {'handle', 'owner'}
    if kind == 'DIMENSION':
        # This is a cached value, actual measurements are independently checked.
        ignored |= {'actual_measurement', 'version'}
    if kind == 'MTEXT':
        ignored |= {'rotation', 'text_direction'}  # Equivalent DXF representations.
    if kind == 'DIMSTYLE':
        # Arc-length and jogged-radius dimensions are outside VERIFIED_TYPES;
        # this arc-length symbol switch has no effect on ordinary dimensions.
        ignored.add('dimarcsym')
    result = {}
    for name, definition in entity.DXFATTRIBS._attribs.items():
        if name in ignored:
            continue
        if kind == 'DIMSTYLE' and name.endswith('_handle'):
            continue  # ezdxf resolves these caches to the separately compared named style/block fields.
        try:
            value = entity.dxf.get(name, definition.default)
        except (AttributeError, ValueError):
            continue
        if name == 'linetype' and isinstance(value, str):
            value = value.upper()
        if name.endswith('_handle') and value in (None, '0'):
            value = None
        if value is None and name in ('width', 'leader_length'):
            value = 0
        if name == 'line_spacing_factor' and value is None:
            value = 1
        if kind == 'TEXT' and name == 'align_point' and not entity.dxf.get('halign', 0) and not entity.dxf.get('valign', 0):
            continue  # Unused for a left/baseline-aligned text.
        result[name] = value
    if kind == 'MTEXT':
        result['rotation'] = entity.get_rotation()
        result['content'] = entity.text
    if kind == 'LWPOLYLINE':
        result['vertices'] = list(entity.get_points('xyseb'))
    if kind == 'INSERT':
        result['attributes'] = [_entity_signature(item) for item in entity.attribs]
    if kind == 'DIMENSION':
        result['measurement'] = entity.get_measurement()
    if entity.xdata:
        result['xdata'] = {name: [(tag.code, tag.value) for tag in tags] for name, tags in entity.xdata.data.items()}
        if kind == 'VIEWPORT':
            result['xdata'].pop(VIEWPORT_APPID, None)
            if not result['xdata']:
                result.pop('xdata')
        if kind == 'STYLE':
            # LibreDWG adds this exact font-file-derived family cache. All font
            # file, style and user-provided family data are still compared.
            inferred = [(1001, 'ACAD'), (1000, Path(entity.dxf.get('font', '')).stem), (1071, 34)]
            if result.get('xdata', {}).get('ACAD') == inferred:
                result['xdata'].pop('ACAD')
            if not result.get('xdata'):
                result.pop('xdata')
    if entity.has_extension_dict:
        # Custom dictionaries may carry associativity or proprietary behavior;
        # they cannot be discarded by an old-version conversion.
        result['extensionDictionary'] = entity.extension_dict.handle
    return _normal(result)


def _entity_signature(entity):
    return {'type': entity.dxftype(), 'attributes': _attributes(entity)}


def _drawing_signature(doc):
    from .native_drawing import DrawingError
    blocks = {}
    for block in doc.blocks:
        for entity in block:
            if entity.dxftype() not in VERIFIED_TYPES:
                raise DrawingError(f'DWG 回写暂不能核验 {entity.dxftype()} 实体（块/布局 {block.name}）；原定义仍完整保存在 DXF。')
            if entity.has_extension_dict:
                raise DrawingError(f'{entity.dxftype()} 含扩展字典/专有关系，目前 DWG 写入器不能保证保留；请导出完整 DXF。')
            if entity.dxf.get('layer', '0') not in doc.layers:
                raise DrawingError(f'原图引用了未定义图层 {entity.dxf.layer}，请先建立该图层再导出 DWG；DXF 原定义可直接保存。')
        blocks[block.name] = {'base': list(block.base_point), 'flags': block.block.dxf.flags,
                             'entities': [_entity_signature(entity) for entity in block]}
    tables = {}
    for name in ('layers', 'styles', 'dimstyles', 'linetypes'):
        tables[name] = {item.dxf.name: _attributes(item) for item in getattr(doc, name)}
        if name == 'linetypes':
            for item in doc.linetypes:
                tables[name][item.dxf.name]['pattern'] = _normal([(tag.code, tag.value) for tag in item.pattern_tags.tags])
    return _normal({'units': doc.units, 'blocks': blocks, 'tables': tables,
                    'layouts': {item.name: _attributes(item.dxf_layout) for item in doc.layouts}})


def _check_object_scope(doc):
    from .native_drawing import DrawingError
    ordinary_roots = {'ACAD_COLOR', 'ACAD_GROUP', 'ACAD_LAYOUT', 'ACAD_MATERIAL',
                      'ACAD_MLEADERSTYLE', 'ACAD_MLINESTYLE', 'ACAD_PLOTSETTINGS',
                      'ACAD_PLOTSTYLENAME', 'ACAD_SCALELIST', 'ACAD_TABLESTYLE',
                      'ACAD_VISUALSTYLE', 'EZDXF_META'}
    unknown = set(doc.rootdict.keys()) - ordinary_roots
    if unknown:
        raise DrawingError(f'DWG 含尚不能核验的自定义对象字典 {sorted(unknown)[0]}；为保留其数据，请导出完整 DXF。')
    ordinary_objects = {'DICTIONARY', 'ACDBDICTIONARYWDFLT', 'ACDBPLACEHOLDER',
                        'LAYOUT', 'MATERIAL', 'MLINESTYLE', 'MLEADERSTYLE',
                        'VISUALSTYLE', 'DICTIONARYVAR'}
    for item in doc.objects:
        kind = item.dxftype()
        if kind not in ordinary_objects:
            raise DrawingError(f'DWG 回写尚不能核验 {kind} 对象；原定义仍完整保存在 DXF。')
        if kind == 'MATERIAL' and item.dxf.get('name', '') not in ('ByLayer', 'ByBlock', 'Global'):
            raise DrawingError('DWG 含自定义材质，R2000 写入器不能保证保留；请导出完整 DXF。')
        if kind in ('MLINESTYLE', 'MLEADERSTYLE') and item.dxf.get('name', '') != 'Standard':
            raise DrawingError(f'DWG 含自定义 {kind}，当前不能保证保留；请导出完整 DXF。')


def _prepare(doc):
    from .native_drawing import DrawingError, _read, _write
    prepared = _read(_write(doc))
    prepared.dxfversion = 'AC1015'
    prepared.encoding = 'gbk'
    prepared.header['$DWGCODEPAGE'] = 'ANSI_936'
    patches = []
    for entity in list(prepared.entitydb.values()):
        kind = entity.dxftype()
        source = doc.entitydb.get(entity.dxf.handle)
        if not source:
            continue
        if kind == 'MTEXT':
            # DXF group 11 is the exact equivalent supported by this parser.
            if entity.dxf.hasattr('rotation') and not entity.dxf.hasattr('text_direction'):
                angle = math.radians(entity.dxf.rotation)
                entity.dxf.text_direction = (math.cos(angle), math.sin(angle), 0)
            entity.dxf.discard('rotation')
            patches.append(f'MTEXT {entity.dxf.handle} {source.dxf.char_height} {source.dxf.get("width", 0)} {source.dxf.get("line_spacing_style", 1)} {source.dxf.get("line_spacing_factor", 1)} {source.dxf.get("flow_direction", 1)}')
        elif kind == 'TEXT' and (source.dxf.get('halign', 0) or source.dxf.get('valign', 0)):
            alignment = source.dxf.get('align_point', source.dxf.insert)
            patches.append(f'TEXTALIGN {entity.dxf.handle} {alignment.x} {alignment.y} {source.dxf.get("halign", 0)} {source.dxf.get("valign", 0)} 0')
        elif kind == 'INSERT':
            patches.append(f'INSERT {entity.dxf.handle} {source.dxf.get("xscale", 1)} {source.dxf.get("yscale", 1)} {source.dxf.get("zscale", 1)} 0 0')
        elif kind == 'LWPOLYLINE':
            widths = source.get_points('se')
            if any(start or end for start, end in widths):
                for index, (start, end) in enumerate(widths):
                    patches.append(f'LWWIDTH {entity.dxf.handle} {index} {start} {end} 0 0')
        elif kind == 'VIEWPORT':
            # IDs and activation ranks are DXF-only: DWG emitters regenerate
            # them globally, which misidentifies main viewports across layouts.
            # Carry the original DXF state as ordinary persistent application
            # XDATA; layouts, view geometry and native viewport objects remain.
            if VIEWPORT_APPID not in prepared.appids:
                prepared.appids.new(VIEWPORT_APPID)
            entity.set_xdata(VIEWPORT_APPID, [(1071, source.dxf.get('id', 0)),
                                             (1071, source.dxf.get('status', 0))])
        elif kind == 'DIMENSION':
            patches.append(f'DIMENSION {entity.dxf.handle} {source.dxf.get("line_spacing_style", 1)} {source.dxf.get("line_spacing_factor", 1)} {math.radians(source.dxf.get("angle", 0))} 0 0')
            if entity.dimtype == 5 and entity.dxf.get('defpoint5', (0, 0, 0)) != (0, 0, 0):
                raise DrawingError('三点角度标注含非零旧版第 5 定义点，当前 DWG 写入器不能保留该数据；请导出 DXF。')
    for style in doc.dimstyles:
        patches.append(f'DIMSTYLE {style.dxf.handle} {style.dxf.get("dimcen", 0.09)} {style.dxf.get("dimtofl", 0)} {style.dxf.get("dimtad", 0)} {style.dxf.get("dimatfit", 3)} 0')
    for block in doc.blocks:
        x, y, z = block.base_point
        patches.append(f'BLOCK {block.block_record.dxf.handle} {block.block.dxf.flags} {x} {y} {z} 0')
    for block in prepared.blocks:
        for entity in block:
            if entity.xdata and len(entity.xdata.data) > 1:
                groups = entity.xdata.data
                total = sum(len(tags) - 1 for tags in groups.values())
                offset = 0
                for appid, tags in groups.items():
                    app_handle = int(prepared.appids.get(appid).dxf.handle, 16)
                    patches.append(f'EEDGROUP {entity.dxf.handle} {offset} {app_handle} {total} 0 0')
                    offset += len(tags) - 1
    # ezdxf writes an unused, zero group 16 on ANG3PT; LibreDWG 0.14 aborts
    # on this pre-R13-only field. Its three actual vertices are 13, 14 and 15.
    lines = _write(prepared).decode('gbk').splitlines()
    encoded = []
    kind = handle = ''
    for code, value in zip(lines[::2], lines[1::2]):
        number = int(code)
        if number == 0:
            kind, handle = value, ''
        if number == 5:
            handle = value
        if (kind == 'DIMENSION' and handle in prepared.entitydb
                and prepared.entitydb[handle].dimtype == 5 and number in (16, 26, 36)):
            continue
        encoded.extend((code, value))
    return ('\n'.join(encoded) + '\n').encode('gbk'), '\n'.join(patches) + '\n' if patches else ''


def _first_difference(a, b, path='图纸'):
    if type(a) is dict and type(b) is dict:
        for key in a.keys() | b.keys():
            if key not in a or key not in b:
                return f'{path}/{key}'
            result = _first_difference(a[key], b[key], f'{path}/{key}')
            if result:
                return result
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f'{path}/数量'
        for index, (left, right) in enumerate(zip(a, b)):
            result = _first_difference(left, right, f'{path}/{index}')
            if result:
                return result
    elif a != b:
        return path
    return ''


def export_verified_dwg(doc):
    from .native_drawing import DrawingError, _read, _convert_dwg
    writer = shutil.which('dxf2dwg')
    if not writer:
        raise DrawingError('当前服务没有可用 DWG 写入器；可导出真实 DXF，不能将 DXF 改后缀冒充 DWG。')
    _check_object_scope(doc)
    baseline = _drawing_signature(doc)
    source_data, corrections = _prepare(doc)
    with tempfile.TemporaryDirectory(prefix='joyniu-native-write-dwg-') as directory:
        source, output = Path(directory) / 'source.dxf', Path(directory) / 'result.dwg'
        source.write_bytes(source_data)
        command = DWGConverterCommand('dxf2dwg', (writer, '--as', 'r2000', '-o', '{output_dxf}', '{input_dwg}'))
        result, error = _run_converter(command, source, output, DWGPreprocessConfig())
        if error:
            raise DrawingError('DWG 写入器未能转换此图纸；原定义仍完整保存在 DXF。')
        if corrections:
            patch = Path(directory) / 'numeric-fields.txt'
            patch.write_text(corrections, encoding='ascii')
            corrected = Path(directory) / 'corrected.dwg'
            command = DWGConverterCommand('libredwg-numeric-compatibility', (_adapter(writer), '{input_dwg}', str(patch), '{output_dxf}'))
            result, error = _run_converter(command, output, corrected, DWGPreprocessConfig())
            if error:
                raise DrawingError('DWG 数值兼容适配失败；已阻止交付，可下载完整 DXF。')
        payload = result.read_bytes()
        if not payload.startswith(b'AC10'):
            raise DrawingError('DWG 写入结果不是有效 DWG 容器。')
        reread = _read(_convert_dwg(payload))
        difference = _first_difference(baseline, _drawing_signature(reread))
        if difference:
            raise DrawingError(f'DWG 回读核对发现变化（{difference}），已阻止交付；原定义仍保存在 DXF。')
        return payload
