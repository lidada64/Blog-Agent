"""Offline trajectory grading over recorded evidence; never execute tools/models."""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from .tracing import EVENT_TYPES, FIELDS, SCHEMA_VERSION
from .metrics import summarize_metrics

GRADER_VERSION = "1.1"
WEIGHTS = {"structure": 30, "safety": 30, "task": 30, "budget": 10}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_rubric(rubric):
    if not isinstance(rubric, dict) or set(rubric) - {"version", "id", "expect", "budgets"}:
        raise ValueError("Invalid rubric fields")
    if type(rubric.get("version")) is not int or rubric.get("version") != 1 or not isinstance(rubric.get("id"), str) or not rubric["id"]:
        raise ValueError("Rubric requires version 1 and a nonempty id")
    budgets = rubric.get("budgets", {})
    if not isinstance(budgets, dict) or set(budgets) - {"model_requests", "tool_attempts", "steps_per_turn", "elapsed_seconds"}:
        raise ValueError("Invalid budget fields")
    for key, value in budgets.items():
        if type(value) not in {int, float} or not 0 < value < float("inf") or (key != "elapsed_seconds" and type(value) is not int):
            raise ValueError("Budgets must be positive, finite numbers; counts must be integers")
    expected = rubric.get("expect", {})
    if not isinstance(expected, dict) or set(expected) - {"status", "min_compactions", "turns"}:
        raise ValueError("Invalid expectations")
    if "status" in expected and not isinstance(expected["status"], str):
        raise ValueError("Expected status must be a string")
    if "min_compactions" in expected and (type(expected["min_compactions"]) is not int or expected["min_compactions"] < 0):
        raise ValueError("Invalid compaction expectation")
    turns = expected.get("turns", [])
    if not isinstance(turns, list):
        raise ValueError("Expected turns must be a list")
    seen = set()
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) - {"number", "tools", "reply_contains", "reply_excludes", "tool_errors", "reads", "result"}:
            raise ValueError("Unknown turn rule")
        if "result" in turn and turn["result"] not in {"completed", "partial", "no_evidence", "budget_stopped", "answered_unverified", "citation_unverified", "output_truncated", "response_incomplete", "generation_failed"}:
            raise ValueError("Invalid expected task result")
        if type(turn.get("number")) is not int or turn["number"] < 0 or turn["number"] in seen:
            raise ValueError("Expected turn numbers must be nonnegative and unique")
        seen.add(turn["number"])
        for key in ("tools", "reply_contains", "reply_excludes"):
            if key in turn and (not isinstance(turn[key], list) or not all(isinstance(item, str) and item for item in turn[key])):
                raise ValueError("Invalid string list rule")
        errors = turn.get("tool_errors", {})
        if not isinstance(errors, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in errors.items()):
            raise ValueError("Invalid expected tool errors")
        reads = turn.get("reads", [])
        if not isinstance(reads, list):
            raise ValueError("Invalid reads")
        for read in reads:
            if not isinstance(read, dict) or set(read) != {"post_id", "source", "version", "start_line", "end_line"}:
                raise ValueError("Read evidence requires source/version/id/line range")
            if not all(isinstance(read[k], str) and read[k] for k in ("post_id", "source", "version")):
                raise ValueError("Invalid read identity")
            if any(type(read[k]) is not int for k in ("start_line", "end_line")) or not 1 <= read["start_line"] <= read["end_line"]:
                raise ValueError("Invalid read range")
    return rubric


