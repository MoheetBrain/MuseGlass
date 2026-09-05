# Meta SDK notes (Muse Code + Wearables Device Access Toolkit)

Everything here was read from official sources on 2026-09-04. Where a fact comes from a
third-party article it is marked *(unverified)*.

## Muse Code

- Product: Meta's terminal coding agent (Meta Superintelligence Labs), launched in beta on
  2026-08-05, out of beta 2026-09-01 with subscription plans, an SDK developer preview,
  inter-session messaging and workflows. Model: Muse Spark 1.2 (1.3 announced 2026-09-03).
- Install: `curl -fsSL https://dev.meta.ai/install.sh | bash` (installer read, not executed:
  it installs a launcher named `muse` into `~/.local/bin`, launcher fetched from
  `https://api.meta.ai/muse-launcher.sh` with a sha256 check).
- Auth: browser sign-in on first run (`/login` inside a session); the docs overview names
  `META_API_KEY` for non-interactive environments. On macOS credentials are stored in the
  Keychain (SDK changelog 1.0.1). One third-party article names `MODEL_API_KEY` *(unverified)*.
- CLI: `muse` (interactive TUI), `muse exec "<prompt>"` (one prompt to completion),
  `muse serve` (MSP host over stdio), `muse schema` (protocol schema, e.g.
  `muse schema generate-json-schema`), `muse --version`. Default model `muse-spark-1.2`.
- Docs: https://dev.meta.ai/docs/muse-code (JS-rendered; sub-pages: Authentication and
  billing, Subscriptions, Permissions and safety, Working with the agent, Workflows, Session
  messaging, Rewind a conversation, Configuration and context, Extending and automating,
  Changelog). SDK docs: https://meta-models.github.io/muse-code-sdk.
- Hooks (CLI side): SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest,
  PostToolUse, PreLLMCall, PostLLMCall, PreCompact, PostCompact, SubagentStart, SubagentStop,
  Stop, SessionEnd, Notification.

### Muse Session Protocol (MSP) v1 — what MuseGlass implements

Source: `schema/msp/msp.d.ts` and `schema/msp/transcripts/*` in
https://github.com/meta-models/muse-code-sdk (stable manifest schemaVersion 1, fingerprint
`sha256:cfd31ee77d78fdada9febc4edccd29b0434ff8f6bf157c7c03fd0ecfcbc29f5a`).

- Transport: spawn `muse serve` (cwd = workspace); JSON-RPC 2.0, **one JSON object per line**
  (`\n`), UTF-8, default frame limit 10 MiB. Requests have `id`; notifications have none.
  The server also sends *requests* (`approval/request`, `userInput/request`) that the client
  must answer with `{"jsonrpc":"2.0","id":<id>,"result":{}}` — decisions travel separately as
  commands.
- Handshake: `initialize {clientInfo{name,version}, capabilities?}` → result with
  `schema.fingerprint`, `serverInfo`, `museHome`, `sessionDurability`; then the client sends
  the `initialized` notification. Fingerprint mismatch is a warning, not an error.
- Commands carry a client-minted UUIDv7 `commandId`; the ack is admission only
  (`status:"accepted"`); outcomes arrive as view notifications.
- `session/start {commandId, workspaceRoot, approvalMode?, modelId?}` → `{session, viewCursor}`.
  Approval modes (closed enum): `allowAll | promptUnmatched | onRequest | denyUnmatched`.
- `turn/start {sessionId, commandId, input:[{type:"text",text}|{type:"image",mediaType,base64Data}], ifBusy?: queue|steer|replace, reasoningEffort?}`
  → `{turnId, disposition: started|queued|steered}`.
- `turn/steer {sessionId, commandId, expectedTurnId, input}` — exact-target mid-turn steering.
- `turn/interrupt {sessionId, commandId, turnId?, retract?}` (priority lane), `turn/cancel`,
  `turn/unqueue`. Terminal: `turn/completed {terminal: completed|failed|cancelled, error?}`.
