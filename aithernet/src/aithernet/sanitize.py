"""Shared secret-redaction utility.

A single place to mask common secret/token patterns before they reach errors, logs,
events, API responses, or the UI. Used by coordinator providers and the MCP session layer.
"""

from __future__ import annotations

import re

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{4,}")
_SK = re.compile(r"sk-[A-Za-z0-9._\-]{4,}")
_GOOGLE = re.compile(r"ya29\.[A-Za-z0-9._\-]{4,}")
_KEYVAL = re.compile(
    r'(?i)("?(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|token|'
    r'password|client[_-]?secret)"?\s*[:=]\s*"?)[^"\s,}]{4,}'
)


def redact_secrets(text: str) -> str:
    """Mask common secret/token patterns so they never reach errors, logs, events, or UI."""
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _SK.sub("[REDACTED]", text)
    text = _GOOGLE.sub("[REDACTED]", text)
    text = _KEYVAL.sub(r"\1[REDACTED]", text)
    return text
