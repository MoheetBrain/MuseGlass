"""Session orchestrator: agent work + user speech + spoken progress, concurrently.

One orchestrator per MuseGlass session. It owns the agent session, persists everything to
the store, runs the summariser, gates approvals through the policy engine and drives TTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from museglass.agent.interface import (
    AgentEvent,
    AgentEventKind,
    AgentSession,
    ApprovalRequest,
    CodingAgent,
    Question,
    Workspace,
)
from museglass.host.latency import LatencyTracker
from museglass.host.policy import Decision, Risk, classify_action, describe_for_speech
from museglass.host.router import CommandRouter, Intent, IntentKind
from museglass.host.workspace import WorkspaceRegistry
from museglass.protocol.events import Event, EventType, Priority, new_event
from museglass.speech.base import TextToSpeechProvider
from museglass.store.sqlite import SessionRecord, SessionStore
from museglass.summariser.summariser import ProgressSummariser, Spoken, SpokenKind, Verbosity

log = logging.getLogger(__name__)


class SessionState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_ANSWER = "waiting_answer"
    ENDED = "ended"


@dataclass
class Pending:
    kind: str  # approval | question | commit_prompt
    request_id: str
    created_at: float = field(default_factory=time.monotonic)
    approval: ApprovalRequest | None = None
    question: Question | None = None
    decision: Decision | None = None
    timeout_task: asyncio.Task | None = None
    resume_state: SessionState = SessionState.RUNNING

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "summary": self.approval.summary if self.approval else (self.question.text if self.question else "commit?"),
            "category": self.decision.category if self.decision else None,
        }


EventListener = Callable[[Event], Awaitable[None] | None]


class SessionOrchestrator:
    def __init__(
        self,
        *,
        agent: CodingAgent,
        store: SessionStore,
        registry: WorkspaceRegistry,
        tts: TextToSpeechProvider,
        router: CommandRouter | None = None,
        summariser: ProgressSummariser | None = None,
        latency: LatencyTracker | None = None,
        verbosity: Verbosity = Verbosity.NORMAL,
        approval_timeout: float = 180.0,
        session_id: str | None = None,
        auto_commit_prompt: bool = True,
    ) -> None:
        self.agent = agent
        self.store = store
        self.registry = registry
        self.tts = tts
        self.router = router or CommandRouter()
        self.summariser = summariser or ProgressSummariser(verbosity)
        self.latency = latency or LatencyTracker()
        self.approval_timeout = approval_timeout
        self.auto_commit_prompt = auto_commit_prompt
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.state = SessionState.IDLE
        self.workspace: Workspace | None = None
        self.current_task: str | None = None
        self.pending: Pending | None = None
        self.token_usage: dict[str, Any] = {}
        self.cost_usd: float | None = None
        self._session: AgentSession | None = None
        self._pump: asyncio.Task | None = None
        self._speech_queue: asyncio.Queue[tuple[Event, float] | None] = asyncio.Queue()
        self._speech_task: asyncio.Task | None = None
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._listeners: list[EventListener] = []
        self._pausing = False
        self._last_instruction_key: str | None = None
        self._last_turn_was_commit = False
        self._closed = False

    # ------------------------------------------------------------------ lifecycle -----
    async def start(self, *, resume: bool = False) -> None:
        self._speech_task = asyncio.create_task(self._speech_pump(), name=f"tts-{self.session_id}")
        record = self.store.get_session(self.session_id) if resume else None
        if record is None:
            self.store.create_session(
                SessionRecord(session_id=self.session_id, backend=self.agent.name, status=self.state.value,
                              verbosity=self.summariser.verbosity.value)
            )
            await self._emit(new_event(EventType.SESSION_STARTED, self.session_id, "Session started.",
                                       backend=self.agent.name))
            return
        # resume path
        self.summariser.set_verbosity(Verbosity.parse(record.verbosity))
        self.current_task = record.current_task
        if record.project_id:
            self.workspace = self.registry.get(record.project_id)
        previous_state = record.status
        if self.workspace:
            await self._ensure_session(resume_id=record.backend_session_id)
        if previous_state == SessionState.RUNNING.value:
            text = "Your task is still running." if self._session and self._session.is_busy else "Your task was interrupted while you were away."
            self.state = SessionState.RUNNING if self._session and self._session.is_busy else SessionState.IDLE
        elif previous_state in (SessionState.WAITING_APPROVAL.value, SessionState.WAITING_ANSWER.value) or record.pending_request:
            text = "I'm still waiting for your answer."
            pending = record.pending_request or {}
            if pending.get("kind") == "commit_prompt":
                self.pending = Pending("commit_prompt", str(pending.get("request_id") or f"commit-{uuid.uuid4().hex[:8]}"), resume_state=SessionState.IDLE)
                text += " Want me to commit it?"
            # approvals / questions are re-issued by the backend after session/resume and re-create pending state
            self.state = SessionState(previous_state) if previous_state in (SessionState.WAITING_APPROVAL.value, SessionState.WAITING_ANSWER.value) else SessionState.WAITING_ANSWER
        else:
            text = "Session resumed."
        detail = self.summariser.detail() if self.state is SessionState.RUNNING else ""
        await self._emit(new_event(EventType.SESSION_RESUMED, self.session_id, f"{text} {detail}".strip(),
                                   project_id=self.workspace.project_id if self.workspace else None))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._emit(new_event(EventType.SESSION_ENDED, self.session_id, "Session ended.",
                                   project_id=self.workspace.project_id if self.workspace else None))
        self._set_state(SessionState.ENDED)
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
        if self._session:
            with contextlib.suppress(Exception):
                await self._session.close()
        await self._speech_queue.put(None)
        if self._speech_task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._speech_task, timeout=5)

    # ------------------------------------------------------------ subscriptions -------
    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    # ------------------------------------------------------------------ inputs --------
    async def handle_transcript(self, text: str, *, speech_end_at: float | None = None) -> Intent:
        """Entry point for every spoken (or typed) utterance."""
        received = time.monotonic()
        if speech_end_at is not None:
            self.latency.record("speech_end_to_transcript", (received - speech_end_at) * 1000.0)
        awaiting = self.pending is not None
        busy = self.state in (SessionState.RUNNING, SessionState.WAITING_APPROVAL, SessionState.WAITING_ANSWER)
        intent = self.router.parse(text, busy=busy, awaiting_response=awaiting)
        if intent.kind is IntentKind.IGNORE:
            return intent
        etype = {
            IntentKind.STOP: EventType.USER_INTERRUPT,
            IntentKind.PAUSE: EventType.USER_INTERRUPT,
            IntentKind.CONTINUE: EventType.USER_INTERRUPT,
            IntentKind.YES: EventType.USER_RESPONSE,
            IntentKind.NO: EventType.USER_RESPONSE,
        }.get(intent.kind, EventType.USER_COMMAND)
        await self._emit(new_event(etype, self.session_id, intent.raw, project_id=self._project_id(),
                                   intent=intent.kind.value))
        await self._dispatch(intent, speech_end_at=speech_end_at)
        return intent

    async def handle_event(self, event: Event) -> None:
        """Entry point for events arriving over the bridge (phone/console)."""
        if not event.is_user_event:
            return
        speech_end = event.metadata.get("speech_end_at")
        await self.handle_transcript(event.message, speech_end_at=speech_end)

    async def _dispatch(self, intent: Intent, *, speech_end_at: float | None) -> None:
        kind = intent.kind
        if kind is IntentKind.OPEN_PROJECT:
            ws = await self.open_project(intent.project or "")
            if ws and intent.text:
                await self.submit_task(intent.text, speech_end_at=speech_end_at)
        elif kind is IntentKind.TASK:
            await self.submit_task(intent.text, speech_end_at=speech_end_at)
        elif kind is IntentKind.STEER:
            await self.steer(intent.text, speech_end_at=speech_end_at)
        elif kind is IntentKind.STOP:
            await self.stop()
        elif kind is IntentKind.PAUSE:
            await self.pause()
        elif kind is IntentKind.CONTINUE:
            await self.resume_task()
        elif kind in (IntentKind.YES, IntentKind.NO):
            await self.respond(kind is IntentKind.YES, intent.text, speech_end_at=speech_end_at)
        elif kind is IntentKind.STATUS:
            await self._say_progress(self.status_text())
        elif kind is IntentKind.DETAIL:
            await self._say_progress(self.summariser.detail())
        elif kind is IntentKind.SHORT:
            await self._say_progress(self.status_text(short=True))
        elif kind is IntentKind.WHY:
            await self.steer("The user asks: " + intent.text + " Answer in one or two short sentences, then continue.", speech_end_at=speech_end_at)
        elif kind is IntentKind.UNDO:
            await self._instruct("Undo the last change you made and confirm in one sentence.", speech_end_at=speech_end_at)
        elif kind is IntentKind.SHOW_DIFF:
            await self._instruct("Describe what you changed so far in two short spoken sentences, without reading code.", speech_end_at=speech_end_at)
        elif kind is IntentKind.VERBOSITY and intent.verbosity:
            self.summariser.set_verbosity(intent.verbosity)
            self.store.update_session(self.session_id, verbosity=intent.verbosity.value)
            await self._say_progress(f"{intent.verbosity.value.capitalize()} mode.")
        elif kind is IntentKind.LIST_PROJECTS:
            names = [w.display_name for w in self.registry.list()]
            await self._say_progress("Projects: " + ", ".join(names) + "." if names else "No projects are registered.")
        elif kind is IntentKind.END_SESSION:
            await self.close()

    # ------------------------------------------------------------------ actions -------
    async def open_project(self, spoken_name: str) -> Workspace | None:
        ws = self.registry.resolve_spoken(spoken_name)
        if ws is None:
            names = ", ".join(w.display_name for w in self.registry.list()) or "none"
            await self._say_error(f"I don't know a project called {spoken_name}. Registered projects: {names}.")
            return None
        if self.workspace and self.workspace.project_id != ws.project_id and self._session:
            await self._session.close()
            self._session = None
            if self._pump:
                self._pump.cancel()
        self.workspace = ws
        self.store.update_session(self.session_id, project_id=ws.project_id)
        await self._ensure_session()
        await self._say_progress(f"Opened {ws.display_name}.")
        return ws

    async def submit_task(self, text: str, *, speech_end_at: float | None = None) -> None:
        if not self.workspace:
            await self._say_error("Open a project first. For example: Muse, open the demo project.")
            return
        await self._ensure_session()
        assert self._session
        if self._session.is_busy or self.state is SessionState.RUNNING:
            await self.steer(text, speech_end_at=speech_end_at)
            return
        self.current_task = text
        self._last_turn_was_commit = _looks_like_commit(text)
        self.store.update_session(self.session_id, current_task=text, status=SessionState.RUNNING.value)
        self._set_state(SessionState.RUNNING)
        key = f"ack:{uuid.uuid4().hex}"
        if speech_end_at is not None:
            self.latency.mark(key, speech_end_at)
            self._last_instruction_key = key
        await self._session.send_instruction(text)

    async def steer(self, text: str, *, speech_end_at: float | None = None) -> None:
        if not self._session or not self.workspace:
            await self.submit_task(text, speech_end_at=speech_end_at)
            return
        if not self._session.is_busy:
            await self.submit_task(text, speech_end_at=speech_end_at)
            return
        key = f"ack:{uuid.uuid4().hex}"
        if speech_end_at is not None:
            self.latency.mark(key, speech_end_at)
            self._last_instruction_key = key
        await self._session.send_instruction(text, steer=True)
        if self.current_task:
            self.current_task = f"{self.current_task} | {text}"
            self.store.update_session(self.session_id, current_task=self.current_task)

    async def _instruct(self, text: str, *, speech_end_at: float | None) -> None:
        if self._session and self._session.is_busy:
            await self.steer(text, speech_end_at=speech_end_at)
        else:
            await self.submit_task(text, speech_end_at=speech_end_at)

    async def stop(self) -> None:
        await self.tts.stop()
        self._drain_speech_queue()
        if self._session and (self._session.is_busy or self.pending):
            self.latency.mark("interrupt")
            if self.pending and self.pending.kind == "approval":
                await self._resolve_pending_approval(approved=False, feedback="stopped by user")
            await self._session.interrupt()
            asyncio.create_task(self._interrupt_watchdog())
        else:
            await self._say_progress("Nothing is running.")

    async def _interrupt_watchdog(self, grace: float = 8.0) -> None:
        """If the backend confirms nothing after an interrupt, do not leave the session stuck."""
        await asyncio.sleep(grace)
        if self.state in (SessionState.RUNNING, SessionState.WAITING_APPROVAL, SessionState.WAITING_ANSWER) and self._session and not self._session.is_busy and self.pending is None:
            self.latency.close("interrupt", "interrupt_to_ack")
            self._set_state(SessionState.IDLE)
            await self._say_progress("Stopped.", priority=Priority.HIGH)

    async def pause(self) -> None:
        await self.tts.stop()
        if self._session and self._session.is_busy:
            self._pausing = True
            self.latency.mark("interrupt")
            await self._session.pause()
        else:
            await self._say_progress("Nothing is running.")

    async def resume_task(self) -> None:
        if self.state is SessionState.PAUSED and self._session:
            self._set_state(SessionState.RUNNING)
            await self._emit(new_event(EventType.SESSION_RESUMED, self.session_id, "Continuing.", project_id=self._project_id()))
            await self._session.resume()
        elif self._session and self._session.is_busy:
            await self._say_progress("Still working.")
        else:
            await self._say_progress("Nothing to continue.")

    async def respond(self, affirmative: bool, extra: str = "", *, speech_end_at: float | None = None) -> None:
        pending = self.pending
        if pending is None:
            await self._say_progress("There's nothing waiting for an answer.")
            return
        if pending.kind == "approval":
            await self._resolve_pending_approval(approved=affirmative, feedback=extra or None)
            if affirmative and extra:
                await self.steer(extra, speech_end_at=speech_end_at)
        elif pending.kind == "question":
            self._clear_pending()
            assert self._session and pending.question
            answer = _answer_text(affirmative, extra, pending.question)
            await self._session.answer_question(pending.question.question_id, answer)
            self._set_state(SessionState.RUNNING)
        elif pending.kind == "commit_prompt":
            self._clear_pending()
            if not affirmative:
                await self._say_progress("Okay, leaving the changes uncommitted.")
                self._set_state(SessionState.IDLE)
                return
            wants_push = "push" in extra.lower() and not _negated_push(extra)
            instruction = "Commit all current changes locally with a concise descriptive message."
            instruction += " Then push." if wants_push else " Do not push."
            await self._say_progress("Committing.")
            await self.submit_task(instruction, speech_end_at=speech_end_at)

    # --------------------------------------------------------------- agent pump -------
    async def _ensure_session(self, *, resume_id: str | None = None) -> None:
        if self._session is not None or not self.workspace:
            return
        self._session = await self.agent.create_session(self.workspace, resume_id=resume_id)
        self._session.workspace = self.workspace
        self.store.update_session(self.session_id, backend_session_id=self._session.backend_session_id)
        self._pump = asyncio.create_task(self._agent_pump(self._session), name=f"agent-pump-{self.session_id}")

    async def _agent_pump(self, session: AgentSession) -> None:
        try:
            async for ev in session.events():
                try:
                    await self._on_agent_event(ev)
                except Exception:  # noqa: BLE001
                    log.exception("error handling agent event %s", ev.kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("agent event stream failed")
            await self._say_error(f"The agent connection failed: {exc}")
            self._set_state(SessionState.IDLE)

    async def _on_agent_event(self, ev: AgentEvent) -> None:
        self.store.append_agent_log(self.session_id, ev.to_dict())
        kind = ev.kind
        if kind is AgentEventKind.SESSION_READY:
            if self._session and self._session.backend_session_id:
                self.store.update_session(self.session_id, backend_session_id=self._session.backend_session_id)
            return
        if kind is AgentEventKind.TURN_STARTED:
            if self._last_instruction_key:
                self.latency.close(self._last_instruction_key, "speech_end_to_agent_ack")
                self._last_instruction_key = None
            self._set_state(SessionState.RUNNING)
        elif kind is AgentEventKind.TOKEN_USAGE and ev.usage:
            self.token_usage = ev.usage
            if "cost_usd" in ev.usage:
                self.cost_usd = ev.usage["cost_usd"]
        elif kind is AgentEventKind.APPROVAL_REQUESTED and ev.approval:
            await self._on_approval(ev)
            return
        elif kind is AgentEventKind.QUESTION and ev.question:
            self._set_pending(Pending("question", ev.question.question_id, question=ev.question))
            self._set_state(SessionState.WAITING_ANSWER)
        elif kind is AgentEventKind.QUESTION_SETTLED:
            if self.pending and self.pending.kind == "question":
                self._clear_pending()
                self._set_state(SessionState.RUNNING)
        elif kind is AgentEventKind.APPROVAL_RESOLVED:
            if self.pending and self.pending.kind == "approval" and self.pending.request_id == (ev.approval.request_id if ev.approval else None):
                self._clear_pending()
                self._set_state(SessionState.RUNNING)
        elif kind is AgentEventKind.TURN_CANCELLED:
            self.latency.close("interrupt", "interrupt_to_ack")
            if self._pausing:
                self._pausing = False
                self._set_state(SessionState.PAUSED)
                await self._emit(new_event(EventType.SESSION_PAUSED, self.session_id, "Paused.", project_id=self._project_id()))
            else:
                self._set_state(SessionState.IDLE)
        elif kind in (AgentEventKind.TURN_COMPLETED, AgentEventKind.TURN_FAILED):
            self._set_state(SessionState.IDLE)
            if self._last_instruction_key:
                self.latency.close(self._last_instruction_key, "speech_end_to_agent_ack")
                self._last_instruction_key = None
        # Everything else flows through the summariser.
        for spoken in self.summariser.feed(ev):
            await self._emit_spoken(spoken, ev)
        if kind is AgentEventKind.TURN_COMPLETED:
            await self._after_completion()

    async def _on_approval(self, ev: AgentEvent) -> None:
        assert ev.approval and self._session and self.workspace
        req = ev.approval
        tool_input = req.raw.get("tool_input") or _tool_input_from_request(req)
        decision = classify_action(req.tool_name, tool_input, self.workspace.root)
        if decision.risk is Risk.SAFE:
            await self._session.approve(req.request_id)
            if self.summariser.verbosity is Verbosity.TALKATIVE:
                await self._say_progress(f"Allowed: {req.summary}", priority=Priority.LOW)
            return
        if decision.risk is Risk.FORBIDDEN:
            await self._session.reject(req.request_id, feedback=f"Not allowed by MuseGlass policy: {decision.reason}. Find another way or ask the user.")
            await self._say_progress(f"I blocked an action: {decision.reason}.", priority=Priority.HIGH)
            return
        summary = describe_for_speech(req.tool_name, tool_input, decision) if tool_input else req.summary
        req.summary = summary
        pending = Pending("approval", req.request_id, approval=req, decision=decision, resume_state=self.state)
        self._set_pending(pending)
        self._set_state(SessionState.WAITING_APPROVAL)
        pending.timeout_task = asyncio.create_task(self._approval_timeout(pending))
        for spoken in self.summariser.feed(ev):
            spoken.text = summary
            spoken.metadata.update({"category": decision.category, "reason": decision.reason})
            await self._emit_spoken(spoken, ev)

    async def _approval_timeout(self, pending: Pending) -> None:
        try:
            await asyncio.sleep(self.approval_timeout)
        except asyncio.CancelledError:
            return
        if self.pending is pending:
            await self._resolve_pending_approval(approved=False, feedback="no answer from the user within the time limit")
            await self._say_progress("No answer, so I did not do that.", priority=Priority.HIGH)

    async def _resolve_pending_approval(self, *, approved: bool, feedback: str | None) -> None:
        pending = self.pending
        if not pending or pending.kind != "approval" or not self._session:
            return
        self._clear_pending()
        if approved:
            await self._session.approve(pending.request_id)
        else:
            await self._session.reject(pending.request_id, feedback=feedback)
        self._set_state(SessionState.RUNNING)

    async def _after_completion(self) -> None:
        if not self.auto_commit_prompt or not self.workspace or self._last_turn_was_commit:
            return
        if self.state is not SessionState.IDLE:
            return
        if not _has_uncommitted_changes(self.workspace):
            return
        pending = Pending("commit_prompt", f"commit-{uuid.uuid4().hex[:8]}", resume_state=SessionState.IDLE)
        self._set_pending(pending)
        await self._emit(new_event(EventType.MUSE_QUESTION, self.session_id, "Want me to commit it?",
                                   project_id=self._project_id(), priority=Priority.HIGH, requires_response=True,
                                   request_id=pending.request_id, kind="commit_prompt"))

    # ---------------------------------------------------------------- speaking -------
    async def _emit_spoken(self, spoken: Spoken, source: AgentEvent) -> None:
        etype = {
            SpokenKind.APPROVAL: EventType.MUSE_APPROVAL_REQUEST,
            SpokenKind.QUESTION: EventType.MUSE_QUESTION,
            SpokenKind.COMPLETE: EventType.MUSE_COMPLETE,
            SpokenKind.ERROR: EventType.MUSE_ERROR,
        }.get(spoken.kind, EventType.MUSE_PROGRESS)
        priority = Priority.CRITICAL if spoken.kind is SpokenKind.APPROVAL else (
            Priority.HIGH if spoken.critical or spoken.kind in (SpokenKind.COMPLETE, SpokenKind.TESTS) else Priority.NORMAL)
        event = new_event(etype, self.session_id, spoken.text, project_id=self._project_id(), priority=priority,
                          requires_response=spoken.requires_response, spoken_kind=spoken.kind.value,
                          agent_event_at=source.at, **spoken.metadata)
        await self._emit(event)

    async def _say_progress(self, text: str, *, priority: Priority = Priority.NORMAL) -> None:
        await self._emit(new_event(EventType.MUSE_PROGRESS, self.session_id, text, project_id=self._project_id(), priority=priority))

    async def _say_error(self, text: str) -> None:
        await self._emit(new_event(EventType.MUSE_ERROR, self.session_id, text, project_id=self._project_id(), priority=Priority.HIGH))

    async def _emit(self, event: Event) -> None:
        self.store.append_event(event)
        for q in list(self._subscribers):
            q.put_nowait(event)
        for listener in self._listeners:
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("event listener failed")
        if event.is_spoken:
            await self._speech_queue.put((event, time.monotonic()))

    async def _speech_pump(self) -> None:
        while True:
            item = await self._speech_queue.get()
            if item is None:
                return
            event, queued_at = item
            agent_at = event.metadata.get("agent_event_at")
            if isinstance(agent_at, (int, float)):
                self.latency.record("agent_event_to_spoken", (time.monotonic() - agent_at) * 1000.0)
            try:
                await self.tts.speak(event.message)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("TTS failed")

    def _drain_speech_queue(self) -> None:
        kept: list[tuple[Event, float]] = []
        while not self._speech_queue.empty():
            item = self._speech_queue.get_nowait()
            if item is None:
                self._speech_queue.put_nowait(None)
                break
            if item[0].requires_response or item[0].priority is Priority.CRITICAL:
                kept.append(item)
        for item in kept:
            self._speech_queue.put_nowait(item)

    # ------------------------------------------------------------------ state ---------
    def _set_state(self, state: SessionState) -> None:
        if self.state is SessionState.ENDED:
            return
        self.state = state
        self.store.update_session(self.session_id, status=state.value)

    def _set_pending(self, pending: Pending) -> None:
        if self.pending and self.pending.timeout_task:
            self.pending.timeout_task.cancel()
        self.pending = pending
        self.store.update_session(self.session_id, pending_request=pending.to_dict())

    def _clear_pending(self) -> None:
        if self.pending and self.pending.timeout_task:
            self.pending.timeout_task.cancel()
        self.pending = None
        self.store.update_session(self.session_id, pending_request=None)

    def _project_id(self) -> str | None:
        return self.workspace.project_id if self.workspace else None

    def status_text(self, *, short: bool = False) -> str:
        if self.state is SessionState.RUNNING:
            phase = self.summariser.phase.value
            base = f"Working on it, currently {phase}." if phase != "idle" else "Working on it."
            return base if short else f"{base} {self.summariser.detail()}".strip()
        if self.state is SessionState.PAUSED:
            return "Paused. Say continue to resume."
        if self.state is SessionState.WAITING_APPROVAL and self.pending and self.pending.approval:
            return f"Waiting for your approval: {self.pending.approval.summary}"
        if self.state is SessionState.WAITING_ANSWER and self.pending and self.pending.question:
            return f"Waiting for your answer: {self.pending.question.text}"
        if self.pending and self.pending.kind == "commit_prompt":
            return "Waiting to hear whether I should commit."
        return "Idle." if self.workspace else "Idle. No project is open."

    def snapshot(self) -> dict[str, Any]:
        last = self.store.last_user_command(self.session_id)
        return {
            "session_id": self.session_id,
            "backend": self.agent.name,
            "project": self.workspace.project_id if self.workspace else None,
            "workspace_root": str(self.workspace.root) if self.workspace else None,
            "state": self.state.value,
            "current_task": self.current_task,
            "last_user_command": last.message if last else None,
            "pending": self.pending.to_dict() if self.pending else None,
            "verbosity": self.summariser.verbosity.value,
            "phase": self.summariser.phase.value,
            "files_touched": sorted(self.summariser.files_touched),
            "tests": self.summariser.last_tests,
            "token_usage": self.token_usage,
            "cost_usd": self.cost_usd,
            "latency": self.latency.summary(),
            "recent_events": [e.to_dict() for e in self.store.recent_events(self.session_id, 15)],
            "backend_session_id": self._session.backend_session_id if self._session else None,
        }


# ------------------------------------------------------------------ helpers -----------
def _looks_like_commit(text: str) -> bool:
    t = text.lower()
    return "commit" in t and "don't commit" not in t and "do not commit" not in t


def _negated_push(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in ("don't push", "do not push", "dont push", "no push", "without pushing", "not push", "but not push"))


def _has_uncommitted_changes(workspace: Workspace) -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=workspace.root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and bool(out.stdout.strip())


def _answer_text(affirmative: bool, extra: str, question: Question) -> str:
    if extra:
        return extra
    if question.options:
        lowered = [o.lower() for o in question.options]
        for word in ("yes", "no"):
            if (word == "yes") == affirmative and word in lowered:
                return question.options[lowered.index(word)]
    return "Yes." if affirmative else "No."


def _tool_input_from_request(req: ApprovalRequest) -> dict[str, Any] | None:
    raw = req.raw
    subject = raw.get("subject") if isinstance(raw, dict) else None
    if isinstance(subject, dict):
        if subject.get("command"):
            return {"command": subject["command"]}
        if subject.get("path"):
            return {"path": subject["path"], "access": subject.get("access")}
    if req.detail:
        return {"command": req.detail} if req.kind in ("shell", "process") else {"path": req.detail}
    return None
