# Demo script (the V0 acceptance test)

Setup once:

```bash
scripts/setup_workspaces.sh        # ~/MuseWorkspaces/demo-project, git-initialised
museglass --console                # or: --backend scripted --stt typed  (offline)
```

Start the app, put the laptop down, and say everything below without touching the keyboard.

| # | you say | expected spoken response / behaviour | verifies |
|---|---------|--------------------------------------|----------|
| 1 | "Muse, open the demo project. Add a `/health` endpoint. Return service status, current Git SHA and app version. Write tests. Keep me updated." | "Opened demo project." then within seconds "I'm looking through the code…" / "I found the relevant module and I'm implementing the change." | start session, assign task, real repo |
| 2 | (while it works) "Also include uptime." | no wake word needed while busy; the instruction enters the same task. TALKATIVE mode would read the acknowledgement. | interrupt + steer into the running task |
| 3 | "What are you doing?" | one sentence of status ("Working on it, currently implementing. Files changed so far: main.py…") | detail on demand |
| 4 | wait | "Implementation is done. Tests are running." … "Done. I added the health endpoint and 4 tests. All 9 tests pass. Want me to commit it?" | tests actually execute; completion verified |
| 5 | "Yes. Commit but don't push." | "Committing." … "Committed locally. Nothing was pushed." | commit only after approval |
| 6 | "Muse, push it." | "I want to push to the remote: git push origin HEAD. Approve?" | approval gating |
| 7 | "No. Commit locally only." | "Understood. I did not push." | verbal denial with feedback |
| 8 | "Muse, be quiet." then another task | only questions/approvals/completion are spoken | verbosity |
| 9 | kill the app mid-task; `museglass --resume <id>` | "Your task is still running…" / "I'm still waiting for your answer." | reconnect/resume |

Success criteria checklist (from the brief):

- [ ] start voice session
- [ ] assign coding task
- [ ] Muse works on the actual repo
- [ ] receive useful spoken progress
- [ ] interrupt running task verbally
- [ ] Muse incorporates the interruption
- [ ] Muse asks for permission when appropriate
- [ ] respond verbally
- [ ] tests actually execute
- [ ] completion actually verified
- [ ] commit only after approval
- [ ] no unrestricted filesystem access
- [ ] session logs retained
- [ ] reconnect/resume works at least at basic level

The automated equivalent runs in `tests/test_orchestrator_e2e.py` with the scripted agent.
The checklist is only ticked in `PROJECT_STATUS.md` after a live run with a real LLM backend.
