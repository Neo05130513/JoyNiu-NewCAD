"""Atomic native parametric-library insertion; existing DXF validation is final."""
import copy
import re
import uuid


def insert_native_template(doc, operation, metadata):
    from .native_drawing import DrawingError, _add, _json, _number, _text

    properties = metadata.setdefault('customProperties', {})
    instances = properties.get('libraryInstances', [])
    if not isinstance(instances, list) or len(instances) > 100:
        raise DrawingError('图库实例数量或格式无效。')
    previous = next((item for item in instances if item.get('id') == operation.get('instanceId')), None)
    if operation.get('instanceId') and previous is None:
        raise DrawingError('待更新的图库实例不存在。')
    template = previous.get('template') if previous else next((item for item in properties.get('library', []) if item.get('id') == operation.get('templateId')), None)
    if not isinstance(template, dict) or template.get('version') != 2 or template.get('id') != operation.get('templateId'):
        raise DrawingError('参数构件不存在或仍是旧版静态构件。')
    sources, constraints, parameters = (operation.get(key) for key in ('entities', 'constraints', 'parameters'))
    if not isinstance(sources, list) or not 1 <= len(sources) <= 40 or not isinstance(constraints, list) or not 1 <= len(constraints) <= 80 or not isinstance(parameters, dict) or not 1 <= len(parameters) <= 100:
        raise DrawingError('参数构件实体、约束或参数数量无效。')
    if set(parameters) != set(template.get('parameters', {})):
        raise DrawingError('参数构件的参数集合不匹配。')
    local_ids = [entity.get('id') for entity in sources if isinstance(entity, dict)]
    if len(local_ids) != len(sources) or any(not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z_]\w{0,39}', identity) for identity in local_ids) or len(set(local_ids)) != len(sources):
        raise DrawingError('参数构件局部图元 ID 无效或重复。')
    for entity in sources:
        if entity.get('type') not in ('LINE', 'CIRCLE', 'ARC', 'LWPOLYLINE', 'TEXT', 'MTEXT'):
            raise DrawingError('参数构件仅支持基础可编辑图元。')
    for constraint in constraints:
        if not isinstance(constraint, dict) or not isinstance(constraint.get('entities'), list) or not constraint['entities'] or any(identity not in local_ids for identity in constraint['entities']):
            raise DrawingError('参数构件不能引用外部图元。')
    translation = operation.get('translation', [0, 0])
    if not isinstance(translation, list) or len(translation) != 2:
        raise DrawingError('参数构件插入位置无效。')
    translation = [_number(value) for value in translation]
    identity = previous['id'] if previous else 'instance_' + uuid.uuid4().hex
    if not re.fullmatch(r'instance_[0-9a-f]{32}', identity):
        raise DrawingError('图库实例标识无效。')
    parameter_map = dict(previous['parameterMap']) if previous else {name: f'p_{identity[9:29]}_{index}' for index, name in enumerate(parameters)}
    if set(parameter_map) != set(parameters) or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_]\w{0,39}', value) for value in parameter_map.values()):
        raise DrawingError('参数构件保存的参数映射无效。')
    for name in parameters:
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_]\w{0,39}', name):
            raise DrawingError('参数构件参数名无效。')

    def expression(value):
        if not isinstance(value, str):
            return value
        return re.sub(r'(?:\d*\.\d+|\d+\.?\d*)(?:[eE][+-]?\d+)?|[A-Za-z_]\w*', lambda match: parameter_map.get(match.group(), match.group()), value)

    current_constraints = properties.get('constraints', [])
    current_parameters = dict(properties.get('parameters', {}))
    if not isinstance(current_constraints, list):
        raise DrawingError('现有约束格式无效。')
    if previous:
        old_ids, old_constraint_ids = set(previous['entityIds']), set(previous['constraintIds'])
        remaining = [constraint for constraint in current_constraints if constraint.get('id') not in old_constraint_ids]
        if any(old_ids.intersection(constraint.get('entities', [])) for constraint in remaining):
            raise DrawingError('该实例关联了外部约束，请先解除后再修改实例参数。')
        for binding in metadata.get('dimensionAssociations', {}).values():
            if any(reference.get('entityId') in old_ids for reference in binding.get('sourceRefs', {}).values()):
                raise DrawingError('该实例关联了尺寸标注，请先删除相关标注后再修改实例参数。')
        native = {entity.dxf.handle: entity for entity in doc.modelspace()}
        if not old_ids.issubset(native):
            raise DrawingError('该实例图元已被删除，请从图库重新插入。')
        for entity_id in old_ids:
            doc.modelspace().delete_entity(native[entity_id])
        current_constraints = remaining
        for name in previous['parameterMap'].values():
            current_parameters.pop(name, None)
    elif len(instances) >= 100:
        raise DrawingError('图库实例最多 100 个。')
    elif set(parameter_map.values()).intersection(current_parameters):
        raise DrawingError('实例参数名冲突，请重试插入。')
    # New handles are allocated by ezdxf. Client IDs cannot overwrite existing
    # drawing entities; metadata and DXF commit in the store's one transaction.
    entity_map = {}
    for source in sources:
        created = _add(doc, source)
        entity_map[source['id']] = created.dxf.handle
    remapped = []
    for index, constraint in enumerate(constraints):
        item = copy.deepcopy(constraint)
        item.update(id=f'{identity}_c{index}', entities=[entity_map[key] for key in item['entities']])
        if 'value' in item:
            item['value'] = expression(item['value'])
        remapped.append(item)
    current_parameters.update({parameter_map[name]: expression(value) for name, value in parameters.items()})
    instance = {'id': identity, 'templateId': template['id'], 'name': _text(operation.get('name', template.get('name', '参数构件')), 200), 'template': copy.deepcopy(template), 'parameters': copy.deepcopy(parameters), 'parameterMap': parameter_map, 'translation': translation, 'entityIds': list(entity_map.values()), 'entityMap': entity_map, 'constraintIds': [item['id'] for item in remapped]}
    properties.update(constraints=current_constraints + remapped, parameters=current_parameters, libraryInstances=[item for item in instances if item['id'] != identity] + [instance])
    _json(properties)
