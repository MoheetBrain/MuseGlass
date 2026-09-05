import json

import pytest

from museglass.protocol.events import PROTOCOL_VERSION, Event, EventType, Priority, new_event
from museglass.protocol.redact import redact


def test_event_round_trip_json():
    event = new_event(EventType.MUSE_APPROVAL_REQUEST, "s1", "I want to push. Approve?", project_id="demo",
                      priority=Priority.CRITICAL, requires_response=True, request_id="r1", choices=["allow_once", "abort"])
    event.seq = 7
    data = json.loads(event.to_json())
    assert data["v"] == PROTOCOL_VERSION
    assert data["type"] == "MUSE_APPROVAL_REQUEST"
    assert data["metadata"]["choices"] == ["allow_once", "abort"]
    back = Event.from_json(event.to_json())
    assert back == event


def test_every_required_type_exists():
    required = {"USER_COMMAND", "USER_INTERRUPT", "MUSE_PROGRESS", "MUSE_QUESTION", "MUSE_APPROVAL_REQUEST",
                "MUSE_COMPLETE", "MUSE_ERROR", "SESSION_STARTED", "SESSION_PAUSED", "SESSION_RESUMED"}
    assert required <= {t.value for t in EventType}


def test_rejects_unknown_version_and_type():
    with pytest.raises(ValueError):
        Event.from_dict({"v": 99, "type": "USER_COMMAND", "session_id": "s"})
    with pytest.raises(ValueError):
        Event.from_dict({"v": 1, "type": "NOPE", "session_id": "s"})


def test_user_and_spoken_classification():
    assert new_event(EventType.USER_COMMAND, "s").is_user_event
    assert not new_event(EventType.USER_COMMAND, "s").is_spoken
    assert new_event(EventType.MUSE_COMPLETE, "s").is_spoken


def test_redaction_masks_common_secrets():
    text = "token ghp_abcdefghijklmnopqrstuvwxyz0123 and key sk-abcdefghijklmnopqrstu and AKIAABCDEFGHIJKLMNOP password=hunter2"
    out = redact(text)
    assert "ghp_" not in out and "sk-abc" not in out and "AKIA" not in out and "hunter2" not in out
    assert redact("plain progress text") == "plain progress text"
