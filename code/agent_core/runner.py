from .errors import PersistenceError, PolicyDenied, tool_error
import copy
import json
import logging
from time import perf_counter

from .state import set_tool_status
from .tracing import emit, result_hash

logger = logging.getLogger(__name__)


def json_item(item):
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    return vars(item).copy()


async def finish_pending(state, store, *, tool_executor):
    """Persist tool transitions and outputs; application executors own authorization."""
    if not any(not call["completed"] for call in state["tool_calls"]) and not any(
        step["status"] == "tools_pending" for step in state["steps"]
    ):
        return
    for call in state["tool_calls"]:
        if call["completed"]:
            continue
        call["attempts"] += 1
        set_tool_status(call, "running")
        state["status"] = "running"
        state["phase"] = "tools"
        span = f"tool:{call['call_id']}:{call['attempts']}"
        turn_number = next((step.get("turn") for step in state["steps"] if step["number"] == call["step"]), state.get("turn_count", 0))
        emit(state, "tool.start", span_id=span, parent_span_id=f"turn:{turn_number}",
             turn_id=turn_number, tool_call_id=call["call_id"], tool=call["name"],
             attempt=call["attempts"], status="running", step=call["step"])
        store.save(state)
        logger.info("Tool started: step=%s tool=%s call_id=%s", call["step"], call["name"], call["call_id"])
        started = perf_counter()
        try:
            arguments = json.loads(call["arguments"])
            if not isinstance(arguments, dict):
                raise TypeError("Tool arguments must be a JSON object")
            result = await tool_executor(call["name"], arguments)
        except PersistenceError:
            raise
        except Exception as exc:
            if isinstance(exc, (ValueError, PolicyDenied)) and not any(
                e["event_type"] == "guard.deny" and e["span_id"] == span
                for e in state.get("trace", {}).get("events", [])
            ):
                emit(state, "guard.deny", span_id=span, tool_call_id=call["call_id"],
                     tool=call["name"], status="denied", error_type=tool_error(exc)['type'], reason="execution_validation")
            logger.exception("Tool error: tool=%s", call["name"])
            result = {"ok": False, "error": tool_error(exc)}
        finally:
            elapsed = perf_counter() - started
            call.setdefault("attempt_seconds", []).append(elapsed)
            call["elapsed_seconds"] = call.get("elapsed_seconds", 0) + elapsed
        call["result"] = result
        set_tool_status(call, "completed" if result["ok"] else "failed")
        emit(state, "tool.end", span_id=span, turn_id=turn_number,
             tool_call_id=call["call_id"], tool=call["name"], status=call["status"],
             elapsed_seconds=elapsed, error_type=result.get("error", {}).get("type"), result_hash=result_hash(result))
        # Result and function output are committed together.
        output = {
            "type": "function_call_output", "call_id": call["call_id"],
            "output": json.dumps(result, ensure_ascii=False),
        }
        state["history"].append(output)
        if "context" in state:
            state["context"].append(copy.deepcopy(output))
        store.save(state)
        logger.info("Tool finished: tool=%s ok=%s", call["name"], result["ok"])
    for step in state["steps"]:
        if step["status"] == "tools_pending":
            step["status"] = "completed"
    state["phase"] = "review"
    store.save(state)
