# Project Ledger

Append-only chronological record. Never rewrite or delete an entry except to correct a
demonstrable factual error, and document that correction in a new entry.

---

## 2026-09-04 15:20 (Europe/London) — Phase 0 / V0 implemented offline

- **Branch:** `main` (initial commit)
- **Project stage:** Phase 0 (V0) implemented and tested with the scripted agent; Muse and
  Claude adapters implemented but not run live (credentials missing).
- **Objective of the change:** create the MuseGlass repository from an empty directory:
  research the current Muse Code, Claude Agent SDK and Meta Wearables DAT surfaces; write the
  architecture, build plan, security model and SDK notes; implement the whole V0 loop with
  tests; document status truthfully.
- **Files and systems changed:** everything under `museglass/`, `tests/`, `docs/`,
  `examples/demo-fastapi/`, `scripts/`, `.githooks/`, `README.md`, `pyproject.toml`,
  `.gitignore`. Python virtualenv `.venv` (uv, Python 3.12) with claude-agent-sdk 0.2.152,
  fastapi, uvicorn, websockets, mlx-whisper, webrtcvad-wheels, pytest, pytest-asyncio.
- **What was completed:** event protocol; agent abstraction with Muse (MSP), Claude (SDK) and
  scripted adapters; orchestrator with steering/interrupt/pause/approvals/questions/commit
  prompt/reconnect; router; summariser with verbosity modes; policy engine + workspace
  sandbox; SQLite store; speech providers (typed, local whisper, macOS say, null); WebSocket
  bridge + developer console; latency instrumentation; desktop CLI; demo FastAPI repo; setup
  and hook scripts; 86 tests.
- **Decisions made and why:** Python host + hand-written MSP client validated against Meta's
  conformance transcripts (official SDK is TypeScript only); one Python distribution instead
  of multi-package workspace; deterministic summariser/router; local whisper + `say` (no
  speech API keys present); SQLite; WebSocket; policy layer forcing human approval on top of
  backend approval modes; phase milestones exempt from rate limiting (rare and deduplicated);
  scripted agent reports `TURN_CANCELLED` even when cancelled before its first step;
  orchestrator interrupt watchdog; pending commit prompt restored on resume.
- **Tests and verification actually executed:**
  - `python -m pytest -q` → 86 passed in 4.98s.
  - `python -m pytest -q` in `examples/demo-fastapi` → 5 passed.
  - MSP client + adapter replay against 6 recorded transcripts → 8 passed.
  - End-to-end demo loop (real file edits, real pytest, real git commit, gated push) → 5 passed.
  - Bridge WebSocket tests → 3 passed.
  - `claude auth status` → not logged in; `which muse` → not found.
- **Results:** all automated tests green. Bugs found and fixed during the run: MSP client
  attribute `command` shadowing its `command()` method (caused a hang); `initialized`
  notification racing the next request; router treating "use an adapter instead" as an
  open-project command; summariser keeping a "Details:" label after stripping a code block;
  summariser rate-limiting phase milestones; scripted agent not reporting cancellation of a
  not-yet-started turn; resume not restoring the pending commit prompt.
- **Failures or unresolved uncertainty:** no live run against a real Muse or Claude host;
  microphone/TTS providers untested with real audio; real-host approval behaviour in
  `promptUnmatched` mode inferred, not observed; latency values not yet measured live.
- **Effect on the overall project:** V0 exists as working, tested software; the acceptance
  demo is one credential away from being runnable live.
- **Next intended step:** user installs/signs in to Muse Code (or `claude auth login`); run
  `docs/demo-script.md` live, record latency, tick the checklist; then Phase 1 Android bridge.
