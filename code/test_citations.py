"""D9 citations must locate current, actually-read lines before display."""

import hashlib
import json
import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from blog_assistant.citations import audit_answer
from blog_assistant.retrieval import SamplePosts
from blog_assistant.session import BlogStore as CurrentBlogStore, INSTRUCTIONS, PROMPT_VERSION, create_conversation
from test_blog_agent import answer, call, model, response
from review_blog_citations import score_review


class BlogStore(CurrentBlogStore):
    """Freeze legacy display regressions; current product tests live separately."""
    def start(self, *args, **kwargs):
        state = super().start(*args, **kwargs)
        state["prompt"]["version"] = "2026-10-13.10"
        self.save(state)
        return state


class CitationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.posts_dir = self.root / "posts"
        self.posts_dir.mkdir()
        self.path = self.posts_dir / "sample.md"
        self.path.write_text("# Sample\nFirst fact.\nSecond fact.\n", encoding="utf-8")
        self.posts = SamplePosts(self.posts_dir)

    def calls(self, *, first=1, limit=3):
        return [{"name": "read_post", "status": "completed",
                 "result": {"data": self.posts.read("sample", first, limit)}}]

    async def test_current_prompt_keeps_factual_boundaries_without_fixed_bullets(self):
        self.assertEqual(PROMPT_VERSION, "2026-10-13.13")
        self.assertIn("不得自创唯一触发条件", INSTRUCTIONS)
        self.assertIn("原文事实与合理推断明确区分", INSTRUCTIONS)
        self.assertIn("不强制逐句引用", INSTRUCTIONS)
        self.assertNotIn("回答最多写 8 条", INSTRUCTIONS)

    async def test_valid_location_uses_authoritative_version_and_exact_lines(self):
        result = audit_answer("First fact. [[sample|L2-L2]]", self.calls(), self.posts)
        self.assertEqual(result["status"], "located")
        self.assertIn("sample L2-L2 v" + self.posts.snapshot("sample")["version"][:12], result["rendered"])
        self.assertEqual(result["citations"][0]["evidence_sha256"], hashlib.sha256(b"First fact.").hexdigest())
        self.assertIsNone(result["semantic_support"])
        shorthand = audit_answer("First fact. [[sample|L2]]", self.calls(), self.posts)
        self.assertEqual(shorthand["status"], "located")
        self.assertEqual(shorthand["citations"][0]["end_line"], 2)

    async def test_wrong_range_wide_range_unread_candidate_and_bad_marker_are_not_shown_as_citations(self):
        limited = self.calls(first=2, limit=1)
        for marker in ("[[sample|L3-L3]]", "[[sample|L1-L20]]", "[[other|L2-L2]]",
                       "[[sample|Lx]]"):
            with self.subTest(marker=marker):
                result = audit_answer("Claim " + marker, limited, self.posts)
                self.assertEqual(result["status"], "invalid")
                self.assertNotIn(marker, result["rendered"])
                self.assertIn("引用未核验", result["rendered"])

    async def test_one_valid_marker_does_not_certify_other_uncited_claims(self):
        result = audit_answer("First fact. [[sample|L2-L2]]\nUnsupported second claim.",
                              self.calls(), self.posts)
        self.assertEqual(result["status"], "uncited_lines")
        self.assertEqual(result["uncited_lines"], [2])

    async def test_legacy_renderer_limits_cited_output_to_eight_bullets(self):
        answer_text = "\n".join(["- First fact. [[sample|L2]]"] * 9 +
                                ["Extra prose. [[sample|L2]]"])
        result = audit_answer(answer_text, self.calls(), self.posts, fail_closed=True,
                              bullet_only=True, max_visible_lines=8)
        self.assertEqual(result["status"], "located")
        self.assertEqual(result["visible_claim_lines"], 8)
        self.assertEqual(result["format_omitted_lines"], 2)
        self.assertEqual(result["omitted_lines"], 2)
        self.assertNotIn("Extra prose.", result["rendered"])

    async def test_fail_closed_keeps_only_wholly_located_claim_lines(self):
        result = audit_answer(
            "First fact. [[sample|L2]]\n"
            "Wrong location. [[sample|L9]]\n"
            "Unsupported second claim.\n"
            "Mixed claim. [[sample|L2]] [[sample|Lx]]",
            self.calls(), self.posts, fail_closed=True)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["omitted_lines"], 3)
        self.assertEqual(result["visible_claim_lines"], 1)
        self.assertIn("First fact. 【sample L2-L2 v", result["rendered"])
        for hidden in ("Wrong location", "Unsupported second", "Mixed claim", "[[sample"):
            self.assertNotIn(hidden, result["rendered"])
        self.assertIn("已省略 3 行", result["rendered"])

    async def test_fail_closed_with_no_located_claim_uses_fixed_no_evidence_reply(self):
        result = audit_answer("False claim. [[sample|L9]]\nAnother claim.",
                              self.calls(), self.posts, fail_closed=True)
        self.assertEqual(result["visible_claim_lines"], 0)
        self.assertEqual(result["omitted_lines"], 2)
        self.assertEqual(result["rendered"],
                         "本轮没有可定位到已读原文的断言，无法提供可靠回答。已省略 2 行未经核验的内容。")
        self.assertNotIn("False claim", result["rendered"])

    async def test_edit_or_delete_invalidates_prior_read_version(self):
        old = self.calls()
        self.path.write_text("# Sample\nChanged fact.\n", encoding="utf-8")
        result = audit_answer("Old claim [[sample|L2-L2]]", old, self.posts)
        self.assertEqual(result["citations"][0]["reason"], "not_read_or_stale")
        self.path.unlink()
        result = audit_answer("Old claim [[sample|L2-L2]]", old, self.posts)
        self.assertEqual(result["citations"][0]["reason"], "source_unavailable")

    async def test_runtime_rewrites_location_and_persists_guarded_reply(self):
        store = BlogStore(self.root / "qa.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        state["prompt"]["version"] = "2026-10-13.8"
        store.save(state)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                       answer("First fact. [[sample|L2-L2]]\nWrong location. [[sample|L9-L9]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("What does Sample say?")
        self.assertIn("sample L2-L2 v", reply)
        self.assertIn("已省略 1 行", reply)
        self.assertNotIn("Wrong location", reply)
        self.assertNotIn("[[sample|L9-L9]]", reply)
        self.assertEqual(state["turns"][-1]["citation_audit"]["invalid"], 1)
        self.assertEqual(state["turns"][-1]["result"], "citation_unverified")
        self.assertEqual(store.load()["turns"][-1]["reply"], reply)
        self.assertNotIn("Wrong location", str(state["history"][-1]))

    async def test_saved_point_three_prompt_preserves_warning_and_original_claim(self):
        store = BlogStore(self.root / "v3.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        state["prompt"]["version"] = "2026-10-13.3"
        store.save(state)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                       answer("Wrong location. [[sample|L9]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("What does Sample say?")
        self.assertIn("Wrong location. 【引用未核验】", reply)
        self.assertEqual(state["turns"][-1]["result"], "citation_unverified")

    async def test_overwide_citation_retries_once_without_exposing_first_answer(self):
        store = BlogStore(self.root / "retry.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        state["instructions"] = "Archived 2026-10-13.4 instructions"
        state["prompt"] = {"version": "2026-10-13.4",
                           "hash": hashlib.sha256(state["instructions"].encode()).hexdigest()}
        store.save(state)
        self.assertFalse(store.prompt_status(state)["current"])
        client = model(answer("hello"), response(call("read_post", {
            "post_id": "sample", "start_line": 1, "limit": 3}, "read")),
                       answer("Private first draft. [[sample|L1-L20]]"),
                       answer("First fact. [[sample|L2]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("What does Sample say?")
        self.assertIn("First fact. 【sample L2-L2 v", reply)
        self.assertEqual(state["turns"][-1]["retrieval"]["citation_retries"], 1)
        self.assertEqual(state["turns"][-1]["result"], "answered_unverified")
        self.assertEqual(client.responses.create.call_count, 4)
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])
        self.assertNotIn("Private first draft", json.dumps(state["history"], ensure_ascii=False))
        self.assertNotIn("Private first draft", json.dumps(state["context"], ensure_ascii=False))
        self.assertNotIn("Private first draft", json.dumps(store.load()["turns"], ensure_ascii=False))

    async def test_uncited_line_retries_once_without_exposing_first_answer(self):
        store = BlogStore(self.root / "uncited-retry.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                       answer("- First fact. [[sample|L2]]\nPrivate uncited draft."),
                       answer("- First fact. [[sample|L2]]\n- Second fact. [[sample|L3]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("What does Sample say?")
        self.assertEqual(state["turns"][-1]["retrieval"]["citation_retries"], 1)
        self.assertEqual(state["turns"][-1]["citation_audit"]["status"], "located")
        self.assertIn("Second fact.", reply)
        self.assertNotIn("Private uncited draft", json.dumps(state["history"], ensure_ascii=False))
        self.assertEqual(client.responses.create.call_count, 4)
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])

    async def test_reasoning_only_answer_retries_once_with_saved_allowance(self):
        store = BlogStore(self.root / "empty-retry.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                       answer(""), answer("- First fact. [[sample|L2]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        status, reply = await conversation.submit("What does Sample say?")
        self.assertEqual(status, "waiting_user")
        self.assertIn("First fact.", reply)
        self.assertEqual(state["turns"][-1]["retrieval"]["output_retries"], 1)
        self.assertEqual(state["turns"][-1]["result"], "answered_unverified")
        self.assertEqual(client.responses.create.call_count, 4)
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])

    async def test_wrong_read_range_can_retry_during_no_progress_finalization(self):
        store = BlogStore(self.root / "wrong-range-retry.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        responses = [answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                     answer("- First fact. [[sample|L2]]\n- Private wrong line. [[sample|L9]]"),
                     answer("- First fact. [[sample|L2]]\n- Second fact. [[sample|L3]]")]
        client = model()

        async def create(**kwargs):
            item = responses.pop(0)
            if len(responses) == 1:
                state["turns"][-1]["retrieval"].update(reason="no_progress", finalizing=True)
            return item

        client.responses.create.side_effect = create
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("What does Sample say?")
        self.assertEqual(state["turns"][-1]["retrieval"]["citation_retries"], 1)
        self.assertEqual(state["turns"][-1]["citation_audit"]["status"], "located")
        self.assertIn("Second fact.", reply)
        self.assertNotIn("Private wrong line", json.dumps(state["history"], ensure_ascii=False))
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])

    async def test_retry_resume_keeps_one_retry_and_source_answer_private(self):
        store = BlogStore(self.root / "resume.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                       answer("Private first draft. [[sample|L1-L20]]"), asyncio.CancelledError())
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit("What does Sample say?")
        saved = store.load()
        self.assertEqual(saved["turns"][-1]["retrieval"]["citation_retries"], 1)
        self.assertNotIn("Private first draft", json.dumps(saved["history"], ensure_ascii=False))
        resumed = create_conversation(model(answer("- First fact. [[sample|L2]]")), saved, store)
        _, reply = await resumed.resume_turn()
        self.assertIn("First fact. 【sample L2-L2 v", reply)
        self.assertEqual(saved["turns"][-1]["retrieval"]["citation_retries"], 1)

    async def test_second_overwide_answer_is_not_retried_again(self):
        store = BlogStore(self.root / "twice.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                       answer("First draft. [[sample|L1-L20]]"),
                       answer("Second draft. [[sample|L1-L20]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("What does Sample say?")
        self.assertEqual(client.responses.create.call_count, 4)
        self.assertEqual(state["turns"][-1]["result"], "citation_unverified")
        self.assertNotIn("First draft", reply)
        self.assertNotIn("Second draft", reply)

    async def test_no_retry_when_request_or_time_budget_is_exhausted(self):
        for limit in (2, 8):
            with self.subTest(limit=limit):
                store = BlogStore(self.root / f"budget-{limit}.json", posts_dir=self.posts_dir)
                state = store.start("fixture", compact_mode="none", new=True)
                responses = [answer("hello"), response(call("read_post_full", {"post_id": "sample"}, "read")),
                             answer("Unverified. [[sample|L1-L20]]")]
                client = model()
                async def create(**kwargs):
                    item = responses.pop(0)
                    if not responses and limit == 8:
                        state["turns"][-1]["retrieval"]["deadline"] = time.time() - 1
                    return item
                client.responses.create.side_effect = create
                conversation = create_conversation(client, state, store, max_steps=limit)
                await conversation.introduce()
                _, reply = await conversation.submit("What does Sample say?")
                self.assertEqual(client.responses.create.call_count, 3)
                self.assertEqual(state["turns"][-1]["retrieval"]["citation_retries"], 0)
                self.assertNotIn("Unverified.", reply)

    async def test_manual_score_requires_atomic_labeled_assertions(self):
        path = self.root / "review.jsonl"
        rows = [{"unit_id": "a", "manual_support": "supported", "atomic_assertion": False,
                 "citation_reference": "sample L2", "citation_location_valid": True},
                {"unit_id": "b", "manual_support": "unsupported", "atomic_assertion": True,
                 "citation_reference": "sample L3", "citation_location_valid": False}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertEqual(score_review(path)["status"], "incomplete")
        rows[0]["atomic_assertion"] = True
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertEqual(score_review(path)["semantic_support_rate"], 0.5)
        rows[0]["citation_location_valid"] = False
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with self.assertRaises(ValueError):
            score_review(path)

    async def test_saved_legacy_prompt_keeps_its_reply_contract(self):
        store = BlogStore(self.root / "legacy.json", posts_dir=self.posts_dir)
        state = store.start("fixture", compact_mode="none", new=True)
        state["instructions"] = "Legacy read and answer guidance"
        state["prompt"] = {"version": "legacy", "hash": hashlib.sha256(
            state["instructions"].encode()).hexdigest()}
        store.save(state)
        client = model(answer("hello"), response(call("read_post", {
            "post_id": "sample", "start_line": 1, "limit": 3}, "read")),
            answer("Legacy answer with free-form citation."))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain Sample")
        self.assertEqual(reply, "Legacy answer with free-form citation.")
        self.assertNotIn("citation_audit", state["turns"][-1])


if __name__ == "__main__":
    unittest.main()
