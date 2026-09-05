# MuseGlass demo service

A deliberately tiny FastAPI app used as the target repository for the MuseGlass acceptance
demo. It has no `/health` endpoint on purpose: adding one (with the current Git SHA and app
version, plus tests) is the canonical task the voice-driven agent performs.

```bash
python -m pytest -q
uvicorn app.main:app --reload
```

`scripts/setup_workspaces.sh` copies this directory to `~/MuseWorkspaces/demo-project` and
initialises a git repository there so the agent works on a real repo, not on this template.
