"""Meter one real, unseeded CAD conversion without changing the application.

Only numeric usage and safe request metadata are captured at the provider
boundary. Every retry is a separate event; diagnostics update that event rather
than being counted as additional calls. Source drawings are never modified.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sys
import threading
import time
from typing import Mapping
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def safe_usage(value) -> dict:
    if not isinstance(value, Mapping):
        return {}
    keys = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens",
            "cached_input_tokens", "reasoning_tokens", "reasoning_output_tokens"}
    result = {key: item for key, item in value.items()
              if key in keys and type(item) is int and 0 <= item <= 10**12}
    for outer, inner in (("input_tokens_details", "cached_tokens"),
                         ("output_tokens_details", "reasoning_tokens")):
        nested = value.get(outer)
        item = nested.get(inner) if isinstance(nested, Mapping) else None
        if type(item) is int and 0 <= item <= 10**12:
            result[outer] = {inner: item}
    return result


def request_stage(body: Mapping) -> str:
    instructions = body.get("instructions", "")
    if not isinstance(instructions, str):
        return "unknown"
    for prefix, stage in (("只转录图片", "source_reading"),
                          ("只从原图像素", "source_spatial"),
                          ("你是独立工程图复核员", "drawing_review"),
                          ("独立检查拟向用户", "question_rereading"),
                          ("You are", "modeling")):
        if instructions.startswith(prefix):
            return stage
    return "modeling"


class MeteredProvider:
    supports_diagnostics = True

    def __init__(self, delegate, output: Path, run_id: str):
        self.delegate, self.output, self.run_id = delegate, output, run_id
        self.events: dict[str, dict] = {}
        self.deadlines: dict[str, float] = {}
        self.lock = threading.Lock()

    @property
    def provider_info(self):
        return dict(self.delegate.provider_info)

    def _persist(self):
        write_json(self.output / "usage-events.json", list(self.events.values()))

    def __call__(self, body, timeout, on_diagnostics=None):
        begin = time.monotonic()
        with self.lock:
            event_id = f"{self.run_id}:call-{len(self.events) + 1:03d}"
            event = {"event_id": event_id, "provider": self.provider_info["mode"],
                     "model": self.provider_info["model"], "stage": request_stage(body),
                     "reasoning_effort": (body.get("reasoning") or {}).get("effort"),
                     "started_at": datetime.now(timezone.utc).isoformat(),
                     "timeout_seconds": round(timeout, 3), "status": "pending", "usage": {}}
            self.events[event_id] = event
            self.deadlines[event_id] = begin + timeout
            self._persist()

        def receive(value):
            with self.lock:
                usage = safe_usage(value.get("usage")) if isinstance(value, Mapping) else {}
                if usage:
                    event["usage"].update(usage)
                for key in ("terminalStatus", "errorCategory", "eventCount", "responseBytes"):
                    item = value.get(key) if isinstance(value, Mapping) else None
                    if isinstance(item, (str, int)):
                        event.setdefault("diagnostics", {})[key] = item
                event["elapsed_seconds"] = round(time.monotonic() - begin, 4)
                self._persist()
            if on_diagnostics is not None:
                on_diagnostics(value)

        try:
            payload = self.delegate(body, timeout, on_diagnostics=receive)
            with self.lock:
                event["usage"].update(safe_usage(payload.get("usage")))
                event["status"] = "completed"
            return payload
        except Exception as error:
            diagnostics = getattr(error, "diagnostics", None)
            if isinstance(diagnostics, Mapping):
                receive(diagnostics)
            with self.lock:
                event["status"] = "failed"
                event["error_type"] = type(error).__name__
            raise
        finally:
            with self.lock:
                event["elapsed_seconds"] = round(time.monotonic() - begin, 4)
                self._persist()
                compact = {key: event.get(key) for key in
                           ("event_id", "stage", "status", "elapsed_seconds", "usage")}
            print(json.dumps(compact, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drawing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be an empty directory; use a new name for a repeat")
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["JOYNIU_CAD_PROVIDER"] = "codex"
    os.environ["JOYNIU_CAD_CODEX_MODEL"] = args.model
    os.environ["JOYNIU_LLM_REASONING_EFFORT"] = args.reasoning_effort
    from app.ai_proxy import AIFile
    from app.cad_agent import CadAgentService
    from app.cad_codex_provider import CodexCadProvider

    data = args.drawing.read_bytes()
    provider = MeteredProvider(CodexCadProvider(), args.output, "cost_" + uuid4().hex)
    manifest = {"version": "cad-cost-probe-v2", "run_id": provider.run_id,
                "source_path": str(args.drawing.resolve()),
                "source_sha256": hashlib.sha256(data).hexdigest(), "source_bytes": len(data),
                "provider": provider.provider_info, "max_turns": args.max_turns,
                "timeout_seconds": args.timeout, "resume": False,
                "source_sent_as": "drawing" + args.drawing.suffix.lower(),
                "cost_basis": "usage_only_no_supplier_invoice_or_rate_assumed",
                "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(args.output / "manifest.json", manifest)
    event_file = args.output / "progress.jsonl"
    def progress(value):
        safe = {key: value[key] for key in ("stage", "iteration") if key in value}
        safe["at"] = datetime.now(timezone.utc).isoformat()
        with event_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, ensure_ascii=False) + "\n")
        print(json.dumps(safe, ensure_ascii=False), flush=True)

    begin = time.monotonic()
    try:
        # Generic filename and request keep sampling labels and reference answers
        # out of the actual inference. The source itself is the only evidence.
        result = CadAgentService(provider_call=provider).run(
            message="请根据图纸建立可编辑的 CAD 模型，核对实际尺寸与结构，必要信息不明确时提问。",
            files=[AIFile(manifest["source_sent_as"], mimetypes.guess_type(args.drawing.name)[0] or "application/octet-stream", data)],
            state=None, output_dir=args.output / "agent", max_turns=args.max_turns,
            timeout_seconds=args.timeout, progress=progress)
        write_json(args.output / "result.json", result)
        summary = {"status": result.get("status"), "elapsed_seconds": result.get("elapsedSeconds"),
                   "questions": result.get("questions", []), "provider": result.get("provider"),
                   "geometry_generated": bool((result.get("artifacts") or {}).get("step")),
                   "artifacts": result.get("artifacts"),
                   "acceptance": ((result.get("inspection") or {}).get("acceptance") or {}).get("status"),
                   "drawing_review": (result.get("drawingReview") or {}).get("status"),
                   "human_verified": False}
    except Exception as error:
        summary = {"status": "probe_error", "error_type": type(error).__name__,
                   "elapsed_seconds": round(time.monotonic() - begin, 3), "human_verified": False}
    # A source-spatial request can outlive an early needs_input result. Preserve
    # its ORIGINAL bounded deadline instead of cutting it off for the probe.
    with provider.lock:
        drain_deadline = max((provider.deadlines[key] for key, event in provider.events.items()
                              if event["status"] == "pending"), default=time.monotonic()) + 5
    while time.monotonic() < drain_deadline:
        with provider.lock:
            pending = any(e["status"] == "pending" for e in provider.events.values())
        if not pending:
            break
        time.sleep(.2)
    with provider.lock:
        summary["provider_calls"] = len(provider.events)
        summary["pending_provider_calls"] = sum(e["status"] == "pending" for e in provider.events.values())
        summary["usage_capture_complete"] = summary["pending_provider_calls"] == 0
        summary["probe_wall_seconds"] = round(time.monotonic() - begin, 3)
        snapshot = copy.deepcopy(list(provider.events.values()))
    try:
        from app.cad_usage_accounting import aggregate_usage_events
        write_json(args.output / "usage-summary.json", aggregate_usage_events(snapshot))
    except ImportError:
        summary["usage_summary_pending"] = True
    write_json(args.output / "summary.json", summary)
    print(json.dumps({"completed_probe": args.output.name, **summary}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
