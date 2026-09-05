# Project Status

Last updated: 2026-09-04 (Europe/London). Branch: `main`.

## Project Objective

MuseGlass: a hands-free interface between Meta AI glasses and a coding agent (Muse Code first)
running on the user's Mac or a remote host. Speak a task, walk away, hear concise progress,
interrupt conversationally, approve sensitive actions, and complete real software work
without returning to the keyboard. Phase 0 (V0) proves the interaction loop on a Mac with its
own microphone and speakers; later phases add the Android/glasses bridge, vision and display.

## Current Stage

**Phase 0 / V0 implemented and tested offline; live LLM backends blocked on credentials.**

- The complete loop (speech → router → orchestrator → agent → real repo → real tests →
  summariser → speech, with steering, stop/pause/continue, approvals, commit prompt and
  reconnect) runs end-to-end with the scripted demo agent and is covered by automated tests.
- The Muse Code adapter is implemented against the published Muse Session Protocol v1 schema
  and passes replay tests over Meta's recorded conformance transcripts, but has **never been
  run against a real `muse` host** (binary not installed on this machine, no sign-in).
- The Claude Code adapter is implemented against `claude-agent-sdk` 0.2.152 but has **never
  been run live** (`claude auth status` → `loggedIn: false`).
- The V0 acceptance checklist in `docs/demo-script.md` is therefore **not yet ticked**.

## Current Branch

`main` (first commit). Remote: `https://github.com/MoheetBrain/code_with_meta_glasses.git`.

## Current System Architecture

See `docs/architecture.md`. Summary:

```
mic ─STT─▶ transcripts ─router─▶ intents ─▶ SessionOrchestrator ─▶ CodingAgent adapter ─▶ backend
                                              │  store (SQLite)     (muse MSP | claude SDK | scripted)
                                              │  summariser → spoken events → TTS
                                              │  policy engine gates approvals
                                              └─ WebSocket bridge + developer console
```

One Python distribution `museglass` with sub-packages `protocol`, `agent`, `host`,
`summariser`, `speech`, `store`, `bridge`, `apps`. Layering: apps → bridge → host →
{agent, summariser, speech, store} → protocol.

## Capabilities Implemented

- Typed event protocol (`USER_COMMAND`, `USER_INTERRUPT`, `USER_RESPONSE`, `MUSE_PROGRESS`,
  `MUSE_QUESTION`, `MUSE_APPROVAL_REQUEST`, `MUSE_COMPLETE`, `MUSE_ERROR`, `SESSION_STARTED`,
  `SESSION_PAUSED`, `SESSION_RESUMED`, `SESSION_ENDED`) with `session_id`, `project_id`,
  `timestamp`, `message`, `priority`, `requires_response`, `metadata`, per-session `seq`;
  JSON codec; secret redaction.
- `CodingAgent` / `AgentSession` / `AgentEvent` abstraction with three adapters:
  - `MuseCodeAgent` — Python MSP v1 client over `muse serve` (handshake, `session/start`,
    `session/resume`, `turn/start|steer|interrupt|cancel`, `approval/decide` with feedback,
    `userInput/answer|clarify`, item/turn/approval/userInput/tokenUsage notifications,
    server-request acknowledgement, host-exit handling, UUIDv7 command ids).
  - `ClaudeCodeAgent` — `ClaudeSDKClient` with `can_use_tool` permission callback,
    `interrupt()`, resume; steer = interrupt + re-instruct.
  - `ScriptedDemoAgent` — deterministic; really edits the demo repo, really runs pytest,
    honours "also include uptime" mid-task, asks approval before `git push`, commits on request.
- Session orchestrator with concurrent agent/input/speech pumps; steering into the running
  task; stop/pause/continue; approval gating with timeout; question answering; automatic
  "Want me to commit it?" after a completed turn with uncommitted changes; status/detail/
  short/why/undo/show-diff/verbosity/list-projects intents; interrupt watchdog.
- Rule-based command router with wake word ("Muse", tolerant of common mishearings),
  no wake word needed while busy or when a response is pending.
