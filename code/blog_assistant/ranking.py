"""Production lexical ranking shared by hybrid retrieval and offline comparisons."""
import math
import re
from collections import Counter


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

