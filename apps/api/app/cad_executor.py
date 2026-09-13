"""Execute restricted CAD plans in a bounded, disposable worker process.

No model-supplied Python, module imports, paths, arbitrary selectors or shell commands
are executed. The process boundary limits native OCCT failures and cost; it
is not advertised as an operating-system security sandbox for arbitrary code.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from .cad_plan import PlanValidationError, UnknownParametersError, evaluate_expression, resolve_parameters, validate_plan


def _failure(exc: Exception, *, plan: dict[str, Any] | None = None, feature_id: str | None = None) -> dict[str, Any]:
    error = {"code": getattr(exc, "code", "execution_failed"), "message": str(exc)[:2000]}
    if isinstance(exc, PlanValidationError):
        error.update(exc.details)
    selected_feature = feature_id or getattr(exc, "feature_id", None)
    if selected_feature:
        error["featureId"] = selected_feature
    result = {"status": "needs_input" if isinstance(exc, UnknownParametersError) else "failed",
              "valid": False, "errors": [error], "artifacts": {}, "plan": plan}
    if isinstance(exc, UnknownParametersError):
        result["missingParameters"] = exc.missing
    return result


def _check_arc_radius(start: tuple[float, ...], through: tuple[float, ...], end: tuple[float, ...],
                      expected: float, segment_index: int) -> None:
    """Check the local sketch circle before constructing its OCCT arc.

    Translate to the start point before taking the area to avoid cancellation
    from a distant sketch origin. The tolerance covers arithmetic noise, not
    a design tolerance or permission to move the supplied points.
    """
    first = (through[0] - start[0], through[1] - start[1])
    chord = (end[0] - start[0], end[1] - start[1])
    a, b, c = math.dist(start, through), math.dist(through, end), math.dist(start, end)
    twice_area = abs(first[0] * chord[1] - first[1] * chord[0])
    if min(a, b, c) <= 1e-12 or twice_area <= 1e-12 * a * c:
        raise PlanValidationError(f"Arc segment {segment_index} cannot define a finite radius: points must be distinct and non-collinear",
                                  code="invalid_arc_geometry", details={"segmentIndex": segment_index, "expectedRadius": expected})
    actual = a * b * c / (2 * twice_area)
    if not math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-5):
        raise PlanValidationError(f"Arc segment {segment_index} radius mismatch: actual {actual:.9g} mm, expected {expected:.9g} mm; correct the start/through/to points or the radius constraint",
                                  code="arc_radius_mismatch", details={"segmentIndex": segment_index,
                                                                       "actualRadius": actual, "expectedRadius": expected})


def build_plan_shape(plan: dict[str, Any], *, progress: Callable[[dict[str, Any]], None] | None = None,
                     history=None, history_output=None, imported_assets=None) -> tuple[Any, dict[str, float], list[dict[str, Any]]]:
    """Worker-level builder; application callers use the isolated public APIs."""
    from .geometry import get_cadquery
    from .cad_advanced_features import ADVANCED_OP_FIELDS, build_advanced_feature
    from .cad_topology import custom_workplane, resolve_topology_selection, edge_signature
    from .cad_sketch_constraints import check_sketch_constraints
    from .cad_history import profile_builder, boolean_builder, rounding_builder, compound_builder
    from .cad_profile_operations import PROFILE_OP_FIELDS, build_profile_operation
    from .cad_standard_parts import STANDARD_PART_OP_FIELDS, build_standard_part
    from .cad_import_assets import IMPORT_OP_FIELDS, build_import_step
    from .cad_editor_entities import resolve_feature_entities
    from .cad_surface_features import SURFACE_OP_FIELDS, SHEET_OPS, build_surface_feature

    canonical = validate_plan(plan, allow_unresolved=False)
    if not canonical["features"]: raise PlanValidationError("请先由草图创建实体或曲面，再预览或导出。", code="empty_geometry")
    parameters = resolve_parameters(canonical)
    cq = get_cadquery()
    if cq is None:
        raise RuntimeError("CadQuery/OCCT is required; no substitute geometry will be generated")
    shapes: dict[str, Any] = {}
    trace = []

    def number(value: Any) -> float:
        return evaluate_expression(value, parameters)

    def vector(value: list[Any]) -> tuple[float, ...]:
        return tuple(number(component) for component in value)

    def positive(value: Any, name: str) -> float:
        result = number(value)
        if result <= 1e-7:
            raise PlanValidationError(f"{name} must be greater than 0.0000001 mm")
        return result

    for stored_feature in canonical["features"]:
        feature_id, op = stored_feature["id"], stored_feature["op"]
        started = time.monotonic()
        if progress:
            progress({"featureId": feature_id, "operation": op, "state": "running"})
        try:
            def sketch_frame(sketch):
                check_sketch_constraints(sketch, number)
                context = {**deepcopy(sketch), "id": feature_id}
                if any(item["id"] == sketch["id"] for item in canonical.get("sketches", [])): context["sketchId"] = sketch["id"]
                context.pop("axisReference", None)
                if context.get("planeReference"): context = resolve_feature_entities(context, canonical, number)
                workplane = (custom_workplane(cq, context, vector, canonical, shapes, history) if context["plane"] == "custom"
                             else cq.Workplane(context["plane"], origin=vector(context.get("origin", [0,0,0]))))
                for key in ("planeSource", "planeAttachment"):
                    if key in context: sketch[key] = deepcopy(context[key])
                return {"origin": list(workplane.plane.origin.toTuple()), "normal": list(workplane.plane.zDir.toTuple()), "xDir": list(workplane.plane.xDir.toTuple())}
            linked = stored_feature.get("sketchId") or stored_feature.get("planeReference") or stored_feature.get("axisReference") or op == "profile_loft" or (op == "profile_sweep" and stored_feature.get("path", {}).get("sketchId"))
            feature = resolve_feature_entities(stored_feature, canonical, number, sketch_frame_resolver=sketch_frame) if linked else stored_feature
            history_registered = False
            history_builder = None
            profile_parts = None
            profile_semantic = None
            profile_cleanup = None
            edge_roles = []
            generated_edges = []
            origin = vector(feature.get("origin", [0, 0, 0]))
            if op == "box":
                size = tuple(positive(component, "box size") for component in feature["size"])
                shape = cq.Workplane("XY").newObject([cq.Solid.makeBox(*size, cq.Vector(*origin))])
            elif op == "cylinder":
                direction = vector(feature.get("direction", [0, 0, 1]))
                if math.sqrt(sum(value * value for value in direction)) < 1e-9:
                    raise PlanValidationError("Cylinder direction must be nonzero")
                solid = cq.Solid.makeCylinder(positive(feature["radius"], "radius"), positive(feature["height"], "height"),
                                              cq.Vector(*origin), cq.Vector(*direction))
                shape = cq.Workplane("XY").newObject([solid])
            elif op in {"profile_extrude", "profile_revolve"}:
                check_sketch_constraints(feature, number)
                current_point = vector(feature["start"])
                plane = custom_workplane(cq, feature, vector, canonical, shapes, history) if feature["plane"] == "custom" else cq.Workplane(feature["plane"], origin=origin)
                if feature.get("contours") or any(segment["type"] not in {"line", "arc"} for segment in feature["segments"]):
                    from .cad_profile_geometry import build_profile_regions
                    if op == "profile_extrude" and abs(number(feature["distance"])) < 1e-7:
                        raise PlanValidationError("Profile extrusion distance must be nonzero")
                    if op == "profile_revolve" and (not 0 < number(feature.get("angle",360)) <= 360 or math.dist(vector(feature["axisStart"]),vector(feature["axisEnd"])) < 1e-7):
                        raise PlanValidationError("Revolve needs distinct axis points and an angle in (0,360]")
                    shape, profile_parts = build_profile_regions(cq, feature, plane, number, vector)
                    # The legacy single-contour builder below retains its exact
                    # established edge ordering for all previously saved plans.
                else:
                    shape = None
                if profile_parts is not None:
                    wire = None
                else:
                    wire = plane.moveTo(*current_point)
                for segment_index, segment in enumerate(feature["segments"] if profile_parts is None else []):
                    end_point = vector(segment["to"])
                    if segment["type"] == "line":
                        wire = wire.lineTo(*end_point)
                    else:
                        through_point = vector(segment["through"])
                        if "radius" in segment:
                            _check_arc_radius(current_point, through_point, end_point,
                                              positive(segment["radius"], "arc radius"), segment_index)
                        wire = wire.threePointArc(through_point, end_point)
                    current_point = end_point
                    if history: edge_roles.append((segment_index, wire.vals()[-1]))
                if wire is not None: wire = wire.close()
                if history and profile_parts is None:
                    original_roles = [(role, edge_signature(edge)) for role, edge in edge_roles]
                    edge_roles = []
                    for edge in wire.val().Edges():
                        matching = [role for role, signature in original_roles if signature == edge_signature(edge)]
                        if len(matching) == 1: edge_roles.append((matching[0], edge))
                        elif not matching: edge_roles.append(("closing", edge))
                if profile_parts is not None:
                    pass
                elif op == "profile_extrude":
                    distance = number(feature["distance"])
                    if abs(distance) < 1e-7:
                        raise PlanValidationError("Profile extrusion distance must be nonzero")
                    if history: history_builder, shape, generated_profile_faces = profile_builder(cq, feature, plane, wire, number, vector, edge_roles)
                    else: shape = wire.extrude(distance)
                else:
                    angle = positive(feature.get("angle", 360), "revolve angle")
                    if angle > 360:
                        raise PlanValidationError("Revolve angle must be at most 360 degrees")
                    start, end = vector(feature["axisStart"]), vector(feature["axisEnd"])
                    if math.dist(start, end) < 1e-7:
                        raise PlanValidationError("Revolve axis requires distinct points")
                    if history: history_builder, shape, generated_profile_faces = profile_builder(cq, feature, plane, wire, number, vector, edge_roles)
                    else: shape = wire.revolve(angle, start, end)
            elif op in IMPORT_OP_FIELDS:
                shape = build_import_step(cq, feature, imported_assets)
            elif op in STANDARD_PART_OP_FIELDS:
                shape = build_standard_part(cq, feature, number, vector)
            elif op in PROFILE_OP_FIELDS:
                plane = (custom_workplane(cq, feature, vector, canonical, shapes, history) if feature["plane"] == "custom" else cq.Workplane(feature["plane"], origin=origin)) if op == "profile_sweep" else None
                shape, profile_semantic = build_profile_operation(cq, feature, number, vector, plane, canonical)
            elif op == "compound":
                shape, copies = compound_builder(cq, feature["inputs"], shapes)
                if history:
                    history.compound(feature_id, copies, shape)
                    history_registered = True
            elif op in {"union", "cut", "intersect"}:
                shape = shapes[feature["inputs"][0]]
                previous_id = feature["inputs"][0]
                for reference in feature["inputs"][1:]:
                    if history:
                        history_builder, shape = boolean_builder(cq, op, shape, shapes[reference])
                        shape = shape.clean()
                        history.through(feature_id, [previous_id, reference], history_builder, shape)
                        history_registered = True
                        previous_id = feature_id
                    else: shape = getattr(shape, op)(shapes[reference])
            elif op == "translate":
                shape = shapes[feature["input"]].translate(vector(feature["vector"]))
            elif op == "mirror":
                if feature["plane"] == "custom":
                    plane = custom_workplane(cq, feature, vector, canonical, shapes).plane
                    normal, origin = plane.zDir, plane.origin
                else: normal = {"XY": (0, 0, 1), "XZ": (0, 1, 0), "YZ": (1, 0, 0)}[feature["plane"]]
                shape = shapes[feature["input"]].mirror(normal, basePointVector=origin, union=feature.get("keepOriginal", False))
            elif op in {"fillet", "chamfer", "shell"} and isinstance(feature.get("edges", feature.get("faces")), list) and isinstance(feature.get("edges", feature.get("faces"))[0], dict):
                base = shapes[feature["input"]]
                if history:
                    selected, selected_keys = history.resolve(canonical, feature, feature["input"], base, feature.get("edges", feature.get("faces")), "face" if op == "shell" else "edge", "faces" if op == "shell" else "edges")
                    generated_edges = [(key, edge) for key, edge in zip(selected_keys, selected) if key]
                else: selected = resolve_topology_selection(canonical, feature["input"], base, feature.get("edges", feature.get("faces")), "face" if op == "shell" else "edge")
                selection = base.newObject(selected)
                if op == "fillet":
                    positive(feature["radius"], "fillet radius")
                    if history: history_builder, shape = rounding_builder(cq, feature, base, selected, number)
                    else: shape = selection.fillet(positive(feature["radius"], "fillet radius"))
                elif op == "chamfer":
                    positive(feature["length"], "chamfer length")
                    if "length2" in feature: positive(feature["length2"], "chamfer second length")
                    if history: history_builder, shape = rounding_builder(cq, feature, base, selected, number)
                    else: shape = selection.chamfer(positive(feature["length"], "chamfer length"), positive(feature["length2"], "chamfer second length") if "length2" in feature else None)
                else:
                    thickness = number(feature["thickness"])
                    if abs(thickness) < 1e-7: raise PlanValidationError("Shell thickness must be nonzero")
                    shape = selection.shell(thickness)
            elif op == "fillet":
                selectors = {"all": None, "parallelX": "|X", "parallelY": "|Y", "parallelZ": "|Z"}
                selector = selectors[feature["edges"]]
                shape = shapes[feature["input"]].edges(selector).fillet(positive(feature["radius"], "fillet radius"))
            elif op in ADVANCED_OP_FIELDS:
                shape = build_advanced_feature(cq, feature, shapes, number, vector)
            elif op in SURFACE_OP_FIELDS:
                shape = build_surface_feature(cq, feature, shapes, number, vector, canonical, history)
            else:  # Defensive; validate_plan already limits operation names.
                raise PlanValidationError(f"Unsupported CAD operation: {op}")
            # A compound is a collection of independent bodies. Do not run
            # same-domain unification across its touching/overlapping members.
            if profile_parts is not None and history:
                from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
                cleaner = ShapeUpgrade_UnifySameDomain(shape.val().wrapped, True, True, True)
                cleaner.AllowInternalEdges(False); cleaner.Build()
                profile_cleanup = cleaner.History()
                shape = cq.Workplane("XY").newObject([cq.Shape.cast(cleaner.Shape())])
            elif op != "compound": shape = shape.clean()
            value = shape.val()
            solid_volume = sum(solid.Volume() for solid in value.Solids())
            allows_sheet = op in SHEET_OPS or op in {"translate", "rotate", "mirror", "linear_pattern", "circular_pattern"}
            if not value.isValid() or (not value.Faces() or value.Area() <= 1e-9 if allows_sheet else not value.Solids() or solid_volume <= 1e-9):
                raise PlanValidationError("Feature produced an empty or invalid solid", code="invalid_geometry")
            if len(value.Faces()) > 10_000:
                raise PlanValidationError("Feature exceeds the 10,000 face complexity limit", code="resource_limit")
            shapes[feature_id] = shape
            if stored_feature.get("sketchId") and not feature.get("planeReference"):
                sketch = next(item for item in canonical["sketches"] if item["id"] == stored_feature["sketchId"])
                for key in ("planeSource", "planeAttachment"):
                    if key in feature: sketch[key] = deepcopy(feature[key])
            elif feature is not stored_feature and not stored_feature.get("planeReference"):
                for key in ("planeSource", "planeAttachment"):
                    if key in feature: stored_feature[key] = deepcopy(feature[key])
            if history and not history_registered:
                if op in {"box", "cylinder"}: history.primitive(feature, shape, vector, number)
                elif profile_semantic is not None:
                    from .cad_topology import planar_face_frame
                    history.register(feature_id, shape, {history.name(feature_id, [op, role]): [(face, planar_face_frame(face)) for face in faces] for role, faces in profile_semantic.items()})
                elif profile_parts is not None:
                    entries = {}
                    for local, builder, generated, part, workplane in profile_parts:
                        history.generated_profile(local, builder, generated, part, workplane)
                        for key, items in history.maps.get(local["id"], {}).get("face", {}).items(): entries.setdefault(key, []).extend(items)
                    history.profile_cleanup(feature_id, entries, profile_cleanup, shape)
                elif op in {"profile_extrude", "profile_revolve"}: history.generated_profile(feature, history_builder, generated_profile_faces, shape, plane)
                elif history_builder: history.through(feature_id, [feature["input"]], history_builder, shape, generated_edges)
                elif op in {"translate", "rotate", "mirror", "linear_pattern", "circular_pattern"}: history.transform(feature, shape, vector, number)
                elif op in {"surface_style", "surface_boundary"} and len(value.Faces()) == 1:
                    from .cad_topology import planar_face_frame
                    face = value.Faces()[0]
                    history.register(feature_id, shape, {history.name(feature_id, [op, "patch"]): [(face, planar_face_frame(face))]})
                else: history.carry_unchanged(feature_id, [feature["input"]] if "input" in feature else feature.get("inputs", []), shape)
            if history and "input" not in feature and "inputs" not in feature:
                history.register_exact(feature, shape)
            step = {"featureId": feature_id, "operation": op, "state": "succeeded", "solidCount": len(value.Solids()),
                    "volumeMm3": float(solid_volume), "durationMs": round((time.monotonic() - started) * 1000)}
            trace.append(step)
            if progress:
                progress(step)
        except Exception as exc:
            if isinstance(exc, PlanValidationError):
                exc.feature_id = feature_id
                raise
            raise PlanValidationError(f"{op} failed: {type(exc).__name__}: {exc}", feature_id=feature_id,
                                      code="feature_failed") from exc
    if canonical.get("annotations"):
        from .cad_pmi import resolve_plan_annotations
        resolve_plan_annotations(canonical, shapes, number, history)
    if canonical.get("bodyStates"):
        from .cad_body_state import resolve_body_states
        resolve_body_states(canonical, shapes[canonical["result"]], history)
    if history_output is not None: history_output.update(plan=canonical, history=history)
    return shapes[canonical["result"]], parameters, trace


def _build_worker_shape(request, progress):
    from .cad_history import TopologyHistory, authorize_bindings, selectors
    if not request.get("persistTopology"):
        return build_plan_shape(request["plan"], progress=progress, imported_assets=request.get("importedAssets"))
    baseline = request.get("bindingBasePlan")
    authorize_bindings(request["plan"], baseline)
    bound_base = baseline
    # A saved unbuildable draft without reused topology must remain repairable.
    original_features = {(collection, feature["id"]): feature for collection in ("features", "sketches", "annotations") for feature in (baseline or {}).get(collection, [])}
    needs_baseline = any("binding" not in value and (field, value) in list(selectors(original_features.get((collection, feature["id"]), {})))
                         for collection in ("features", "sketches", "annotations") for feature in request["plan"].get(collection, []) for field, value in selectors(feature))
    previous_bodies = {item["id"]: item for item in (baseline or {}).get("bodyStates", [])}
    needs_baseline = needs_baseline or any("binding" not in item and item.get("signature") == previous_bodies.get(item["id"], {}).get("signature")
                                        for item in request["plan"].get("bodyStates", []))
    if baseline and needs_baseline:
        output = {}
        build_plan_shape(baseline, progress=progress, history=TopologyHistory(persist=True, trusted_source=True), history_output=output, imported_assets=request.get("importedAssets"))
        bound_base = output["plan"]
    output = {}
    result = build_plan_shape(request["plan"], progress=progress,
                              history=TopologyHistory(persist=True, baseline=bound_base, original_baseline=baseline), history_output=output, imported_assets=request.get("importedAssets"))
    request["builtPlan"] = validate_plan(output["plan"])
    return result


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _inspect_draft_in_worker(request: dict[str, Any], progress_path: Path) -> dict[str, Any]:
    """Check construction with optional views, without delivery/source review."""
    plan = request["plan"]
    trace = []

    def record_progress(state: dict[str, Any]) -> None:
        if state.get("state") == "succeeded":
            trace.append(state)
        _write_result(progress_path, state)

    try:
        shape, parameters, trace = build_plan_shape(plan, progress=record_progress, imported_assets=request.get("importedAssets"))
        _write_result(progress_path, {"featureId": plan["result"], "operation": "inspect_draft", "state": "running"})
        value = shape.val()
        solid_count = len(value.Solids())
        valid = bool(value.isValid() and solid_count)
        if not valid:
            raise PlanValidationError("Draft OCCT solid is invalid", feature_id=plan["result"], code="invalid_geometry")
        bounds = value.BoundingBox()
        inspection = {"engine": "cadquery-occt", "kernelBacked": True, "valid": valid,
                      "scope": "draft_construction", "drawingAgreement": "not_checked", "solidCount": solid_count,
                      "bbox": {"min": [bounds.xmin, bounds.ymin, bounds.zmin],
                               "max": [bounds.xmax, bounds.ymax, bounds.zmax],
                               "size": [bounds.xlen, bounds.ylen, bounds.zlen]}}
        result = {"status": "succeeded", "valid": True, "errors": [], "plan": plan,
                  "resolvedParameters": parameters, "inspection": inspection, "featureTrace": trace, "artifacts": {}}
        if request.get("includeDraftProjections"):
            from .cad_inspector import export_projections

            _write_result(progress_path, {"featureId": plan["result"], "operation": "draft_projections", "state": "running"})
            # The application chooses outputDir; the plan cannot supply paths.
            # Keep construction views separate from model-export artifacts.
            result["draftViews"] = export_projections(shape, Path(request["outputDir"]) / "draft-views")
        if request.get("includeIsometric"):
            from .cad_inspector import export_spatial_views

            _write_result(progress_path, {"featureId": plan["result"], "operation": "spatial_diagnostic", "state": "running"})
            result["spatialViews"] = export_spatial_views(shape, Path(request["outputDir"]) / "spatial-views")
        return result
    except Exception as exc:
        result = _failure(exc, plan=plan)
        result["featureTrace"] = trace
        return result


def _execute_in_worker(request: dict[str, Any], progress_path: Path) -> dict[str, Any]:
    from .cad_inspector import export_projections, export_spatial_views, inspect_shape, inspect_step
    from .geometry import _mesh_from_cadquery, mesh_to_glb

    plan = request["plan"]
    output_dir = Path(request["outputDir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        shape, parameters, trace = _build_worker_shape(request, lambda state: _write_result(progress_path, state))
        plan = request.get("builtPlan", plan)
        _write_result(progress_path, {"featureId": plan["result"], "operation": "inspect", "state": "running"})
        from .cad_surface_features import SHEET_OPS
        allow_surfaces = any(feature["op"] in SHEET_OPS for feature in plan["features"])
        inspection = inspect_shape(shape, ray_probes=request.get("rayProbes"), parameters=parameters, allow_surfaces=allow_surfaces)
        if not inspection["valid"]:
            raise PlanValidationError("Final OCCT solid is invalid", feature_id=plan["result"], code="invalid_geometry")
        from cadquery import exporters
        step_path, glb_path = output_dir / "model.step", output_dir / "model.glb"
        # Export the actual topological result. Wrapping an inward shell in a
        # new compound via Workplane iteration can serialize only its shell in
        # some OCCT versions even though the in-memory result is a solid.
        exporters.export(shape.val(), str(step_path), exportType="STEP")
        readback = inspect_step(step_path, allow_surfaces=allow_surfaces)
        if (not readback.get("valid") or readback.get("solidCount") != inspection.get("solidCount")
                or not math.isclose(readback.get("volumeMm3", 0), inspection.get("volumeMm3", 0), rel_tol=2e-5, abs_tol=1e-6)
                or (allow_surfaces and not math.isclose(readback.get("surfaceAreaMm2", 0), inspection.get("surfaceAreaMm2", 0), rel_tol=2e-5, abs_tol=1e-6))
                or any(not math.isclose(a, b, rel_tol=2e-5, abs_tol=1e-5)
                       for a, b in zip(readback["bbox"]["size"], inspection["bbox"]["size"]))):
            raise PlanValidationError("STEP readback differs from the generated solid", feature_id=plan["result"], code="step_roundtrip_failed")
        inspection["stepReadback"] = {"valid": True, "solidCount": readback["solidCount"],
                                      "volumeMm3": readback["volumeMm3"], "bbox": readback["bbox"]}
        mesh = _mesh_from_cadquery(shape)
        if mesh is None:
            raise RuntimeError("OCCT tessellation failed; no substitute preview is generated")
        glb_path.write_bytes(mesh_to_glb(mesh, name=plan.get("name", "CAD model")))
        views = export_projections(shape, output_dir / "views") if request.get("includeProjections", True) else {}
        _write_result(output_dir / "plan.json", plan)
        _write_result(output_dir / "inspection.json", inspection)
        result = {"status": "succeeded", "valid": True, "errors": [], "plan": plan,
                "resolvedParameters": parameters, "inspection": inspection, "metrics": inspection, "featureTrace": trace,
                "artifacts": {"step": {"path": str(step_path), "mimeType": "application/step"},
                              "glb": {"path": str(glb_path), "mimeType": "model/gltf-binary"}, "views": views,
                              "plan": {"path": str(output_dir / "plan.json"), "mimeType": "application/json"}},
                "productionReady": False, "drawingAgreement": "not_checked"}
        if request.get("includeIsometric"):
            _write_result(progress_path, {"featureId": plan["result"], "operation": "spatial_diagnostic", "state": "running"})
            result["spatialViews"] = export_spatial_views(shape, output_dir / "spatial-views")
        return result
    except Exception as exc:
        return _failure(exc, plan=plan)


def _preview_in_worker(request: dict[str, Any], progress_path: Path) -> dict[str, Any]:
    from .cad_topology import export_topology_preview
    plan, output = request["plan"], Path(request["outputDir"])
    try:
        shape, parameters, trace = _build_worker_shape(request, lambda state: _write_result(progress_path, state))
        payload, glb = export_topology_preview(plan, shape, output)
        value = shape.val()
        inspection = {"engine": "cadquery-occt", "kernelBacked": True, "valid": bool(value.isValid()),
                      "solidCount": len(value.Solids()), "volumeMm3": float(sum(solid.Volume() for solid in value.Solids())),
                      "bbox": {**payload["bounds"], "size": [high-low for low, high in zip(payload["bounds"]["min"], payload["bounds"]["max"])]}}
        return {**payload, "status": "succeeded", "valid": True, "scope": "transaction_preview", "errors": [],
                "plan": request.get("builtPlan", plan),
                "inspection": inspection, "resolvedParameters": parameters, "featureTrace": trace,
                "drawingAgreement": "not_checked", "productionReady": False,
                "artifacts": {"glb": {"path": str(glb), "mimeType": "model/gltf-binary"}}}
    except Exception as exc:
        return _failure(exc, plan=plan)


def _apply_worker_limits(timeout: float, memory_bytes: int) -> None:
    """Set enforceable CPU, file, descriptor and supported address-space limits."""
    import resource

    cpu_seconds = max(1, math.ceil(timeout))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    # macOS's RLIMIT_AS is not reliably supported by the kernel. The parent
    # monitors resident memory there; Linux also enforces address space.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _run_plan_worker(plan: dict[str, Any], output_dir: str | Path, *, timeout_seconds: float,
                     ray_probes: list[dict[str, Any]] | None = None, memory_limit_mb: int = 4096,
                     draft_only: bool = False, include_draft_projections: bool = False,
                     include_isometric: bool = False, cancel_event=None, topology_preview: bool = False,
                     persist_topology: bool = False, binding_base_plan=None, include_projections: bool = True, imported_assets=None) -> dict[str, Any]:
    """Shared process isolation and resource bounds for both execution scopes."""
    canonical = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            from .metered_cad_provider import CadOperationCancelled
            raise CadOperationCancelled("CAD execution cancelled before launch")
        canonical = validate_plan(plan)
        resolve_parameters(canonical)
        if not 0.05 <= timeout_seconds <= 180:
            raise PlanValidationError("Worker timeout must be between 0.05 and 180 seconds")
        if not isinstance(memory_limit_mb, int) or isinstance(memory_limit_mb, bool) or not 256 <= memory_limit_mb <= 8192:
            raise PlanValidationError("Worker memory limit must be between 256 and 8192 MB")
        if not isinstance(include_draft_projections, bool):
            raise PlanValidationError("Draft projection option must be a boolean")
        if not isinstance(include_isometric, bool):
            raise PlanValidationError("Isometric diagnostic option must be a boolean")
        if not isinstance(include_projections, bool): raise PlanValidationError("Projection export option must be a boolean")
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        request = {"plan": canonical, "outputDir": str(destination), "rayProbes": ray_probes,
                   "timeoutSeconds": timeout_seconds, "memoryLimitMb": memory_limit_mb,
                   "scope": "topology_preview" if topology_preview else "draft_construction" if draft_only else "model_export",
                   "includeDraftProjections": draft_only and include_draft_projections,
                   "includeIsometric": include_isometric, "persistTopology": persist_topology, "includeProjections": include_projections,
                   "bindingBasePlan": binding_base_plan, "importedAssets": imported_assets}
        api_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="joyniu-cad-worker-") as temporary:
            directory = Path(temporary)
            request_path, result_path, progress_path = directory / "request.json", directory / "result.json", directory / "progress.json"
            _write_result(request_path, request)
            environment = {key: value for key, value in os.environ.items() if key in {
                "PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "JOYNIU_DISABLE_CADQUERY"}}
            environment.update({"PYTHONPATH": str(api_root), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
            started = time.monotonic()
            process = subprocess.Popen([sys.executable, "-m", "app.cad_executor", "--worker", str(request_path),
                                        "--result", str(result_path), "--progress", str(progress_path)],
                                       cwd=directory, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
            termination_code = None
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        termination_code = "user_cancelled"
                    if time.monotonic() - started > timeout_seconds:
                        termination_code = "worker_timeout"
                    if sys.platform == "darwin":
                        # ps is a system command with a generated numeric PID, not
                        # model input. RSS monitoring supplements macOS CPU limits.
                        try:
                            rss = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(process.pid)], capture_output=True, text=True, timeout=1)
                            if rss.returncode == 0 and int(rss.stdout.strip() or 0) > memory_limit_mb * 1024:
                                termination_code = "worker_memory_limit"
                        except (OSError, ValueError, subprocess.TimeoutExpired):
                            pass
                    if termination_code:
                        break
                    time.sleep(0.05)
            finally:
                # Also reap native workers when a request is interrupted or a
                # monitoring error occurs, not just on the normal timeout path.
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            elapsed = round((time.monotonic() - started) * 1000)
            if termination_code or not result_path.exists():
                progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
                error = PlanValidationError("CAD execution cancelled" if termination_code == "user_cancelled" else
                                            "CAD worker exceeded its time/memory budget" if termination_code else
                                            f"CAD worker stopped without a result (exit {process.returncode})",
                                            feature_id=progress.get("featureId"), code=termination_code or "worker_failed")
                result = _failure(error, plan=canonical)
            else:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            result["execution"] = {"isolatedProcess": True, "durationMs": elapsed, "timeoutSeconds": timeout_seconds,
                                   "memoryLimitMb": memory_limit_mb, "arbitraryCodeAllowed": False}
            return result
    except Exception as exc:
        return _failure(exc, plan=canonical)


def execute_cad_plan(plan: dict[str, Any], output_dir: str | Path, *, timeout_seconds: float = 60,
                     ray_probes: list[dict[str, Any]] | None = None, memory_limit_mb: int = 4096,
                     include_isometric: bool = False, cancel_event=None, persist_topology: bool = False, binding_base_plan=None, include_projections: bool = True, imported_assets=None) -> dict[str, Any]:
    """Build, measure and export in a disposable subprocess; return JSON only.

    ``output_dir`` is selected by the application, never taken from the plan.
    A failed build may leave partial files; its empty artifacts result prevents
    clients from presenting them as successfully generated deliverables.
    Optional isometric images are returned only under ``spatialViews``; the
    engineering ``artifacts.views`` remain front/top/right.
    """
    return _run_plan_worker(plan, output_dir, timeout_seconds=timeout_seconds, ray_probes=ray_probes,
                            memory_limit_mb=memory_limit_mb, include_isometric=include_isometric, cancel_event=cancel_event,
                            persist_topology=persist_topology, binding_base_plan=binding_base_plan, include_projections=include_projections, imported_assets=imported_assets)


def inspect_cad_draft(plan: dict[str, Any], output_dir: str | Path, *, timeout_seconds: float = 15,
                      memory_limit_mb: int = 4096, include_projections: bool = False,
                      include_isometric: bool = False, cancel_event=None) -> dict[str, Any]:
    """Build a partial plan in isolation; never assert delivery or drawing fit.

    A valid draft only proves that its current generic features construct valid
    OCCT solids. It says nothing about missing features, source dimensions,
    visual agreement or readiness for delivery. Optional ``draftViews`` contain
    actual OCCT SVG/PNG projections under the application-selected output_dir;
    they share the worker deadline and are never delivery artifacts. No STEP
    or GLB is produced. Failed/unfinished inspection exposes no draft views.
    ``include_isometric`` separately enables a ``spatialViews`` diagnostic;
    it never adds an isometric image to the engineering ``draftViews``.
    """
    result = _run_plan_worker(plan, output_dir, timeout_seconds=timeout_seconds,
                              memory_limit_mb=memory_limit_mb, draft_only=True,
                              include_draft_projections=include_projections, include_isometric=include_isometric, cancel_event=cancel_event)
    result.update({"scope": "draft_construction", "drawingAgreement": "not_checked", "artifacts": {}})
    result.setdefault("featureTrace", [])
    return result


execute_plan = execute_cad_plan


def preview_cad_plan(plan, output_dir, *, timeout_seconds=45, cancel_event=None, persist_topology=False, binding_base_plan=None, imported_assets=None):
    return _run_plan_worker(plan, output_dir, timeout_seconds=timeout_seconds, memory_limit_mb=4096,
                            topology_preview=True, cancel_event=cancel_event, persist_topology=persist_topology, binding_base_plan=binding_base_plan, imported_assets=imported_assets)


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--progress", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.worker).read_text(encoding="utf-8"))
    _apply_worker_limits(float(request["timeoutSeconds"]), int(request["memoryLimitMb"]) * 1024 * 1024)
    operation = _preview_in_worker if request.get("scope") == "topology_preview" else _inspect_draft_in_worker if request.get("scope") == "draft_construction" else _execute_in_worker
    _write_result(Path(args.result), operation(request, Path(args.progress)))


if __name__ == "__main__":
    _main()
