"""Internal child-process entry; use blog_memory.py init/status/stop/resume."""
import argparse
from pathlib import Path

from blog_assistant.article_memory_worker import run_worker


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--library-id', required=True)
    parser.add_argument('--token', type=int, required=True)
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    run_worker(args.database, args.library_id, args.token, args.interval, args.log)
