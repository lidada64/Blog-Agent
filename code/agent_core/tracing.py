"""Local metadata-only events. No prompts, tool arguments or hidden reasoning."""

from uuid import uuid4
import hashlib
import json

from .state import now

SCHEMA_VERSION = 1
EVENT_TYPES = frozenset({
    "session.start", "session.resume", "session.stop", "turn.start", "turn.end",
    "model.start", "model.end", "tool.start", "tool.end", "guard.allow",
    "guard.deny", "compact.end", "run.error", "routing", "pagination",
    "retrieval.progress", "retrieval.context", "budget", "finalization", "citation.audit",
})
FIELDS = frozenset({
    "status", "tool", "attempt", "error_type", "purpose", "elapsed_seconds",
    "input_tokens", "output_tokens", "cached_tokens", "mode", "before_bytes",
    "after_bytes", "step", "max_steps", "max_turns", "reason", "result_hash",
    "located", "invalid",
})


def result_hash(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def emit(state, event_type, *, span_id=None, parent_span_id=None,
         turn_id=None, tool_call_id=None, **metadata):
    if event_type not in EVENT_TYPES or set(metadata) - FIELDS:
        raise ValueError("Unknown trace event or metadata field")
    # Applications opt in by starting a trace; untraced checkpoints remain unchanged.
    if "trace" not in state and event_type != "session.start":
        return None
    trace = state.setdefault("trace", {
        "schema_version": SCHEMA_VERSION,
        "session_id": state.get("conversation_id") or str(uuid4()),
        "run_id": str(uuid4()), "events": [],
    })
    if trace.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported trace schema")
    event = {
        "sequence": len(trace["events"]) + 1, "event_id": str(uuid4()),
        "run_id": trace["run_id"], "session_id": trace["session_id"],
        "event_type": event_type, "observed_at": now(),
        "span_id": span_id, "parent_span_id": parent_span_id,
        "turn_id": state.get("turn_count") if turn_id is None else turn_id,
        "tool_call_id": tool_call_id, "metadata": metadata,
    }
    trace["events"].append(event)
    return event


def resume(state):
    # A resume is a new execution run within the same durable session.
    if "trace" in state:
        state["trace"]["run_id"] = str(uuid4())
    emit(state, "session.resume", status=state.get("status"))


def interrupt_tools(state, reason):
    """Close observed running attempts; never invent an unobserved start/duration."""
    events = state.get("trace", {}).get("events", [])
    ended = {e["span_id"] for e in events if e["event_type"] == "tool.end"}
    for call in state.get("tool_calls", []):
        span = f"tool:{call['call_id']}:{call['attempts']}"
        if call.get("running") and span not in ended and any(e["span_id"] == span and e["event_type"] == "tool.start" for e in events):
            emit(state, "tool.end", span_id=span, tool_call_id=call["call_id"],
                 tool=call["name"], status="interrupted", reason=reason, elapsed_seconds=None)