- Progress summariser: phase machine (exploring → implementing → testing → verifying →
  done), test-result parsing (pytest/jest), architectural-finding detection, QUIET / NORMAL /
  TALKATIVE with rate limiting; blockers/questions/approvals/phase milestones never suppressed.
- Policy engine: `SAFE` / `NEEDS_APPROVAL` / `FORBIDDEN` classification for git push,
  deploy, destructive delete/command, credential access, package install, system settings,
  destructive DB migration, financial, network, outside-workspace paths (symlink-safe).
- Workspace registry and sandbox under `~/MuseWorkspaces` (`MUSEGLASS_WORKSPACES`), spoken
  name resolution ("the demo project" → `demo-project`).
- SQLite session store (sessions, ordered events, raw agent log) with reconnect replay.
- Speech providers: `TypedTextSTT`, `LocalWhisperSTT` (ffmpeg AVFoundation mic → WebRTC
  VAD → mlx-whisper), `MacSayTTS` (barge-in capable), `NullTTS`.
- WebSocket bridge (token, hello/welcome, replay since `last_seq`, live events, transcript
  frames) and developer console (`/console`) showing session, project, task, last command,
  agent state, pending approval, recent events, token/cost, latency.
- Latency instrumentation: speech_end→transcript, speech_end→agent_ack, agent_event→spoken,
  interrupt→ack.
- Desktop demo CLI `museglass` (`--backend auto|muse|claude|scripted`, `--stt whisper|typed`,
  `--tts say|null`, `--console`, `--resume`), `museglass-console`.
- Demo FastAPI repository and `scripts/setup_workspaces.sh`.
- Commit Continuity Protocol fallback hook (`.githooks/pre-commit`, `scripts/install-git-hooks.sh`).

## Work Completed to Date

1. Environment and capability audit (Muse Code, Claude Code, Meta Wearables DAT, speech).
2. Design docs: `architecture.md`, `build-plan.md` (decision table, capability matrix,
   smallest path to V0), `meta-sdk-notes.md`, `security-model.md`, `demo-script.md`, README.
3. Full V0 implementation as listed above.
4. Test suite: 86 tests (unit, MSP conformance replay, WebSocket bridge, end-to-end).

## Important Files and Artifacts

| path | purpose |
|---|---|
| `museglass/protocol/events.py` | event protocol |
| `museglass/agent/interface.py` | backend abstraction |
| `museglass/agent/muse_msp/client.py`, `adapter.py` | Muse Code over MSP |
| `museglass/agent/claude/adapter.py` | Claude Code over the Agent SDK |
| `museglass/agent/scripted.py` | offline demo agent (real edits, real tests) |
| `museglass/host/orchestrator.py` | the concurrent session core |
| `museglass/host/router.py`, `policy.py`, `workspace.py`, `latency.py` | intents, approval policy, sandbox, latency |
| `museglass/summariser/summariser.py` | spoken-progress summariser |
| `museglass/speech/providers/*` | STT/TTS providers |
| `museglass/store/sqlite.py` | persistence |
| `museglass/bridge/server.py` | WebSocket bridge + console |
| `museglass/apps/desktop_demo.py` | V0 app |
| `examples/demo-fastapi/` | demo repository |
| `tests/fixtures/msp/` | Meta's MSP conformance transcripts (MIT) |
| `tests/fake_msp_host.py` | transcript-replaying fake `muse serve` |
| `docs/*.md` | design, plan, SDK notes, security, demo script, status, ledger |

## Data Sources and Provenance

- Muse Session Protocol schema, transcripts and SDK source: `github.com/meta-models/muse-code-sdk`
  (stable fingerprint `sha256:cfd31ee77d78fdada9febc4edccd29b0434ff8f6bf157c7c03fd0ecfcbc29f5a`,
  fetched 2026-09-04). Muse Code docs overview: `dev.meta.ai/docs/muse-code`; Meta AI dev blog
  post "Muse Code: New plans and features".
