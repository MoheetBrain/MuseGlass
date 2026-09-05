# MuseGlass Build Plan

Date: 2026-09-04. Environment inspected: macOS (Apple M5 Max, 48 GB), Python 3.12/3.14,
Node 24, Claude Code CLI 2.1.222 (not signed in), Codex CLI 0.152.1, `ffmpeg` 9, macOS `say`,
no `muse` binary installed, no `~/MuseWorkspaces`, no speech API keys in the environment.

## 1. Capability audit (what is actually available today)

Legend: **C** confirmed from official docs/schema · **W** workaround implemented ·
**M** mock required · **F** future integration.

### Muse Code (Meta) — the first backend

Source of truth: `meta-models/muse-code-sdk` (schema `schema/msp/msp.d.ts` v1,
fingerprint `sha256:cfd31ee7…`, 48 recorded conformance transcripts) and the Muse Code docs
overview at `dev.meta.ai/docs/muse-code`. Details in `meta-sdk-notes.md`.

| required capability                    | status | how |
|----------------------------------------|--------|-----|
| programmatic session creation          | C | `muse serve` + `initialize` handshake + `session/start {workspaceRoot}` |
| persistent sessions / resume           | C | sessions are durable logs; `session/resume {sessionId, cursor?}` returns history + pending requests |
| progress / event streaming             | C | notifications `turn/*`, `item/started|delta|updated|completed`, `session/tokenUsage`, … |
| user messages during execution         | C | `turn/steer {expectedTurnId}` or `turn/start {ifBusy:"steer"|"queue"}` |
| approvals                              | C | `approval/requested` → `approval/decide {choiceId, feedback?}`; modes `allowAll|promptUnmatched|onRequest|denyUnmatched` selected at `session/start` |
| questions from the agent               | C | `userInput/requested` → `userInput/answer|clarify|cancel` |
| cancellation / interrupt               | C | `turn/interrupt` (priority lane) and `turn/cancel`; terminal arrives as `turn/completed {terminal:"cancelled"}` |
| pause / resume a task                  | W | no pause method: interrupt, mark paused, resume with a "continue" turn |
| repository selection                   | C | `workspaceRoot` on `session/start`; `session/list {workspaceRoot}` |
| custom tools                           | F | not on the v1 stable schema (hooks/skills exist CLI-side); not needed for V0 |
| official SDK in Python                 | W | official SDK is TypeScript (`@muse-code/sdk` 0.1.1). MuseGlass implements a small Python MSP client from the schema and validates it against the transcript corpus |
| credentials                            | **blocked** | `muse` not installed; sign-in requires the user (browser/device flow or API key) |

### Claude Code — second backend (already installed)

| capability | status | how |
|---|---|---|
| sessions, resume | C | `ClaudeSDKClient(ClaudeAgentOptions(cwd, resume=…))` |
| event stream | C | `receive_messages()` → `AssistantMessage/ToolUseBlock/ToolResultBlock/ResultMessage` |
| approvals | C | `can_use_tool` callback → `PermissionResultAllow/Deny` |
| interrupt | C | `client.interrupt()` |
| mid-turn steer | W | not exposed: interrupt + re-instruct with context |
| workspace restriction | C+W | `cwd`, no `add_dirs`, `can_use_tool` path check, PreToolUse hook |
| credentials | **blocked** | `claude auth status` → `loggedIn: false` |

### Meta Wearables Device Access Toolkit (Phase 1) — Android 0.9.0

| capability | status | how |
|---|---|---|
| app registration, permissions, device state | C | `mwdat-core` |
| glasses microphone to the app | C | platform Bluetooth HFP (8 kHz mono, beamformed); no DAT audio API |
| TTS to the glasses                | C | platform A2DP (stereo, output only) or HFP (mutually exclusive) |
| camera frame / photo              | C | `mwdat-camera` (Phase 2) |
| display                           | C (Display glasses only) | `mwdat-display` components (Phase 3); Mock Device Kit does not support display |
| Meta speech APIs for 3rd parties  | **not available** | STT/TTS stay on phone or host |
| test without hardware             | C | `mwdat-mockdevice` |
| publishing                        | F | not available during developer preview; release channels for testers |

