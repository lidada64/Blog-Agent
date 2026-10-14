"""Local-only subprocess adapter; cancellation kills the CPU worker."""
import asyncio
import json
import os
from pathlib import Path

from .embedding_worker import MODEL


class LocalEmbedding:
    model_id = MODEL + ":fastembed-0.9.0:mean-normalized:no-prefix:v1"
    dimension = 384

    def __init__(self, runtime_dir, *, python=None):
        self.runtime_dir = Path(runtime_dir)
        self.revision = None
        try:
            manifest = json.loads((self.runtime_dir / "embedding-manifest.json").read_text(encoding="utf-8"))
            if manifest.get("model") == MODEL and manifest.get("dimension") == self.dimension and manifest.get("runtime") == "fastembed-0.9.0":
                self.revision = manifest["revision"]
                self.model_id += ":" + self.revision
        except (OSError, ValueError, KeyError):
            pass
        self.python = Path(python) if python else self.runtime_dir / "embedding-venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.process = None
        self.lock = asyncio.Lock()

    async def aclose(self):
        # External close must not race an exchange using these same streams.
        async with self.lock:
            await self._close_process()

    async def _close_process(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # It exited between returncode observation and kill.
        # wait() alone can finish before Windows pipe callbacks, or hang when a
        # reader is paused with unread output. Drain to EOF before loop shutdown,
        # including a worker whose exit was already observed.
        await process.communicate()
        if process.stdin is not None:
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when killing a worker during an exchange.

    async def encode(self, texts, *, kind, timeout):
        if not self.python.is_file():
            raise FileNotFoundError("Local embedding runtime not installed")
        if self.revision is None:
            raise FileNotFoundError("Local model manifest unavailable; run setup_local_embedding.py")
        async with self.lock:
            try:
                if self.process is None or self.process.returncode is not None:
                    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                        str(self.python), str(Path(__file__).with_name("embedding_worker.py")),
                        "--cache", str(self.runtime_dir / "embedding-models"),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        limit=4 * 1024 * 1024,
                        env={**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}))
                    try:
                        self.process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        # If cancellation arrives during startup, take ownership
                        # of the child before the common cleanup below runs.
                        self.process = await spawning
                        raise
                process = self.process

                async def exchange():
                    process.stdin.write(json.dumps({"texts": texts, "kind": kind}, ensure_ascii=False).encode("utf-8") + b"\n")
                    await process.stdin.drain()
                    return await process.stdout.readline()

                stdout = await asyncio.wait_for(exchange(), timeout=timeout)
                if not stdout:
                    raise ValueError("Local embedding worker failed; check downloaded model/runtime")
                result = json.loads(stdout)
                if (result.get("model") != MODEL or result.get("dimension") != self.dimension
                        or result.get("revision") != self.revision):
                    raise ValueError("Unexpected embedding model")
                return result["vectors"]
            except BaseException:
                await self._close_process()  # encode already owns the lock.
                raise
