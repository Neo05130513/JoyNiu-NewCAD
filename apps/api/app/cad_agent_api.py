"""HTTP boundary for the tool-using CAD agent, its revisions and deliverables."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Mapping
import unicodedata
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .ai_proxy import AIFile, MAX_FILE_BYTES
from .cad_agent_store import (CadRunConflict, CadRunStore, PROCESS_INSTANCE, comparison_policy, source_question_reviews,
                              MAX_REVISION_REQUESTS, MAX_REVISION_REQUEST_CHARACTERS)
from .cad_acceptance import acceptance_ray_probes, evaluate_cad_acceptance
from .download_headers import attachment_content_disposition
from .platform import Permission, PlatformError
from .platform_api import _anonymous_ai_request_allowed, _domain_http_exception, _parse_ai_history, _token_user


_LOGGER = logging.getLogger("joyniu.cad_agent_jobs")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


_PROVIDER_IDENTITY_FIELDS = ("mode", "name", "model", "reasoningEffort", "configured", "streaming", "authentication")
_PROVIDER_COUNT_FIELDS = ("attempts", "retryCount", "sourceReaderAttempts", "sourceReadingPasses", "sourceSpatialAttempts")


def _public_provider(value: Any) -> dict[str, Any] | None:
    """Only engine identity and bounded counters belong in a run response.

    Provider objects may also hold endpoints, executable paths or credentials;
    none of those fields are copied into durable/public job metadata.
    """
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in ("name", "model", "lastErrorCode"):
        item = value.get(key)
        if (isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item)
                and not item.casefold().startswith(("sk-", "sk_", "bearer", "token-", "token_"))):
            result[key] = item
    for key, choices in (
        ("mode", {"codex", "remote", "local-fallback", "unknown"}),
        ("reasoningEffort", {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
        ("authentication", {"cli-managed", "api-key", "managed"}),
    ):
        item = value.get(key)
        if isinstance(item, str) and item in choices:
            result[key] = item
    for key in ("configured", "streaming"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in _PROVIDER_COUNT_FIELDS:
        if type(value.get(key)) is int and 0 <= value[key] <= 1_000_000:
            result[key] = value[key]
    return result


def _runner_provider(runner: Any) -> dict[str, Any]:
    from .cad_provider import provider_details
    if hasattr(runner, "provider_call"):
        details = provider_details(runner.provider_call)
    else:
        # Older injected services need not implement the provider interface.
        details = getattr(runner, "provider_info", {"mode": "unknown"})
    safe = _public_provider(details) or {"mode": "unknown"}
    return {key: safe[key] for key in _PROVIDER_IDENTITY_FIELDS if key in safe}


def _completed_provider(bound: Mapping[str, Any], reported: Any) -> dict[str, Any]:
    safe = _public_provider(reported) or {}
    return {**copy.deepcopy(dict(bound)),
            **{key: safe[key] for key in (*_PROVIDER_COUNT_FIELDS, "lastErrorCode") if key in safe}}


def _explicit_geometry_revision(message: str, plan: Mapping[str, Any] | None = None) -> bool:
    """Recognize a deliberately small set of unambiguous design edits.

    A continuation, source correction, question or client plan delta is not
    permission to differ from the original. Unknown wording stays strict.
    These local patterns never decide whether the resulting geometry is right;
    measurement and independent review still apply to the recorded request.
    """
    text = unicodedata.normalize("NFKC", message).casefold().strip()
    if not text:
        return False
    # Only remove a complete, trailing "keep the other geometry unchanged"
    # clause for intent classification. The unmodified message is still stored
    # in the request ledger, so review must also enforce this restriction.
    remainder = r"(?:dimensions|geometry|features|structure)"
    preserve_other_suffixes = (
        r"[,;。]\s*(?:保持)?(?:其他|其余)(?:(?:尺寸|结构|几何|特征)(?:[与和及、](?:尺寸|结构|几何|特征))?)?\s*(?:按(?:照)?(?:原图|图纸)(?:保持)?不变|(?:保持)?与(?:原图|图纸)一致|(?:保持)?不变|不要(?:修改|改变|改动))\s*[。.!]?$",
        r"[,;]\s*(?:keep|leave|preserve)\s+(?:all\s+)?(?:(?:other|remaining)\s+" + remainder + r"(?:\s+and\s+" + remainder + r")?|everything else)\s+(?:unchanged(?:\s+from\s+(?:the\s+)?(?:original\s+)?drawing)?|as\s+(?:(?:shown|specified)\s+)?in\s+(?:the\s+)?(?:original\s+)?drawing)\s*[.!]?$",
        r"[,;]\s*do not\s+(?:change|modify)\s+(?:any\s+)?(?:other|remaining)\s+" + remainder + r"\s*[.!]?$",
    )
    for suffix in preserve_other_suffixes:
        match = re.search(suffix, text)
        if match:
            text = text[:match.start()].rstrip()
            break
    # A mixed or ambiguous instruction cannot grant a new exception. Treat
    # references back to a drawing conservatively as reproduction, including
    # corrections of misread dimensions; those must keep the contour gate.
    preserve_source = (
        r"(?:原图|图纸)",
        r"(?:纠正|修正|修复|误读|错读|漏掉|漏识别|补齐|补全)",
        r"\b(?:original|source|drawing)\b",
        r"\b(?:correct|fix|repair|misread|missing|omitted)\b",
        r"(?:不要|不得|禁止|不允许|无需|不需要|不应|不必|勿|别).{0,10}(?:改|变|调整|增|减|加|删|移|旋转)",
        r"\b(?:do not|don't|dont|must not|never|without|no need to)\b.{0,30}\b(?:change|modify|edit|redesign|resize|adjust|add|remove|delete|move|rotate|increase|decrease)\b",
        r"(?:[?？]|如果|假如|假设|例如|举例|是否|能否|可否|要不要|会怎样|解释)",
        r"\b(?:if|suppose|example|explain|should|would|could|whether)\b",
        r"(?:标注|注释|文字|标签|颜色|材质|命名|名称|报告|说明)",
        r"\b(?:annotations?|labels?|notes?|reports?|text|metadata|material|colou?r|description)\b",
    )
    if any(re.search(pattern, text) for pattern in preserve_source):
        return False
    dimensions = [r"厚度|板厚|壁厚|长度|总长|板长|宽度|板宽|高度|总高|直径|半径|孔径|孔距|中心距|间距|间隙|深度|槽宽|槽深|槽长|角度|偏移量",
                  r"\b(?:thickness|width|height|length|diameter|radius|pitch|depth|spacing|gap|angle|offset)\b"]
    features = [r"通孔|盲孔|孔|凹槽|槽|凸台|筋板|加强筋|圆角|倒角|安装耳|凸缘|法兰|支脚|台阶",
                r"\b(?:holes?|slots?|boss(?:es)?|ribs?|fillets?|chamfers?|flanges?|lugs?|pockets?|grooves?|steps?)\b"]
    if isinstance(plan, Mapping):
        parameters = plan.get("parameters")
        for name, parameter in (parameters.items() if isinstance(parameters, Mapping) else []):
            if isinstance(name, str) and name:
                dimensions.append(r"(?<![a-z0-9_])" + re.escape(name.casefold()) + r"(?![a-z0-9_])")
            if isinstance(parameter, Mapping):
                for key in ("label", "name"):
                    label = parameter.get(key)
                    if isinstance(label, str) and label:
                        dimensions.append(re.escape(label.casefold()))
        for feature in plan.get("features") if isinstance(plan.get("features"), list) else []:
            if isinstance(feature, Mapping) and isinstance(feature.get("id"), str) and feature["id"]:
                features.append(r"(?<![a-z0-9_])" + re.escape(feature["id"].casefold()) + r"(?![a-z0-9_])")
    dimension, feature = "(?:" + "|".join(dimensions) + ")", "(?:" + "|".join(features) + ")"
    number = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"
    edits = (
        dimension + r"[^。;；\n]{0,40}(?:改为|改成|改到|调整为|调整到|设为|设置为|增至|减至|加厚到|减薄到)\s*" + number,
        r"(?:增加|增大|减少|减小|扩大|缩小)\s*" + dimension + r"[^。;；\n]{0,12}" + number,
        r"\b(?:increase|decrease|change|set|resize|adjust|enlarge|reduce|extend|shorten|widen|narrow|thicken)\b[^.;\n]{0,40}" + dimension + r"[^;\n]{0,35}\b(?:to|by|from)\s*" + number,
        r"(?:新增|增加|添加|加上|去掉|删除|移除|取消)\s*(?:(?:一个|两个|三个|所有|全部|现有|新的|新|左侧|右侧|顶部|底部|贯穿|矩形|圆形|\d+\s*个?)\s*){0,5}" + feature,
        r"\b(?:add|delete|remove)\s+(?:(?:a|an|the|one|two|three|all|both|existing|new|through|blind|mounting|circular|rectangular|left|right|top|bottom|\d+)\s+){0,5}" + feature,
        feature + r"[^。;；\n]{0,25}(?:移动|平移|旋转|偏移)[^。;；\n]{0,20}" + number,
        r"\b(?:move|translate|rotate)\b[^.;\n]{0,25}" + feature + r"[^;\n]{0,30}" + number,
    )
    return any(re.search(pattern, text) for pattern in edits)


def _artifact_entries(artifacts: Mapping[str, Any]):
    for kind in ("step", "glb"):
        if artifacts.get(kind):
            yield kind, kind, None, artifacts[kind]
    for view, artifact in (artifacts.get("views") or {}).items():
        if isinstance(artifact, Mapping) and artifact.get("path"):
            yield f"view-{view}", Path(artifact["path"]).suffix.lstrip("."), view, artifact


def _drawing_review_issue(record: Mapping[str, Any], plan: Mapping[str, Any] | None = None) -> str | None:
    """Use only durable server evidence, never a confirmation payload claim."""
    review = record.get("drawingReview")
    if (not isinstance(review, Mapping) or not isinstance(review.get("status"), str)
            or review.get("status") not in {"consistent", "not_applicable", "human_confirmed"}
            or review.get("differences") or review.get("questions") or review.get("errorCode") or record.get("questions")):
        return "图纸或设计对照仍有未解决的问题，请先继续检查和修改。"
    state = record.get("state")
    has_drawing = bool(record.get("files") or record.get("sourceFiles")
                       or (state.get("sourceFiles") if isinstance(state, Mapping) else None))
    if not has_drawing:
        return None
    required = "原图模型尚未通过当前计划的独立图纸复核，请继续对话重新检查；旧自评或人工确认不能替代该复核。"
    if review.get("status") == "not_applicable":
        return required
    independent = review.get("independentReview")
    if (not isinstance(independent, Mapping) or independent.get("source") != "independent_drawing_review"
            or independent.get("status") != "consistent" or independent.get("errorCode")
            or independent.get("differences") != [] or independent.get("questions") != []):
        return required
    current = plan if plan is not None else record.get("plan")
    if not isinstance(current, Mapping) or not current.get("features") or not current.get("result"):
        return required
    try:
        plan_hash = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        return required
    if independent.get("planHash") != plan_hash:
        return required
    comparison = review.get("projectionComparison")
    # Only the durable API policy grants this scope. Agent state is recoverable
    # evidence, not an alternative authority when this field is absent.
    policy = comparison_policy(record.get("comparisonPolicy"))
    original_fidelity = policy["mode"] != "user_revision"
    if isinstance(comparison, Mapping):
        if comparison.get("planHash") != plan_hash:
            return "轮廓核对记录不属于当前模型，请重新执行原图与实体检查。"
        if original_fidelity and any(isinstance(item, Mapping) and item.get("status") == "mismatch" and item.get("reliability") == "high"
               for item in comparison.get("views", [])):
            return "原图与模型仍有明确轮廓差异，请修正模型后再确认交付。"
    elif record.get("sourceSpatialContract") or (isinstance(state, Mapping) and state.get("sourceSpatialContract")):
        return "当前图纸模型尚未完成轮廓核对，请重新执行原图与实体检查。"
    observations = independent.get("observations")
    if not isinstance(observations, list) or not observations:
        return required
    fields = ("sourceImageId", "sourceLocation", "modelImageId", "modelLocation", "finding")
    for observation in observations:
        if (not isinstance(observation, Mapping)
                or any(not isinstance(observation.get(key), str) or not observation[key].strip() for key in fields)
                or not isinstance(observation.get("confidence"), str) or observation["confidence"] not in {"high", "medium"}):
            return required
    return None


def _step_export_issue(record: Mapping[str, Any]) -> str | None:
    if record.get("status") != "ready":
        return "请先确认当前模型的数据，再导出 STEP。"
    if issue := _drawing_review_issue(record):
        return issue
    inspection = record.get("inspection")
    if (not isinstance(inspection, dict) or inspection.get("valid") is not True
            or inspection.get("kernelBacked") is not True or inspection.get("engine") != "cadquery-occt"):
        return "已存版本缺少真实实体检查，请重新检查后导出 STEP。"
    state = record.get("state")
    observations = record.get("observations") or (state.get("observations") if isinstance(state, Mapping) else None) or []
    try:
        if evaluate_cad_acceptance(inspection, observations)["status"] != "passed":
            return "已存版本未通过当前完整尺寸检查，请重新执行检查后导出 STEP。"
    except (TypeError, ValueError, KeyError):
        return "已存版本的尺寸检查证据不完整，请重新执行检查后导出 STEP。"
    return None


def _public_result(record: dict[str, Any], prefix: str) -> dict[str, Any]:
    result = {key: copy.deepcopy(record.get(key)) for key in (
        "runId", "revision", "status", "message", "plan", "inspection", "questions",
        "trace", "drawingReview", "provider", "createdAt", "confirmedAt", "parentRunId",
        "observations", "resolvedParameters", "sourceTranscription", "sourceSpatialContract", "sourceQuestionReviews", "projectionComparison", "comparisonPolicy", "draftInspection",
        "progress", "updatedAt", "completedAt",
    )}
    result["provider"] = _public_provider(record.get("provider"))
    if isinstance(result.get("progress"), dict):
        result["progress"]["provider"] = copy.deepcopy(result["provider"])
    result["sourceQuestionReviews"] = source_question_reviews(record.get("sourceQuestionReviews", (record.get("state") or {}).get("sourceQuestionReviews")))
    result["artifacts"] = []
    step_issue = _step_export_issue(record)
    if record.get("status") == "ready" and step_issue:
        result["deliveryBlockedReason"] = step_issue
    for key, fmt, view, artifact in _artifact_entries(record.get("artifacts") or {}):
        # STEP is never exposed from an unconfirmed candidate, including by
        # guessing its artifact endpoint. GLB and projections are draft views.
        if fmt == "step" and step_issue:
            continue
        url = f"{prefix}/cad-agent/runs/{record['runId']}/{record['revision']}/artifacts/{key}?access={record['downloadToken']}"
        path = Path(artifact["path"])
        result["artifacts"].append({
            "id": key, "format": fmt, "view": view, "url": url, "downloadUrl": url,
            "filename": path.name, "mimeType": artifact.get("mimeType"),
            "engine": "cadquery-occt", "productionReady": fmt == "step" and record["status"] == "ready",
        })
    result["sourceFiles"] = [{key: item[key] for key in ("filename", "contentType", "sha256")} for item in record.get("files", [])]
    return result


def create_cad_agent_router(services, store: CadRunStore, *, prefix: str = "/api/v1", agent=None, executor=None, service_factory=None):
    # Lazy imports keep API tests able to inject a deterministic provider and
    # keep the existing recipe service independent from optional CAD tooling.
    from .cad_agent import CadAgentService
    from .cad_executor import execute_cad_plan

    make_runner = service_factory or CadAgentService
    execute = executor or execute_cad_plan
    background_tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            # A browser disconnect is not a shutdown. Only an actual API
            # worker shutdown interrupts jobs and saves their checkpoints.
            pending = list(background_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    router = APIRouter(prefix=prefix, tags=["cad-agent"], lifespan=lifespan)

    def owner_for(request: Request, authorization: str | None) -> str:
        try:
            if authorization:
                actor = _token_user(services, authorization)
                services.auth.require(actor, Permission.AI_CHAT)
                return actor.id
            if services.ai.allow_anonymous and _anonymous_ai_request_allowed(request):
                return "local-anonymous"
            raise HTTPException(401, "bearer token is required")
        except PlatformError as exc:
            raise _domain_http_exception(exc)

    def owned(run_id: str, owner: str, revision: int | None = None):
        try:
            record = store.load(run_id, revision)
        except (KeyError, ValueError):
            raise HTTPException(404, "CAD run not found")
        if record["owner"] != owner:
            raise HTTPException(403, "CAD run belongs to another user")
        return record

    @router.get("/cad-agent/capabilities")
    async def capabilities():
        from .cad_plan import cad_plan_schema
        return {"version": "cad-agent-v1", "planSchema": cad_plan_schema(), "formats": ["step", "glb"], "confirmationRequired": True}

    @router.post("/cad-agent/run")
    async def run(request: Request, message: str = Form(""), modelState: str = Form("{}"),
                  history: str = Form("[]"), files: list[UploadFile] | None = File(None),
                  authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        if len(message) > 16000 or len(modelState) > 750000 or len(history) > 100000:
            raise HTTPException(422, "CAD request is too large")
        try:
            state = json.loads(modelState)
            if not isinstance(state, dict):
                raise ValueError("modelState must be an object")
            if state.get("agentRun") is not None and not isinstance(state["agentRun"], dict):
                raise ValueError("agentRun must be an object")
            state = {key: state[key] for key in ("cadPlan", "agentRun") if key in state}
            if "agentRun" in state:
                state["agentRun"] = {key: state["agentRun"][key] for key in ("runId", "revision") if key in state["agentRun"]}
            parsed_history = _parse_ai_history(history)
        except (ValueError, PlatformError) as exc:
            raise HTTPException(422, str(exc))
        incoming = list(files or [])
        if len(incoming) > 4:
            raise HTTPException(422, "最多上传 4 份图纸。")
        run_id = f"cad_{uuid4().hex}"
        directory = store.directory(run_id)
        source_files = []
        attachments = []
        parent = None
        prior_id = (state.get("agentRun") or {}).get("runId")
        if prior_id and not incoming:
            prior_revision = (state.get("agentRun") or {}).get("revision")
            parent = owned(prior_id, owner, prior_revision)
            if parent.get("status") == "running":
                raise HTTPException(409, {"message": "本轮建模仍在后台运行，请查询任务状态，无需重复提交。",
                                          "runId": prior_id, "revision": parent["revision"], "status": "running"})
            source_files = copy.deepcopy(parent.get("files") or [])
            for source in source_files:
                try:
                    data = store.checked_path(source["path"]).read_bytes()
                except ValueError:
                    raise HTTPException(409, "原图文件已丢失，请重新上传后继续。")
                attachments.append(AIFile(filename=source["filename"], content_type=source["contentType"], data=data))
            # Restore the full tool state from durable storage, never the
            # client-supplied trace or claim of a passed geometry check.
            state["agentState"] = copy.deepcopy(parent.get("state") or {})
            state["cadPlan"] = state.get("cadPlan") or parent.get("plan")
            # A request to revise a server-verified result may intentionally
            # change the original silhouette. Preserve this explicit design
            # context separately from source-only reading; unverified initial
            # reconstructions remain under the strict original-image gate.
            policy = comparison_policy(parent.get("comparisonPolicy"))
            explicit_revision = _explicit_geometry_revision(message, parent.get("plan"))
            if policy["mode"] == "user_revision":
                requests = [*policy["requests"], *([message] if explicit_revision else [])]
                if len(requests) > MAX_REVISION_REQUESTS or sum(map(len, requests)) > MAX_REVISION_REQUEST_CHARACTERS:
                    raise HTTPException(422, "累计修改要求已达到本轮上下文上限，请保存当前版本后开始新的设计；已有要求不会被自动删除。")
                policy = {**policy, "requests": requests}
            elif (explicit_revision and parent.get("status") in {"ready", "review_required"}
                  and _step_export_issue({**parent, "status": "ready"}) is None):
                # Recompute acceptance from real saved measurements, not the
                # cached `inspection.acceptance.status` or a client claim.
                baseline_hash = hashlib.sha256(json.dumps(parent.get("plan"), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                policy = {"mode": "user_revision", "source": "server_verified_parent",
                          "baselinePlanHash": baseline_hash, "requests": [message]}
            state["agentState"]["comparisonPolicy"] = policy
        allowed = {".pdf", ".dxf", ".dwg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        for index, upload in enumerate(incoming):
            filename = Path(upload.filename or "drawing").name
            suffix = Path(filename).suffix.lower()
            if suffix not in allowed:
                raise HTTPException(422, "支持图片、PDF、DWG 和 DXF 图纸。")
            data = await upload.read(MAX_FILE_BYTES + 1)
            if not data or len(data) > MAX_FILE_BYTES:
                raise HTTPException(422, "图纸为空或超过大小限制。")
            attachments.append(AIFile(filename=filename, content_type=upload.content_type or "application/octet-stream", data=data))
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"source-{index}{suffix}"
            path.write_bytes(data)
            source_files.append({"filename": filename, "contentType": upload.content_type or "application/octet-stream", "path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
        if not message.strip() and not attachments:
            raise HTTPException(422, "请输入建模要求或上传图纸。")
        if incoming:
            # New drawing is a new modelling context. An unrelated model may
            # not silently provide dimensions or features for it.
            state = {}
            parsed_history = ()
        # Resolve before saving/queueing: a later request or a changed default
        # cannot choose this job's service after its running identity is sent.
        runner = agent if agent is not None else make_runner()
        run_provider = _runner_provider(runner)
        directory.mkdir(parents=True, exist_ok=True)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        subscribed = threading.Event()
        subscribed.set()
        stopping = threading.Event()
        initial_state = copy.deepcopy(state.get("agentState") or {})
        initial_state["sourceQuestionReviews"] = source_question_reviews(initial_state.get("sourceQuestionReviews", (parent or {}).get("sourceQuestionReviews")))
        initial_state["comparisonPolicy"] = comparison_policy(initial_state.get("comparisonPolicy"))
        initial_plan = state.get("cadPlan", initial_state.get("cadPlan"))
        initial_plan = copy.deepcopy(initial_plan) if isinstance(initial_plan, dict) else None
        initial_state["cadPlan"] = initial_plan
        initial_state["status"] = "running"
        initial_state["provider"] = copy.deepcopy(run_provider)
        started = {"stage": "started", "message": "正在读取图纸与建模要求…",
                   "runId": run_id, "revision": 1, "status": "running", "provider": copy.deepcopy(run_provider)}
        initial_record = {"runId": run_id, "revision": 1, "owner": owner, "status": "running",
                          "createdAt": _utc(), "updatedAt": _utc(), "progress": started,
                          "message": started["message"], "plan": initial_plan, "state": initial_state,
                          "inspection": None, "artifacts": {}, "resolvedParameters": {}, "questions": [],
                          "trace": copy.deepcopy(initial_state.get("trace") or []),
                          "observations": copy.deepcopy(initial_state.get("observations") or []),
                          "sourceTranscription": copy.deepcopy(initial_state.get("sourceTranscription")),
                          "sourceQuestionReviews": copy.deepcopy(initial_state["sourceQuestionReviews"]),
                          "sourceSpatialContract": copy.deepcopy(initial_state.get("sourceSpatialContract")),
                          "comparisonPolicy": copy.deepcopy(initial_state.get("comparisonPolicy")),
                          "provider": copy.deepcopy(run_provider),
                          "drawingReview": {"status": "unverified", "humanConfirmed": False},
                          "downloadToken": secrets.token_urlsafe(32), "files": source_files,
                          "parentRunId": parent["runId"] if parent else None,
                          "workerPid": os.getpid(), "workerInstance": PROCESS_INSTANCE}
        store.save(initial_record)

        def enqueue(event, payload):
            if subscribed.is_set():
                queue.put_nowait((event, payload))

        def progress(payload):
            if stopping.is_set():
                raise RuntimeError("CAD worker shutting down")
            payload = {**payload, "runId": run_id, "revision": 1, "status": "running", "provider": copy.deepcopy(run_provider)}
            store.update_progress(run_id, owner, payload)
            if subscribed.is_set():
                loop.call_soon_threadsafe(enqueue, "progress", payload)

        def persist_interruption(*, status="interrupted", code="worker_interrupted"):
            current = store.load(run_id)
            if current.get("status") == "running":
                current = store.interrupted_record(current, status=status, code=code)
                current["provider"] = _completed_provider(run_provider, current.get("provider"))
                current["progress"]["provider"] = copy.deepcopy(current["provider"])
                store.complete_running(current)
            return current

        async def work():
            try:
                try:
                    duration = float(os.getenv("JOYNIU_CAD_AGENT_TIMEOUT_SECONDS", "900"))
                    if not math.isfinite(duration):
                        raise ValueError("invalid job duration")
                except ValueError:
                    duration = 900
                result = await asyncio.to_thread(runner.run, message=message, files=attachments,
                    state=state, history=parsed_history, output_dir=directory / "builds", progress=progress,
                    max_turns=20, timeout_seconds=max(60, min(1800, duration)))
                record = {**initial_record, **result, "runId": run_id, "revision": 1, "owner": owner,
                          "completedAt": _utc(), "updatedAt": _utc(), "files": source_files,
                          "downloadToken": initial_record["downloadToken"]}
                if record.get("status") not in {"needs_input", "review_required", "failed"}:
                    raise ValueError("invalid CAD agent terminal status")
                record["provider"] = _completed_provider(run_provider, result.get("provider"))
                if isinstance(record.get("state"), dict) and "provider" in record["state"]:
                    record["state"]["provider"] = copy.deepcopy(record["provider"])
                record["progress"] = {"stage": record["status"], "message": record.get("message", ""),
                                      "provider": copy.deepcopy(record["provider"])}
                record = store.complete_running(record)
                enqueue("result", _public_result(record, prefix))
            except asyncio.CancelledError:
                stopping.set()
                try:
                    persist_interruption()
                except Exception as exc:
                    _LOGGER.error("CAD job interruption could not be persisted (run=%s, error=%s)", run_id, type(exc).__name__)
                raise
            except Exception as exc:
                # Preserve recoverable state even for unexpected runner errors.
                # Log only the class, never an upstream message or raw payload.
                _LOGGER.error("CAD job failed (run=%s, error=%s)", run_id, type(exc).__name__)
                try:
                    record = persist_interruption(status="failed", code="task_exception")
                    enqueue("result", _public_result(record, prefix))
                except Exception as persistence_error:
                    _LOGGER.error("CAD job failure could not be persisted (run=%s, error=%s)", run_id, type(persistence_error).__name__)
                    enqueue("error", {"message": "本次建模异常结束，请查询任务状态后继续。", "code": "task_persistence_failed", "runId": run_id, "revision": 1})
            finally:
                enqueue(None, None)

        # Retain the task independently of the StreamingResponse/request. Its
        # lifecycle is durable even if the browser never consumes the stream.
        task = asyncio.create_task(work(), name=f"cad-job-{run_id}")
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

        async def events():
            try:
                yield "event: progress\ndata: " + json.dumps(started, ensure_ascii=False) + "\n\n"
                while True:
                    try:
                        event, payload = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if event is None:
                        break
                    yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n"
            finally:
                subscribed.clear()
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/cad-agent/runs/{run_id}")
    async def get_run(run_id: str, request: Request, revision: int | None = None, authorization: str | None = Header(None)):
        store.recover_interrupted()
        return _public_result(owned(run_id, owner_for(request, authorization), revision), prefix)

    @router.post("/cad-agent/confirm")
    async def confirm(request: Request, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        record = owned(str(payload.get("runId") or ""), owner)
        if type(payload.get("revision")) is not int or payload["revision"] != record["revision"]:
            raise HTTPException(409, "模型已更新，请使用当前版本确认。")
        if record.get("status") not in {"review_required", "ready"}:
            raise HTTPException(422, "请先完成建模和图纸检查，补充信息后继续与 AI 对话。")
        plan = copy.deepcopy(record.get("plan"))
        if not isinstance(plan, dict) or not plan.get("features") or not plan.get("result"):
            raise HTTPException(422, "尚无可执行的建模方案，请继续补充图纸或描述。")
        overrides = payload.get("parameters") or {}
        if not isinstance(overrides, dict):
            raise HTTPException(422, "parameters must be an object")
        changed = []
        for key, value in overrides.items():
            parameter = (plan.get("parameters") or {}).get(key)
            if not isinstance(parameter, dict) or parameter.get("expression"):
                raise HTTPException(422, f"参数 {key} 不存在或属于关联尺寸。")
            if type(value) not in (float, int) or not math.isfinite(value):
                raise HTTPException(422, f"参数 {key} 必须是明确的有限数值。")
            if value != parameter.get("value"):
                changed.append(key)
                parameter.update({"value": value, "source": {"type": "user", "confirmedAt": _utc()}, "question": ""})
        if changed:
            raise HTTPException(422, "参数已改变，请先检查修改并更新预览，再确认新的模型。")
        review = copy.deepcopy(record.get("drawingReview") or {})
        if issue := _drawing_review_issue(record, plan):
            raise HTTPException(422, issue)
        next_revision = record["revision"] + 1
        directory = store.directory(record["runId"]) / f"confirmed-{next_revision}-{uuid4().hex[:8]}"
        observations = record.get("observations") or (record.get("state") or {}).get("observations") or []
        probes = acceptance_ray_probes(observations)
        result = await asyncio.to_thread(execute, plan, directory, ray_probes=probes)
        inspection = dict(result.get("inspection") or {})
        if (result.get("status") != "succeeded" or result.get("valid") is not True
                or inspection.get("valid") is not True or inspection.get("kernelBacked") is not True
                or inspection.get("engine") != "cadquery-occt"
                or not (result.get("artifacts") or {}).get("step")):
            raise HTTPException(422, {"message": "模型校核未通过，未确认或导出。", "errors": result.get("errors", []), "missingParameters": result.get("missingParameters", [])})
        executed_plan = result.get("plan", plan)
        if not isinstance(executed_plan, dict):
            raise HTTPException(422, "重新执行未返回当前建模计划，不能确认或导出。")
        if issue := _drawing_review_issue(record, executed_plan):
            raise HTTPException(422, issue)
        review.update({"humanConfirmed": True, "humanConfirmedAt": _utc()})
        inspection["acceptance"] = evaluate_cad_acceptance(inspection, observations)
        if inspection["acceptance"]["status"] != "passed":
            raise HTTPException(422, {"message": "实际实体与记录的尺寸依据仍有差异，请继续修改或澄清后再确认。",
                "errors": [{"message": item["label"] + "：" + item["message"]} for item in inspection["acceptance"]["checks"] if item["passed"] is not True]})
        updated = {**record, "revision": next_revision, "status": "ready", "confirmedAt": _utc(),
            "message": "已按确认的数据生成 CAD 实体，可下载 STEP。", "questions": [],
            "plan": result.get("plan", plan), "inspection": inspection, "resolvedParameters": result.get("resolvedParameters"), "artifacts": result.get("artifacts", {}),
            "drawingReview": review, "downloadToken": secrets.token_urlsafe(32),
            "trace": [*(record.get("trace") or []), {"stage": "human_confirmation", "revision": next_revision, "changedParameters": changed, "at": _utc()}]}
        updated["state"] = {**(record.get("state") or {}), "cadPlan": updated["plan"], "status": "ready", "questions": [],
            "observations": observations, "trace": updated["trace"], "drawingReview": review}
        try:
            store.save(updated, previous_revision=record["revision"])
        except CadRunConflict as exc:
            raise HTTPException(409, str(exc))
        return _public_result(updated, prefix)

    @router.get("/cad-agent/runs/{run_id}/{revision}/artifacts/{artifact_id}")
    async def artifact(run_id: str, revision: int, artifact_id: str, request: Request,
                       access: str = "", authorization: str | None = Header(None)):
        try:
            record = store.load(run_id, revision)
        except (KeyError, ValueError):
            raise HTTPException(404, "CAD artifact not found")
        # The scoped random capability supports GLTFLoader and ordinary file
        # downloads without putting a user's bearer token in a URL.
        if not access or not hmac.compare_digest(access, record["downloadToken"]):
            owned(run_id, owner_for(request, authorization), revision)
        entry = next((item for item in _artifact_entries(record.get("artifacts") or {}) if item[0] == artifact_id), None)
        if entry is None:
            raise HTTPException(404, "CAD artifact not found")
        _, fmt, _, info = entry
        if fmt == "step" and (issue := _step_export_issue(record)):
            raise HTTPException(409, issue)
        try:
            path = store.checked_path(info["path"])
        except ValueError:
            raise HTTPException(404, "CAD artifact missing")
        headers = {"Cache-Control": "private, max-age=3600", "X-JoyNiu-Engine": "cadquery-occt"}
        if fmt == "step":
            headers["Cache-Control"] = "private, no-store"
        if fmt in {"step", "glb"}:
            headers["Content-Disposition"] = attachment_content_disposition(path.name)
        return FileResponse(path, media_type=info.get("mimeType") or "application/octet-stream", headers=headers)

    return router
