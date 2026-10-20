"""Offline Stage A acceptance: temporary synthetic articles, no provider calls."""
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blog_assistant.article_memory_store import ArticleMemoryStore
from blog_assistant.article_memory_sync import ScanLimits, scan_library, synchronize


class ArticleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "posts"
        self.root.mkdir()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = ArticleMemoryStore(self.path)
        self.addCleanup(self.store.close)
        self.library = self.store.register_library(self.root, "qa:test")

    def write(self, name="a.md", text="# Title\nIntro\n## Same\nOne\n## Same\nTwo\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def rows(self, table):
        self.assertIn(table, {"jobs", "nodes", "documents", "usage"})
        return list(self.store.db.execute(f"SELECT * FROM {table}"))

    def test_initialization_restart_idempotency_and_no_fake_summaries(self):
        self.write()
        synchronize(self.store, self.library)
        with ArticleMemoryStore(self.path) as reopened:
            self.assertEqual(reopened.register_library(self.root, "qa:test"), self.library)
            synchronize(reopened, self.library)
            status = reopened.status(self.library)
        self.assertTrue(status["initialized"])
        self.assertEqual(status["jobs"], {"blocked_config": 1})
        self.assertTrue(all(row["summary"] is None for row in self.rows("nodes")))
        self.assertIsNone(self.rows("documents")[0]["active_version"])
        self.assertEqual(self.rows("usage"), [])

    def test_add_modify_delete_and_restore_original_version(self):
        path = self.write(text="# A\nold\n")
        synchronize(self.store, self.library)
        old = self.rows("documents")[0]["detected_version"]
        self.write(text="# A\nnew\n")
        self.write("b.md")
        synchronize(self.store, self.library)
        self.assertEqual(self.store.status(self.library)["jobs"], {"blocked_config": 2, "superseded": 1})
        path.unlink()
        synchronize(self.store, self.library)
        self.assertEqual(self.store.status(self.library)["documents"], {"deleted": 1, "stale": 1})
        self.write(text="# A\nold\n")
        synchronize(self.store, self.library)
        row = next(r for r in self.rows("jobs") if r["article_id"] == "a" and r["source_version"] == old)
        self.assertEqual(row["state"], "blocked_config")
        self.assertEqual(len(self.rows("jobs")), 3)

    def test_partial_scan_does_not_delete_missing_documents(self):
        path = self.write()
        synchronize(self.store, self.library)
        path.unlink()
        (self.root / "bad.md").write_bytes(b"\xff")
        result = synchronize(self.store, self.library)
        self.assertFalse(result.complete)
        self.assertEqual(self.store.status(self.library)["documents"], {"stale": 1})
        self.assertEqual(self.store.status(self.library)["scan_state"], "partial")

    def test_initial_limits_never_mark_initialized(self):
        self.write()
        result = synchronize(self.store, self.library, ScanLimits(max_article_bytes=1))
        self.assertFalse(result.complete)
        self.assertFalse(self.store.status(self.library)["initialized"])
        self.write("b.md")
        result = synchronize(self.store, self.library, ScanLimits(max_entries=1))
        self.assertFalse(result.complete)
        self.assertFalse(self.store.status(self.library)["initialized"])

    def test_missing_root_and_enumeration_error_do_not_delete(self):
        path = self.write()
        synchronize(self.store, self.library)
        with patch("blog_assistant.article_memory_sync.os.scandir", side_effect=PermissionError):
            self.assertFalse(synchronize(self.store, self.library).complete)
        path.unlink()
        self.root.rmdir()
        self.assertFalse(synchronize(self.store, self.library).complete)
        self.assertEqual(self.store.status(self.library)["documents"], {"stale": 1})

    def test_newest_scan_fences_old_results(self):
        self.write()
        root, epoch = self.store.begin_scan(self.library)
        old_scan = scan_library(self.library, root, epoch)
        with ArticleMemoryStore(self.path) as other:
            synchronize(other, self.library)
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.store.apply_scan(old_scan)
        self.assertEqual(len(self.rows("jobs")), 1)

    def test_scope_and_root_isolation(self):
        self.write()
        other_scope = self.store.register_library(self.root, "manage:test")
        other_root = Path(self.temp.name) / "other"
        other_root.mkdir()
        (other_root / "a.md").write_text("different", encoding="utf-8")
        other_library = self.store.register_library(other_root, "qa:test")
        for library in (self.library, other_scope, other_library):
            synchronize(self.store, library)
        self.assertEqual(len({r["library_id"] for r in self.rows("jobs")}), 3)

    def test_hash_detects_same_size_and_mtime_change(self):
        path = self.write(text="# A\nold\n")
        synchronize(self.store, self.library)
        stat = path.stat()
        self.write(text="# A\nnew\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        synchronize(self.store, self.library)
        self.assertEqual(len(self.rows("jobs")), 2)

    def test_real_section_ids_and_unheaded_article_range(self):
        self.write()
        self.write("plain.md", "line one\nline two\n")
        synchronize(self.store, self.library)
        nodes = self.rows("nodes")
        same = [n for n in nodes if n["title"] == "Same"]
        self.assertEqual(len(same), 2)
        self.assertNotEqual(same[0]["node_id"], same[1]["node_id"])
        self.assertTrue(all(n["citation_id"] == n["node_id"] for n in same))
        plain = next(n for n in nodes if n["article_id"] == "plain")
        self.assertEqual((plain["start_line"], plain["end_line"], plain["kind"]), (1, 2, "article"))

    def test_large_library_exceeds_foreground_limits(self):
        for index in range(101):
            self.write(f"nested/{index}.md", "# Title\n" + "body\n" * (30_000 if index == 0 else 1))
        self.assertTrue(synchronize(self.store, self.library).complete)
        self.assertEqual(len(self.rows("documents")), 101)

    def test_unknown_schema_rejected_without_rewriting(self):
        future = Path(self.temp.name) / "future.sqlite3"
        with closing(sqlite3.connect(future)) as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            ArticleMemoryStore(future)
        with closing(sqlite3.connect(future)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_unstable_read_is_partial(self):
        path = self.write(text="first")
        original_open = Path.open
        count = 0

        def racing_open(candidate, *args, **kwargs):
            nonlocal count
            if candidate == path and args == ("rb",):
                count += 1
                if count == 2:
                    with original_open(candidate, "wb") as stream:
                        stream.write(b"other")
            return original_open(candidate, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            scan = synchronize(self.store, self.library)
        self.assertFalse(scan.complete)
        self.assertEqual(scan.issues[0]["reason"], "source_changed_during_scan")
        self.assertEqual(self.rows("jobs"), [])

    def test_failed_apply_rolls_back_inventory_and_queue(self):
        self.write()
        root, epoch = self.store.begin_scan(self.library)
        scan = scan_library(self.library, root, epoch)
        self.store.db.execute("""CREATE TRIGGER reject_job BEFORE INSERT ON jobs
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.apply_scan(scan)
        self.assertEqual(self.rows("documents"), [])
        self.assertEqual(self.rows("nodes"), [])
        self.assertFalse(self.store.status(self.library)["initialized"])
        self.store.db.execute("DROP TRIGGER reject_job")
        self.store.apply_scan(scan)
        self.assertEqual(len(self.rows("jobs")), 1)

    def test_linked_entry_never_read(self):
        self.write("linked.md")
        # Portable boundary test without requiring Windows symlink privilege.
        real_resolve = Path.resolve

        def resolve(candidate, *args, **kwargs):
            if candidate.name == "linked.md":
                return Path(self.temp.name) / "outside.md"
            return real_resolve(candidate, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            scan = synchronize(self.store, self.library)
        self.assertFalse(scan.complete)
        self.assertEqual(scan.issues[0]["reason"], "linked_entry")
        self.assertEqual(self.rows("nodes"), [])

    def test_cli_init_and_status(self):
        self.write()
        command = [sys.executable, str(Path(__file__).with_name("blog_memory.py")), "init",
                   "--posts-dir", str(self.root), "--scope", "cli:test", "--database", str(self.path), '--no-worker']
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"worker_state": "stopped"', result.stdout)
        command[2] = "status"
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
