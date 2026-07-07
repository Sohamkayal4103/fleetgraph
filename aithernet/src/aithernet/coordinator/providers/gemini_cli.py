"""Gemini CLI coordinator provider.

Reasons via the authenticated Google Gemini CLI run **headless** (verified against the
installed CLI, gemini 0.46.0). Gemini reasons; Aithernet executes. The coordinator is
reasoning-only:

* it runs with ``--output-format json`` and the prompt on **stdin** (never argv);
* it runs in a dedicated, empty, sandboxed runtime directory (never the repo root, coding
  workspace, gr-mcp checkout, or HOME), with a workspace ``.gemini/settings.json`` that
  denies built-in tools and MCP servers;
* it is never run in YOLO/auto-approval mode; headless mode defaults to *deny* for any
  un-allowed tool;
* **any reported tool call** (``stats.tools.totalCalls > 0``) is treated as a provider
  failure — the decision from a run where Gemini used a tool is rejected, never trusted.

Authentication uses the user's existing Gemini CLI login via the inherited HOME/XDG
environment; those values are never logged, persisted, or returned. The full prompt is
never persisted; only a bounded, sanitized response preview is used for diagnostics.

Residual limitation (documented): this CLI version's ``--policy`` deny-all rule makes the
Gemini API reject the request (empty ``tools[0].tool_type`` -> HTTP 400), so an explicit
deny-all policy file is **not** used. Defense is instead: empty sandbox dir + workspace
settings disabling tools + headless deny-by-default + authoritative tool-call detection.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
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


def _resolve_executable(executable: str | None) -> str | None:
    """Return a runnable path for ``executable`` (absolute/relative file or PATH lookup)."""
    if not executable:
        return None
    candidate = Path(executable)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(executable)

#: Readiness reason codes (returned by check_ready()).
REASON_EXECUTABLE = "executable"
REASON_NOT_FOUND = "executable_not_found"
REASON_NOT_RUNNABLE = "executable_not_runnable"
REASON_PROVIDER_MISMATCH = "executable_provider_mismatch"

#: Short timeout (seconds) for the identity/version probe so status polling never blocks.
_VERSION_PROBE_TIMEOUT = 8.0

#: Max chars of CLI stderr / response to surface in an error (bounded, sanitized).
_ERROR_TEXT_LIMIT = 600
#: Max chars of model content to preview in a parse-failure error.
_CONTENT_PREVIEW_LIMIT = 400
#: Max bytes captured from the child's stdout/stderr (defensive bound).
_OUTPUT_LIMIT = 4 * 1024 * 1024

#: Markers that indicate the executable is a *different* known agent, not Gemini.
_FOREIGN_MARKERS = ("codex", "claude", "anthropic", "openai", "qwen", "ollama")

#: Reasoning-only instruction appended to every coordinator prompt.
_NO_TOOLS_INSTRUCTION = (
    "IMPORTANT: You are reasoning only. Do NOT call, use, or attempt any tools, shell "
    "commands, file reads/writes, web search/fetch, MCP servers, extensions, or sub-agents. "
    "Do not act on the node yourself — Aithernet executes the route you choose. Return ONLY "
    "the single JSON decision object described above, with no prose and no code fences."
)

#: Workspace settings written into the runtime dir. ``tools.core: []`` is the modern
#: allow-no-core-tools form (no deprecated keys, no empty-tools API error); ``mcpServers``
#: empty denies MCP servers; ``ide.enabled: false`` stops the CLI's IDE companion from
#: attaching to the editor's open workspace (which would break the isolated runtime dir).
#: Best-effort defense in depth — the authoritative guarantee is tool-call detection (any
#: reported tool call fails the decision).
_RUNTIME_SETTINGS = {
    "tools": {"core": []},
    "mcpServers": {},
    "ide": {"enabled": False},
}

#: Prefix of every environment variable the Gemini CLI's IDE companion uses to discover and
#: attach to the editor's open workspace (e.g. ``GEMINI_CLI_IDE_WORKSPACE_PATH``,
#: ``GEMINI_CLI_IDE_SERVER_PORT``). These are stripped from every coordinator subprocess so
#: Gemini runs purely in its isolated runtime directory, never the IDE's repository workspace.
_IDE_ENV_PREFIX = "GEMINI_CLI_IDE_"


class GeminiCLIProvider(CoordinatorProvider):
    """Reason over a mission by invoking the Gemini CLI headlessly (JSON, no tools)."""

    name: ClassVar[str] = "gemini_cli"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._readiness_cache: list[str] | None = None
        self._cli_version: str | None = None

    # -- runtime directory -------------------------------------------------------

    def runtime_dir(self) -> Path:
        """The dedicated, empty, sandboxed directory Gemini runs in (cwd).

        Deliberately **outside the repository** (under the XDG state area) so Gemini never
        discovers the Aithernet repo, coding workspace, or gr-mcp checkout as a project (a
        project context makes this CLI version send an invalid empty tools declaration that
        the Gemini API rejects with HTTP 400). It is a dedicated empty subdir — never $HOME
        itself — and holds only the tool-denying settings, no mission or source files.
        """
        if self.config.working_directory:
            return Path(self.config.working_directory)
        state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return Path(state_home) / "aithernet" / "coordinator" / "gemini-cli"

    def _ensure_runtime_dir(self) -> Path:
        """Create the runtime dir + its tool-denying settings (idempotent)."""
        runtime = self.runtime_dir()
        try:
            (runtime / ".gemini").mkdir(parents=True, exist_ok=True)
            (runtime / ".gemini" / "settings.json").write_text(
                json.dumps(_RUNTIME_SETTINGS), encoding="utf-8"
            )
        except OSError as exc:
            raise CoordinatorConfigurationError(
                f"Gemini coordinator runtime directory '{runtime}' could not be prepared: {exc}"
            ) from exc
        return runtime

    # -- subprocess environment --------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        """The single coordinator-subprocess environment builder (shared by all invocations).

        Starts from the inherited environment so the user's Google-account authentication
        (HOME/XDG/PATH/Node variables) keeps working, then removes every ``GEMINI_CLI_IDE_*``
        variable so the CLI's IDE companion never attaches to the editor's open workspace.
        Used identically by the readiness probe, the diagnostics probe, and every real
        coordinator decision. The environment is used only to spawn the child — it is never
        logged, persisted, returned, or surfaced in errors/events/status.
        """
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_IDE_ENV_PREFIX)
        }

    # -- readiness ---------------------------------------------------------------

    def _resolve_executable(self) -> str | None:
        return _resolve_executable(self.config.executable)

    def check_ready(self) -> list[str]:
        """Missing/blocking readiness reasons (empty when ready); cached per instance.

        Runs only a lightweight ``--version`` identity probe — never a paid inference — so
        dashboard status polling is cheap and never spawns repeated processes.
        """
        if self._readiness_cache is None:
            self._readiness_cache = self._probe_readiness()
        return list(self._readiness_cache)

    def _probe_readiness(self) -> list[str]:
        if not self.config.executable:
            return [REASON_EXECUTABLE]
        resolved = self._resolve_executable()
        if resolved is None:
            return [REASON_NOT_FOUND]
        try:
            proc = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_PROBE_TIMEOUT,
                env=self._child_env(),
            )
        except subprocess.TimeoutExpired:
            return [REASON_NOT_RUNNABLE]
        except OSError:
            return [REASON_NOT_RUNNABLE]
        output = f"{proc.stdout}\n{proc.stderr}".strip()
        lowered = output.lower()
        # A non-zero exit (e.g. gemini on Node < 20 crashes with "File is not defined")
        # or a version line that names a different agent is not a usable Gemini CLI.
        if proc.returncode != 0:
            return [REASON_NOT_RUNNABLE]
        if any(marker in lowered for marker in _FOREIGN_MARKERS):
            return [REASON_PROVIDER_MISMATCH]
        version = self._parse_version(proc.stdout)
        if version is None:
            return [REASON_PROVIDER_MISMATCH]
        self._cli_version = version
        return []

    @staticmethod
    def _parse_version(text: str) -> str | None:
        """Return the first semver-like token (e.g. '0.46.0') from version output."""
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b", text)
        return match.group(0) if match else None

    def cli_version(self) -> str | None:
        """Cached Gemini CLI version (from the readiness probe), or None if not probed."""
        self.check_ready()
        return self._cli_version

    # -- invocation --------------------------------------------------------------

    def _argv(self, resolved: str) -> list[str]:
        argv = [resolved, "--output-format", "json", "--skip-trust"]
        if self.config.model:  # blank/None -> let the authenticated CLI pick its default
            argv += ["--model", self.config.model]
        argv += list(self.config.extra_args)
        return argv

    def _build_prompt(self, payload: CoordinatorInput) -> str:
        return (
            f"{build_system_prompt()}\n\n"
            f"{build_user_prompt(payload)}\n\n"
            f"{_NO_TOOLS_INSTRUCTION}"
        )

    async def decide(self, payload: CoordinatorInput) -> CoordinatorDecision:
        reasons = self.check_ready()
        if reasons:
            raise CoordinatorConfigurationError(
                f"Gemini CLI coordinator is not ready (executable='{self.config.executable}'): "
                + ", ".join(reasons)
            )
        resolved = self._resolve_executable()
        assert resolved is not None  # guarded by check_ready

        prompt = self._build_prompt(payload)
        stdout, stderr, exit_code = await self._run(resolved, prompt)
        parsed = self._parse_cli_json(stdout, stderr, exit_code)
        self._reject_tool_use(parsed)
        content = self._extract_response(parsed)
        return self._parse_decision(content, payload)

    async def healthcheck(self) -> dict:
        """One minimal authenticated headless inference for the ``--probe`` diagnostics."""
        reasons = self.check_ready()
        if reasons:
            raise CoordinatorConfigurationError(
                "Gemini CLI coordinator is not ready: " + ", ".join(reasons)
            )
        resolved = self._resolve_executable()
        assert resolved is not None
        prompt = (
            "Return EXACTLY this JSON object and nothing else (no prose, no tools): "
            '{"ok": true}'
        )
        started = time.monotonic()
        stdout, stderr, exit_code = await self._run(resolved, prompt)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        parsed = self._parse_cli_json(stdout, stderr, exit_code)
        self._reject_tool_use(parsed)
        response = self._extract_response(parsed)
        # Validate a response actually came back (do not fake success).
        if "ok" not in response.lower():
            raise CoordinatorProviderError(
                "Gemini CLI headless probe returned an unexpected response "
                f"(preview: {redact_secrets(response)[:120]!r})."
            )
        stats = parsed.get("stats") if isinstance(parsed, dict) else None
        return {
            "provider": self.name,
            "cli_version": self._cli_version,
            "model_reported": self._reported_models(stats),
            "tool_calls": self._tool_calls(stats),
            "elapsed_ms": elapsed_ms,
            "exit_code": exit_code,
            "response_received": True,
        }

    # -- subprocess lifecycle ----------------------------------------------------

    async def _run(self, resolved: str, prompt: str) -> tuple[str, str, int | None]:
        """Spawn Gemini headless in the sandbox, feed the prompt on stdin, capture output.

        Robust lifecycle: graceful terminate then kill on timeout, always awaiting the
        process so no orphan Gemini process is left behind. Output is bounded.
        """
        runtime = self._ensure_runtime_dir()
        argv = self._argv(resolved)
        # One shared environment builder: inherits the user's Gemini login (HOME/XDG/PATH/
        # Node) but strips GEMINI_CLI_IDE_* so the CLI never attaches to the editor's open
        # workspace. Used only to spawn the child — never logged or persisted.
        env = self._child_env()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(runtime),
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise CoordinatorProviderError(f"Failed to launch Gemini CLI: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError as exc:
            await self._terminate(process)
            raise CoordinatorProviderError(
                f"Gemini CLI timed out after {self.config.timeout_seconds}s."
            ) from exc

        stdout = stdout_bytes[:_OUTPUT_LIMIT].decode("utf-8", errors="replace")
        stderr = stderr_bytes[:_OUTPUT_LIMIT].decode("utf-8", errors="replace")
        return stdout, stderr, process.returncode

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """Terminate, then kill if needed, always awaiting exit (no orphan process)."""
        if process.returncode is not None:
            return
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
                return
            except TimeoutError:
                pass
            process.kill()
            await process.wait()
        except ProcessLookupError:
            return

    # -- output parsing ----------------------------------------------------------

    def _parse_cli_json(self, stdout: str, stderr: str, exit_code: int | None) -> dict:
        """Decode the outer CLI JSON, raising sanitized provider errors on failure."""
        text = stdout.strip()
        if not text:
            tail = redact_secrets(stderr.strip())[:_ERROR_TEXT_LIMIT] or "<no output>"
            # A common headless failure: the installed CLI's IDE companion still tried to
            # attach to the editor's open workspace (despite ide.enabled=false + stripped
            # GEMINI_CLI_IDE_* env), so it printed a directory-mismatch error and no JSON.
            if self._is_ide_integration_failure(stderr):
                raise CoordinatorProviderError(
                    "Gemini CLI produced no JSON output because IDE integration attempted "
                    "to attach to a different workspace. Coordinator subprocess IDE "
                    "integration is disabled, but the installed CLI still attempted a "
                    f"connection. (exit code {exit_code}, provider {self.name}). "
                    f"stderr: {tail}"
                )
            raise CoordinatorProviderError(
                f"Gemini CLI produced no JSON output (exit code {exit_code}, "
                f"provider {self.name}). stderr: {tail}"
            )
        try:
            data = json.loads(text)
        except ValueError as exc:
            preview = redact_secrets(text)[:_ERROR_TEXT_LIMIT]
            raise CoordinatorProviderError(
                f"Gemini CLI returned malformed JSON [{type(exc).__name__}] "
                f"(exit code {exit_code}, provider {self.name}): {preview!r}"
            ) from exc
        if not isinstance(data, dict):
            raise CoordinatorProviderError(
                f"Gemini CLI JSON was {type(data).__name__}, expected an object "
                f"(provider {self.name})."
            )
        error = data.get("error")
        if error:
            etype, emsg = self._describe_cli_error(error)
            raise CoordinatorProviderError(
                f"Gemini CLI reported an error [{etype}] (exit code {exit_code}, "
                f"provider {self.name}): {redact_secrets(emsg)[:_ERROR_TEXT_LIMIT]}"
            )
        return data

    @staticmethod
    def _is_ide_integration_failure(stderr: str) -> bool:
        """True when stderr indicates the CLI's IDE companion tried to attach to a workspace.

        Recognizes the directory-mismatch / IDE-client errors the Gemini CLI emits when its
        editor integration activates (e.g. ``[IDEClient] Directory mismatch`` / "running in a
        different location than the open workspace in the IDE").
        """
        lowered = stderr.lower()
        return (
            "ideclient" in lowered
            or "directory mismatch" in lowered
            or "different location than the open workspace" in lowered
        )

    @staticmethod
    def _describe_cli_error(error: object) -> tuple[str, str]:
        if isinstance(error, dict):
            etype = str(error.get("type") or error.get("code") or "error")
            emsg = str(error.get("message") or error)
            return etype, emsg
        return "error", str(error)

    def _reject_tool_use(self, parsed: dict) -> None:
        """Treat ANY reported tool call as a provider failure (reasoning-only contract)."""
        stats = parsed.get("stats")
        calls = self._tool_calls(stats)
        if calls and calls > 0:
            by_name = {}
            if isinstance(stats, dict):
                by_name = (stats.get("tools") or {}).get("byName") or {}
            tools = ", ".join(sorted(by_name)) if isinstance(by_name, dict) else ""
            raise CoordinatorProviderError(
                f"Gemini CLI attempted {calls} tool call(s) during a coordinator decision "
                f"(provider {self.name}); the coordinator is reasoning-only and the decision "
                f"is rejected. Tools: {tools or 'unknown'}."
            )

    @staticmethod
    def _tool_calls(stats: object) -> int:
        if not isinstance(stats, dict):
            return 0
        tools = stats.get("tools")
        if not isinstance(tools, dict):
            return 0
        try:
            return int(tools.get("totalCalls") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _reported_models(stats: object) -> list[str]:
        if not isinstance(stats, dict):
            return []
        models = stats.get("models")
        return sorted(models) if isinstance(models, dict) else []

    def _extract_response(self, parsed: dict) -> str:
        response = parsed.get("response")
        if not isinstance(response, str) or not response.strip():
            raise CoordinatorProviderError(
                f"Gemini CLI JSON had no usable 'response' field (provider {self.name}; "
                f"response type {type(response).__name__})."
            )
        return response

    def _parse_decision(self, content: str, payload: CoordinatorInput) -> CoordinatorDecision:
        """Parse a decision, reporting the failing stage and a sanitized content preview."""
        preview = redact_secrets(content)[:_CONTENT_PREVIEW_LIMIT]
        try:
            raw_decision = extract_json_object(content)
        except CoordinatorProviderError as exc:
            raise CoordinatorProviderError(
                f"Gemini CLI returned a response but it was not a JSON decision object "
                f"(extraction stage, provider {self.name}): {exc} | preview: {preview!r}"
            ) from exc
        try:
            return CoordinatorDecision.from_model_json(
                raw_decision, mission_id=payload.mission_id
            )
        except CoordinatorProviderError as exc:
            raise CoordinatorProviderError(
                f"Gemini CLI decision did not match the schema (validation stage, provider "
                f"{self.name}): {exc} | preview: {preview!r}"
            ) from exc
