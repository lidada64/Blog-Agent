"""EXP-1 boundaries and source integrity over disposable Markdown articles."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blog_assistant.experiment import canonical, validate_dataset, write_fixtures
from blog_assistant.reading_experiment import INPUT_LIMIT, OUTPUT_RESERVE, as_evidence, retrieve, units


class ReadingExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        dataset = validate_dataset(json.loads((Path(__file__).parent / "evals" / "retrieval-exp0.json").read_text(encoding="utf-8")))
        self.posts = write_fixtures(dataset, Path(self.temp.name) / "fixtures")
        self.cases = {case["id"]: case for case in dataset["cases"]}

    def test_d9_shape_routes_across_thresholds(self):
        low = retrieve(self.posts, self.cases["t03"], "auto", 0.5)
        high = retrieve(self.posts, self.cases["t03"], "auto", 0.75)
        self.assertEqual(low["decisions"][0]["route"], "paragraph")
        self.assertEqual(high["decisions"][0]["route"], "full")
        self.assertTrue(high["evidence"][0]["article_complete"])
        self.assertLessEqual(high["serialized_input_bytes"] + OUTPUT_RESERVE, INPUT_LIMIT)

    def test_long_single_line_is_complete_only_when_full_fits(self):
        case = {"id": "long", "question": "Read long line", "query": "long"}
        result = retrieve(self.posts, case, "full")
        self.assertEqual(result["evidence"][0]["mode"], "full")
        self.assertTrue(result["evidence"][0]["article_complete"])
        self.assertIn("long line", result["evidence"][0]["text"])

    def test_full_threshold_below_equal_above(self):
        post = self.posts.snapshot("cache")
        cost = len(canonical(as_evidence(post, "cache", units(post["body"], "full")[0], "full", True)))
        routes = []
        for remaining in (cost - 1, cost, cost + 1):
            with patch("blog_assistant.reading_experiment.available_bytes", return_value=remaining):
                routes.append(retrieve(self.posts, self.cases["t01"], "auto", 1)["decisions"][0]["route"])
        self.assertEqual(routes, ["paragraph", "full", "full"])

    def test_total_input_budget_bounds_multi_article_reads(self):
        for index in range(3):
            (self.posts.directory / f"giant-{index}.md").write_text(
                f"# Giant {index}\n" + "z" * 6100, encoding="utf-8")
        case = {"id": "many", "question": "Review all giant articles", "query": "giant"}
        result = retrieve(self.posts, case, "full")
        self.assertLessEqual(result["serialized_input_bytes"] + OUTPUT_RESERVE, INPUT_LIMIT)
        self.assertTrue(any(decision["route"] == "skip" for decision in result["decisions"]))

    def test_fenced_code_and_table_remain_whole(self):
        body = self.posts.snapshot("markdown")["body"]
        for mode in ("section", "paragraph"):
            chunks = units(body, mode)
            self.assertTrue(any("```python\nprint('hello')\n```" in chunk[2] for chunk in chunks))
            self.assertTrue(any("| key | value |\n| a | b |" in chunk[2] for chunk in chunks))

    def test_expired_search_version_is_rejected(self):
        case = self.cases["t01"]
        matches, issues = self.posts.search(case["query"], 5)["matches"], []
        matches[0]["version"] = "outdated"
        with patch.object(self.posts, "search", return_value={"matches": matches, "issues": issues}):
            result = retrieve(self.posts, case, "auto")
        self.assertEqual(result["decisions"][0]["reason"], "version_expired")

    def test_draft_is_outside_discovery_scope(self):
        case = {"id": "draft", "question": "Read unpublished draft", "query": "Secret draft marker"}
        result = retrieve(self.posts, case, "auto")
        self.assertFalse(any(item["article_id"] == "draft" for item in result["evidence"]))


if __name__ == "__main__":
    unittest.main()
