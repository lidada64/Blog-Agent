"""Offline EXP-2 K0/K1 retrieval comparison on synthetic fixtures."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from blog_assistant.experiment import canonical, digest, validate_dataset, write_fixtures
from blog_assistant.retrieval_methods import build_chunks, rank_bm25, rank_historical

ROOT = Path(__file__).resolve().parent.parent
EVALS = Path(__file__).parent / "evals"
OUTPUT_ROOT = ROOT / ".blog-agent-data" / "experiments"
INPUT_EVIDENCE_BYTES = 12000


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cases_for(split):
    dataset = validate_dataset(json.loads((EVALS / "retrieval-exp0.json").read_text(encoding="utf-8")))
    if split == "tune":
        return dataset, [case for case in dataset["cases"] if case["split"] == "tune"], None
    frozen = json.loads((EVALS / "retrieval-exp1-holdout.json").read_text(encoding="utf-8"))
    if frozen.get("version") != "exp1-holdout-v2" or frozen.get("source_dataset") != dataset["version"]:
        raise ValueError("Unsupported frozen holdout")
    return dataset, frozen["cases"], digest(frozen)


def select(rows, byte_limit=INPUT_EVIDENCE_BYTES):
    delivered, remaining = [], byte_limit
    for row in rows:
        item = row["chunk"]
        cost = len(canonical(item))
        if cost <= remaining:
            delivered.append(item)
            remaining -= cost
    return delivered


def grade(case, delivered):
    required = set(case["required"])
    found = [item["article_id"] for item in delivered]
    unique = set(found)
    first_rank = next((index for index, article in enumerate(found, 1) if article in required), None)
    text = "\n".join(item["text"] for item in delivered).casefold()
    facts = case["facts"]
    return {"source_recall": len(required & unique) / len(required) if required else None,
            "mrr": 1 / first_rank if first_rank else 0 if required else None,
            "fact_literal_coverage": sum(fact.casefold() in text for fact in facts) / len(facts) if facts else None,
            "unanswerable_candidate_count": len(delivered) if not case.get("answerable", True) else None,
            "missing_sources": sorted(required - unique)}


def run(output, split, top_k=None, per_article=None):
    output = output.resolve()
    if not output.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("Output must stay in ignored experiment root")
    dataset, cases, holdout_hash = cases_for(split)
    if split == "holdout" and (top_k not in {3, 5} or per_article not in {2, 3}):
        raise ValueError("Holdout requires frozen top-k and per-article limits")
    configs = [(3, 2), (3, 3), (5, 2), (5, 3)] if split == "tune" else [(top_k, per_article)]
    output.mkdir(parents=True, exist_ok=False)
    posts = write_fixtures(dataset, output / "fixtures")
    built = build_chunks(posts, target_tokens=300)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=False)
    manifest = {"stage": "EXP-2-offline", "experiment_id": output.name, "split": split,
                "dataset_sha256": digest(dataset), "holdout_sha256": holdout_hash,
                "git_commit": revision.stdout.decode().strip() if revision.returncode == 0 else None,
                "source_sha256": hashlib.sha256(b"".join(path.read_bytes() for path in
                    (Path(__file__), Path(__file__).parent / "blog_assistant" / "retrieval_methods.py",
                     EVALS / "retrieval-exp0.json", EVALS / "retrieval-exp1-holdout.json"))).hexdigest(),
                "index_version": built["index_version"], "catalog_version": built["catalog_version"],
                "chunk_target_estimated_tokens": 300, "evidence_limit_bytes": INPUT_EVIDENCE_BYTES,
                "configs": [{"method": "K0"}] + [{"method": "K1", "top_k": k, "per_article": a}
                                                  for k, a in configs],
                "provider": None, "embedding_model": None, "spend_cny": 0,
                "authorization": "synthetic fixtures and offline computation only",
                "status": "running"}
    save(output / "manifest.json", manifest)
    save(output / "cases.json", cases)
    save(output / "index.json", {"index_version": built["index_version"], "chunks": built["chunks"],
                                 "issues": built["issues"]})
    rows = []
    for case in cases:
        historical = rank_historical(posts, case["query"])
        candidates = [("K0", historical["evidence"])]
        for k, article_limit in configs:
            ranked = rank_bm25(built["chunks"], case["query"], top_k=k, per_article=article_limit)
            candidates.append((f"K1-k{k}-a{article_limit}", select(ranked)))
        for config, delivered in candidates:
            rows.append({"run_id": f"{output.name}:{config}:{case['id']}", "case_id": case["id"],
                         "family": case["family"], "config_id": config, "retrieval": grade(case, delivered),
                         "evidence": delivered, "evidence_bytes": sum(len(canonical(item)) for item in delivered),
                         "tool_calls": 1 if config != "K0" else 1 + len(delivered),
                         "usage_observed": False, "model_cost_cny": None, "answer_quality": None})
    (output / "retrieval.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                           encoding="utf-8")
    summary = []
    for config in dict.fromkeys(row["config_id"] for row in rows):
        group = [row for row in rows if row["config_id"] == config]
        eligible = [row for row in group if row["retrieval"]["source_recall"] is not None]
        full = [row for row in group if row["retrieval"]["fact_literal_coverage"] is not None]
        summary.append({"config_id": config, "cases": len(group),
                        "required_source_complete": sum(row["retrieval"]["source_recall"] == 1 for row in eligible),
                        "source_cases": len(eligible),
                        "literal_facts_complete": sum(row["retrieval"]["fact_literal_coverage"] == 1 for row in full),
                        "fact_cases": len(full),
                        "evidence_bytes": sum(row["evidence_bytes"] for row in group),
                        "unanswerable_candidates": sum(row["retrieval"]["unanswerable_candidate_count"] or 0
                                                       for row in group)})
    report = {"stage": "EXP-2-offline", "split": split, "summary": summary,
              "semantic_methods": "S1/H1 unavailable: no authorized embedding provider/model; vector lifecycle uses mock tests only",
              "answer_quality_observed": False, "provider_cost_observed": False,
              "limits": "Literal fact presence and article recall are not semantic correctness or precise citation support."}
    save(output / "report.json", report)
    lines = ["# EXP-2 离线检索对照", "", f"分组：{split}；合成题 {len(cases)}；无外部模型调用。", "",
             "| 方法 | 必需来源全覆盖/适用题 | 必需片段字面覆盖/适用题 | 序列化证据字节 | 无答案题候选片段 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for item in summary:
        lines.append(f"| {item['config_id']} | {item['required_source_complete']}/{item['source_cases']} | "
                     f"{item['literal_facts_complete']}/{item['fact_cases']} | {item['evidence_bytes']} | "
                     f"{item['unanswerable_candidates']} |")
    lines += ["", "K0 保留旧关键词/前80行读取；K1 使用相同合成语料、Markdown 段落块和 BM25。"
              "结果只衡量检索与字面片段；没有真实回答、引用支持率、embedding 或费用。",
              "S1/H1 仅有离线向量接口、更新/删除/维度/融合故障测试，未做语义质量对照。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    manifest["status"] = "completed"
    save(output / "manifest.json", manifest)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("tune", "holdout"), default="tune")
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--per-article", type=int)
    parser.add_argument("--experiment-id", default=datetime.now(timezone.utc).strftime("exp2-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    if not args.experiment_id.startswith("exp2-") or any(character not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in args.experiment_id):
        parser.error("experiment-id must be a simple exp2-* identifier")
    try:
        result = run(OUTPUT_ROOT / args.experiment_id, args.split, args.top_k, args.per_article)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Experiment failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"EXP-2: {result['split']}; {OUTPUT_ROOT / args.experiment_id / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
