# MuseGlass — a hands-free terminal for autonomous software engineering, built for Meta AI glasses

> *"Wait — you were walking around while your computer was coding for you?"*

MuseGlass turns Meta AI glasses into the conversational control surface for a coding agent
that runs on your Mac. You **see** a problem, **speak** a task, **delegate** it to
[Muse Code](https://dev.meta.ai/docs/muse-code) (Meta's terminal coding agent), **walk away**,
**hear** concise progress in your ear, **interrupt** the agent mid-task in plain English,
**approve** the sensitive steps, and **complete** real software work — without returning to
the keyboard.

```
SEE → SPEAK → DELEGATE → WALK AWAY → HEAR PROGRESS → INTERRUPT → APPROVE → COMPLETE
```

This is not voice control. It is **ambient software engineering**: the computer becomes a
remotely operating agent and the glasses become the way you talk to it.

---

## Why this exists

I built MuseGlass because I love Meta's AI glasses and I think their most exciting future is
not as a camera or a chat window, but as the place where you *direct work* while living your
life. Coding agents like Muse Code can now execute a whole task end-to-end. The missing piece
is the interaction model for a person who is **not at a desk**: how do you get *useful*
progress without narration, change your mind mid-task, and stay in control of anything
dangerous — all through a voice channel and, on Meta Ray-Ban Display, a few words on a lens?

MuseGlass is my attempt at that interaction model, engineered properly: a typed event
protocol, a real session store, a policy layer that keeps a human in the loop, a summariser
that decides what deserves to be spoken, and adapters that isolate the agent so the same
experience works with Muse Code first and other agents later. It is built against the
**current** Meta developer surfaces — the Muse Session Protocol v1 schema and the Wearables
Device Access Toolkit 0.9.0 — with every capability classified as confirmed, worked-around,
mocked or future (see [docs/build-plan.md](docs/build-plan.md)).

## The experience

```
You:   "Muse, open the demo project. Add a /health endpoint, include the current Git SHA
        and app version, write tests, and keep me updated."
Muse:  "Opened demo project."                        ← you put the laptop down and walk
Muse:  "I found the relevant module and I'm implementing the change."
You:   "Also include uptime."                        ← lands in the SAME running task
Muse:  "Implementation is done. Tests are running."
Muse:  "Done. I added the health endpoint and 4 tests. All 9 tests pass. Want me to commit it?"
You:   "Yes. Commit but don't push."
Muse:  "Committing."  …  "Committed locally. Nothing was pushed."
You:   "Muse, push it."
Muse:  "I want to push to the remote: git push origin HEAD. Approve?"   ← policy-gated
You:   "No. Commit locally only."
Muse:  "Understood. I did not push."
```

Every line above is produced by the system in this repository and verified by the
end-to-end test (`tests/test_orchestrator_e2e.py`), which edits real files and runs the demo
project's real pytest suite — no mocked conversations.

## Architecture at a glance

```
META GLASSES ──HFP/A2DP──▶ ANDROID BRIDGE ──WSS + token──▶ HOST (Mac / server)
  mic + speaker             phone app                     ├─ speech   (STT / TTS providers)
  (display later)                                         ├─ router   (control words vs content)
                                                          ├─ orchestrator (3 concurrent pumps)
                                                          ├─ summariser (milestones, not noise)
                                                          ├─ policy + workspace sandbox
                                                          ├─ SQLite session store (reconnect)
                                                          └─ agent adapters
                                                                 │ Muse Session Protocol (stdio JSON-RPC)
                                                                 ▼
                                                             muse serve → repo, shell, tests
```

Full design: [docs/architecture.md](docs/architecture.md).

### What makes it more than a voice wrapper

| concern | how MuseGlass handles it |
|---|---|
| **Concurrency** | Agent work, your speech and spoken progress run at the same time (asyncio pumps). Nothing is a blocking chatbot turn. |
| **Interruption is first-class** | "Stop", "pause", "continue", "don't touch the parser", "also benchmark it", "why are you doing that?" all affect the *running* task. On Muse Code this uses native mid-turn steering (`turn/steer`) and the priority interrupt lane. |
| **Speak milestones, not events** | A deterministic summariser folds hundreds of tool calls into "I found the module and I'm implementing the change" / "Tests are running" / "Two tests failed, I need a decision". Three verbosity modes (QUIET / NORMAL / TALKATIVE). Blockers, questions and approvals are never rate-limited. |
| **Security from day one** | The agent only works inside registered workspaces under `~/MuseWorkspaces/`. A policy engine classifies every concrete tool action: git push, deploys, destructive deletes, credential access, package installs, system settings, destructive migrations, financial actions and anything outside the workspace **require spoken approval**; deletion outside the project is refused outright. |
| **Persistence and reconnect** | SQLite stores session, project, task, pending approval and the ordered event log. A phone that drops and reconnects sends its last sequence number and hears "Your task is still running. Tests are currently executing." |
| **Replaceable adapters** | `CodingAgent` / `AgentSession` interface with three implementations: Muse Code (MSP), Claude Code (Agent SDK) and a scripted demo agent. `SpeechToTextProvider` / `TextToSpeechProvider` with local whisper, typed text and macOS `say`. |
| **Observability** | Developer console (session, project, task, agent state, pending approval, recent events, token/cost, latency) at `/console`; latency spans for speech-end→transcript, speech-end→agent-ack, event→spoken, interrupt→ack. |
| **Privacy** | No continuous recording, no audio or image retention by default, secrets redacted before persistence, no accounts, no cloud relay. Threat model in [docs/security-model.md](docs/security-model.md). |

## Built against the real Meta developer surfaces

- **Muse Code / Muse Session Protocol v1.** MuseGlass includes a Python MSP client written
  from the published schema (`meta-models/muse-code-sdk`, fingerprint `sha256:cfd31ee7…`)
  and validated by replaying Meta's recorded conformance transcripts through a fake host
  (`tests/test_msp_client.py`). It drives `muse serve` directly: `session/start` with a
  `workspaceRoot`, `turn/start` / `turn/steer` / `turn/interrupt`, `approval/requested` →
  `approval/decide` with feedback, `userInput/*` for questions, `session/resume` for
  reconnects. Findings and workarounds: [docs/meta-sdk-notes.md](docs/meta-sdk-notes.md).
- **Wearables Device Access Toolkit 0.9.0 (Android).** Registration, permissions and camera
  come from `mwdat-core` / `mwdat-camera`; audio is the platform Bluetooth route (HFP mic at
  8 kHz, A2DP for high-quality TTS), display cards from `mwdat-display` on Ray-Ban Display.
  Meta exposes no third-party speech APIs, so STT/TTS live on the phone or host by design.
  The Phase 1 Android client is structured so the DAT integration is one interface with a
  mock behind it (Mock Device Kit for hardware-free testing).

## Repository map

```
museglass/               Python package (host)
  protocol/              typed event protocol (USER_COMMAND … SESSION_RESUMED), redaction
  agent/                 CodingAgent interface; muse_msp/ (MSP client + adapter); claude/; scripted.py
  host/                  orchestrator, command router, policy engine, workspace sandbox, latency
  summariser/            progress summariser with verbosity modes
  speech/                STT/TTS interfaces; providers: local whisper (mlx), typed text, macOS say
  store/                 SQLite session store
  bridge/                WebSocket bridge for the phone + developer console
  apps/desktop_demo.py   the V0 desktop app (mic → agent → speakers)
examples/demo-fastapi/   the demo repository the acceptance task runs against
tests/                   unit, MSP conformance (recorded transcripts) and end-to-end tests
docs/                    architecture, build plan + decision table, Meta SDK notes,
                         security model, demo script, project status + ledger
scripts/                 workspace setup, git hooks
apps/android/            Phase 1 Kotlin client (after V0 acceptance)
```

## Quick start (macOS, Apple Silicon)

```bash
git clone https://github.com/MoheetBrain/code_with_meta_glasses.git museglass && cd museglass
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[claude,speech,dev]"
scripts/setup_workspaces.sh          # creates ~/MuseWorkspaces/demo-project as a real git repo
python -m pytest -q                  # unit + conformance + end-to-end (scripted agent)
```

Run the loop with the offline scripted agent (real edits, real tests, no credentials):

```bash
museglass --backend scripted --stt typed --console
# then type:  Muse, open the demo project. Add a /health endpoint with tests and keep me updated.
```

Run it for real, hands-free, with Muse Code:

```bash
curl -fsSL https://dev.meta.ai/install.sh | bash   # installs `muse`; run `muse` once and sign in
museglass --console                                # auto-selects muse, local whisper STT, macOS say TTS
```

Then walk around the room: *"Muse, open the demo project…"*. Open the console link that is
printed to watch state, approvals and latency.

Backends are auto-selected (`muse` → `claude` → `scripted`), or forced with `--backend`.
`--resume <session-id>` reconnects to a session after a disconnect.

## Status

V0 (Phase 0) is implemented end-to-end and tested with the scripted agent; the Muse and
Claude adapters are implemented against their real protocols and need only credentials to
run live. The precise, honest state of the project — what is verified, what is not, and
what is next — is always in [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) with a
chronological record in [docs/PROJECT_LEDGER.md](docs/PROJECT_LEDGER.md).

Roadmap: Phase 1 Android bridge (Wearables DAT + WebSocket), Phase 2 explicit "Muse, look
at this" vision capture (Muse turns accept image parts natively), Phase 3 compact status and
approval cards on Ray-Ban Display.

## About

Built by Moheet Ahmed. I want to spend my career making Meta's AI glasses the most natural
way to get things done in the world, and this project is what that looks like to me for
software engineering: an agent that works while you walk, a voice that says only what
matters, and a person who stays in charge.
