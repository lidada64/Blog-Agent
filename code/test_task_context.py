"""Task continuity regressions at the real durable Blog conversation seam."""
import asyncio
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.convergence import RetrievalLimits
from test_blog_agent import model, answer, response, call


class TaskContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.posts = self.root / "posts"
        self.posts.mkdir()
        for identifier in ("first", "second"):
            (self.posts / (identifier + ".md")).write_text(
                f"# {identifier.title()}\n## Rule\n{identifier.upper()}_EVIDENCE\n", encoding="utf-8")
        self.store = BlogStore(self.root / "state.json", posts_dir=self.posts)
        self.state = self.store.start("fixture", new=True, compact_threshold=500)

    def request_text(self, client):
        return json.dumps(client.responses.create.call_args.kwargs["input"], ensure_ascii=False)

    async def test_followup_reuses_verified_source_without_new_tool(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("Explanation [[first|Rule]]"), answer("More detail [[first|Rule]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        _, reply = await conversation.submit("Explain the distinction further")
        self.assertIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertIn("More detail", reply)
        self.assertEqual(self.state["turns"][-1]["result"], "answered_unverified")
        self.assertEqual(len(self.state["tool_calls"]), 1)

    async def test_evidence_only_appears_once_across_memo_and_tool_returns(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("Explanation [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        self.assertEqual(self.request_text(client).count("FIRST_EVIDENCE"), 1)

    async def test_input_omission_cannot_certify_invisible_source(self):
        for identifier, char in (("first", "a"), ("second", "b")):
            (self.posts / (identifier + ".md")).write_text(
                f"# {identifier.title()}\n{identifier.upper()}_EVIDENCE\n" + char * 3300, encoding="utf-8")
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1"),
                       call("read_post_full", {"post_id": "second"}, "r2")),
                       answer("Fabricated comparison [[first]] [[second]]"))
        conversation = create_conversation(client, self.state, self.store,
                                           retrieval_limits=RetrievalLimits(context_bytes=5000))
        await conversation.introduce()
        _, reply = await conversation.submit("Compare first and second")
        self.assertNotEqual(self.state["turns"][-1]["result"], "answered_unverified")
        self.assertNotIn("Fabricated comparison", reply)

    async def test_ten_turn_switch_return_restart_and_compact(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), *[answer("Details [[first]]") for _ in range(3)],
                       response(call("read_post_full", {"post_id": "second"}, "r2")),
                       answer("Second [[second]]"), answer("Back [[first]]"),
                       *[answer("Details [[first]]") for _ in range(4)])
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        for _ in range(3):
            await conversation.submit("继续解释")
        await conversation.submit("换个话题，解释 second")
        self.assertNotIn("FIRST_EVIDENCE", self.request_text(client))
        restored = self.store.load()
        conversation = create_conversation(client, restored, self.store)
        await conversation.submit("回到 first，继续解释")
        for _ in range(4):
            await conversation.submit("继续解释")
            self.assertIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertEqual(restored["turn_count"], 10)
        self.assertTrue(restored["compactions"])
        self.assertEqual(len(restored["tool_calls"]), 2)
        self.assertEqual(restored["turns"][1]["task_id"], restored["turns"][-1]["task_id"])

    async def test_updated_and_deleted_sources_are_not_reused(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), answer("Old claim [[first]]"), answer("Old claim [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        path = self.posts / "first.md"
        path.write_text("# First\nNEW_CONTENT\n", encoding="utf-8")
        _, reply = await conversation.submit("继续解释")
        self.assertNotIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertNotIn("Old claim", reply)
        path.unlink()
        await conversation.submit("继续解释")
        self.assertNotIn("FIRST_EVIDENCE", self.request_text(client))

    async def test_comparison_reuses_old_source_but_requires_unread_named_source(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), answer("Invented comparison [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        _, reply = await conversation.submit("Compare first and second")
        self.assertEqual(self.state["turns"][-1]["projection"]["missing"], ["second"])
        self.assertEqual(self.state["turns"][-1]["result"], "partial")
        self.assertNotIn("Invented comparison", reply)

    async def test_compare_two_previously_read_tasks_uses_both_without_new_reads(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), response(call("read_post_full", {"post_id": "second"}, "r2")),
                       answer("Second [[second]]"), answer("Comparison [[first]] [[second]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        await conversation.submit("Explain second")
        _, reply = await conversation.submit("Compare first and second")
        self.assertIn("Comparison", reply)
        for name in ("FIRST_EVIDENCE", "SECOND_EVIDENCE"):
            self.assertEqual(self.request_text(client).count(name), 1)
        self.assertEqual(len(self.state["tool_calls"]), 2)

    async def test_revoked_read_permission_does_not_replay_stored_body(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), answer("Old claim [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        with patch.object(conversation.turn_policy.tools, "require", side_effect=PermissionError("revoked")):
            _, reply = await conversation.submit("继续解释")
        self.assertNotIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertNotIn("Old claim", reply)

    async def test_interrupted_request_rebuilds_projection_without_resetting_allowance(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       asyncio.CancelledError())
        conversation = create_conversation(client, self.state, self.store,
                                           retrieval_limits=RetrievalLimits(model_requests=4))
        await conversation.introduce()
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit("Explain first")
        saved = self.store.load()
        used = saved["turns"][-1]["retrieval"]["model_requests"]
        client = model(answer("Recovered [[first]]"))
        resumed = create_conversation(client, saved, self.store,
                                     retrieval_limits=RetrievalLimits(model_requests=20))
        _, reply = await resumed.introduce()
        self.assertIn("Recovered", reply)
        self.assertIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertEqual(saved["turns"][-1]["retrieval"]["limits"]["model_requests"], 4)
        self.assertEqual(saved["turns"][-1]["retrieval"]["model_requests"], used + 1)
        self.assertEqual(saved["task_memory"]["tasks"][0]["turns"], [1])

    async def test_point_eleven_stays_legacy_until_explicit_upgrade(self):
        self.state["instructions"] = "Saved .11 section citation instructions"
        self.state["prompt"] = {"version": "2026-10-13.11",
                                "hash": hashlib.sha256(self.state["instructions"].encode()).hexdigest()}
        self.store.save(self.state)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        self.assertNotIn("task_memory", self.state)
        self.assertEqual(self.store.load()["prompt"]["version"], "2026-10-13.11")
        self.store.upgrade_prompt(self.state)
        client = model(answer("Upgraded followup [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        _, reply = await conversation.submit("继续解释")
        self.assertIn("Upgraded followup", reply)
        self.assertIn("FIRST_EVIDENCE", self.request_text(client))

    async def test_overlap_is_deduplicated_and_unread_range_cannot_be_cited(self):
        (self.posts / "first.md").write_text("# First\n## Rule\nFIRST_EVIDENCE\nOTHER_EVIDENCE\n", encoding="utf-8")
        client = model(answer("hello"), response(
            call("read_post", {"post_id": "first", "start_line": 1, "limit": 3}, "r1"),
            call("read_post", {"post_id": "first", "start_line": 2, "limit": 2}, "r2")),
            answer("Explanation [[first|Rule]]"), answer("Invalid [[first|L4]]"),
            answer("Invalid [[first|L4]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        self.assertEqual(self.request_text(client).count("FIRST_EVIDENCE"), 1)
        self.assertNotIn("OTHER_EVIDENCE", self.request_text(client))
        _, reply = await conversation.submit("继续解释")
        self.assertNotIn("Invalid", reply)
        self.assertEqual(self.state["turns"][-1]["result"], "citation_unverified")

    async def test_named_different_topic_with_followup_word_does_not_inherit_old_source(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), answer("Invented [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        _, reply = await conversation.submit("Explain second further")
        self.assertNotIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertNotIn("Invented", reply)

    async def test_history_lookup_preserves_exact_user_constraints_without_reasoning_or_stale_body(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), response(call("read_conversation_history", {"start": 1, "limit": 5}, "h1")),
                       answer("Details [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first with constraint UNIQUE_USER_CONSTRAINT")
        await conversation.submit("继续解释")
        request = client.responses.create.call_args.kwargs["input"]
        history_output = next(i for i in request if i.get("call_id") == "h1" and i.get("type") == "function_call_output")
        self.assertIn("UNIQUE_USER_CONSTRAINT", history_output["output"])
        self.assertNotIn("FIRST_EVIDENCE", history_output["output"])

    async def test_corrupt_memory_references_are_rejected(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")), answer("First [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        self.state["task_memory"]["tasks"][0]["source_calls"] = ["not-a-real-call"]
        self.store.save(self.state)
        with self.assertRaisesRegex(ValueError, "task memory"):
            self.store.load()

    async def test_fresh_read_after_edit_supersedes_old_source_and_recovers(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), response(call("read_post_full", {"post_id": "first"}, "r2")),
                       answer("Updated [[first]]"), answer("Continued [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        (self.posts / "first.md").write_text("# First\nNEW_EVIDENCE\n", encoding="utf-8")
        _, reply = await conversation.submit("继续解释")
        self.assertIn("Updated", reply)
        _, reply = await conversation.submit("继续解释")
        self.assertIn("Continued", reply)
        self.assertNotIn("FIRST_EVIDENCE", self.request_text(client))
        self.assertEqual(self.request_text(client).count("NEW_EVIDENCE"), 1)
        self.assertFalse(self.state["turns"][-1]["projection"]["missing"])

    async def test_large_user_constraints_are_not_silently_dropped_on_followup(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), answer("Unsupported completion [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first with constraints " + "x" * 4000)
        # New rounds may choose a smaller input cap; the persisted prior task
        # constraints must not disappear while the answer is certified complete.
        conversation.turn_policy.limits = RetrievalLimits(context_bytes=1000)
        _, reply = await conversation.submit("继续解释")
        self.assertTrue(self.state["turns"][-1]["projection"]["missing_constraints"])
        self.assertEqual(self.state["turns"][-1]["result"], "partial")
        self.assertNotIn("Unsupported completion", reply)

    async def test_search_preview_and_read_body_do_not_duplicate_source_text(self):
        client = model(answer("hello"), response(call("search_posts", {"query": "First", "limit": 3}, "s1")),
                       response(call("read_post_full", {"post_id": "first"}, "r1")), answer("First [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        self.assertEqual(self.request_text(client).count("FIRST_EVIDENCE"), 1)

    async def test_summary_mode_uses_local_task_projection_without_extra_model_requests(self):
        self.state["compact_mode"] = "summary"
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("First [[first]]"), answer("Details [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        await conversation.submit("继续解释")
        self.assertEqual(client.responses.create.call_count, 4)
        self.assertTrue(self.state["compactions"])
        self.assertEqual(self.state["compactions"][-1]["requested_mode"], "summary")
        self.assertEqual(self.state["compactions"][-1]["mode"], "task_local")
        self.assertGreater(self.state["compactions"][-1]["before_bytes"], self.state["compactions"][-1]["after_bytes"])

    async def test_reasoning_protocol_stays_with_complete_parallel_tool_group(self):
        protocol = {"type": "reasoning", "id": "opaque-fixture-id", "encrypted_content": "opaque-fixture",
                    "summary": [], "content": [{"type": "reasoning_text", "text": "SYNTHETIC_PROTOCOL"}]}
        client = model(answer("hello"), response(protocol,
                       call("search_posts", {"query": "First", "limit": 3}, "s1"),
                       call("search_posts", {"query": "Second", "limit": 3}, "s2")),
                       response(call("read_post_full", {"post_id": "first"}, "r1")), answer("First [[first]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("Explain first")
        items = client.responses.create.call_args_list[2].kwargs["input"]
        self.assertIn(protocol, items)
        reasoning_index = items.index(protocol)
        self.assertEqual([i.get("call_id") for i in items[reasoning_index + 1:reasoning_index + 3]], ["s1", "s2"])
        self.assertEqual({i["call_id"] for i in items if i.get("type") == "function_call"},
                         {i["call_id"] for i in items if i.get("type") == "function_call_output"})
        await conversation.submit("你可以做什么")
        self.assertNotIn("SYNTHETIC_PROTOCOL", json.dumps(self.state.get("task_memory", {})))

    async def test_trailing_blank_line_full_read_is_not_falsely_stale_after_projection(self):
        (self.posts / "first.md").write_text("# First\n## Cache hit\nCached prefix is reused.\n\n", encoding="utf-8")
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "first"}, "r1")),
                       answer("Explanation [[first|Cache hit]]"), answer("Explanation [[first|Cache hit]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain cache hit")
        self.assertEqual(self.state["turns"][-1]["citation_audit"]["status"], "located")
        self.assertIn("Explanation", reply)
        self.assertEqual(self.state["turns"][-1]["retrieval"]["citation_retries"], 0)

    async def test_range_ending_in_blank_line_remains_valid_evidence(self):
        (self.posts / "first.md").write_text("# First\n## Rule\nEvidence.\n\nLater.\n", encoding="utf-8")
        client = model(answer("hello"), response(call("read_post", {"post_id": "first", "start_line": 2, "limit": 3}, "r1")),
                       answer("Explanation [[first|Rule]]"), answer("Explanation [[first|Rule]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain first")
        self.assertEqual(self.state["turns"][-1]["citation_audit"]["status"], "located")
        self.assertIn("Explanation", reply)


if __name__ == "__main__":
    unittest.main()