def validate_trace(trace):
    if not isinstance(trace, dict) or type(trace.get("schema_version")) is not int or trace.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Missing or unsupported trace schema")
    if set(trace) != {"schema_version", "session_id", "run_id", "events"}:
        raise ValueError("Unknown trace fields")
    if not all(isinstance(trace.get(k), str) and trace[k] for k in ("session_id", "run_id")):
        raise ValueError("Missing trace identity")
    events = trace.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("Trace events must be a nonempty list")
    ids = set()
    previous_time = None
    for sequence, event in enumerate(events, 1):
        if not isinstance(event, dict) or set(event) != {"sequence", "event_id", "run_id", "session_id", "event_type", "observed_at", "span_id", "parent_span_id", "turn_id", "tool_call_id", "metadata"} or type(event.get("sequence")) is not int or event.get("sequence") != sequence or event.get("event_type") not in EVENT_TYPES:
            raise ValueError("Invalid trace event sequence/type")
        if not isinstance(event.get("event_id"), str) or event["event_id"] in ids:
            raise ValueError("Invalid/duplicate event identity")
        ids.add(event["event_id"])
        if event.get("session_id") != trace["session_id"] or not isinstance(event.get("run_id"), str):
            raise ValueError("Broken trace identity")
        for key in ("span_id", "parent_span_id", "tool_call_id"):
            if event.get(key) is not None and not isinstance(event[key], str):
                raise ValueError("Invalid span reference")
        if type(event.get("turn_id")) is not int or event["turn_id"] < 0:
            raise ValueError("Invalid turn reference")
        metadata = event.get("metadata")
        if not isinstance(metadata, dict) or set(metadata) - FIELDS:
            raise ValueError("Invalid trace metadata")
        for key, value in metadata.items():
            if value is None:
                continue
            if key in {"attempt", "input_tokens", "output_tokens", "cached_tokens", "before_bytes", "after_bytes", "step", "max_steps", "max_turns", "located", "invalid"}:
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid trace counter")
            elif key == "elapsed_seconds":
                if type(value) not in {int, float} or not 0 <= value < float("inf"):
                    raise ValueError("Invalid trace duration")
            elif not isinstance(value, str):
                raise ValueError("Invalid trace label")
        timestamp = datetime.fromisoformat(event["observed_at"])
        if timestamp.tzinfo is None or (previous_time is not None and timestamp < previous_time):
            raise ValueError("Invalid event time order")
        previous_time = timestamp
    return trace


