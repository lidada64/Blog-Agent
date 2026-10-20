"""Configure, inspect, pause and resume independent article memory maintenance."""
import argparse
import json
from pathlib import Path

from blog_assistant.article_memory_store import ArticleMemoryStore
from blog_assistant.article_memory_sync import synchronize
from blog_assistant.article_memory_worker import ensure_worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "scan", "status", "stop", "resume", "configure", "overview", "section", "usage"))
    parser.add_argument("--posts-dir", type=Path, required=True)
    parser.add_argument("--scope", required=True, help="Trusted local visibility namespace; not authentication")
    parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parents[1] /
                        ".blog-agent-data" / "article-memory.sqlite3")
    parser.add_argument('--config', type=Path, help='Explicit maintenance model and total budget JSON; no secrets')
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--no-worker', action='store_true', help='Diagnostic/offline operation only; suppress automatic launch')
    parser.add_argument('--post-id')
    parser.add_argument('--selector', help='Exact chapter title or version-bound id')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--foreground-state', type=Path, action='append', default=[])
    parser.add_argument('--prices', type=Path, help='Existing price_usage JSON format; profiles keyed by foreground model')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    with ArticleMemoryStore(args.database) as store:
        if args.command == 'usage':
            from blog_assistant.article_memory_usage import project_usage
            prices = json.loads(args.prices.read_text(encoding='utf-8')) if args.prices else None
            print(json.dumps(project_usage(store,args.foreground_state,prices),ensure_ascii=False,indent=2))
            return 0
        library_id = (store.register_library(args.posts_dir, args.scope) if args.command == "init"
                      else store.find_library(args.posts_dir, args.scope))
        if args.command in {"init", "scan"}:
            synchronize(store, library_id)
        if args.command == 'configure':
            if not args.config:
                parser.error('configure requires --config with an explicit model and total budgets')
            store.configure(library_id, json.loads(args.config.read_text(encoding='utf-8')))
            synchronize(store, library_id)
        if args.command == 'stop':
            store.pause(library_id)
        if args.command == 'resume':
            store.resume_worker(library_id)
        if args.command in {'init','configure','resume'} and not args.no_worker:
            ensure_worker(args.database, library_id, interval=args.interval)
        if args.command in {'overview','section'}:
            from blog_assistant.article_memory_read import ArticleMemoryReader, render_memory
            if not args.post_id or (args.command=='section' and not args.selector):
                parser.error('overview/section require --post-id; section also requires --selector')
            reader = ArticleMemoryReader(args.database,args.posts_dir,args.scope)
            print(render_memory(reader.read(args.post_id,selector=args.selector if args.command=='section' else None,offset=args.offset)))
            return 0
        status = store.status(library_id)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 2 if status["scan_state"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
