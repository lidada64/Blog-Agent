"""Reading experiment reaches real requests and survives checkpoint recovery."""
import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_core.context import context_size
from blog_assistant.convergence import RetrievalLimits
from blog_assistant.reading_skill import load_snapshot, overhead
from blog_assistant.session import BlogStore, create_conversation
from test_blog_agent import answer, call, model, response


class ReadingSkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.posts = self.root / "posts"
        self.posts.mkdir()
        (self.posts / "example.md").write_text("# Example\nA concrete fact.\n", encoding="utf-8")
        # A fixed fixture is independent of the developer's installed skills.
        self.snapshot = {"name": "efficient-web-research", "version": 1,
                         "sha256": "fixture", "text": "Skim before you dive.", "adapter": "fixture"}
        self.loader = patch("blog_assistant.session.load_snapshot", return_value=self.snapshot)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.validation = patch("blog_assistant.session.validate_snapshot")
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.store = BlogStore(self.root / "state.json", posts_dir=self.posts)

    async def test_request_includes_skill_and_uses_existing_tools_and_citations(self):
        state = self.store.start("mock", skill="efficient-web-research")
        client = model(answer("Hello"), response(call("read_post_full", {"post_id": "example"}, "read")),
                       answer("A concrete fact. [[example]]"))
        conv = create_conversation(client, state, self.store)
        await conv.introduce()
        await conv.submit("Explain example")
        request = client.responses.create.call_args.kwargs
        self.assertIn("Skim before you dive.", request["instructions"])
        self.assertIn("Blog Agent", request["instructions"])
        self.assertEqual(state["turns"][-1]["result"], "answered_unverified")
        self.assertLessEqual(context_size(request["input"]) + overhead(state), 32000)
        self.assertNotIn("search_web", [t["name"] for t in request["tools"]])

    async def test_restart_uses_snapshot_and_cannot_silently_change_skill(self):
        state = self.store.start("mock", skill="efficient-web-research")
        state["custom_test_counter"] = 7
        self.store.save(state)
        with patch("blog_assistant.session.load_snapshot", side_effect=AssertionError("must not reread")):
            restored = self.store.start("mock", skill="efficient-web-research", resume=True)
        self.assertEqual(restored["reading_skill"], self.snapshot)
        self.assertEqual(restored["custom_test_counter"], 7)
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.start("mock", skill="none")
        self.assertEqual(before, self.store.path.read_bytes())

    async def test_baseline_unchanged_and_old_session_requires_separate_file(self):
        state = self.store.start("mock")
        self.assertNotIn("reading_skill", state)
        self.assertEqual(overhead(state), 0)
        with self.assertRaises(ValueError):
            self.store.start("mock", skill="efficient-web-research")

    async def test_skill_cannot_bypass_small_input_budget(self):
        state = self.store.start("mock", skill="efficient-web-research")
        client = model(answer("Hello"))
        conv = create_conversation(client, state, self.store,
                                   retrieval_limits=RetrievalLimits(context_bytes=10))
        await conv.introduce()
        await conv.submit("Explain example")
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(state["turns"][-1]["retrieval"]["model_requests"], 0)


class SnapshotValidationTests(unittest.TestCase):
    def test_tampered_source_is_rejected(self):
        with patch("blog_assistant.reading_skill.Path.read_bytes", return_value=b"changed"):
            with self.assertRaises(ValueError):
                load_snapshot()

    def test_invalid_checkpoint_is_rejected(self):
        from blog_assistant.reading_skill import validate_snapshot
        for invalid in ({}, {"name": "unknown"}, "text"):
            with self.assertRaises(ValueError):
                validate_snapshot({"reading_skill": copy.deepcopy(invalid)})
