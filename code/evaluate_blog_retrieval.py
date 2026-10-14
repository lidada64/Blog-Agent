"""Run EXP-0 against synthetic articles and a deterministic mock generator only."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from blog_assistant.experiment import (digest, replay_d9, run_case,
                                       validate_dataset, write_fixtures)

ROOT = Path(__file__).resolve().parent.parent
DATASET = Path(__file__).parent / "evals" / "retrieval-exp0.json"
OUTPUT_ROOT = ROOT / ".blog-agent-data" / "experiments"


def git_identity():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    diff = git("diff", "--binary")
    return {"commit": head.decode().strip() if head else None,
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest() if diff is not None else None,
            "untracked_included_in_hash": False,
            "experiment_source_sha256": source_digest()}


def source_digest():
    files = [Path(__file__), Path(__file__).parent / "blog_assistant" / "experiment.py", DATASET]
    return hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def annotated_cases(posts, cases):
    annotations = []
    for case in cases:
        annotated = dict(case)
        annotated["allowed_visibility"] = ["local_article"]
        annotated["forbidden_conclusions"] = ["unsupported assertion", "draft disclosure"]
        annotated["support"] = []
        for fact in case["facts"]:
            matches = []
            for article_id in case["required"]:
                post = posts.snapshot(article_id)
                matches.extend({"article_id": article_id, "version": post["version"], "line": index}
                               for index, line in enumerate(post["body"].splitlines(), 1)
                               if fact.casefold() in line.casefold())
            if not matches:
                raise ValueError(f"Annotation has no supporting line: {case['id']}")
            annotated["support"].append({"fact": fact, "ranges": matches})
        annotations.append(annotated)
    return annotations


def run(output, split="tune"):
    dataset = validate_dataset(json.loads(DATASET.read_text(encoding="utf-8")))
    output = output.resolve()
    if not output.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("Experiment output must be inside .blog-agent-data/experiments")
    output.mkdir(parents=True, exist_ok=False)
    posts = write_fixtures(dataset, output / "fixtures")
    selected = [case for case in dataset["cases"] if split == "all" or case["split"] == split]
    manifest = {"experiment_id": output.name, "stage": "EXP-0", "schema": dataset["version"],
                "status": "running", "git": git_identity(), "dataset_sha256": digest(dataset),
                "fixture_sha256": digest(dataset["articles"]), "rubric_version": "exp0-rules-v1",
                "seed": dataset["seed"], "split": split, "model": "deterministic-extractive-mock",
                "provider": None, "token_method": "unobserved; bytes only", "max_model_requests": 8,
                "max_exploration_requests": 6, "max_recovery_requests": 1,
                "authorization": "synthetic fixtures; offline only", "spend_limit": 0,
                "spend_observed": 0, "index_version": None, "chunk_version": "production-read-80-lines",
                "query_order": [case["id"] for case in selected], "next_step": "EXP-1 tune-set retrieval comparison"}
    write_json(output / "manifest.json", manifest)
    write_json(output / "cases.json", annotated_cases(posts, selected))
    write_json(output / "prices.json", {"provider": None, "prices": None, "reason": "offline mock"})
    (output / "checkpoint").mkdir()
    results = []
    for case in selected:
        result = run_case(posts, case, simulate_truncation=case["id"] == "t03",
                          checkpoint=output / "checkpoint" / f"{case['id']}.json")
        result["run_id"] = f"{output.name}:{case['id']}:1"
        result["question_id"] = case["id"]
        result["repetition"] = 1
        result["config_id"] = "fixed-read-80-mock-v1"
        result["parent_id"] = None
        for index, call in enumerate(result["tool_calls"], 1):
            call["tool_call_id"] = f"{result['run_id']}:tool:{index}"
        results.append(result)
    write_jsonl(output / "requests.jsonl", [dict(run_id=result["run_id"], question_id=result["question_id"],
                                                    repetition=1, config_id=result["config_id"], parent_id=None,
                                                    **request)
                                                    for result in results for request in result["requests"]])
    write_jsonl(output / "retrieval.jsonl", [{"run_id": result["run_id"], "question_id": result["question_id"],
                                              "repetition": 1, "config_id": result["config_id"], "parent_id": None,
                                              "retrieval": result["retrieval"],
                                              "tool_calls": result["tool_calls"], "evidence": result["evidence"],
                                              "error_type": result["error_type"]} for result in results])
    write_jsonl(output / "quality.jsonl", [{"run_id": result["run_id"], "question_id": result["question_id"],
                                            "repetition": 1, "config_id": result["config_id"], "parent_id": None,
                                            "answer": result["answer"],
                                            "answer_status": result["answer_status"],
                                            "citations": result["citations"]} for result in results])
    replay = replay_d9(posts)
    write_json(output / "d9-replay.json", replay)
    families = {}
    for result in results:
        entry = families.setdefault(result["family"], {"count": 0, "retrieval_hits": 0,
                                                        "completed": 0, "errors": 0})
        entry["count"] += 1
        entry["retrieval_hits"] += result["retrieval"]["recall"] == 1
        entry["completed"] += result["answer"]["complete_answer"]
        entry["errors"] += result["error_type"] is not None
    report = {"stage": "EXP-0", "dataset_sha256": manifest["dataset_sha256"],
              "split": split, "cases": len(results), "families": families, "d9_replay": replay,
              "usage_observed_requests": 0, "actual_cost": 0,
              "semantic_quality_observed": False, "results": [{k: result[k] for k in
              ("case_id", "family", "retrieval", "answer", "answer_status", "error_type",
               "serialized_input_bytes", "evidence_utf8_bytes", "duplicate_body_bytes", "budget")}
              for result in results]}
    write_json(output / "report.json", report)
    lines = ["# EXP-0 离线检索与证据报告", "", f"数据集：{manifest['dataset_sha256']}；分组：{split}；题数：{len(results)}", "",
             "合成文章与抽取式模拟回答。规则事实覆盖仅检查字面片段，真实语义质量、token 用量与服务费用未观测。", "",
             "| 任务族 | 样本 | 检索全命中 | 回答结束 | 读取错误 |", "| --- | ---: | ---: | ---: | ---: |"]
    for family, value in sorted(families.items()):
        lines.append(f"| {family} | {value['count']} | {value['retrieval_hits']} | {value['completed']} | {value['errors']} |")
    lines += ["", "D9 形状回放：历史 7 次读取，8/8 请求后没有恢复槽；实验配置预留 6 次探索、1 次回答、1 次恢复。",
              "", "逐题检索、证据、回答规则、请求和检查点见同目录 JSON/JSONL。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    manifest["status"] = "completed"
    write_json(output / "manifest.json", manifest)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("tune", "holdout", "all"), default="tune")
    parser.add_argument("--experiment-id", default=datetime.now(timezone.utc).strftime("exp0-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    if not args.experiment_id.startswith("exp0-") or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in args.experiment_id):
        parser.error("experiment-id must be a simple exp0-* identifier")
    try:
        report = run(OUTPUT_ROOT / args.experiment_id, args.split)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Experiment failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"EXP-0: {report['cases']} cases; {OUTPUT_ROOT / args.experiment_id / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
