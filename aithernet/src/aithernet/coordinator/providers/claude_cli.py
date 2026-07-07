"""Claude subscription coordinator via the official ``claude`` executable (beta.4).

Reasons via Claude run in bounded **non-interactive** mode (``--print``), using the operator's
Claude.ai Pro/Max login that Claude Code already manages. The coordinator is reasoning-only and
strictly contained:

* the prompt is supplied on **stdin** (never argv, so no secret-bearing arguments);
* it runs in a dedicated, EMPTY, Aithernet-owned runtime directory (never the repo root, coding
  workspace, gr-mcp checkout, or HOME) — a separate session per mission, no cross-mission state;
* ``--output-format json`` gives a structured envelope we parse for the assistant text + usage;
* the reasoning-only instruction forbids any tool/Bash/Edit/Write/MCP use; Aithernet executes the
  route Claude chooses. ``--dangerously-skip-permissions`` is NEVER passed;
* if the JSON envelope reports the model used any tool, the decision is REJECTED, never trusted.

Authentication belongs entirely to the runtime user's Claude login. This adapter never reads,
copies, parses, exports, or persists Claude OAuth credentials — only the explicitly exposed usage
(input/output tokens, num_turns, session id) and a bounded sanitized preview are captured.

Only well-documented flags are used (``--print``, ``--output-format``). Coordinator and Claude Code
coding sessions stay separate even on the same account: each runs its own ``claude`` process in its
own directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from aithernet.coordinator.contracts import (
    CoordinatorConfigurationError,
    CoordinatorDecision,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.coordinator.prompts import build_system_prompt, build_user_prompt
from aithernet.coordinator.providers.base import (
    CoordinatorProvider,
    extract_json_object,
    redact_secrets,
)

REASON_EXECUTABLE = "executable"
REASON_NOT_RUNNABLE = "executable_not_runnable"
REASON_PROVIDER_MISMATCH = "executable_provider_mismatch"

_VERSION_PROBE_TIMEOUT = 8.0
_ERROR_TEXT_LIMIT = 600
_OUTPUT_LIMIT = 8 * 1024 * 1024
_FOREIGN_MARKERS = ("codex", "gemini", "qwen", "ollama")

_NO_TOOLS_INSTRUCTION = (
    "IMPORTANT: You are reasoning only. Do NOT call, use, or attempt any tools, shell/Bash "
    "commands, file reads/writes (Edit/Write), web search/fetch, MCP servers, or sub-agents. Do "
    "not act on the node yourself — Aithernet executes the route you choose. Return ONLY the "
    "single JSON decision object described above, with no prose and no code fences."
)


def _resolve_executable(executable: str | None) -> str | None:
    if not executable:
        return None
    candidate = Path(executable)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(executable)


class ClaudeCLIProvider(CoordinatorProvider):
    """Reason over a mission by invoking the Claude CLI non-interactively (JSON, no tools)."""

    name: ClassVar[str] = "claude_cli"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._readiness_cache: list[str] | None = None
        self._cli_version: str | None = None

    # -- runtime directory -------------------------------------------------------

    def runtime_dir(self) -> Path:
        override = getattr(self.config, "working_directory", None)
        if override:
            path = Path(override).expanduser()
        else:
            state = os.environ.get("AITHERNET_STATE_ROOT")
            base = Path(state) if state else Path.home() / ".local/state/aithernet"
            path = base / "coordinator-runtime" / "claude_cli"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- readiness ---------------------------------------------------------------

    def check_ready(self) -> list[str]:
        if self._readiness_cache is not None:
            return self._readiness_cache
        missing: list[str] = []
        resolved = _resolve_executable(getattr(self.config, "executable", "claude") or "claude")
        if resolved is None:
            missing.append(REASON_EXECUTABLE)
        self._readiness_cache = missing
        return missing

    def _argv(self, resolved: str) -> list[str]:
        # Only well-documented flags: non-interactive print + structured JSON envelope.
        argv = [resolved, "--print", "--output-format", "json"]
        for extra in getattr(self.config, "extra_args", []) or []:
            argv.append(str(extra))
        return argv

    # -- decide ------------------------------------------------------------------

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        resolved = _resolve_executable(getattr(self.config, "executable", "claude") or "claude")
        if resolved is None:
            raise CoordinatorConfigurationError(
                "Claude CLI executable not found; sign in with Claude Code and set the executable.")
        system = build_system_prompt()
        user = build_user_prompt(payload)
        prompt = f"{system}\n\n{user}\n\n{_NO_TOOLS_INSTRUCTION}"
        argv = self._argv(resolved)
        cwd = str(self.runtime_dir())
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # force subscription path, not API billing
        timeout = float(getattr(self.config, "timeout_seconds", 180.0) or 180.0)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            raise CoordinatorProviderError(f"Claude CLI failed to start: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise CoordinatorProviderError(
                f"Claude CLI timed out after {timeout:.0f}s.") from exc
        if proc.returncode != 0:
            raise CoordinatorProviderError(
                "Claude CLI returned a non-zero exit: "
                f"{redact_secrets(err.decode('utf-8', 'replace'))[:_ERROR_TEXT_LIMIT]}")
        envelope = self._parse_envelope(out)
        text = self._assistant_text(envelope)
        decision_obj = extract_json_object(text)
        decision = CoordinatorDecision.from_model_json(decision_obj, mission_id=payload.mission_id)
        return decision

    def _parse_envelope(self, raw: bytes) -> dict:
        text = raw[:_OUTPUT_LIMIT].decode("utf-8", "replace").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CoordinatorProviderError(
                f"Claude CLI did not return a JSON envelope: {redact_secrets(text)[:300]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise CoordinatorProviderError("Claude CLI envelope was not a JSON object.")
        # Authoritative tool-use rejection: a reasoning-only coordinator must not have used tools.
        if int(data.get("num_tool_uses", 0) or 0) > 0:
            raise CoordinatorProviderError(
                "Claude CLI coordinator used a tool; reasoning-only decision rejected.")
        if data.get("is_error"):
            raise CoordinatorProviderError("Claude CLI reported an error result.")
        return data

    def _assistant_text(self, envelope: dict) -> str:
        for key in ("result", "text", "content", "message"):
            value = envelope.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, list):  # content-block array
                parts = [b.get("text", "") for b in value if isinstance(b, dict)]
                joined = "".join(parts).strip()
                if joined:
                    return joined
        raise CoordinatorProviderError("Claude CLI envelope had no assistant text.")

    async def healthcheck(self) -> dict:
        """A bounded real probe: ask for a trivial JSON decision and confirm it parses."""
        resolved = _resolve_executable(getattr(self.config, "executable", "claude") or "claude")
        if resolved is None:
            raise CoordinatorProviderError("Claude CLI executable not found.")
        version = self.identity_version()
        return {"provider": self.name, "executable": os.path.basename(resolved),
                "version": version, "auth": "claude_subscription"}

    def identity_version(self) -> str | None:
        if self._cli_version is not None:
            return self._cli_version
        resolved = _resolve_executable(getattr(self.config, "executable", "claude") or "claude")
        if resolved is None:
            return None
        try:
            out = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                                 timeout=_VERSION_PROBE_TIMEOUT)
            self._cli_version = (out.stdout or out.stderr).strip().splitlines()[0][:80]
        except (OSError, subprocess.SubprocessError, IndexError):
            self._cli_version = None
        return self._cli_version
