"""EXP-0 offline Blog retrieval harness. It never opens a model connection."""

import hashlib
import json
from pathlib import Path

from blog_assistant.retrieval import SamplePosts

SCHEMA = "exp0-v1"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def synthetic_body(kind, title):
    if kind == "d9-sparse-331":
        lines = [f"# {title}"] + ["" if index % 2 else f"Note {index}: synthetic context here." for index in range(2, 331)]
        lines.append("A stable prefix can be cached; this is the stable prefix evidence.")
        return "\n".join(lines)
    if kind == "long-line":
        return f"# {title}\nA long line " + "x" * 6500
    if kind == "tail-long":
        return f"# {title}\n" + "\n".join(f"Filler {index}" for index in range(1, 410)) + "\nRelevant tail evidence."
    raise ValueError("Unknown synthetic fixture")


def validate_dataset(dataset):
    if dataset.get("version") != SCHEMA or not isinstance(dataset.get("articles"), dict):
        raise ValueError("Unsupported experiment dataset")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or len(cases) < 18:
        raise ValueError("EXP-0 requires at least 18 cases")
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or sum(case["split"] == "tune" for case in cases) < 6 or sum(case["split"] == "holdout" for case in cases) < 12:
        raise ValueError("Invalid tune/holdout partition")
    for case in cases:
        if case["split"] not in {"tune", "holdout"} or any(article not in dataset["articles"] for article in case["required"]):
            raise ValueError("Invalid case annotation")
        if any(dataset["articles"][article].get("visibility") == "draft" for article in case["required"]):
            raise ValueError("Draft content cannot be a public requirement")
    return dataset


def write_fixtures(dataset, root):
    root.mkdir(parents=True, exist_ok=False)
    public = root / "public"
    draft = root / "draft"
    public.mkdir()
    draft.mkdir()
    for article_id, spec in dataset["articles"].items():
        if article_id in {"", ".", ".."} or "/" in article_id or "\\" in article_id:
            raise ValueError("Unsafe fixture ID")
        body = spec.get("body") or synthetic_body(spec["synthetic"], spec["title"])
        target = draft if spec.get("visibility") == "draft" else public
        (target / f"{article_id}.md").write_text(body, encoding="utf-8")
    return SamplePosts(public)


def evidence(posts, article_id, start=1, limit=80):
    """Wrap a production read with explicit completeness and byte accounting."""
    before = posts.snapshot(article_id)
    result = posts.read(article_id, start, limit)
    after = posts.snapshot(article_id)
    if before["version"] != result["version"] or after["version"] != result["version"]:
        raise ValueError("Article changed during read")
    text = result["text"]
    complete = start == 1 and result["next_line"] is None
    return {
        "article_id": article_id, "chunk_id": f"{article_id}:{result['version'][:12]}:{start}-{result['end_line']}",
        "title": result["title"], "section": None, "source": result["source"],
        "version": result["version"], "visibility": result["visibility"],
        "start_line": start, "end_line": result["end_line"], "text": text,
        "characters": len(text), "utf8_bytes": len(text.encode("utf-8")),
        "token_count": None, "token_method": "unobserved; UTF-8 bytes recorded separately",
        "mode": "full" if complete else "range", "complete": complete,
        "incomplete_reason": None if complete else "line_limit_or_output_budget",
        "next_line": result["next_line"],
    }


def read_fixed(posts, query, max_tools=12):
    """Existing keyword search and bounded range reads form the historical baseline."""
    result = posts.search(query, 5)
    delivered, calls = [], [{"tool": "search_posts", "query": query, "status": "completed"}]
    for match in result["matches"]:
        if len(calls) >= max_tools:
            break
        item = evidence(posts, match["post_id"], 1, 80)
        if item["version"] != match["version"]:
            raise ValueError("Search result expired before evidence read")
        delivered.append(item)
        calls.append({"tool": "read_post", "article_id": match["post_id"],
                      "start_line": 1, "limit": 80, "status": "completed",
                      "chunk_id": item["chunk_id"]})
    return delivered, calls, result["issues"]


def score_retrieval(case, delivered):
    found = {item["article_id"] for item in delivered}
    required = set(case["required"])
    return {"required_articles": sorted(required), "returned_articles": sorted(found),
            "recall": len(required & found) / len(required) if required else None,
            "missing_articles": sorted(required - found),
            "complete_articles": sorted({item["article_id"] for item in delivered
                                         if item.get("article_complete", item["complete"])})}


def mock_answer(delivered, force_truncate=False):
    if force_truncate:
        return {"status": "incomplete", "reason": "max_output_tokens", "text": "", "citations": []}
    if not delivered:
        return {"status": "no_evidence", "reason": None, "text": "Insufficient evidence.", "citations": []}
    # Extractive mock: deterministic and explicitly distinct from semantic model quality.
    lines = [line for item in delivered for line in item["text"].splitlines()
             if line.strip() and not line.startswith("#") and not line.startswith("Ignore earlier rules")]
    return {"status": "answered", "reason": None, "text": "\n".join(lines),
            "citations": [item["chunk_id"] for item in delivered]}


