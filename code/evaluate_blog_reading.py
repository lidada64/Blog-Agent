"""Compare EXP-1 reading modes offline; holdout requires a frozen config."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from blog_assistant.experiment import (digest, mock_answer, score_answer,
                                       score_retrieval, validate_dataset, write_fixtures)
from blog_assistant.reading_experiment import retrieve

ROOT = Path(__file__).resolve().parent.parent
EVALS = Path(__file__).parent / "evals"
OUTPUT_ROOT = ROOT / ".blog-agent-data" / "experiments"
MODES = (("r0", 0.75), ("full", 0.75), ("section", 0.75),
         ("paragraph", 0.75), ("auto", 0.25), ("auto", 0.5), ("auto", 0.75))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(split):
    dataset = validate_dataset(json.loads((EVALS / "retrieval-exp0.json").read_text(encoding="utf-8")))
    if split == "tune":
        cases = [case for case in dataset["cases"] if case["split"] == "tune"]
        holdout_hash = None
    else:
        frozen = json.loads((EVALS / "retrieval-exp1-holdout.json").read_text(encoding="utf-8"))
        if frozen.get("version") != "exp1-holdout-v2" or frozen.get("source_dataset") != dataset["version"]:
            raise ValueError("Unsupported holdout version")
        cases = frozen["cases"]
        if len(cases) != 12 or len({case["id"] for case in cases}) != 12:
            raise ValueError("Invalid frozen holdout")
        holdout_hash = digest(frozen)
    return dataset, cases, holdout_hash


def run(output, split="tune", frozen_mode=None, frozen_fraction=None):
    dataset, cases, holdout_hash = load(split)
    output = output.resolve()
    if not output.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("Output must be inside the ignored experiment directory")
    if split == "holdout":
        if frozen_mode not in {"r0", "full", "section", "paragraph", "auto"} or frozen_fraction not in {0.25, 0.5, 0.75}:
            raise ValueError("Holdout requires a preselected mode and threshold")
        configs = [("r0", 0.75), (frozen_mode, frozen_fraction)]
    else:
        configs = MODES
    output.mkdir(parents=True, exist_ok=False)
    posts = write_fixtures(dataset, output / "fixtures")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=False)
    manifest = {"experiment_id": output.name, "stage": "EXP-1", "split": split,
                "dataset_sha256": digest(dataset), "holdout_sha256": holdout_hash,
                "git_commit": revision.stdout.decode().strip() if revision.returncode == 0 else None,
                "source_sha256": hashlib.sha256(b"".join(path.read_bytes() for path in
                    (Path(__file__), Path(__file__).parent / "blog_assistant" / "reading_experiment.py",
                     EVALS / "retrieval-exp0.json", EVALS / "retrieval-exp1-holdout.json"))).hexdigest(),
                "configs": [{"mode": mode, "fraction": fraction} for mode, fraction in configs],
                "provider": None, "model": "deterministic-extractive-mock", "spend_limit": 0,
                "actual_spend": 0, "token_method": "unobserved; serialized bytes only",
                "authorization": "synthetic articles only", "status": "running"}
    save(output / "manifest.json", manifest)
    save(output / "cases.json", cases)
    rows = []
    for mode, fraction in configs:
        config_id = f"{mode}-{int(fraction * 100)}"
        for case in cases:
            result = retrieve(posts, case, mode, fraction)
            answer = mock_answer(result["evidence"])
            rows.append({"run_id": f"{output.name}:{config_id}:{case['id']}",
                         "question_id": case["id"], "family": case["family"], "config_id": config_id,
                         "route": [decision["route"] for decision in result["decisions"]],
                         "reasons": [decision["reason"] for decision in result["decisions"]],
                         "retrieval": score_retrieval(case, result["evidence"]),
                         "quality": score_answer(case, answer, result["evidence"]),
                         "tool_calls": len(result["calls"]), "serialized_input_bytes": result["serialized_input_bytes"],
                         "output_reserve_bytes": result["output_reserve_bytes"],
                         "evidence": result["evidence"]})
    (output / "runs.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    summaries = []
    for mode, fraction in configs:
        config_id = f"{mode}-{int(fraction * 100)}"
        group = [row for row in rows if row["config_id"] == config_id]
        eligible = [row for row in group if row["quality"]["fact_coverage"] is not None]
        summaries.append({"config_id": config_id, "cases": len(group),
                          "all_facts_present": sum(row["quality"]["fact_coverage"] == 1 for row in eligible),
                          "fact_cases": len(eligible),
                          "complete_articles": sum(len(row["retrieval"]["complete_articles"]) for row in group),
                          "input_bytes": sum(row["serialized_input_bytes"] for row in group),
                          "tool_calls": sum(row["tool_calls"] for row in group),
                          "unanswered_or_unsafe": [row["question_id"] for row in group
                                                  if row["quality"]["no_answer_correct"] is False]})
    report = {"stage": "EXP-1", "split": split, "cases": len(cases), "summaries": summaries,
              "input_limit_bytes": 16000, "output_reserve_bytes": 3000,
              "observed_model_usage": False, "semantic_quality_observed": False,
              "cost_observed": False,
              "limits": "No production routing or model generation was changed; full-text budget uses serialized UTF-8 bytes."}
    save(output / "report.json", report)
    lines = ["# EXP-1 离线返回方式对照", "", f"分组：{split}；合成题：{len(cases)}。事实覆盖仅检查标注字面片段是否出现在模拟回答，不是语义正确率。", "",
             "| 配置 | 必需事实全部出现/适用题 | 整篇交付数 | 序列化输入字节合计 | 工具调用合计 |", "| --- | ---: | ---: | ---: | ---: |"]
    for item in summaries:
        lines.append(f"| {item['config_id']} | {item['all_facts_present']}/{item['fact_cases']} | {item['complete_articles']} | {item['input_bytes']} | {item['tool_calls']} |")
    lines += ["", "| 配置 | 任务族 | 路由 | 必需事实覆盖 | 错误终态 |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(f"| {row['config_id']} | {row['family']} ({row['question_id']}) | {','.join(row['route']) or '无证据'} | "
                     f"{row['quality']['fact_coverage'] if row['quality']['fact_coverage'] is not None else '不适用'} | "
                     f"{'是' if row['quality']['no_answer_correct'] is False else '否'} |")
    lines += ["", "具体回退原因、证据版本与行号见 runs.jsonl。模型 token、费用与真实问答质量均未观测。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    manifest["status"] = "completed"
    save(output / "manifest.json", manifest)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("tune", "holdout"), default="tune")
    parser.add_argument("--frozen-mode", choices=("r0", "full", "section", "paragraph", "auto"))
    parser.add_argument("--frozen-fraction", type=float)
    parser.add_argument("--experiment-id", default=datetime.now(timezone.utc).strftime("exp1-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    if not args.experiment_id.startswith("exp1-") or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in args.experiment_id):
        parser.error("experiment-id must be a simple exp1-* identifier")
    try:
        result = run(OUTPUT_ROOT / args.experiment_id, args.split, args.frozen_mode, args.frozen_fraction)
    except (ValueError, OSError, KeyError) as exc:
        print(f"Experiment failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"EXP-1: {result['cases']} cases; {OUTPUT_ROOT / args.experiment_id / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
