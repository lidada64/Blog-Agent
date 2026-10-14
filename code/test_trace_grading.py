"""Fault injection for local trace graders and real loop instrumentation."""

import asyncio
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_core.trace_grading import grade_trace, validate_rubric
from blog_assistant.permissions import READ_TOOLS
from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.tools import BLOG_TOOLS, validate_arguments
from test_blog_agent import answer, call, model, response
from grade_trace import main


SCHEMAS = {tool["name"]: tool["parameters"] for tool in BLOG_TOOLS}


def grade(state, rubric):
    return grade_trace(state, rubric, allowed_tools=READ_TOOLS,
                       argument_validator=lambda name, args: validate_arguments(SCHEMAS[name], args))


class TraceGradingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.posts = self.root / "posts"
        self.posts.mkdir()
        (self.posts / "sample.md").write_text("# Sample\nPrivate fixture: atomic state.\n", encoding="utf-8")
        self.store = BlogStore(self.root / "qa.json", posts_dir=self.posts)
        self.state = self.store.start("test", compact_mode="none", new=True)
        # These trace fixtures intentionally use free-form legacy answers.
        self.state["prompt"]["version"] = "2026-10-13.3"
        self.store.save(self.state)
        self.rubric = {"version": 1, "id": "fixture-v1", "expect": {
            "status": "waiting_user", "turns": [{"number": 1, "tools": ["read_post"], "reply_contains": ["atomic"]}]},
            "budgets": {"model_requests": 4, "tool_attempts": 2}}

    async def normal(self):
        conversation = create_conversation(model(answer("hello"),
            response(call("read_post", {"post_id": "sample", "start_line": 1, "limit": 2}, "read")),
            answer("atomic state [sample L1-L2]")), self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain state")
        return self.state

    async def test_recorded_loop_passes_with_unknown_usage_and_metadata_only_export(self):
        state = await self.normal()
        report = grade(state, self.rubric)
        self.assertTrue(report["passed"])
        self.assertEqual(report["usage_coverage"], 0)
        self.assertIsNone(report["metrics"]["input_tokens"])
        self.assertIsNone(report["metrics"]["cache_hit_ratio"])
        self.assertEqual(report, grade(copy.deepcopy(state), self.rubric))
        trace = self.store.path.with_suffix(".trace.json").read_text(encoding="utf-8")
        self.assertNotIn("Private fixture", trace)
        self.assertNotIn("Explain state", trace)
        self.assertNotIn(str(self.posts), trace)
        self.assertNotIn("atomic", json.dumps(report))

    async def test_missing_output_and_tampered_result_are_detected(self):
        state = await self.normal()
        state["history"] = [i for i in state["history"] if i.get("type") != "function_call_output"]
        self.assertFalse(grade(state, self.rubric)["passed"])
        state = copy.deepcopy(self.store.load())
        state["tool_calls"][0]["result"]["data"]["version"] = "changed"
        report = grade(state, self.rubric)
        self.assertIn("missing_evidence", report["failure_counts"])

    async def test_forbidden_success_blocks_grade(self):
        state = await self.normal()
        state["tool_calls"][0]["name"] = "publish_post"
        report = grade(state, self.rubric)
        self.assertFalse(report["passed"])
        self.assertIn("permission_violation", report["failure_counts"])

    async def test_expected_permission_rejection_passes_but_unexpected_rejection_fails(self):
        conversation = create_conversation(model(answer("hello"),
            response(call("publish_post", {"role": "admin"}, "write")), answer("Denied")), self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Publish")
        self.rubric["expect"]["turns"] = [{"number": 1, "tools": ["publish_post"],
            "tool_errors": {"publish_post": "PermissionError"}, "reply_contains": ["越权工具调用被拒绝"]}]
        self.assertTrue(grade(self.state, self.rubric)["passed"])
        del self.rubric["expect"]["turns"][0]["tool_errors"]
        self.assertFalse(grade(self.state, self.rubric)["passed"])

    async def test_budget_and_wrong_source_version_fail(self):
        state = await self.normal()
        self.rubric["budgets"]["model_requests"] = 1
        self.assertIn("budget", grade(state, self.rubric)["failure_counts"])
        self.rubric["budgets"]["model_requests"] = 4
        data = state["tool_calls"][0]["result"]["data"]
        read = {k: data[k] for k in ("post_id", "source", "version", "start_line", "end_line")}
        self.rubric["expect"]["turns"][0]["reads"] = [read]
        self.assertTrue(grade(state, self.rubric)["passed"])
        read["version"] = "wrong-version"
        self.assertIn("source_evidence", grade(state, self.rubric)["failure_counts"])

    async def test_missing_event_schema_and_wrong_parent_fail(self):
        state = await self.normal()
        broken = copy.deepcopy(state)
        broken["trace"]["schema_version"] = 99
        self.assertFalse(grade(broken, self.rubric)["passed"])
        start = next(e for e in state["trace"]["events"] if e["event_type"] == "tool.start")
        start["parent_span_id"] = "turn:999"
        self.assertFalse(grade(state, self.rubric)["passed"])

    async def test_discovery_then_text_search_and_missing_return(self):
        path = str(self.posts / "sample.md")
        conversation = create_conversation(model(answer("hello"),
            response(call("find_file", {"name": "sample"}, "discover")),
            response(call("find_text", {"paths": [path], "text": "atomic"}, "text")),
            answer("atomic state")), self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Find sample then atomic")
        self.rubric["expect"]["turns"][0]["tools"] = ["find_file", "find_text"]
        self.assertTrue(grade(self.state, self.rubric)["passed"])
        broken = copy.deepcopy(self.state)
        broken["tool_calls"].reverse()
        self.assertIn("retrieval_order", grade(broken, self.rubric)["failure_counts"])
        self.state["trace"]["events"] = [e for e in self.state["trace"]["events"] if not (e["event_type"] == "tool.end" and e["tool_call_id"] == "text")]
        for n, event in enumerate(self.state["trace"]["events"], 1):
            event["sequence"] = n
        self.assertIn("missing_evidence", grade(self.state, self.rubric)["failure_counts"])

    async def test_successful_outside_source_and_invalid_arguments_fail(self):
        state = await self.normal()
        state["tool_calls"][0]["result"]["data"]["source"] = (self.root / "outside.md").as_uri()
        report = grade(state, self.rubric)
        self.assertIn("permission_violation", report["failure_counts"])
        state["tool_calls"][0]["arguments"] = '{"post_id":"sample","start_line":true,"limit":2}'
        self.assertIn("invalid_arguments", grade(state, self.rubric)["failure_counts"])

    async def test_sidecar_failure_keeps_checkpoint_events(self):
        await self.normal()
        with patch("blog_assistant.session.atomic_write_json", side_effect=OSError("fixture sidecar failure")):
            self.store.save(self.state)
        self.assertEqual(self.store.load()["trace"], self.state["trace"])

    async def test_no_task_expectation_needs_review_not_pass(self):
        state = await self.normal()
        self.rubric["expect"] = {}
        report = grade(state, self.rubric)
        self.assertIsNone(report["passed"])
        self.assertEqual(report["task_verdict"], "unknown")

    async def test_cancelled_tool_resume_records_two_attempts(self):
        client = model(answer("hello"),
            response(call("read_post", {"post_id": "sample", "start_line": 1, "limit": 2}, "read")),
            answer("atomic state"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        async def cancelled(name, arguments):
            raise asyncio.CancelledError()
        conversation.tool_executor = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit("Explain state")
        saved = self.store.start("test", resume=True)
        resumed = create_conversation(client, saved, self.store)
        await resumed.resume_turn()
        self.rubric["budgets"]["tool_attempts"] = 3
        report = grade(saved, self.rubric)
        self.assertTrue(report["passed"], [c for c in report["checks"] if not c["passed"]])
        self.assertEqual(saved["tool_calls"][0]["attempts"], 2)
        self.assertTrue(any(e["event_type"] == "session.resume" for e in saved["trace"]["events"]))

    async def test_model_timeout_is_retained_after_successful_resume(self):
        client = model(answer("hello"), TimeoutError("fixture timeout"),
            response(call("read_post", {"post_id": "sample", "start_line": 1, "limit": 2}, "read")),
            answer("atomic state"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        status, _ = await conversation.submit("Explain state")
        self.assertEqual(status, "model_error")
        saved = self.store.start("test", resume=True)
        await create_conversation(client, saved, self.store).resume_turn()
        report = grade(saved, self.rubric)
        self.assertTrue(report["passed"])
        self.assertEqual(report["metrics"]["errors"], 1)

    async def test_local_compaction_event_survives_state_replacement(self):
        from agent_core.context import compact_context
        client = model(answer("hello"), *(answer("atomic " * 300) for _ in range(4)))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        for number in range(4):
            await conversation.submit(f"Question {number}" + " x" * 300)
        self.state["context"] = copy.deepcopy(self.state["history"])
        self.state["compact_mode"] = "local"
        self.state["compact_threshold"] = 1200
        self.assertTrue(await compact_context(client, self.state, self.store, force=True))
        rubric = {"version": 1, "id": "compact-v1", "expect": {"status": "waiting_user", "min_compactions": 1}}
        self.assertTrue(grade(self.state, rubric)["passed"])
        self.assertEqual(len([e for e in self.state["trace"]["events"] if e["event_type"] == "compact.end"]), 1)

    async def test_cli_reports_and_preserves_source_and_existing_output(self):
        await self.normal()
        config = self.root / "rubric.json"
        config.write_text(json.dumps(self.rubric), encoding="utf-8")
        before = self.store.path.read_bytes()
        output = self.root / "report"
        with patch("grade_trace.provenance", return_value={"commit": "fixture"}):
            self.assertEqual(main(["--state-file", str(self.store.path), "--rubric", str(config), "--output-dir", str(output)]), 0)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertTrue((output / "trace.json").exists())
        self.assertNotIn("Private fixture", (output / "report.json").read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            main(["--state-file", str(self.store.path), "--output-dir", str(output)])

    def test_unknown_rules_and_invalid_budget_are_rejected(self):
        for rubric in ({"version": 1, "id": "x", "exepct": {}},
                       {"version": 1, "id": "x", "budgets": {"model_requests": True}},
                       {"version": 1, "id": "x", "expect": {"turns": [{"number": 1, "tool_error": {}}]}}):
            with self.assertRaises(ValueError):
                validate_rubric(rubric)
