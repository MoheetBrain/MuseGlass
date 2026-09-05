"""Minimal Muse Session Protocol (MSP v1) client over stdio.

Written from the published schema (`schema/msp/msp.d.ts`, fingerprint sha256:cfd31ee7…) and
validated against the recorded conformance transcripts in `tests/fixtures/msp/`.

Wire facts this client relies on:
- one JSON-RPC 2.0 object per line (`\\n`), UTF-8, frame limit 10 MiB by default;
- requests carry an `id`; notifications carry none; the server may send *requests*
  (`approval/request`, `userInput/request`) which must be answered with `{"result": {}}`;
- commands carry a client-minted UUIDv7 `commandId`; the ack is admission only;
- handshake: `initialize` → result → client notification `initialized`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_FRAME_LIMIT = 10 * 1024 * 1024
CLIENT_NAME = "museglass"
CLIENT_VERSION = "0.1.0"
STABLE_FINGERPRINT = "sha256:cfd31ee77d78fdada9febc4edccd29b0434ff8f6bf157c7c03fd0ecfcbc29f5a"


def uuid7() -> str:
    """RFC 9562 UUIDv7 (time-ordered), required for MSP command ids."""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


class MspError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
        self.data = data or {}

    @property
    def kind(self) -> str:
        return str(self.data.get("kind", ""))

    @property
    def retryable(self) -> bool:
        return bool(self.data.get("retryable", False))


class HostExited(MspError):
    def __init__(self, code: int | None, stderr_tail: str) -> None:
        super().__init__(-1, f"muse host exited (code={code})", {"kind": "hostExited", "stderr": stderr_tail})
        self.exit_code = code


class MspClient:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        request_timeout: float = 120.0,
        frame_limit: int = DEFAULT_FRAME_LIMIT,
        client_name: str = CLIENT_NAME,
        client_version: str = CLIENT_VERSION,
        requested_capabilities: list[str] | None = None,
    ) -> None:
        self.argv = list(command)
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.request_timeout = request_timeout
        self.frame_limit = frame_limit
        self.client_name = client_name
        self.client_version = client_version
        self.requested_capabilities = requested_capabilities or []
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.initialize_result: dict[str, Any] | None = None
        self.fingerprint_warning: str | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=50)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.frame_limit + 1024,
        )
        self._reader = asyncio.create_task(self._read_loop(), name="msp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="msp-stderr")
        params: dict[str, Any] = {"clientInfo": {"name": self.client_name, "version": self.client_version}}
        if self.requested_capabilities:
            params["capabilities"] = {"requestedCapabilities": self.requested_capabilities}
        result = await self.request("initialize", params)
        fingerprint = (result.get("schema") or {}).get("fingerprint")
        if not isinstance(fingerprint, str):
            raise MspError(-32600, "initialize result has no schema fingerprint")
        if fingerprint != STABLE_FINGERPRINT:
            self.fingerprint_warning = f"host schema fingerprint {fingerprint} differs from the bundle this client was built against"
            log.warning(self.fingerprint_warning)
        await self._write({"jsonrpc": "2.0", "method": "initialized"})
        self.initialize_result = result
        return result

    async def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc and proc.stdin and not proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                if proc.returncode is None:
                    proc.kill()
        for task in (self._reader, self._stderr_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_pending(HostExited(proc.returncode if proc else None, self._stderr_text()))

    @property
    def exited(self) -> bool:
        return self._proc is None or self._proc.returncode is not None

    # -- messaging ---------------------------------------------------------------------
    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        if self.exited and self._proc is not None:
            raise HostExited(self._proc.returncode, self._stderr_text())
        request_id = self._next_id
        self._next_id += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            frame["params"] = params
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        await self._write(frame)
        try:
            return await asyncio.wait_for(fut, timeout=timeout or self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise MspError(-32000, f"timeout waiting for {method}", {"kind": "timeout"}) from None

    async def command(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        merged = dict(params or {})
        merged.setdefault("commandId", uuid7())
        return await self.request(method, merged, timeout=timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            frame["params"] = params
        async def _send() -> None:
            with contextlib.suppress(HostExited):
                await self._write(frame)

        asyncio.get_running_loop().create_task(_send())

    async def _write(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise HostExited(proc.returncode if proc else None, self._stderr_text())
        line = json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            proc.stdin.write(line.encode("utf-8"))
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise HostExited(proc.returncode, self._stderr_text()) from exc

    # -- reading -----------------------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    log.error("MSP frame exceeded limit: %s", exc)
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("MSP: dropping unparseable frame: %.200s", line)
                    continue
                if not isinstance(frame, dict):
                    continue
                await self._handle_frame(frame)
        finally:
            await self._proc.wait()
            code = self._proc.returncode
            exc = HostExited(code, self._stderr_text())
            self._fail_pending(exc)
            await self.notifications.put({"method": "_host_exited", "params": {"code": code, "stderr": self._stderr_text()}})

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        has_id = "id" in frame
        if method is not None and has_id:
            # server-initiated request: acknowledge immediately, surface as a notification
            with contextlib.suppress(Exception):
                await self._write({"jsonrpc": "2.0", "id": frame["id"], "result": {}})
            await self.notifications.put({"method": method, "params": frame.get("params") or {}, "server_request": True})
            return
        if method is not None:
            await self.notifications.put({"method": method, "params": frame.get("params") or {}, "emittedAtMs": frame.get("emittedAtMs")})
            return
        if has_id:
            request_id = frame.get("id")
            fut = self._pending.pop(request_id, None) if isinstance(request_id, int) else None
            if fut is None or fut.done():
                log.debug("MSP: response for unknown id %r", request_id)
                return
            if "error" in frame and frame["error"] is not None:
                err = frame["error"] or {}
                fut.set_exception(MspError(int(err.get("code", -32603)), str(err.get("message", "error")), err.get("data") or {}))
            else:
                result = frame.get("result")
                fut.set_result(result if isinstance(result, dict) else {})

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                log.debug("muse stderr: %s", text)

    def _stderr_text(self) -> str:
        return "\n".join(self.stderr_tail)[-2000:]

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
