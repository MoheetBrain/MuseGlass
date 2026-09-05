# MuseGlass security model and threat model

## Assets

1. The user's Mac: filesystem, credentials, shell.
2. Registered repositories under `~/MuseWorkspaces/`.
3. Remote services reachable from the Mac (git remotes, deployment targets, package indexes).
4. The user's speech and, later, camera frames.

## Trust boundaries

```
glasses ─HFP/A2DP─ phone app ─WSS+token─ host bridge ─in-process─ orchestrator ─MSP/SDK─ agent process ─OS─ workspace
```

- The agent process is **untrusted**: it executes model-authored actions.
- The phone app is trusted only after presenting the pre-shared token over TLS.
- Speech is untrusted input: the router only reacts to a small control grammar; everything
  else is passed to the agent as text and is subject to the same approval policy.

## Controls

### Workspace sandbox

- The agent is started with the registered project directory as its only root
  (Muse `workspaceRoot`; Claude `cwd`, no `add_dirs`).
- `WorkspaceRegistry.is_within()` resolves symlinks (`realpath`) before comparing paths.
- Any tool call whose path argument resolves outside the workspace is classified
  `outside_workspace` and needs human approval (deletion outside the project is `FORBIDDEN`).
- Backends add their own layer: Muse's OS sandbox and approval modes; Claude's permission
  system with `can_use_tool` and a PreToolUse hook.

### Approval gating (human-in-the-loop by category)

`museglass/host/policy.py` classifies every tool action. Always requires spoken approval:

| category             | examples                                                                          |
|----------------------|-----------------------------------------------------------------------------------|
| `git_push`           | `git push`, `git push --force`, `gh pr merge`                                     |
| `deploy`             | `kubectl apply`, `terraform apply`, `vercel deploy`, `fly deploy`, `serverless deploy` |
| `destructive_delete` | `rm -rf` outside the workspace (FORBIDDEN), `git clean -fdx`, `git reset --hard`, `git branch -D` |
| `destructive_command`| `mkfs`, `dd`, `diskutil erase`, `kill -9 -1`, `shutdown`, `launchctl`            |
| `credential_access`  | reading `~/.ssh`, `~/.aws`, `.env`, keychain, `gh auth token`, `security find-*`  |
| `package_install`    | `pip install`, `npm install <pkg>`, `brew install`, `curl … | sh`                  |
| `system_settings`    | `defaults write`, `sudo`, `chmod 777 /`, `systemsetup`, `networksetup`            |
| `db_migration`       | `alembic downgrade`, `DROP TABLE`, `prisma migrate reset`, `rails db:drop`         |
| `financial`          | `stripe`, `aws … purchase`, anything matching payment/transfer verbs               |
| `outside_workspace`  | any read/write/exec path outside the registered root                             |

Everything else inside the workspace (reads, edits, running the project's tests, `git add`,
`git commit`, `git status/diff/log`) is `SAFE` and auto-approved so the user is not pestered.

Approval requests are typed events (`MUSE_APPROVAL_REQUEST` with `metadata.request_id`,
`category`, `choices`). A spoken "no" carries feedback to the agent. Backends run in a mode
where the *host* prompts (Muse `promptUnmatched`; Claude `can_use_tool`) so the policy engine
is always in the path.

### Transport

- WebSocket bridge requires a token (`MUSEGLASS_TOKEN`, generated on first run and stored in
  `~/.museglass/config.json`, mode 600). Missing/invalid token → connection closed before any
  event is sent.
- TLS: run uvicorn with a certificate, or terminate TLS in a reverse proxy/Tailscale. The
  Android client pins the host certificate fingerprint (Phase 1).
- The bridge binds to `127.0.0.1` by default; binding to a LAN address is an explicit flag.

### Privacy

- The microphone is only streamed while a session is active and the wake word/dialog is
  expected; audio is processed in memory and **not written to disk** unless
  `MUSEGLASS_DEBUG_AUDIO=1`.
- Camera capture (Phase 2) is explicit and user-triggered; frames are sent to the agent for
  the turn and not retained unless the user says "save it".
- Logs and the session store redact secrets: values matching common token shapes
  (`AKIA…`, `ghp_…`, `sk-…`, `Bearer …`, `password=`) are replaced with `[REDACTED]` before
  persistence (`museglass/protocol/redact.py`).
- No accounts, no cloud relay, no telemetry.

## Threat model

| threat | mitigation |
|---|---|
| Model executes a destructive command | policy classification + backend approval prompts + OS sandbox (Muse) |
| Model exfiltrates secrets via network | `credential_access` gating, workspace-only file access, `curl`/`wget` with non-repo URLs need approval |
| Someone on the LAN sends commands to the host | token + TLS + loopback default |
| Replay of an old approval | approvals are single-use (`request_id`), and Muse's `requirementId` guard rejects stale decisions |
| Misheard "yes" | approvals are read back with the concrete action ("push branch X?") and require an explicit affirmative; "no" or silence = deny after timeout |
| Prompt injection through repo content | the agent's own instruction hygiene; the policy engine does not trust model text — it classifies concrete tool inputs |
| Audio/camera retained without consent | off by default, explicit flags, documented |

## Non-goals for V0

Multi-user isolation, remote (non-loopback) deployment hardening, and OS-level sandboxing
beyond what the backend provides.
