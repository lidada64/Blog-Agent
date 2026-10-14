"""Production local semantic/BM25 retrieval with restart-safe vector caching."""
import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from agent_core.tracing import emit
from .retrieval_methods import rank_bm25
from .sections import CHUNK_VERSION, chunks


def valid_vector(vector, dimension):
    if (not isinstance(vector, (list, tuple)) or len(vector) != dimension
            or any(type(v) not in {int, float} or not math.isfinite(v) for v in vector)
            or not any(vector)):
        raise ValueError("Invalid embedding vector")
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid embedding norm")
    return [v / norm for v in vector]


class SemanticRetriever:
    def __init__(self, posts, encoder, cache_file, *, mode="qa", store=None, min_similarity=0.35,
                 max_chunks=2048, diagnostics=False):
        if mode not in {"qa", "manage"} or not -1 <= min_similarity <= 1 or max_chunks < 1:
            raise ValueError("Invalid retrieval configuration")
        self.posts, self.encoder, self.store = posts, encoder, store
        self.min_similarity, self.max_chunks = min_similarity, max_chunks
        self.diagnostics = diagnostics
        self.path = Path(cache_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = hashlib.sha256(json.dumps([str(posts.directory), mode, encoder.model_id,
                                               encoder.dimension, CHUNK_VERSION], ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS vectors (scope TEXT, key TEXT, kind TEXT, vector TEXT, PRIMARY KEY(scope,key))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, state):
        if self.store:
            self.store.save(state)

    def allowance(self, state):
        turns = state.get("turns", [])
        if not turns or turns[-1].get("kind") != "user":
            return None
        return turns[-1].setdefault("embedding", {"version": 1, "model": self.encoder.model_id,
            "batches": 0, "texts": 0, "max_batches": 40, "max_texts": 2112,
            "external_cost": 0, "provider_tokens": None})

    async def encode(self, texts, kind, state):
        budget = self.allowance(state)
        timeout = 30.0
        if state.get("turns"):
            retrieval = state["turns"][-1].get("retrieval", {})
            if "deadline" in retrieval:
                timeout = min(timeout, retrieval["deadline"] - time.time() - 15)
        if timeout <= 0:
            raise TimeoutError("Embedding time allowance exhausted")
        if budget is not None:
            if (budget["model"] != self.encoder.model_id or budget["batches"] >= budget["max_batches"]
                    or budget["texts"] + len(texts) > budget["max_texts"]):
                raise ValueError("Saved embedding allowance exhausted or model changed")
            budget["batches"] += 1
            budget["texts"] += len(texts)
            self.save(state)  # Reserve before CPU work; cancellation/resume grants no extra allowance.
        started = time.perf_counter()
        try:
            vectors = await self.encoder.encode(texts, kind=kind, timeout=timeout)
            if len(vectors) != len(texts):
                raise ValueError("Embedding result count mismatch")
            return [valid_vector(v, self.encoder.dimension) for v in vectors]
        finally:
            state.setdefault("embedding_requests", []).append({"kind": kind, "texts": len(texts),
                "model": self.encoder.model_id, "elapsed_seconds": time.perf_counter() - started,
                "external_cost": 0, "provider_tokens": None})
            emit(state, "retrieval.progress", mode=kind, purpose="embedding", status="local",
                 elapsed_seconds=time.perf_counter() - started)
            self.save(state)

    async def search(self, query, limit, state):
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("Invalid search arguments")
        metas, issues, catalog_version = self.posts.catalog()
        built = []
        for meta in metas:
            post = self.posts.snapshot(meta["post_id"])
            if post["version"] != meta["version"] or post["visibility"] != "local_article":
                raise ValueError("Source changed during retrieval")
            rows = chunks(post)
            if any(len(line) > 6000 for line in post["body"].splitlines()):
                issues.append({"post_id": post["post_id"], "error_type": "LineExceedsReadBudget"})
            if len(built) + len(rows) > self.max_chunks:
                raise ValueError("Chunk index exceeds configured bound")
            built.extend({**row, "article_id": row["post_id"]} for row in rows)
        if self.posts.catalog()[2] != catalog_version:
            raise ValueError("Catalog changed during chunking")
        stopwords = {"how", "to", "the", "and", "or", "of", "in", "a", "an", "is", "are", "do", "does", "what", "why", "can", "with"}
        keyword_query = " ".join(word for word in query.split() if word.casefold() not in stopwords)
        keyword = rank_bm25(built, keyword_query, top_k=20, per_article=4)
        vectors, fallback, vector_rows = {}, None, []
        current_ids = {c["chunk_id"] for c in built}
        with self.connect() as db:
            cached = db.execute("SELECT key, vector FROM vectors WHERE scope=? AND kind='passage'", (self.scope,)).fetchall()
            for key, raw in cached:
                if key not in current_ids:
                    db.execute("DELETE FROM vectors WHERE scope=? AND key=?", (self.scope, key))
                else:
                    try:
                        vectors[key] = valid_vector(json.loads(raw), self.encoder.dimension)
                    except (ValueError, TypeError):
                        db.execute("DELETE FROM vectors WHERE scope=? AND key=?", (self.scope, key))
        try:
            missing = [c for c in built if c["chunk_id"] not in vectors]
            for offset in range(0, len(missing), 64):
                batch = missing[offset:offset + 64]
                encoded = await self.encode([c["title"][:40] + "\n" + (c["section"] or "")[:40] + "\n" + c["text"] for c in batch], "passage", state)
                if self.posts.catalog()[2] != catalog_version:
                    raise ValueError("Catalog changed during embedding")
                with self.connect() as db:
                    for chunk, vector in zip(batch, encoded):
                        vectors[chunk["chunk_id"]] = vector
                        db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)", (
                            self.scope, chunk["chunk_id"], "passage", json.dumps(vector)))
            if built:
                key = "query:" + hashlib.sha256(query.encode()).hexdigest()
                with self.connect() as db:
                    cached_query = db.execute("SELECT vector FROM vectors WHERE scope=? AND key=?", (self.scope, key)).fetchone()
                try:
                    qvector = valid_vector(json.loads(cached_query[0]), self.encoder.dimension) if cached_query else None
                except (ValueError, TypeError):
                    qvector = None
                if qvector is None:
                    qvector = (await self.encode([query], "query", state))[0]
                    with self.connect() as db:
                        db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)", (self.scope, key, "query", json.dumps(qvector)))
                        db.execute("DELETE FROM vectors WHERE scope=? AND kind='query' AND rowid NOT IN (SELECT rowid FROM vectors WHERE scope=? AND kind='query' ORDER BY rowid DESC LIMIT 128)", (self.scope, self.scope))
                for chunk in built:
                    score = sum(a * b for a, b in zip(qvector, vectors[chunk["chunk_id"]]))
                    if score >= self.min_similarity:
                        vector_rows.append({"score": score, "chunk": chunk})
                vector_rows.sort(key=lambda row: (-row["score"], row["chunk"]["chunk_id"]))
        except (OSError, ValueError, TimeoutError) as exc:
            fallback = type(exc).__name__
            emit(state, "retrieval.progress", purpose="embedding_fallback", status="keyword", error_type=fallback)
        if self.posts.catalog()[2] != catalog_version:
            raise ValueError("Catalog changed; stale results withheld")
        def article_ranks(rows):
            used, result = set(), []
            for row in rows:
                if row["chunk"]["post_id"] not in used:
                    used.add(row["chunk"]["post_id"])
                    result.append(row)
            return result

        # Multiple chunks from one article must not consume source ranking slots.
        fusion = {}
        for rows in (article_ranks(keyword), article_ranks(vector_rows)[:20]):
            for rank, row in enumerate(rows, 1):
                identifier = row["chunk"]["post_id"]
                slot = fusion.setdefault(identifier, {"score": 0, "chunk": row["chunk"]})
                slot["score"] += 1 / (60 + rank)
        ranked = sorted(fusion.values(), key=lambda row: (-row["score"], row["chunk"]["post_id"]))
        matches, used = [], set()
        for row in ranked:
            chunk = row["chunk"]
            if chunk["post_id"] in used:
                continue
            used.add(chunk["post_id"])
            matches.append({k: chunk[k] for k in ("post_id", "title", "section", "source", "version", "visibility", "start_line", "end_line")}
                           | {"snippet": chunk["text"][:500], "snippet_truncated": len(chunk["text"]) > 500,
                              "score": row["score"], "candidate_only": True})
            if len(matches) >= limit:
                break
        result = {"matches": matches, "issues": issues[:5], "issues_count": len(issues),
                "retrieval": "keyword_fallback" if fallback else "hybrid", "fallback_reason": fallback,
                "model": self.encoder.model_id, "index_version": catalog_version,
                "embedding_external_cost": 0, "ambiguous": len(matches) > 1}
        if self.diagnostics:
            result["ranking_diagnostics"] = {"keyword": [{"post_id": r["chunk"]["post_id"], "score": r["score"]} for r in keyword],
                                             "semantic": [{"post_id": r["chunk"]["post_id"], "score": r["score"]} for r in vector_rows[:20]]}
        return result
