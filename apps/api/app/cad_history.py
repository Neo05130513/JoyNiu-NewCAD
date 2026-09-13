"""Server-authorized semantic topology history, separate from transient picks.

Names follow generating features and OCCT Modified/Generated relationships.
No nearest-entity or numeric-index fallback is used for a persisted name.
"""
from copy import deepcopy
from itertools import product
import math

from .cad_plan import PlanValidationError
from .cad_topology import digest, edge_signature, face_signature, planar_face_frame, resolve_topology_selection


def _fail(message, code="unresolved_topology_binding"):
    raise PlanValidationError(message, code=code)


def selectors(feature):
    if isinstance(feature.get("planeSource"), dict):
        yield "planeSource", feature["planeSource"]
    for field in ("edges", "faces", "references"):
        if isinstance(feature.get(field), list):
            for value in feature[field]:
                if isinstance(value, dict): yield field, value


def authorize_bindings(plan, baseline=None):
    """Only the private saved owner/revision may grant persistent references."""
    old = {(collection, item["id"]): item for collection in ("features", "sketches", "annotations") for item in (baseline or {}).get(collection, [])}
    for collection, feature in ((collection, item) for collection in ("features", "sketches", "annotations") for item in plan.get(collection, [])):
        previous = old.get((collection, feature["id"]), {})
        for field, value in selectors(feature):
            if "binding" in value and not any(field == old_field and value == old_value for old_field, old_value in selectors(previous)):
                _fail("持久拓扑引用必须来自当前设计的已保存版本，请重新拾取。", "untrusted_topology_binding")
        if "planeAttachment" in feature and feature["planeAttachment"] != previous.get("planeAttachment"):
            _fail("草图附着坐标必须来自当前设计的已保存版本。", "untrusted_topology_binding")
        if collection == "annotations" and "anchors" in feature and feature["anchors"] != previous.get("anchors"):
            _fail("PMI 锚点必须由服务器拾取生成并来自当前已保存版本。", "untrusted_topology_binding")
    previous_bodies = {item["id"]: item for item in (baseline or {}).get("bodyStates", [])}
    for body in plan.get("bodyStates", []):
        if "binding" in body and body["binding"] != previous_bodies.get(body["id"], {}).get("binding"):
            _fail("实体显示关联必须由服务器生成并来自当前已保存版本。", "untrusted_topology_binding")


def strip_history_metadata(plan):
    value = deepcopy(plan)
    for feature in [*value.get("features", []), *value.get("sketches", []), *value.get("annotations", [])]:
        feature.pop("planeAttachment", None)
        for _, item in selectors(feature): item.pop("binding", None)
    for body in value.get("bodyStates", []): body.pop("binding", None)
    return value


def profile_builder(cq, feature, plane, wire, number, vector, edge_roles, *, face=None):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
    from OCP.gp import gp_Ax1
    face = cq.Face.makeFromWires(wire.val()) if face is None else face
    originals = [(role, edge_signature(edge)) for role, edge in edge_roles]
    edge_roles[:] = [(role, edge) for edge in face.Edges() for role, signature in originals if edge_signature(edge) == signature]
    if feature["op"] == "profile_extrude":
        builder = BRepPrimAPI_MakePrism(face.wrapped, plane.plane.zDir.multiply(number(feature["distance"])).wrapped)
    else:
        begin = plane.plane.toWorldCoords(tuple(vector(feature["axisStart"])))
        end = plane.plane.toWorldCoords(tuple(vector(feature["axisEnd"])))
        axis, angle = gp_Ax1(begin.toPnt(), (end-begin).toDir()), math.radians(number(feature.get("angle", 360)))
        builder = BRepPrimAPI_MakeRevol(face.wrapped, axis, angle)
    generated = {}
    for role, edge in edge_roles:
        values = list(builder.Generated(edge.wrapped))
        if not values and feature["op"] == "profile_revolve":
            # OCCT's full revolution omits radial edges from Generated().
            # Sweep that exact generating edge with the same kernel axis;
            # register only an exact unique resulting-face correspondence.
            swept = BRepPrimAPI_MakeRevol(edge.wrapped, axis, angle).Shape()
            if not swept.IsNull(): values = [swept]
        generated[role] = [face for raw in values for face in cq.Shape.cast(raw).Faces()]
    return builder, cq.Workplane("XY").newObject([cq.Shape.cast(builder.Shape())]), generated


