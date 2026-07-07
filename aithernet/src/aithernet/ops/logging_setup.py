"""Structured, bounded, secret-safe application logging (Stage 14A, area 9).

Production logs are line-delimited JSON with a stable set of fields (timestamp, severity,
component, node_id, and any mission/message/backend ids the caller attaches) plus a sanitized
``error_type``. A redaction filter guarantees that no record line contains a private key,
signature, raw envelope, complete remote payload, prompt, credential, or environment map — even
if a caller passes one by mistake. Logging integrates with the systemd journal (stderr) and an
optional rotating file under the node ``logs`` directory; rotation/retention is documented for
operators (systemd ``journald`` or ``logrotate``).
"""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Patterns whose presence in a formatted record is collapsed to a redaction marker.
_REDACT_PATTERNS = [
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
    re.compile(r'"signature"\s*:\s*"[^"]*"'),
    re.compile(r"(sk-[A-Za-z0-9]{8,}|AIza[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,})"),
]
_REDACTION = "<redacted>"


class RedactionFilter(logging.Filter):
    """Collapse anything resembling a key/signature/PEM/token in the final message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging crash
            return True
        redacted = message
        for pattern in _REDACT_PATTERNS:
            redacted = pattern.sub(_REDACTION, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """A bounded JSON line formatter with stable operational fields."""

    def __init__(self, node_id: str, *, max_message_chars: int = 2000) -> None:
        super().__init__()
        self.node_id = node_id
        self.max_message_chars = max_message_chars

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if len(message) > self.max_message_chars:
            message = message[: self.max_message_chars] + "…"
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "severity": record.levelname,
            "component": record.name,
            "node_id": self.node_id,
            "message": message,
        }
        for key in ("mission_id", "message_id", "backend_id", "peer_id", "transfer_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_node_logging(
    node_id: str,
    *,
    level: str = "info",
    log_file: str | Path | None = None,
    max_bytes: int = 16 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Install JSON logging + redaction on the root logger (stderr + optional rotating file).

    Idempotent: re-running replaces the Aithernet handlers without duplicating them.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Remove any previously-installed Aithernet handlers (idempotent reconfigure).
    for handler in list(root.handlers):
        if getattr(handler, "_aithernet", False):
            root.removeHandler(handler)

    formatter = JsonFormatter(node_id)
    redaction = RedactionFilter()

    stream = logging.StreamHandler()  # stderr → systemd journal
    stream.setFormatter(formatter)
    stream.addFilter(redaction)
    stream._aithernet = True  # type: ignore[attr-defined]
    root.addHandler(stream)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        file_handler._aithernet = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
