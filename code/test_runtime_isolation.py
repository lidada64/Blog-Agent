"""Production imports stay independent of retired demos and offline experiments."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_core.conversation import Conversation
from agent_core.conversation_state import ConversationStore
from test_fixtures.conversation import call, model_with, tool_response, answer


class RuntimeIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_hybrid_retrieval_imports_without_experiment_or_demo_modules(self):
        script = """
import sys
for name in ('mcp', 'agent_core.config', 'agent_core.local_tools',
             'agent_core.mcp_tools', 'agent_core.skill_library',
             'blog_assistant.experiment', 'blog_assistant.retrieval_methods'):
    sys.modules[name] = None
from blog_assistant.session import BlogStore
from blog_assistant.semantic_retrieval import rank_bm25
assert rank_bm25([dict(title='agent', section='', text='agent',
                      article_id='a', start_line=1)], 'agent')[0]['chunk']['article_id'] == 'a'
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).parent,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_core_does_not_apply_blog_search_rules_to_injected_tools(self):
        # A different application may legitimately give this name other semantics.
        with TemporaryDirectory() as directory:
            store = ConversationStore(Path(directory) / "state.json")
            state = store.start("fixture")
            received = []

            async def execute(name, arguments):
                received.append((name, arguments))
                return {"ok": True, "data": "application result"}

            client = model_with(
                answer("hello"),
                tool_response(call("find_text", {"query": "example"}, "lookup")),
                answer("done"),
            )
            conversation = Conversation(
                client, state, store, tools=[], tool_executor=execute,
            )
            await conversation.introduce()
            await conversation.submit("lookup")
            self.assertEqual(received, [("find_text", {"query": "example"})])
            saved = store.load()
            self.assertEqual(saved["tool_calls"][0]["status"], "completed")
            self.assertNotIn("library_file", saved)
            outputs = [item for item in saved["history"]
                       if item.get("type") == "function_call_output"]
            self.assertTrue(json.loads(outputs[0]["output"])["ok"])
