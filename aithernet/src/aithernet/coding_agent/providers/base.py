"""Abstract coding-agent provider interface, shared executable resolution + identity probe.

Readiness is more than "the executable resolves": a provider also runs a lightweight,
short-timeout *identity probe* (e.g. ``--version``) and verifies the binary actually
behaves like the expected agent. This prevents a misconfiguration such as
``provider=claude_code`` with ``executable=codex`` from reporting ``configured: yes`` —
that binary exists but does not speak Claude Code's flags. The probe result is cached per
provider instance so dashboard polling never respawns it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from aithernet.coding_agent.contracts import (
    CodingTaskExecutionInput,
    CodingTaskExecutionResult,
)
from aithernet.config.settings import CodingAgentConfig

#: Short timeout (seconds) for the identity probe so status polling never blocks.
_IDENTITY_PROBE_TIMEOUT = 5.0

#: Readiness reason codes returned by :meth:`CodingAgentProvider.check_ready`.
REASON_EXECUTABLE = "executable"  # missing / not found on PATH
REASON_NOT_RUNNABLE = "executable_not_runnable"  # resolved but could not be spawned
REASON_PROVIDER_MISMATCH = "executable_provider_mismatch"  # ran, but is the wrong agent
REASON_PROBE_FAILED = "executable_probe_failed"  # probe timed out / unusable


def resolve_executable(executable: str | None) -> str | None:
    """Return a runnable path for ``executable`` (absolute/relative file or PATH lookup)."""
    if not executable:
        return None
    candidate = Path(executable)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(executable)


class CodingAgentProvider(ABC):
    """Contract every coding-agent provider implements.

    A provider is constructed with a :class:`CodingAgentConfig` and exposes an async
    :meth:`execute`. It knows nothing about FastAPI or the database. Subclasses set
    :attr:`identity_args`/:attr:`identity_tokens` to describe their identity probe.
    """

    #: Stable registry name for this provider.
    name: ClassVar[str]

    #: argv (after the executable) for the safe identity probe.
    identity_args: ClassVar[tuple[str, ...]] = ("--version",)

    #: Case-insensitive tokens expected in probe output to confirm the binary's identity.
    #: Empty means "any resolvable executable is acceptable" (no identity requirement).
    identity_tokens: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: CodingAgentConfig) -> None:
        self.config = config
        self._readiness_cache: list[str] | None = None
        self._identity_output: str | None = None

    @abstractmethod
    async def execute(self, task: CodingTaskExecutionInput) -> CodingTaskExecutionResult:
        """Execute ``task`` in the configured workspace and return its result.

        Implementations raise
        :class:`~aithernet.coding_agent.contracts.CodingAgentConfigurationError` if
        required configuration is missing or the executable is the wrong agent, or
        :class:`~aithernet.coding_agent.contracts.CodingAgentProviderError` if the agent
        cannot run (timeout, spawn failure). A process that runs but exits non-zero is
        returned as a result with ``status="failed"``.
        """

    def _resolve_executable(self) -> str | None:
        return resolve_executable(self.config.executable)

    def check_ready(self) -> list[str]:
        """Return missing/blocking readiness reasons (empty when ready); cached per instance."""
        if self._readiness_cache is None:
            self._readiness_cache = self._probe_readiness()
        return list(self._readiness_cache)

    def reset_readiness_cache(self) -> None:
        """beta.5: drop the cached executable/identity readiness so the next check re-probes.

        The readiness probe is memoised for the process lifetime; on ``aithernet mission retry`` we
        clear it so a locally-repaired environment (a newly-installed executable, a fixed coding
        sandbox) is re-detected instead of a stale "unavailable" result persisting for the run —
        the exact beta.4 complaint that retry kept reporting the coding agent unavailable after the
        host was fixed."""
        self._readiness_cache = None
        self._identity_output = None

    def _probe_readiness(self) -> list[str]:
        resolved = self._resolve_executable()
        if not self.config.executable or resolved is None:
            return [REASON_EXECUTABLE]
        if not self.identity_tokens:
            return []  # provider imposes no identity requirement
        try:
            proc = subprocess.run(
                [resolved, *self.identity_args],
                capture_output=True,
                text=True,
                timeout=_IDENTITY_PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return [REASON_PROBE_FAILED]
        except OSError:
            return [REASON_NOT_RUNNABLE]
        output = f"{proc.stdout}\n{proc.stderr}".strip()
        self._identity_output = output[:200]
        lowered = output.lower()
        if any(token.lower() in lowered for token in self.identity_tokens):
            return []
        return [REASON_PROVIDER_MISMATCH]

    def identity_version(self) -> str | None:
        """First line of the cached identity-probe output (e.g. ``codex-cli 0.137.0``)."""
        self.check_ready()  # ensure the probe has run (cached)
        if not self._identity_output:
            return None
        first = self._identity_output.splitlines()[0].strip()
        return first or None
