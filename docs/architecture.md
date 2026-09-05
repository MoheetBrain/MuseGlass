# MuseGlass Architecture

MuseGlass turns Meta AI glasses into a hands-free control surface for an autonomous coding
agent running on a Mac (or a remote host). The agent is **Muse Code** (Meta's terminal coding
agent) first; the design isolates it behind a `CodingAgent` interface so Claude Code, Codex or
others can be swapped in.

```
META GLASSES ──(Bluetooth HFP/A2DP)──▶ ANDROID BRIDGE ──(WSS, token)──▶ HOST (Mac/server)
   mic/speaker                          phone app                       ┌────────────────────┐
                                                                        │ speech  (STT/TTS)  │
                                                                        │ router  (intents)  │
                                                                        │ orchestrator       │
                                                                        │ summariser         │
                                                                        │ policy + sandbox   │
                                                                        │ session store      │
                                                                        │ agent adapters     │
                                                                        └────────┬───────────┘
                                                                                 │ MSP (stdio JSON-RPC)
                                                                                 ▼
                                                                          muse serve ──▶ repo, shell, tests
```

Phase 0 (V0) runs the whole host on the Mac with its own microphone and speakers; the phone
and glasses are added in Phase 1 without changing the host's event protocol.

## 1. Design principles

1. **Concurrency is the product.** Agent work, user speech and spoken progress run at the same
   time. Nothing in the host is a blocking request/response chatbot.
2. **One session, many inputs.** Every spoken instruction lands in the *same* live agent
   session (steered into the running turn, queued, or delivered as an answer/approval).
3. **Speak milestones, not events.** Raw agent events are folded by a deterministic
   summariser into a handful of short spoken updates. Blockers, questions and approvals are
   surfaced immediately.
4. **The agent never has unrestricted access.** Work happens only inside registered
   workspaces under `~/MuseWorkspaces/`; a policy engine forces human approval for risky
   actions regardless of what the backend would allow.
5. **Real work only.** Acceptance means the agent modified real files and real tests ran.
6. **Replaceable adapters.** Speech providers and coding agents are interfaces with more
   than one implementation from day one.

## 2. Repository layout (and why it differs from the proposal)

The proposal had `packages/*` and `services/*` as separate packages. For a single-language
prototype that adds packaging ceremony (inter-package versioning, path deps) with no benefit,
so MuseGlass ships **one Python distribution, `museglass`, with one sub-package per proposed
package**. Module boundaries are preserved by import discipline (lower layers never import
higher ones) and can be split into separate distributions later without renaming anything.

```
museglass/
├── museglass/                  Python package (host side)
│   ├── protocol/               typed event protocol (proposal: packages/protocol)
│   ├── agent/                  CodingAgent interface + adapters (packages/agent-interface)
│   │   ├── interface.py        the abstraction every backend implements
│   │   ├── scripted.py         deterministic demo agent: real edits, real pytest, no LLM
│   │   ├── muse_msp/           Muse Code adapter: Python MSP client over `muse serve`
│   │   └── claude/             Claude Code adapter via claude-agent-sdk
│   ├── store/                  SQLite session store (packages/session-store)
│   ├── summariser/             progress summariser (packages/progress-summariser)
│   ├── speech/                 STT / TTS interfaces + providers (services/speech)
│   ├── host/                   orchestrator, command router, policy, workspace sandbox,
│   │                           latency instrumentation (services/agent-host)
│   ├── bridge/                 WebSocket bridge + developer console (services/bridge)
│   └── apps/desktop_demo.py    the V0 desktop app: mic → agent → speakers (apps/desktop-demo)
├── apps/android/               Phase 1 Kotlin client (Meta DAT integration point)
├── examples/demo-fastapi/      the demo repository the acceptance test runs against
├── tests/                      unit, protocol-conformance and end-to-end tests
├── scripts/                    workspace setup, git hooks
└── docs/
```

Layering (arrows are allowed imports):

```
apps ─▶ bridge ─▶ host ─▶ {agent, summariser, speech, store} ─▶ protocol
```

## 3. Components

### 3.1 Event protocol (`museglass/protocol/events.py`)

A single typed `Event` envelope crosses every boundary (in-process queues, SQLite, WebSocket):