## 2. Technology decisions

| decision | choice | alternatives considered | why |
|---|---|---|---|
| Host language | Python 3.12, asyncio | TypeScript (official Muse SDK) | claude-agent-sdk, mlx-whisper and the test tooling are Python; MSP is a documented stdio protocol with a conformance corpus explicitly meant for other-language clients. One runtime on the host. |
| Muse integration | Python MSP v1 client over `muse serve` | Node sidecar with `@muse-code/sdk`; `muse exec` batch | Sidecar doubles the runtimes and adds an IPC layer; `muse exec` has no steering or approvals mid-run. |
| Agent abstraction | `CodingAgent`/`AgentSession` + `AgentEvent` | Couple directly to MSP | Required by the brief; also lets the scripted agent run the loop without credentials. |
| STT (V0) | local `mlx-whisper` + WebRTC VAD; typed text for tests | Deepgram/OpenAI realtime, Apple Speech via PyObjC | No keys available; Apple Silicon runs whisper fast; VAD gives endpointing. Cloud streaming is a drop-in provider slot for lower latency. |
| TTS (V0) | macOS `say` | ElevenLabs/OpenAI TTS, local Qwen3-TTS | Zero setup, offline, killable for barge-in. Provider interface allows better voices later. |
| Persistence | SQLite (stdlib) | JSON files, Redis | Real, transactional, zero dependencies; simplest mechanism that survives disconnects. |
| Phone ↔ host transport | WebSocket (FastAPI/uvicorn) with token, TLS-capable | gRPC, MQTT | Bidirectional, trivial from Kotlin (OkHttp), browsers can hit the console. |
| Summariser | deterministic phase machine | LLM summarisation | Testable, zero latency, no cost; can be upgraded to LLM-assisted later. |
| Intent routing | rule grammar for control words; everything else goes to the agent | LLM intent classifier | Predictable in noisy audio; the agent already understands free text. |
| Demo repo | FastAPI + pytest | Flask | Matches the brief; tiny and fast tests. |

## 3. Smallest path to V0

1. Event protocol + agent interface + workspace sandbox + policy engine (pure, unit-tested).
2. SQLite session store with reconnect replay.
3. Progress summariser with verbosity modes (unit-tested on synthetic event streams).
4. Speech interfaces with typed-text STT and `say` TTS; local whisper STT provider.
5. Command router + orchestrator (concurrent pumps; steering, stop/pause/continue,
   approvals, questions, completion → commit prompt).
6. Scripted demo agent that performs the canonical task for real on the demo repo → the
   end-to-end test proves the loop with real files and real pytest, offline.
7. Muse MSP client + adapter, validated against the transcript corpus with a fake host.
8. Claude adapter via `claude-agent-sdk`.
9. Bridge WebSocket + developer console; latency instrumentation.
10. Desktop demo CLI wiring everything (mic → agent → speakers), `scripts/setup_workspaces.sh`.

Acceptance (`docs/demo-script.md`) is only claimed once a real LLM backend performs the demo
task hands-free, which needs one of the two credentials the user must supply.

## 4. Blockers requiring the user

- Install Muse Code (`curl -fsSL https://dev.meta.ai/install.sh | bash`) and sign in
  (`muse`, then `/login`; or set the documented API-key variable for headless use), **or**
  run `claude auth login` for the Claude backend. Either unblocks the live demo.
- Microphone permission for the terminal app running `museglass` (macOS prompts once).

## 5. Later phases (not started until V0 is accepted)

- Phase 1 Android: Kotlin app with `WearablesBridge` interface (real DAT + mock), OkHttp
  WebSocket client, HFP audio capture, A2DP TTS playback, session state screen.
- Phase 2 vision: explicit capture → `TurnInputPart{type:"image"}`.
- Phase 3 display: compact status cards.
