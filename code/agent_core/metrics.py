"""Provider-neutral request metrics; missing usage stays unknown, never zero."""

import hashlib
import json
from time import perf_counter

from .state import now
from .tracing import emit


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def count(value):
    return value if type(value) is int and value >= 0 else None


def usage_metrics(response):
    usage = field(response, "usage")
    input_tokens = count(field(usage, "input_tokens", field(usage, "prompt_tokens")))
    output_tokens = count(field(usage, "output_tokens", field(usage, "completion_tokens")))
    details = field(usage, "input_tokens_details", field(usage, "prompt_tokens_details"))
    cached = count(field(details, "cached_tokens"))
    if cached is None:
        cached = count(field(usage, "prompt_cache_hit_tokens"))
    if input_tokens is not None and cached is not None and cached > input_tokens:
        cached = None  # Invalid provider counters must not produce >100% hit rates.
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "reasoning_tokens": count(field(field(usage, "output_tokens_details"), "reasoning_tokens")),
            "cached_tokens": cached,
            "cache_hit_ratio": cached / input_tokens if input_tokens and cached is not None else None}


async def measured_request(client, state, *, purpose, operation="create", **kwargs):
    """Keep every attempt, including retries and summary/compact API requests."""
    stable = {key: kwargs.get(key) for key in ("model", "instructions", "tools", "reasoning")}
    prefix_hash = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    record = {"number": len(state.setdefault("model_requests", [])) + 1,
              "created_at": now(), "purpose": purpose, "operation": operation,
              "turn": state.get("turn_count"), "prefix_hash": prefix_hash,
              "input_bytes": len(json.dumps(kwargs.get("input", []), ensure_ascii=False).encode()),
              "compaction_count": len(state.get("compactions", [])),
              "status": "requesting", "input_tokens": None, "output_tokens": None,
              "cached_tokens": None, "cache_hit_ratio": None,
              "max_output_tokens": count(kwargs.get("max_output_tokens")),
              "reasoning_effort": field(kwargs.get("reasoning"), "effort"),
              "incomplete_reason": None, "reasoning_tokens": None}
    state["model_requests"].append(record)
    span = f"model:{record['number']}"
    emit(state, "model.start", span_id=span, parent_span_id=f"turn:{state.get('turn_count', 0)}",
         purpose=purpose, status="requesting")
    started = perf_counter()
    try:
        response = await getattr(client.responses, operation)(**kwargs)
        record.update(usage_metrics(response))
        record.update(status=field(response, "status", "completed"), response_id=field(response, "id"))
        reason = field(field(response, "incomplete_details"), "reason")
        record["incomplete_reason"] = reason if isinstance(reason, str) else None
        return response
    except BaseException as exc:
        record.update(status="error" if isinstance(exc, Exception) else "interrupted",
                      error_type=type(exc).__name__)
        raise
    finally:
        record["elapsed_seconds"] = round(perf_counter() - started, 6)
        emit(state, "model.end", span_id=span, purpose=purpose, status=record["status"],
             elapsed_seconds=record["elapsed_seconds"], error_type=record.get("error_type"),
             input_tokens=record["input_tokens"], output_tokens=record["output_tokens"],
             cached_tokens=record["cached_tokens"], reason=record["incomplete_reason"])


def summarize_metrics(state, *, group=True):
    requests = state.get("model_requests", [])
    observed = [r for r in requests if r.get("input_tokens") is not None and r.get("cached_tokens") is not None]
    denominator = sum(r["input_tokens"] for r in observed)
    cached = sum(r["cached_tokens"] for r in observed)
    return {"requests": len(requests), "errors": sum(r.get("status") == "error" for r in requests),
            "elapsed_seconds": round(sum(r.get("elapsed_seconds", 0) for r in requests), 6),
            "input_tokens": sum(r["input_tokens"] for r in requests if r.get("input_tokens") is not None),
            "output_tokens": sum(r["output_tokens"] for r in requests if r.get("output_tokens") is not None),
            "cached_tokens": cached, "cache_observed_requests": len(observed),
            "usage_observed_requests": sum(r.get("input_tokens") is not None for r in requests),
            "cache_hit_ratio": cached / denominator if denominator else None,
            "compactions": len(state.get("compactions", [])),
            "by_purpose": {purpose: summarize_metrics({"model_requests": [r for r in requests if r["purpose"] == purpose]}, group=False)
                           for purpose in sorted({r["purpose"] for r in requests})} if group else {}}
