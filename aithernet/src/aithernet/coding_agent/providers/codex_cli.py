"""Codex CLI coding-agent provider.

Runs the OpenAI Codex CLI non-interactively via ``codex exec`` (verified against the
installed ``codex exec --help``, codex-cli 0.137.0). Codex is a *real agent*: with
``--sandbox workspace-write`` it inspects and edits files inside the configured workspace
and runs its own tools — this provider does not merely ask Codex for code text.

Invocation rationale (all flags exist in the installed ``codex exec``):

* ``exec``                      — non-interactive subcommand.
* prompt on **stdin** (``-``)   — avoids putting a large task prompt on the argv.
* ``--sandbox workspace-write`` — Codex may read/write within the workspace + cwd, but not
  outside it (so the provider never modifies files outside the configured workspace).
* ``-C <workspace>``            — Codex's working root (also the subprocess ``cwd``).
* ``--skip-git-repo-check``     — the workspace need not be a git repository.
* ``-c approval_policy="never"``— never block waiting for human approval (headless).
* ``--json``                    — emit JSONL events to stdout (retained for audit).
* ``-o <file>``                 — write the final agent message to a file (the summary).
* ``-m <model>``                — only when a model is configured.

A non-zero exit is a persisted ``failed`` result (never reported as success). Timeouts
kill the process cleanly and raise ``CodingAgentProviderError`` so no orphan Codex process
is left behind. ``files_changed``/``commands_run`` are left empty unless Codex output
provides trustworthy structured evidence — they are never invented.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import ClassVar

from aithernet.coding_agent.contracts import (
    CodingAgentConfigurationError,
    CodingAgentProviderError,
    CodingTaskExecutionInput,
    CodingTaskExecutionResult,
)
from aithernet.coding_agent.providers.base import CodingAgentProvider
from aithernet.coding_agent.providers.prompt import build_task_prompt

#: How much captured output to retain in the derived summary.
_SUMMARY_TAIL = 2000


class CodexCliProvider(CodingAgentProvider):
    """Execute coding tasks by invoking the Codex CLI (``codex exec``) non-interactively."""

    name: ClassVar[str] = "codex_cli"
    # Identity probe: `codex --version` reports e.g. "codex-cli 0.137.0".
    identity_args: ClassVar[tuple[str, ...]] = ("--version",)
    identity_tokens: ClassVar[tuple[str, ...]] = ("codex",)

    def _argv(self, resolved_executable: str, workspace: str, last_message_path: str) -> list[str]:
        argv = [
            resolved_executable,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-C",
            workspace,
            "-c",
            'approval_policy="never"',
            "-o",
            last_message_path,
        ]
        if self.config.model:
            argv += ["-m", self.config.model]
        argv += list(self.config.extra_args)
        argv += ["-"]  # read the prompt from stdin
        return argv

    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        reasons = self.check_ready()
        if reasons:
            raise CodingAgentConfigurationError(
                f"Codex CLI provider is not ready (executable='{self.config.executable}'): "
                + ", ".join(reasons)
            )
        resolved = self._resolve_executable()
        assert resolved is not None  # guarded by check_ready

        workspace = Path(task.workspace)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CodingAgentConfigurationError(
                f"Workspace '{task.workspace}' could not be created: {exc}"
            ) from exc

        prompt = build_task_prompt(task)
        env = {**os.environ, **self.config.env}

        fd, last_message_path = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
        os.close(fd)
        try:
            argv = self._argv(resolved, str(workspace), last_message_path)
            stdout, stderr, exit_code = await self._run(argv, prompt, env)
            last_message = self._read_last_message(last_message_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(last_message_path)

        status = "completed" if exit_code == 0 else "failed"
        summary = self._summarize(status, exit_code, last_message, stdout, stderr)

        return CodingTaskExecutionResult(
            status=status,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            # Not fabricated: only populate from trustworthy structured evidence (none here).
            files_changed=[],
            commands_run=[],
            payload={
                "provider": self.name,
                "executable": self.config.executable,
                "codex_version": self.identity_version(),
                "model": self.config.model,
                "output_mode": "jsonl",
                "sandbox": "workspace-write",
            },
        )

    async def _run(
        self, argv: list[str], prompt: str, env: dict[str, str]
    ) -> tuple[str, str, int | None]:
        """Spawn Codex, feed the prompt on stdin, and capture output with a hard timeout."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=argv[argv.index("-C") + 1],
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise CodingAgentProviderError(f"Failed to launch Codex CLI: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError as exc:
            # Terminate cleanly and await exit so no orphan Codex process is left behind.
            with contextlib.suppress(OSError):
                process.kill()
            await process.wait()
            raise CodingAgentProviderError(
                f"Codex CLI timed out after {self.config.timeout_seconds}s."
            ) from exc

        return (
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
            process.returncode,
        )

    @staticmethod
    def _read_last_message(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    @classmethod
    def _summarize(
        cls, status: str, exit_code: int | None, last_message: str, stdout: str, stderr: str
    ) -> str:
        """Derive a summary, preferring Codex's final message (never fabricated)."""
        if last_message:
            return last_message[-_SUMMARY_TAIL:]
        jsonl_message = cls._last_agent_message(stdout)
        if jsonl_message:
            return jsonl_message[-_SUMMARY_TAIL:]
        if status == "failed":
            tail = (stderr.strip() or stdout.strip())[-_SUMMARY_TAIL:]
            return f"Codex CLI exited with code {exit_code}. {tail}".strip()
        if stdout.strip():
            return stdout.strip()[-_SUMMARY_TAIL:]
        return f"Codex CLI completed with exit code {exit_code} and no final message."

    @staticmethod
    def _last_agent_message(stdout: str) -> str | None:
        """Best-effort: the last agent-message text from the JSONL event stream.

        Defensive — any line that is not a recognizable agent message is ignored, and the
        raw stdout is always retained regardless. Returns ``None`` if nothing matched.
        """
        last: str | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            text = _agent_message_text(event)
            if text:
                last = text
        return last


def _agent_message_text(event: object) -> str | None:
    """Extract agent-message text from a Codex JSONL event, tolerating shape variation."""
    if not isinstance(event, dict):
        return None
    # Unwrap a couple of common envelopes seen across Codex versions.
    for key in ("item", "msg", "message"):
        inner = event.get(key)
        if isinstance(inner, dict):
            text = _agent_message_text(inner)
            if text:
                return text
    type_name = str(event.get("type", "")).lower()
    if "agent_message" in type_name or type_name in {"assistant", "message"}:
        for field in ("text", "message", "content"):
            value = event.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None

