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
from blog_assistant.convergence import RetrievalLimits

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POSTS = ROOT / "Blog"

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--mode", choices=("qa", "manage"), default="qa")
    parser.add_argument("--posts-dir", type=Path, default=DEFAULT_POSTS)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--retrieval", choices=("hybrid", "keyword"), default="hybrid",
                        help="Local CPU embeddings with BM25; keyword disables embeddings")
    parser.add_argument("--embedding-python", type=Path,
                        help="Python executable in the isolated FastEmbed environment")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--max-tool-calls", type=int, default=12)
    parser.add_argument("--max-searches", type=int, default=2)
    parser.add_argument("--max-file-searches", type=int, default=2)
    parser.add_argument("--max-text-searches", type=int, default=3)
    parser.add_argument("--no-progress-limit", type=int, default=2)
    parser.add_argument("--turn-timeout", type=int, default=120)
    parser.add_argument("--context-bytes", type=int, default=32000,
                        help="Serialized UTF-8 input budget, not a model token limit")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--compact-mode", choices=("none", "local", "summary", "standalone"))
    parser.add_argument("--compact-threshold", type=int, default=12000)
    parser.add_argument("--reasoning-effort", choices=EFFORTS)
    parser.add_argument("--markdown", choices=("rich", "plain", "auto", "glow"), default="rich",
                        help="Markdown renderer for reasoning, replies and SessionEnd (default: rich)")
    parser.add_argument("--render-session-end", action=argparse.BooleanOptionalAction, default=True,
                        help="Render the exported conversation on exit (default: enabled); export is always retained")
    parser.add_argument("--check-local", action="store_true", help="Read-only probe; no API, MCP or checkpoint writes")
    parser.add_argument("--upgrade-prompt", action="store_true",
                        help="Explicitly upgrade a saved prompt at a turn boundary; no model request")
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--resume", action="store_true")
    group.add_argument("--new", action="store_true")
    args=parser.parse_args()
    if min(args.max_steps, args.max_turns, args.compact_threshold, args.max_tool_calls,
           args.max_searches, args.max_file_searches, args.max_text_searches, args.no_progress_limit,
           args.turn_timeout, args.context_bytes, args.max_output_tokens) < 1:
        parser.error("Step, turn and compaction limits must be positive")
    if args.resume and args.prompt:
        parser.error("--resume continues the saved turn; omit prompt")
    if args.upgrade_prompt and (args.resume or args.new or args.prompt or args.check_local):
        parser.error("--upgrade-prompt is a standalone checkpoint operation")
    try:
        if args.upgrade_prompt:
            path = args.state_file or ROOT / ".blog-agent-data" / f"{args.mode}.json"
            store = BlogStore(path, mode=args.mode, posts_dir=args.posts_dir)
            state = store.load()
            if state is None:
                raise ValueError("No saved conversation to upgrade")
            store.upgrade_prompt(state)
            print("Prompt upgraded; history, mode, directory and budgets retained.")
            return 0
        if args.check_local:
            # This probe remains read-only and never loads/downloads embeddings.
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
        if not store.prompt_status(state)["current"]:
            print("Saved prompt differs from current guidance; retained. Use --upgrade-prompt after finishing the turn.")
        async with AsyncOpenAI(max_retries=0) as client:
            hooks=MarkdownHooks(store.path, mode=args.markdown, render_session_end=args.render_session_end)
            limits = RetrievalLimits(model_requests=args.max_steps, tool_attempts=args.max_tool_calls,
                                     searches=args.max_searches, file_searches=args.max_file_searches,
                                     text_searches=args.max_text_searches, no_progress=args.no_progress_limit,
                                     elapsed_seconds=args.turn_timeout, context_bytes=args.context_bytes,
                                     output_tokens=args.max_output_tokens)
            retriever = None
            if args.retrieval == "hybrid" and store.prompt_status(state)["current"]:
                from blog_assistant.local_embedding import LocalEmbedding
                from blog_assistant.semantic_retrieval import SemanticRetriever
                encoder = LocalEmbedding(ROOT / ".blog-agent-data", python=args.embedding_python)
                retriever = SemanticRetriever(SamplePosts(store.posts_dir), encoder,
                    ROOT / ".blog-agent-data" / "retrieval.sqlite3", mode=store.mode, store=store)
            conversation=create_conversation(client, state, store, max_steps=args.max_steps,
                                             on_reasoning=hooks.on_reasoning, retrieval_limits=limits, retriever=retriever)
            try:
                return await interactive_loop(conversation, args.prompt, hooks=hooks)
            finally:
                if retriever is not None:
                    await encoder.aclose()
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
