"""Deterministic maintenance regressions; temporary articles and fake providers only."""
import asyncio
import copy
import hashlib
import json
import unittest
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import test_retrieval_convergence as fixtures
from test_blog_agent import answer, call, model, response
from blog_assistant.session import BlogStore, INSTRUCTIONS, create_conversation
from blog_assistant.convergence import RetrievalLimits
from agent_core.trace_grading import grade_trace
from blog_assistant.permissions import READ_TOOLS


def truncated(reason="max_output_tokens", *calls):
    return SimpleNamespace(status="incomplete", incomplete_details={"reason": reason},
                           output_text="partial", output=list(calls),
                           usage={"input_tokens": 100, "output_tokens": 2048,
                                  "output_tokens_details": {"reasoning_tokens": 1000}})


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ConvergenceTests.setUp
    begin = fixtures.ConvergenceTests.begin
    async def test_failed_finalization_then_new_question_owns_new_step_and_trace(self):
        client = model(answer("hello"), RuntimeError("final failure"))
        first = create_conversation(client, self.state, self.store, max_steps=1)
        await first.introduce()
        await first.submit("First question")
        old_step = copy.deepcopy(self.state["steps"][-1])
        client = model(response(call("read_post", {"post_id": "sample", "start_line": 1, "limit": 2}, "new-read")),
                       answer("State persists results [sample L2]."))
        second = create_conversation(client, self.state, self.store)
        await second.submit("Read sample")
        step = next(s for s in self.state["steps"] if s["number"] == self.state["tool_calls"][-1]["step"])
        self.assertEqual(step["turn"], 2)
        self.assertEqual(self.state["steps"][-3], old_step)
        events = [e for e in self.state["trace"]["events"] if e.get("tool_call_id") == "new-read"]
        self.assertTrue(events)
        self.assertTrue(all(e["turn_id"] == 2 for e in events))
        final_input = json.dumps(client.responses.create.call_args.kwargs["input"])
        self.assertIn("State persists", final_input)

    async def test_file_and_body_queries_have_independent_allowances(self):
        client = model(answer("hello"), response(call("find_file", {"name": "sample"}, "file")),
                       response(call("search_posts", {"query": "State", "limit": 3}, "search")),
                       response(call("find_text", {"paths": [str(self.posts / "sample.md")], "text": "persists"}, "text")),
                       answer("State persists."))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Find sample and locate persists")
        self.assertEqual(self.state["tool_calls"][-1]["status"], "completed")
        budget = conversation.turn_policy.initialize()
        self.assertEqual([budget[k] for k in ("file_searches", "searches", "text_searches")], [1, 1, 1])

    async def test_exhausted_lookup_does_not_disable_reading_or_body_search(self):
        policy = await self.begin(no_progress=5)
        for query in ("sample", "absent"):
            await policy.execute("find_file", {"name": query})
        with self.assertRaisesRegex(ValueError, "file_searches"):
            await policy.execute("find_file", {"name": "third"})
        self.assertFalse(policy.initialize()["finalizing"])
        result = await policy.execute("read_post", {"post_id": "sample", "start_line": 1, "limit": 2})
        self.assertIn("State persists", result["data"]["text"])
        await policy.execute("search_posts", {"query": "State", "limit": 3})

    async def test_v1_budget_keeps_shared_consumption_on_resume(self):
        policy = await self.begin(no_progress=5)
        budget = policy.initialize()
        budget["version"] = 1
        for key in ("file_searches", "text_searches", "output_retries"):
            budget.pop(key, None)
        for key in ("file_searches", "text_searches"):
            budget["limits"].pop(key, None)
        budget["searches"] = 2
        self.store.save(self.state)
        saved = self.store.load()
        resumed = create_conversation(model(), saved, self.store)
        with self.assertRaisesRegex(ValueError, "Search/rewrite"):
            await resumed.turn_policy.execute("find_file", {"name": "sample"})
        self.assertEqual(saved["turns"][-1]["retrieval"]["searches"], 2)

    async def test_prompt_version_and_explicit_upgrade_preserve_history(self):
        await create_conversation(model(answer("hello")), self.state, self.store).introduce()
        self.state["instructions"] = "Legacy instructions"
        self.state.pop("prompt", None)
        self.store.save(self.state)
        history = copy.deepcopy(self.state["history"])
        saved = self.store.start("fixture")
        self.assertEqual(saved["instructions"], "Legacy instructions")
        self.assertFalse(self.store.prompt_status(saved)["current"])
        self.store.upgrade_prompt(saved)
        self.assertEqual(saved["instructions"], INSTRUCTIONS)
        self.assertEqual(saved["history"], history)
        self.assertEqual(saved["prompt"]["hash"], hashlib.sha256(INSTRUCTIONS.encode()).hexdigest())
        self.assertEqual(len(saved["prompt_upgrades"]), 1)
        self.assertEqual(self.store.load()["prompt"], saved["prompt"])

    async def test_prompt_upgrade_refuses_unfinished_turn_without_writing(self):
        conversation = create_conversation(model(answer("hello"), RuntimeError("offline")), self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Question")
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.store.upgrade_prompt(self.state)
        self.assertEqual(self.store.path.read_bytes(), before)

    async def test_truncation_retries_once_without_tools_and_keeps_usage(self):
        client = model(answer("hello"),
                       response(call("read_post", {"post_id": "sample", "start_line": 1, "limit": 2}, "read")),
                       truncated("max_output_tokens", call("read_post", {}, "partial-call")),
                       answer("State persists [sample L2]."))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        status, reply = await conversation.submit("Explain sample")
        self.assertEqual(status, "waiting_user")
        self.assertIn("State persists", reply)
        self.assertEqual(len(self.state["tool_calls"]), 1)
        request = client.responses.create.call_args.kwargs
        self.assertEqual(request["tools"], [])
        self.assertIn("State persists", json.dumps(request["input"]))
        record = self.state["model_requests"][-2]
        self.assertEqual(record["incomplete_reason"], "max_output_tokens")
        self.assertEqual(record["output_tokens"], 2048)
        self.assertEqual(record["reasoning_tokens"], 1000)
        self.assertEqual(record["max_output_tokens"], 2048)

    async def test_full_read_truncation_retries_with_evidence_under_saved_budget(self):
        client = model(answer("hello"),
                       response(call("read_post_full", {"post_id": "sample"}, "full")),
                       truncated(), answer("State persists [sample L2]."))
        conversation = create_conversation(client, self.state, self.store, max_steps=4)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain sample")
        self.assertIn("State persists", reply)
        self.assertEqual(client.responses.create.call_count, 4)
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])
        self.assertIn("State persists results", json.dumps(client.responses.create.call_args.kwargs["input"]))
        self.assertEqual(self.state["turns"][-1]["retrieval"]["output_retries"], 1)
        self.assertEqual(self.state["turns"][-1]["retrieval"]["model_requests"], 3)

    async def test_d9_seven_read_shape_reserves_answer_and_retry(self):
        body = "# Caching\n" + "".join(f"Line {i:03}: context.\n" for i in range(1, 330))
        body += "Line 331: FINAL FACT is reused input.\n"
        (self.posts / "caching.md").write_text(body, encoding="utf-8")
        requested_tools = []
        final_inputs = []
        final_attempts = 0
        client = model()

        async def replay(**request):
            nonlocal final_attempts
            if client.responses.create.call_count == 1:
                return answer("hello")
            requested_tools.append(bool(request["tools"]))
            if request["tools"]:
                index = sum(requested_tools) - 1
                if index == 0:
                    return response(call("search_posts", {"query": "Caching", "limit": 3}, "search"))
                return response(call("read_post", {"post_id": "caching",
                                                   "start_line": [1, 81, 161, 241, 321, 1, 1][index - 1],
                                                   "limit": 80}, f"read-{index - 1}"))
            final_attempts += 1
            final_inputs.append(json.dumps(request["input"], ensure_ascii=False))
            return truncated() if final_attempts == 1 else answer("The FINAL FACT is reused input [caching L331].")

        client.responses.create.side_effect = replay
        conversation = create_conversation(client, self.state, self.store, max_steps=8)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain caching")
        self.assertIn("FINAL FACT", reply)
        self.assertEqual(requested_tools, [True] * 6 + [False, False])
        self.assertEqual(len(self.state["tool_calls"]), 6)
        self.assertIn("FINAL FACT", final_inputs[-1])
        budget = self.state["turns"][-1]["retrieval"]
        self.assertEqual((budget["model_requests"], budget["output_retries"]), (8, 1))
        self.assertEqual(budget["finalization_reserve"], 2)
        self.assertEqual(self.state["turns"][-1]["result"], "partial")

    async def test_legacy_v2_checkpoint_keeps_one_request_reserve(self):
        policy = await self.begin(model_requests=8)
        budget = policy.initialize()
        self.assertEqual(budget["finalization_reserve"], 2)
        del budget["finalization_reserve"]
        budget["model_requests"] = 6
        self.store.save(self.state)
        resumed = create_conversation(model(), self.store.load(), self.store,
                                      retrieval_limits=RetrievalLimits(model_requests=99))
        self.assertIsNone(resumed.turn_policy.stop_reason())
        resumed.turn_policy.initialize()["model_requests"] = 7
        self.assertEqual(resumed.turn_policy.stop_reason(), "model_budget")

    async def test_repeated_truncation_is_bounded_and_explained(self):
        client = model(answer("hello"), truncated(), truncated())
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Question")
        self.assertEqual(client.responses.create.call_count, 3)
        self.assertIn("output_truncated", reply)
        self.assertEqual(self.state["turns"][-1]["result"], "output_truncated")
        self.assertEqual(self.store.load()["turns"][-1]["retrieval"]["output_retries"], 1)
        grade_trace(self.state, {"version": 1, "id": "truncated", "expect": {
            "turns": [{"number": 1, "result": "output_truncated"}]}}, allowed_tools=READ_TOOLS)

    async def test_truncation_with_no_request_budget_does_not_retry(self):
        client = model(answer("hello"), truncated())
        conversation = create_conversation(client, self.state, self.store, max_steps=1)
        await conversation.introduce()
        _, reply = await conversation.submit("Question")
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertIn("output_truncated", reply)

    async def test_other_incomplete_reason_does_not_trigger_token_retry(self):
        client = model(answer("hello"), truncated("content_filter"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Question")
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertIn("response_incomplete", reply)
        self.assertEqual(self.state["model_requests"][-1]["incomplete_reason"], "content_filter")

    async def test_saved_split_budget_cannot_be_enlarged_on_resume(self):
        policy = await self.begin(no_progress=5)
        await policy.execute("find_file", {"name": "sample"})
        before = copy.deepcopy(policy.initialize())
        resumed = create_conversation(model(), self.store.load(), self.store,
            retrieval_limits=RetrievalLimits(file_searches=99, text_searches=99, searches=99))
        self.assertEqual(resumed.turn_policy.initialize(), before)
        await resumed.turn_policy.execute("find_file", {"name": "absent"})
        with self.assertRaises(ValueError):
            await resumed.turn_policy.execute("find_file", {"name": "third"})

    async def test_cancelled_truncation_retry_cannot_reset_retry_count_on_resume(self):
        client = model(answer("hello"), truncated(), asyncio.CancelledError())
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit("Question")
        saved = self.store.load()
        before = saved["turns"][-1]["retrieval"]["model_requests"]
        client = model(truncated())
        resumed = create_conversation(client, saved, self.store)
        _, reply = await resumed.introduce()
        self.assertIn("output_truncated", reply)
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(saved["turns"][-1]["retrieval"]["model_requests"], before + 1)
        self.assertEqual(saved["turns"][-1]["retrieval"]["output_retries"], 1)
        self.assertEqual(saved["turns"][-1]["retrieval"]["finalization_reserve"], 2)

    async def test_prompt_upgrade_cli_is_offline_and_idempotent(self):
        await create_conversation(model(answer("hello")), self.state, self.store).introduce()
        self.state["instructions"] = "Legacy instructions"
        self.state.pop("prompt", None)
        self.store.save(self.state)
        command = [sys.executable, str(Path(__file__).parent / "blog_agent.py"),
                   "--upgrade-prompt", "--state-file", str(self.store.path), "--posts-dir", str(self.posts)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.store.load()
        self.assertEqual(saved["instructions"], INSTRUCTIONS)
        self.assertEqual(saved["model_requests"], self.state["model_requests"])
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.store.load()["prompt_upgrades"]), 1)

    async def test_corrupt_prompt_metadata_and_retry_counters_are_rejected(self):
        await self.begin()
        original = copy.deepcopy(self.state)
        for field, value in (("output_retries", True), ("output_retries", 2),
                             ("file_searches", -1), ("finalization_reserve", 3)):
            with self.subTest(field=field, value=value):
                self.state = copy.deepcopy(original)
                self.state["turns"][-1]["retrieval"][field] = value
                self.store.save(self.state)
                with self.assertRaises(ValueError):
                    self.store.load()
        self.state = copy.deepcopy(original)
        self.state["prompt"]["hash"] = "tampered"
        self.store.save(self.state)
        with self.assertRaisesRegex(ValueError, "prompt"):
            self.store.load()

    async def test_fixed_evidence_cases_keep_code_conflicts_and_sources_paired(self):
        # These fixtures test evidence delivery, not a fake model's semantic accuracy.
        for scenario in ("single", "code", "conflict", "cross-language"):
            with self.subTest(scenario=scenario):
                texts = {
                    "single": ["# State\nAtomic saves preserve results."],
                    "code": ["# Code\n```python\nvalue = 42\n```\nCode must stay intact."],
                    "conflict": ["# A\nThe limit is 2.", "# B\nThe limit is 3."],
                    "cross-language": ["# 状态\n状态保留工具结果。", "# State\nState preserves tool results."],
                }[scenario]
                for i, text in enumerate(texts):
                    (self.posts / f"fixed-{i}.md").write_text(text, encoding="utf-8")
                store = BlogStore(self.root / f"{scenario}.json", posts_dir=self.posts)
                state = store.start("fixture", new=True)
                # Preserve the pre-section-citation fixture contract. Current
                # source-repair retry/resume is tested in test_answer_strategy.
                state["prompt"]["version"] = "2026-10-13.10"
                store.save(state)
                client = model(answer("hello"), *[
                    response(call("read_post", {"post_id": f"fixed-{i}", "start_line": 1, "limit": 20}, f"fixed-{i}"))
                    for i in range(len(texts))], answer("Fixture answer; unverified."))
                conversation = create_conversation(client, state, store,
                    retrieval_limits=RetrievalLimits(context_bytes=3500))
                await conversation.introduce()
                await conversation.submit("Compare the evidence")
                request = client.responses.create.call_args.kwargs
                outputs = [json.loads(item["output"])["data"] for item in request["input"]
                           if item.get("type") == "function_call_output"]
                self.assertEqual(len(outputs), len(texts))
                for i, data in enumerate(outputs):
                    self.assertEqual(data["text"].strip(), texts[i])
                    self.assertEqual(data["version"], hashlib.sha256((self.posts / f"fixed-{i}.md").read_bytes()).hexdigest())
                    self.assertTrue(data["source"].endswith(f"fixed-{i}.md"))
                calls = {item["call_id"] for item in request["input"] if item.get("type") == "function_call"}
                returns = {item["call_id"] for item in request["input"] if item.get("type") == "function_call_output"}
                self.assertEqual(calls, returns)
                self.assertEqual(state["turns"][-1]["result"], "citation_unverified")


if __name__ == "__main__":
    unittest.main()
