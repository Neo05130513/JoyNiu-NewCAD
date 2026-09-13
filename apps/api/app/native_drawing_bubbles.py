"""Stable inspection balloons linked to native dimension handles in DXF XDATA."""
import json

APPID = 'JOYNIU_INSPECTION_BALLOON'


def bubbles(doc):
    result = []
    current = {entity.dxf.handle: entity for entity in doc.modelspace()}
    for entity in doc.modelspace().query('CIRCLE'):
        if not entity.has_xdata(APPID):
            continue
        try:
            data = json.loads(entity.get_xdata(APPID)[0].value)
            valid = (isinstance(data, dict) and data.get('circleId') == entity.dxf.handle
                     and isinstance(data.get('dimensionId'), str)
                     and isinstance(data.get('textId'), str)
                     and isinstance(data.get('leaderId'), str)
                     and type(data.get('number')) is int and 1 <= data['number'] <= 999999
                     and all(data.get(key) not in current or current[data[key]].dxftype() == kind
                             for key, kind in [('dimensionId', 'DIMENSION'), ('textId', 'TEXT'), ('leaderId', 'LINE')]))
            if valid:
                result.append(data)
        except (ValueError, IndexError, TypeError):
            continue
    return result


def _number(value):
    from .native_drawing import DrawingError
    try:
        number = int(str(value))
    except (ValueError, TypeError):
        raise DrawingError('气泡编号须为 1–999999 的整数。') from None
    if not 1 <= number <= 999999:
        raise DrawingError('气泡编号须为 1–999999 的整数。')
    return number


def _members(data):
    return [data[key] for key in ('circleId', 'textId', 'leaderId') if data.get(key)]


def _save(doc, data):
    if APPID not in doc.appids:
        doc.appids.new(APPID)
    doc.entitydb[data['circleId']].set_xdata(APPID, [(1000, json.dumps(data))])


def expand_bubble_selection(doc, ids, *, deleting=False):
    expanded = set(ids)
    for data in bubbles(doc):
        if expanded.intersection(_members(data)) or (deleting and data.get('dimensionId') in expanded):
            expanded.update(_members(data))
    return list(expanded)


def set_bubble(doc, operation):
    from .native_drawing import DrawingError, _attributes, _point, _number as coordinate, _selected
    from ezdxf.enums import TextEntityAlignment
    source = operation.get('dimensionId')
    dimensions = {e.dxf.handle: e for e in doc.modelspace().query('DIMENSION')}
    if source not in dimensions:
        raise DrawingError('气泡必须关联当前图纸中的原生尺寸。')
    existing = next((data for data in bubbles(doc) if data['dimensionId'] == source), None)
    number = _number(operation.get('number', existing['number'] if existing else 1))
    if any(data['number'] == number and data['dimensionId'] != source for data in bubbles(doc)):
        raise DrawingError('气泡编号已使用，请选择其他编号。')
    radius = coordinate(operation.get('radius', 3), positive=True)
    center = _point(operation['position'])
    target = _point(operation.get('target', list(dimensions[source].dxf.defpoint)))
    if existing:
        _selected(doc, _members(existing))
        circle, text = (doc.entitydb[existing[key]] for key in ('circleId', 'textId'))
        circle.dxf.center, circle.dxf.radius = center, radius
        text.dxf.text, text.dxf.height = str(number), radius
        text.set_placement(center, align=TextEntityAlignment.MIDDLE_CENTER)
        if existing.get('leaderId'):
            leader = doc.entitydb[existing['leaderId']]
            leader.dxf.start, leader.dxf.end = target, center
        existing['number'] = number
        _save(doc, existing)
        return
    attrs = _attributes(doc, operation)
    circle = doc.modelspace().add_circle(center, radius, dxfattribs=attrs)
    text = doc.modelspace().add_text(str(number), dxfattribs={**attrs, 'height': radius})
    text.set_placement(center, align=TextEntityAlignment.MIDDLE_CENTER)
    leader = doc.modelspace().add_line(target, center, dxfattribs=attrs)
    _save(doc, {'dimensionId': source, 'number': number, 'circleId': circle.dxf.handle,
                'textId': text.dxf.handle, 'leaderId': leader.dxf.handle})


def sync_bubbles(doc):
    from .native_drawing import DrawingError
    from ezdxf.enums import TextEntityAlignment
    numbers = set()
    current = {e.dxf.handle: e for e in doc.modelspace()}
    for data in bubbles(doc):
        source = current.get(data.get('dimensionId'))
        circle, text = current.get(data['circleId']), current.get(data.get('textId'))
        if source is None or source.dxftype() != 'DIMENSION' or text is None:
            for identity in _members(data):
                if identity in current:
                    doc.modelspace().delete_entity(current[identity])
            continue
        number = _number(text.dxf.text)
        if number in numbers:
            raise DrawingError('气泡编号重复，未保存本次变更。')
        numbers.add(number)
        data['number'] = number
        text.set_placement(circle.dxf.center, align=TextEntityAlignment.MIDDLE_CENTER)
        text.dxf.height = circle.dxf.radius
        if data.get('leaderId') in current:
            leader = current[data['leaderId']]
            leader.dxf.start, leader.dxf.end = source.dxf.defpoint, circle.dxf.center
        _save(doc, data)
