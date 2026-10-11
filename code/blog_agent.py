"""Local Blog Agent entry point; no Skill MCP server or dependency required."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from agent_core.conversation import interactive_loop
from agent_core.reasoning import EFFORTS
from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.retrieval import SamplePosts
from blog_assistant.tools import BlogTools

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POSTS = ROOT / "Blog"

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--mode", choices=("qa", "manage"), default="qa")
    parser.add_argument("--posts-dir", type=Path, default=DEFAULT_POSTS)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--compact-mode", choices=("none", "local", "summary", "standalone"))
    parser.add_argument("--compact-threshold", type=int, default=12000)
    parser.add_argument("--reasoning-effort", choices=EFFORTS)
    parser.add_argument("--markdown", choices=("rich", "plain", "auto", "glow"), default="rich",
                        help="Markdown renderer for reasoning, replies and SessionEnd (default: rich)")
    parser.add_argument("--render-session-end", action=argparse.BooleanOptionalAction, default=True,
                        help="Render the exported conversation on exit (default: enabled); export is always retained")
    parser.add_argument("--check-local", action="store_true", help="Read-only probe; no API, MCP or checkpoint writes")
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--resume", action="store_true")
    group.add_argument("--new", action="store_true")
    args=parser.parse_args()
    if min(args.max_steps, args.max_turns, args.compact_threshold) < 1:
        parser.error("Step, turn and compaction limits must be positive")
    if args.resume and args.prompt:
        parser.error("--resume continues the saved turn; omit prompt")
    try:
        if args.check_local:
            tools=BlogTools(SamplePosts(args.posts_dir), args.mode, {"history": []})
            print(json.dumps(await tools.execute("search_posts", {"query": "Agent", "limit": 5}), ensure_ascii=False, indent=2))
            return 0
        from dotenv import load_dotenv
        from openai import AsyncOpenAI
        from agent_core.presentation import MarkdownHooks
        load_dotenv(Path(__file__).parent / ".env")
        load_dotenv(ROOT / ".env")
        if not os.getenv("OPENAI_API_KEY"):
            parser.error("Set OPENAI_API_KEY and a Responses-compatible OPENAI_BASE_URL")
        path = args.state_file or ROOT / ".blog-agent-data" / f"{args.mode}.json"
        store = BlogStore(path, mode=args.mode, posts_dir=args.posts_dir, max_user_turns=args.max_turns)
        state = store.start(args.model or os.getenv("OPENAI_MODEL", "deepseek-flash"),
                            compact_threshold=args.compact_threshold, compact_mode=args.compact_mode,
                            reasoning_effort=args.reasoning_effort, resume=args.resume, new=args.new)
        async with AsyncOpenAI(max_retries=0) as client:
            hooks=MarkdownHooks(store.path, mode=args.markdown, render_session_end=args.render_session_end)
            conversation=create_conversation(client, state, store, max_steps=args.max_steps, on_reasoning=hooks.on_reasoning)
            return await interactive_loop(conversation, args.prompt, hooks=hooks)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

if __name__ == "__main__":
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted; use --resume to continue the saved turn.")
        raise SystemExit(130)
