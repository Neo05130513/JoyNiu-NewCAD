"""Private saved display attributes follow proven semantic body membership."""
from copy import deepcopy
import re

from .cad_plan import PlanValidationError

_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _fail(message, code="unresolved_body_binding"):
    raise PlanValidationError(message, code=code)


def validate_body_binding(body):
    binding = body.get("binding")
    if binding is None: return
    if not isinstance(binding, dict) or set(binding) != {"version", "keys", "otherKeys"} or type(binding.get("version")) is not int or binding["version"] != 1:
        _fail("实体显示关联格式无效。", "invalid_body_binding")
    for field in ("keys", "otherKeys"):
        values = binding[field]
        if not isinstance(values, list) or len(values) > 1024 or (field == "keys" and not values) or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in values) or len(set(values)) != len(values):
            _fail("实体显示关联需要唯一、有限的语义面标识。", "invalid_body_binding")
    if set(binding["keys"]) & set(binding["otherKeys"]):
        _fail("实体显示关联的本体与其它实体不能混淆。", "invalid_body_binding")


def resolve_body_states(plan, shape, history=None):
    from .cad_surface_features import body_signature
    states = plan.get("bodyStates", [])
    if not states: return
    bodies = shape.val().Solids()
    signatures = [body_signature(body) for body in bodies]
    body_faces = [body.Faces() for body in bodies]
    key_bodies = {}
    if history:
        for key, items in history.maps.get(plan["result"], {}).get("face", {}).items():
            for face, _ in items:
                for index, faces in enumerate(body_faces):
                    if any(face.isSame(candidate) for candidate in faces): key_bodies.setdefault(key, set()).add(index)
    current_keys = [{key for key, indices in key_bodies.items() if indices == {index}} for index in range(len(bodies))]
    # Compound and pattern copies have new instance names, with explicit
    # source ancestry. Multiple descendants remain multiple candidates.
    lineage_bodies = {}
    if history:
        for key, indices in key_bodies.items():
            for ancestor in {key, *history.body_lineage.get(key, set())}:
                lineage_bodies.setdefault(history.canonical_key(ancestor), set()).update(indices)
    assigned = set()
    for state in states:
        old = getattr(history, "baseline_body_states", {}).get(state["id"], {}) if history else {}
        original = getattr(history, "original_body_states", {}).get(state["id"], old) if history else {}
        binding = state.get("binding")
        if not binding and state.get("signature") == original.get("signature"): binding = old.get("binding")
        if binding:
            if not history or not (history.trusted_source or binding == old.get("binding")):
                _fail("实体显示关联必须来自当前设计的已保存版本。", "untrusted_topology_binding")
            candidates = set()
            for key in binding["keys"]: candidates.update(lineage_bodies.get(history.canonical_key(key), set()))
            if len(candidates) != 1: _fail("原实体已消失或分裂，名称、颜色和隐藏设置需要重新关联。")
            index = next(iter(candidates))
            if any(index in lineage_bodies.get(history.canonical_key(key), set()) for key in binding["otherKeys"]):
                _fail("原实体已与其它实体合并，不能自动套用旧实体显示设置。")
        else:
            candidates = [i for i, signature in enumerate(signatures) if signature == state["signature"]]
            if len(candidates) != 1: _fail("实体显示设置的原实体不存在或不唯一，请重新选择。")
            index = candidates[0]
        if index in assigned: _fail("多个实体显示设置不能自动合并到同一个实体。")
        assigned.add(index)
        state["signature"] = signatures[index]
        if history and history.persist:
            if current_keys[index]:
                other_keys = set().union(*(keys for i, keys in enumerate(current_keys) if i != index))
                value = {"version":1,"keys":sorted(current_keys[index]),"otherKeys":sorted(other_keys)}
                validate_body_binding({"binding":value})
                state["binding"] = value
            elif binding:
                state["binding"] = deepcopy(binding)
