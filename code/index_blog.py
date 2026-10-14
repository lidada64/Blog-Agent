"""Prepare/reuse the selected local index without a generation API or checkpoint change."""
import argparse
import asyncio
import json
from pathlib import Path

from blog_assistant.local_embedding import LocalEmbedding
from blog_assistant.retrieval import SamplePosts
from blog_assistant.semantic_retrieval import SemanticRetriever

ROOT = Path(__file__).resolve().parents[1]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts-dir", type=Path, default=ROOT / "Blog")
    parser.add_argument("--query", default="系统构造设计")
    parser.add_argument("--mode", choices=("qa", "manage"), default="qa")
    args = parser.parse_args()
    encoder = LocalEmbedding(ROOT / ".blog-agent-data")
    retriever = SemanticRetriever(SamplePosts(args.posts_dir), encoder,
                                  ROOT / ".blog-agent-data/retrieval.sqlite3", mode=args.mode)
    state = {}
    try:
        result = await retriever.search(args.query, 3, state)
        print(json.dumps({"retrieval": result["retrieval"], "model": result["model"],
                          "fallback_reason": result["fallback_reason"], "issues_count": result["issues_count"],
                          "candidates": [{k: m[k] for k in ("post_id", "title", "section")} for m in result["matches"]],
                          "embedding_batches": len(state.get("embedding_requests", [])),
                          "external_cost": 0}, ensure_ascii=False, indent=2))
        return 0 if result["retrieval"] == "hybrid" else 1
    finally:
        await encoder.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
