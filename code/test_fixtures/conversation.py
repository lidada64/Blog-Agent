"""Data and injected tools for tests of the real shared conversation runtime."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from openai.types.responses import ResponseFunctionToolCall

INSTRUCTIONS = "Answer questions using the supplied tools."
DOCUMENT = "Fixture document: Python testing with pytest."
TOOLS = [{"type": "function", "name": "lookup", "description": "Read a test record", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]

TOOLS.append({"type": "function", "name": "read_conversation_history", "description": "Read an exact archived item", "parameters": {"type": "object", "properties": {"start": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["start", "limit"]}})

def answer(text, *, status="completed"):
    return SimpleNamespace(status=status, output_text=text, output=[{
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }])

def tool_response(*calls):
    return SimpleNamespace(status="completed", output_text="", output=list(calls))

def call(name, arguments, call_id):
    return ResponseFunctionToolCall(type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id)

def model_with(*responses):
    return SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=list(responses)), compact=AsyncMock()))

async def execute_tool(name, arguments):
    if name != "lookup":
        raise ValueError("Unknown fixture tool")
    return {"ok": True, "data": {"content": DOCUMENT, "query": arguments.get("query", "")}}


def executor_for(state):
    """Expose the real archive reader as an application-provided test tool."""
    from agent_core.context import read_history
    async def execute(name, arguments):
        if name == "read_conversation_history":
            return read_history(state, **arguments)
        return await execute_tool(name, arguments)
    return execute
