"""Grade a local Blog-Agent checkpoint without model calls or tool execution."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agent_core.state import atomic_write_json
from agent_core.trace_grading import grade_trace, fingerprint, validate_rubric
from blog_assistant.permissions import MODE_TOOLS

ROOT = Path(__file__).resolve().parents[1]


def provenance():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=10)
        return result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else None
    head, changes, diff = git("rev-parse", "HEAD"), git("status", "--porcelain"), git("diff", "HEAD")
    # Untracked source files must also affect provenance, before the first commit.
    sources = {str(path.relative_to(ROOT)): fingerprint(path.read_text(encoding="utf-8"))
               for path in (ROOT / "code").rglob("*.py") if "__pycache__" not in path.parts}
    return {"commit": head.strip() if head else None,
            "worktree_dirty": bool(changes) if changes is not None else None,
            "worktree_diff_hash": fingerprint(diff) if diff is not None else None,
            "python_source_hash": fingerprint(sources)}


def write_report(report, output):
    atomic_write_json(output / "report.json", report)
    verdict = "PASS" if report["passed"] is True else "FAIL" if report["passed"] is False else "NEEDS_REVIEW"
    lines = ["# Trace grading 报告", "", f"结果：{verdict}",
             "", f"规则评分：{report.get('score', 0):.1%}；语义质量：未评分。"]
    for name, group in report.get("groups", {}).items():
        value = "不适用" if group["score"] is None else f"{group['score']:.1%}"
        lines.append(f"- {name}: {value} ({group['checks']} checks)")
    lines += ["", "## 失败检查", ""]
    lines += [f"- {c['name']}：{c['failure_category']}；证据 ID：{', '.join(map(str, c['evidence_ids'])) or '缺失'}"
              for c in report.get("checks", []) if not c["passed"]]
    if report.get("error_type"):
        lines += [f"- 输入/评分错误：{report['error_type']}"]
    lines += ["", "## 观测限制", ""]
    lines += [f"- {warning}" for warning in report.get("warnings", [])]
    lines += ["- 未配置任务断言时，过程检查通过仍显示 NEEDS_REVIEW。",
              "- expected read 仅核对实际读取证据，不判断自由文本引用的语义支持。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, default=ROOT / "code/evals/trace-rubric.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--case-id", default="checkpoint")
    parser.add_argument("--dataset-version", default="manual-checkpoint-v1")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir or ROOT / ".blog-agent-data/trace-grades" / stamp
    if output.exists() and any(output.iterdir()):
        parser.error("Output directory must be empty; existing results are preserved")
    output.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(args.state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("kind") != "blog_conversation":
            raise ValueError("A Blog-Agent conversation checkpoint is required")
        mode = state.get("blog", {}).get("mode")
        if mode not in MODE_TOOLS:
            raise ValueError("Missing Blog-Agent mode")
        rubric = validate_rubric(json.loads(args.rubric.read_text(encoding="utf-8")))
        from blog_assistant.tools import BLOG_TOOLS, validate_arguments
        schemas = {tool["name"]: tool["parameters"] for tool in BLOG_TOOLS}
        report = grade_trace(state, rubric, allowed_tools=MODE_TOOLS[mode],
                             argument_validator=lambda name, arguments: validate_arguments(schemas[name], arguments))
        if any(c["name"] == "trace_schema" and c["passed"] for c in report["checks"]):
            atomic_write_json(output / "trace.json", state["trace"])
        report.update(provenance=provenance(), config={
            "model": state.get("model"), "compact_mode": state.get("compact_mode"),
            "compact_threshold": state.get("compact_threshold"), "mode": mode,
        })
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        report = {"report_version": 1, "passed": False, "error_type": type(exc).__name__,
                  "failure_counts": {"invalid_input": 1}, "warnings": ["Input rejected; raw contents/errors are omitted."]}
    report.update(created_at=stamp, case_id=args.case_id, dataset_version=args.dataset_version)
    write_report(report, output)
    verdict = "PASS" if report["passed"] is True else "FAIL" if report["passed"] is False else "NEEDS_REVIEW"
    print(f"{verdict}: {output / 'report.md'}")
    return 0 if report["passed"] is True else 1 if report["passed"] is False else 2


if __name__ == "__main__":
    raise SystemExit(main())
