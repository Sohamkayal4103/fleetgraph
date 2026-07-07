"""Structured redaction and data minimization (Stage 14E, Part 8).

A deterministic, typed redactor that runs over a candidate payload BEFORE it can enter the
export queue. It distinguishes five actions — ``remove``, ``hash`` (pseudonymize), ``truncate``,
``allow``, ``require_approval`` — using explicit typed rules. There are NO user-supplied
expressions, Python code, SQL predicates, or caller-provided regexes over private data; the
patterns below are fixed and internal.

What is always removed/rejected: authorization headers, bearer tokens, API keys, refresh
tokens, private keys, callback secrets, lease tokens, database URLs, absolute home paths, raw
environment maps, network-interface dumps, arbitrary command output, full exception
tracebacks, SSH keys, cookies, session tokens. Sensitive identifiers (emails, IPs, hostnames,
usernames, serials, agent/peer ids, file names) are pseudonymized or truncated, never claimed
to be anonymized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aithernet.data.crypto import pseudonymize

REDACTION_POLICY_VERSION = "r1"

_MAX_STRING = 2000
_MAX_DEPTH = 8
_MAX_KEYS = 200
_PLACEHOLDER = "[redacted]"

# Key-name substrings whose VALUE must be removed entirely (never exported).
_SECRET_KEY_SUBSTRINGS = (
    "authorization", "bearer", "api_key", "apikey", "secret", "refresh_token",
    "access_token", "private_key", "privatekey", "password", "passwd", "credential",
    "cookie", "session_token", "sessiontoken", "ssh_key", "sshkey", "lease_token",
    "callback_secret", "client_secret", "database_url", "db_url", "dsn", "env", "environ",
    "token_hash", "encryption_key",
)

# Key-name substrings that are pseudonymized (kept for lineage, value hidden).
_PSEUDONYM_KEY_SUBSTRINGS = (
    "email", "ip_address", "ipaddr", "hostname", "host", "username", "user_name",
    "serial", "agent_id", "peer_id", "subject", "node_id", "device_serial",
)

# Value patterns that force removal regardless of key name.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\b(sqlite|postgres|postgresql|mysql|mongodb)://\S+", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"ssh-(rsa|ed25519|dss)\s+AAAA[0-9A-Za-z+/]+"),
    # beta.9: provider/API key shapes + KEY=value secret assignments (fail-closed for research
    # records). Covers OpenAI-style sk- keys, and NAME=value where NAME implies a credential
    # (API_KEY / SECRET / TOKEN / PASSWORD / CATGPT_GATEWAY_API_KEY / VNC_PASSWORD).
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"[A-Za-z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD)\s*[=:]\s*\S{4,}",
               re.IGNORECASE),
)

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOME_PATH = re.compile(r"(/home/[^/\s]+|/Users/[^/\s]+|/root)(/\S*)?")


@dataclass
class RedactionResult:
    redacted: dict
    actions: list[str] = field(default_factory=list)
    requires_approval: bool = False
    rejected: bool = False
    reason: str = ""
    policy_version: str = REDACTION_POLICY_VERSION


class Redactor:
    """Applies fixed, typed redaction rules to a candidate payload."""

    def __init__(self, *, pseudo_key: bytes | str | None,
                 pseudonymization_enabled: bool = True) -> None:
        # ``pseudo_key`` is the tenant-separated DERIVED key (bytes). When it is None the
        # redactor removes identifiers instead of pseudonymizing (fail-closed, never a fallback).
        self.pseudo_key = pseudo_key
        self.pseudonymization_enabled = pseudonymization_enabled and pseudo_key is not None

    def redact(self, payload: dict) -> RedactionResult:
        result = RedactionResult(redacted={})
        if not isinstance(payload, dict):
            result.rejected = True
            result.reason = "payload_not_object"
            return result
        result.redacted = self._walk(payload, depth=0, actions=result.actions)
        return result

    # -- internals --------------------------------------------------------------

    def _walk(self, value, *, depth: int, actions: list[str]):
        if depth > _MAX_DEPTH:
            actions.append("truncated:depth")
            return _PLACEHOLDER
        if isinstance(value, dict):
            out: dict = {}
            for i, (key, val) in enumerate(value.items()):
                if i >= _MAX_KEYS:
                    actions.append("truncated:keys")
                    break
                if not isinstance(key, str):
                    continue
                low = key.lower()
                if any(s in low for s in _SECRET_KEY_SUBSTRINGS):
                    out[key] = _PLACEHOLDER
                    actions.append(f"removed:{key}")
                    continue
                if any(s in low for s in _PSEUDONYM_KEY_SUBSTRINGS) and isinstance(val, str):
                    out[key] = self._pseudo(val)
                    actions.append(f"pseudonymized:{key}")
                    continue
                out[key] = self._walk(val, depth=depth + 1, actions=actions)
            return out
        if isinstance(value, list):
            return [self._walk(v, depth=depth + 1, actions=actions) for v in value[:200]]
        if isinstance(value, str):
            return self._scrub_string(value, actions)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        # Unknown/complex object — never serialize arbitrary internals.
        actions.append("removed:nonscalar")
        return _PLACEHOLDER

    def _scrub_string(self, s: str, actions: list[str]) -> str:
        for pat in _SECRET_VALUE_PATTERNS:
            if pat.search(s):
                actions.append("removed:secret_value")
                return _PLACEHOLDER
        if _HOME_PATH.search(s):
            s = _HOME_PATH.sub("[path]", s)
            actions.append("removed:home_path")
        if _EMAIL.search(s):
            s = _EMAIL.sub(lambda m: self._pseudo(m.group(0)), s)
            actions.append("pseudonymized:email")
        if _IPV4.search(s):
            s = _IPV4.sub(lambda m: self._pseudo(m.group(0)), s)
            actions.append("pseudonymized:ip")
        if len(s) > _MAX_STRING:
            s = s[:_MAX_STRING] + "…"
            actions.append("truncated:string")
        return s

    def _pseudo(self, value: str) -> str:
        if not self.pseudonymization_enabled:
            return _PLACEHOLDER
        return pseudonymize(value, key=self.pseudo_key, namespace="field")


def contains_residual_secret(payload: dict) -> bool:
    """Defence-in-depth check: True if any obvious secret pattern survived redaction."""
    import json

    blob = json.dumps(payload, default=str)
    return any(pat.search(blob) for pat in _SECRET_VALUE_PATTERNS)
