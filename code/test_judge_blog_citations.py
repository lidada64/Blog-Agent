"""The semantic judge receives only exact cited lines and never spends in dry-run."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from blog_assistant.retrieval import SamplePosts
from judge_blog_citations import execute, load_existing, parse_refs, parse_verdict, prepare, save_results, source_for, task_sha256


class SemanticJudgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.posts_dir = self.root / "posts"
        self.posts_dir.mkdir()
        self.article = self.posts_dir / "sample.md"
        self.article.write_text("# Sample\nFact one.\nFact two.\n", encoding="utf-8")
        self.posts = SamplePosts(self.posts_dir)
        data = self.posts.read("sample", 1, 3)
        self.run = {"posts": self.posts, "lines": {("sample", data["version"]):
                    {index: line for index, line in enumerate(data["text"].splitlines(), 1)}}}

    def test_only_cited_lines_are_prepared(self):
        row = {"citations": [{"post_id": "sample", "start_line": 2, "end_line": 2}]}
        source = source_for(row, self.run)
        self.assertEqual(source[0]["lines"], ["L2 Fact one."])
        self.assertNotIn("Fact two", json.dumps(source))

    def test_unread_stale_or_too_many_lines_are_rejected(self):
        row = {"citations": [{"post_id": "sample", "start_line": 3, "end_line": 3}]}
        self.run["lines"][("sample", self.posts.snapshot("sample")["version"])].pop(3)
        with self.assertRaisesRegex(ValueError, "not read"):
            source_for(row, self.run)
        self.article.write_text("# Sample\nChanged.\nFact two.\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            source_for(row, self.run)
        with self.assertRaisesRegex(ValueError, "eight"):
            parse_refs({"citations": [{"post_id": "sample", "start_line": 1, "end_line": 9}]})

    def test_human_shorthand_is_complete_or_rejected(self):
        self.assertEqual(parse_refs({"citation_reference": "PC47,103"}),
                         [("Prompt Cache", 47, 47), ("Prompt Cache", 103, 103)])
        for shorthand in ("invalid", "no_article_citation", "PC267,invalid"):
            with self.subTest(shorthand=shorthand), self.assertRaises(ValueError):
                parse_refs({"citation_reference": shorthand})

    def test_strict_structured_verdict(self):
        good = SimpleNamespace(output_text='{"verdict":"supported","reason":"Direct statement."}')
        self.assertEqual(parse_verdict(good)["verdict"], "supported")
        for content in ('{"verdict":"yes","reason":"x"}',
                        '{"verdict":"supported","reason":"x","extra":1}', "Maybe"):
            with self.subTest(content=content), self.assertRaises((ValueError, json.JSONDecodeError)):
                parse_verdict(SimpleNamespace(output_text=content))

    def test_checkpoint_review_mapping_and_not_citable_does_not_need_network(self):
        # Real saved checkpoints stay under the ignored experiment root; synthetic
        # state uses the same turn/step/read schema without an API client.
        from unittest.mock import patch
        import judge_blog_citations as judge
        state = {"turns": [{"kind": "user", "reply": "Fact one.", "number": 2}],
                 "steps": [{"number": 1, "turn": 2}],
                 "tool_calls": [{"step": 1, "name": "read_post", "status": "completed",
                                 "result": {"data": self.posts.read("sample", 1, 3)}}],
                 "blog": {"posts_dir": str(self.posts_dir)}}
        run_dir = self.root / "runs"
        run_dir.mkdir()
        (run_dir / "test-1.json").write_text(json.dumps(state), encoding="utf-8")
        review = self.root / "review.jsonl"
        rows = [{"id": "1", "run": 1, "atomic_claim": "Fact one", "verdict": "supported",
                 "citation_reference": "PC47", "citations": [{"post_id": "sample", "start_line": 2,
                                                       "end_line": 2}]},
                {"id": "2", "run": 1, "atomic_claim": "No source", "verdict": "uncited",
                 "citation_reference": "invalid"}]
        review.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with patch.object(judge, "OUTPUT_ROOT", self.root):
            tasks, runs = prepare(review, run_dir, "test-*.json")
        self.assertEqual(len(runs), 1)
        self.assertEqual([task["status"] for task in tasks], ["ready", "not_citable"])
        self.assertEqual(tasks[0]["human_verdict"], "supported")

    def test_resume_accepts_only_matching_saved_tasks_and_preserves_pending(self):
        task = {"id": "x", "run_file": "run-1", "atomic_claim": "Fact one",
                "status": "ready", "evidence": [{"lines": ["L2 Fact one."]}]}
        output = self.root / "judge.jsonl"
        save_results(output, [{"id": "x", "task_sha256": task_sha256(task),
                               "status": "pending_may_have_sent", "ledger_request_index": 9}])
        self.assertEqual(load_existing(output, [task])[0]["status"], "pending_may_have_sent")
        with self.assertRaisesRegex(ValueError, "does not match"):
            load_existing(output, [{**task, "atomic_claim": "Changed"}])
        self.assertFalse(output.with_suffix(".jsonl.tmp").exists())

    def test_parse_failure_is_saved_and_next_claim_continues_without_real_network(self):
        class FakeClient:
            responses = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        class FakeGuard:
            def __init__(self, *_):
                self.ledger = {"requests": []}

            async def create(self, *_args, **_kwargs):
                self.ledger["requests"].append({"charged_cny_upper": 0.001})
                result = "bad json" if len(self.ledger["requests"]) == 1 else \
                    '{"verdict":"supported","reason":"Exact match."}'
                return SimpleNamespace(output_text=result, usage={"input_tokens": 1,
                                              "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}})

        tasks = [{"id": str(index), "run_file": "test-1", "atomic_claim": "Fact one",
                  "status": "ready", "evidence": [{"lines": ["L2 Fact one."]}]}
                 for index in (1, 2)]
        output = self.root / "results.jsonl"
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.deepseek.com",
                                          "OPENAI_API_KEY": "test-key"}), \
             patch("judge_blog_citations.CostGuard", FakeGuard), \
             patch("openai.AsyncOpenAI", return_value=FakeClient()):
            rows = asyncio.run(execute(tasks, output, 8.0))
        self.assertEqual([row["status"] for row in rows], ["judge_parse_error", "judged"])
        self.assertEqual(rows[0]["raw_verdict_text"], "bad json")
        self.assertEqual(len(load_existing(output, tasks)), 2)

    def test_resume_skips_pending_unknown_request(self):
        class FakeClient:
            responses = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        class NoCallGuard:
            def __init__(self, *_):
                self.ledger = {"requests": []}

            async def create(self, *_args, **_kwargs):
                raise AssertionError("Pending claim must never be resent")

        task = {"id": "pending", "run_file": "test-1", "atomic_claim": "Fact one",
                "status": "ready", "evidence": [{"lines": ["L2 Fact one."]}]}
        output = self.root / "pending.jsonl"
        save_results(output, [{"id": "pending", "task_sha256": task_sha256(task),
                               "status": "pending_may_have_sent", "ledger_request_index": 0}])
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.deepseek.com",
                                          "OPENAI_API_KEY": "test-key"}), \
             patch("judge_blog_citations.CostGuard", NoCallGuard), \
             patch("openai.AsyncOpenAI", return_value=FakeClient()):
            rows = asyncio.run(execute([task], output, 8.0, resume=True))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending_may_have_sent")

    def test_single_retry_preserves_original_attempt_and_never_retries_twice(self):
        class FakeClient:
            responses = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        class FakeGuard:
            calls = 0

            def __init__(self, *_):
                self.ledger = {"requests": [{"charged_cny_upper": 0.002}]}

            async def create(self, *_args, **kwargs):
                FakeGuard.calls += 1
                self.ledger["requests"].append({"charged_cny_upper": 0.003})
                self.asserted_max_output = kwargs["max_output_tokens"]
                return SimpleNamespace(output_text='{"verdict":"unsupported","reason":"Missing premise."}',
                                       usage={"input_tokens": 1, "output_tokens": 3})

        task = {"id": "one", "run_file": "test-1", "atomic_claim": "Claim",
                "status": "ready", "evidence": [{"lines": ["L2 Fact one."]}]}
        output = self.root / "retry.jsonl"
        original = {"id": "one", "task_sha256": task_sha256(task), "status": "judge_parse_error",
                    "ledger_request_index": 0, "charged_cny_upper": 0.002,
                    "raw_verdict_text": "", "usage": {"output_tokens": 300}}
        save_results(output, [original])
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.deepseek.com",
                                          "OPENAI_API_KEY": "test-key"}), \
             patch("judge_blog_citations.CostGuard", FakeGuard), \
             patch("openai.AsyncOpenAI", return_value=FakeClient()):
            rows = asyncio.run(execute([task], output, 8.0, resume=True, retry_parse_errors=True))
            rows_again = asyncio.run(execute([task], output, 8.0, resume=True,
                                             retry_parse_errors=True))
        self.assertEqual(FakeGuard.calls, 1)
        self.assertEqual(rows[0]["status"], "judge_parse_error")
        self.assertEqual(rows[0]["charged_cny_upper"], 0.002)
        self.assertEqual(rows[0]["raw_verdict_text"], "")
        self.assertEqual(rows[0]["retry"]["status"], "judged")
        self.assertEqual(rows[0]["retry"]["charged_cny_upper"], 0.003)
        self.assertEqual(rows[0]["retry"]["max_output_tokens"], 1024)
        self.assertEqual(rows_again, rows)

    def test_pending_retry_after_crash_is_never_resent(self):
        class FakeClient:
            responses = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        class NoCallGuard:
            def __init__(self, *_):
                self.ledger = {"requests": []}

            async def create(self, *_args, **_kwargs):
                raise AssertionError("Unknown retry must never be resent")

        task = {"id": "one", "run_file": "test-1", "atomic_claim": "Claim",
                "status": "ready", "evidence": [{"lines": ["L2 Fact one."]}]}
        output = self.root / "retry-pending.jsonl"
        save_results(output, [{"id": "one", "task_sha256": task_sha256(task),
                               "status": "judge_parse_error",
                               "retry": {"status": "pending_may_have_sent", "ledger_request_index": 2}}])
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://api.deepseek.com",
                                          "OPENAI_API_KEY": "test-key"}), \
             patch("judge_blog_citations.CostGuard", NoCallGuard), \
             patch("openai.AsyncOpenAI", return_value=FakeClient()):
            rows = asyncio.run(execute([task], output, 8.0, resume=True,
                                       retry_parse_errors=True))
        self.assertEqual(rows[0]["retry"]["status"], "pending_may_have_sent")


if __name__ == "__main__":
    unittest.main()
