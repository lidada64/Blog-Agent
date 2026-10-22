"""Atomic checkpoints. A state file belongs to one task and one running process."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import PersistenceError


def now():
    return datetime.now(timezone.utc).isoformat()


def set_tool_status(call, status):
    call.update(
        status=status,
        called=call["attempts"] > 0,
        running=status == "running",
        completed=status in {"completed", "failed"},
        updated_at=now(),
    )


def atomic_write_json(path, data):
    """Replace one JSON file only after its complete contents reach disk."""
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise PersistenceError(f"Checkpoint write failed: {path.name} ({type(exc).__name__})") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass  # Cleanup must not mask the original failure or a successful replace.


class JsonCheckpointStore:
    """Atomic JSON persistence; subclasses own schema and lifecycle validation."""
    def __init__(self, path):
        self.path = Path(path)

    def save(self, state):
        state["updated_at"] = now()
        atomic_write_json(self.path, state)
