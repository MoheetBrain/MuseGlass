"""Conformance of the Python MSP client and Muse adapter against recorded transcripts."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from museglass.agent.interface import AgentEventKind, Workspace
from museglass.agent.muse_msp.adapter import MuseSession
from museglass.agent.muse_msp.client import MspClient, uuid7
from tests.conftest import FIXTURES

FAKE_HOST = Path(__file__).resolve().parent / "fake_msp_host.py"
TRANSCRIPTS = FIXTURES / "msp"


def host_command(name: str) -> list[str]:
    return [sys.executable, str(FAKE_HOST), str(TRANSCRIPTS / name / "transcript.ndjson")]


def test_uuid7_is_time_ordered_and_versioned():
    a, b = uuid7(), uuid7()
    assert a[14] == "7" and b[14] == "7"
    assert a[19] in "89ab"
    assert a.split("-")[0] <= b.split("-")[0]


async def test_handshake_and_session_start():
    client = MspClient(host_command("session-start"))
    init = await client.start()
    assert init["serverInfo"]["name"] == "muse-session-server"
    assert init["schema"]["version"] == 1
    assert client.fingerprint_warning is not None  # fixture fingerprint differs from the stable bundle; warning, not error
    result = await client.command("session/start", {"sessionId": "0198f0aa-1111-7000-8000-0000000000aa", "workspaceRoot": "/home/me/src/proj"})
    assert result["session"]["sessionId"] == "0198f0aa-1111-7000-8000-0000000000aa"
    note = await asyncio.wait_for(client.notifications.get(), 5)
    assert note["method"] == "session/started"
    await client.close()


async def test_single_turn_streams_message_and_completion(tmp_path):
    client = MspClient(host_command("text-run-single-turn"))
    await client.start()
    session = MuseSession(client, Workspace("proj", tmp_path), approval_mode="promptUnmatched", model_id=None, reasoning_effort=None)
    await session.open(resume_id=None)
    turn_id = await session.send_instruction("Run the agent test suite and summarize failures")
    assert turn_id == "018f6a1e-9b3c-7c21-a54a-2f30bd3c9f10"
    kinds = []
    async def collect():
        async for ev in session.events():
            kinds.append(ev)
            if ev.kind is AgentEventKind.TURN_COMPLETED:
                return
    await asyncio.wait_for(collect(), 10)
    seq = [e.kind for e in kinds]
    assert seq[0] is AgentEventKind.SESSION_READY
    assert AgentEventKind.TURN_STARTED in seq and AgentEventKind.MESSAGE_DELTA in seq
    message = next(e for e in kinds if e.kind is AgentEventKind.MESSAGE)
    assert message.text == "All 214 tests pass except two in tbh-agent..."
    done = kinds[-1]
    assert done.text == message.text and done.usage["inputTokens"] == 48210
    assert not session.is_busy
    await session.close()


async def test_approval_round_trip_answers_server_request_and_decides(tmp_path):
    client = MspClient(host_command("approval-round-trip"))
    await client.start()
    session = MuseSession(client, Workspace("proj", tmp_path), approval_mode="promptUnmatched", model_id=None, reasoning_effort=None)
    await session.open(resume_id=None)
    await session.send_instruction("Update the manifest")
    approvals = []
    async def until_approval():
        async for ev in session.events():
            if ev.kind is AgentEventKind.APPROVAL_REQUESTED:
                approvals.append(ev)
                return
    await asyncio.wait_for(until_approval(), 10)
    req = approvals[0].approval
    assert req.kind == "fileAccess" and req.tool_name == "write_file" and req.detail.endswith("Cargo.toml")
    assert [c.choice_id for c in req.choices] == ["allow_once", "allow_session", "abort"]
    assert req.raw["tool_input"] == {"path": "/home/me/src/proj/Cargo.toml", "access": "write"}
    # one approval only, even though the host sent both the notification and the server request
    await session.approve(req.request_id, choice_id="allow_session")
    resolved = []
    async def until_resolved():
        async for ev in session.events():
            if ev.kind is AgentEventKind.APPROVAL_RESOLVED:
                resolved.append(ev)
                return
    await asyncio.wait_for(until_resolved(), 10)
    assert resolved[0].success is True
    await session.close()


async def test_reject_with_feedback(tmp_path):
    client = MspClient(host_command("approval-deny-round-trip"))
    await client.start()
    session = MuseSession(client, Workspace("proj", tmp_path), approval_mode="promptUnmatched", model_id=None, reasoning_effort=None)
    await session.open(resume_id=None)
    await session.send_instruction("Update the manifest")
    async def until(kind):
        async for ev in session.events():
            if ev.kind is kind:
                return ev
    req = (await asyncio.wait_for(until(AgentEventKind.APPROVAL_REQUESTED), 10)).approval
    await session.reject(req.request_id, feedback="Do not modify the manifest")
    resolved = await asyncio.wait_for(until(AgentEventKind.APPROVAL_RESOLVED), 10)
    assert resolved.success is False
    await session.close()


async def test_cancel_mid_turn(tmp_path):
    client = MspClient(host_command("cancel-mid-turn"))
    await client.start()
    session = MuseSession(client, Workspace("proj", tmp_path), approval_mode="promptUnmatched", model_id=None, reasoning_effort=None)
    await session.open(resume_id=None)
    await session.send_instruction("Run the flaky integration suite")
    assert session.is_busy
    await session.cancel()
    async def until_cancelled():
        async for ev in session.events():
            if ev.kind is AgentEventKind.TURN_CANCELLED:
                return ev
    ev = await asyncio.wait_for(until_cancelled(), 10)
    assert "cancelled" in ev.text and not session.is_busy
    await session.close()


async def test_user_input_question_round_trip(tmp_path):
    client = MspClient(host_command("userinput-answer-round-trip"))
    await client.start()
    session = MuseSession(client, Workspace("proj", tmp_path), approval_mode="promptUnmatched", model_id=None, reasoning_effort=None)
    await session.open(resume_id="0198f0aa-1111-7000-8000-0000000000bb")
    assert session.is_busy  # resumed into a running turn
    async def until(kind):
        async for ev in session.events():
            if ev.kind is kind:
                return ev
    q = (await asyncio.wait_for(until(AgentEventKind.QUESTION), 10)).question
    assert "database" in q.text.lower() and q.options == ["Postgres", "SQLite"]
    # the transcript pages history next; drive that request exactly as the recorded client did
    await client.request("view/page", {"sessionId": session.session_id, "direction": "forward", "limit": 200})
    await session.answer_question(q.question_id, "let's go with postgres")
    settled = await asyncio.wait_for(until(AgentEventKind.QUESTION_SETTLED), 10)
    assert settled.text == "answered"
    await session.close()


async def test_host_death_surfaces_as_error(tmp_path):
    client = MspClient([sys.executable, "-c", "import sys; print(sys.stdin.readline() and '')"])
    with pytest.raises(Exception):
        await asyncio.wait_for(client.start(), 10)
    await client.close()