- Items: `item/started|updated|completed {item}` and `item/delta {itemId, field?, delta}`.
  Kinds: `userMessage, agentMessage, reasoning, toolCall(tool,args,visibleOutput,status,approvalId), userShell, subagent, workflow, reminderChild, compaction`.
  Unknown kinds must be rendered generically (`fallbackText`).
- Approvals: `approval/requested {approvalId, subject{kind: shell|fileAccess|network|process|tool, command?, path?}, toolName, rawArgs, availableChoices[{choiceId, decision, scope, acceptsFeedback?}], currentRequirementId{approvalId, sourceIndex}}`
  → `approval/decide {sessionId, commandId, approvalId, requirementId, choiceId, feedback?}`
  → `approval/resolved`. Typical choice ids seen in transcripts: `allow_once`, `allow_session`, `abort`.
- Questions: `userInput/requested {userInputId, questions[{id, header, question, options[{label}], selection{mode}}]}`
  → `userInput/answer {answers:[{questionId, selectedLabel|freeText}]}` or `userInput/clarify`/`userInput/cancel`.
- Resume: `session/resume {commandId, sessionId, cursor?, history?, excludeItems?}` → history
  (`inline|snapshot|anchoredSnapshot|none`) + `pendingRequests` (re-issued as server requests).
  `session/list`, `session/read`, `view/page` for cold reads. Errors: `sessionInUse`,
  `sessionNotFound`, `commandRejected`, `backpressured`…
- Other: `session/setApprovalMode`, `session/setModel`, `model/list`, `session/compact`,
  `session/userShell` (capability `userShell` must be requested at initialize), subagent
  commands. `goal/*` appear in some transcripts but are **not** on the stable v1 method list, so
  MuseGlass does not use them.

MuseGlass workarounds recorded:

1. Official SDK is TypeScript only → Python client written from the schema, replay-tested
   against the transcript corpus (`tests/test_msp_client.py`).
2. No pause primitive → `turn/interrupt` + a later "continue" turn.
3. Human approval policy is MuseGlass's own layer on top of Muse's approval modes: the session
   runs in `promptUnmatched` (host prompts for unmatched actions); MuseGlass auto-decides safe
   in-workspace actions and asks the user by voice for the risky classes.

## Meta Wearables Device Access Toolkit (DAT)

- Developer preview (announced 2025-12); publishing not available yet; release channels for
  testers. Supported glasses: Ray-Ban Meta Gen 1/Gen 2, Ray-Ban Meta Display, Oakley Meta
  HSTN, Oakley Meta Vanguard. iOS 15.2+, Android 10+; developer mode via the Meta AI app.
- Android SDK **0.9.0**, GitHub Packages Maven `https://maven.pkg.github.com/facebook/meta-wearables-dat-android`:
  `com.meta.wearable:mwdat-core`, `mwdat-camera`, `mwdat-display`, `mwdat-mockdevice`.
  iOS reference at 0.8. Repos: `facebook/meta-wearables-dat-android`, `facebook/meta-wearables-dat-ios`.
- Audio: **no DAT audio API.** Microphone = Bluetooth HFP (8 kHz mono, beamformed, request via
  platform permission dialogs; Android `AudioManager.setCommunicationDevice(TYPE_BLUETOOTH_SCO)`
  + `MODE_IN_COMMUNICATION`). Speaker = A2DP (44.1/48 kHz stereo, output only) or HFP.
  HFP and A2DP are mutually exclusive; switching takes ~2 s to stabilise. Implication for
  MuseGlass: STT quality is bounded by 8 kHz HFP audio; TTS should use A2DP while no capture
  is active, or accept HFP quality for full-duplex.
- Camera: `mwdat-camera` video stream + single-frame capture (Phase 2).
- Display: `mwdat-display` FlexBox/Text/Image/Button/Icon + MP4 (Display glasses only; Mock
  Device Kit has no display support) (Phase 3).
- Neural Band: only predefined gestures; no custom gestures.
- Voice: no wake word / Meta AI voice integration for third-party apps → "Muse …" wake word is
  handled by MuseGlass's own STT.
- Docs: https://wearables.developer.meta.com/docs/develop/dat (integration overview,
  `microphones-and-speakers`, `build-integration-android`, reference `android/dat/0.9`).
