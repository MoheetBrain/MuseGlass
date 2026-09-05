"""The MuseGlass event protocol.

One typed envelope crosses every boundary: in-process queues, the SQLite store and the
WebSocket between phone and host. It is deliberately flat so it can be serialised as JSON
without a schema library.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1


class EventType(str, Enum):
    # user -> host
    USER_COMMAND = "USER_COMMAND"  # a task or a steer instruction
    USER_INTERRUPT = "USER_INTERRUPT"  # stop / pause / continue
    USER_RESPONSE = "USER_RESPONSE"  # answer to a question or approval request
    # host -> user
    MUSE_PROGRESS = "MUSE_PROGRESS"
    MUSE_QUESTION = "MUSE_QUESTION"
    MUSE_APPROVAL_REQUEST = "MUSE_APPROVAL_REQUEST"
    MUSE_COMPLETE = "MUSE_COMPLETE"
    MUSE_ERROR = "MUSE_ERROR"
    # lifecycle
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_RESUMED = "SESSION_RESUMED"
    SESSION_ENDED = "SESSION_ENDED"


class Priority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


USER_EVENT_TYPES = frozenset(
    {EventType.USER_COMMAND, EventType.USER_INTERRUPT, EventType.USER_RESPONSE}
)
SPOKEN_EVENT_TYPES = frozenset(
    {
        EventType.MUSE_PROGRESS,
        EventType.MUSE_QUESTION,
        EventType.MUSE_APPROVAL_REQUEST,
        EventType.MUSE_COMPLETE,
        EventType.MUSE_ERROR,
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Event:
    type: EventType
    session_id: str
    message: str = ""
    project_id: str | None = None
    priority: Priority = Priority.NORMAL
    requires_response: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now_iso)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int | None = None  # assigned by the session store, per session, monotonic

    # -- serialisation -------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "type": self.type.value,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "message": self.message,
            "priority": self.priority.value,
            "requires_response": self.requires_response,
            "metadata": self.metadata,
            "seq": self.seq,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        version = data.get("v", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version {version!r}")
        try:
            etype = EventType(data["type"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"invalid event type: {data.get('type')!r}") from exc
        return cls(
            type=etype,
            session_id=str(data.get("session_id") or ""),
            message=str(data.get("message") or ""),
            project_id=data.get("project_id"),
            priority=Priority(data.get("priority") or Priority.NORMAL.value),
            requires_response=bool(data.get("requires_response", False)),
            metadata=dict(data.get("metadata") or {}),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            event_id=str(data.get("event_id") or uuid.uuid4().hex),
            seq=data.get("seq"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Event":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("event must be a JSON object")
        return cls.from_dict(data)

    # -- convenience ---------------------------------------------------------------------
    @property
    def is_user_event(self) -> bool:
        return self.type in USER_EVENT_TYPES

    @property
    def is_spoken(self) -> bool:
        return self.type in SPOKEN_EVENT_TYPES


def new_event(
    type: EventType,
    session_id: str,
    message: str = "",
    *,
    project_id: str | None = None,
    priority: Priority = Priority.NORMAL,
    requires_response: bool = False,
    **metadata: Any,
) -> Event:
    """Small factory so call sites read like sentences."""
    return Event(
        type=type,
        session_id=session_id,
        message=message,
        project_id=project_id,
        priority=priority,
        requires_response=requires_response,
        metadata=metadata,
    )
