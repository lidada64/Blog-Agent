"""Evaluate offline regressions or a checkpoint; writes JSON and Markdown."""

import argparse
import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from agent_core.evaluation import evaluate_state, validate_dataset


def offline():
    # Tests exercise production code with mock models and real local files/processes.
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
    stream = io.StringIO()
    started = perf_counter()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {"passed": result.wasSuccessful() and result.testsRun > 0,
            "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "elapsed_seconds": perf_counter() - started,
            "log": stream.getvalue()}


def save_report(report, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Agent 评估报告", "", f"模式：{report['mode']}；结果：{'PASS' if report['passed'] else 'FAIL'}", ""]
    if "error_type" in report:
        lines += [f"评估未能完成：{report['error_type']}", ""]
    if report["mode"] == "offline" and "tests" in report:
        lines += [f"测试：{report['tests']}；失败：{report['failures']}；错误：{report['errors']}；跳过：{report['skipped']}", "", "完整日志见 report.json。"]
    for case in report.get("cases", [report] if report["mode"] == "checkpoint" and "checks" in report else []):
        metrics = case["metrics"]
        lines += [f"## {case.get('id', 'checkpoint')}", "", f"结果：{'PASS' if case['passed'] else 'FAIL'}；规则得分：{case['score']:.1%}",
                  f"模型请求：{metrics['requests']}；API 总耗时：{metrics['elapsed_seconds']:.3f}s；压缩：{metrics['compactions']}",
                  f"已观测 input/output/cache token：{metrics['input_tokens']}/{metrics['output_tokens']}/{metrics['cached_tokens']}",
                  f"usage 可观测请求：{metrics['usage_observed_requests']}/{metrics['requests']}；缓存可观测请求：{metrics['cache_observed_requests']}/{metrics['requests']}",
                  f"缓存 token 命中率：{metrics['cache_hit_ratio']:.1%}" if metrics['cache_hit_ratio'] is not None else "缓存 token 命中率：未知", ""]
        lines += [f"- FAIL {c['name']}: {c['detail']}" for c in case["checks"] if not c["passed"]]
        if case.get("error"):
            lines += [f"- 执行错误：{case['error']['type']}"]
    lines += ["", "规则得分不代表语义质量。旧检查点缺少 usage 时，token 总和仅包含已观测请求。"]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "checkpoint"), default="offline")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--expect-file", type=Path, help="JSON expectation object for checkpoint mode")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.mode == "checkpoint" and not args.state_file:
        parser.error("checkpoint mode requires --state-file")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output_dir or Path(__file__).parent / "eval_results" / stamp).resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output-dir must be empty; previous reports/checkpoints are preserved")
    output.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "offline":
            report = offline()
        elif args.mode == "checkpoint":
            state = json.loads(args.state_file.read_text(encoding="utf-8"))
            expected = json.loads(args.expect_file.read_text(encoding="utf-8")) if args.expect_file else {}
            validate_dataset([{"id": "checkpoint", "messages": ["inspect"], "expect": expected}]) if expected else None
            report = evaluate_state(state, expected)
    except Exception as exc:
        report = {"passed": False, "error_type": type(exc).__name__, "cases": []}
        print(f"Evaluation failed: {type(exc).__name__}", file=sys.stderr)
    report.update(mode=args.mode, created_at=stamp)
    save_report(report, output)
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {output / 'report.md'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
