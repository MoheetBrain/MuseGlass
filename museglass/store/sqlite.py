"""SQLite session store: the simplest real persistence for V0.

Everything needed to reconnect and resume lives here: session identity, active project,
backend session id, current task, pending request, and the full ordered event log.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from museglass.protocol.events import Event, EventType, utc_now_iso
from museglass.protocol.redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT,
    backend TEXT NOT NULL,
    backend_session_id TEXT,
    status TEXT NOT NULL,
    current_task TEXT,
    pending_request TEXT,
    verbosity TEXT NOT NULL DEFAULT 'NORMAL',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS agent_log (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""


@dataclass
class SessionRecord:
    session_id: str
    backend: str
    status: str
    project_id: str | None = None
    backend_session_id: str | None = None
    current_task: str | None = None
    pending_request: dict[str, Any] | None = None
    verbosity: str = "NORMAL"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class SessionStore:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions ------------------------------------------------------------------------
    def create_session(self, record: SessionRecord) -> SessionRecord:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, project_id, backend, backend_session_id, status, current_task, pending_request, verbosity, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    record.session_id,
                    record.project_id,
                    record.backend,
                    record.backend_session_id,
                    record.status,
                    record.current_task,
                    json.dumps(record.pending_request) if record.pending_request else None,
                    record.verbosity,
                    record.created_at,
                    record.updated_at,
                ),
            )
        return record

    def update_session(self, session_id: str, **fields: Any) -> None:
        allowed = {"project_id", "backend_session_id", "status", "current_task", "pending_request", "verbosity"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown session fields: {sorted(unknown)}")
        if not fields:
            return
        if "pending_request" in fields:
            pr = fields["pending_request"]
            fields["pending_request"] = json.dumps(pr) if pr is not None else None
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE sessions SET {sets}, updated_at = ? WHERE session_id = ?",
                (*fields.values(), utc_now_iso(), session_id),
            )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_sessions(self, *, active_only: bool = False) -> list[SessionRecord]:
        query = "SELECT * FROM sessions"
        if active_only:
            query += " WHERE status NOT IN ('ended', 'failed')"
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [self._row_to_record(r) for r in rows]

    def latest_active_session(self) -> SessionRecord | None:
        sessions = self.list_sessions(active_only=True)
        return sessions[0] if sessions else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            project_id=row["project_id"],
            backend=row["backend"],
            backend_session_id=row["backend_session_id"],
            status=row["status"],
            current_task=row["current_task"],
            pending_request=json.loads(row["pending_request"]) if row["pending_request"] else None,
            verbosity=row["verbosity"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -- events --------------------------------------------------------------------------
    def append_event(self, event: Event) -> Event:
        """Assign the next per-session sequence number and persist. Returns the event with
        `seq` set (the same object)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM events WHERE session_id = ?",
                (event.session_id,),
            ).fetchone()
            event.seq = int(row["next"])
            event.message = redact(event.message)
            self._conn.execute(
                "INSERT INTO events (session_id, seq, event_id, type, timestamp, payload) VALUES (?,?,?,?,?,?)",
                (event.session_id, event.seq, event.event_id, event.type.value, event.timestamp, redact(event.to_json())),
            )
            self._conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (utc_now_iso(), event.session_id))
        return event

    def events_since(self, session_id: str, after_seq: int = 0, *, limit: int | None = None) -> list[Event]:
        query = "SELECT payload FROM events WHERE session_id = ? AND seq > ? ORDER BY seq"
        params: tuple[Any, ...] = (session_id, after_seq)
        if limit:
            query += " LIMIT ?"
            params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Event.from_json(r["payload"]) for r in rows]

    def recent_events(self, session_id: str, limit: int = 20) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT ?", (session_id, limit)
            ).fetchall()
        return [Event.from_json(r["payload"]) for r in reversed(rows)]

    def last_seq(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE session_id = ?", (session_id,)).fetchone()
        return int(row["s"])

    def conversation(self, session_id: str) -> list[Event]:
        """User commands/responses and spoken agent events, in order (the transcript)."""
        wanted = {
            EventType.USER_COMMAND, EventType.USER_INTERRUPT, EventType.USER_RESPONSE,
            EventType.MUSE_PROGRESS, EventType.MUSE_QUESTION, EventType.MUSE_APPROVAL_REQUEST,
            EventType.MUSE_COMPLETE, EventType.MUSE_ERROR,
        }
        return [e for e in self.events_since(session_id, 0) if e.type in wanted]

    def last_user_command(self, session_id: str) -> Event | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM events WHERE session_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
                (session_id, EventType.USER_COMMAND.value),
            ).fetchone()
        return Event.from_json(row["payload"]) if row else None

    # -- raw agent log (low-level, for the console and post-mortems) -----------------------
    def append_agent_log(self, session_id: str, payload: dict[str, Any]) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM agent_log WHERE session_id = ?", (session_id,)
            ).fetchone()
            seq = int(row["next"])
            self._conn.execute(
                "INSERT INTO agent_log (session_id, seq, timestamp, payload) VALUES (?,?,?,?)",
                (session_id, seq, utc_now_iso(), redact(json.dumps(payload, default=str))),
            )
        return seq

    def agent_log(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM agent_log WHERE session_id = ? ORDER BY seq DESC LIMIT ?", (session_id, limit)
            ).fetchall()
        return [json.loads(r["payload"]) for r in reversed(rows)]
