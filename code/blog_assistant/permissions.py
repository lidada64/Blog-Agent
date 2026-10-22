"""Application-owned modes. Model arguments never grant permissions."""
from dataclasses import dataclass
from agent_core.errors import PolicyDenied

READ_TOOLS = frozenset({"list_posts", "search_posts", "read_post", "read_post_full", "find_file", "find_text", "read_conversation_history"})
# Management preparation/publishing is deliberately not registered in this slice.
MODE_TOOLS = {"qa": READ_TOOLS, "manage": READ_TOOLS}

@dataclass(frozen=True)
class Permissions:
    mode: str
    def __post_init__(self):
        if self.mode not in MODE_TOOLS:
            raise ValueError("mode must be qa or manage")
    def require(self, name):
        if name not in MODE_TOOLS[self.mode]:
            raise PolicyDenied(f"Tool {name} is not allowed in {self.mode} mode")