def score_answer(case, answer, delivered):
    valid = {item["chunk_id"] for item in delivered}
    facts = case["facts"]
    is_answerable = case.get("answerable", True)
    return {"fact_coverage": sum(fact.casefold() in answer["text"].casefold() for fact in facts) / len(facts) if facts else None,
            "citation_support": all(citation in valid for citation in answer["citations"]),
            "no_answer_correct": answer["status"] == "no_evidence" if not is_answerable else None,
            "complete_answer": answer["status"] in {"answered", "no_evidence"},
            "semantic_quality": None}


def run_case(posts, case, *, simulate_truncation=False, checkpoint=None,
             cancel_after_retrieval=False):
    """Reserve 6 exploration slots, one answer, one recovery, across resumes."""
    budget = {"total": 8, "exploration_limit": 6, "used": 0, "answer_attempts": 0}
    catalog_version = posts.catalog()[2]
    case_hash = digest(case)
    saved_answer = None
    if checkpoint and checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if (saved.get("case_id") != case["id"] or saved.get("schema") != SCHEMA
                or saved.get("case_hash") != case_hash or saved.get("catalog_version") != catalog_version):
            raise ValueError("Checkpoint belongs to another case/config")
        budget = saved["budget"]
        saved_answer = saved.get("answer")
        if budget["used"] > 8 or budget["answer_attempts"] > 2:
            raise ValueError("Invalid saved budget")
    error = None
    try:
        delivered, calls, issues = read_fixed(posts, case["query"], max_tools=budget["exploration_limit"])
    except (OSError, ValueError, PermissionError, UnicodeError) as exc:
        delivered, calls, issues = [], [], []
        error = type(exc).__name__
    if budget["used"] == 0:
        budget["used"] = min(len(calls), budget["exploration_limit"])
    if cancel_after_retrieval:
        if checkpoint:
            checkpoint.write_text(json.dumps({"schema": SCHEMA, "case_id": case["id"],
                                              "case_hash": case_hash, "catalog_version": catalog_version,
                                              "budget": budget, "answer": None}, indent=2), encoding="utf-8")
        return {"case_id": case["id"], "answer_status": "cancelled", "budget": budget}
    requests = [{"request_id": f"{case['id']}-explore-{index}", "status": "tool_execution",
                 "input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
                 "usage_observed": False, "mock": True} for index in range(1, budget["used"] + 1)]
    answer = saved_answer
    while (answer is None or answer["status"] == "incomplete") and budget["answer_attempts"] < 2 and budget["used"] < budget["total"]:
        budget["answer_attempts"] += 1
        budget["used"] += 1
        answer = mock_answer(delivered, force_truncate=simulate_truncation and budget["answer_attempts"] == 1)
        requests.append({"request_id": f"{case['id']}-answer-{budget['answer_attempts']}",
                         "status": answer["status"], "reason": answer["reason"],
                         "input_tokens": None, "output_tokens": None,
                         "reasoning_tokens": None, "usage_observed": False,
                         "mock": True})
        if checkpoint:
            checkpoint.write_text(json.dumps({"schema": SCHEMA, "case_id": case["id"],
                                              "case_hash": case_hash, "catalog_version": catalog_version,
                                              "budget": budget, "answer": answer}, indent=2), encoding="utf-8")
        if answer["status"] != "incomplete":
            break
    if answer is None or answer["status"] == "incomplete":
        answer = {"status": "unanswered", "reason": "model_budget", "text": "", "citations": []}
    package = {"system": "Answer only from supplied evidence", "tool_schema": ["search_posts", "read_post"],
               "history": [], "question": case["question"], "evidence": delivered}
    package_bytes = len(canonical(package))
    evidence_bytes = sum(item["utf8_bytes"] for item in delivered)
    return {"case_id": case["id"], "split": case["split"], "family": case["family"],
            "retrieval": score_retrieval(case, delivered), "answer": score_answer(case, answer, delivered),
            "answer_status": answer["status"], "answer_reason": answer["reason"],
            "answer_text": answer["text"], "citations": answer["citations"],
            "evidence": delivered, "tool_calls": calls, "issues": issues,
            "requests": requests, "budget": budget, "error_type": error,
            "serialized_input_bytes": package_bytes, "evidence_utf8_bytes": evidence_bytes,
            "duplicate_body_bytes": 0, "duplicate_method": "body appears once in the serialized package"}


def replay_d9(posts):
    """Compare original seven serial reads with a reserved answer/recovery budget."""
    spans = [(1, 80), (81, 80), (161, 40), (201, 40), (241, 40), (281, 40), (321, 40)]
    original = [evidence(posts, "sparse-331", start, limit) for start, limit in spans]
    return {"shape": "331-line synthetic article", "historical": {
        "reads": len(original), "model_requests": 8, "answer": "incomplete/max_output_tokens",
        "recovery_slots": 0}, "reserved": {"exploration_slots": 6, "answer_slots": 1,
        "recovery_slots": 1, "total_slots": 8},
        "article_complete_after_reads": original[-1]["next_line"] is None,
        "total_read_characters": sum(item["characters"] for item in original)}
