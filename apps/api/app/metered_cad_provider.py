"""Per-run provider instrumentation with cooperative cancellation.

One journal id is created at the actual provider boundary. Diagnostics are
snapshots of that call, not additional calls; inherited CAD traces are never
read for billing. Instrumented instances are private to one attempt.
"""
from __future__ import annotations

import copy
import inspect
import threading
from typing import Mapping
from uuid import uuid4


class CadOperationCancelled(RuntimeError):
    code = "user_cancelled"


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def normalized_usage(payload, diagnostics, outcome, usage_scope=None):
    """Prefer final envelope counts; fill missing breakdowns from diagnostics.

    Cached inputs are a subset of input and reasoning a subset of output. We
    preserve them separately without adding either to totals. No unknown count
    or supplier cost is silently converted to zero.
    """
    final = payload.get("usage", {}) if isinstance(payload, Mapping) else {}
    final = final if isinstance(final, Mapping) else {}
    transport = diagnostics.get("usage", {}) if isinstance(diagnostics, Mapping) else {}
    transport = transport if isinstance(transport, Mapping) else {}
    def known(*values):
        return next((count for value in values if (count := _count(value)) is not None), None)

    def details(value, key):
        return value.get(key) if isinstance(value.get(key), Mapping) else {}

    input_tokens = known(final.get("input_tokens"), transport.get("input_tokens"))
    output_tokens = known(final.get("output_tokens"), transport.get("output_tokens"))
    cached = known(details(final, "input_tokens_details").get("cached_tokens"), final.get("cached_input_tokens"), final.get("cached_tokens"),
                   details(transport, "input_tokens_details").get("cached_tokens"), transport.get("cached_input_tokens"), transport.get("cached_tokens"))
    reasoning = known(details(final, "output_tokens_details").get("reasoning_tokens"), final.get("reasoning_output_tokens"), final.get("reasoning_tokens"),
                      details(transport, "output_tokens_details").get("reasoning_tokens"), transport.get("reasoning_output_tokens"), transport.get("reasoning_tokens"))
    if input_tokens is not None and cached is not None and cached > input_tokens:
        cached = None
    if output_tokens is not None and reasoning is not None and reasoning > output_tokens:
        reasoning = None
    extra = {}
    writes = known(details(final, "input_tokens_details").get("cache_write_tokens"), final.get("cache_write_tokens"),
                   details(transport, "input_tokens_details").get("cache_write_tokens"), transport.get("cache_write_tokens"))
    if writes is not None:
        extra["cache_write_tokens"] = writes
    if usage_scope is not None:
        extra["usage_scope"] = usage_scope
    return {**extra, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cached_input_tokens": cached, "reasoning_output_tokens": reasoning,
            "outcome": outcome, "cost_micro_usd": None}


def _accepts_cancel(function):
    if getattr(function, "supports_cancellation", False):
        return True
    try:
        return "cancel_event" in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


class RunCallContext:
    def __init__(self, *, registry, billing, owner, job_id, attempt_id, cancel_event=None):
        self.registry, self.billing = registry, billing
        self.owner, self.job_id, self.attempt_id = owner, job_id, attempt_id
        self.cancel_event = cancel_event or threading.Event()
        self._condition = threading.Condition()
        self.active_calls = 0
        self.admission_check = None
        self.provider_check = None
        self.admission_failure = None

    def check(self):
        if self.cancel_event.is_set():
            raise CadOperationCancelled("任务已请求取消，不再执行后续步骤。")

    def is_idle(self):
        with self._condition:
            return self.active_calls == 0

    def guard(self, function):
        def guarded(*args, **kwargs):
            self.check()
            if _accepts_cancel(function):
                kwargs["cancel_event"] = self.cancel_event
            result = function(*args, **kwargs)
            self.check()
            return result
        return guarded


