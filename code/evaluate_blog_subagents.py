"""Offline EXP-3 paired parent/child contract replay on frozen EXP-2 evidence."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from blog_assistant.subagent_experiment import SharedBudget, run_fixed_evidence
from evaluate_blog_search import OUTPUT_ROOT, grade


CASE_IDS = ("b01", "b08", "b09", "b12")
MODES = ("A0", "A1", "A2")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(source, output):
    source, output = source.resolve(), output.resolve()
    if not source.is_relative_to(OUTPUT_ROOT.resolve()) or not output.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("Input and output must stay in ignored experiment root")
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if source_manifest["stage"] != "EXP-2-offline" or source_manifest["split"] != "holdout":
        raise ValueError("EXP-3 requires frozen EXP-2 holdout evidence")
    cases = {case["id"]: case for case in json.loads((source / "cases.json").read_text(encoding="utf-8"))}
    source_bytes = (source / "retrieval.jsonl").read_bytes()
    source_rows = [json.loads(line) for line in source_bytes.decode("utf-8").splitlines()]
    selected = {row["case_id"]: row for row in source_rows if row["config_id"] == "K1-k3-a2"
                and row["case_id"] in CASE_IDS}
    if set(selected) != set(CASE_IDS) or any(not selected[case_id]["evidence"] for case_id in CASE_IDS):
        raise ValueError("Missing fixed evidence for paired cases")
    output.mkdir(parents=True, exist_ok=False)
    save(output / "manifest.json", {"stage": "EXP-3-offline", "experiment_id": output.name,
                                    "source_experiment": source.name,
                                    "source_retrieval_sha256": hashlib.sha256(source_bytes).hexdigest(),
                                    "case_ids": CASE_IDS, "modes": MODES, "shared_request_budget": 8,
                                    "final_reserve": 2, "external_provider": None,
                                    "external_spend_cny": 0, "model_usage": None,
                                    "status": "running"})
    rows = []
    for case_id in CASE_IDS:
        evidence = selected[case_id]["evidence"]
        for mode in MODES:
            run_id = f"{output.name}:{case_id}:{mode}"
            budget = SharedBudget(output / f"budget-{case_id}-{mode}.json", run_id)
            result = run_fixed_evidence(mode, evidence, budget)
            answer = result["answer"].casefold()
            facts = cases[case_id]["facts"]
            rows.append({"run_id": run_id, "case_id": case_id, "family": cases[case_id]["family"],
                         "mode": mode, "evidence_ids": [item["chunk_id"] for item in evidence],
                         "retrieval": grade(cases[case_id], evidence),
                         "literal_facts_in_extractive_answer": sum(fact.casefold() in answer for fact in facts),
                         "fact_count": len(facts), "request_count_simulated": result["request_count"],
                         "child_count": len(result["children"]), "verified_claims": result["accepted"],
                         "rejected_claims": result["rejected"], "citation_ids": result["citations"],
                         "answer": result["answer"], "children": result["children"],
                         "provider_usage": None, "provider_cost_cny": None,
                         "semantic_quality": None, "latency_ms": None})
    (output / "runs.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                      encoding="utf-8")
    summary = []
    for mode in MODES:
        group = [row for row in rows if row["mode"] == mode]
        summary.append({"mode": mode, "cases": len(group),
                        "literal_fact_cases_complete": sum(row["literal_facts_in_extractive_answer"] == row["fact_count"]
                                                           for row in group if row["fact_count"]),
                        "fact_cases": sum(bool(row["fact_count"]) for row in group),
                        "verified_claims": sum(row["verified_claims"] for row in group),
                        "rejected_claims": sum(row["rejected_claims"] for row in group),
                        "simulated_requests": sum(row["request_count_simulated"] for row in group),
                        "children": sum(row["child_count"] for row in group)})
    report = {"stage": "EXP-3-offline", "source": source.name, "summary": summary,
              "quality_observed": False, "latency_observed": False, "provider_cost_observed": False,
              "limits": "Deterministic extraction, sequential reservations and fixed evidence only; no model agents or semantic citation grading."}
    save(output / "report.json", report)
    lines = ["# EXP-3 离线固定证据配对对照", "",
             f"来源：`{source.name}` 的 K1-k3-a2 封存证据；4 题 × A0/A1/A2，均使用相同 8 次模拟总请求额度。", "",
             "| 配置 | 字面事实全覆盖/可答题 | 已核对原文断言 | 模拟请求合计 | 子任务合计 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for item in summary:
        lines.append(f"| {item['mode']} | {item['literal_fact_cases_complete']}/{item['fact_cases']} | "
                     f"{item['verified_claims']} | {item['simulated_requests']} | {item['children']} |")
    lines += ["", "A0 是确定性抽取；A1 增加一次引用核查；A2 按证据 ID 分给最多两个抽取子任务。",
              "引用核查只验证原文精确匹配、版本、可见性和证据 ID，不评估语义推断；故障注入测试另见单元测试。",
              "请求是离线模拟次数，不是实际 provider 调用。未测语言模型质量、引用支持率、耗时、真实费用或并发预算原子性。",
              "既有 D9 真实单 Agent 引用基线尚未过门槛，因此不启用线上 Subagent 自动触发。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "completed"
    save(output / "manifest.json", manifest)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="exp2-20261012-holdout-k3a2")
    parser.add_argument("--experiment-id", default=datetime.now(timezone.utc).strftime("exp3-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    if not args.experiment_id.startswith("exp3-") or any(character not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in args.experiment_id):
        parser.error("experiment-id must be a simple exp3-* identifier")
    try:
        report = run(OUTPUT_ROOT / args.source, OUTPUT_ROOT / args.experiment_id)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Experiment failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"EXP-3: {len(report['summary'])} modes; {OUTPUT_ROOT / args.experiment_id / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
