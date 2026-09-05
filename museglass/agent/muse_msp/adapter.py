"""Muse Code backend: `CodingAgent` implemented over the Muse Session Protocol.

Mapping (MSP → AgentEvent) is documented in docs/meta-sdk-notes.md. Workarounds:
- pause = `turn/interrupt` + a later "continue" turn (MSP has no pause);
- approvals: the session runs in `promptUnmatched`; MuseGlass's policy engine decides which
  `approval/requested` are auto-approved and which are read to the user.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from museglass.agent.interface import (
    AgentEvent,
    AgentEventKind,
    AgentHealth,
    AgentSession,
    ApprovalChoice,
    ApprovalRequest,
    CodingAgent,
    Question,
    Workspace,
)
from museglass.agent.muse_msp.client import HostExited, MspClient, MspError

log = logging.getLogger(__name__)

INSTALL_HINT = "install Muse Code: curl -fsSL https://dev.meta.ai/install.sh | bash, then run `muse` once and sign in"


def find_muse_binary() -> str | None:
    env = os.environ.get("MUSE_BIN")
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    found = shutil.which("muse")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "muse"
    return str(candidate) if candidate.exists() else None


class MuseCodeAgent(CodingAgent):
    name = "muse"

    def __init__(
        self,
        binary: str | None = None,
        *,
        approval_mode: str = "promptUnmatched",
        model_id: str | None = None,
        reasoning_effort: str | None = None,
        serve_args: list[str] | None = None,
    ) -> None:
        self.binary = binary or find_muse_binary()
        self.approval_mode = approval_mode
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.serve_args = serve_args or ["serve"]

    async def health(self) -> AgentHealth:
        if not self.binary:
            return AgentHealth(False, f"`muse` binary not found ({INSTALL_HINT})")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (OSError, asyncio.TimeoutError) as exc:
            return AgentHealth(False, f"could not run {self.binary} --version: {exc}")
        version = out.decode(errors="replace").strip().splitlines()[-1] if out else ""
        if proc.returncode != 0:
            return AgentHealth(False, f"`muse --version` failed: {version}")
        return AgentHealth(True, "muse binary present (sign-in is checked when a session starts)", version=version)

    async def create_session(self, workspace: Workspace, *, resume_id: str | None = None) -> AgentSession:
        if not self.binary:
            raise RuntimeError(f"muse binary not found; {INSTALL_HINT}")
        client = MspClient([self.binary, *self.serve_args], cwd=workspace.root)
        await client.start()
        session = MuseSession(client, workspace, approval_mode=self.approval_mode, model_id=self.model_id,
                              reasoning_effort=self.reasoning_effort)
        try:
            await session.open(resume_id=resume_id)
        except Exception:
            await client.close()
            raise
        return session


class MuseSession(AgentSession):
    def __init__(self, client: MspClient, workspace: Workspace, *, approval_mode: str, model_id: str | None,
                 reasoning_effort: str | None) -> None:
        self.client = client
        self.workspace = workspace
        self.approval_mode = approval_mode
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.session_id: str | None = None
        self.active_turn_id: str | None = None
        self.view_cursor: str | None = None
        self._busy = False
        self._paused = False
        self._events: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._items: dict[str, dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._questions: dict[str, dict[str, Any]] = {}
        self._seen_requests: set[str] = set()
        self._last_message_by_turn: dict[str, str] = {}
        self._turn_done: asyncio.Event = asyncio.Event()
        self._turn_done.set()

    # -- lifecycle ---------------------------------------------------------------------
    async def open(self, *, resume_id: str | None) -> None:
        if resume_id:
            result = await self.client.command("session/resume", {"sessionId": resume_id, "excludeItems": True})
        else:
            params: dict[str, Any] = {"workspaceRoot": str(self.workspace.root), "approvalMode": self.approval_mode}
            if self.model_id:
                params["modelId"] = self.model_id
            result = await self.client.command("session/start", params)
        session = result.get("session") or {}
        self.session_id = session.get("sessionId")
        self.view_cursor = result.get("viewCursor")
        self.active_turn_id = session.get("activeTurnId")
        self._busy = session.get("status") == "running"
        if self._busy:
            self._turn_done.clear()
        self._pump = asyncio.create_task(self._pump_notifications(), name="muse-msp-pump")
        self._emit(AgentEvent(AgentEventKind.SESSION_READY, text=f"muse session {self.session_id}", raw=session))

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
        await self.client.close()
        await self._events.put(None)

    # -- interface ---------------------------------------------------------------------
    @property
    def backend_session_id(self) -> str | None:
        return self.session_id

    @property
    def is_busy(self) -> bool:
        return self._busy

    def _input(self, text: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": text}]

    async def send_instruction(self, text: str, *, steer: bool = False) -> str:
        assert self.session_id
        if steer and self._busy and self.active_turn_id:
            params: dict[str, Any] = {"sessionId": self.session_id, "expectedTurnId": self.active_turn_id, "input": self._input(text)}
            if self.reasoning_effort:
                params["reasoningEffort"] = self.reasoning_effort
            try:
                result = await self.client.command("turn/steer", params)
                return str(result.get("turnId") or self.active_turn_id)
            except MspError as exc:
                if exc.kind not in ("commandRejected", "notFound"):
                    raise
                log.info("turn/steer rejected (%s); falling back to turn/start", exc.kind)
        params = {"sessionId": self.session_id, "input": self._input(text), "ifBusy": "steer" if steer else "queue"}
        if self.reasoning_effort:
            params["reasoningEffort"] = self.reasoning_effort
        result = await self.client.command("turn/start", params)
        turn_id = str(result.get("turnId") or "")
        if result.get("disposition") == "started":
            self.active_turn_id = turn_id
            self._busy = True
            self._turn_done.clear()
        return turn_id

    async def interrupt(self) -> None:
        if not self.session_id or not self._busy:
            return
        params: dict[str, Any] = {"sessionId": self.session_id}
        if self.active_turn_id:
            params["turnId"] = self.active_turn_id
        with contextlib.suppress(MspError):
            await self.client.command("turn/interrupt", params)

    async def pause(self) -> None:
        self._paused = True
        await self.interrupt()

    async def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        await self.send_instruction("Continue the task you were working on from where you stopped. Do not start over.")

    async def cancel(self) -> None:
        if not self.session_id or not self._busy:
            return
        params: dict[str, Any] = {"sessionId": self.session_id}
        if self.active_turn_id:
            params["turnId"] = self.active_turn_id
        with contextlib.suppress(MspError):
            await self.client.command("turn/cancel", params)

    async def approve(self, request_id: str, choice_id: str | None = None) -> None:
        info = self._approvals.get(request_id)
        if not info:
            log.warning("approve: unknown approval %s", request_id)
            return
        choices = info["choices"]
        choice = None
        if choice_id:
            choice = next((c for c in choices if c["choiceId"] == choice_id), None)
        if choice is None:
            approved = [c for c in choices if str(c.get("decision", "")).startswith("approved")]
            approved.sort(key=lambda c: 0 if c.get("scope") == "once" else 1)
            choice = approved[0] if approved else None
        if choice is None:
            log.error("approval %s offers no approving choice: %s", request_id, choices)
            return
        await self._decide(info, choice["choiceId"], None)

    async def reject(self, request_id: str, feedback: str | None = None) -> None:
        info = self._approvals.get(request_id)
        if not info:
            log.warning("reject: unknown approval %s", request_id)
            return
        choices = [c for c in info["choices"] if str(c.get("decision", "")) in ("denied", "abort", "deniedPolicyAmendment")]
        choices.sort(key=lambda c: 0 if c.get("acceptsFeedback") else 1)
        if not choices:
            log.error("approval %s offers no denying choice", request_id)
            return
        choice = choices[0]
        await self._decide(info, choice["choiceId"], feedback if choice.get("acceptsFeedback") else None)

    async def _decide(self, info: dict[str, Any], choice_id: str, feedback: str | None) -> None:
        params: dict[str, Any] = {
            "sessionId": self.session_id,
            "approvalId": info["approvalId"],
            "requirementId": info["requirementId"],
            "choiceId": choice_id,
        }
        if feedback:
            params["feedback"] = feedback[:500]
        try:
            await self.client.command("approval/decide", params)
        except MspError as exc:
            if exc.kind == "approvalRequirementStale" and exc.data.get("currentRequirementId"):
                params["requirementId"] = exc.data["currentRequirementId"]
                await self.client.command("approval/decide", params)
            elif exc.kind != "approvalAlreadyResolved":
                raise

    async def answer_question(self, question_id: str, answer: str) -> None:
        info = self._questions.get(question_id)
        if not info:
            log.warning("answer_question: unknown prompt %s", question_id)
            return
        answers = []
        needs_clarify = False
        for q in info["questions"]:
            labels = [o.get("label", "") for o in q.get("options", [])]
            match = _match_option(answer, labels)
            mode = (q.get("selection") or {}).get("mode", "single")
            if match and mode == "multiple":
                answers.append({"questionId": q["id"], "selectedLabels": [match]})
            elif match:
                answers.append({"questionId": q["id"], "selectedLabel": match})
            elif labels:
                needs_clarify = True
            else:
                answers.append({"questionId": q["id"], "freeText": answer[:500]})
        params: dict[str, Any] = {"sessionId": self.session_id, "userInputId": question_id}
        if needs_clarify or not answers:
            params["clarification"] = {"format": "text", "content": answer[:500]}
            await self.client.command("userInput/clarify", params)
        else:
            params["answers"] = answers
            await self.client.command("userInput/answer", params)

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    # -- translation -------------------------------------------------------------------
    def _emit(self, ev: AgentEvent) -> None:
        self._events.put_nowait(ev)

    async def _pump_notifications(self) -> None:
        while True:
            note = await self.client.notifications.get()
            try:
                self._translate(note)
            except Exception:  # noqa: BLE001
                log.exception("failed to translate MSP notification %s", note.get("method"))

    def _translate(self, note: dict[str, Any]) -> None:
        method = note.get("method")
        params = note.get("params") or {}
        if params.get("sessionId") and self.session_id and params["sessionId"] != self.session_id:
            return  # another session's view (should not happen: one host per session)
        if params.get("viewCursor"):
            self.view_cursor = params["viewCursor"]
        if method == "turn/started":
            self.active_turn_id = params.get("turnId")
            self._busy = True
            self._turn_done.clear()
            self._emit(AgentEvent(AgentEventKind.TURN_STARTED, turn_id=self.active_turn_id, raw=params))
        elif method == "turn/completed":
            turn_id = params.get("turnId")
            terminal = params.get("terminal")
            self._busy = False
            self._turn_done.set()
            if self.active_turn_id == turn_id:
                self.active_turn_id = None
            text = self._last_message_by_turn.pop(turn_id, "") if turn_id else ""
            usage = params.get("usage")
            if terminal == "completed":
                self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text=text, turn_id=turn_id, usage=usage, success=True, raw=params))
            elif terminal == "cancelled":
                self._emit(AgentEvent(AgentEventKind.TURN_CANCELLED, text=params.get("reason") or "", turn_id=turn_id, raw=params))
            else:
                err = params.get("error") or {}
                self._emit(AgentEvent(AgentEventKind.TURN_FAILED, text=err.get("message") or params.get("reason") or "turn failed", turn_id=turn_id, success=False, raw=params))
        elif method in ("item/started", "item/updated", "item/completed"):
            self._on_item(method, params)
        elif method == "item/delta":
            item = self._items.setdefault(params.get("itemId", ""), {"kind": "unknown", "text": "", "output": ""})
            field = params.get("field") or "text"
            delta = params.get("delta") or ""
            if field == "text":
                item["text"] = item.get("text", "") + delta
                if item.get("kind") == "agentMessage":
                    self._emit(AgentEvent(AgentEventKind.MESSAGE_DELTA, text=delta, turn_id=item.get("turnId")))
            elif field == "output":
                item["output"] = item.get("output", "") + delta
        elif method in ("approval/requested", "approval/request"):
            self._on_approval_request(params)
        elif method == "approval/resolved":
            approval_id = params.get("approvalId", "")
            info = self._approvals.pop(approval_id, None)
            req = ApprovalRequest(approval_id, info.get("toolName", "") if info else "", "", "")
            self._emit(AgentEvent(AgentEventKind.APPROVAL_RESOLVED, approval=req, success=params.get("policyResult") == "allow", raw=params))
        elif method in ("userInput/requested", "userInput/request"):
            self._on_user_input_request(params)
        elif method == "userInput/settled":
            self._questions.pop(params.get("userInputId", ""), None)
            self._emit(AgentEvent(AgentEventKind.QUESTION_SETTLED, text=params.get("outcome", ""), raw=params))
        elif method == "session/tokenUsage":
            cumulative = params.get("cumulative") or {}
            self._emit(AgentEvent(AgentEventKind.TOKEN_USAGE, usage={
                "prompt_tokens": params.get("promptTokens"), "output_tokens": (params.get("usage") or {}).get("outputTokens"),
                "total_tokens": params.get("totalTokens"), "cumulative_total_tokens": cumulative.get("totalTokens"),
                "model_id": params.get("modelId"),
            }, raw=params))
        elif method == "session/goalChanged":
            goal = params.get("goal") or {}
            self._emit(AgentEvent(AgentEventKind.STATUS, text=f"goal: {goal.get('objective', '')} ({goal.get('status', '')})", raw=params))
        elif method == "session/todoListChanged":
            items = params.get("items") or []
            done = sum(1 for i in items if i.get("status") == "completed")
            self._emit(AgentEvent(AgentEventKind.STATUS, text=f"plan: {done}/{len(items)} steps done", raw=params))
        elif method == "view/gap":
            self._emit(AgentEvent(AgentEventKind.STATUS, text="some agent events were dropped by the host", raw=params))
        elif method == "_host_exited":
            self._busy = False
            self._turn_done.set()
            self._emit(AgentEvent(AgentEventKind.ERROR, text=f"Muse host exited (code {params.get('code')}). {params.get('stderr', '')[-300:]}", raw=params))
            self._events.put_nowait(None)
        elif method == "turn/retryScheduled":
            self._emit(AgentEvent(AgentEventKind.STATUS, text=f"model retry {params.get('nextAttempt')}/{params.get('maxAttempts')}: {params.get('reason', '')}", raw=params))
        elif method == "session/started":
            return
        # other notifications (modelChanged, branchChanged, contextUsage, approvalModeChanged) are ignored

    def _on_item(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        item_id = item.get("itemId", "")
        kind = item.get("kind", "unknown")
        turn_id = item.get("turnId")
        state = self._items.setdefault(item_id, {"kind": kind, "text": "", "output": ""})
        state["kind"] = kind
        state["turnId"] = turn_id
        if kind == "toolCall":
            tool = item.get("tool") or "tool"
            tool_input = _parse_args(item.get("args"))
            if method == "item/started":
                self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool=tool, tool_input=tool_input, turn_id=turn_id, raw=item))
            elif method == "item/completed":
                output = item.get("visibleOutput") or state.get("output") or ""
                status = item.get("status")
                self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool=tool, tool_input=tool_input, tool_output=output,
                                      success=status == "completed", turn_id=turn_id, raw=item))
                self._items.pop(item_id, None)
        elif kind == "agentMessage":
            if method == "item/completed":
                text = item.get("text") or state.get("text") or ""
                if turn_id:
                    self._last_message_by_turn[turn_id] = text
                self._emit(AgentEvent(AgentEventKind.MESSAGE, text=text, turn_id=turn_id, raw={"itemId": item_id}))
                self._items.pop(item_id, None)
        elif kind == "reasoning":
            if method == "item/completed":
                summary = " ".join(item.get("summary") or []) or item.get("text") or ""
                if summary:
                    self._emit(AgentEvent(AgentEventKind.REASONING, text=summary, turn_id=turn_id))
                self._items.pop(item_id, None)
        elif kind == "subagent":
            if method == "item/started":
                self._emit(AgentEvent(AgentEventKind.STATUS, text=f"delegating to a subagent: {item.get('objective', '')}", turn_id=turn_id, raw=item))
            elif method == "item/completed":
                self._emit(AgentEvent(AgentEventKind.STATUS, text=f"subagent finished: {(item.get('result') or {}).get('summary', '')}", turn_id=turn_id, raw=item))
        elif kind in ("userMessage", "userShell", "compaction", "workflow", "reminderChild"):
            if method == "item/completed":
                self._items.pop(item_id, None)
        else:
            if method == "item/completed":
                self._emit(AgentEvent(AgentEventKind.STATUS, text=item.get("fallbackText") or f"{kind} {item.get('status', '')}", turn_id=turn_id, raw=item))
                self._items.pop(item_id, None)

    def _on_approval_request(self, params: dict[str, Any]) -> None:
        approval_id = params.get("approvalId", "")
        requirement = params.get("currentRequirementId") or {"approvalId": approval_id, "sourceIndex": 0}
        key = f"{approval_id}:{requirement.get('sourceIndex', 0)}"
        if key in self._seen_requests:
            return  # `approval/requested` notification and `approval/request` server request both arrive
        self._seen_requests.add(key)
        choices_raw = params.get("availableChoices") or []
        self._approvals[approval_id] = {"approvalId": approval_id, "requirementId": requirement, "choices": choices_raw,
                                        "toolName": params.get("toolName", "")}
        subject = params.get("subject") or {}
        kind = str(subject.get("kind") or "tool")
        detail = subject.get("command") or subject.get("path") or subject.get("target") or subject.get("host") or params.get("rawArgs") or ""
        tool_input: dict[str, Any] = {}
        if subject.get("command"):
            tool_input["command"] = subject["command"]
        elif subject.get("path"):
            tool_input["path"] = subject["path"]
            tool_input["access"] = subject.get("access")
        else:
            tool_input = _parse_args(params.get("rawArgs")) or {}
        summary = _approval_summary(kind, params.get("toolName", ""), detail)
        choices = [ApprovalChoice(c.get("choiceId", ""), c.get("label", ""), c.get("decision", ""), bool(c.get("acceptsFeedback"))) for c in choices_raw]
        req = ApprovalRequest(request_id=approval_id, tool_name=params.get("toolName", ""), kind=kind, summary=summary,
                              detail=str(detail), choices=choices, raw={"subject": subject, "rawArgs": params.get("rawArgs"),
                                                                       "tool_input": tool_input, "protectedWrite": params.get("protectedWrite")})
        self._emit(AgentEvent(AgentEventKind.APPROVAL_REQUESTED, approval=req, tool=params.get("toolName"), tool_input=tool_input,
                              turn_id=params.get("turnId"), raw=params))

    def _on_user_input_request(self, params: dict[str, Any]) -> None:
        user_input_id = params.get("userInputId", "")
        if f"ui:{user_input_id}" in self._seen_requests:
            return
        self._seen_requests.add(f"ui:{user_input_id}")
        questions = params.get("questions") or []
        self._questions[user_input_id] = {"questions": questions}
        texts = [q.get("question", "") for q in questions]
        first = questions[0] if questions else {}
        options = [o.get("label", "") for o in first.get("options", [])]
        question = Question(question_id=user_input_id, text=" ".join(t for t in texts if t), options=options,
                            multiple=(first.get("selection") or {}).get("mode") == "multiple", header=first.get("header", ""), raw=params)
        self._emit(AgentEvent(AgentEventKind.QUESTION, question=question, turn_id=params.get("turnId"), raw=params))


def _parse_args(args: Any) -> dict[str, Any] | None:
    if args is None:
        return None
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args)
    except (TypeError, ValueError):
        return {"raw": str(args)}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def _approval_summary(kind: str, tool_name: str, detail: Any) -> str:
    detail_text = str(detail)
    if len(detail_text) > 90:
        detail_text = detail_text[:87] + "…"
    if kind == "shell":
        return f"I want to run: {detail_text}. Approve?"
    if kind == "fileAccess":
        return f"I want to access {detail_text}. Approve?"
    if kind == "network":
        return f"I want to reach {detail_text} over the network. Approve?"
    return f"I need approval to use {tool_name or kind}: {detail_text}. Approve?"


def _match_option(answer: str, labels: list[str]) -> str | None:
    a = answer.strip().lower()
    if not a:
        return None
    for label in labels:
        if a == label.lower():
            return label
    for label in labels:
        if label.lower() in a or a in label.lower():
            return label
    yes = a.startswith(("yes", "yeah", "yep", "ok", "sure"))
    no = a.startswith(("no", "nope", "nah"))
    for label in labels:
        if (yes and label.lower() in ("yes", "y", "approve", "allow")) or (no and label.lower() in ("no", "n", "deny", "reject")):
            return label
    return None


def muse_version(binary: str) -> str:
    try:
        return subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _unused() -> str:  # keep uuid imported for future retract ids without lint noise
    return uuid.uuid4().hex
