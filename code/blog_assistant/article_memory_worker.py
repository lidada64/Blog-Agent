"""Detached single-lease maintenance worker with explicit pause/resume."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .article_memory_store import ArticleMemoryStore
from .article_memory_sync import synchronize
from .article_memory_runtime import LeaseLost
from .article_memory_engine import build_next

DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / '.blog-agent-data' / 'article-memory.sqlite3'


def ensure_worker(database, library_id, *, interval=30, startup_timeout=8):
    if not 0.1 <= interval <= 3600:
        raise ValueError('Scan interval must be 0.1..3600 seconds')
    database = Path(database).resolve()
    process = None
    with ArticleMemoryStore(database) as store:
        owner = str(uuid.uuid4())
        token = store.claim_worker(library_id, owner)
        if token is not None:
            log = database.parent / f'memory-worker-{library_id[:16]}.log'
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'blog_memory_worker.py'),
                       '--database', str(database), '--library-id', library_id,
                       '--token', str(token), '--interval', str(interval), '--log', str(log)]
            options = {'stdin': subprocess.DEVNULL, 'close_fds': True}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            else:
                options['start_new_session'] = True
            try:
                with log.open('ab') as stream:
                    process = subprocess.Popen(command, stdout=stream, stderr=stream, **options)
            except OSError:
                store.release_worker(library_id, token, 'spawn_failed')
                raise
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            worker = store.worker(library_id)
            if worker['paused'] or worker['state'] == 'running':
                return worker
            if process is not None and process.poll() is not None:
                store.release_worker(library_id, token, f'startup_exit_{process.returncode}')
                return store.worker(library_id)
            time.sleep(0.05)
        # Starting is truthful: do not report running before a child heartbeat.
        return store.worker(library_id)


def run_worker(database, library_id, token, interval, log_path):
    stop = threading.Event()
    logging.basicConfig(filename=log_path, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logger = logging.getLogger(__name__)
    generator = None

    def pulse():
        try:
            with ArticleMemoryStore(database) as heart:
                while not stop.wait(1):
                    heart.heartbeat(library_id, token, os.getpid())
        except Exception:
            stop.set()

    with ArticleMemoryStore(database) as store:
        try:
            store.heartbeat(library_id, token, os.getpid(), log_path=str(log_path))
            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            config = json.loads(store._library(library_id)['maintenance_json'])
            if config and os.getenv('BLOG_MEMORY_API_KEY'):
                from .article_memory_provider import ResponsesSummaryGenerator
                generator = ResponsesSummaryGenerator(config)
            logger.info('Worker started pid=%s token=%s', os.getpid(), token)
            next_scan = 0

            def poll_scan():
                nonlocal next_scan
                if time.monotonic() >= next_scan:
                    try:
                        synchronize(store, library_id, token=token)
                    except ValueError as exc:
                        if str(exc) != 'Stale or mismatched scan':
                            raise
                        # Concurrent explicit reconciliation won; retry next cycle.
                    next_scan = time.monotonic() + interval

            while not stop.is_set():
                store.require_worker(library_id, token)
                poll_scan()
                if not build_next(store, library_id, token, generator, on_progress=poll_scan):
                    stop.wait(min(0.5, max(0.05, next_scan - time.monotonic())))
            reason = 'lease_lost_or_stopped'
        except LeaseLost:
            reason = 'lease_lost_or_paused'
        except Exception as exc:
            reason = type(exc).__name__
            logger.error('Worker stopped: %s', reason)  # No source, keys or provider payloads.
        finally:
            stop.set()
            if 'thread' in locals():
                thread.join(timeout=3)
            if generator is not None:
                generator.close()
            store.release_worker(library_id, token, locals().get('reason', 'shutdown'))
            logger.info('Worker exit: %s', locals().get('reason', 'shutdown'))
