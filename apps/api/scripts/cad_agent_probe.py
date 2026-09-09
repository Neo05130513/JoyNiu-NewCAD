"""Run the real CAD agent on an arbitrary drawing or text specification.

This probe injects neither a recipe nor expected dimensions. Provider settings
must already be in the environment; --env-file is an explicit local option.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drawing", type=Path)
    parser.add_argument("--message", default="请根据图纸建立可编辑的 CAD 模型，核对实际尺寸与结构，必要信息不明确时提问。")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", type=Path, help="Resume a saved agent-state/checkpoint; drawing hashes are checked before transcription reuse")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    from app.ai_proxy import AIFile
    from app.cad_agent import CadAgentService
    files = []
    if args.drawing:
        files.append(AIFile(args.drawing.name, mimetypes.guess_type(args.drawing.name)[0] or "application/octet-stream", args.drawing.read_bytes()))
    state = None
    if args.resume:
        saved = json.loads(args.resume.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            parser.error("--resume must contain a saved JSON object")
        state = saved.get("state", saved)
    args.output.mkdir(parents=True, exist_ok=True)
    def progress(event):
        # Keep the transcript small and credential-free. Detailed observations
        # and actual artifacts are stored by the service in its output folder.
        print(json.dumps({key: event[key] for key in ("stage", "iteration", "message") if key in event}, ensure_ascii=False), flush=True)
    result = CadAgentService().run(message=args.message, files=files, state=state, output_dir=args.output,
                                  max_turns=args.max_turns, timeout_seconds=args.timeout, progress=progress)
    print(json.dumps({"status": result["status"], "questions": result["questions"],
                      "elapsedSeconds": result["elapsedSeconds"], "provider": result["provider"],
                      "geometryGenerated": bool(result.get("artifacts", {}).get("step")),
                      "acceptance": (result.get("inspection") or {}).get("acceptance", {}).get("status")}, ensure_ascii=False), flush=True)
    return 0 if result["status"] in {"review_required", "needs_input"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
