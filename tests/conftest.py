from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from canvas_tool.gate import CanvasGate
from canvas_tool.memory import Store


@dataclass
class FakeClient:
    self_id: int = 7
    control: str = "COURSE-TEAM CONTROL: RUNNING"
    entries: list[dict[str, Any]] = field(default_factory=list)
    post_calls: list[tuple[str, int | None, str]] = field(default_factory=list)
    next_id: int = 100

    def discover(self):
        return {"course_id": 11, "topic_id": 22, "self_user_id": self.self_id}

    def get_control_line(self, course_id, topic_id):
        return self.control

    def get_entries(self, course_id, topic_id):
        return self.entries

    def post_entry(self, course_id, topic_id, message, parent_id=None):
        self.post_calls.append(("reply" if parent_id is not None else "new", parent_id, message))
        entry = {
            "id": self.next_id,
            "parent_id": parent_id,
            "user_id": self.self_id,
            "author_name": "Agent",
            "created_at": "2026-10-06T12:00:01Z",
            "text": message,
            "deleted": False,
        }
        self.entries.append(entry)
        self.next_id += 1
        return entry["id"]


@pytest.fixture
def gate(tmp_path: Path):
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    state.mkdir()
    logs.mkdir()
    client = FakeClient(entries=[{
        "id": 42, "parent_id": None, "user_id": 8, "author_name": "Peer",
        "created_at": "2026-10-06T11:00:00Z", "text": "A useful peer post", "deleted": False,
    }])
    store = Store(state / "agent.db")
    now = lambda: datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    return CanvasGate(client, store, state, now=now), client, store, state
