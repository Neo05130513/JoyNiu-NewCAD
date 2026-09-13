"""PMI point anchors on server-authorized, uniquely named OCCT topology."""
from copy import deepcopy
import math
from .cad_plan import PlanValidationError
from .cad_topology import resolve_topology_selection


def _fail(message, code='invalid_pmi_anchor'):
    raise PlanValidationError(message, code=code)


def validate_pmi_anchors(note):
    anchors = note.get('anchors')
    if anchors is None:
        return
    if not isinstance(anchors, list) or len(anchors) != len(note.get('references', [])) or len(anchors) != len(note.get('points', [])):
        _fail('标注锚点必须与拾取的边、面和测量点一一对应。')
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) != {'version', 'kind', 'coordinates'} or anchor['version'] != 1 or anchor['kind'] not in {'face','edge'}:
            _fail('标注锚点格式无效。')
        coordinates = anchor['coordinates']
        if not isinstance(coordinates, list) or len(coordinates) != (2 if anchor['kind']=='face' else 1) or any(type(v) not in {int,float} or not math.isfinite(v) or abs(v)>1e6 for v in coordinates):
            _fail('标注锚点坐标无效。')


def resolve_plan_annotations(plan, shapes, number, history=None):
    import cadquery as cq
    for note in plan.get('annotations', []):
        refs = note.get('references', [])
        if not refs:
            note.pop('anchors', None)
            continue
        if len(refs) != len(note.get('points', [])):
            _fail('标注引用与测量点不对应，请重新选择标注位置。')
        old = getattr(history, 'baseline_annotations', {}).get(note['id'], {}) if history else {}
        original = getattr(history, 'original_annotations', {}).get(note['id'], old) if history else {}
        if note.get('anchors') and not (history and history.trusted_source) and note['anchors'] != old.get('anchors'):
            _fail('标注锚点必须来自当前账号的已保存版本。', 'untrusted_topology_binding')
        previous_points = [[number(v) for v in p] for p in note['points']]
        old_refs = old.get('references', [])
        anchors, points = [], []
        for index, ref in enumerate(refs):
            kind, source_id = ref['kind'], ref['sourceFeatureId']
            if source_id not in shapes:
                _fail('标注所引用的特征已不存在，请重新选择。')
            previous_ref = old_refs[index] if index < len(old_refs) else {}
            binding = ref.get('binding')
            original_refs = original.get('references', [])
            same_original = index < len(original_refs) and ref == original_refs[index]
            if not binding and same_original:
                binding = previous_ref.get('binding')
            if binding:
                if not history:
                    _fail('读取关联标注需要其已保存建模历史。')
                if not history.trusted_source and not (binding == previous_ref.get('binding') and (ref == previous_ref or same_original)):
                    _fail('标注拓扑引用未获当前保存版本授权。', 'untrusted_topology_binding')
                key = history.canonical_key(binding['key'])
                items = history.maps.get(source_id, {}).get(kind, {}).get(key, [])
                if len(items) != 1:
                    _fail('标注所选的边或面已消失或分裂，请重新拾取。', 'unresolved_pmi_anchor')
                entity = items[0][0]
            else:
                entity = resolve_topology_selection(plan, source_id, shapes[source_id], [ref], kind)[0]
                key = history.entity_key(source_id, kind, entity) if history else None
            anchor = (note.get('anchors') or old.get('anchors') or [])[index] if index < len(note.get('anchors') or old.get('anchors') or []) and (ref == previous_ref or same_original or history and history.trusted_source) else None
            # Explicit edits to a stored coordinate require a fresh geometry pick.
            if anchor and note.get('points') != original.get('points') and not (history and history.trusted_source):
                _fail('请重新拾取标注位置，或解除关联后输入坐标。')
            if anchor:
                if anchor['kind'] != kind: _fail('标注关联类型发生变化。')
                if kind == 'face':
                    u0,u1,v0,v1 = entity.uvBounds(); u,v = anchor['coordinates']
                    point = entity.positionAt(u0+u*(u1-u0),v0+v*(v1-v0))
                else:
                    t0,t1 = entity.paramAt(0.),entity.paramAt(1.)
                    point = entity.positionAt(t0+anchor['coordinates'][0]*(t1-t0),mode='parameter')
            else:
                picked = cq.Vector(*previous_points[index])
                if kind == 'face':
                    u,v = entity.paramAt(picked); u0,u1,v0,v1 = entity.uvBounds()
                    if min(abs(u1-u0),abs(v1-v0)) < 1e-12: _fail('所选面不能建立稳定标注锚点。')
                    anchor={'version':1,'kind':kind,'coordinates':[(u-u0)/(u1-u0),(v-v0)/(v1-v0)]}
                    point=entity.positionAt(u,v)
                else:
                    t=entity.paramAt(picked); t0,t1=entity.paramAt(0.),entity.paramAt(1.)
                    if abs(t1-t0)<1e-12: _fail('所选边不能建立稳定标注锚点。')
                    anchor={'version':1,'kind':kind,'coordinates':[(t-t0)/(t1-t0)]}
                    point=entity.positionAt(t,mode='parameter')
                tolerance=max(.075,entity.BoundingBox().DiagonalLength*1e-6)
                if entity.distance(cq.Vertex.makeVertex(*point.toTuple())) > tolerance:
                    _fail('标注点不在所选面的实际边界内，请重新拾取。')
                if (point-picked).Length > tolerance:
                    _fail('标注点不在所选边或面上，请重新拾取。')
            points.append(list(point.toTuple())); anchors.append(anchor)
            if history and history.persist:
                if not key:
                    _fail('所选几何不能建立唯一标注关联，请重新选取或明确解除几何关联。', 'unresolved_pmi_anchor')
                ref['binding']={'version':1,'key':key}
        if history and history.persist:
            note['anchors']=deepcopy(anchors)
        if points:
            delta=[points[-1][i]-previous_points[-1][i] for i in range(3)]
            if any(abs(v)>1e-10 for v in delta):
                note['position']=[number(note['position'][i])+delta[i] for i in range(3)]
            note['points']=points
