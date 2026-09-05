"""Workspace registry and sandbox path checks.

The agent may only ever work inside a registered project under the workspaces root
(default `~/MuseWorkspaces`, override `MUSEGLASS_WORKSPACES`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from museglass.agent.interface import Workspace

DEFAULT_ROOT = Path.home() / "MuseWorkspaces"
REGISTRY_FILE = ".museglass-projects.json"


class WorkspaceError(Exception):
    pass


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t != "project"}


@dataclass
class WorkspaceRegistry:
    root: Path

    def __init__(self, root: Path | str | None = None) -> None:
        env_root = os.environ.get("MUSEGLASS_WORKSPACES")
        self.root = Path(root or env_root or DEFAULT_ROOT).expanduser().resolve()

    # -- registry ------------------------------------------------------------------------
    @property
    def registry_path(self) -> Path:
        return self.root / REGISTRY_FILE

    def _load(self) -> dict[str, dict]:
        if not self.registry_path.exists():
            return {}
        try:
            data = json.loads(self.registry_path.read_text())
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"corrupt registry {self.registry_path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def register(self, project_id: str, path: Path | str | None = None, display_name: str = "") -> Workspace:
        """Register a project directory. It must live under the workspaces root."""
        project_id = _slug(project_id)
        if not project_id:
            raise WorkspaceError("project id must contain letters or digits")
        target = Path(path) if path else self.root / project_id
        target = target.expanduser().resolve()
        if not self.is_within(target, self.root) or target == self.root:
            raise WorkspaceError(f"{target} is not inside the workspaces root {self.root}")
        if not target.is_dir():
            raise WorkspaceError(f"{target} does not exist")
        data = self._load()
        data[project_id] = {"path": str(target), "display_name": display_name or project_id.replace("-", " ")}
        self._save(data)
        return Workspace(project_id=project_id, root=target, display_name=data[project_id]["display_name"])

    def list(self) -> list[Workspace]:
        out = []
        for pid, info in sorted(self._load().items()):
            path = Path(info["path"])
            if path.is_dir():
                out.append(Workspace(project_id=pid, root=path, display_name=info.get("display_name", pid)))
        # Also expose unregistered directories directly under the root (read-only discovery).
        known = {w.project_id for w in out}
        if self.root.is_dir():
            for child in sorted(self.root.iterdir()):
                if child.is_dir() and not child.name.startswith(".") and _slug(child.name) not in known:
                    out.append(Workspace(project_id=_slug(child.name), root=child, display_name=child.name.replace("-", " ")))
        return out

    def get(self, project_id: str) -> Workspace | None:
        pid = _slug(project_id)
        for ws in self.list():
            if ws.project_id == pid:
                return ws
        return None

    def resolve_spoken(self, spoken: str) -> Workspace | None:
        """Match "the demo project" → demo-project. Exact slug first, then token overlap."""
        pid = _slug(spoken)
        exact = self.get(pid)
        if exact:
            return exact
        wanted = _tokens(spoken)
        wanted.discard("the")
        best: tuple[int, Workspace] | None = None
        for ws in self.list():
            overlap = len(wanted & (_tokens(ws.project_id) | _tokens(ws.display_name)))
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, ws)
        return best[1] if best else None

    # -- sandbox -------------------------------------------------------------------------
    @staticmethod
    def is_within(path: Path | str, root: Path | str) -> bool:
        """True when `path` (after resolving symlinks and `..`) is inside `root`."""
        try:
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = Path(root) / p
            resolved = p.resolve()
            root_resolved = Path(root).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        return resolved == root_resolved or root_resolved in resolved.parents

    def contains(self, path: Path | str, workspace: Workspace) -> bool:
        return self.is_within(path, workspace.root)
