"""EXP-1 offline reading modes over the selected synthetic knowledge root."""

import re

from blog_assistant.experiment import canonical, evidence

INPUT_LIMIT = 16000
OUTPUT_RESERVE = 3000
MAX_EVIDENCE = 5
STOPWORDS = frozenset({"the", "and", "for", "how", "what", "when", "does", "with", "from",
                       "note", "notes", "read", "find", "show", "explain", "compare", "can", "is", "a"})


def query_terms(question):
    return [term for term in re.findall(r"[\w-]+", question.casefold())
            if len(term) > 2 and term not in STOPWORDS]


def candidates(posts, query):
    result = posts.search(query, 5)
    return result["matches"], result["issues"]


def units(body, mode):
    """Split at headings or blank lines without splitting fenced code or table rows."""
    lines = body.splitlines()
    if mode == "full":
        return [(1, len(lines), body, None)]
    groups = []
    start = 1
    fence = False
    for index, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        boundary = (mode == "section" and line.startswith("#") and not fence and index > start
                    or mode == "paragraph" and not line.strip() and not fence and index > start)
        if boundary:
            segment = "\n".join(lines[start - 1:index - 1]).strip("\n")
            if segment.strip():
                groups.append((start, index - 1, segment, None))
            start = index + (1 if mode == "paragraph" else 0)
    if start <= len(lines):
        segment = "\n".join(lines[start - 1:]).strip("\n")
        if segment.strip():
            groups.append((start, len(lines), segment, None))
    return groups


def as_evidence(post, article_id, unit, mode, article_complete):
    start, end, text, section = unit
    return {"article_id": article_id,
            "chunk_id": f"{article_id}:{post['version'][:12]}:{start}-{end}",
            "title": post["title"], "section": section,
            "source": post["source"], "version": post["version"],
            "visibility": post["visibility"], "start_line": start, "end_line": end,
            "text": text, "characters": len(text), "utf8_bytes": len(text.encode("utf-8")),
            "token_count": None, "token_method": "unobserved; UTF-8 bytes recorded separately",
            "mode": mode, "complete": True, "article_complete": article_complete,
            "incomplete_reason": None, "next_line": None}


def available_bytes(question):
    overhead = len(canonical({"system": "Answer only from supplied evidence",
                              "tool_schema": ["search_posts", "read_post"],
                              "history": [], "question": question, "evidence": []}))
    return INPUT_LIMIT - OUTPUT_RESERVE - overhead - 64


def retrieve(posts, case, mode, fraction=0.75):
    """Return immutable evidence and explicit routing reasons; no answer labels are consulted."""
    if mode not in {"r0", "full", "section", "paragraph", "auto"}:
        raise ValueError("Unknown reading mode")
    if not 0 < fraction <= 1:
        raise ValueError("Invalid full-text threshold")
    matches, issues = candidates(posts, case["query"])
    remaining = available_bytes(case["question"])
    full_threshold = int(remaining * fraction)
    delivered, decisions, calls = [], [], [{"tool": "search_posts", "query": case["query"]}]
    terms = query_terms(case["question"])
    for match in matches:
        if len(delivered) >= MAX_EVIDENCE:
            decisions.append({"article_id": match["post_id"], "route": "skip", "reason": "evidence_count_limit"})
            continue
        article_id = match["post_id"]
        post = posts.snapshot(article_id)
        if post["version"] != match["version"]:
            decisions.append({"article_id": article_id, "route": "reject", "reason": "version_expired"})
            continue
        if post["visibility"] != "local_article":
            decisions.append({"article_id": article_id, "route": "reject", "reason": "visibility"})
            continue
        if mode == "r0":
            item = evidence(posts, article_id, 1, 80)
            item["article_complete"] = item["complete"]
            options = [item]
            chosen = "range"
        else:
            full = as_evidence(post, article_id, units(post["body"], "full")[0], "full", True)
            full_cost = len(canonical(full))
            if mode == "full" or mode == "auto" and full_cost <= min(full_threshold, remaining):
                options, chosen = [full], "full"
            else:
                shape = "section" if mode == "section" else "paragraph"
                available = [as_evidence(post, article_id, unit, shape, False)
                             for unit in units(post["body"], shape)]
                available.sort(key=lambda item: (-sum(term in item["text"].casefold() for term in terms), item["start_line"]))
                options, chosen = available, shape
        added = 0
        for item in options:
            cost = len(canonical(item))
            if cost > remaining:
                continue
            delivered.append(item)
            remaining -= cost
            added += 1
            calls.append({"tool": "read_post", "article_id": article_id,
                          "mode": chosen, "chunk_id": item["chunk_id"]})
            if chosen in {"full", "range"} or len(delivered) >= MAX_EVIDENCE:
                break
        decisions.append({"article_id": article_id, "route": chosen if added else "skip",
                          "reason": ("within_threshold" if chosen == "full" and added else
                                     "bounded_selection" if added else "input_limit"),
                          "full_bytes": full_cost if mode != "r0" else None,
                          "delivered_chunks": added})
    package = {"system": "Answer only from supplied evidence", "tool_schema": ["search_posts", "read_post"],
               "history": [], "question": case["question"], "evidence": delivered}
    size = len(canonical(package))
    if size + OUTPUT_RESERVE > INPUT_LIMIT:
        raise AssertionError("Serialized input exceeded configured limit")
    return {"evidence": delivered, "decisions": decisions, "calls": calls,
            "issues": issues, "serialized_input_bytes": size,
            "remaining_bytes": remaining, "input_limit": INPUT_LIMIT,
            "output_reserve_bytes": OUTPUT_RESERVE, "threshold_fraction": fraction}
