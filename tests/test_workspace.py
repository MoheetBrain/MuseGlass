from pathlib import Path

import pytest

from museglass.host.workspace import WorkspaceError, WorkspaceRegistry


def test_register_list_resolve(workspaces_root: Path):
    (workspaces_root / "demo-project").mkdir()
    (workspaces_root / "project-a").mkdir()
    registry = WorkspaceRegistry(workspaces_root)
    ws = registry.register("demo-project")
    assert ws.root == (workspaces_root / "demo-project").resolve()
    ids = {w.project_id for w in registry.list()}
    assert ids == {"demo-project", "project-a"}  # unregistered dirs are discoverable too
    assert registry.resolve_spoken("the demo project").project_id == "demo-project"
    assert registry.resolve_spoken("demo").project_id == "demo-project"
    assert registry.resolve_spoken("project a").project_id == "project-a"
    assert registry.resolve_spoken("nonexistent thing") is None


def test_cannot_register_outside_root(workspaces_root: Path, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    registry = WorkspaceRegistry(workspaces_root)
    with pytest.raises(WorkspaceError):
        registry.register("evil", elsewhere)
    with pytest.raises(WorkspaceError):
        registry.register("root", workspaces_root)


def test_is_within_resolves_symlinks_and_dotdot(workspaces_root: Path, tmp_path: Path):
    project = workspaces_root / "p"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escape").symlink_to(outside)
    assert WorkspaceRegistry.is_within(project / "src" / "x.py", project)
    assert not WorkspaceRegistry.is_within(project / ".." / "other", project)
    assert not WorkspaceRegistry.is_within(project / "escape" / "f", project)
    assert not WorkspaceRegistry.is_within(outside, project)
