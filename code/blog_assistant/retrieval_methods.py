"""Offline EXP-2 chunk and ranking contracts; no embedding network access."""

import math
import re
from collections import Counter

from .experiment import digest, evidence
from .reading_experiment import as_evidence, units


def estimated_tokens(text):
    """A sampling coordinate, never a provider token or billing count."""
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def terms(text):
    result = []
    for part in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.casefold()):
        if "\u3400" <= part[0] <= "\u9fff":
            result.extend(part[index:index + 2] for index in range(len(part) - 1))
            if len(part) == 1:
                result.append(part)
        elif len(part) > 1:
            result.append(part)
    return result


def chunks_for_post(post, article_id, target_tokens=300):
    if not 1 <= target_tokens <= 2000:
        raise ValueError("Invalid target token estimate")
    blocks = units(post["body"], "paragraph")
    chunks, group, section = [], [], post["title"]

    def flush():
        nonlocal group
        if not group:
            return
        start, end = group[0][0], group[-1][1]
        text = "\n".join(item[2] for item in group)
        item = as_evidence(post, article_id, (start, end, text, section), "paragraph", False)
        item["estimated_tokens"] = estimated_tokens(text)
        item["oversized_block"] = item["estimated_tokens"] > target_tokens
        chunks.append(item)
        group = []

    for block in blocks:
        heading = next((line.lstrip("# ").strip() for line in block[2].splitlines()
                        if line.startswith("# ")), None)
        if heading and group:
            flush()
        if heading:
            section = heading
        candidate = "\n".join(item[2] for item in group + [block])
        if group and estimated_tokens(candidate) > target_tokens:
            flush()
        group.append(block)
        if estimated_tokens(block[2]) > target_tokens:
            flush()  # Preserve an indivisible code/table/paragraph block.
    flush()
    return chunks


def build_chunks(posts, target_tokens=300):
    items, issues, catalog_version = posts.catalog()
    chunks = []
    for meta in items:
        post = posts.snapshot(meta["post_id"])
        if post["version"] != meta["version"] or post["visibility"] != "local_article":
            raise ValueError("Article changed or became invisible during indexing")
        chunks.extend(chunks_for_post(post, meta["post_id"], target_tokens))
    if posts.catalog()[2] != catalog_version:
        raise ValueError("Catalog changed during indexing")
    return {"chunks": chunks, "issues": issues, "catalog_version": catalog_version,
            "index_version": digest([[chunk["chunk_id"], chunk["version"], chunk["visibility"]]
                                     for chunk in chunks] + [target_tokens]),
            "token_method": "estimated from UTF-8 bytes/4; not provider usage"}


def rank_bm25(chunks, query, top_k=5, per_article=2):
    if not 1 <= top_k <= 20 or not 1 <= per_article <= top_k:
        raise ValueError("Invalid ranking limits")
    query_terms = set(terms(query))
    docs = [Counter(terms(chunk["title"] + " " + (chunk["section"] or "") + " " + chunk["text"]))
            for chunk in chunks]
    lengths = [sum(doc.values()) for doc in docs]
    mean_length = sum(lengths) / len(lengths) if lengths else 1
    frequency = Counter(term for doc in docs for term in doc)
    ranked = []
    for chunk, doc, length in zip(chunks, docs, lengths):
        score = 0.0
        for term in query_terms:
            tf = doc[term]
            if not tf:
                continue
            idf = math.log1p((len(chunks) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / mean_length))
        if score > 0:
            ranked.append((score, chunk))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["article_id"], pair[1]["start_line"]))
    selected, counts = [], Counter()
    for score, chunk in ranked:
        if counts[chunk["article_id"]] < per_article:
            selected.append({"score": score, "chunk": chunk})
            counts[chunk["article_id"]] += 1
        if len(selected) == top_k:
            break
    return selected


def rank_historical(posts, query, top_k=5):
    result = posts.search(query, top_k)
    delivered = [evidence(posts, match["post_id"], 1, 80) for match in result["matches"]]
    return {"evidence": delivered, "issues": result["issues"]}


class OfflineVectorIndex:
    """Accept caller-supplied vectors for fault tests; not a semantic model."""

    def __init__(self, model_id, dimension):
        if not model_id or dimension < 1:
            raise ValueError("Invalid mock vector model")
        self.model_id, self.dimension = model_id, dimension
        self.records = {}
        self.index_version = None

    def update(self, built, encode):
        current = {}
        encoded = 0
        for chunk in built["chunks"]:
            if chunk["visibility"] != "local_article":
                raise PermissionError("Vector index requires public local evidence")
            key = chunk["chunk_id"]
            prior = self.records.get(key)
            if prior and prior["version"] == chunk["version"]:
                current[key] = prior
                continue
            vector = tuple(float(value) for value in encode(chunk["text"]))
            if len(vector) != self.dimension or not all(math.isfinite(value) for value in vector):
                raise ValueError("Invalid vector fixture")
            current[key] = {"vector": vector, "chunk": chunk, "version": chunk["version"]}
            encoded += 1
        removed = len(set(self.records) - set(current))
        self.records, self.index_version = current, built["index_version"]
        return {"encoded": encoded, "removed": removed, "reused": len(current) - encoded}

    def rank(self, query_vector, *, index_version, top_k=5):
        if self.index_version != index_version:
            raise ValueError("Stale vector index")
        query = tuple(float(value) for value in query_vector)
        if len(query) != self.dimension or not all(math.isfinite(value) for value in query):
            raise ValueError("Invalid query vector")
        norm = math.sqrt(sum(value * value for value in query))
        if not norm:
            return []
        ranked = []
        for record in self.records.values():
            vector = record["vector"]
            denominator = norm * math.sqrt(sum(value * value for value in vector))
            if denominator:
                ranked.append((sum(a * b for a, b in zip(query, vector)) / denominator, record["chunk"]))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
        return [{"score": score, "chunk": chunk} for score, chunk in ranked[:top_k]]


def reciprocal_rank_fusion(keyword, vector, top_k=5, constant=60):
    if constant < 1:
        raise ValueError("Invalid fusion constant")
    by_id = {}
    for rows in (keyword, vector):
        for rank, row in enumerate(rows, 1):
            chunk = row["chunk"]
            slot = by_id.setdefault(chunk["chunk_id"], {"score": 0.0, "chunk": chunk})
            slot["score"] += 1 / (constant + rank)
    return sorted(by_id.values(), key=lambda row: (-row["score"], row["chunk"]["chunk_id"]))[:top_k]
