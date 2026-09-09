"""Execute restricted CAD plans in a bounded, disposable worker process.

No model-supplied Python, module imports, paths, selectors or shell commands
are executed. The process boundary limits native OCCT failures and cost; it
is not advertised as an operating-system security sandbox for arbitrary code.
"""

from __future__ import annotations

import argparse
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


def build_plan_shape(plan: dict[str, Any], *, progress: Callable[[dict[str, Any]], None] | None = None) -> tuple[Any, dict[str, float], list[dict[str, Any]]]:
    """Worker-level builder; application callers use the isolated public APIs."""
    from .geometry import get_cadquery

    canonical = validate_plan(plan, allow_unresolved=False)
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

    for feature in canonical["features"]:
        feature_id, op = feature["id"], feature["op"]
        started = time.monotonic()
        if progress:
            progress({"featureId": feature_id, "operation": op, "state": "running"})
        try:
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
                current_point = vector(feature["start"])
                wire = cq.Workplane(feature["plane"], origin=origin).moveTo(*current_point)
                for segment_index, segment in enumerate(feature["segments"]):
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
                wire = wire.close()
                if op == "profile_extrude":
                    distance = number(feature["distance"])
                    if abs(distance) < 1e-7:
                        raise PlanValidationError("Profile extrusion distance must be nonzero")
                    shape = wire.extrude(distance)
                else:
                    angle = positive(feature.get("angle", 360), "revolve angle")
                    if angle > 360:
                        raise PlanValidationError("Revolve angle must be at most 360 degrees")
                    start, end = vector(feature["axisStart"]), vector(feature["axisEnd"])
                    if math.dist(start, end) < 1e-7:
                        raise PlanValidationError("Revolve axis requires distinct points")
                    shape = wire.revolve(angle, start, end)
            elif op in {"union", "cut", "intersect"}:
                shape = shapes[feature["inputs"][0]]
                for reference in feature["inputs"][1:]:
                    shape = getattr(shape, op)(shapes[reference])
            elif op == "translate":
                shape = shapes[feature["input"]].translate(vector(feature["vector"]))
            elif op == "fillet":
                selectors = {"all": None, "parallelX": "|X", "parallelY": "|Y", "parallelZ": "|Z"}
                selector = selectors[feature["edges"]]
                shape = shapes[feature["input"]].edges(selector).fillet(positive(feature["radius"], "fillet radius"))
            else:  # Defensive; validate_plan already limits operation names.
                raise PlanValidationError(f"Unsupported CAD operation: {op}")
            shape = shape.clean()
            value = shape.val()
            if not value.Solids() or not value.isValid() or value.Volume() <= 1e-9:
                raise PlanValidationError("Feature produced an empty or invalid solid", code="invalid_geometry")
            if len(value.Faces()) > 10_000:
                raise PlanValidationError("Feature exceeds the 10,000 face complexity limit", code="resource_limit")
            shapes[feature_id] = shape
            step = {"featureId": feature_id, "operation": op, "state": "succeeded", "solidCount": len(value.Solids()),
                    "volumeMm3": float(value.Volume()), "durationMs": round((time.monotonic() - started) * 1000)}
            trace.append(step)
            if progress:
                progress(step)
        except Exception as exc:
            if isinstance(exc, PlanValidationError):
                exc.feature_id = feature_id
                raise
            raise PlanValidationError(f"{op} failed: {type(exc).__name__}: {exc}", feature_id=feature_id,
                                      code="feature_failed") from exc
    return shapes[canonical["result"]], parameters, trace


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
        shape, parameters, trace = build_plan_shape(plan, progress=record_progress)
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
    from .cad_inspector import export_projections, export_spatial_views, inspect_shape
    from .geometry import _mesh_from_cadquery, mesh_to_glb

    plan = request["plan"]
    output_dir = Path(request["outputDir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        shape, parameters, trace = build_plan_shape(plan, progress=lambda state: _write_result(progress_path, state))
        _write_result(progress_path, {"featureId": plan["result"], "operation": "inspect", "state": "running"})
        inspection = inspect_shape(shape, ray_probes=request.get("rayProbes"), parameters=parameters)
        if not inspection["valid"]:
            raise PlanValidationError("Final OCCT solid is invalid", feature_id=plan["result"], code="invalid_geometry")
        from cadquery import exporters
        step_path, glb_path = output_dir / "model.step", output_dir / "model.glb"
        exporters.export(shape, str(step_path), exportType="STEP")
        mesh = _mesh_from_cadquery(shape)
        if mesh is None:
            raise RuntimeError("OCCT tessellation failed; no substitute preview is generated")
        glb_path.write_bytes(mesh_to_glb(mesh, name=plan.get("name", "CAD model")))
        views = export_projections(shape, output_dir / "views")
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
                     include_isometric: bool = False) -> dict[str, Any]:
    """Shared process isolation and resource bounds for both execution scopes."""
    canonical = None
    try:
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
        destination = Path(output_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        request = {"plan": canonical, "outputDir": str(destination), "rayProbes": ray_probes,
                   "timeoutSeconds": timeout_seconds, "memoryLimitMb": memory_limit_mb,
                   "scope": "draft_construction" if draft_only else "model_export",
                   "includeDraftProjections": draft_only and include_draft_projections,
                   "includeIsometric": include_isometric}
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
                error = PlanValidationError("CAD worker exceeded its time/memory budget" if termination_code else
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
                     include_isometric: bool = False) -> dict[str, Any]:
    """Build, measure and export in a disposable subprocess; return JSON only.

    ``output_dir`` is selected by the application, never taken from the plan.
    A failed build may leave partial files; its empty artifacts result prevents
    clients from presenting them as successfully generated deliverables.
    Optional isometric images are returned only under ``spatialViews``; the
    engineering ``artifacts.views`` remain front/top/right.
    """
    return _run_plan_worker(plan, output_dir, timeout_seconds=timeout_seconds, ray_probes=ray_probes,
                            memory_limit_mb=memory_limit_mb, include_isometric=include_isometric)


def inspect_cad_draft(plan: dict[str, Any], output_dir: str | Path, *, timeout_seconds: float = 15,
                      memory_limit_mb: int = 4096, include_projections: bool = False,
                      include_isometric: bool = False) -> dict[str, Any]:
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
                              include_draft_projections=include_projections, include_isometric=include_isometric)
    result.update({"scope": "draft_construction", "drawingAgreement": "not_checked", "artifacts": {}})
    result.setdefault("featureTrace", [])
    return result


execute_plan = execute_cad_plan


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--progress", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.worker).read_text(encoding="utf-8"))
    _apply_worker_limits(float(request["timeoutSeconds"]), int(request["memoryLimitMb"]) * 1024 * 1024)
    operation = _inspect_draft_in_worker if request.get("scope") == "draft_construction" else _execute_in_worker
    _write_result(Path(args.result), operation(request, Path(args.progress)))


if __name__ == "__main__":
    _main()
