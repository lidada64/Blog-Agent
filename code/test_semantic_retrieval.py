"""Deterministic lifecycle/fault checks; fake vectors never count as semantic quality."""
import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from blog_assistant.retrieval import SamplePosts
from blog_assistant.semantic_retrieval import SemanticRetriever
from blog_assistant.sections import chunks, sections
from blog_assistant.tools import BlogTools


class Encoder:
    model_id = "fixture-v1"
    dimension = 3

    def __init__(self):
        self.calls = []
        self.failure = None

    async def encode(self, texts, *, kind, timeout):
        self.calls.append((kind, texts))
        if self.failure:
            raise self.failure
        return [[1, 0, 0] if "architecture" in text.casefold() or "构造" in text else [0, 1, 0] for text in texts]


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.posts_dir = self.root / "posts"
        self.posts_dir.mkdir()
        self.article = self.posts_dir / "design.md"
        self.article.write_text("# Architecture\n\n## Boundaries\nSeparate concerns.\n", encoding="utf-8")
        self.posts = SamplePosts(self.posts_dir)
        self.encoder = Encoder()
        self.state = {"turns": [{"kind": "user"}]}
        self.retriever = SemanticRetriever(self.posts, self.encoder, self.root / "vectors.sqlite")

    async def test_semantic_search_runs_through_formal_tool_and_is_candidate_only(self):
        tools = BlogTools(self.posts, "qa", self.state, enable_full=True, retriever=self.retriever)
        result = await tools.execute("search_posts", {"query": "系统构造设计", "limit": 3})
        self.assertEqual(result["data"]["retrieval"], "hybrid")
        self.assertEqual(result["data"]["matches"][0]["post_id"], "design")
        self.assertTrue(result["data"]["matches"][0]["candidate_only"])
        self.assertEqual([c[0] for c in self.encoder.calls], ["passage", "query"])

    async def test_restart_reuses_both_passage_and_query_vectors(self):
        await self.retriever.search("architecture", 3, self.state)
        again = SemanticRetriever(self.posts, self.encoder, self.root / "vectors.sqlite")
        await again.search("architecture", 3, self.state)
        self.assertEqual(len(self.encoder.calls), 2)

    async def test_edit_delete_and_root_or_mode_isolation(self):
        await self.retriever.search("architecture", 3, self.state)
        self.article.write_text("# New title\n## New section\nChanged body.\n", encoding="utf-8")
        result = await self.retriever.search("Changed", 3, self.state)
        self.assertEqual(result["matches"][0]["title"], "New title")
        with closing(sqlite3.connect(self.root / "vectors.sqlite")) as db:
            count = db.execute("SELECT count(*) FROM vectors WHERE kind='passage'").fetchone()[0]
        self.assertEqual(count, len(chunks(self.posts.snapshot("design"))))
        other = SemanticRetriever(self.posts, self.encoder, self.root / "vectors.sqlite", mode="manage")
        self.assertNotEqual(other.scope, self.retriever.scope)
        self.article.unlink()
        self.assertEqual((await self.retriever.search("architecture", 3, self.state))["matches"], [])

    async def test_failure_falls_back_without_using_stale_vectors(self):
        self.encoder.failure = ValueError("bad vectors")
        result = await self.retriever.search("Boundaries", 3, self.state)
        self.assertEqual(result["retrieval"], "keyword_fallback")
        self.assertEqual(result["matches"][0]["post_id"], "design")
        self.assertEqual(self.state["turns"][-1]["embedding"]["batches"], 1)

    async def test_cancellation_reserves_and_resume_never_restores_allowance(self):
        self.encoder.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.retriever.search("architecture", 3, self.state)
        self.assertEqual(self.state["turns"][-1]["embedding"]["batches"], 1)
        saved = json.loads(json.dumps(self.state))
        saved["turns"][-1]["embedding"]["max_batches"] = 1
        self.encoder.failure = None
        result = await self.retriever.search("architecture", 3, saved)
        self.assertEqual(result["retrieval"], "keyword_fallback")
        self.assertEqual(len(self.encoder.calls), 1)

    async def test_bad_vectors_or_wrong_count_fall_back(self):
        for value in ([[float("nan"), 0, 0]], [[0, 0, 0]], [], [[1, 0]]):
            async def encode(*args, **kwargs):
                return value
            self.encoder.encode = encode
            result = await self.retriever.search("architecture", 3, self.state)
            self.assertEqual(result["retrieval"], "keyword_fallback")

    def test_code_headings_are_not_sections_and_line_coordinates_remain_exact(self):
        body = "# Real\n\n```python\n# Fake\nprint('hi')\n```\n\n## End\nDone\n"
        self.article.write_text(body, encoding="utf-8")
        self.assertEqual([s["title"] for s in sections(body)], ["Real", "End"])
        for chunk in chunks(self.posts.snapshot("design")):
            self.assertEqual(chunk["text"], "\n".join(body.splitlines()[chunk["start_line"] - 1:chunk["end_line"]]))

    def test_long_line_chunks_preserve_character_coordinates_without_silent_skip(self):
        line = "复杂实现与简单接口" * 120
        self.article.write_text("# Design\n" + line, encoding="utf-8")
        rows = [c for c in chunks(self.posts.snapshot("design")) if c.get("line_fragment")]
        self.assertEqual("".join(c["text"] for c in rows), line)
        for row in rows:
            self.assertEqual(row["start_line"], 2)
            self.assertEqual(row["text"], line[row["start_character"]:row["end_character"]])


if __name__ == "__main__":
    unittest.main()