class MeteredCadProvider:
    supports_diagnostics = True

    def __init__(self, provider, context, stage="planner"):
        self.provider = provider
        self.context = context
        self.stage = stage
        from .cad_provider import provider_details
        self.provider_info = provider_details(provider)

    def is_configured(self):
        from .cad_provider import provider_is_configured
        return provider_is_configured(self.provider)

    def __call__(self, body, timeout, on_diagnostics=None, **kwargs):
        if not isinstance(body, Mapping):
            raise TypeError("Provider request must be a mapping")
        context = self.context
        call_id = "call_" + uuid4().hex
        diagnostics = {}
        payload = None
        outcome = "failed"
        stage = self.stage
        if stage == "planner":
            from .cad_source_questions import QUESTION_READING_PROMPT
            text = body.get("text")
            format_spec = text.get("format") if isinstance(text, Mapping) else None
            format_name = format_spec.get("name") if isinstance(format_spec, Mapping) else None
            if format_name == "joyniu_independent_drawing_review":
                stage = "drawing_review"
            elif body.get("instructions") == QUESTION_READING_PROMPT:
                stage = "source_question_review"
        identity = {"provider": str(self.provider_info.get("mode") or "unknown"),
                    "model": str(body.get("model") or self.provider_info.get("model") or "unknown")}
        started = False

        def receive(value):
            if isinstance(value, Mapping):
                diagnostics.update(value)
            if on_diagnostics:
                on_diagnostics(value)

        with context._condition:
            context.check()
            if context.admission_check is not None:
                context.admission_check()
                context.admission_check = None
            if context.provider_check is not None:
                context.provider_check(identity)
            context.active_calls += 1
        try:
            context.registry.begin_call(call_id=call_id, owner=context.owner, job_id=context.job_id,
                                        attempt_id=context.attempt_id, stage=stage, identity=identity)
            started = True
            call_kwargs = dict(kwargs)
            if getattr(self.provider, "supports_diagnostics", False) or getattr(self.provider, "__name__", "") == "_call_provider":
                call_kwargs["on_diagnostics"] = receive
            if _accepts_cancel(self.provider):
                call_kwargs["cancel_event"] = context.cancel_event
            payload = self.provider(body, timeout, **call_kwargs)
            outcome = "completed"
            context.check()
            return payload
        except BaseException as exc:
            if isinstance(getattr(exc, "diagnostics", None), Mapping):
                diagnostics.update(exc.diagnostics)
            outcome = "cancelled" if context.cancel_event.is_set() else "failed"
            raise
        finally:
            try:
                if started:
                    context.registry.finish_call(call_id, normalized_usage(payload, diagnostics, outcome, "aggregate" if identity["provider"] == "codex" else "request"))
                    context.registry.deliver_usage(context.billing, call_id=call_id)
            finally:
                with context._condition:
                    context.active_calls -= 1
                    context._condition.notify_all()


def instrument_runner(runner, context):
    """Clone wiring, never mutate a shared injected runner between users."""
    from . import ai_proxy
    from .cad_agent import _default_executor, _default_draft_inspector
    from .cad_executor import execute_cad_plan, inspect_cad_draft
    bound = copy.copy(runner)
    if hasattr(runner, "provider_call"):
        bound.provider_call = MeteredCadProvider(runner.provider_call or ai_proxy._call_provider, context)
    for name, stage in (("source_reader", "source_reader"), ("spatial_interpreter", "source_spatial")):
        component = getattr(runner, name, None)
        if component is not None and hasattr(component, "provider_call"):
            component = copy.copy(component)
            raw = component.provider_call or ai_proxy._call_provider
            component.provider_call = MeteredCadProvider(raw, context, stage)
            setattr(bound, name, component)
    for name in ("executor", "draft_inspector", "drawing_reviewer", "projection_comparer", "question_resolver"):
        function = getattr(runner, name, None)
        if not callable(function):
            continue
        if function is _default_executor:
            def execute(plan, output_dir, *, timeout_seconds, ray_probes=None):
                return execute_cad_plan(plan, output_dir, timeout_seconds=timeout_seconds, ray_probes=ray_probes,
                                        include_isometric=True, cancel_event=context.cancel_event)
            function = execute
        elif function is _default_draft_inspector:
            def inspect_draft(plan, output_dir, *, timeout_seconds):
                return inspect_cad_draft(plan, output_dir, timeout_seconds=timeout_seconds,
                                         include_projections=True, include_isometric=True, cancel_event=context.cancel_event)
            function = inspect_draft
        setattr(bound, name, context.guard(function))
    return bound