| field               | meaning                                                            |
|---------------------|--------------------------------------------------------------------|
| `type`              | `USER_COMMAND`, `USER_INTERRUPT`, `USER_RESPONSE`, `MUSE_PROGRESS`, `MUSE_QUESTION`, `MUSE_APPROVAL_REQUEST`, `MUSE_COMPLETE`, `MUSE_ERROR`, `SESSION_STARTED`, `SESSION_PAUSED`, `SESSION_RESUMED`, `SESSION_ENDED` |
| `session_id`        | MuseGlass session (not the backend's id; that lives in the store)  |
| `project_id`        | registered workspace name                                          |
| `timestamp`         | ISO-8601 UTC                                                       |
| `message`           | the human-readable text (what gets spoken for MUSE_* events)       |
| `priority`          | `LOW`, `NORMAL`, `HIGH`, `CRITICAL`                                |
| `requires_response` | true for questions and approval requests                           |
| `metadata`          | typed payloads: request ids, choices, test stats, latency marks    |
| `seq`               | per-session monotonic sequence assigned by the store (reconnect)   |

JSON on the wire, dataclasses in process. `protocol_version` is carried in the WebSocket
hello so the Android client can refuse a host it does not understand.

### 3.2 Coding-agent interface (`museglass/agent/interface.py`)

```
CodingAgent.create_session(workspace, resume_id=None) -> AgentSession
AgentSession.send_instruction(text, steer=False) -> turn_id
AgentSession.interrupt() / pause() / resume() / cancel()
AgentSession.approve(request_id, choice_id) / reject(request_id, feedback)
AgentSession.answer_question(question_id, answer)
AgentSession.events() -> AsyncIterator[AgentEvent]
```

`AgentEvent` is the backend-neutral low-level stream (`TOOL_STARTED`, `TOOL_COMPLETED`,
`MESSAGE`, `APPROVAL_REQUESTED`, `QUESTION`, `TURN_COMPLETED`, ...). Adapters translate their
native protocol into it; nothing above the adapter knows which backend is running.

Backends:

| adapter                    | mechanism                                                                    | status |
|----------------------------|------------------------------------------------------------------------------|--------|
| `MuseCodeAgent`            | spawns `muse serve`, speaks the Muse Session Protocol v1 (NDJSON JSON-RPC over stdio). Native steer (`turn/steer`), interrupt (`turn/interrupt`), approvals (`approval/requested` → `approval/decide`), questions (`userInput/*`), resume (`session/resume`). | implemented against the published schema + conformance transcripts; live run needs the `muse` binary and sign-in |
| `ClaudeCodeAgent`          | `claude-agent-sdk` `ClaudeSDKClient` (streaming input, `interrupt()`, `can_use_tool` permission callback, `resume`). No mid-turn injection API, so *steer* = interrupt + re-instruct (documented workaround). | implemented; live run needs `claude auth login` |
| `ScriptedDemoAgent`        | no LLM. Deterministically performs the canonical demo task on the real demo repo: edits real files, runs real `pytest`, asks a real approval before committing. Used by the end-to-end test and for demoing the interaction loop offline. | implemented |

### 3.3 Session orchestrator (`museglass/host/orchestrator.py`)

One `SessionOrchestrator` per MuseGlass session. It owns three concurrent asyncio tasks:

- **agent pump** – drains `AgentSession.events()`, persists every event, feeds the summariser,
  and emits protocol events (progress, questions, approvals, completion, errors);
- **input pump** – receives routed user intents (task, steer, stop, pause, continue, yes/no,
  detail requests) and applies them to the live session;
- **speech pump** – serialises text-to-speech, and lets a user interrupt cut playback short.

Approval flow: an agent approval request is first evaluated by the policy engine. Safe
in-workspace actions are auto-approved (spoken only in TALKATIVE mode); everything in the
"requires human approval" list becomes a `MUSE_APPROVAL_REQUEST` and the session waits for a
`USER_RESPONSE`. A "no" carries the user's feedback back to the agent.

### 3.4 Command router (`museglass/host/router.py`)

Rule-based intent parsing of transcripts. It distinguishes control words from content:
`stop`, `pause`, `continue`, `yes/no/approve/deny`, `status`, `more detail/short version`,
`quiet/normal/talkative`, `open project X`. Anything else while a task runs is a **steer** into
the current task; anything else while idle is a **new task**. No LLM in the router: the agent
is the smart part, the router only needs to be predictable.

### 3.5 Progress summariser (`museglass/summariser/summariser.py`)

Deterministic and stateful. Tracks a phase machine (`exploring → implementing → testing →
verifying → done`) from tool usage, compresses the agent's own messages to one sentence, and
rate-limits by verbosity:

| mode      | speaks                                                        | min gap |
|-----------|---------------------------------------------------------------|---------|
| QUIET     | questions, approvals, errors, completion                      | –       |
| NORMAL    | + phase transitions and test results                          | 20 s    |
| TALKATIVE | + agent narration sentences and auto-approved actions         | 6 s     |

Blockers, approvals and questions are never rate-limited.

### 3.6 Speech (`museglass/speech/`)

`SpeechToTextProvider` yields `Transcript(text, is_final, speech_end_at)`;
`TextToSpeechProvider.speak(text)` is cancellable for barge-in.

| provider                | notes                                                                 |
|-------------------------|-----------------------------------------------------------------------|
| `TypedTextSTT`          | stdin / WebSocket text. Deterministic; used by tests and dev.        |
| `LocalWhisperSTT`       | `ffmpeg` (AVFoundation mic) → 16 kHz PCM → WebRTC VAD endpointing → `mlx-whisper` on Apple Silicon. Fully local, no key. Emits one final transcript per utterance; endpoint detection is the VAD silence tail. |
| `MacSayTTS`             | macOS `say`; killable for barge-in. No key, offline.                  |
| `NullTTS`               | logs instead of speaking (tests, console-only).                       |

Cloud streaming STT (partials, sub-300 ms) is an optional provider slot; none is required
for V0. Meta exposes no third-party speech API for the glasses (see `meta-sdk-notes.md`): on
the phone the glasses are an HFP microphone and an A2DP/HFP speaker, so STT/TTS keep running
on the phone or the host.

### 3.7 Policy and workspace sandbox (`museglass/host/policy.py`, `workspace.py`)

- Registered workspaces live under `~/MuseWorkspaces/<project>/` (override with
  `MUSEGLASS_WORKSPACES`). A spoken project name resolves against the registry.
- Every backend is started with the workspace as its root (Muse `workspaceRoot`, Claude `cwd`
  with no `add_dirs`).
- `classify_action(tool, input, root)` returns `SAFE`, `NEEDS_APPROVAL` or `FORBIDDEN` with a
  category: `git_push`, `deploy`, `destructive_delete`, `destructive_command`,
  `credential_access`, `package_install`, `system_settings`, `db_migration`, `financial`,
  `outside_workspace`. Approval requests and tool calls are checked before anything is
  auto-approved. Details in `security-model.md`.

### 3.8 Session store (`museglass/store/sqlite.py`)

SQLite (stdlib). Tables: `sessions` (id, project, backend, backend session id, status, current
task, pending request, verbosity), `events` (per-session sequence, JSON). Reconnect = client
sends `last_seq`, host replays newer events and states the current activity aloud.

### 3.9 Bridge and developer console (`museglass/bridge/server.py`)

FastAPI app: `ws://host:8765/ws?token=…` carries protocol events both ways (phone → host:
`USER_COMMAND`/`USER_INTERRUPT`/`USER_RESPONSE`; host → phone: `MUSE_*`/`SESSION_*`).
`GET /console` is the development console (session, project, task, last command, agent state,
pending approval, recent events, cost when the backend reports it, latency). It is for
development only.

Transport security: token in the hello, TLS via `--ssl-certfile/--ssl-keyfile` (uvicorn) or a
reverse proxy; the Android client pins the host. See `security-model.md`.

### 3.10 Latency instrumentation (`museglass/host/latency.py`)

Spans recorded per session and shown in the console:
`speech_end→transcript`, `speech_end→agent_ack`, `agent_event→spoken`, `interrupt→agent_ack`.

## 4. Concurrency model

```
mic ──STT──▶ transcripts ──router──▶ intents ──┐
                                               ▼
                    ┌──────────── orchestrator (asyncio) ────────────┐
                    │ input pump   ──▶ AgentSession.{send,steer,     │
                    │                   interrupt,approve,answer}     │
                    │ agent pump   ◀── AgentSession.events()          │
                    │      │ store.append + summariser.feed           │
                    │      ▼                                          │
                    │ speech pump  ──▶ TTS (cancellable)              │
                    └──────────────────────────────────────────────────┘
```

A user interrupt (`stop`) cancels current speech, sends `interrupt()` to the backend and
records `interrupt→ack` latency when the backend confirms the turn ended. A steer never waits
for the agent to finish: Muse absorbs it into the running turn; Claude gets interrupt +
re-instruct.

## 5. Phase plan

- **Phase 0 (this repo, V0):** Mac-only loop with local STT/TTS, scripted + Muse + Claude
  adapters, policy, store, console, tests, demo repo.
- **Phase 1:** Android bridge (Kotlin). Meta Wearables Device Access Toolkit 0.9.0 for
  registration/permissions/camera; audio via the platform Bluetooth route (HFP for mic,
  A2DP for high-quality TTS). Same WebSocket event protocol.
- **Phase 2:** explicit "Muse, look at this" capture → `TurnInputPart{type:"image"}` (Muse
  supports image parts natively) / Claude image content block.
- **Phase 3:** Ray-Ban Display compact status cards via `mwdat-display` (Text/Button
  components) for approvals and completion.
