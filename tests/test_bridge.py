"""The WebSocket bridge: token auth, hello/welcome, replay after reconnect, live events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest
import uvicorn
import websockets

from museglass.agent.scripted import ScriptedDemoAgent
from museglass.bridge.server import create_app
from museglass.host.orchestrator import SessionOrchestrator
from museglass.protocol.events import PROTOCOL_VERSION, EventType
from museglass.speech.providers.null_tts import NullTTS
from museglass.store.sqlite import SessionStore
from tests.conftest import wait_until


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def bridge(demo_workspace):
    registry, _ = demo_workspace
    store = SessionStore(":memory:")
    orch = SessionOrchestrator(agent=ScriptedDemoAgent(step_delay=0.01), store=store, registry=registry, tts=NullTTS(), auto_commit_prompt=False)
    await orch.start()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(orch, "secret-token"), host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, timeout=10)
    yield orch, port
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, 5)
    await orch.close()


async def test_rejects_bad_token(bridge):
    _, port = bridge
    with pytest.raises(Exception):
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token=wrong") as ws:
            await ws.recv()


async def test_hello_welcome_live_events_and_replay(bridge):
    orch, port = bridge
    url = f"ws://127.0.0.1:{port}/ws?token=secret-token"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol_version": PROTOCOL_VERSION, "last_seq": 0}))
        welcome = json.loads(await ws.recv())
        assert welcome["type"] == "welcome" and welcome["session_id"] == orch.session_id
        replay = json.loads(await ws.recv())
        assert replay["type"] == "SESSION_STARTED"
        # phone sends a USER_COMMAND event
        await ws.send(json.dumps({"v": 1, "type": "USER_COMMAND", "session_id": orch.session_id, "message": "Muse, open the demo project"}))
        got = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert got["type"] == "MUSE_PROGRESS" and "Opened demo project" in got["message"]
        last_seq = got["seq"]
        # and a raw transcript frame
        await ws.send(json.dumps({"type": "transcript", "text": "Muse, describe what changed"}))
        got2 = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert got2["seq"] > last_seq
    # reconnect with last_seq: only newer host events are replayed
    await wait_until(lambda: orch.store.last_seq(orch.session_id) > last_seq, timeout=10)
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol_version": PROTOCOL_VERSION, "last_seq": last_seq}))
        json.loads(await ws.recv())  # welcome
        replayed = []
        with contextlib.suppress(asyncio.TimeoutError):
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 1))
                if "seq" in frame:
                    replayed.append(frame)
        assert replayed and all(f["seq"] > last_seq for f in replayed)
        assert all(f["type"] not in (EventType.USER_COMMAND.value,) for f in replayed)


async def test_wrong_protocol_version_is_refused(bridge):
    _, port = bridge
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token=secret-token") as ws:
        await ws.send(json.dumps({"type": "hello", "protocol_version": 42}))
        err = json.loads(await ws.recv())
        assert err["type"] == "error"
