"""The backend-neutral coding-agent interface.

Muse Code is the first backend. Nothing above this module may import a backend directly:
the orchestrator, summariser and bridge only ever see `AgentSession` and `AgentEvent`.
"""

from __future__ import annotations

import abc
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Workspace:
    """A registered project directory the agent is allowed to work in."""

    project_id: str
    root: Path
    display_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        if not self.display_name:
            object.__setattr__(self, "display_name", self.project_id.replace("-", " "))


class AgentEventKind(str, Enum):
    SESSION_READY = "session_ready"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_CANCELLED = "turn_cancelled"
    TURN_FAILED = "turn_failed"
    MESSAGE_DELTA = "message_delta"  # streaming assistant text
    MESSAGE = "message"  # a complete assistant message
    REASONING = "reasoning"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    QUESTION = "question"
    QUESTION_SETTLED = "question_settled"
    TOKEN_USAGE = "token_usage"
    STATUS = "status"  # informational backend state changes
    ERROR = "error"


@dataclass(frozen=True)
class ApprovalChoice:
    choice_id: str
    label: str
    decision: str  # approved | approvedForSession | denied | abort | ...
    accepts_feedback: bool = False


@dataclass
class ApprovalRequest:
    request_id: str
    tool_name: str
    kind: str  # shell | fileAccess | network | process | tool | git | unknown
    summary: str  # one line the user can hear: "run `git push origin main`"
    detail: str = ""  # command / path / raw args
    choices: list[ApprovalChoice] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def choice_for(self, decision_prefix: str) -> ApprovalChoice | None:
        for choice in self.choices:
            if choice.decision.startswith(decision_prefix):
                return choice
        return None


@dataclass
class Question:
    question_id: str
    text: str
    options: list[str] = field(default_factory=list)
    multiple: bool = False
    header: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentEvent:
    kind: AgentEventKind
    text: str = ""
    tool: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: str | None = None
    success: bool | None = None
    approval: ApprovalRequest | None = None
    question: Question | None = None
    turn_id: str | None = None
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "tool": self.tool,
            "tool_input": self.tool_input,
            "tool_output": self.tool_output,
            "success": self.success,
            "turn_id": self.turn_id,
            "usage": self.usage,
        }


@dataclass(frozen=True)
class AgentHealth:
    available: bool
    reason: str = ""
    version: str = ""


class AgentSession(abc.ABC):
    """A live, persistent conversation with one backend inside one workspace."""

    workspace: Workspace

    @property
    @abc.abstractmethod
    def backend_session_id(self) -> str | None:
        """The backend's own session identifier, once known (used for resume)."""

    @property
    @abc.abstractmethod
    def is_busy(self) -> bool:
        """True while a turn is running."""

    @abc.abstractmethod
    async def send_instruction(self, text: str, *, steer: bool = False) -> str:
        """Deliver user text. `steer=True` injects it into the running turn; otherwise a new
        turn starts (or is queued behind the current one). Returns the turn id."""

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Stop the running turn as fast as the backend allows."""

    @abc.abstractmethod
    async def approve(self, request_id: str, choice_id: str | None = None) -> None: ...

    @abc.abstractmethod
    async def reject(self, request_id: str, feedback: str | None = None) -> None: ...

    @abc.abstractmethod
    async def answer_question(self, question_id: str, answer: str) -> None: ...

    @abc.abstractmethod
    async def pause(self) -> None: ...

    @abc.abstractmethod
    async def resume(self) -> None: ...

    @abc.abstractmethod
    async def cancel(self) -> None: ...

    @abc.abstractmethod
    def events(self) -> AsyncIterator[AgentEvent]:
        """The live low-level event stream (single consumer)."""

    @abc.abstractmethod
    async def close(self) -> None: ...


class CodingAgent(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def health(self) -> AgentHealth: ...

    @abc.abstractmethod
    async def create_session(
        self, workspace: Workspace, *, resume_id: str | None = None
    ) -> AgentSession: ...
