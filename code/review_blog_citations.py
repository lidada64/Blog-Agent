"""Prepare an offline, manual assertion review from saved Blog Agent checkpoints."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from blog_assistant.citations import audit_answer
from blog_assistant.retrieval import SamplePosts


ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = ROOT / ".blog-agent-data" / "experiments"
LABELS = {"supported", "unsupported", "unknown", "not_claim"}


def review_units(reply):
    """Conservative line-level units; reviewers split compound lines manually."""
    for number, line in enumerate(reply.splitlines(), 1):
        stripped = line.strip()
        if (not stripped or stripped.startswith("#") or re.fullmatch(r"[-*_=|\s]+", stripped)
                or stripped.startswith("|")):
            continue
        yield number, stripped


def review(paths, output):
    output = output.resolve()
    if not output.is_relative_to(EXPERIMENTS.resolve()):
        raise ValueError("Review output must stay in ignored experiment root")
    if output.exists():
        raise ValueError("Review output already exists")
    records, summary = [], []
    spot_path = paths[0].parent / "quality.jsonl"
    spot = ({row["run_id"]: row for row in (json.loads(line) for line in
             spot_path.read_text(encoding="utf-8").splitlines())} if spot_path.exists() else {})
    for path in paths:
        path = path.resolve()
        if not path.is_relative_to(EXPERIMENTS.resolve()):
            raise ValueError("Only saved experiment checkpoints may be reviewed")
        raw = path.read_bytes()
        state = json.loads(raw.decode("utf-8"))
        turns = [turn for turn in state["turns"] if turn.get("kind") == "user" and turn.get("reply")]
        if not turns:
            raise ValueError(f"No answered user turn in {path.name}")
        turn = turns[-1]
        steps = {step["number"] for step in state["steps"] if step.get("turn") == turn["number"]}
        calls = [call for call in state["tool_calls"] if call["step"] in steps]
        posts = SamplePosts(state["blog"]["posts_dir"])
        audit = audit_answer(turn["reply"], calls, posts)
        units = list(review_units(turn["reply"]))
        for index, (line_number, text) in enumerate(units, 1):
            records.append({"run_id": path.stem, "turn_number": turn["number"],
                            "unit_id": f"{path.stem}:u{index:03}", "answer_line": line_number,
                            "text": text, "unit_scope": "answer line; split compound claims during review",
                            "atomic_assertion": False, "citation_reference": None,
                            "citation_location_valid": None, "manual_support": None, "reviewer_note": None})
        summary.append({"run_id": path.stem, "checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
                        "result": turn.get("result"), "answer_lines_for_review": len(units),
                        "machine_citation_status": audit["status"],
                        "located_markers": audit["located"], "invalid_markers": audit["invalid"],
                        "prior_spot_audit": spot.get(path.stem, {}).get("citation_audit"),
                        "semantic_support_rate": None})
    output.mkdir(parents=True)
    (output / "review-units.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                                     for row in records), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    lines = ["# D9 逐条引用人工审阅准备", "", "本报告仅整理待审单位；每行可能有多条断言，需人工拆分后标注。",
             "机器仅核对新格式引用的位置、版本和已读范围，不判断语义支持。旧自由格式回答不能自动计为引用正确。", "",
             "| 运行 | 待拆分/核对的回答行 | 新格式引用状态 | 原抽查 | 语义支持率 |",
             "| --- | ---: | --- | --- | --- |"]
    for row in summary:
        lines.append(f"| {row['run_id']} | {row['answer_lines_for_review']} | "
                     f"{row['machine_citation_status']} | {row['prior_spot_audit'] or '无'} | 未评分 |")
    lines += ["", "逐条文本在 `review-units.jsonl`，人工标签保留空值；不得以未评分样本计算 95% 门槛。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def score_review(path):
    """Only complete human labels can produce a semantic support denominator."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    ids = [row["unit_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate review unit")
    labels = [row.get("manual_support") for row in rows]
    if any(label not in LABELS and label is not None for label in labels):
        raise ValueError("Unsupported manual label")
    if any(row.get("manual_support") == "supported" and row.get("citation_location_valid") is False
           for row in rows):
        raise ValueError("A structurally invalid citation cannot be labeled supported")
    pending = sum(label is None or label == "unknown" or
                  (label in {"supported", "unsupported"} and
                   (row.get("atomic_assertion") is not True or
                    not isinstance(row.get("citation_reference"), str) or
                    not row["citation_reference"].strip() or
                    type(row.get("citation_location_valid")) is not bool))
                  for row, label in zip(rows, labels))
    denominator = sum(label in {"supported", "unsupported"} for label in labels)
    supported = sum(label == "supported" for label in labels)
    return {"units": len(rows), "pending_or_unknown": pending,
            "supported": supported, "unsupported": denominator - supported,
            "semantic_support_rate": supported / denominator if denominator and not pending else None,
            "status": "complete" if denominator and not pending else "incomplete"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Saved experiment directory under .blog-agent-data/experiments")
    parser.add_argument("--output", required=True, help="New output directory under .blog-agent-data/experiments")
    parser.add_argument("--score-existing", action="store_true",
                        help="Read manual labels in an existing output; never auto-fill them")
    args = parser.parse_args()
    output = (EXPERIMENTS / args.output).resolve()
    if args.score_existing:
        if not output.is_relative_to(EXPERIMENTS.resolve()):
            parser.error("Output must stay in ignored experiment root")
        try:
            print(json.dumps(score_review(output / "review-units.jsonl"), ensure_ascii=False))
        except (OSError, ValueError, KeyError) as exc:
            print(f"Review scoring failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0
    if not args.source:
        parser.error("--source is required when preparing a new review")
    source = (EXPERIMENTS / args.source).resolve()
    paths = sorted(path for path in source.glob("cache-compact-compare-low-*.json")
                   if not path.name.endswith(".trace.json"))
    if len(paths) != 5:
        parser.error("Expected exactly five saved D9 comparison checkpoints")
    try:
        summary = review(paths, output)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Review preparation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Prepared {len(summary)} runs at {EXPERIMENTS / args.output / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
