"""Folder discovery and article reads stay within the selected knowledge root."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from blog_assistant.retrieval import SamplePosts
from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.permissions import READ_TOOLS
from agent_core.trace_grading import grade_trace
from test_blog_agent import answer, call, model, response


class NestedPostsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.posts = self.root / "Blog"
        self.folder = self.posts / "底层技能"
        self.folder.mkdir(parents=True)
        (self.folder / "恢复.md").write_text("# 恢复\nCheckpoint restores state.\n", encoding="utf-8")

    def test_find_folder_then_read_article(self):
        posts = SamplePosts(self.posts)
        found = posts.find_file("底层技能")
        self.assertEqual(found["data"], [str(self.folder / "恢复.md")])
        read = posts.read("底层技能/恢复", 1, 2)
        self.assertIn("Checkpoint", read["text"])

    def test_nested_search_and_keyword_location_have_relative_identity(self):
        posts = SamplePosts(self.posts)
        match = posts.search("Checkpoint", 3)["matches"][0]
        self.assertEqual(match["post_id"], "底层技能/恢复")
        path = posts.find_file("底层技能")["data"][0]
        located = posts.find_text([path], "Checkpoint")["data"][0]
        self.assertEqual(located["line"], 2)
        self.assertEqual(located["post_id"], match["post_id"])

    def test_duplicate_filenames_in_different_folders_stay_distinct(self):
        other = self.posts / "其他"
        other.mkdir()
        (other / "恢复.md").write_text("# Other\nDistinct body", encoding="utf-8")
        posts = SamplePosts(self.posts)
        ids = {i["post_id"] for i in posts.list_posts(20)["items"]}
        self.assertEqual(ids, {"底层技能/恢复", "其他/恢复"})
        self.assertIn("Distinct", posts.read("其他/恢复", 1, 2)["text"])

    def test_deeper_article_is_reachable_and_updates_invalidate_pagination(self):
        child = self.folder / "内层"
        child.mkdir()
        path = child / "notes.md"
        path.write_text("# Notes\nFirst version", encoding="utf-8")
        posts = SamplePosts(self.posts)
        cursor = posts.list_posts(1)["next_cursor"]
        self.assertIn("First version", posts.read("底层技能/内层/notes", 1, 2)["text"])
        path.write_text("# Notes\nChanged version", encoding="utf-8")
        with self.assertRaises(ValueError):
            posts.list_posts(1, cursor)

    def test_traversal_absolute_paths_and_windows_streams_are_rejected(self):
        posts = SamplePosts(self.posts)
        for identifier in ("../outside", "底层技能/../../outside", "/outside", "C:/outside",
                           "底层技能\\恢复", "file:stream", "a//b", "a/../b", "a/./b"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                posts.read(identifier, 1, 2)

    def directory_link(self, link, target):
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_outside_directory_link_is_not_traversed_and_explicit_read_is_denied(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("# Outside\nDo not read", encoding="utf-8")
        self.directory_link(self.posts / "escape", outside)
        posts = SamplePosts(self.posts)
        self.assertEqual(posts.find_file("secret")["data"], [])
        self.assertEqual(posts.list_posts(20)["total"], 1)
        with self.assertRaises(PermissionError):
            posts.read("escape/secret", 1, 2)

    def test_directory_link_cycle_does_not_duplicate_or_loop(self):
        self.directory_link(self.folder / "loop", self.posts)
        page = SamplePosts(self.posts).list_posts(20)
        self.assertEqual(page["total"], 1)
        self.assertTrue(page["complete"])

    def test_discovered_file_replaced_by_outside_directory_link_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("# Outside", encoding="utf-8")
        self.directory_link(self.posts / "escape", outside)
        result = SamplePosts(self.posts).find_text([str(self.posts / "escape" / "secret.md")], "Outside")
        self.assertEqual(result["data"], [])
        self.assertEqual(result["issues"][0]["error_type"], "PermissionError")

    def test_recursive_article_and_directory_limits_are_enforced(self):
        from unittest.mock import patch
        posts = SamplePosts(self.posts)
        with patch("blog_assistant.retrieval.MAX_DIRECTORIES", 1), self.assertRaisesRegex(ValueError, "directory"):
            posts.candidates()
        with patch("blog_assistant.retrieval.MAX_ARTICLES", 0), self.assertRaisesRegex(ValueError, "articles"):
            posts.candidates()


class NestedRoundtripTests(unittest.IsolatedAsyncioTestCase):
    setUp = NestedPostsTests.setUp

    async def test_folder_discovery_keyword_read_and_trace_grading(self):
        store = BlogStore(self.root / "qa.json", posts_dir=self.posts)
        state = store.start("fixture", compact_mode="none", new=True)
        client = model(answer("hello"), response(call("find_file", {"name": "底层技能"}, "find")),
                       response(call("find_text", {"paths": [str(self.folder / "恢复.md")], "text": "Checkpoint"}, "locate")),
                       response(call("read_post", {"post_id": "底层技能/恢复", "start_line": 1, "limit": 2}, "read")),
                       answer("- Checkpoint restores state [[底层技能/恢复|L2]]"))
        conversation = create_conversation(client, state, store)
        await conversation.introduce()
        status, reply = await conversation.submit("读底层技能文件夹里的文章")
        self.assertEqual(status, "waiting_user")
        self.assertIn("Checkpoint", reply)
        rubric = {"version": 1, "id": "nested", "expect": {"turns": [{"number": 1,
            "tools": ["find_file", "find_text", "read_post"], "reply_contains": ["Checkpoint"]}]}}
        report = grade_trace(state, rubric, allowed_tools=READ_TOOLS)
        self.assertTrue(report["passed"], json.dumps(report["failure_counts"]))
        state["tool_calls"][-1]["result"]["data"]["source"] = self.posts.as_uri() + "/nested/../../outside.md"
        self.assertFalse(grade_trace(state, rubric, allowed_tools=READ_TOOLS)["passed"])


if __name__ == "__main__":
    unittest.main()
