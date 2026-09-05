"""Progress summariser: folds noisy low-level agent events into a few useful spoken updates.

Deterministic and stateful so it can be unit-tested on synthetic event streams. It knows
nothing about speech or the wire protocol; it returns `Spoken` records and the orchestrator
decides what to do with them.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from museglass.agent.interface import AgentEvent, AgentEventKind


class Verbosity(str, Enum):
    QUIET = "QUIET"
    NORMAL = "NORMAL"
    TALKATIVE = "TALKATIVE"

    @classmethod
    def parse(cls, value: str) -> "Verbosity":
        return cls(value.strip().upper())


class SpokenKind(str, Enum):
    PHASE = "phase"  # milestone / phase transition
    NARRATION = "narration"  # the agent's own sentence
    TESTS = "tests"
    BLOCKER = "blocker"
    QUESTION = "question"
    APPROVAL = "approval"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"
    STATUS = "status"


@dataclass
class Spoken:
    text: str
    kind: SpokenKind
    requires_response: bool = False
    critical: bool = False
    metadata: dict = field(default_factory=dict)


class Phase(str, Enum):
    IDLE = "idle"
    EXPLORING = "exploring"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    VERIFYING = "verifying"
    DONE = "done"


_MIN_GAP = {Verbosity.QUIET: 45.0, Verbosity.NORMAL: 20.0, Verbosity.TALKATIVE: 6.0}
_READ_TOOLS = {"read", "read_file", "readfile", "grep", "search", "glob", "ls", "list", "list_dir", "find", "cat", "view", "webfetch", "websearch", "todowrite", "task"}
_EDIT_TOOLS = {"edit", "write", "write_file", "writefile", "edit_file", "multiedit", "apply_patch", "patch", "create_file", "notebookedit", "str_replace_editor", "replace"}
_TEST_RE = re.compile(r"\b(pytest|py\.test|python\s+-m\s+pytest|npm\s+test|pnpm\s+test|yarn\s+test|go\s+test|cargo\s+test|mvn\s+test|gradle\s+test|jest|vitest|mocha|rspec|phpunit|unittest|make\s+test|tox|nox)\b")
_PYTEST_RESULT_RE = re.compile(r"(?:(\d+)\s+failed,?\s*)?(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error")
_JEST_RESULT_RE = re.compile(r"Tests:\s+(?:(\d+)\s+failed,\s+)?(\d+)\s+passed,\s+(\d+)\s+total")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_ARCH_WORDS = re.compile(r"\b(architectur|circular|coupling|duplicat|deprecated|breaking change|security|vulnerab|race condition|blocked|cannot|can't|unable|missing|not found|conflict)\w*", re.IGNORECASE)


def _first_sentences(text: str, max_chars: int = 200, max_sentences: int = 2) -> str:
    cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[#*_>]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    sentences = [s for s in _SENTENCE_RE.split(cleaned) if s and not s.endswith(":")]
    out = ""
    for s in sentences[:max_sentences]:
        if len(out) + len(s) + 1 > max_chars:
            break
        out = f"{out} {s}".strip()
    if not out:
        out = cleaned[: max_chars - 1] + "…"
    return out


def parse_test_result(output: str) -> dict | None:
    """Extract pass/fail counts from pytest / jest style output."""
    if not output:
        return None
    m = _JEST_RESULT_RE.search(output)
    if m:
        failed = int(m.group(1) or 0)
        return {"passed": int(m.group(2)), "failed": failed, "total": int(m.group(3))}
    last = None
    for m in _PYTEST_RESULT_RE.finditer(output):
        last = m
    if last:
        if last.group(2) is not None:
            failed = int(last.group(1) or 0)
            passed = int(last.group(2))
            return {"passed": passed, "failed": failed, "total": passed + failed}
        if last.group(3) is not None:
            return {"passed": 0, "failed": int(last.group(3)), "total": int(last.group(3))}
        if last.group(4) is not None:
            return {"passed": 0, "failed": int(last.group(4)), "total": int(last.group(4))}
    return None


class ProgressSummariser:
    def __init__(self, verbosity: Verbosity = Verbosity.NORMAL, *, clock=time.monotonic) -> None:
        self.verbosity = verbosity
        self._clock = clock
        self.phase = Phase.IDLE
        self.files_touched: set[str] = set()
        self.tools_used = 0
        self.last_tests: dict | None = None
        self.last_agent_message = ""
        self._last_spoken_at = float("-inf")
        self._recent: deque[str] = deque(maxlen=12)
        self._spoken_phase_texts: set[str] = set()

    # -- public ------------------------------------------------------------------------
    def set_verbosity(self, verbosity: Verbosity) -> None:
        self.verbosity = verbosity

    def reset_turn(self) -> None:
        self.phase = Phase.IDLE
        self.files_touched.clear()
        self.tools_used = 0
        self.last_tests = None
        self._spoken_phase_texts.clear()

    def detail(self) -> str:
        """Answer to "what are you doing?" / "more detail"."""
        parts = []
        if self.phase is not Phase.IDLE:
            parts.append(f"Currently {self.phase.value}.")
        if self.files_touched:
            names = sorted(self.files_touched)
            shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
            parts.append(f"Files changed so far: {shown}.")
        if self.last_tests:
            parts.append(self._tests_sentence(self.last_tests))
        if self._recent:
            parts.append("Recent steps: " + "; ".join(list(self._recent)[-4:]) + ".")
        if self.last_agent_message:
            parts.append(f"Last note from the agent: {_first_sentences(self.last_agent_message, 160, 1)}")
        return " ".join(parts) or "Nothing is running."

    def feed(self, event: AgentEvent) -> list[Spoken]:
        """Consume one low-level event; return zero or more things worth saying."""
        out: list[Spoken] = []
        kind = event.kind
        if kind is AgentEventKind.TURN_STARTED:
            self.reset_turn()
            return out
        if kind is AgentEventKind.TOOL_STARTED:
            out.extend(self._on_tool_started(event))
        elif kind is AgentEventKind.TOOL_COMPLETED:
            out.extend(self._on_tool_completed(event))
        elif kind is AgentEventKind.MESSAGE:
            out.extend(self._on_message(event))
        elif kind is AgentEventKind.APPROVAL_REQUESTED and event.approval:
            out.append(Spoken(event.approval.summary, SpokenKind.APPROVAL, requires_response=True, critical=True,
                              metadata={"request_id": event.approval.request_id, "kind": event.approval.kind,
                                        "choices": [c.choice_id for c in event.approval.choices]}))
        elif kind is AgentEventKind.QUESTION and event.question:
            text = event.question.text.strip()
            if event.question.options:
                opts = event.question.options[:4]
                text = f"{text} Options: " + ", ".join(opts) + "."
            out.append(Spoken(f"I need a decision. {text}", SpokenKind.QUESTION, requires_response=True, critical=True,
                              metadata={"question_id": event.question.question_id, "options": event.question.options}))
        elif kind is AgentEventKind.TURN_COMPLETED:
            out.append(self._completion(event))
            self.phase = Phase.DONE
        elif kind is AgentEventKind.TURN_CANCELLED:
            out.append(Spoken("Stopped.", SpokenKind.CANCELLED, critical=True))
            self.phase = Phase.IDLE
        elif kind is AgentEventKind.TURN_FAILED or kind is AgentEventKind.ERROR:
            msg = _first_sentences(event.text or "The agent hit an error.", 160, 1)
            out.append(Spoken(f"Problem: {msg}", SpokenKind.ERROR, critical=True))
        return [s for s in out if self._admit(s)]

    # -- internals ---------------------------------------------------------------------
    def _admit(self, spoken: Spoken) -> bool:
        now = self._clock()
        if spoken.critical or spoken.kind in (SpokenKind.COMPLETE, SpokenKind.TESTS, SpokenKind.BLOCKER, SpokenKind.PHASE):
            if spoken.kind in (SpokenKind.TESTS, SpokenKind.PHASE) and self.verbosity is Verbosity.QUIET:
                return False
            self._last_spoken_at = now
            return True
        if self.verbosity is Verbosity.QUIET:
            return False
        if spoken.kind is SpokenKind.NARRATION and self.verbosity is not Verbosity.TALKATIVE:
            return False
        if now - self._last_spoken_at < _MIN_GAP[self.verbosity]:
            return False
        self._last_spoken_at = now
        return True

    def _tool_class(self, tool: str | None, tool_input: dict | None) -> str:
        name = (tool or "").lower()
        inp = tool_input or {}
        command = str(inp.get("command") or inp.get("commandText") or "")
        if name in _EDIT_TOOLS:
            return "edit"
        if name in _READ_TOOLS:
            return "read"
        if command:
            if _TEST_RE.search(command):
                return "test"
            if re.search(r"\bgit\s+(commit|add)\b", command):
                return "commit"
            if re.search(r"\b(cat|grep|rg|ls|find|head|tail|sed\s+-n|git\s+(status|diff|log|show))\b", command):
                return "read"
            return "shell"
        return "other"

    def _on_tool_started(self, event: AgentEvent) -> list[Spoken]:
        self.tools_used += 1
        cls = self._tool_class(event.tool, event.tool_input)
        inp = event.tool_input or {}
        path = inp.get("file_path") or inp.get("path") or inp.get("filePath")
        if cls == "edit" and path:
            self.files_touched.add(str(path).rsplit("/", 1)[-1])
        self._recent.append(f"{cls} {str(path or inp.get('command') or inp.get('commandText') or event.tool or '')[:60]}".strip())
        new_phase = {
            "read": Phase.EXPLORING,
            "edit": Phase.IMPLEMENTING,
            "test": Phase.TESTING,
            "commit": Phase.VERIFYING,
        }.get(cls)
        if new_phase is None or new_phase == self.phase:
            return []
        # Phase transitions are the milestones worth hearing.
        text = {
            Phase.EXPLORING: "I'm looking through the code to find the right place.",
            Phase.IMPLEMENTING: "I found the relevant module and I'm implementing the change.",
            Phase.TESTING: "Implementation is done. Tests are running.",
            Phase.VERIFYING: "Committing the change.",
        }[new_phase]
        if new_phase is Phase.EXPLORING and self.phase in (Phase.IMPLEMENTING, Phase.TESTING):
            # going back to reading after edits is not worth announcing
            self.phase = new_phase
            return []
        self.phase = new_phase
        if text in self._spoken_phase_texts:
            return []
        self._spoken_phase_texts.add(text)
        return [Spoken(text, SpokenKind.PHASE)]

    def _on_tool_completed(self, event: AgentEvent) -> list[Spoken]:
        cls = self._tool_class(event.tool, event.tool_input)
        out: list[Spoken] = []
        if cls == "test":
            stats = parse_test_result(event.tool_output or "")
            if stats:
                self.last_tests = stats
                if stats["failed"]:
                    out.append(Spoken(self._tests_sentence(stats), SpokenKind.TESTS, metadata={"tests": stats}))
                else:
                    out.append(Spoken(self._tests_sentence(stats), SpokenKind.TESTS, metadata={"tests": stats}))
            elif event.success is False:
                out.append(Spoken("The test run failed to complete.", SpokenKind.BLOCKER, critical=True))
        return out

    def _on_message(self, event: AgentEvent) -> list[Spoken]:
        text = (event.text or "").strip()
        if not text:
            return []
        self.last_agent_message = text
        sentence = _first_sentences(text, 200, 2)
        if not sentence:
            return []
        if _ARCH_WORDS.search(sentence) and self.phase is not Phase.DONE:
            return [Spoken(sentence, SpokenKind.BLOCKER, critical=True)]
        return [Spoken(sentence, SpokenKind.NARRATION)]

    @staticmethod
    def _tests_sentence(stats: dict) -> str:
        if stats["failed"]:
            return f"{stats['failed']} of {stats['total']} tests failed."
        return f"All {stats['passed']} tests pass."

    def _completion(self, event: AgentEvent) -> Spoken:
        parts: list[str] = ["Done."]
        summary = _first_sentences(event.text or "", 220, 2)
        if summary and not summary.lower().startswith("done"):
            parts.append(summary)
        if self.last_tests:
            parts.append(self._tests_sentence(self.last_tests))
        meta = {"files_changed": sorted(self.files_touched), "tests": self.last_tests}
        return Spoken(" ".join(parts), SpokenKind.COMPLETE, metadata=meta)