def boolean_builder(cq, operation, first, second):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut, BRepAlgoAPI_Common
    builder = {"union": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "intersect": BRepAlgoAPI_Common}[operation](first.val().wrapped, second.val().wrapped)
    builder.Build()
    if not builder.IsDone(): _fail("布尔运算失败，原版本未改变。", "feature_failed")
    return builder, cq.Workplane("XY").newObject([cq.Shape.cast(builder.Shape())])


def compound_builder(cq, source_ids, shapes):
    """Copy actual independent solid/sheet/curve members, never fuse them."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    checked, solid_count, face_count = [], 0, 0
    for source_id in source_ids:
        value = shapes[source_id].val()
        pending, members = [value], []
        while pending:
            item = pending.pop()
            if item.ShapeType() == "Solid":
                if not item.isValid() or item.Volume() <= 1e-9:
                    _fail("多实体输入必须是有效的正体积实体。", "invalid_geometry")
                members.append(item)
            elif item.ShapeType() in {"Compound", "CompSolid"}: pending.extend(item)
            elif item.ShapeType() in {"Face", "Shell"}:
                if not item.isValid() or item.Area() <= 1e-9: _fail("集合中的曲面无效或面积为零。", "invalid_geometry")
                members.append(item)
            elif item.ShapeType() in {"Edge", "Wire"}:
                if not item.isValid() or item.Length() <= 1e-9: _fail("集合中的曲线无效或长度为零。", "invalid_geometry")
                members.append(item)
            else: _fail("集合只支持有效实体、曲面和曲线。", "invalid_geometry")
        if not members: _fail("集合输入没有有效几何。", "invalid_geometry")
        solid_count += sum(len(item.Solids()) for item in members); face_count += sum(len(item.Faces()) for item in members)
        if solid_count > 1024 or face_count > 10_000:
            _fail("多实体结果超过 1024 个实体或 10000 个面的资源上限。", "resource_limit")
        checked.append((source_id, members))
    copies, members = {}, []
    class MemberCopies:
        def __init__(self, builders): self.builders = builders
        def Modified(self, source):
            # A shared source face may occur both in a solid and as an
            # independent sheet. Preserve both outputs; history resolution
            # will explicitly reject any resulting ambiguous semantic key.
            return [raw for builder, source_faces in self.builders
                    if any(face.wrapped.IsSame(source) for face in source_faces)
                    for raw in builder.Modified(source)]
    for source_id, source_members in checked:
        builders = []
        for value in source_members:
            # Copy each actual member independently, including a standalone
            # face that also occurs inside another member of the same input.
            copier = BRepBuilderAPI_Copy(value.wrapped, True, False)
            builders.append((copier, value.Faces()))
            members.append(cq.Shape.cast(copier.Shape()))
        copies[source_id] = MemberCopies(builders)
    return cq.Workplane("XY").newObject([cq.Compound.makeCompound(members)]), copies


def rounding_builder(cq, feature, base, selected, number):
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet, BRepFilletAPI_MakeChamfer
    builder = (BRepFilletAPI_MakeFillet if feature["op"] == "fillet" else BRepFilletAPI_MakeChamfer)(base.val().wrapped)
    for edge in selected:
        if feature["op"] == "fillet": builder.Add(number(feature["radius"]), edge.wrapped)
        else:
            adjacent = [face for face in base.val().Faces() if any(edge.isSame(candidate) for candidate in face.Edges())]
            if not adjacent: _fail("倒角边没有关联面。")
            builder.Add(number(feature["length"]), number(feature.get("length2", feature["length"])), edge.wrapped, adjacent[0].wrapped)
    builder.Build()
    if not builder.IsDone(): _fail("圆角或倒角无法生成，请减小尺寸或重新选边。", "feature_failed")
    return builder, cq.Workplane("XY").newObject([cq.Shape.cast(builder.Shape())])


class TopologyHistory:
    def __init__(self, *, persist=False, baseline=None, original_baseline=None, trusted_source=False):
        self.persist = persist
        self.baseline = {f["id"]: f for f in (baseline or {}).get("features", [])}
        self.original = {f["id"]: f for f in (original_baseline or baseline or {}).get("features", [])}
        self.baseline_sketches = {f["id"]: f for f in (baseline or {}).get("sketches", [])}
        self.original_sketches = {f["id"]: f for f in (original_baseline or baseline or {}).get("sketches", [])}
        self.baseline_annotations = {f["id"]: f for f in (baseline or {}).get("annotations", [])}
        self.original_annotations = {f["id"]: f for f in (original_baseline or baseline or {}).get("annotations", [])}
        self.baseline_body_states = {f["id"]: f for f in (baseline or {}).get("bodyStates", [])}
        self.original_body_states = {f["id"]: f for f in (original_baseline or baseline or {}).get("bodyStates", [])}
        self.body_lineage = {}
        self.trusted_source = trusted_source
        self.maps = {}
        self.aliases = {}
        self.frame_aliases = {}
        self.attachment_aliases = {}

    def canonical_key(self, key):
        seen = set()
        while key in self.aliases and key not in seen:
            seen.add(key)
            key = self.aliases[key]
        return key

    def equivalent_keys(self, key):
        return [key, *[old for old in self.aliases if old != key and self.canonical_key(old) == key]]

    @staticmethod
    def name(feature_id, role):
        return digest(["cad-topology-history-v1", feature_id, role])

    def register(self, feature_id, shape, entries, *, independent_members=False):
        """Carry all candidates; split/merged/deleted names remain unresolved."""
        faces = shape.val().Faces() if hasattr(shape, "val") else shape.Faces()
        actual = {}
        for key, candidates in entries.items():
            unique_sources = []
            for source, frame in candidates:
                if not any(source.isSame(other[0]) for other in unique_sources): unique_sources.append((source, frame))
            # Cleaning may merge edges or discard one of a split face's old
            # identities. Never turn a one-to-many history into a unique name
            # merely because only one fragment still matches after cleaning.
            if len(unique_sources) != 1:
                actual[key] = []
                continue
            mapped = []
            for source, frame in unique_sources:
                matches = [face for face in faces if face.isSame(source)]
                if not matches:
                    signature = face_signature(source)
                    matches = [face for face in faces if face_signature(face) == signature]
                for face in matches:
                    if not any(face.isSame(existing[0]) for existing in mapped): mapped.append((face, frame))
            actual[key] = mapped
        # Multiple distinct names on the same resulting face signal a merge.
        # Do not guess which input face a later selection intended.
        owners = {}
        for key, items in actual.items():
            if len(items) == 1:
                # Distinct compound members may legitimately have identical
                # geometry. Their copied OCCT identities prove independence;
                # a new ambiguous surface pick remains rejected separately.
                signature = (next(i for i, face in enumerate(faces) if face.isSame(items[0][0]))
                             if independent_members else face_signature(items[0][0]))
                owners.setdefault(signature, []).append(key)
        for keys in owners.values():
            if len(keys) > 1:
                for key in keys: actual[key] = []
        edges = {}
        for edge in (shape.val() if hasattr(shape, "val") else shape).Edges():
            adjacent = []
            for key, items in actual.items():
                if len(items) == 1 and any(edge.isSame(candidate) for candidate in items[0][0].Edges()): adjacent.append(key)
            if len(adjacent) == 2:
                key = digest(["adjacent-faces", *sorted(adjacent)])
                edges.setdefault(key, []).append((edge, None))
                for pair in product(*(self.equivalent_keys(face_key) for face_key in adjacent)):
                    alias = digest(["adjacent-faces", *sorted(pair)])
                    if alias != key: self.aliases[alias] = key
        self.maps[feature_id] = {"face": actual, "edge": edges}

    def primitive(self, feature, shape, vector, number):
        import cadquery as cq
        op, fid = feature["op"], feature["id"]
        entries = {}
        for face in shape.val().Faces():
            role = None
            if op == "box" and face.geomType() == "PLANE":
                normal = face.normalAt().toTuple()
                axis = next((i for i, item in enumerate(normal) if abs(item) > 1 - 1e-7), None)
                if axis is not None: role = ["box", axis, 1 if normal[axis] > 0 else -1]
            elif op == "cylinder":
                if face.geomType() == "CYLINDER": role = ["cylinder", "side"]
                elif face.geomType() == "PLANE":
                    direction = cq.Vector(*vector(feature.get("direction", [0, 0, 1]))).normalized()
                    role = ["cylinder", "end" if face.normalAt().dot(direction) > 0 else "start"]
            if role is not None:
                frame = planar_face_frame(face)
                if frame:
                    normal = cq.Vector(*frame["normal"])
                    axis = cq.Vector(1, 0, 0) if abs(normal.x) < .9 else cq.Vector(0, 1, 0)
                    frame["xDir"] = list((axis-normal.multiply(axis.dot(normal))).normalized().toTuple())
                entries.setdefault(self.name(fid, role), []).append((face, frame))
        self.register(fid, shape, entries)

    def register_exact(self, feature, shape):
        """Name only uniquely proven opaque-source faces, never an index.

        Immutable imports use their verified asset digest. Other generators
        use an exact whole-geometry fingerprint, intentionally invalidating
        all such references when their generated dimensions change.
        """
        fid = feature["id"]
        existing = self.maps.get(fid, {}).get("face", {})
        # Missing generator coverage may be filled. A prior ambiguous,
        # split, merged or deleted semantic role may never be resurrected.
        if any(len(items) != 1 for items in existing.values()): return
        faces = shape.val().Faces()
        signatures = [face_signature(face) for face in faces]
        from collections import Counter
        counts = Counter(signatures)
        if feature["op"] == "import_step":
            source = ["immutable-step", feature["sha256"]]
        else:
            source = ["exact-generator", feature["op"], feature.get("catalogId"),
                      digest({"faces":sorted(signatures),"solidVolumes":sorted(round(body.Volume(),8) for body in shape.val().Solids())})]
        entries = {key:list(items) for key,items in existing.items()}
        for face, signature in zip(faces, signatures):
            if counts[signature] != 1 or any(face.isSame(item[0]) for items in existing.values() for item in items): continue
            entries[self.name(fid, ["exact-source-face-v1",source,signature])] = [(face,planar_face_frame(face))]
        self.register(fid,shape,entries)

    def generated_profile(self, feature, builder, generated, shape, plane):
        import cadquery as cq
        entries = {}
        faces = shape.val().Faces()
        axes = (plane.plane.xDir, plane.plane.yDir, plane.plane.zDir)
        rectangle = (feature["op"] == "profile_extrude" and len(faces) == 6 and len(shape.val().Edges()) == 12
                     and all(face.geomType() == "PLANE" and any(abs(face.normalAt().dot(axis)) > 1-1e-7 for axis in axes) for face in faces))
        cylinder = (feature["op"] == "profile_extrude" and len(faces) == 3
                    and sorted(face.geomType() for face in faces) == ["CYLINDER", "PLANE", "PLANE"])
        structure = [feature["op"], [item["type"] for item in feature["segments"]]]
        previous = self.baseline.get(feature["id"], {})
        previous_structure = ([previous["op"], [item["type"] for item in previous["segments"]]]
                              if previous.get("op") == "profile_extrude" else None)
        extrusion_sign = 1
        if feature["op"] == "profile_extrude":
            end_shape = builder.LastShape()
            if not end_shape.IsNull() and (cq.Shape.cast(end_shape).Center()-plane.plane.origin).dot(axes[2]) < 0: extrusion_sign = -1

        def cap_role_for_face(face):
            # Unification can replace two semicircles by one full circle,
            # changing exact boundary signatures. Its analytic start plane
            # is still fixed by the actual OCCT prism's sketch workplane.
            return "start" if abs((face.Center()-plane.plane.origin).dot(axes[2])) < 1e-8 else "end"

        def cap_aliases(role, face, primary, primary_frame):
            # The extrusion's unique start/end face has the same generating
            # meaning even if a rectangle is edited into a trapezoid. Preserve
            # the existing v1 names as proved semantic aliases, not an index
            # or closest-face guess. Side faces do not receive this exemption.
            if feature["op"] != "profile_extrude" or face.geomType() != "PLANE": return
            sign = extrusion_sign * (1 if role == "end" else -1)
            normal = axes[2].multiply(sign)
            reference = cq.Vector(1,0,0) if abs(normal.x) < .9 else cq.Vector(0,1,0)
            cylindrical_x = (reference-normal.multiply(reference.dot(normal))).normalized()
            legacy_raw = builder.FirstShape() if role == "start" else builder.LastShape()
            legacy_frame = planar_face_frame(cq.Shape.cast(legacy_raw))
            box_frame = {"origin": primary_frame["origin"], "xDir": list(axes[0].toTuple()), "normal": list(normal.toTuple())}
            cylinder_frame = {**box_frame, "xDir": list(cylindrical_x.toTuple())}
            aliases = [(self.name(feature["id"], ["box", 2, sign]), box_frame),
                       (self.name(feature["id"], ["cylinder", "end" if sign > 0 else "start"]), cylinder_frame),
                       (self.name(feature["id"], [structure, role]), legacy_frame)]
            if previous_structure: aliases.append((self.name(feature["id"], [previous_structure, role]), legacy_frame))
            target = cq.Plane(origin=primary_frame["origin"], xDir=primary_frame["xDir"], normal=primary_frame["normal"])
            for alias, alternate in aliases:
                if alias != primary:
                    self.aliases[alias] = primary
                    if alternate:
                        source = cq.Plane(origin=alternate["origin"], xDir=alternate["xDir"], normal=alternate["normal"])
                        def local_direction(value): return [value.dot(axis) for axis in (target.xDir,target.yDir,target.zDir)]
                        self.frame_aliases[alias] = {"origin": list(target.toLocalCoords(source.origin).toTuple()),
                                                    "xDir": local_direction(source.xDir), "normal": local_direction(source.zDir)}

        if rectangle or cylinder:
            for face in faces:
                frame = planar_face_frame(face)
                if rectangle:
                    index = next(i for i, axis in enumerate(axes) if abs(face.normalAt().dot(axis)) > 1-1e-7)
                    role = ["box", index, 1 if face.normalAt().dot(axes[index]) > 0 else -1]
                    xdir = axes[1] if index == 0 else axes[0]
                    frame["xDir"] = list(xdir.toTuple())
                else:
                    role = ["cylinder", "side"] if not frame else ["cylinder", "end" if face.normalAt().dot(axes[2]) > 0 else "start"]
                    if frame:
                        normal = cq.Vector(*frame["normal"])
                        axis = cq.Vector(1,0,0) if abs(normal.x) < .9 else cq.Vector(0,1,0)
                        frame["xDir"] = list((axis-normal.multiply(axis.dot(normal))).normalized().toTuple())
                key = self.name(feature["id"], role)
                if frame and abs(face.normalAt().dot(axes[2])) > 1-1e-7:
                    cap_aliases(cap_role_for_face(face), face, key, frame)
                entries.setdefault(key, []).append((face, frame))
            self.register(feature["id"], shape, entries)
            return
        for role, raw in [("start", builder.FirstShape()), ("end", builder.LastShape())]:
            if not raw.IsNull():
                value = cq.Shape.cast(raw)
                for face in value.Faces():
                    key = self.name(feature["id"], [structure, role])
                    frame = planar_face_frame(face)
                    cap_aliases(role, face, key, frame)
                    entries.setdefault(key, []).append((face, frame))
        for role, faces in generated.items():
            for face in faces:
                entries.setdefault(self.name(feature["id"], [structure, "segment", role]), []).append((face, planar_face_frame(face)))
        self.register(feature["id"], shape, entries)

    def through(self, feature_id, source_ids, builder, shape, generated_edges=()):
        import cadquery as cq
        entries = {}
        for source_id in source_ids:
            for key, items in self.maps.get(source_id, {}).get("face", {}).items():
                for source, frame in items:
                    modified = list(builder.Modified(source.wrapped))
                    if not modified and not builder.IsDeleted(source.wrapped): modified = [source.wrapped]
                    for raw in modified:
                        for face in cq.Shape.cast(raw).Faces(): entries.setdefault(key, []).append((face, frame))
        for edge_key, edge in generated_edges:
            for raw in builder.Generated(edge.wrapped):
                for face in cq.Shape.cast(raw).Faces():
                    key = self.name(feature_id, ["generated-from-edge", edge_key])
                    for previous in self.equivalent_keys(edge_key):
                        alias = self.name(feature_id, ["generated-from-edge", previous])
                        if alias != key: self.aliases[alias] = key
                    entries.setdefault(key, []).append((face, planar_face_frame(face)))
        self.register(feature_id, shape, entries)

    def carry_unchanged(self, feature_id, source_ids, shape):
        entries = {}
        for source_id in source_ids:
            for key, value in self.maps.get(source_id, {}).get("face", {}).items(): entries.setdefault(key, []).extend(value)
        self.register(feature_id, shape, entries)

    def profile_cleanup(self, feature_id, entries, cleanup, shape):
        """Follow OCCT's explicit unification history, including rebuilt caps.

        A new merged face is named from its exact contributing generation
        roles. Old individual face names do not alias that merged identity.
        """
        import cadquery as cq
        groups = []
        for key, items in entries.items():
            for source, frame in items:
                modified = list(cleanup.Modified(source.wrapped))
                if not modified and not cleanup.IsRemoved(source.wrapped): modified = [source.wrapped]
                for raw in modified:
                    for face in cq.Shape.cast(raw).Faces():
                        group = next((item for item in groups if item[0].isSame(face)), None)
                        if group is None:
                            group = [face, {}]; groups.append(group)
                        group[1][key] = frame
        mapped = {}
        for face, sources in groups:
            keys = sorted(sources)
            key = keys[0] if len(keys) == 1 else self.name(feature_id, ["profile-cleanup-face", keys])
            if len(keys) > 1: self.body_lineage.setdefault(key, set()).update(keys)
            frame = sources[keys[0]] if len(keys) == 1 else planar_face_frame(face)
            mapped.setdefault(key, []).append((face, frame))
        self.register(feature_id, shape, mapped)

    def compound(self, feature_id, copies, shape):
        import cadquery as cq
        entries = {}
        for source_id, copier in copies.items():
            for key, items in self.maps.get(source_id, {}).get("face", {}).items():
                member_key = self.name(feature_id, ["member", source_id, key])
                self.body_lineage.setdefault(member_key, set()).update({key, *self.body_lineage.get(key, set())})
                for previous in self.equivalent_keys(key):
                    alias = self.name(feature_id, ["member", source_id, previous])
                    if alias != member_key:
                        self.aliases[alias] = member_key
                        if previous in self.frame_aliases: self.frame_aliases[alias] = deepcopy(self.frame_aliases[previous])
                for face, frame in items:
                    for raw in copier.Modified(face.wrapped):
                        for copied in cq.Shape.cast(raw).Faces():
                            entries.setdefault(member_key, []).append((copied, frame))
        self.register(feature_id, shape, entries, independent_members=True)

    def transform(self, feature, shape, vector, number):
        import cadquery as cq
        op, source_id = feature["op"], feature["input"]
        entries = {}
        count = int(number(feature["count"])) if op.endswith("pattern") else 1
        for instance in range(count):
            def moved(value):
                if op in {"translate", "linear_pattern"}:
                    return value.translate(tuple(component * (instance if op.endswith("pattern") else 1) for component in vector(feature["vector"])))
                if op in {"rotate", "circular_pattern"}:
                    angle = number(feature["angle"])
                    if op.endswith("pattern"): angle *= instance / (count if abs(abs(angle)-360) < 1e-7 else count-1)
                    return value.rotate(vector(feature["axisStart"]), vector(feature["axisEnd"]), angle)
                if feature["plane"] == "custom": normal, origin = vector(feature["frame"]["normal"]), vector(feature["frame"]["origin"])
                else: normal, origin = {"XY": (0,0,1), "XZ": (0,1,0), "YZ": (1,0,0)}[feature["plane"]], vector(feature.get("origin", [0,0,0]))
                return value.mirror(normal, origin)
            for key, items in self.maps.get(source_id, {}).get("face", {}).items():
                for face, frame in items:
                    new_frame = None
                    if frame:
                        point = cq.Vector(*frame["origin"])
                        translated = moved(cq.Vertex.makeVertex(*point.toTuple())).Center()
                        def direction(values):
                            endpoint = moved(cq.Vertex.makeVertex(*(point+cq.Vector(*values)).toTuple())).Center()
                            return list((endpoint-translated).toTuple())
                        new_frame = {"origin": list(translated.toTuple()), "xDir": direction(frame["xDir"]), "normal": direction(frame["normal"])}
                    derived_key = self.name(feature["id"], ["instance", instance, key]) if op.endswith("pattern") else key
                    if derived_key != key: self.body_lineage.setdefault(derived_key, set()).update({key, *self.body_lineage.get(key, set())})
                    if op.endswith("pattern"):
                        for previous in self.equivalent_keys(key):
                            alias = self.name(feature["id"], ["instance", instance, previous])
                            if alias != derived_key: self.aliases[alias] = derived_key
                    entries.setdefault(derived_key, []).append((moved(face), new_frame))
                    if op == "mirror" and feature.get("keepOriginal"):
                        original_key = self.name(feature["id"], ["original", key])
                        self.body_lineage.setdefault(original_key, set()).update({key, *self.body_lineage.get(key, set())})
                        entries.setdefault(original_key, []).append((face, frame))
        self.register(feature["id"], shape, entries)

    def entity_key(self, source_id, kind, entity):
        matches = [key for key, items in self.maps.get(source_id, {}).get(kind, {}).items()
                   if len(items) == 1 and items[0][0].isSame(entity)]
        return matches[0] if len(matches) == 1 else None

    def _baseline_selector(self, feature, field, selector):
        original, baseline = self._reference_baselines(feature)
        for old_field, old_value in selectors(original):
            if field == old_field and selector == old_value:
                old_items = list(selectors(original))
                index = old_items.index((old_field, old_value))
                bound_items = list(selectors(baseline))
                if index < len(bound_items) and bound_items[index][0] == field: return bound_items[index][1]
        return selector if self.trusted_source else None

    def _reference_baselines(self, feature):
        if feature.get("sketchId") and not feature.get("planeReference"):
            return self.original_sketches.get(feature["sketchId"], {}), self.baseline_sketches.get(feature["sketchId"], {})
        return self.original.get(feature["id"], {}), self.baseline.get(feature["id"], {})

    def resolve(self, plan, feature, source_id, shape, selected, kind, field):
        values, keys = [], []
        for selector in selected:
            if selector["sourceFeatureId"] != source_id: _fail("拓扑来源与当前输入特征不一致。", "stale_topology")
            old = self._baseline_selector(feature, field, selector)
            binding = old.get("binding") if old else None
            if "binding" in selector and not binding:
                _fail("持久拓扑引用未获当前保存版本授权。", "untrusted_topology_binding")
            if binding:
                key = self.canonical_key(binding["key"])
                items = self.maps.get(source_id, {}).get(kind, {}).get(key, [])
                if len(items) != 1:
                    _fail("原先选择的边或面已消失、分裂或无法唯一对应，请重新拾取。")
                entity, _ = items[0]
                if field == "planeSource" and binding["key"] in self.frame_aliases:
                    self.attachment_aliases[feature["id"]] = self.frame_aliases[binding["key"]]
                selector["binding"] = {"version": 1, "key": key}
            else:
                entity = resolve_topology_selection(plan, source_id, shape, [selector], kind)[0]
                key = self.entity_key(source_id, kind, entity)
                if self.persist and key: selector["binding"] = {"version": 1, "key": key}
            if any(entity.isSame(previous) for previous in values): _fail("不能重复选择同一条边或面。", "invalid_topology_selector")
            values.append(entity); keys.append(key)
        return values, keys

    def attached_frame(self, feature, source_id, face, frame):
        import cadquery as cq
        key = feature["planeSource"].get("binding", {}).get("key")
        items = self.maps.get(source_id, {}).get("face", {}).get(key, [])
        if len(items) != 1 or not items[0][1]: return frame
        source = items[0][1]
        plane = cq.Plane(origin=source["origin"], xDir=source["xDir"], normal=source["normal"])
        original, baseline = self._reference_baselines(feature)
        attachment = feature.get("planeAttachment")
        if not attachment and {key:value for key,value in feature.get("planeSource", {}).items() if key != "binding"} == {key:value for key,value in baseline.get("planeSource", {}).items() if key != "binding"}:
            attachment = baseline.get("planeAttachment")
        if attachment and not self.trusted_source and (feature.get("frame") != original.get("frame") or attachment != baseline.get("planeAttachment")):
            attachment = None
            feature.pop("planeAttachment", None)
        if attachment:
            adjustment = self.attachment_aliases.get(feature["id"])
            if adjustment:
                relative = cq.Plane(origin=adjustment["origin"], xDir=adjustment["xDir"], normal=adjustment["normal"])
                def corrected(value): return list((relative.xDir.multiply(value[0])+relative.yDir.multiply(value[1])+relative.zDir.multiply(value[2])).toTuple())
                attachment = {"version": 1, "origin": list(relative.toWorldCoords(tuple(attachment["origin"])).toTuple()),
                              "xDir": corrected(attachment["xDir"]), "normal": corrected(attachment["normal"])}
            def direction(values): return plane.xDir.multiply(values[0]) + plane.yDir.multiply(values[1]) + plane.zDir.multiply(values[2])
            feature["planeAttachment"] = deepcopy(attachment)
            return {"origin": list(plane.toWorldCoords(tuple(attachment["origin"])).toTuple()),
                    "xDir": list(direction(attachment["xDir"]).toTuple()), "normal": list(direction(attachment["normal"]).toTuple())}
        if self.persist:
            def local_direction(values):
                value = cq.Vector(*values)
                return [value.dot(axis) for axis in (plane.xDir, plane.yDir, plane.zDir)]
            feature["planeAttachment"] = {"version": 1, "origin": list(plane.toLocalCoords(cq.Vector(*frame["origin"])).toTuple()),
                                           "xDir": local_direction(frame["xDir"]), "normal": local_direction(frame["normal"])}
        return frame
