"""Claude Code coding-agent provider.

Runs Claude Code through its CLI in non-interactive ("print") mode via
``asyncio.create_subprocess_exec``, from the configured workspace directory. This is the
first real coding-agent provider for Stage 3.
"""

from __future__ import annotations

import asyncio
import os
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

#: Backwards-compatible alias; prompt construction is now provider-neutral and shared.
build_prompt = build_task_prompt


class ClaudeCodeProvider(CodingAgentProvider):
    """Execute coding tasks by invoking the Claude Code CLI in headless print mode."""

    name: ClassVar[str] = "claude_code"
    # Identity probe: `claude --version` reports e.g. "1.0.88 (Claude Code)".
    identity_args: ClassVar[tuple[str, ...]] = ("--version",)
    identity_tokens: ClassVar[tuple[str, ...]] = ("claude",)

    def _argv(self, resolved_executable: str, prompt: str) -> list[str]:
        argv = [resolved_executable, "--print", prompt]
        argv += ["--output-format", self.config.output_format]
        if self.config.model:
            argv += ["--model", self.config.model]
        argv += list(self.config.extra_args)
        return argv

    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        reasons = self.check_ready()
        if reasons:
            raise CodingAgentConfigurationError(
                f"Claude Code provider is not ready (executable='{self.config.executable}'): "
                + ", ".join(reasons)
            )
        resolved = self._resolve_executable()
        if resolved is None:  # pragma: no cover - guarded by check_ready above
            raise CodingAgentConfigurationError(
                f"Claude Code executable '{self.config.executable}' was not found on PATH."
            )

        workspace = Path(task.workspace)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CodingAgentConfigurationError(
                f"Workspace '{task.workspace}' could not be created: {exc}"
            ) from exc

        prompt = build_prompt(task)
        argv = self._argv(resolved, prompt)
        env = {**os.environ, **self.config.env}

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise CodingAgentProviderError(
                f"Failed to launch Claude Code: {exc}"
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.config.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CodingAgentProviderError(
                f"Claude Code timed out after {self.config.timeout_seconds}s."
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = process.returncode
        status = "completed" if exit_code == 0 else "failed"
        summary = self._summarize(status, exit_code, stdout, stderr)

        return CodingTaskExecutionResult(
            status=status,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            payload={
                "executable": self.config.executable,
                "output_format": self.config.output_format,
                "model": self.config.model,
            },
        )

    @staticmethod
    def _summarize(status: str, exit_code: int | None, stdout: str, stderr: str) -> str:
        """Derive a concise summary from captured output (never fabricated)."""
        if stdout.strip():
            return stdout.strip()[-_SUMMARY_TAIL:]
        if status == "failed":
            tail = stderr.strip()[-_SUMMARY_TAIL:]
            return f"Claude Code exited with code {exit_code}. {tail}".strip()
        return f"Claude Code completed with exit code {exit_code} and no stdout."
