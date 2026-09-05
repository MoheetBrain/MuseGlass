"""A deterministic demo agent that performs the canonical MuseGlass task for real.

No LLM. It edits real files in the workspace, runs the workspace's real test suite with
pytest, honours mid-task steers ("also include uptime"), can be interrupted and resumed, asks
for approval before `git push`, and commits when told to. It exists so the whole voice loop
can be exercised end-to-end without credentials — and so the end-to-end test proves the
system with real file changes and real tests rather than a mocked conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

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

HEALTH_BLOCK = '''

# --- health endpoint (added by Muse) -------------------------------------------------
import subprocess as _subprocess
import time as _time

_START_TIME = _time.monotonic()


def git_sha() -> str:
    """Current commit SHA, or "unknown" when not in a git repository."""
    try:
        out = _subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, _subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


@app.get("/health")
def health() -> dict:
    payload = {"status": "ok", "git_sha": git_sha(), "version": __version__}
{UPTIME_LINE}    return payload
'''

UPTIME_LINE = '    payload["uptime_seconds"] = round(_time.monotonic() - _START_TIME, 3)\n'

HEALTH_TESTS = '''from fastapi.testclient import TestClient

from app import __version__
from app.main import app

client = TestClient(app)


def test_health_reports_ok_status():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_includes_git_sha():
    body = client.get("/health").json()
    sha = body["git_sha"]
    assert sha == "unknown" or (7 <= len(sha) <= 40 and all(c in "0123456789abcdef" for c in sha))


def test_health_includes_app_version():
    assert client.get("/health").json()["version"] == __version__
{UPTIME_TEST}'''

UPTIME_TEST = '''

def test_health_includes_uptime():
    body = client.get("/health").json()
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0
'''


class ScriptedDemoAgent(CodingAgent):
    name = "scripted-demo"

    def __init__(self, *, step_delay: float = 0.05) -> None:
        self.step_delay = step_delay

    async def health(self) -> AgentHealth:
        return AgentHealth(available=True, reason="deterministic demo agent (no LLM)", version="1")

    async def create_session(self, workspace: Workspace, *, resume_id: str | None = None) -> AgentSession:
        session = ScriptedSession(workspace, step_delay=self.step_delay, session_id=resume_id)
        return session


class ScriptedSession(AgentSession):
    def __init__(self, workspace: Workspace, *, step_delay: float, session_id: str | None) -> None:
        self.workspace = workspace
        self.step_delay = step_delay
        self._id = session_id or uuid.uuid4().hex
        self._events: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._steers: list[str] = []
        self._pending_approvals: dict[str, asyncio.Future[tuple[bool, str | None]]] = {}
        self._include_uptime = False
        self._include_version = True
        self._paused = False
        self._resume_from: int = 0
        self._current_instruction: str | None = None
        self._turn_id: str | None = None
        self._closed = False
        self._cancel_reported = False
        self._events.put_nowait(AgentEvent(AgentEventKind.SESSION_READY, text="scripted session ready"))

    # -- interface ---------------------------------------------------------------------
    @property
    def backend_session_id(self) -> str | None:
        return self._id

    @property
    def is_busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def send_instruction(self, text: str, *, steer: bool = False) -> str:
        if steer and self.is_busy:
            self._steers.append(text)
            self._apply_steer_flags(text)
            self._emit(AgentEvent(AgentEventKind.MESSAGE, text=self._ack_for_steer(text), turn_id=self._turn_id))
            return self._turn_id or ""
        if self.is_busy:
            # queue behind the current turn: simplest correct behaviour for the demo
            await self._task  # type: ignore[arg-type]
        self._turn_id = uuid.uuid4().hex
        self._current_instruction = text
        self._resume_from = 0
        self._task = asyncio.create_task(self._run(text, self._turn_id))
        return self._turn_id

    async def interrupt(self) -> None:
        if self._task and not self._task.done():
            self._cancel_reported = False
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            if not self._cancel_reported:
                # the coroutine never ran (cancelled before its first step); report it anyway
                self._emit(AgentEvent(AgentEventKind.TURN_CANCELLED, turn_id=self._turn_id))

    async def pause(self) -> None:
        self._paused = True
        await self.interrupt()

    async def resume(self) -> None:
        if self._paused and self._current_instruction and not self.is_busy:
            self._paused = False
            self._turn_id = uuid.uuid4().hex
            self._task = asyncio.create_task(self._run(self._current_instruction, self._turn_id, start_step=self._resume_from))

    async def cancel(self) -> None:
        await self.interrupt()

    async def approve(self, request_id: str, choice_id: str | None = None) -> None:
        fut = self._pending_approvals.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result((True, choice_id))
            self._emit(AgentEvent(AgentEventKind.APPROVAL_RESOLVED, approval=ApprovalRequest(request_id, "", "", ""), success=True))

    async def reject(self, request_id: str, feedback: str | None = None) -> None:
        fut = self._pending_approvals.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result((False, feedback))
            self._emit(AgentEvent(AgentEventKind.APPROVAL_RESOLVED, approval=ApprovalRequest(request_id, "", "", ""), success=False))

    async def answer_question(self, question_id: str, answer: str) -> None:
        # The scripted agent never asks free questions; approvals cover the demo.
        self._emit(AgentEvent(AgentEventKind.QUESTION_SETTLED, text=answer))

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        self._closed = True
        await self.interrupt()
        await self._events.put(None)

    # -- the script --------------------------------------------------------------------
    def _emit(self, ev: AgentEvent) -> None:
        if not self._closed:
            self._events.put_nowait(ev)

    def _apply_steer_flags(self, text: str) -> None:
        t = text.lower()
        if "uptime" in t:
            self._include_uptime = "don't" not in t and "do not" not in t and "without" not in t
        if "version" in t:
            self._include_version = "don't" not in t and "do not" not in t and "without" not in t

    @staticmethod
    def _ack_for_steer(text: str) -> str:
        t = text.lower()
        if "uptime" in t:
            return "Got it. I'll include uptime as well."
        if "version" in t:
            return "Got it. I'll return the app version too."
        if "why" in t:
            return "I'm adding the endpoint in the main module because that's where the app and its routes live."
        if "minimal" in t or "small" in t:
            return "Understood. Keeping the patch minimal."
        return "Noted. I'll take that into account."

    async def _run(self, instruction: str, turn_id: str, *, start_step: int = 0) -> None:
        self._emit(AgentEvent(AgentEventKind.TURN_STARTED, turn_id=turn_id))
        text = instruction.lower()
        try:
            if "health" in text or "endpoint" in text:
                self._apply_steer_flags(text)
                await self._health_task(turn_id, start_step)
            elif "commit" in text:
                await self._commit_task(turn_id, push="push" in text and not _negated_push(text))
            elif "push" in text:
                await self._push_task(turn_id)
            elif "undo" in text:
                await self._undo_task(turn_id)
            elif "changed" in text or "describe" in text or "diff" in text:
                await self._describe_changes(turn_id)
            elif "test" in text:
                await self._run_tests(turn_id)
            else:
                self._emit(AgentEvent(AgentEventKind.MESSAGE, text="The demo agent only knows the health-endpoint task, testing, committing and pushing.", turn_id=turn_id))
                self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text="I can only do the demo task.", turn_id=turn_id))
        except asyncio.CancelledError:
            self._cancel_reported = True
            self._emit(AgentEvent(AgentEventKind.TURN_CANCELLED, turn_id=turn_id))
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit(AgentEvent(AgentEventKind.TURN_FAILED, text=str(exc), turn_id=turn_id))

    async def _step(self) -> None:
        await asyncio.sleep(self.step_delay)

    async def _health_task(self, turn_id: str, start_step: int) -> None:
        root = self.workspace.root
        main_py = root / "app" / "main.py"
        tests_dir = root / "tests"
        steps = ["explore", "edit", "tests", "run"]
        for index, step in enumerate(steps):
            if index < start_step:
                continue
            self._resume_from = index
            if step == "explore":
                for rel in ("app/main.py", "app/__init__.py", "tests/test_app.py"):
                    self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="read_file", tool_input={"path": rel}, turn_id=turn_id))
                    await self._step()
                    content = (root / rel).read_text() if (root / rel).exists() else ""
                    self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="read_file", tool_input={"path": rel}, tool_output=content[:200], success=True, turn_id=turn_id))
                self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="grep", tool_input={"pattern": "@app.get", "path": "app"}, turn_id=turn_id))
                await self._step()
                self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="grep", tool_input={"pattern": "@app.get"}, tool_output="app/main.py: @app.get(\"/\")", success=True, turn_id=turn_id))
            elif step == "edit":
                self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="edit_file", tool_input={"path": "app/main.py"}, turn_id=turn_id))
                await self._step()
                self._write_endpoint(main_py)
                self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="edit_file", tool_input={"path": "app/main.py"}, tool_output="added /health", success=True, turn_id=turn_id))
            elif step == "tests":
                self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="write_file", tool_input={"path": "tests/test_health.py"}, turn_id=turn_id))
                await self._step()
                # a late "also include uptime" steer still lands here
                self._write_endpoint(main_py)
                tests_dir.mkdir(exist_ok=True)
                (tests_dir / "test_health.py").write_text(HEALTH_TESTS.replace("{UPTIME_TEST}", UPTIME_TEST if self._include_uptime else ""))
                self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="write_file", tool_input={"path": "tests/test_health.py"}, tool_output="wrote tests", success=True, turn_id=turn_id))
            elif step == "run":
                output, ok = await self._pytest(turn_id)
                stats = _parse_pytest(output)
                n_health = 4 if self._include_uptime else 3
                if ok:
                    summary = f"I added the health endpoint and {n_health} tests. All {stats['passed']} tests pass." if stats else "I added the health endpoint and its tests. All tests pass."
                else:
                    summary = f"I added the endpoint but {stats['failed'] if stats else 'some'} tests fail."
                self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text=summary, turn_id=turn_id, success=ok))
                return

    def _write_endpoint(self, main_py: Path) -> None:
        source = main_py.read_text()
        marker = "# --- health endpoint (added by Muse)"
        if marker in source:
            source = source[: source.index("\n\n" + marker)] if "\n\n" + marker in source else source[: source.index(marker)]
        block = HEALTH_BLOCK.replace("{UPTIME_LINE}", UPTIME_LINE if self._include_uptime else "")
        if not self._include_version:
            block = block.replace(', "version": __version__', "")
        main_py.write_text(source.rstrip("\n") + "\n" + block)

    async def _pytest(self, turn_id: str) -> tuple[str, bool]:
        cmd = f"{Path(sys.executable).name} -m pytest -q"
        self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="bash", tool_input={"command": cmd}, turn_id=turn_id))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            cwd=self.workspace.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.CancelledError:
            proc.kill()
            raise
        output = out.decode(errors="replace")
        ok = proc.returncode == 0
        self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": cmd}, tool_output=output[-2000:], success=ok, turn_id=turn_id))
        return output, ok

    async def _run_tests(self, turn_id: str) -> None:
        output, ok = await self._pytest(turn_id)
        stats = _parse_pytest(output)
        text = ("All tests pass." if ok else "Some tests fail.") + (f" {stats['passed']} passed, {stats['failed']} failed." if stats else "")
        self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text=text, turn_id=turn_id, success=ok))

    async def _commit_task(self, turn_id: str, *, push: bool) -> None:
        root = self.workspace.root
        message = "Add /health endpoint with git SHA, app version" + (" and uptime" if self._include_uptime else "") + " plus tests"
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", message]):
            shown = " ".join(cmd if cmd[0:2] != ["git", "commit"] else ["git", "commit", "-m", f'"{message}"'])
            self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="bash", tool_input={"command": shown}, turn_id=turn_id))
            await self._step()
            result = await asyncio.to_thread(subprocess.run, cmd, cwd=root, capture_output=True, text=True)
            ok = result.returncode == 0
            self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": shown}, tool_output=(result.stdout + result.stderr)[-1000:], success=ok, turn_id=turn_id))
            if not ok:
                self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text=f"The commit failed: {(result.stderr or result.stdout).strip()[:160]}", turn_id=turn_id, success=False))
                return
        if push:
            pushed = await self._request_push(turn_id)
            self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text="Committed locally and pushed." if pushed else "Committed locally. I did not push.", turn_id=turn_id, success=True))
            return
        self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text="Committed locally. Nothing was pushed.", turn_id=turn_id, success=True))

    async def _push_task(self, turn_id: str) -> None:
        pushed = await self._request_push(turn_id)
        self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text="Pushed." if pushed else "Understood. I did not push.", turn_id=turn_id, success=True))

    async def _request_push(self, turn_id: str) -> bool:
        """Ask for approval exactly like a real backend would; run `git push` only if allowed."""
        request_id = uuid.uuid4().hex
        command = "git push origin HEAD"
        req = ApprovalRequest(
            request_id=request_id, tool_name="bash", kind="shell",
            summary=f"I want to run {command}. Approve?", detail=command,
            choices=[ApprovalChoice("allow_once", "Allow once", "approved"), ApprovalChoice("abort", "Reject", "abort", accepts_feedback=True)],
            raw={"tool_input": {"command": command}, "subject": {"kind": "shell", "command": command}},
        )
        fut: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        self._pending_approvals[request_id] = fut
        self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="bash", tool_input={"command": command}, turn_id=turn_id))
        self._emit(AgentEvent(AgentEventKind.APPROVAL_REQUESTED, approval=req, tool="bash", tool_input={"command": command}, turn_id=turn_id))
        approved, feedback = await fut
        if not approved:
            self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": command}, tool_output=f"rejected: {feedback or 'no'}", success=False, turn_id=turn_id))
            return False
        result = await asyncio.to_thread(subprocess.run, ["git", "push", "origin", "HEAD"], cwd=self.workspace.root, capture_output=True, text=True)
        self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": command}, tool_output=(result.stdout + result.stderr)[-500:], success=result.returncode == 0, turn_id=turn_id))
        return result.returncode == 0

    async def _undo_task(self, turn_id: str) -> None:
        root = self.workspace.root
        self._emit(AgentEvent(AgentEventKind.TOOL_STARTED, tool="bash", tool_input={"command": "git checkout -- app/main.py"}, turn_id=turn_id))
        result = await asyncio.to_thread(subprocess.run, ["git", "checkout", "--", "app/main.py"], cwd=root, capture_output=True, text=True)
        test_file = root / "tests" / "test_health.py"
        if test_file.exists():
            test_file.unlink()
        self._emit(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": "git checkout -- app/main.py"}, tool_output=result.stdout + result.stderr, success=result.returncode == 0, turn_id=turn_id))
        self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text="I reverted the health endpoint and removed its tests.", turn_id=turn_id, success=True))

    async def _describe_changes(self, turn_id: str) -> None:
        result = await asyncio.to_thread(subprocess.run, ["git", "status", "--porcelain"], cwd=self.workspace.root, capture_output=True, text=True)
        files = [line[3:] for line in result.stdout.splitlines() if line.strip()]
        text = f"{len(files)} files changed: {', '.join(files[:4])}." if files else "No uncommitted changes."
        self._emit(AgentEvent(AgentEventKind.TURN_COMPLETED, text=text, turn_id=turn_id, success=True))


def _parse_pytest(output: str) -> dict | None:
    m = None
    for m in re.finditer(r"(?:(\d+) failed,? )?(\d+) passed", output):
        pass
    if m:
        return {"passed": int(m.group(2)), "failed": int(m.group(1) or 0)}
    m = re.search(r"(\d+) failed", output)
    if m:
        return {"passed": 0, "failed": int(m.group(1))}
    return None


def _negated_push(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in ("don't push", "do not push", "dont push", "no push", "without pushing", "not push"))


def is_git_available() -> bool:
    return shutil.which("git") is not None


def started_at() -> float:
    return time.monotonic()
