"""End-to-end: the canonical demo interaction against the real demo repository.

The scripted agent edits real files and runs the real pytest suite; the assertions check the
filesystem, git history and the event log — not a mocked conversation.
"""

from __future__ import annotations

import pytest

from museglass.agent.scripted import ScriptedDemoAgent
from museglass.host.orchestrator import SessionOrchestrator, SessionState
from museglass.protocol.events import EventType
from museglass.speech.providers.null_tts import NullTTS
from museglass.store.sqlite import SessionStore
from museglass.summariser.summariser import Verbosity
from tests.conftest import git, wait_until

pytestmark = pytest.mark.e2e


def make_orchestrator(registry, store=None, tts=None, **kw) -> SessionOrchestrator:
    return SessionOrchestrator(
        agent=ScriptedDemoAgent(step_delay=0.05), store=store or SessionStore(":memory:"), registry=registry,
        tts=tts or NullTTS(), verbosity=Verbosity.NORMAL, approval_timeout=10, **kw,
    )


def spoken(store: SessionStore, session_id: str, *types: EventType) -> list[str]:
    return [e.message for e in store.events_since(session_id) if not types or e.type in types]


async def test_full_demo_loop_with_steer_commit_and_gated_push(demo_workspace):
    registry, ws = demo_workspace
    store = SessionStore(":memory:")
    tts = NullTTS()
    orch = make_orchestrator(registry, store, tts)
    await orch.start()

    # 1. task assignment (with project selection in the same sentence)
    await orch.handle_transcript(
        "Muse, open the demo project. Add a /health endpoint, include the current Git SHA and app version, write tests, and keep me updated."
    )
    assert orch.workspace is not None and orch.workspace.project_id == "demo-project"
    await wait_until(lambda: orch.state is SessionState.RUNNING, timeout=5)

    # 2. interrupt with a steer while the agent is exploring (no wake word needed while busy)
    await orch.handle_transcript("Also include uptime.")
    await wait_until(lambda: orch.pending is not None and orch.pending.kind == "commit_prompt", timeout=120)

    # 3. real files changed, real tests ran
    main_py = (ws.root / "app" / "main.py").read_text()
    assert '@app.get("/health")' in main_py and "git_sha()" in main_py and "uptime_seconds" in main_py
    test_file = (ws.root / "tests" / "test_health.py").read_text()
    assert test_file.count("def test_") == 4
    log = store.agent_log(orch.session_id, limit=500)
    pytest_runs = [e for e in log if e.get("kind") == "tool_completed" and "pytest" in str(e.get("tool_input"))]
    assert pytest_runs and "passed" in pytest_runs[-1]["tool_output"] and pytest_runs[-1]["success"] is True

    # 4. spoken progress was compressed: milestones + completion, no per-file narration
    progress = spoken(store, orch.session_id, EventType.MUSE_PROGRESS, EventType.MUSE_COMPLETE)
    assert any("implementing" in m for m in progress)
    assert any(m.startswith("Done.") and "tests pass" in m for m in progress)
    assert not any("app/__init__.py" in m for m in progress)
    assert any(m.startswith("Got it. I'll include uptime") for m in spoken(store, orch.session_id)) is False  # narration hidden in NORMAL
    assert spoken(store, orch.session_id, EventType.MUSE_QUESTION)[-1] == "Want me to commit it?"
    assert tts.spoken  # something was actually sent to TTS

    # 5. approve the commit but not a push
    await orch.handle_transcript("Yes. Commit but don't push.")
    await wait_until(lambda: orch.state is SessionState.IDLE and orch.pending is None, timeout=60)
    assert "Add /health endpoint" in git(ws.root, "log", "-1", "--pretty=%s").stdout
    assert git(ws.root, "status", "--porcelain").stdout.strip() == ""

    # 6. a push is gated by policy: spoken approval request → "no" → nothing pushed
    await orch.handle_transcript("Muse, push it.")
    await wait_until(lambda: orch.state is SessionState.WAITING_APPROVAL, timeout=30)
    request = [e for e in store.events_since(orch.session_id) if e.type is EventType.MUSE_APPROVAL_REQUEST][-1]
    assert request.requires_response and request.metadata["category"] == "git_push"
    assert "push" in request.message
    await orch.handle_transcript("No. Commit locally only.")
    await wait_until(lambda: orch.state is SessionState.IDLE and orch.pending is None, timeout=30)
    assert "did not push" in spoken(store, orch.session_id, EventType.MUSE_COMPLETE)[-1]
    assert git(ws.root, "remote").stdout.strip() == ""  # there was never a remote; nothing could have been pushed

    # latency instrumentation captured the interrupt/ack and event→spoken metrics
    summary = orch.latency.summary()
    assert summary["agent_event_to_spoken"]["count"] > 0
    await orch.close()


