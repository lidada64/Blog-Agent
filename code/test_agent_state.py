"""Durable transitions of the real conversation loop and atomic checkpoints."""
import asyncio
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from agent_core.conversation import Conversation
from agent_core.conversation_state import ConversationStore
from agent_core.state import JsonCheckpointStore
from test_fixtures.conversation import answer, call, model_with, tool_response, TOOLS, execute_tool

class PersistentAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "state.json"
        self.store = ConversationStore(self.path)
        self.state = self.store.start("test")
        self.executor = AsyncMock(side_effect=execute_tool)

    def conversation(self, model, state=None, **kwargs):
        return Conversation(model, state if state is not None else self.state, self.store,
                            tools=TOOLS, tool_executor=self.executor, **kwargs)

    async def begin(self, *responses, max_steps=8):
        model = model_with(answer("intro"), *responses)
        conversation = self.conversation(model, max_steps=max_steps)
        await conversation.introduce()
        return conversation, model

    async def test_injected_tool_is_available_from_first_user_request(self):
        conversation, model = await self.begin(tool_response(call("lookup", {"query": "x"}, "one")), answer("done"))
        self.assertEqual(await conversation.submit("find"), ("waiting_user", "done"))
        self.assertEqual(model.responses.create.call_args_list[1].kwargs["tools"], TOOLS)
        self.assertEqual(self.state["tool_calls"][0]["result"]["data"]["query"], "x")

    async def test_each_tool_transition_is_saved_before_next_request(self):
        conversation, model = await self.begin(tool_response(call("lookup", {}, "one")), answer("done"))
        snapshots = []
        save = JsonCheckpointStore.save
        def capture(store, state):
            save(store, state)
            snapshots.append(json.loads(store.path.read_text(encoding="utf-8")))
        with patch.object(ConversationStore, "save", capture):
            await conversation.submit("find")
        statuses = [c["status"] for s in snapshots for c in s["tool_calls"]]
        for status in ("pending", "running", "completed"):
            self.assertIn(status, statuses)
        self.assertEqual(model.responses.create.call_args.kwargs["input"][-1]["call_id"], "one")

    async def test_resume_after_step_limit_does_not_repeat_completed_tool(self):
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "one")), max_steps=1)
        self.assertEqual(await conversation.submit("find"), ("max_steps", ""))
        identifier = self.state["conversation_id"]
        resumed = self.conversation(model_with(answer("done")), self.store.load())
        self.assertEqual(await resumed.resume_turn(), ("waiting_user", "done"))
        self.assertEqual(resumed.state["conversation_id"], identifier)
        self.executor.assert_awaited_once()
        self.assertEqual(resumed.state["tool_calls"][0]["attempts"], 1)

    async def test_cancel_mid_batch_only_unfinished_tool_is_retried_before_model(self):
        entered = asyncio.Event()
        async def execute(name, arguments):
            if arguments.get("query") == "second":
                self.assertTrue(self.store.load()["tool_calls"][1]["running"])
                entered.set()
                await asyncio.Event().wait()
            return await execute_tool(name, arguments)
        self.executor.side_effect = execute
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "one"), call("lookup", {"query": "second"}, "two")))
        task = asyncio.create_task(conversation.submit("find"))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = self.store.load()
        self.assertEqual([c["status"] for c in saved["tool_calls"]], ["completed", "interrupted"])
        self.executor.side_effect = execute_tool
        model = model_with(answer("done"))
        await self.conversation(model, saved).resume_turn()
        self.assertEqual([c["attempts"] for c in saved["tool_calls"]], [1, 2])
        outputs = [i["call_id"] for i in model.responses.create.call_args.kwargs["input"] if i.get("type") == "function_call_output"]
        self.assertEqual(outputs, ["one", "two"])
        self.assertEqual(self.executor.await_count, 3)

    async def test_hard_exit_snapshot_with_running_tool_recovers(self):
        class ProcessKilled(BaseException):
            pass
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "one")))
        save = JsonCheckpointStore.save
        def kill(store, state):
            save(store, state)
            if any(c["running"] for c in state["tool_calls"]):
                raise ProcessKilled()
        with patch.object(ConversationStore, "save", kill):
            with self.assertRaises(ProcessKilled):
                await conversation.submit("find")
        saved = self.store.load()
        self.assertTrue(saved["tool_calls"][0]["running"])
        await self.conversation(model_with(answer("recovered")), saved).resume_turn()
        self.executor.assert_awaited_once()
        self.assertEqual(saved["tool_calls"][0]["attempts"], 2)

    async def test_model_failure_can_resume_with_saved_tool_outputs(self):
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "one")), RuntimeError("offline"))
        self.assertEqual(await conversation.submit("find"), ("model_error", ""))
        saved = self.store.load()
        self.assertEqual(saved["last_error"]["message"], "offline")
        model = model_with(answer("done"))
        await self.conversation(model, saved).resume_turn()
        self.executor.assert_awaited_once()
        self.assertEqual(model.responses.create.call_args.kwargs["input"][-1]["call_id"], "one")

    async def test_cancel_model_request_retains_previous_progress(self):
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "one")), asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit("find")
        saved = self.store.load()
        self.assertEqual(saved["steps"][-1]["status"], "interrupted")
        await self.conversation(model_with(answer("done")), saved).resume_turn()
        self.executor.assert_awaited_once()
        self.assertEqual(saved["turn_count"], 1)

    async def test_error_output_is_persisted_and_not_reexecuted_on_resume(self):
        self.executor.side_effect = ValueError("invalid arguments")
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "bad")), max_steps=1)
        await conversation.submit("find")
        saved = self.store.load()
        self.assertEqual(saved["tool_calls"][0]["status"], "failed")
        model = model_with(answer("tool failed"))
        await self.conversation(model, saved).resume_turn()
        self.executor.assert_awaited_once()
        self.assertFalse(json.loads(model.responses.create.call_args.kwargs["input"][-1]["output"])["ok"])

    async def test_completed_resume_returns_saved_answer_without_model_request(self):
        conversation, _ = await self.begin(answer("done"))
        await conversation.submit("find")
        model = model_with()
        self.assertEqual(await self.conversation(model, self.store.load()).resume_turn(), ("waiting_user", ""))
        self.assertEqual(self.store.load()["turns"][-1]["reply"], "done")
        model.responses.create.assert_not_awaited()

    async def test_different_prompt_cannot_overwrite_unfinished_turn(self):
        conversation, _ = await self.begin(tool_response(call("lookup", {}, "one")), max_steps=1)
        await conversation.submit("find")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unfinished"):
            await conversation.submit("different")
        self.assertEqual(self.path.read_bytes(), original)
        fresh = self.store.start("test", new=True)
        self.assertNotEqual(fresh["conversation_id"], self.state["conversation_id"])

    def test_atomic_write_failure_keeps_previous_valid_checkpoint(self):
        original = self.path.read_bytes()
        with patch("agent_core.state.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                JsonCheckpointStore(self.path).save({"changed": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_corrupt_checkpoint_is_never_silently_replaced(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.start("test")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_resume_requires_existing_state(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "No saved conversation"):
            self.store.start("test", resume=True)
