"""Real subprocess cleanup, no model weights/network or provider requests."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from blog_assistant.local_embedding import LocalEmbedding
from blog_assistant.embedding_worker import MODEL


class LocalEmbeddingCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "embedding-manifest.json").write_text(json.dumps({
            "model": MODEL, "dimension": 384, "runtime": "fastembed-0.9.0", "revision": "fixture"}), encoding="utf-8")
        self.encoder = LocalEmbedding(self.root, python=sys.executable)

    async def test_exited_child_with_unread_output_is_fully_closed(self):
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c",
            "import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(30)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=1024)
        self.addAsyncCleanup(self.emergency_cleanup, process)
        # Pause the reader with buffered output, then kill the child before the
        # adapter sees it: returncode is known while pipe cleanup is still pending.
        for _ in range(100):
            if process.stdout._buffer:
                break
            await asyncio.sleep(0.01)
        process.kill()
        for _ in range(100):
            if process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(process.returncode)
        self.encoder.process = process
        await asyncio.wait_for(self.encoder.aclose(), 3)
        self.assertTrue(process.stdin.is_closing())
        self.assertTrue(process.stdout.at_eof())
        self.assertTrue(process._transport.is_closing())
        await self.encoder.aclose()

    @staticmethod
    async def emergency_cleanup(process):
        if process.returncode is None:
            process.kill()
        if process.stdin:
            process.stdin.close()
        await process.communicate()

    async def test_timeout_and_cancellation_close_real_worker(self):
        original = asyncio.create_subprocess_exec
        for failure in ("timeout", "cancel"):
            captured = []
            async def spawn(*args, **kwargs):
                process = await original(sys.executable, "-u", "-c", "import time; time.sleep(30)", **kwargs)
                captured.append(process)
                return process
            with patch("blog_assistant.local_embedding.asyncio.create_subprocess_exec", side_effect=spawn):
                task = asyncio.create_task(self.encoder.encode(["synthetic"], kind="query", timeout=0.2 if failure == "timeout" else 10))
                if failure == "cancel":
                    while not captured:
                        await asyncio.sleep(0.01)
                    task.cancel()
                with self.assertRaises(asyncio.TimeoutError if failure == "timeout" else asyncio.CancelledError):
                    await task
            process = captured[0]
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdin.is_closing())
            self.assertTrue(process.stdout.at_eof())
            self.assertTrue(process._transport.is_closing())

    async def test_cancellation_during_startup_still_owns_and_closes_child(self):
        original = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        release = asyncio.Event()
        captured = []
        async def delayed_spawn(*args, **kwargs):
            process = await original(sys.executable, "-u", "-c", "import time; time.sleep(30)", **kwargs)
            captured.append(process)
            spawned.set()
            await release.wait()
            return process
        with patch("blog_assistant.local_embedding.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            task = asyncio.create_task(self.encoder.encode(["synthetic"], kind="query", timeout=5))
            await spawned.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process = captured[0]
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.at_eof())
        self.assertTrue(process._transport.is_closing())
        self.assertIsNone(self.encoder.process)


if __name__ == "__main__":
    unittest.main()
