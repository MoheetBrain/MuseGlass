"""Secret redaction applied before anything is persisted or logged."""

from __future__ import annotations

import re

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),  # OpenAI / Anthropic style keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{16,}=*"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out
