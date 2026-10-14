"""EXP-2 offline contracts with temporary Markdown, no external model."""

import tempfile
import unittest
from pathlib import Path

from blog_assistant.retrieval import SamplePosts
from blog_assistant.retrieval_methods import (OfflineVectorIndex, build_chunks,
                                              rank_bm25, reciprocal_rank_fusion)


class RetrievalMethodsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def posts(self):
        return SamplePosts(self.root)

    def test_chunk_keeps_code_and_table_with_verifiable_lines(self):
        (self.root / "notes.md").write_text(
            "# Notes\n\nIntro paragraph.\n\n## Code\n```python\nprint('x')\n```\n\n"
            "## Table\n| key | value |\n| a | b |\n", encoding="utf-8")
        built = build_chunks(self.posts(), target_tokens=8)
        chunks = built["chunks"]
        self.assertTrue(any("```python\nprint('x')\n```" in chunk["text"] for chunk in chunks))
        self.assertTrue(any("| key | value |\n| a | b |" in chunk["text"] for chunk in chunks))
        self.assertTrue(all(chunk["version"] and chunk["source"] and chunk["start_line"] <= chunk["end_line"]
                            for chunk in chunks))
        self.assertTrue(any(chunk["oversized_block"] for chunk in chunks))

    def test_keyword_ranking_finds_chinese_and_respects_per_article_limit(self):
        (self.root / "zh.md").write_text("# 缓存\n\n稳定的前缀可以命中缓存。\n\n另一个缓存段落。", encoding="utf-8")
        (self.root / "other.md").write_text("# Other\n\nUnrelated paragraph.", encoding="utf-8")
        built = build_chunks(self.posts(), target_tokens=10)
        rows = rank_bm25(built["chunks"], "缓存 前缀", top_k=3, per_article=1)
        self.assertEqual(rows[0]["chunk"]["article_id"], "zh")
        self.assertLessEqual(sum(row["chunk"]["article_id"] == "zh" for row in rows), 1)

    def test_mock_vector_index_invalidates_on_edit_and_delete(self):
        source = self.root / "one.md"
        source.write_text("# One\n\nAlpha evidence.", encoding="utf-8")
        index = OfflineVectorIndex("deterministic-fixture", 2)
        first = build_chunks(self.posts())
        self.assertGreater(index.update(first, lambda _: (1, 0))["encoded"], 0)
        self.assertEqual(index.update(first, lambda _: (1, 0))["encoded"], 0)
        self.assertTrue(index.rank((1, 0), index_version=first["index_version"]))
        source.write_text("# One\n\nChanged evidence.", encoding="utf-8")
        changed = build_chunks(self.posts())
        with self.assertRaises(ValueError):
            index.rank((1, 0), index_version=changed["index_version"])
        self.assertGreater(index.update(changed, lambda _: (0, 1))["encoded"], 0)
        source.unlink()
        empty = build_chunks(self.posts())
        self.assertGreater(index.update(empty, lambda _: (0, 1))["removed"], 0)
        self.assertEqual(index.rank((1, 0), index_version=empty["index_version"]), [])

    def test_fusion_deduplicates_and_keeps_one_evidence_identity(self):
        a = {"chunk_id": "a"}
        b = {"chunk_id": "b"}
        rows = reciprocal_rank_fusion([{"chunk": a}, {"chunk": b}],
                                      [{"chunk": b}, {"chunk": a}])
        self.assertEqual([row["chunk"]["chunk_id"] for row in rows], ["a", "b"])
        self.assertEqual(len(rows), 2)
