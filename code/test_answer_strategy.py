"""Product regressions use temporary sources and the actual conversation loop."""
import unittest
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.section_citations import audit_sections
from blog_assistant.retrieval import SamplePosts
from test_blog_agent import answer, call, model, response


class AnswerStrategyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.posts = self.root / "posts"
        self.posts.mkdir()
        self.store = BlogStore(self.root / "state.json", posts_dir=self.posts)
        self.state = self.store.start("fixture", new=True, compact_mode="none")

    async def test_empty_library_help_keeps_capabilities_without_evidence(self):
        client = model(answer("hello"), answer("只读搜索、阅读；尚未连接网站。"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("你可以做什么？")
        self.assertIn("只读", reply)
        self.assertNotIn("检索未返回", reply)
        self.assertEqual(self.state["turns"][-1]["result"], "completed")
        self.assertFalse(self.state["tool_calls"])

    async def test_mixed_language_capability_question_keeps_chinese_reply(self):
        conversation = create_conversation(model(answer("hello")), self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Blog Agent可以做什么？")
        self.assertIn("只读", reply)
        self.assertEqual(self.state["turns"][-1]["result"], "completed")

    async def test_structured_answer_keeps_prose_table_and_section_source(self):
        (self.posts / "design.md").write_text(
            "# System design\n\n## Boundaries\nSeparate storage and presentation.\n", encoding="utf-8")
        draft = ("## Explanation\n\nSeparate storage and presentation.\n\n"
                 "| Layer | Role |\n| --- | --- |\n| Storage | Persistence |\n\n"
                 "[[design|Boundaries]]")
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "design"}, "read")), answer(draft))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain design")
        self.assertIn("## Explanation", reply)
        self.assertIn("| Layer | Role |", reply)
        self.assertIn("System design", reply)
        self.assertIn("Boundaries", reply)
        self.assertNotRegex(reply, r"L\d+-L\d+|v[0-9a-f]{12}")
        self.assertNotIn("[[", reply)

    async def test_empty_discoveries_allow_one_distinct_rewrite_within_budget(self):
        (self.posts / "design.md").write_text("# Architecture\nEvidence.\n", encoding="utf-8")
        client = model(answer("hello"),
            response(call("search_posts", {"query": "构造", "limit": 3}, "first")),
            response(call("find_file", {"name": "构造"}, "filename")),
            response(call("search_posts", {"query": "Architecture", "limit": 3}, "rewrite")),
            response(call("read_post_full", {"post_id": "design"}, "read")),
            answer("Evidence.\n\n[[design]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("解释系统构造设计")
        self.assertIn("Evidence", reply)
        budget = self.state["turns"][-1]["retrieval"]
        self.assertTrue(budget["recovery_used"])
        self.assertEqual(budget["searches"], 2)
        self.assertLessEqual(budget["model_requests"], budget["limits"]["model_requests"])

    async def test_source_repair_is_bounded_and_persists_clean_history(self):
        (self.posts / "design.md").write_text("# Design\n## Boundary\nEvidence.\n", encoding="utf-8")
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "design"}, "read")),
                       answer("Private draft. [[design|Invented section]]"),
                       answer("## Explanation\nEvidence.\n\n[[design|Boundary]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain the design")
        self.assertIn("## Explanation", reply)
        self.assertEqual(self.state["turns"][-1]["retrieval"]["citation_retries"], 1)
        self.assertNotIn("Private draft", str(self.state["history"]))
        self.assertEqual(client.responses.create.call_args.kwargs["tools"], [])

    def test_article_section_sources_reject_unread_stale_and_fenced_headings(self):
        path = self.posts / "design.md"
        path.write_text("# Design\n## Real\nEvidence.\n```python\n# Fake\n```\n", encoding="utf-8")
        posts = SamplePosts(self.posts)
        calls = [{"name": "read_post_full", "status": "completed", "result": {"data": posts.read_full("design")}}]
        good = audit_sections("## Explanation\nEvidence.\n\n[[design|Real]]\n[[design|Real]]", calls, posts)
        self.assertEqual(good["rendered"].count("Source:"), 1)
        for marker in ("[[design|Fake]]", "[[missing]]", "[[design|Imaginary]]", "[[design", "no source"):
            with self.subTest(marker=marker):
                self.assertNotEqual(audit_sections("Private draft " + marker, calls, posts)["status"], "located")
        path.write_text("# Design\nChanged.\n", encoding="utf-8")
        self.assertEqual(audit_sections("Old claim [[design]]", calls, posts)["status"], "invalid")

    async def test_repeated_sources_keep_labels_in_body_and_summary(self):
        from agent_core.presentation import MarkdownRenderer
        from rich.text import Text

        (self.posts / "design.md").write_text("# Design\n## Boundary\nEvidence.\n", encoding="utf-8")
        (self.posts / "storage.md").write_text("# Storage\n## Persistence\nEvidence.\n", encoding="utf-8")
        for separator, label in (("、", "来源："), (", ", "Sources:")):
            with self.subTest(separator=separator):
                draft = ("正文 Evidence. [[design|Boundary]] [[storage|Persistence]]\n\n**" + label + "** "
                         + separator.join(("[[design|Boundary]]", "[[storage|Persistence]]", "[[design|Boundary]]")))
                state = self.store.start("fixture", new=True, compact_mode="none")
                client = model(answer("hello"),
                               response(call("read_post_full", {"post_id": "design"}, "design-read"),
                                        call("read_post_full", {"post_id": "storage"}, "storage-read")), answer(draft))
                conversation = create_conversation(client, state, self.store)
                await conversation.introduce()
                _, reply = await conversation.submit("Explain design and storage")
                summary = reply.split("**" + label + "** ")[1]
                self.assertEqual(summary.count("Design"), 2)
                self.assertEqual(summary.count("Storage"), 1)
                self.assertEqual(summary.count("Boundary"), 2)
                self.assertNotIn("[[", reply)
                audit = state["turns"][-1]["citation_audit"]
                self.assertEqual((audit["status"], audit["located"], audit["invalid"]), ("located", 5, 0))
                self.assertEqual(self.store.load()["turns"][-1]["reply"], reply)
                for mode in ("plain", "rich"):
                    output = []
                    MarkdownRenderer(mode, write_output=output.append).render(reply)
                    self.assertEqual(Text.from_ansi(output[0]).plain.count("Design"), 3)

    async def test_article_help_word_does_not_bypass_evidence_policy(self):
        client = model(answer("hello"), answer("Invented architecture result."))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Help explain the architecture article")
        self.assertEqual(self.state["turns"][-1]["result"], "no_evidence")
        self.assertNotIn("Invented", reply)

    async def test_actual_saved_point_ten_instructions_keep_existing_full_read_tool(self):
        (self.posts / "design.md").write_text("# Design\nEvidence.\n", encoding="utf-8")
        self.state["instructions"] = "Saved 2026-10-13.10 instructions with read_post_full and line sources"
        self.state["prompt"] = {"version": "2026-10-13.10", "hash": hashlib.sha256(self.state["instructions"].encode()).hexdigest()}
        self.store.save(self.state)
        self.assertFalse(self.store.prompt_status(self.state)["current"])
        client = model(answer("hello"), response(call("read_post_full", {"post_id": "design"}, "read")),
                       answer("- Evidence. [[design|L2]]"))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit("Explain the design")
        self.assertEqual(self.state["tool_calls"][0]["status"], "completed")
        self.assertIn("design L2-L2 v", reply)
        self.assertEqual(self.store.load()["prompt"]["version"], "2026-10-13.10")


if __name__ == "__main__":
    unittest.main()
