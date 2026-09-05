from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from museglass.agent.interface import Workspace
from museglass.host.workspace import WorkspaceRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_TEMPLATE = REPO_ROOT / "examples" / "demo-fastapi"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def workspaces_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "MuseWorkspaces"
    root.mkdir()
    monkeypatch.setenv("MUSEGLASS_WORKSPACES", str(root))
    return root


@pytest.fixture
def demo_workspace(workspaces_root: Path) -> tuple[WorkspaceRegistry, Workspace]:
    """A real copy of the demo repo, git-initialised with one commit, registered as demo-project."""
    target = workspaces_root / "demo-project"
    shutil.copytree(DEMO_TEMPLATE, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    assert git(target, "init", "-q", "-b", "main").returncode == 0
    assert git(target, "add", "-A").returncode == 0
    assert git(target, "commit", "-q", "-m", "Initial demo service").returncode == 0
    registry = WorkspaceRegistry(workspaces_root)
    workspace = registry.register("demo-project", target, display_name="demo project")
    return registry, workspace


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 30.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)
