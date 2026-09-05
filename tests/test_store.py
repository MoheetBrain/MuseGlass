from pathlib import Path

from museglass.protocol.events import EventType, new_event
from museglass.store.sqlite import SessionRecord, SessionStore


def test_sessions_and_events_persist_across_reopen(tmp_path: Path):
    db = tmp_path / "s.db"
    store = SessionStore(db)
    store.create_session(SessionRecord(session_id="abc", backend="scripted", status="idle"))
    store.update_session("abc", project_id="demo-project", status="running", current_task="add /health",
                         pending_request={"kind": "approval", "request_id": "r1"})
    e1 = store.append_event(new_event(EventType.USER_COMMAND, "abc", "Muse, add /health"))
    e2 = store.append_event(new_event(EventType.MUSE_PROGRESS, "abc", "Working."))
    assert (e1.seq, e2.seq) == (1, 2)
    store.close()

    reopened = SessionStore(db)
    record = reopened.get_session("abc")
    assert record and record.project_id == "demo-project" and record.status == "running"
    assert record.pending_request == {"kind": "approval", "request_id": "r1"}
    assert [e.message for e in reopened.events_since("abc", 1)] == ["Working."]
    assert reopened.last_seq("abc") == 2
    assert reopened.latest_active_session().session_id == "abc"
    assert reopened.last_user_command("abc").message == "Muse, add /health"


def test_event_sequences_are_per_session():
    store = SessionStore(":memory:")
    for sid in ("a", "b"):
        store.create_session(SessionRecord(session_id=sid, backend="x", status="idle"))
    store.append_event(new_event(EventType.MUSE_PROGRESS, "a", "1"))
    store.append_event(new_event(EventType.MUSE_PROGRESS, "a", "2"))
    b = store.append_event(new_event(EventType.MUSE_PROGRESS, "b", "1"))
    assert b.seq == 1
    assert store.last_seq("a") == 2


def test_secrets_are_redacted_before_persistence():
    store = SessionStore(":memory:")
    store.create_session(SessionRecord(session_id="a", backend="x", status="idle"))
    store.append_event(new_event(EventType.MUSE_PROGRESS, "a", "found token ghp_abcdefghijklmnopqrstuvwxyz0123"))
    assert "ghp_" not in store.events_since("a")[0].message
    store.append_agent_log("a", {"tool_output": "AKIAABCDEFGHIJKLMNOP"})
    assert "AKIA" not in str(store.agent_log("a"))
