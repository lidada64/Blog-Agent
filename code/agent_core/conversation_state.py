"""A conversation owns user turns; model steps and tool calls belong to those turns."""

import json
from uuid import uuid4

from .config import DEFAULT_CONVERSATION_PATH, MAX_USER_TURNS, SKILL_SYSTEM_PROMPT
from .state import StateStore, now
from .reasoning import configure_reasoning, validate_reasoning


class ConversationStore(StateStore):
    def __init__(self, path=DEFAULT_CONVERSATION_PATH, *, kind="skill_conversation",
                 instructions=SKILL_SYSTEM_PROMPT, max_user_turns=MAX_USER_TURNS,
                 introduction="请先向用户介绍你的 Skill 检索、下载录入、本地文件与关键词检索能力和退出方法，然后等待用户输入。"):
        super().__init__(path)
        if type(max_user_turns) is not int or max_user_turns < 1:
            raise ValueError("max_user_turns must be positive")
        self.kind, self.instructions = kind, instructions
        self.max_user_turns, self.introduction = max_user_turns, introduction

    def load(self):
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") not in {2, 3} or state.get("kind") != self.kind:
            raise ValueError(f"Not a {self.kind} checkpoint; use the matching entry point")
        if state["version"] == 2:
            if self.kind != "skill_conversation":
                raise ValueError("Only legacy Skill checkpoints support version 2 migration")
            state.update(version=3, max_user_turns=MAX_USER_TURNS, introduction=self.introduction)
        required = {
            "conversation_id": str, "model": str, "instructions": str, "status": str,
            "history": list, "context": list, "turns": list, "steps": list,
            "tool_calls": list, "compactions": list, "turn_count": int,
            "compact_threshold": int, "max_user_turns": int, "introduction": str,
        }
        for key, expected in required.items():
            if type(state.get(key)) is not expected:
                raise ValueError(f"Invalid conversation field: {key}")
        if self.kind == "skill_conversation" and not isinstance(state.get("library_file"), str):
            raise ValueError("Invalid Skill library_file")
        if state["max_user_turns"] < 1:
            raise ValueError("Invalid maximum user turns")
        # Older checkpoints only supported the standalone endpoint. Migrate to
        # client-owned local memory unless an explicit mode was already saved.
        state.setdefault("compact_mode", "local")
        if not isinstance(state["compact_mode"], str) or state["compact_mode"] not in {"none", "local", "summary", "standalone"}:
            raise ValueError("Invalid conversation compaction mode")
        memory = state.get("summary_memory")
        if memory is not None and (
            not isinstance(memory, dict) or not isinstance(memory.get("text"), str)
            or type(memory.get("history_end")) is not int
            or not 0 <= memory["history_end"] <= len(state["history"])
        ):
            raise ValueError("Invalid conversation summary memory")
        statuses = {"pending", "running", "waiting_user", "model_error", "compact_error", "max_steps", "interrupted", "closed"}
        if state["status"] not in statuses or state["compact_threshold"] < 1:
            raise ValueError("Invalid conversation status or compaction threshold")
        if not 0 <= state["turn_count"] <= state["max_user_turns"]:
            raise ValueError("Invalid conversation turn count")
        for turn in state["turns"]:
            if not isinstance(turn, dict) or turn.get("kind") not in {"introduction", "user"} or turn.get("status") not in {"running", "completed"} or not isinstance(turn.get("reply"), str):
                raise ValueError("Invalid conversation turn")
        if state["turn_count"] != sum(turn["kind"] == "user" for turn in state["turns"]):
            raise ValueError("Conversation turn count does not match its turns")
        if sum(turn["status"] == "running" for turn in state["turns"]) > 1:
            raise ValueError("Multiple unfinished conversation turns")
        for step in state["steps"]:
            if not isinstance(step, dict) or type(step.get("number")) is not int or "status" not in step:
                raise ValueError("Invalid conversation step")
        for call in state["tool_calls"]:
            if not isinstance(call, dict) or not all(isinstance(call.get(key), str) for key in ("call_id", "name", "arguments", "status")) or type(call.get("attempts")) is not int:
                raise ValueError("Invalid conversation tool call")
            if call["status"] not in {"pending", "running", "interrupted", "completed", "failed"}:
                raise ValueError("Invalid tool status")
            if call.get("running") != (call["status"] == "running") or call.get("completed") != (call["status"] in {"completed", "failed"}):
                raise ValueError("Inconsistent conversation tool status")
            if "step" not in call or "result" not in call or (call["completed"] and not isinstance(call["result"], dict)):
                raise ValueError("Invalid conversation tool result")
        if not all(isinstance(item, dict) for item in state["history"] + state["context"]):
            raise ValueError("Invalid conversation history")
        validate_reasoning(state)
        return state

    def start(self, model, library_file, compact_threshold, *, compact_mode=None, reasoning_effort=None, resume=False, new=False):
        if compact_mode is not None and compact_mode not in {"none", "local", "summary", "standalone"}:
            raise ValueError("Invalid conversation compaction mode")
        existing = None if new else self.load()
        if resume and existing is None:
            raise ValueError("No saved conversation to resume")
        if existing is not None:
            configure_reasoning(existing, reasoning_effort)
            if compact_mode is not None and compact_mode != existing["compact_mode"]:
                existing["compact_mode"] = compact_mode
            self.save(existing)
            return existing
        state = {
            "version": 3, "kind": self.kind, "conversation_id": str(uuid4()),
            "model": model, "instructions": self.instructions,
            "max_user_turns": self.max_user_turns, "introduction": self.introduction,
            "status": "pending", "phase": "introduction", "created_at": now(),
            "history": [], "context": [], "turns": [], "turn_count": 0,
            "steps": [], "tool_calls": [], "compactions": [], "last_error": None,
            "compact_threshold": compact_threshold,
            "compact_mode": compact_mode or "local",
            "close_reason": None,
        }
        if self.kind == "skill_conversation":
            state["library_file"] = str(library_file)
        configure_reasoning(state, reasoning_effort)
        self.save(state)
        return state
