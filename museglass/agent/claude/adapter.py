"""Claude Code backend via `claude-agent-sdk` (second backend; proves the abstraction).

Workarounds (documented): the SDK has no mid-turn message injection, so `steer` is
`interrupt()` + a re-instruction that tells the model to continue the same task. Approvals use
the `can_use_tool` permission callback, so MuseGlass's policy engine sees every tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from typing import Any

from museglass.agent.interface import (
    AgentEvent,
    AgentEventKind,
    AgentHealth,
    AgentSession,
    ApprovalChoice,
    ApprovalRequest,
    CodingAgent,
    Workspace,
)

log = logging.getLogger(__name__)

VOICE_SYSTEM_PROMPT = (
    "You are being operated hands-free by voice through MuseGlass. The user cannot see your "
    "screen. Keep every message to one or two short plain sentences that make sense when read "
    "aloud: no code, no file paths unless essential, no lists. Announce meaningful milestones "
    "only. When you need a decision, ask one short yes/no question. Instructions prefixed with "
    "[voice update] arrived while you were working: incorporate them into the current task and "
    "continue without starting over."
)

_KIND_BY_TOOL = {"bash": "shell", "edit": "fileAccess", "write": "fileAccess", "multiedit": "fileAccess", "notebookedit": "fileAccess",
                 "webfetch": "network", "websearch": "network", "read": "fileAccess", "glob": "fileAccess", "grep": "fileAccess"}


class ClaudeCodeAgent(CodingAgent):
    name = "claude"

    def __init__(self, *, model: str | None = None, cli_path: str | None = None) -> None:
        self.model = model
        self.cli_path = cli_path or shutil.which("claude")

    async def health(self) -> AgentHealth:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return AgentHealth(False, "claude-agent-sdk is not installed (pip install claude-agent-sdk)")
        if not self.cli_path:
            return AgentHealth(False, "`claude` CLI not found (npm install -g @anthropic-ai/claude-code)")
        try:
            proc = await asyncio.create_subprocess_exec(self.cli_path, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (OSError, asyncio.TimeoutError) as exc:
            return AgentHealth(False, f"could not run `claude auth status`: {exc}")
        text = out.decode(errors="replace")
        try:
            status = json.loads(text[text.index("{"):])
        except (ValueError, json.JSONDecodeError):
            status = {}
        if not status.get("loggedIn"):
            return AgentHealth(False, "Claude Code is not signed in: run `claude auth login` (or set ANTHROPIC_API_KEY)")
        return AgentHealth(True, f"signed in via {status.get('authMethod', 'unknown')}")

    async def create_session(self, workspace: Workspace, *, resume_id: str | None = None) -> AgentSession:
        session = ClaudeSession(workspace, model=self.model, resume_id=resume_id, cli_path=self.cli_path)
        await session.open()
        return session


class ClaudeSession(AgentSession):
    def __init__(self, workspace: Workspace, *, model: str | None, resume_id: str | None, cli_path: str | None) -> None:
        self.workspace = workspace
        self.model = model
        self.resume_id = resume_id
        self.cli_path = cli_path
        self._client: Any = None
        self._session_id: str | None = resume_id
        self._events: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._busy = False
        self._paused = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._pending: dict[str, asyncio.Future[tuple[bool, str | None]]] = {}
        self._tool_names: dict[str, tuple[str, dict[str, Any]]] = {}
        self._turn_id: str | None = None
        self._last_text = ""

    async def open(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(
            cwd=str(self.workspace.root),
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            resume=self.resume_id,
            model=self.model,
            cli_path=self.cli_path,
            setting_sources=[],
            system_prompt={"type": "preset", "preset": "claude_code", "append": VOICE_SYSTEM_PROMPT},
            include_partial_messages=False,
        )
        self._client = ClaudeSDKClient(options)
        await self._client.connect()
        self._pump = asyncio.create_task(self._pump_messages(), name="claude-pump")
        info = None
        with contextlib.suppress(Exception):
            info = await self._client.get_server_info()
        if isinstance(info, dict) and info.get("session_id"):
            self._session_id = str(info["session_id"])
        self._emit(AgentEvent(AgentEventKind.SESSION_READY, text=f"claude session {self._session_id or 'pending'}"))

    # -- interface ---------------------------------------------------------------------
    @property
    def backend_session_id(self) -> str | None:
        return self._session_id

    @property
    def is_busy(self) -> bool:
        return self._busy

    async def send_instruction(self, text: str, *, steer: bool = False) -> str:
        if steer and self._busy:
            await self._client.interrupt()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._turn_done.wait(), timeout=20)
            text = f"[voice update] {text}"
        elif self._busy:
            await self._turn_done.wait()
        self._turn_id = uuid.uuid4().hex
        self._busy = True
        self._turn_done.clear()
        self._emit(AgentEvent(AgentEventKind.TURN_STARTED, turn_id=self._turn_id))
        await self._client.query(text)
        return self._turn_id

    async def interrupt(self) -> None:
        if self._busy and self._client:
            await self._client.interrupt()

    async def pause(self) -> None:
        self._paused = True
        await self.interrupt()

    async def resume(self) -> None:
        if self._paused:
            self._paused = False
            await self.send_instruction("Continue the task you were working on from where you stopped. Do not start over.")

    async def cancel(self) -> None:
        await self.interrupt()

    async def approve(self, request_id: str, choice_id: str | None = None) -> None:
        fut = self._pending.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result((True, None))

    async def reject(self, request_id: str, feedback: str | None = None) -> None:
        fut = self._pending.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result((False, feedback))

    async def answer_question(self, question_id: str, answer: str) -> None:
        # Claude asks questions in plain text; the answer is simply the next user message.
        await self.send_instruction(answer)

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        await self._events.put(None)

    # -- permissions -------------------------------------------------------------------
    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        request_id = getattr(context, "tool_use_id", None) or uuid.uuid4().hex
        kind = _KIND_BY_TOOL.get(tool_name.lower(), "tool")
        detail = str(tool_input.get("command") or tool_input.get("file_path") or tool_input.get("path") or tool_input.get("url") or "")
        summary = f"I want to run: {detail[:90]}. Approve?" if kind == "shell" else f"I want to use {tool_name} on {detail[:90]}. Approve?"
        req = ApprovalRequest(request_id=request_id, tool_name=tool_name, kind=kind, summary=summary, detail=detail,
                              choices=[ApprovalChoice("allow_once", "Allow once", "approved"), ApprovalChoice("deny", "Deny", "denied", True)],
                              raw={"tool_input": tool_input})
        fut: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        self._emit(AgentEvent(AgentEventKind.APPROVAL_REQUESTED, approval=req, tool=tool_name, tool_input=tool_input, turn_id=self._turn_id))
        approved, feedback = await fut
        self._emit(AgentEvent(AgentEventKind.APPROVAL_RESOLVED, approval=req, success=approved))
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message=feedback or "The user did not approve this action.")

    # -- translation -------------------------------------------------------------------
    def _emit(self, ev: AgentEvent) -> None:
        self._events.put_nowait(ev)

    async def _pump_messages(self) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

        try:
            async for msg in self._client.receive_messages():
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init":
                        sid = (msg.data or {}).get("session_id")
                        if sid:
                            self._session_id = str(sid)
                            self._emit(AgentEvent(AgentEventKind.SESSION_READY, text=f"claude session {sid}"))
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            self._last_text = block.text
                            self._emit(AgentEvent(AgentEventKind.MESSAGE, text=block.text, turn_id=self._turn_id))
                        elif isinstance(block, ToolUseBlock):
                            self._tool_names[block.id] = (block.name, dict(block.input or {}))
                            self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool=block.name, tool_input=dict(block.input or {}), turn_id=self._turn_id))
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            name, tool_input = self._tool_names.pop(block.tool_use_id, ("tool", {}))
                            self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool=name, tool_input=tool_input,
                                                  tool_output=_content_text(block.content), success=not bool(block.is_error), turn_id=self._turn_id))
                elif isinstance(msg, ResultMessage):
                    self._busy = False
                    self._turn_done.set()
                    if msg.session_id:
                        self._session_id = msg.session_id
                    usage = {"cost_usd": msg.total_cost_usd, **(msg.usage or {})} if (msg.usage or msg.total_cost_usd is not None) else None
                    if usage:
                        self._emit(AgentEvent(AgentEventKind.TOKEN_USAGE, usage=usage))
                    terminal = str(getattr(msg, "terminal_reason", "") or "")
                    if "abort" in terminal or "interrupt" in terminal:
                        self._emit(AgentEvent(AgentEventKind.TURN_CANCELLED, turn_id=self._turn_id))
                    elif msg.is_error:
                        self._emit(AgentEvent(AgentEventKind.TURN_FAILED, text=str(msg.result or "error"), turn_id=self._turn_id, success=False))
                    else:
                        self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text=str(msg.result or self._last_text or ""), turn_id=self._turn_id, success=True))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("claude message pump failed")
            self._busy = False
            self._turn_done.set()
            self._emit(AgentEvent(AgentEventKind.ERROR, text=str(exc)))
            self._events.put_nowait(None)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(content)