def grade_trace(state, rubric, *, allowed_tools, argument_validator=None):
    validate_rubric(rubric)
    checks = []

    def check(name, passed, category, evidence=(), failure="trajectory"):
        checks.append({"name": name, "passed": bool(passed), "category": category,
                       "evidence_ids": list(evidence), "failure_category": None if passed else failure})

    trace = state.get("trace")
    try:
        validate_trace(trace)
        check("trace_schema", True, "structure")
    except (ValueError, KeyError, TypeError):
        check("trace_schema", False, "structure", failure="missing_evidence")
        return finish(checks, state, rubric)
    events = trace["events"]
    check("session_identity", trace["session_id"] == state.get("conversation_id"), "structure")
    check("session_terminal", state.get("status") in {"waiting_user", "closed"}, "structure", failure="unfinished")
    starts, ends = {}, {}
    turn_spans = {e["span_id"] for e in events if e["event_type"] == "turn.start"}
    for event in events:
        kind, span = event["event_type"], event["span_id"]
        if kind in {"model.start", "tool.start"}:
            check(f"start_unique:{event['sequence']}", span is not None and span not in starts, "structure", [event["event_id"]])
            starts[span] = event
            check(f"parent:{event['sequence']}", event["parent_span_id"] in turn_spans, "structure", [event["event_id"]])
        elif kind in {"model.end", "tool.end"}:
            start = starts.get(span)
            check(f"end_pair:{event['sequence']}", start is not None and span not in ends
                  and start["event_type"].split(".")[0] == kind.split(".")[0]
                  and start["tool_call_id"] == event["tool_call_id"]
                  and start["turn_id"] == event["turn_id"], "structure", [event["event_id"]])
            ends[span] = event
    check("span_returns", set(starts) == set(ends), "structure",
          [e["event_id"] for span, e in starts.items() if span not in ends], "missing_evidence")
    calls = state.get("tool_calls", [])
    history = state.get("history", [])
    ids = [c["call_id"] for c in calls]
    for turn in state.get("turns", []):
        turn_ends = [e for e in events if e["event_type"] == "turn.end" and e["turn_id"] == turn["number"]]
        check(f"turn_evidence:{turn['number']}", turn.get("status") == "completed" and len(turn_ends) == 1,
              "structure", [f"turn:{turn['number']}"], "unfinished")
    check("unique_tool_ids", len(ids) == len(set(ids)), "structure", ids)
    outputs = [i for i in history if i.get("type") == "function_call_output"]
    final_calls = [c for c in calls if c.get("completed")]
    check("output_pairing", Counter(i.get("call_id") for i in outputs) == Counter(c["call_id"] for c in final_calls), "structure", ids, "missing_evidence")
    known_paths = set()
    directory = state.get("blog", {}).get("posts_dir")
    root = Path(directory) if isinstance(directory, str) else None
    directory_uri = root.as_uri() + "/" if root is not None and root.is_absolute() else None
    for call in calls:
        identifier = call["call_id"]
        result = call.get("result") or {}
        success = call.get("status") == "completed" and result.get("ok") is True
        check(f"terminal:{identifier}", call.get("status") in {"completed", "failed"} and call.get("completed") is True, "structure", [identifier], "unfinished")
        tool_ends = [e for e in events if e["event_type"] == "tool.end" and e["tool_call_id"] == identifier]
        check(f"tool_evidence:{identifier}", bool(tool_ends) and tool_ends[-1]["metadata"].get("status") == call.get("status"), "structure", [identifier], "missing_evidence")
        check(f"tool_result_hash:{identifier}", bool(tool_ends) and tool_ends[-1]["metadata"].get("result_hash") == fingerprint(result), "structure", [identifier], "missing_evidence")
        matching = [i for i in outputs if i.get("call_id") == identifier]
        paired = False
        if len(matching) == 1:
            try:
                paired = json.loads(matching[0]["output"]) == result
            except (KeyError, TypeError, ValueError):
                pass
        check(f"result_integrity:{identifier}", paired, "structure", [identifier], "missing_evidence")
        check(f"permission:{identifier}", not success or call["name"] in allowed_tools, "safety", [identifier], "permission_violation")
        if success:
            guards = [e for e in events if e["tool_call_id"] == identifier and e["event_type"] in {"guard.allow", "guard.deny"}]
            check(f"guard_evidence:{identifier}", bool(guards) and guards[-1]["event_type"] == "guard.allow", "safety", [identifier], "missing_evidence")
            if argument_validator is not None:
                valid = True
                try:
                    argument_validator(call["name"], json.loads(call["arguments"]))
                except (ValueError, KeyError, TypeError):
                    valid = False
                check(f"arguments:{identifier}", valid, "safety", [identifier], "invalid_arguments")
        if success and call["name"] == "find_text":
            try:
                paths = json.loads(call["arguments"])["paths"]
                ordered = isinstance(paths, list) and bool(paths) and set(paths).issubset(known_paths)
            except (ValueError, TypeError, KeyError):
                ordered = False
            check(f"discovery_order:{identifier}", ordered, "safety", [identifier], "retrieval_order")
        if success and call["name"] == "find_file":
            paths = result.get("data", [])
            bounded = isinstance(paths, list) and root is not None and all(
                isinstance(p, str) and Path(p).is_absolute() and Path(p).is_relative_to(root)
                and ".." not in Path(p).relative_to(root).parts and Path(p).suffix.casefold() == ".md" for p in paths)
            check(f"file_scope:{identifier}", bounded, "safety", [identifier], "permission_violation")
            if bounded:
                known_paths.update(paths)
        if success and call["name"] in {"list_posts", "read_post", "search_posts", "find_text"}:
            data = result.get("data", {})
            records = ([data] if call["name"] == "read_post" else data.get("items", [])
                       if call["name"] == "list_posts" else data.get("matches", []) if isinstance(data, dict) else data)
            bounded = directory_uri is not None and isinstance(records, list)
            for record in records if isinstance(records, list) else []:
                source = record.get("source") if isinstance(record, dict) else None
                tail = unquote(source[len(directory_uri):]) if isinstance(source, str) and directory_uri and source.startswith(directory_uri) else ""
                bounded = (bounded and bool(tail) and tail.casefold().endswith(".md")
                           and not any(c in tail for c in '\\:\0')
                           and all(part not in {"", ".", ".."} for part in tail.split('/')))
            check(f"source_scope:{identifier}", bounded, "safety", [identifier], "permission_violation")
    for request in state.get("model_requests", []):
        span = f"model:{request['number']}"
        check(f"model_evidence:{request['number']}", span in ends and ends[span]["metadata"].get("status") == request.get("status"), "structure", [span], "missing_evidence")
    check("model_ledger_pairing", {span for span in starts if isinstance(span, str) and span.startswith("model:")}
          == {f"model:{r['number']}" for r in state.get("model_requests", [])}, "structure", failure="missing_evidence")
    expected = rubric.get("expect", {})
    if "status" in expected:
        check("final_status", state.get("status") == expected["status"], "task", failure="unfinished")
    if "min_compactions" in expected:
        check("compactions", len(state.get("compactions", [])) >= expected["min_compactions"], "task", failure="context")
    expected_errors = {}
    for rule in expected.get("turns", []):
        number = rule["number"]
        turn = next((t for t in state.get("turns", []) if t["number"] == number), None)
        step_ids = {s["number"] for s in state.get("steps", []) if s.get("turn") == number}
        turn_calls = [c for c in calls if c["step"] in step_ids]
        check(f"turn_complete:{number}", turn is not None and turn.get("status") == "completed", "task", [f"turn:{number}"], "unfinished")
        if "result" in rule:
            check(f"task_result:{number}", (turn or {}).get("result") == rule["result"], "task", [f"turn:{number}"], "answer_assertion")
        if "tools" in rule:
            check(f"tools:{number}", [c["name"] for c in turn_calls] == rule["tools"], "task", [c["call_id"] for c in turn_calls], "tool_selection")
        reply = (turn or {}).get("reply", "").casefold()
        for key in ("reply_contains", "reply_excludes"):
            for index, text in enumerate(rule.get(key, [])):
                check(f"{key}:{number}:{index}", (text.casefold() in reply) == (key == "reply_contains"), "task", [f"turn:{number}"], "answer_assertion")
        for name, error in rule.get("tool_errors", {}).items():
            matching = [c for c in turn_calls if c["name"] == name and c.get("status") == "failed" and (c.get("result") or {}).get("error", {}).get("type") == error]
            check(f"expected_error:{number}:{name}", bool(matching), "task", [c["call_id"] for c in matching], "rejection")
            expected_errors.update({c["call_id"]: error for c in matching})
            if error in {"PermissionError", "ValueError", "JSONDecodeError"}:
                check(f"denial_evidence:{number}:{name}", bool(matching) and all(any(
                    e["event_type"] == "guard.deny" and e["tool_call_id"] == c["call_id"]
                    and e["metadata"].get("error_type") == error for e in events) for c in matching),
                    "safety", [c["call_id"] for c in matching], "missing_evidence")
        for index, read in enumerate(rule.get("reads", [])):
            supporting = []
            for call in turn_calls:
                data = (call.get("result") or {}).get("data", {})
                if call["name"] != "read_post" or call.get("status") != "completed" or not isinstance(data, dict):
                    continue
                if all(data.get(k) == read[k] for k in ("post_id", "source", "version")) and data.get("start_line", 0) <= read["start_line"] <= read["end_line"] <= data.get("end_line", 0):
                    supporting.append(call["call_id"])
            check(f"read_support:{number}:{index}", bool(supporting), "task", supporting, "source_evidence")
    for call in calls:
        if call.get("status") == "failed":
            check(f"tool_error:{call['call_id']}", call["call_id"] in expected_errors, "task", [call["call_id"]], "tool_failure")
    for request in state.get("model_requests", []):
        # Earlier failed attempts are diagnostic, not a task failure after recovery.
        if request.get("status") in {"error", "interrupted"} and state.get("status") not in {"waiting_user", "closed"}:
            check(f"model_error:{request['number']}", False, "task", [f"model:{request['number']}"], "service_failure")
    budgets = rubric.get("budgets", {})
    durations = [r.get("elapsed_seconds") for r in state.get("model_requests", [])] + [c.get("elapsed_seconds") for c in calls]
    duration_known = all(type(value) in {int, float} and 0 <= value < float("inf") for value in durations)
    values = {
        "model_requests": len(state.get("model_requests", [])),
        "tool_attempts": sum(c.get("attempts", 0) for c in calls),
        "steps_per_turn": max(Counter(s.get("turn") for s in state.get("steps", [])).values(), default=0),
        "elapsed_seconds": sum(durations) if duration_known else None,
    }
    for key, limit in budgets.items():
        check(f"budget:{key}", values[key] is not None and values[key] <= limit, "budget", failure="budget" if values[key] is not None else "missing_evidence")
    for turn in state.get("turns", []):
        budget = turn.get("retrieval")
        if budget is None:
            continue
        limits = budget.get("limits", {})
        number = turn["number"]
        for key in ("model_requests", "tool_attempts", "searches") + (
                ("file_searches", "text_searches") if budget.get("version") == 2 else ()):
            actual, limit = budget.get(key), limits.get(key)
            check(f"runtime_budget:{number}:{key}", type(actual) is int and type(limit) is int
                  and 0 <= actual <= limit, "budget", [f"turn:{number}"], "budget")
        check(f"runtime_context:{number}", type(budget.get("context_peak_bytes")) is int
              and type(limits.get("context_bytes")) is int
              and 0 <= budget["context_peak_bytes"] <= limits["context_bytes"], "budget", [f"turn:{number}"], "budget")
        if turn.get("result") == "completed":
            step_ids = {s["number"] for s in state["steps"] if s.get("turn") == number}
            pages = [c["result"]["data"] for c in calls if c["step"] in step_ids
                     and c["name"] == "list_posts" and c.get("status") == "completed"]
            if pages:
                pages = [p for p in pages if p["snapshot_version"] == pages[-1]["snapshot_version"]]
            complete = bool(pages) and len({p["snapshot_version"] for p in pages}) == 1 and any(
                p["offset"] == 0 for p in pages) and any(p["next_cursor"] is None for p in pages)
            complete = complete and not any(p["issues_count"] for p in pages) and len({
                i["post_id"] for p in pages for i in p["items"]}) == pages[-1]["total"]
            check(f"catalog_complete:{number}", complete, "task", [f"turn:{number}"], "source_evidence")
    return finish(checks, state, rubric)


