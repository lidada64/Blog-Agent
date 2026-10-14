"""Real local embeddings over frozen synthetic cases; no paid generation or user articles."""
import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from blog_assistant.local_embedding import LocalEmbedding
from blog_assistant.retrieval import SamplePosts
from blog_assistant.semantic_retrieval import SemanticRetriever
from blog_assistant.tools import BlogTools

ROOT = Path(__file__).resolve().parents[1]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "code/evals/semantic-holdout-v1.json")
    parser.add_argument("--min-similarity", type=float)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    similarity = args.min_similarity if args.min_similarity is not None else dataset["frozen_parameters"]["min_similarity"]
    encoder = LocalEmbedding(ROOT / ".blog-agent-data")
    rows = []
    with TemporaryDirectory() as temp:
        posts_dir = Path(temp) / "posts"
        posts_dir.mkdir()
        for identifier, body in dataset["articles"].items():
            (posts_dir / (identifier + ".md")).write_text(body, encoding="utf-8")
        posts = SamplePosts(posts_dir)
        retriever = SemanticRetriever(posts, encoder, Path(temp) / "vectors.sqlite", min_similarity=similarity, diagnostics=True)
        for case in dataset["cases"]:
            state = {"turns": [{"kind": "user"}]}
            tools = BlogTools(posts, "qa", state, enable_full=True, retriever=retriever)
            started = perf_counter()
            result = (await tools.execute("search_posts", {"query": case["query"], "limit": 3}))["data"]
            ids = [m["post_id"] for m in result["matches"]]
            baseline = [m["post_id"] for m in posts.search(case["query"], 3)["matches"]]
            required = case["required"]
            rank = next((i + 1 for i, identifier in enumerate(ids) if identifier in required), None)
            rows.append({"id": case["id"], "required": required, "retrieved": ids,
                         "literal_baseline": baseline,
                         "recall_at_3": len(set(ids) & set(required)) / len(required) if required else None,
                         "mrr": 1 / rank if rank else 0 if required else None,
                         "no_answer_candidates": len(ids) if not required else None,
                         "retrieval": result["retrieval"], "wall_seconds": perf_counter() - started,
                         "ranking": result["ranking_diagnostics"],
                         "embedding_requests": state.get("embedding_requests", [])})
            print(case["id"], result["retrieval"], ids, flush=True)
    await encoder.aclose()
    relevant = [r for r in rows if r["recall_at_3"] is not None]
    report = {"dataset": dataset["version"], "model": encoder.model_id,
              "parameters": {"top_k": 3, "min_similarity": similarity}, "cases": rows,
              "recall_at_3": sum(r["recall_at_3"] for r in relevant) / len(relevant),
              "mrr": sum(r["mrr"] for r in relevant) / len(relevant),
              "generation_quality": "not measured; no paid generation",
              "external_embedding_cost": 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("dataset", "recall_at_3", "mrr", "external_embedding_cost")}))
    return 0 if all(r["retrieval"] == "hybrid" and (r["recall_at_3"] == 1 if r["required"] else r["no_answer_candidates"] == 0) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