async def test_stop_pause_continue(demo_workspace):
    registry, ws = demo_workspace
    store = SessionStore(":memory:")
    orch = make_orchestrator(registry, store)
    await orch.start()
    await orch.handle_transcript("Muse, open the demo project and add a health endpoint with tests.")
    await wait_until(lambda: orch.state is SessionState.RUNNING, timeout=5)
    await orch.handle_transcript("Muse, stop.")
    await wait_until(lambda: orch.state is SessionState.IDLE, timeout=10)
    assert orch.latency.summary()["interrupt_to_ack"]["count"] == 1
    assert "Stopped." in spoken(store, orch.session_id)

    await orch.handle_transcript("Muse, add a health endpoint with tests.")
    await wait_until(lambda: orch.state is SessionState.RUNNING, timeout=5)
    await orch.handle_transcript("pause")
    await wait_until(lambda: orch.state is SessionState.PAUSED, timeout=10)
    assert EventType.SESSION_PAUSED in {e.type for e in store.events_since(orch.session_id)}
    await orch.handle_transcript("Muse, continue.")
    await wait_until(lambda: orch.pending is not None and orch.pending.kind == "commit_prompt", timeout=120)
    assert '@app.get("/health")' in (ws.root / "app" / "main.py").read_text()
    await orch.handle_transcript("No.")
    await orch.close()


async def test_approval_timeout_denies(demo_workspace):
    registry, ws = demo_workspace
    store = SessionStore(":memory:")
    orch = SessionOrchestrator(agent=ScriptedDemoAgent(step_delay=0.01), store=store, registry=registry, tts=NullTTS(),
                               approval_timeout=0.5, auto_commit_prompt=False)
    await orch.start()
    await orch.open_project("demo project")
    await orch.handle_transcript("Muse, push it.")
    await wait_until(lambda: orch.state is SessionState.WAITING_APPROVAL, timeout=10)
    await wait_until(lambda: orch.state is SessionState.IDLE, timeout=10)
    assert any("No answer" in m for m in spoken(store, orch.session_id))
    await orch.close()


async def test_reconnect_resume_replays_state(demo_workspace, tmp_path):
    registry, ws = demo_workspace
    db = tmp_path / "sessions.db"
    store = SessionStore(db)
    orch = make_orchestrator(registry, store)
    await orch.start()
    sid = orch.session_id
    await orch.handle_transcript("Muse, open the demo project and add a health endpoint with tests.")
    await wait_until(lambda: orch.pending is not None and orch.pending.kind == "commit_prompt", timeout=120)
    seen = store.last_seq(sid)
    # "phone disconnects": the orchestrator is torn down, the store survives
    await orch.close()
    store.close()

    store2 = SessionStore(db)
    orch2 = make_orchestrator(registry, store2, session_id=sid)
    await orch2.start(resume=True)
    assert orch2.workspace is not None and orch2.workspace.project_id == "demo-project"
    resumed = [e for e in store2.events_since(sid, seen) if e.type is EventType.SESSION_RESUMED]
    assert resumed and "waiting for your answer" in resumed[0].message
    assert orch2.state is SessionState.WAITING_APPROVAL or orch2.state is SessionState.WAITING_ANSWER
    # the client that reconnects with last_seq gets exactly the events it missed
    assert all(e.seq > seen for e in store2.events_since(sid, seen))
    await orch2.close()


async def test_unknown_project_is_reported(demo_workspace):
    registry, _ = demo_workspace
    store = SessionStore(":memory:")
    orch = make_orchestrator(registry, store)
    await orch.start()
    await orch.handle_transcript("Muse, open the banana project")
    assert any("don't know a project called banana" in m for m in spoken(store, orch.session_id, EventType.MUSE_ERROR))
    await orch.close()
