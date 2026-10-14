"""EXP-0 contract and failure tests using only temporary synthetic fixtures."""

import json
import tempfile
import unittest
from pathlib import Path

from blog_assistant.experiment import (evidence, replay_d9, run_case,
                                       validate_dataset, write_fixtures)


DATASET = Path(__file__).parent / "evals" / "retrieval-exp0.json"


class RetrievalExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = validate_dataset(json.loads(DATASET.read_text(encoding="utf-8")))
        self.posts = write_fixtures(self.dataset, self.root / "fixtures")
        self.cases = {case["id"]: case for case in self.dataset["cases"]}

    def test_frozen_partition_and_d9_shape(self):
        self.assertEqual(sum(case["split"] == "tune" for case in self.dataset["cases"]), 6)
        self.assertEqual(sum(case["split"] == "holdout" for case in self.dataset["cases"]), 12)
        replay = replay_d9(self.posts)
        self.assertTrue(replay["article_complete_after_reads"])
        self.assertEqual(replay["historical"]["reads"], 7)
        self.assertGreater(replay["total_read_characters"], 5000)

    def test_evidence_has_real_source_version_and_incomplete_range(self):
        item = evidence(self.posts, "sparse-331", 1, 80)
        self.assertFalse(item["complete"])
        self.assertEqual(item["next_line"], 81)
        self.assertEqual(item["utf8_bytes"], len(item["text"].encode("utf-8")))
        self.assertIn(item["version"][:12], item["chunk_id"])
        self.assertIsNone(item["token_count"])

    def test_illegal_path_and_draft_cannot_be_read(self):
        for article_id in ("../draft/draft", "draft", "C:/secret"):
            with self.assertRaises((OSError, ValueError, PermissionError)):
                evidence(self.posts, article_id)
        self.assertFalse(any(item["post_id"] == "draft" for item in self.posts.search("secret", 5)["matches"]))

    def test_no_answer_and_unsupported_instruction_are_scored(self):
        empty = run_case(self.posts, self.cases["h10"])
        self.assertTrue(empty["answer"]["no_answer_correct"])
        injection = run_case(self.posts, self.cases["h12"])
        self.assertFalse(injection["answer"]["no_answer_correct"])

    def test_recovery_is_reserved_and_resume_does_not_refresh_budget(self):
        checkpoint = self.root / "checkpoint.json"
        first = run_case(self.posts, self.cases["t03"], simulate_truncation=True, checkpoint=checkpoint)
        self.assertEqual(first["budget"]["used"], 4)
        self.assertEqual(first["budget"]["answer_attempts"], 2)
        self.assertEqual(first["answer_status"], "answered")
        second = run_case(self.posts, self.cases["t03"], simulate_truncation=True, checkpoint=checkpoint)
        self.assertEqual(second["budget"], first["budget"])
        self.assertEqual(second["answer_status"], first["answer_status"])

    def test_stale_checkpoint_rejected_after_article_change(self):
        checkpoint = self.root / "checkpoint.json"
        run_case(self.posts, self.cases["t01"], checkpoint=checkpoint)
        path = self.posts.directory / "cache.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nUpdated.", encoding="utf-8")
        with self.assertRaises(ValueError):
            run_case(self.posts, self.cases["t01"], checkpoint=checkpoint)

    def test_cancel_and_resume_preserve_exploration_spend(self):
        checkpoint = self.root / "cancelled.json"
        cancelled = run_case(self.posts, self.cases["t01"], checkpoint=checkpoint,
                             cancel_after_retrieval=True)
        self.assertEqual(cancelled["answer_status"], "cancelled")
        used = cancelled["budget"]["used"]
        resumed = run_case(self.posts, self.cases["t01"], checkpoint=checkpoint)
        self.assertEqual(resumed["budget"]["used"], used + 1)
        self.assertEqual(resumed["answer_status"], "answered")

    def test_long_line_is_reported_incomplete_not_full(self):
        result = run_case(self.posts, self.cases["h04"])
        self.assertEqual(result["retrieval"]["recall"], 1)
        self.assertNotIn("long-line", result["retrieval"]["complete_articles"])
        self.assertEqual(result["answer"]["fact_coverage"], 0)


if __name__ == "__main__":
    unittest.main()
