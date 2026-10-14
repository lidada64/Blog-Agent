"""Task evidence contracts through the actual durable conversation loop."""
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


class AnswerContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.posts = self.root / "posts"
        self.posts.mkdir()
        (self.posts / "mcp.md").write_text("# MCP\n## Protocol\nMCP connects clients to tools.\n\n", encoding="utf-8")
        self.store = BlogStore(self.root / "state.json", posts_dir=self.posts)
        self.state = self.store.start("fixture", new=True)

    async def test_arithmetic_after_article_is_not_article_fact_or_citation_failure(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp|Protocol]]"), answer("2"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        before = client.responses.create.call_count
        _, reply = await conversation.submit("1+1")
        self.assertEqual(reply, "1+1 = 2")
        self.assertEqual(client.responses.create.call_count, before)
        self.assertEqual(self.state["turns"][-1]["answer_contract"]["kind"], "calculation")
        self.assertEqual(self.state["turns"][-1]["result"], "completed")

    async def test_identity_variants_use_runtime_contract_not_article_evidence(self):
        client = model(answer("hello"), *[answer("我是博客助手") for _ in range(3)])
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        for question in ("你是谁", "你是干嘛的我不认识你", "好的，你可以做什么？"):
            _, reply = await conversation.submit(question)
            self.assertIn("只读", reply)
            self.assertNotIn("检索未返回", reply)
        self.assertEqual(client.responses.create.call_count, 1)

    async def test_existing_help_forms_share_runtime_evidence_contract(self):
        client = model(answer("hello"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        for question in ("Blog Agent可以做什么？", "what can you do?", "how do i use this agent?", "help"):
            _, reply = await conversation.submit(question)
            self.assertEqual(self.state["turns"][-1]["answer_contract"]["kind"], "runtime")
            self.assertEqual(self.state["turns"][-1]["result"], "completed")
            self.assertNotIn("未返回支持", reply)
        self.assertEqual(client.responses.create.call_count, 1)

    async def test_previous_user_message_uses_history_without_article_citations(self):
        client = model(answer("hello"), answer("2"), answer("你上一轮说的是1+1"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("1+1")
        _, reply = await conversation.submit("我上一轮说了什么")
        self.assertIn("1+1", reply)
        self.assertNotIn("未返回支持", reply)
        self.assertFalse(self.state["tool_calls"])

    async def test_one_sentence_rewrite_selects_older_answer_across_intervening_topic(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("PRIOR_VISIBLE_ANSWER: MCP connects clients to tools. [[mcp|Protocol]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        client.responses.create.side_effect = [answer("2")]
        await conversation.submit("1+1")
        client.responses.create.side_effect = [answer("MCP connects clients to tools. [[mcp|Protocol]]")]
        _, reply = await conversation.submit("参考你之前的回答，用一句话总结mcp是什么")
        request = client.responses.create.call_args.kwargs
        self.assertIn("PRIOR_VISIBLE_ANSWER", json.dumps(request["input"]))
        self.assertEqual(request["tools"], [])
        self.assertIn("一句话", request["instructions"])
        self.assertIn("MCP connects", reply)
        self.assertEqual(len(self.state["tool_calls"]), 1)
        self.assertEqual(self.state["turns"][1]["task_id"], self.state["turns"][-1]["task_id"])

    async def test_one_sentence_rewrite_retries_long_answer_once_without_rereading(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp]]"),
                       answer("## MCP\n\nLong section.\n\nAnother section. [[mcp]]"),
                       answer("MCP connects clients to tools. [[mcp]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        _, reply = await conversation.submit("按照你之前的回答，用一句话总结mcp")
        self.assertNotIn("## MCP", reply)
        self.assertIn("MCP connects", reply)
        self.assertEqual(len(self.state["tool_calls"]), 1)
        self.assertEqual(self.state["turns"][-1]["answer_contract"]["format_retries"], 1)

    async def test_stale_previous_answer_is_not_reused_as_fact(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp]]"), answer("Old claim [[mcp]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        (self.posts / "mcp.md").write_text("# MCP\nChanged.\n", encoding="utf-8")
        _, reply = await conversation.submit("参考之前回答一句话总结mcp")
        self.assertNotIn("Old claim", reply)
        self.assertNotEqual(self.state["turns"][-1]["result"], "answered_unverified")

    async def test_article_math_and_mixed_identity_do_not_bypass_article_policy(self):
        client = model(answer("hello"), answer("Fabricated article claim."), answer("Fabricated identity article claim."))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        for question in ("这篇文章说1+1是多少", "你是谁，解释没有读过的文章"):
            _, reply = await conversation.submit(question)
            self.assertNotIn("Fabricated", reply)
            self.assertFalse(self.state["turns"][-1].get("answer_contract", {}).get("kind") == "calculation")

    async def test_point_twelve_does_not_implicitly_acquire_new_contract(self):
        self.state["instructions"] = "Saved .12 instructions"
        self.state["prompt"] = {"version": "2026-10-13.12", "hash": hashlib.sha256(self.state["instructions"].encode()).hexdigest()}
        client = model(answer("hello"), answer("2"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("1+1")
        self.assertNotIn("answer_contract", self.state["turns"][-1])
        self.assertEqual(self.store.load()["prompt"]["version"], "2026-10-13.12")

    async def test_calculation_variants_zero_division_and_decimal_use_local_evidence(self):
        conversation = create_conversation(model(answer("hello")), self.state, self.store)
        await conversation.introduce()
        for question, expected in (("请问1+1等于多少？", "1+1 = 2"), ("计算2*(3+4)", "2*(3+4) = 14"),
                                   ("what is 0.1+0.2?", "0.1+0.2 = 0.3"), ("4÷2", "4/2 = 2")):
            _, reply = await conversation.submit(question)
            self.assertEqual(reply, expected)
        _, reply = await conversation.submit("1/0")
        self.assertIn("除零", reply)
        self.assertFalse(self.state["tool_calls"])
        self.assertEqual(sum(t.get("retrieval", {}).get("model_requests", 0) for t in self.state["turns"]), 0)

    async def test_explicit_upgrade_keeps_consumed_budget_but_enables_new_user_turn_contract(self):
        self.state["instructions"] = "Saved .12 instructions"
        self.state["prompt"] = {"version": "2026-10-13.12", "hash": hashlib.sha256(self.state["instructions"].encode()).hexdigest()}
        client = model(answer("hello"), answer("2"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("1+1")
        before = dict(self.state["turns"][-1]["retrieval"])
        self.store.upgrade_prompt(self.state)
        client = model()
        conversation = create_conversation(client, self.state, self.store)
        _, reply = await conversation.submit("1+1")
        self.assertEqual(reply, "1+1 = 2")
        self.assertEqual(self.state["turns"][-2]["retrieval"], before)
        self.assertEqual(client.responses.create.call_count, 0)

    async def test_rewrite_restart_after_format_retry_does_not_reset_requests_or_retry(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp]]"),
                       answer("## Long\n\nFirst section.\n\nSecond section. [[mcp]]"), asyncio.CancelledError())
        conversation = create_conversation(client, self.state, self.store, retrieval_limits=RetrievalLimits(model_requests=4))
        await conversation.introduce()
        await conversation.submit("解释mcp")
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit("按照之前回答用一句话总结mcp")
        saved = self.store.load()
        used = saved["turns"][-1]["retrieval"]["model_requests"]
        client = model(answer("MCP connects clients to tools. [[mcp]]"))
        resumed = create_conversation(client, saved, self.store, retrieval_limits=RetrievalLimits(model_requests=99))
        _, reply = await resumed.introduce()
        self.assertIn("MCP connects", reply)
        self.assertEqual(saved["turns"][-1]["answer_contract"]["format_retries"], 1)
        self.assertEqual(saved["turns"][-1]["retrieval"]["model_requests"], used + 1)
        self.assertEqual(saved["turns"][-1]["retrieval"]["limits"]["model_requests"], 4)
        self.assertEqual(len(saved["tool_calls"]), 1)
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])

    async def test_repeated_long_rewrite_fails_format_without_claiming_citation_failure(self):
        long_answer = "## Long\n\nFirst section.\n\nSecond section. [[mcp]]"
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp]]"), answer(long_answer), answer(long_answer))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        _, reply = await conversation.submit("按照之前回答一句话总结mcp")
        self.assertEqual(self.state["turns"][-1]["result"], "format_unverified")
        self.assertNotIn("## Long", reply)
        self.assertNotIn("章节来源未通过", reply)
        self.assertEqual(self.state["turns"][-1]["answer_contract"]["format_retries"], 1)

    async def test_default_summary_is_brief_and_does_not_repeat_long_sections(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp]]"),
                       answer("## Long\n\nFirst section.\n\nSecond section. [[mcp]]"),
                       answer("MCP connects clients to tools. [[mcp]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        _, reply = await conversation.submit("按照你之前的回答，总结下mcp是什么")
        self.assertNotIn("## Long", reply)
        self.assertTrue(self.state["turns"][-1]["answer_contract"]["brief"])
        self.assertFalse(self.state["turns"][-1]["answer_contract"]["one_sentence"])
        self.assertEqual(len(self.state["tool_calls"]), 1)

    async def test_deleted_source_and_revoked_permission_do_not_use_old_reply_as_evidence(self):
        for failure in ("delete", "revoke"):
            with self.subTest(failure=failure):
                (self.posts / "mcp.md").write_text("# MCP\nEvidence.\n", encoding="utf-8")
                self.state = self.store.start("fixture", new=True)
                client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")), answer("MCP connects clients. [[mcp]]"))
                conversation = create_conversation(client, self.state, self.store)
                await conversation.introduce()
                await conversation.submit("解释mcp")
                before = client.responses.create.call_count
                if failure == "delete":
                    (self.posts / "mcp.md").unlink()
                    _, reply = await conversation.submit("参考之前回答一句话总结mcp")
                else:
                    with patch.object(conversation.turn_policy.tools, "require", side_effect=PermissionError("revoked")):
                        _, reply = await conversation.submit("参考之前回答一句话总结mcp")
                self.assertEqual(self.state["turns"][-1]["result"], "partial")
                self.assertNotIn("MCP connects", reply)
                self.assertEqual(client.responses.create.call_count, before)

    async def test_large_old_reply_input_is_missing_constraints_not_certified_summary(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")), answer("Long " * 500 + "[[mcp]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        conversation.turn_policy.limits = RetrievalLimits(context_bytes=1400)
        before = client.responses.create.call_count
        _, reply = await conversation.submit("参考之前回答一句话总结mcp")
        self.assertTrue(self.state["turns"][-1]["projection"]["missing_constraints"])
        self.assertEqual(self.state["turns"][-1]["result"], "partial")
        self.assertNotIn("Long Long", reply)
        self.assertEqual(client.responses.create.call_count, before)

    async def test_unseen_named_reply_and_no_previous_reply_clarify_without_search(self):
        conversation = create_conversation(model(answer("hello")), self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("参考之前回答一句话总结missing")
        self.assertEqual(self.state["turns"][-1]["answer_contract"]["kind"], "clarify")
        self.assertFalse(self.state["tool_calls"])
        self.assertNotIn("未通过核验", reply)

    async def test_local_calculation_reply_can_be_rewritten_without_article_sources(self):
        client = model(answer("hello"), answer("The result is two."))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("1+1")
        _, reply = await conversation.submit("把之前的回答改写成英文，一句话")
        self.assertIn("two", reply)
        self.assertEqual(self.state["turns"][-1]["answer_contract"]["evidence"], "conversation_metadata")
        self.assertEqual(self.state["turns"][-1]["result"], "completed")
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])

    async def test_chinese_reply_and_english_explanation_references_select_last_answer(self):
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("MCP connects clients. [[mcp]]"),
                       answer("MCP connects clients to tools. [[mcp]]"),
                       answer("MCP connects clients to tools. [[mcp]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        for question in ("Summarize your previous explanation in one sentence", "把刚刚的回复改写成一句话"):
            _, reply = await conversation.submit(question)
            self.assertIn("MCP connects", reply)
            self.assertEqual(self.state["turns"][-1]["answer_contract"]["kind"], "rewrite")
            self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])
        self.assertEqual(len(self.state["tool_calls"]), 1)

    async def test_candidate_metadata_cannot_select_unrelated_recent_answer(self):
        (self.posts / "other.md").write_text("# Other\nOTHER_EVIDENCE\n", encoding="utf-8")
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "mcp"}, "r1")),
                       answer("PRIOR_MCP_REPLY: MCP connects clients. [[mcp]]"),
                       response(call("list_posts", {"limit": 20, "cursor": ""}, "catalog"), call("read_post_full", {"post_id": "other"}, "r2")),
                       answer("OTHER_REPLY [[other]]"), answer("MCP connects clients. [[mcp]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit("解释mcp")
        await conversation.submit("解释other")
        before = len(self.state["tool_calls"])
        await conversation.submit("参考之前回答一句话总结mcp")
        request = json.dumps(client.responses.create.call_args.kwargs["input"])
        self.assertIn("PRIOR_MCP_REPLY", request)
        self.assertNotIn("OTHER_REPLY", request)
        self.assertEqual(self.state["turns"][-1]["answer_contract"]["target_turn"], 1)
        self.assertEqual(len(self.state["tool_calls"]), before)

    async def test_modified_saved_calculation_is_rejected_on_load(self):
        conversation = create_conversation(model(answer("hello")), self.state, self.store)
        await conversation.introduce()
        await conversation.submit("1+1")
        self.state["turns"][-1]["answer_contract"]["calculation"]["value"] = "999"
        self.store.save(self.state)
        with self.assertRaisesRegex(ValueError, "calculation"):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