def finish(checks, state, rubric):
    groups = {}
    for category, weight in WEIGHTS.items():
        selected = [c for c in checks if c["category"] == category]
        groups[category] = {"weight": weight, "checks": len(selected),
                            "score": sum(c["passed"] for c in selected) / len(selected) if selected else None}
    denominator = sum(g["weight"] for g in groups.values() if g["score"] is not None)
    score = sum(g["weight"] * g["score"] for g in groups.values() if g["score"] is not None) / denominator
    process_passed = all(c["passed"] for c in checks)
    expected = rubric.get("expect", {})
    task_configured = "status" in expected or "min_compactions" in expected or bool(expected.get("turns"))
    metrics = summarize_metrics(state)
    requests = state.get("model_requests", [])
    def unknown_totals(summary, records):
        for key in ("input_tokens", "output_tokens", "cached_tokens"):
            if not any(r.get(key) is not None for r in records):
                summary[key] = None
    unknown_totals(metrics, requests)
    for purpose, summary in metrics["by_purpose"].items():
        unknown_totals(summary, [r for r in requests if r["purpose"] == purpose])
    return {
        "report_version": 1, "grader_version": GRADER_VERSION,
        "rubric_id": rubric["id"], "rubric_hash": fingerprint(rubric),
        "checkpoint_hash": fingerprint(state), "session_id": state.get("conversation_id"),
        "passed": process_passed if task_configured else (False if not process_passed else None),
        "process_passed": process_passed, "task_verdict": "configured_rules" if task_configured else "unknown",
        "semantic_quality": "not_graded", "score": round(score, 6), "groups": groups,
        "checks": checks, "failure_counts": dict(Counter(c["failure_category"] for c in checks if not c["passed"])),
        "metrics": metrics, "usage_coverage": metrics["usage_observed_requests"] / metrics["requests"] if metrics["requests"] else None,
        "warnings": ["Scores validate recorded rules, not semantic correctness or model hidden reasoning.",
                     "Elapsed budget counts observed API/tool durations, not total wall-clock latency.",
                     "Costs, automatic citation parsing and retrieval recall are not graded in v1."],
    }
