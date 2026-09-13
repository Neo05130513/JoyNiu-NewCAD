"""Generate an auditable batch usage report; quota attribution stays explicit."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cad_usage_accounting import aggregate_usage_events


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def fmt(value):
    return "未完整" if value is None else f"{value:,}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    selection = read(args.root / "selection.json")
    rows, all_events = [], []
    for sample in selection["samples"]:
        folder = args.root / "runs" / sample["id"]
        summary = read(folder / "summary.json", {})
        result = read(folder / "result.json", {})
        events = read(folder / "usage-events.json", [])
        usage = aggregate_usage_events(events)
        all_events.extend(events)
        rows.append({"id": sample["id"], "path": sample["path"], "complexity": sample["complexity"],
                     "status": summary.get("status", "running" if events else "not_started"),
                     "elapsed_seconds": summary.get("elapsed_seconds"),
                     "geometry_generated": summary.get("geometry_generated", False),
                     "human_verified": summary.get("human_verified", False),
                     "questions": summary.get("questions", []),
                     "message": result.get("message"),
                     "last_error_code": (summary.get("provider") or {}).get("lastErrorCode"),
                     "acceptance": summary.get("acceptance"),
                     "drawing_review": summary.get("drawing_review"),
                     "artifacts": summary.get("artifacts", {}),
                     "pending_calls": sum(e.get("status") == "pending" for e in events),
                     "usage": usage})
    usage = aggregate_usage_events(all_events)
    statuses = Counter(row["status"] for row in rows)
    ready = sum(row["status"] == "review_required" and row["geometry_generated"] for row in rows)
    completed = sum(row["status"] not in {"running", "not_started"} for row in rows)
    elapsed = sorted(row["elapsed_seconds"] for row in rows if row["elapsed_seconds"] is not None)
    timing = {"count": len(elapsed), "mean_seconds": round(statistics.mean(elapsed), 2) if elapsed else None,
              "median_seconds": round(statistics.median(elapsed), 2) if elapsed else None,
              "p90_seconds": elapsed[max(0, (9 * len(elapsed) + 9) // 10 - 1)] if elapsed else None,
              "max_seconds": max(elapsed) if elapsed else None}
    strata = {}
    for level in sorted({row["complexity"] for row in rows}):
        group = [row for row in rows if row["complexity"] == level]
        strata[level] = {"selected": len(group), "statuses": dict(Counter(row["status"] for row in group)),
                         "known_input_tokens": sum(row["usage"]["known_subtotal"]["input_tokens"] for row in group),
                         "known_output_tokens": sum(row["usage"]["known_subtotal"]["output_tokens"] for row in group)}
    stages = {stage: aggregate_usage_events([event for event in all_events if event.get("stage", "unknown") == stage])
              for stage in sorted({event.get("stage", "unknown") for event in all_events})}
    snapshots = sorted((read(p) for p in args.root.glob("quota-*.json")), key=lambda s: s["captured_at"])
    baseline, latest = (snapshots[0], snapshots[-1]) if snapshots else ({}, {})
    first = ((baseline.get("rateLimitsByLimitId") or {}).get("codex") or baseline.get("rateLimits") or {}).get("primary") or {}
    last = ((latest.get("rateLimitsByLimitId") or {}).get("codex") or latest.get("rateLimits") or {}).get("primary") or {}
    comparable = bool(first and last and first.get("resetsAt") == last.get("resetsAt")
                      and first.get("windowDurationMins") == last.get("windowDurationMins")
                      and last.get("usedPercent", -1) >= first.get("usedPercent", 0))
    delta = last["usedPercent"]-first["usedPercent"] if comparable else None
    model_cost_scenario = None
    finalized = completed == len(rows) and not any(row["pending_calls"] for row in rows) and latest.get("label", "").startswith("final")
    if finalized and comparable and last.get("windowDurationMins") == 10080 and delta and ready:
        # Scenario only: 30-day month, all weekly capacity used for this workload,
        # no reset coupons or other work, same future quota and success mix.
        allocated = 1400 * 7 / 30 * delta / 100
        model_cost_scenario = {"basis": "full_weekly_capacity_30_day_month_shared_account_delta",
                              "monthly_subscription_cny": 1400, "observed_percentage_points": delta,
                              "batch_subscription_allocation_cny": round(allocated, 4),
                              "per_attempt_cny": round(allocated / completed, 4) if completed else None,
                              "per_review_candidate_cny": round(allocated / ready, 4),
                              "monthly_review_candidates_at_full_capacity": round(ready * 100 / delta * 30 / 7, 2),
                              "invoice": False, "excludes": ["server", "human_review", "support", "payment_fees", "idle_capacity"]}
    combined = {"generated_at": datetime.now(timezone.utc).isoformat(), "selected": len(rows), "completed": completed,
                "status_counts": dict(statuses), "ready_for_human_review": ready, "usage": usage, "finalized": finalized,
                "timing": timing, "strata": strata, "stages": stages,
                "quota": {"baseline": baseline, "latest": latest, "same_window": comparable,
                          "observed_account_percentage_point_change": delta},
                "subscription_capacity_scenario": model_cost_scenario, "samples": rows}
    (args.root / "batch-summary.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 30 张机械图纸：积分计费用量试测", "",
             f"生成时间：{combined['generated_at']}；已结束 {completed}/{len(rows)} 张。", "",
             "最终状态：" + ("全部尝试已结束，已记录最终额度。" if finalized else "测试进行中；中途读数不能作为最终单件成本。"), "",
             "## 测试口径", "",
             "- 选样为 8 张按结构选取的试跑图，加 22 张从剩余去重图纸中固定种子 20260910 随机抽取的图；不是三档各 10 张。随机样本未预判复杂度，记为 unclassified。",
             "- 使用现有 CadAgentService、Codex gpt-6-astra、高推理档；原图转录保持软件原有配置。",
             "- 每张仅提供原图和通用建模请求，不提供参考尺寸、模板答案、旧草稿或人工补答。每张最多 900 秒、20 步。",
             "- 测量范围是首次上传到首次终态；needs_input 不等于转换失败，也不等于完成交付。补答、续跑、人工修改和最终验收成本尚未测量。",
             "- 固定选样，完整转换不自动重新跑；软件内部读图重试、建模修正、独立复核均计入请求事件。",
             "- 输入 token 已包含缓存输入；输出 token 已包含推理输出。分类统计不得再次相加。未知用量保持未知。",
             "- 自动完成仅指进入待人工确认且存在 STEP，不能等同所有尺寸及制造要求均通过独立验收。",
             "- 额度是账户级整数百分比，包含同期本任务辅助工作及其他账户使用；不是 30 张图的独立账单。", "",
             "## 结果", "", f"- 状态分布：{dict(statuses)}。",
             f"- 待人工确认且生成 STEP：{ready}/{len(rows)}。",
             f"- 另有 {sum(row['geometry_generated'] and row['status'] != 'review_required' for row in rows)} 张保留 STEP 草稿，但未进入待核验交付状态，不计入上述候选数。",
             f"- 单张平均耗时：{timing['mean_seconds']} 秒；中位数 {timing['median_seconds']} 秒；P90 {timing['p90_seconds']} 秒。",
             f"- 捕获请求：{usage['event_count']} 次。",
             f"- 已观测 token 小计：输入 {fmt(usage['known_subtotal']['input_tokens'])}，其中缓存 {fmt(usage['known_subtotal']['cached_input_tokens'])}；输出 {fmt(usage['known_subtotal']['output_tokens'])}。",
             f"- 用量缺失请求数（按字段）：{usage['unknown_event_counts']}。", "",
             "| 样本 | 预估复杂度 | 状态 | 秒 | 请求数 | 输入 token | 缓存输入 | 输出 token |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        values = row["usage"]["totals"]
        duration = round(row["elapsed_seconds"], 1) if row["elapsed_seconds"] is not None else "—"
        lines.append(f"| {Path(row['path']).name} | {row['complexity']} | {row['status']} | {duration} | {row['usage']['event_count']} | {fmt(values['input_tokens'])} | {fmt(values['cached_input_tokens'])} | {fmt(values['output_tokens'])} |")
    lines.extend(["", "## 分阶段用量", "", "下表是 token 统计，不能直接换算成各阶段的订阅额度百分比。", "",
                  "| 阶段 | 调用次数 | 已知输入 token | 已知输出 token |", "|---|---:|---:|---:|"])
    for stage, values in stages.items():
        lines.append(f"| {stage} | {values['event_count']} | {fmt(values['known_subtotal']['input_tokens'])} | {fmt(values['known_subtotal']['output_tokens'])} |")
    lines.extend(["", "## 未交付原因与待补充信息", "", "以下为软件返回的原因，未经逐张人工尺寸审计，不能把所有提问都认定为原图缺失信息。", ""])
    for row in rows:
        if row["questions"]:
            lines.append(f"- {Path(row['path']).name}：{row['message'] or ''} 待确认：{'；'.join(row['questions'])}")
        elif row["status"] == "failed":
            lines.append(f"- {Path(row['path']).name}：{row['message'] or '转换失败'}；错误类别：{row['last_error_code'] or '未提供'}。")
    lines.extend(["", "## 已生成的待核验模型", ""])
    for row in rows:
        if row["status"] != "review_required" or not row["geometry_generated"]:
            continue
        step = (row["artifacts"].get("step") or {}).get("path")
        if step:
            lines.append(f"- [{Path(row['path']).name} 原图](<{row['path']}>) · [STEP 模型](<{step}>)。自动尺寸检查：{row['acceptance']}；自动读图复核：{row['drawing_review']}。")
    lines.extend(["", "## 账户额度与订阅分摊", ""])
    if comparable:
        lines.append(f"同一个 {last['windowDurationMins']/1440:g} 天窗口：已用 {first['usedPercent']}% → {last['usedPercent']}%，观察增加 {delta} 个百分点。")
        final_snapshots = [s for s in snapshots if s.get("label", "").startswith("final")]
        if len(final_snapshots) > 1:
            readings = []
            for snapshot in final_snapshots:
                bucket = (snapshot.get("rateLimitsByLimitId") or {}).get("codex") or snapshot.get("rateLimits") or {}
                readings.append((bucket.get("primary") or {}).get("usedPercent"))
            lines.extend(["", f"全部 CAD 调用结束后，额度复读依次为 {readings}%。采用最后一次读数；无法把读数变化精确拆分为额度统计延迟和同期辅助工作。"])
    else:
        lines.append("缺少可比较的同窗口额度快照，不能计算额度变化。")
    if model_cost_scenario:
        s = model_cost_scenario
        lines.extend(["", "以下为容量分摊情景，**不是实际扣款，也不是已验证的长期单件成本**：按 1400 元/月、30 天/月、每周额度相同且可充分利用。", "",
                      f"- 本批额度对应订阅分摊：1400 × 7/30 × {delta}% = {s['batch_subscription_allocation_cny']:.2f} 元。",
                      f"- 每次尝试约 {s['per_attempt_cny']:.2f} 元；每个待复核候选约 {s['per_review_candidate_cny']:.2f} 元。",
                      f"- 同类工作负载满额使用的候选容量约 {s['monthly_review_candidates_at_full_capacity']:.0f} 个/月。",
                      "- 该情景含同期辅助开销的影响，不包含服务器、人工核图、售后、支付及空闲额度成本；赠送重置券不计入可持续月容量。",
                      f"- 月费是固定支出。若整月仅处理本批 {completed} 次尝试，订阅实际分摊为 1400/{completed} = {1400/completed:.2f} 元/次；若只按本批 {ready} 个待核验候选分摊，则为 {1400/ready:.2f} 元/候选。",
                      "- 额度读数每变化 1 个百分点，会使本批订阅容量分摊变化约 3.27 元，说明小样本结果不宜理解为精确到分的成本。",
                      "- 小样本、混合选样与整数额度读数均有限制，不能据此承诺全量图纸成功率或固定每图扣点。"])
    lines.extend(["", "## 可复查材料", "", f"原始记录目录：{args.root.resolve()}", "",
                  "各样本含原图哈希、运行配置、逐调用用量、阶段进度、原始运行结果及真实几何产物；未代替用户确认交付。", ""])
    lines.extend(["## 对积分方案的含义", "",
                  "- 先确认单位、建模范围及关键缺失尺寸，再进入完整转换，减少晚提问造成的消耗。",
                  "- needs_input 与已生成待核验模型应分开处理；软件对齐或复核出错产生的额外消耗应进入内部成本，不能直接当作已交付向用户收费。",
                  "- 本轮提供计量工具和测试基准；尚未接入积分钱包、真实扣点、支付或退款。正式单价还需要补答续跑成本及人工、服务器成本。", ""])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"completed": completed, "selected": len(rows), "ready": ready,
                      "status_counts": dict(statuses), "known_tokens": usage['known_subtotal'],
                      "quota_delta": delta}, ensure_ascii=False))


if __name__ == "__main__":
    main()
