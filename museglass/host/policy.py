"""Human-approval policy for agent actions.

`classify_action()` looks at the *concrete* tool input (never at model prose) and decides
whether the action is SAFE (auto-approved), NEEDS_APPROVAL (ask the human by voice) or
FORBIDDEN (denied outright). See docs/security-model.md for the category table.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from museglass.host.workspace import WorkspaceRegistry


class Risk(str, Enum):
    SAFE = "SAFE"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True)
class Decision:
    risk: Risk
    category: str
    reason: str

    @property
    def needs_human(self) -> bool:
        return self.risk is not Risk.SAFE


SAFE = Decision(Risk.SAFE, "safe", "inside workspace")

# Ordered: first match wins. (regex on the normalised command, category, risk)
_COMMAND_RULES: list[tuple[re.Pattern[str], str, Risk]] = [
    (re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+merge\b|\bgh\s+release\s+create\b"), "git_push", Risk.NEEDS_APPROVAL),
    (re.compile(r"\b(kubectl\s+(apply|delete|rollout)|helm\s+(install|upgrade|uninstall)|terraform\s+(apply|destroy)|pulumi\s+up|vercel\s+(deploy|--prod)|fly\s+deploy|serverless\s+deploy|sls\s+deploy|gcloud\s+.*deploy|aws\s+.*deploy|heroku\s+.*(push|release)|docker\s+push|npm\s+publish|twine\s+upload|cargo\s+publish|gem\s+push)\b"), "deploy", Risk.NEEDS_APPROVAL),
    (re.compile(r"\b(mkfs|diskutil\s+(erase|partition)|dd\s+if=|shutdown|reboot|halt|launchctl\s+(unload|bootout|remove))\b|\bkill\s+-9\s+-1\b|\bkillall\b"), "destructive_command", Risk.FORBIDDEN),
    (re.compile(r"\bgit\s+(clean\s+-[a-z]*f|reset\s+--hard|checkout\s+--\s+\.|restore\s+\.|branch\s+-D|push\s+.*--force|filter-branch|reflog\s+expire)"), "destructive_delete", Risk.NEEDS_APPROVAL),
    (re.compile(r"\b(sudo|doas)\b|\bdefaults\s+write\b|\bsystemsetup\b|\bnetworksetup\b|\bcsrutil\b|\bspctl\b|\bchmod\s+(-R\s+)?[0-7]*7[0-7]*\s+/\s*$|\bchown\s+-R\s+.*\s+/"), "system_settings", Risk.NEEDS_APPROVAL),
    (re.compile(r"\b(alembic\s+downgrade|prisma\s+migrate\s+reset|rails\s+db:(drop|reset)|django-admin\s+flush|manage\.py\s+flush|flyway\s+clean|dropdb)\b|\bDROP\s+(TABLE|DATABASE|SCHEMA)\b|\bTRUNCATE\s+TABLE\b|\bDELETE\s+FROM\s+\w+\s*;?\s*$"), "db_migration", Risk.NEEDS_APPROVAL),
    (re.compile(r"\b(stripe\s+(charges|payment_intents|transfers|payouts)|aws\s+(marketplace|budgets)|paypal|braintree)\b|\b(purchase|buy|checkout|transfer\s+funds|wire\s+transfer)\b"), "financial", Risk.NEEDS_APPROVAL),
    (re.compile(r"(\bcat|\bless|\bmore|\bopen|\bcp|\bscp|\bcurl\s+.*-F|\bbase64)\b.*(~/\.ssh|\.ssh/|~/\.aws|\.aws/credentials|~/\.gnupg|\.netrc|\.pypirc|\.npmrc|\.env\b|id_rsa|id_ed25519|\.pem\b|\.p12\b|keychain)|\bsecurity\s+(find-|dump-)|\bgh\s+auth\s+token\b|\bop\s+(read|item\s+get)\b|\bpass\s+show\b|\bprintenv\b.*(KEY|TOKEN|SECRET)|\becho\s+\$\w*(KEY|TOKEN|SECRET|PASSWORD)\w*"), "credential_access", Risk.NEEDS_APPROVAL),
    (re.compile(r"\b(pip3?|uv\s+pip|pipx|poetry|conda|mamba)\s+(install|add)\b|\buv\s+add\b|\bnpm\s+(install|i|add)\s+\S|\byarn\s+add\b|\bpnpm\s+(add|install)\s+\S|\bbrew\s+(install|cask)\b|\bcargo\s+install\b|\bgo\s+install\b|\bgem\s+install\b|\bapt(-get)?\s+install\b|\bcurl\b[^|]*\|\s*(ba)?sh\b|\bwget\b[^|]*\|\s*(ba)?sh\b"), "package_install", Risk.NEEDS_APPROVAL),
]

_RM_RE = re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+(?P<flags>(?:-\w+\s+)*)(?P<targets>[^;&|]+)")
_PATH_ARG_KEYS = ("path", "file_path", "filePath", "notebook_path", "directory", "cwd", "target", "dest", "source")
_SECRET_PATH_RE = re.compile(r"(^|/)(\.ssh|\.aws|\.gnupg|\.netrc|\.pypirc|\.npmrc|\.env(\.[\w-]+)?|id_rsa|id_ed25519|.*\.pem|.*\.p12|.*\.keystore)$")


def _looks_like_path(token: str) -> bool:
    return token.startswith(("/", "~", "./", "../")) or token.startswith("$HOME")


def _expand(token: str, root: Path) -> Path:
    t = token.strip("'\"")
    t = t.replace("$HOME", str(Path.home())).replace("${HOME}", str(Path.home()))
    p = Path(t).expanduser()
    return p if p.is_absolute() else root / p


def classify_command(command: str, root: Path) -> Decision:
    """Classify a shell command executed with `root` as the working directory."""
    cmd = command.strip()
    if not cmd:
        return SAFE
    for pattern, category, risk in _COMMAND_RULES:
        if pattern.search(cmd):
            return Decision(risk, category, f"matched {category} rule")
    for m in _RM_RE.finditer(cmd):
        flags = m.group("flags") or ""
        recursive = "r" in flags.lower() or "-R" in flags
        for token in shlex.split(m.group("targets"), posix=True) if _safe_split(m.group("targets")) else m.group("targets").split():
            if token.startswith("-"):
                continue
            target = _expand(token, root)
            if token in ("/", "~", "*", "/*") or target == Path.home() or target == Path("/"):
                return Decision(Risk.FORBIDDEN, "destructive_delete", f"refusing to delete {token}")
            if not WorkspaceRegistry.is_within(target, root):
                return Decision(Risk.FORBIDDEN, "destructive_delete", f"delete outside workspace: {token}")
            if recursive and target == root:
                return Decision(Risk.NEEDS_APPROVAL, "destructive_delete", "recursive delete of the whole workspace")
    # Any absolute path outside the workspace that is written to or read from.
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()
    for token in tokens:
        bare = token.split("=", 1)[-1] if "=" in token and not token.startswith("-") else token
        if _looks_like_path(bare):
            target = _expand(bare, root)
            if _SECRET_PATH_RE.search(str(target)):
                return Decision(Risk.NEEDS_APPROVAL, "credential_access", f"touches {bare}")
            if not WorkspaceRegistry.is_within(target, root) and not _is_system_readonly(target):
                return Decision(Risk.NEEDS_APPROVAL, "outside_workspace", f"path outside workspace: {bare}")
    return SAFE


def _safe_split(s: str) -> bool:
    try:
        shlex.split(s, posix=True)
        return True
    except ValueError:
        return False


_SYSTEM_RO_PREFIXES = ("/usr", "/bin", "/sbin", "/opt", "/Library", "/System", "/dev/null", "/tmp", "/private/tmp", "/var/folders", "/etc")


def _is_system_readonly(path: Path) -> bool:
    s = str(path)
    if s.startswith(str(Path.home())):
        return False
    return s.startswith(_SYSTEM_RO_PREFIXES)


def classify_action(tool_name: str, tool_input: dict[str, Any] | None, root: Path | str) -> Decision:
    """Classify one tool invocation. Works for Muse (`tool`, `args`), Claude (`Bash`, `Edit`…)
    and generic `{command}` / `{path}` shapes."""
    root = Path(root).resolve()
    inp = tool_input or {}
    name = (tool_name or "").lower()
    command = inp.get("command") or inp.get("cmd") or inp.get("commandText")
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    if command and (name in ("bash", "shell", "run_shell", "execute", "exec", "terminal", "run_command", "sh") or "shell" in name or "bash" in name or "command" in name):
        return classify_command(str(command), root)
    if command and not any(k in inp for k in _PATH_ARG_KEYS):
        return classify_command(str(command), root)
    # File tools: every path argument must stay inside the workspace.
    for key in _PATH_ARG_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value:
            target = _expand(value, root)
            if _SECRET_PATH_RE.search(str(target)):
                return Decision(Risk.NEEDS_APPROVAL, "credential_access", f"touches {value}")
            if not WorkspaceRegistry.is_within(target, root):
                if name in ("delete", "remove", "rm", "delete_file"):
                    return Decision(Risk.FORBIDDEN, "destructive_delete", f"delete outside workspace: {value}")
                return Decision(Risk.NEEDS_APPROVAL, "outside_workspace", f"path outside workspace: {value}")
    if name in ("webfetch", "web_fetch", "fetch", "http", "network") or inp.get("url"):
        url = str(inp.get("url") or "")
        if url and not url.startswith(("https://docs.", "https://pypi.org", "https://registry.npmjs.org")):
            return Decision(Risk.NEEDS_APPROVAL, "network", f"network request to {url or 'unknown host'}")
    return SAFE


def describe_for_speech(tool_name: str, tool_input: dict[str, Any] | None, decision: Decision) -> str:
    """A short spoken description of the action needing approval."""
    inp = tool_input or {}
    command = inp.get("command") or inp.get("commandText")
    if command:
        text = str(command).strip()
        if len(text) > 80:
            text = text[:77] + "…"
        verb = {
            "git_push": "push to the remote",
            "deploy": "deploy",
            "package_install": "install a package",
            "destructive_delete": "delete files",
            "credential_access": "read credentials",
            "system_settings": "change system settings",
            "db_migration": "run a destructive database migration",
            "financial": "perform a financial action",
            "outside_workspace": "touch a path outside the project",
        }.get(decision.category, "run a command")
        return f"I want to {verb}: {text}. Approve?"
    for key in _PATH_ARG_KEYS:
        if inp.get(key):
            return f"I want to access {inp[key]}, which is outside the project. Approve?"
    return f"I need approval to use {tool_name}. Approve?"