- Claude Agent SDK: installed package 0.2.152 (introspected) and `code.claude.com/docs/en/agent-sdk/python`.
- Meta Wearables DAT: `github.com/facebook/meta-wearables-dat-android` (0.9.0),
  `wearables.developer.meta.com/docs/develop/dat/*`, `developers.meta.com/wearables/faq/`.
- No user data, audio or images are stored by the project.

## Decisions and Rationale

See the decision table in `docs/build-plan.md`. Key ones: Python host with a hand-written
MSP client (validated by Meta's transcript corpus) instead of a Node sidecar; single Python
distribution instead of a multi-package workspace; deterministic summariser and rule-based
router (testable, zero latency); local whisper + macOS `say` because no speech API keys are
available; SQLite for persistence; WebSocket for the phone bridge; policy layer that forces
human approval regardless of backend approval mode.

## Verification and Test Results

Executed 2026-09-04 in `.venv` (Python 3.12.14):

| command | result |
|---|---|
| `python -m pytest -q` (repo root) | **86 passed in 4.98s** |
| `python -m pytest -q` in `examples/demo-fastapi` | 5 passed, 1 warning |
| `python -m pytest -q tests/test_msp_client.py` | 8 passed (handshake, session start, single turn, approval allow/deny, cancel, userInput, host death) |
| `python -m pytest -q tests/test_orchestrator_e2e.py` | 5 passed (full demo loop with steer + commit + gated push, stop/pause/continue, approval timeout, reconnect/resume, unknown project) |
| `python -m pytest -q tests/test_bridge.py` | 3 passed (token rejection, hello/welcome/live/replay, protocol version) |
| `claude auth status` | `loggedIn: false` — Claude backend not runnable |
| `which muse` | not found — Muse backend not runnable |
| Live microphone run of `LocalWhisperSTT` / `MacSayTTS` | **not run** (no interactive audio session in this environment) |

## Known Failures and Limitations

- No live run with a real LLM backend yet; acceptance checklist not ticked.
- `LocalWhisperSTT` and `MacSayTTS` are untested against real audio in this session; the
  whisper model (`mlx-community/whisper-large-v3-turbo`) downloads on first run.
- `ClaudeSession.send_instruction(steer=True)` interrupts the running turn; Claude may lose
  some in-flight tool work (SDK limitation, documented).
- Muse adapter ignores `view/gap` recovery (logs a status only) and does not use
  `goal/*` (not on the stable schema).
- The summariser is deterministic English-only heuristics; completion text quality depends on
  the agent's final message.
- The bridge has no TLS by itself (use uvicorn certs or a proxy/Tailscale); binds loopback by default.
- The Android app (Phase 1) does not exist yet — only the interface contract is designed.

## Risks and Technical Debt

- MSP is pre-1.0 (developer preview); fingerprint drift is a warning today but breaking
  changes are possible. Transcript replay tests will catch method-level drift only.
- `promptUnmatched` behaviour of the real host (which actions prompt) is inferred from the
  schema and changelog, not observed.
- Speech-to-agent latency with local whisper is unmeasured; the streaming-STT provider slot
  (Deepgram/OpenAI) is empty.
- Policy regexes are heuristic; they are a second line behind backend sandboxes, not a
  substitute.

## Current Blockers

External, user-only:

1. Muse Code not installed / not signed in on this Mac, **or** Claude Code CLI not signed in.
   Either unblocks the live V0 acceptance run.

## Exact Next Actions

1. User: install Muse Code (`curl -fsSL https://dev.meta.ai/install.sh | bash`, run `muse`,
   sign in) or run `claude auth login`.
2. Run `scripts/setup_workspaces.sh`, then `museglass --console` and execute
   `docs/demo-script.md` live; record real latency values and tick the checklist here.
3. Fix whatever the real host does differently from the transcripts (approval choice ids,
   item shapes); extend `tests/fixtures/msp` with recorded live frames.
4. Then Phase 1: Android bridge skeleton with `WearablesBridge` interface + Mock Device Kit.
