"""HTTP boundary for the tool-using CAD agent, its revisions and deliverables."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
from typing import Any, Mapping
import unicodedata
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .ai_proxy import AIFile, MAX_FILE_BYTES
from .cad_agent_store import (CadRunConflict, CadRunStore, PROCESS_INSTANCE, comparison_policy, source_question_reviews,
                              MAX_REVISION_REQUESTS, MAX_REVISION_REQUEST_CHARACTERS)
from .cad_acceptance import acceptance_ray_probes, evaluate_cad_acceptance
from .cad_source_preview import SourcePreviewError, source_documents, source_download, source_preview
from .cad_source_locations import cached_source_locations, locate_source_dimensions, source_location_progress
from .download_headers import attachment_content_disposition
from .platform import Permission, PlatformError
from .platform_api import _anonymous_ai_request_allowed, _domain_http_exception, _parse_ai_history, _token_user
from .cad_job_registry import CadJobRegistry, JobConflict, JobCapacityExceeded, JobRequestCancelled
from .metered_cad_provider import RunCallContext, CadOperationCancelled, MeteredCadProvider, instrument_runner


_LOGGER = logging.getLogger("joyniu.cad_agent_jobs")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


_PROVIDER_IDENTITY_FIELDS = ("mode", "name", "model", "reasoningEffort", "configured", "streaming", "authentication",
                             "deployment", "binaryAvailable", "proxyConfigured")
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
        ("deployment", {"local", "server"}),
    ):
        item = value.get(key)
        if isinstance(item, str) and item in choices:
            result[key] = item
    for key in ("configured", "streaming", "binaryAvailable", "proxyConfigured"):
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


def _public_result(record: dict[str, Any], prefix: str, store: CadRunStore | None = None) -> dict[str, Any]:
    result = {key: copy.deepcopy(record.get(key)) for key in (
        "runId", "revision", "status", "message", "plan", "inspection", "questions",
        "trace", "drawingReview", "provider", "createdAt", "confirmedAt", "confirmedBy", "parentRunId",
        "observations", "resolvedParameters", "sourceTranscription", "sourceSpatialContract", "sourceQuestionReviews", "projectionComparison", "comparisonPolicy", "draftInspection",
        "progress", "updatedAt", "completedAt", "jobId", "attemptId", "requestId", "changeKind", "cancelRequested",
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
    result["sourceDocuments"] = source_documents(store, record, prefix) if store is not None else []
    if store is not None:
        locations = cached_source_locations(store, record)
        if locations is not None:
            result["sourceDimensionLocations"] = locations
        location_progress = source_location_progress(store, record)
        if location_progress is not None:
            result["sourceDimensionLocationProgress"] = location_progress
    return result


def create_cad_agent_router(services, store: CadRunStore, *, prefix: str = "/api/v1", agent=None, executor=None, service_factory=None, billing=None, billing_policy=None):
    # Lazy imports keep API tests able to inject a deterministic provider and
    # keep the existing recipe service independent from optional CAD tooling.
    from .cad_agent import CadAgentService
    from .cad_executor import execute_cad_plan

    make_runner = service_factory or CadAgentService
    execute = executor or execute_cad_plan
    background_tasks: set[asyncio.Task] = set()
    registry = CadJobRegistry(store)
    active_contexts: dict[str, RunCallContext] = {}
    from .cad_file_access import CadFileAccess, CadFileAccessDenied
    file_access = CadFileAccess(store, getattr(services, "auth", None), billing=billing,
                                allow_anonymous=bool(services.ai.allow_anonymous))
    if billing_policy is None and billing is not None and hasattr(billing, "_db"):
        from .billing_policy import BillingPolicyService
        billing_policy = BillingPolicyService(billing)
    if billing_policy is not None and billing.charging_status_provider is None:
        billing.charging_status_provider = billing_policy.status
    registry.billing, registry.billing_policy = billing, billing_policy

    def register_billing_attempt(owner, job_id, attempt_id, *, chargeable=True):
        if billing_policy is None or (owner == "local-anonymous" and not billing_policy.status()["enabled"]):
            return
        if owner == "local-anonymous":
            raise HTTPException(401, "请先登录后再开始付费任务。")
        try:
            result = billing_policy.register_attempt(owner_id=owner, job_id=job_id, attempt_id=attempt_id, chargeable=chargeable)
        except PlatformError as exc:
            raise _domain_http_exception(exc) from None
        if not result["allowed"]:
            raise HTTPException(402, result["message"])

    def check_billing_provider(owner, job_id, attempt_id, identity):
        if billing_policy is None or (owner == "local-anonymous" and not billing_policy.status()["enabled"]):
            return
        billing_policy.ensure_provider_supported(owner_id=owner, job_id=job_id, attempt_id=attempt_id,
                                                 provider=identity.get("provider", identity.get("mode", "unknown")), model=identity.get("model", "unknown"))

    def recheck_billing_admission(context):
        if billing_policy is None or (context.owner == "local-anonymous" and not billing_policy.status()["enabled"]):
            return
        try:
            guard = billing_policy.recheck_attempt_start(owner_id=context.owner, job_id=context.job_id,
                                                         attempt_id=context.attempt_id)
        except PlatformError as exc:
            guard = {"allowed": False, "message": str(exc)}
        if not guard["allowed"]:
            context.admission_failure = guard["message"] + " 本次尚未调用 AI。"
            context.cancel_event.set()
            raise CadOperationCancelled(context.admission_failure)

    async def billing_recovery():
        while True:
            try:
                registry.reconcile()
                await asyncio.to_thread(registry.settle_pending, billing, billing_policy)
            except Exception as exc:
                _LOGGER.error("Billing recovery deferred (error=%s)", type(exc).__name__)
            await asyncio.sleep(30)

    def public_result(record):
        return file_access.protect_result(record, _public_result(registry.decorate(record), prefix, store), prefix)

    @asynccontextmanager
    async def lifespan(_app):
        recovery = asyncio.create_task(billing_recovery()) if billing_policy is not None else None
        try:
            yield
        finally:
            if recovery:
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            # A browser disconnect is not a shutdown. Only an actual API
            # worker shutdown interrupts jobs and saves their checkpoints.
            pending = list(background_tasks)
            for context in active_contexts.values():
                context.cancel_event.set()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    router = APIRouter(prefix=prefix, tags=["cad-agent"], lifespan=lifespan)
    router.job_registry = registry
    router.billing_policy = billing_policy
    router.file_access = file_access

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
                  authorization: str | None = Header(None), requestId: str | None = Form(None)):
        owner = owner_for(request, authorization)
        # Direct service tests call the endpoint without FastAPI's injection.
        request_id = requestId if isinstance(requestId, str) else None
        if request_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
            raise HTTPException(422, "请求编号格式不正确。")
        request_id = request_id or uuid4().hex
        registry.reconcile()
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
        fingerprint_state = copy.deepcopy(state)
        if len(incoming) > 4:
            raise HTTPException(422, "最多上传 4 份图纸。")
        run_id = f"cad_{uuid4().hex}"
        directory = store.directory(run_id)
        source_files = []
        attachments = []
        parent = None
        explicit_revision = False
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
        request_hash = hashlib.sha256(json.dumps({"message": message, "state": fingerprint_state,
            "history": parsed_history, "files": [{key: item.get(key) for key in ("filename", "contentType", "sha256")} for item in source_files]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        try:
            identity, created = registry.claim(run_id=run_id, owner=owner, request_id=request_id, request_hash=request_hash,
                parent=parent, change_kind="new_drawing" if incoming else "revision" if explicit_revision else "continuation" if parent else "new_design")
        except JobRequestCancelled:
            if directory.exists():
                shutil.rmtree(directory)
            return JSONResponse({"runId": None, "requestId": request_id, "status": "cancelled", "message": "此请求已取消，不会再启动建模。", "artifacts": []})
        except (JobConflict, JobCapacityExceeded) as exc:
            if directory.exists():
                shutil.rmtree(directory)
            raise HTTPException(409 if isinstance(exc, JobConflict) else 429, str(exc)) from None
        if not created:
            if directory.exists():
                shutil.rmtree(directory)
            try:
                return JSONResponse(public_result(owned(identity["runId"], owner)), headers={"Cache-Control": "no-store"})
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                interrupted = identity["status"] not in {"running", "queued", "cancel_requested"}
                return JSONResponse({**identity, "message": "任务尚未启动，请使用新的请求编号重新提交原图。" if interrupted else "任务已受理，请稍后查询相同运行编号。", "artifacts": []}, status_code=200 if interrupted else 202, headers={"Cache-Control": "no-store"})
        try:
            register_billing_attempt(owner, identity["jobId"], run_id)
        except Exception:
            registry.finish(run_id, "failed")
            if directory.exists():
                shutil.rmtree(directory)
            raise
        # Resolve before saving/queueing: a later request or a changed default
        # cannot choose this job's service after its running identity is sent.
        try:
            runner = agent if agent is not None else make_runner()
            run_provider = _runner_provider(runner)
            from .cad_provider import provider_details
            check_billing_provider(owner, identity["jobId"], run_id, provider_details(getattr(runner, "provider_call", None)))
            context = RunCallContext(registry=registry, billing=billing, owner=owner,
                                     job_id=identity["jobId"], attempt_id=run_id)
            context.admission_check = lambda: recheck_billing_admission(context)
            context.provider_check = lambda value: check_billing_provider(owner, identity["jobId"], run_id, value)
            runner = instrument_runner(runner, context)
        except PlatformError as exc:
            registry.finish(run_id, "failed")
            raise _domain_http_exception(exc) from None
        except Exception:
            registry.finish(run_id, "failed")
            raise HTTPException(503, "建模服务暂不可用，本次任务尚未启动，请稍后重试。") from None
        active_contexts[run_id] = context
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
        started = {"stage": "queued" if identity["status"] == "queued" else "started",
                   "message": "正在排队等待建模资源，可稍后回来查看。" if identity["status"] == "queued" else "正在读取图纸与建模要求…",
                   **identity, "revision": 1, "provider": copy.deepcopy(run_provider)}
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
                          **{key: identity[key] for key in ("jobId", "attemptId", "requestId", "changeKind")},
                          "workerPid": os.getpid(), "workerInstance": PROCESS_INSTANCE}
        try:
            store.save(initial_record)
        except Exception:
            active_contexts.pop(run_id, None)
            registry.finish(run_id, "failed")
            raise HTTPException(503, "任务保存未完成，本次尚未开始建模，请稍后重试。") from None

        def enqueue(event, payload):
            if subscribed.is_set():
                queue.put_nowait((event, payload))

        def progress(payload):
            context.check()
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
                registry.finish(run_id, current["status"])
            return current

        def persist_cancelled():
            current = store.load(run_id)
            if current.get("status") == "running":
                current = store.interrupted_record(current, status="cancelled", code="user_cancelled")
                current["message"] = "任务已取消，已保留原图与可恢复草稿；未继续执行后续步骤。"
                current["progress"] = {"stage": "cancelled", "message": current["message"], "provider": copy.deepcopy(run_provider)}
                current["provider"] = _completed_provider(run_provider, current.get("provider"))
                store.complete_running(current)
            registry.finish(run_id, current["status"])
            return current

        def persist_admission_failure():
            current = store.load(run_id)
            if current.get("status") == "running":
                current = store.interrupted_record(current, status="failed", code="billing_admission")
                current["message"] = context.admission_failure
                current["progress"] = {"stage": "failed", "message": current["message"], "provider": copy.deepcopy(run_provider)}
                current["provider"] = _completed_provider(run_provider, current.get("provider"))
                store.complete_running(current)
            registry.finish(run_id, current["status"])
            return current

        async def wait_calls():
            while not context.is_idle():
                await asyncio.sleep(0.1)

        async def watch_cancel():
            while True:
                if (registry.get(run_id) or {}).get("cancelRequested"):
                    context.cancel_event.set()
                    return
                await asyncio.sleep(0.2)

        async def work():
            watcher = asyncio.create_task(watch_cancel())
            try:
                while not registry.try_start(run_id):
                    context.check()
                    if (registry.get(run_id) or {}).get("cancelRequested"):
                        raise CadOperationCancelled("Queued job cancelled")
                    await asyncio.sleep(0.25)
                context.check()
                recheck_billing_admission(context)
                if identity["status"] == "queued":
                    progress({"stage": "started", "message": "已开始处理本次建模任务。"})
                try:
                    duration = float(os.getenv("JOYNIU_CAD_AGENT_TIMEOUT_SECONDS", "900"))
                    if not math.isfinite(duration):
                        raise ValueError("invalid job duration")
                except ValueError:
                    duration = 900
                result = await asyncio.to_thread(runner.run, message=message, files=attachments,
                    state=state, history=parsed_history, output_dir=directory / "builds", progress=progress,
                    max_turns=20, timeout_seconds=max(60, min(1800, duration)))
                # Auxiliary reader threads may outlive the agent's final result.
                # Stop them and retain their final usage before releasing capacity.
                context.cancel_event.set()
                await wait_calls()
                if context.admission_failure:
                    enqueue("result", public_result(persist_admission_failure()))
                    return
                if (registry.get(run_id) or {}).get("cancelRequested"):
                    enqueue("result", public_result(persist_cancelled()))
                    return
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
                registry.finish(run_id, record["status"])
                enqueue("result", public_result(record))
            except asyncio.CancelledError:
                stopping.set()
                context.cancel_event.set()
                try:
                    persist_interruption()
                except Exception as exc:
                    _LOGGER.error("CAD job interruption could not be persisted (run=%s, error=%s)", run_id, type(exc).__name__)
                raise
            except CadOperationCancelled:
                context.cancel_event.set()
                await wait_calls()
                enqueue("result", public_result(persist_admission_failure() if context.admission_failure else persist_cancelled()))
            except Exception as exc:
                context.cancel_event.set()
                await wait_calls()
                if context.admission_failure:
                    enqueue("result", public_result(persist_admission_failure()))
                    return
                if (registry.get(run_id) or {}).get("cancelRequested"):
                    enqueue("result", public_result(persist_cancelled()))
                    return
                # Preserve recoverable state even for unexpected runner errors.
                # Log only the class, never an upstream message or raw payload.
                _LOGGER.error("CAD job failed (run=%s, error=%s)", run_id, type(exc).__name__)
                try:
                    record = persist_interruption(status="failed", code="task_exception")
                    enqueue("result", public_result(record))
                except Exception as persistence_error:
                    _LOGGER.error("CAD job failure could not be persisted (run=%s, error=%s)", run_id, type(persistence_error).__name__)
                    enqueue("error", {"message": "本次建模异常结束，请查询任务状态后继续。", "code": "task_persistence_failed", "runId": run_id, "revision": 1})
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                active_contexts.pop(run_id, None)
                registry.deliver_usage(billing)
                try:
                    await asyncio.to_thread(registry.settle_pending, billing, billing_policy, owner=owner)
                except Exception as exc:
                    _LOGGER.error("CAD settlement deferred (run=%s, error=%s)", run_id, type(exc).__name__)
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
        owner = owner_for(request, authorization)
        owned(run_id, owner, revision)
        store.recover_interrupted()
        registry.reconcile()
        registry.settle_pending(billing, billing_policy, owner=owner)
        return public_result(owned(run_id, owner, revision))

    @router.get("/cad-agent/jobs")
    async def jobs(request: Request, limit: int = 50, offset: int = 0, authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        try:
            result = registry.list_jobs(owner, limit=limit, offset=offset)
            registry.settle_pending(billing, billing_policy, owner=owner)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        for item in result["items"]:
            item["provider"] = _public_provider(item.get("provider"))
            if isinstance(item.get("progress"), dict):
                item["progress"]["provider"] = copy.deepcopy(item["provider"])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/cad-agent/jobs/by-request/{request_id}")
    async def job_by_request(request_id: str, request: Request, authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
            raise HTTPException(422, "请求编号格式不正确。")
        registry.reconcile()
        identity = registry.by_request(owner, request_id)
        if identity is None:
            raise HTTPException(404, "未找到此请求对应的任务。")
        if identity["runId"] is None:
            return JSONResponse(identity, headers={"Cache-Control": "no-store"})
        try:
            return JSONResponse(public_result(owned(identity["runId"], owner)), headers={"Cache-Control": "no-store"})
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            interrupted = identity["status"] not in {"running", "queued", "cancel_requested"}
            return JSONResponse({**identity, "message": "任务登记期间服务中断，尚未启动建模，请重新提交原图。" if interrupted else "任务正在登记，请稍后查询。", "artifacts": []}, status_code=200 if interrupted else 202, headers={"Cache-Control": "no-store"})

    @router.post("/cad-agent/jobs/by-request/{request_id}/cancel")
    async def cancel_by_request(request_id: str, request: Request, authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
            raise HTTPException(422, "请求编号格式不正确。")
        identity = registry.cancel_request(owner, request_id)
        if identity["runId"] is None:
            return JSONResponse(identity, headers={"Cache-Control": "no-store"})
        context = active_contexts.get(identity["runId"])
        if context is not None and identity["cancelRequested"]:
            context.cancel_event.set()
        try:
            return JSONResponse(public_result(owned(identity["runId"], owner)), headers={"Cache-Control": "no-store"})
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return JSONResponse({**identity, "status": "cancel_requested" if identity["status"] in {"running", "queued", "cancel_requested"} else identity["status"]}, headers={"Cache-Control": "no-store"})

    @router.post("/cad-agent/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request, authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        record = owned(run_id, owner)
        registry.reconcile()
        if record.get("status") == "running":
            identity = registry.request_cancel(run_id, owner)
            if identity is None:
                raise HTTPException(409, "此历史任务不支持在线取消，请先刷新其当前状态。")
            context = active_contexts.get(run_id)
            if context is not None:
                context.cancel_event.set()
        return JSONResponse(public_result(store.load(run_id)), headers={"Cache-Control": "no-store"})

    @router.post("/cad-agent/runs/{run_id}/source-locations")
    async def source_locations(run_id: str, request: Request, payload: dict[str, Any] = Body(default={}),
                               authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        revision = payload.get("revision")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise HTTPException(422, "revision must be a positive integer")
        if "force" in payload and type(payload["force"]) is not bool:
            raise HTTPException(422, "force must be a boolean")
        operation_id = payload.get("operationId")
        if operation_id is not None and (not isinstance(operation_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", operation_id)):
            raise HTTPException(422, "operationId must be a bounded request identifier")
        record = owned(run_id, owner, revision)
        if record.get("status") == "running":
            raise HTTPException(409, "模型正在生成，请完成后再定位图纸尺寸。")
        from . import ai_proxy
        from .cad_provider import configured_cad_provider
        provider = getattr(agent, "provider_call", None) or configured_cad_provider() or ai_proxy._call_provider
        identity = registry.decorate(record)
        # A review-page localization may happen after the model attempt has
        # already ended. Keep its real calls out of that sealed billing batch.
        location_attempt = "location_" + uuid4().hex
        cached = cached_source_locations(store, record)
        operation_registered = False
        def ensure_location_operation():
            nonlocal operation_registered
            if not operation_registered:
                # Opening/retrying source dimension locations enriches an
                # existing result. Keep real usage, but never bill this viewing
                # operation or require a funded wallet/current paid terms.
                register_billing_attempt(owner, identity["jobId"], location_attempt, chargeable=False)
                from .cad_provider import provider_details
                check_billing_provider(owner, identity["jobId"], location_attempt, provider_details(provider))
                registry.begin_operation(attempt_id=location_attempt, owner=owner, job_id=identity["jobId"])
                operation_registered = True
        if cached is None or payload.get("force", False):
            try:
                ensure_location_operation()
            except JobCapacityExceeded as exc:
                raise HTTPException(429, str(exc)) from None
            except PlatformError as exc:
                raise _domain_http_exception(exc) from None
        location_context = RunCallContext(registry=registry, billing=billing, owner=owner,
                                           job_id=identity["jobId"], attempt_id=location_attempt)
        def admit_location_call():
            ensure_location_operation()
            recheck_billing_admission(location_context)
        location_context.admission_check = admit_location_call
        location_context.provider_check = lambda value: check_billing_provider(owner, identity["jobId"], location_attempt, value)
        metered = MeteredCadProvider(provider, location_context, "source_locations")
        outcome = "failed"
        try:
            result = await asyncio.to_thread(locate_source_dimensions, store, record,
                                           force=payload.get("force", False), operation_id=operation_id,
                                           provider_call=metered)
            if location_context.admission_failure:
                raise HTTPException(402, location_context.admission_failure)
            outcome = "ready" if result.get("status") == "succeeded" else "failed"
            return result
        except CadOperationCancelled:
            if location_context.admission_failure:
                raise HTTPException(402, location_context.admission_failure) from None
            raise
        finally:
            location_context.cancel_event.set()
            async def finish_location():
                while not location_context.is_idle():
                    await asyncio.sleep(0.1)
                if operation_registered:
                    registry.finish_operation(location_attempt, outcome)
                    await asyncio.to_thread(registry.settle_pending, billing, billing_policy, owner=owner)
            completion = asyncio.create_task(finish_location())
            background_tasks.add(completion)
            completion.add_done_callback(background_tasks.discard)

    @router.post("/cad-agent/confirm")
    async def confirm(request: Request, payload: dict[str, Any] = Body(...), authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        actor = _token_user(services, authorization) if authorization else None
        confirmed_by = {"id": actor.id, "displayName": actor.display_name} if actor else None
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
                previous_source = parameter.get("source")
                drawing_source = previous_source.get("drawingSource", previous_source) if isinstance(previous_source, Mapping) else previous_source
                parameter.update({"value": value, "source": {"type": "user", "confirmedAt": _utc(),
                    **({"drawingSource": copy.deepcopy(drawing_source)} if drawing_source else {})}, "question": ""})
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
        updated = {**record, "revision": next_revision, "status": "ready", "confirmedAt": _utc(), "confirmedBy": confirmed_by,
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
        return public_result(updated)

    @router.post("/cad-agent/runs/{run_id}/revoke-file-links")
    async def revoke_file_access(run_id: str, request: Request, authorization: str | None = Header(None)):
        owner = owner_for(request, authorization)
        record = owned(run_id, owner)
        try:
            revoked = file_access.revoke(record, owner)
        except CadFileAccessDenied as exc:
            raise HTTPException(403, str(exc)) from None
        return JSONResponse({**public_result(record), **revoked}, headers={"Cache-Control": "no-store"})

    def authorize_file(record, request, access, authorization, resource):
        if authorization:
            owned(record["runId"], owner_for(request, authorization), record["revision"])
            return
        if file_access.allows(record, resource, access):
            return
        if record.get("owner") == "local-anonymous" and services.ai.allow_anonymous and file_access.owner_active(record):
            owned(record["runId"], owner_for(request, authorization), record["revision"])
            return
        raise HTTPException(403, "文件链接无效或已失效，请登录后重新打开。")

    def source_record(run_id: str, revision: int, request: Request, access: str, authorization: str | None, resource: str):
        try:
            record = store.load(run_id, revision)
        except (KeyError, ValueError):
            raise HTTPException(404, "Source drawing not found")
        authorize_file(record, request, access, authorization, resource)
        return record

    @router.get("/cad-agent/runs/{run_id}/{revision}/sources/{file_index}/download")
    async def download_source(run_id: str, revision: int, file_index: int, request: Request,
                              access: str = "", authorization: str | None = Header(None)):
        record = source_record(run_id, revision, request, access, authorization, f"sources/{file_index}/download")
        try:
            path, filename = source_download(store, record, file_index)
        except (ValueError, OSError):
            raise HTTPException(404, "Source drawing is missing") from None
        return FileResponse(path, media_type="application/octet-stream", headers={
            "Content-Disposition": attachment_content_disposition(filename),
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.get("/cad-agent/runs/{run_id}/{revision}/sources/{file_index}/pages/{page}")
    async def preview_source(run_id: str, revision: int, file_index: int, page: int, request: Request,
                             access: str = "", authorization: str | None = Header(None)):
        record = source_record(run_id, revision, request, access, authorization, f"sources/{file_index}/pages/{page}")
        try:
            path = await asyncio.to_thread(source_preview, store, record, file_index, page)
        except (SourcePreviewError, OSError, ValueError):
            raise HTTPException(404, "Source drawing preview is unavailable") from None
        return FileResponse(path, media_type="image/png", headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.get("/cad-agent/runs/{run_id}/{revision}/artifacts/{artifact_id}")
    async def artifact(run_id: str, revision: int, artifact_id: str, request: Request,
                       access: str = "", authorization: str | None = Header(None)):
        try:
            record = store.load(run_id, revision)
        except (KeyError, ValueError):
            raise HTTPException(404, "CAD artifact not found")
        authorize_file(record, request, access, authorization, f"artifacts/{artifact_id}")
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
        headers = {"Cache-Control": "private, no-store", "X-JoyNiu-Engine": "cadquery-occt"}
        if fmt in {"step", "glb"}:
            headers["Content-Disposition"] = attachment_content_disposition(path.name)
        return FileResponse(path, media_type=info.get("mimeType") or "application/octet-stream", headers=headers)

    return router
