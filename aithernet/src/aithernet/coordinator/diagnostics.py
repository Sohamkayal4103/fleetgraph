"""Coordinator integration diagnostics.

Reports whether the configured coordinator provider is genuinely ready and — only when
explicitly asked to ``probe`` — makes one minimal real inference to confirm it. Config-level
diagnostics never launch a paid/model inference; a probe runs the provider's
:meth:`~aithernet.coordinator.providers.base.CoordinatorProvider.healthcheck`.

The report is secret-free: it never exposes authentication files, OAuth/API tokens, the
environment, or the full prompt — only the resolved executable path (already a status
convention) and bounded, sanitized metadata.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from aithernet.coordinator.contracts import CoordinatorError
from aithernet.coordinator.runtime import CoordinatorRuntime

#: Intended/default Gemini CLI auth model (documented, never assumed for any subscription).
GEMINI_CLI_URL = "https://github.com/google-gemini/gemini-cli"


class CoordinatorProbeResult(BaseModel):
    """Outcome of one minimal authenticated inference (only when probe=True)."""

    attempted: bool = False
    succeeded: bool | None = None
    response_received: bool | None = None
    model_reported: list[str] | None = None
    tool_calls: int | None = None
    elapsed_ms: int | None = None
    exit_code: int | None = None
    error_type: str | None = None
    error: str | None = None


class CoordinatorDiagnostics(BaseModel):
    """A secret-free diagnostic report for the configured coordinator provider."""

    provider: str
    configured: bool
    missing_configuration: list[str] = Field(default_factory=list)
    model: str = "default"
    timeout_seconds: float
    # CLI providers (gemini_cli):
    executable: str | None = None
    executable_resolves: bool | None = None
    resolved_executable_path: str | None = None
    cli_version: str | None = None
    working_directory: str | None = None
    # HTTP providers (openai_compatible, anthropic):
    base_url_configured: bool = False
    api_key_configured: bool = False
    probe: CoordinatorProbeResult = Field(default_factory=CoordinatorProbeResult)
    hints: list[str] = Field(default_factory=list)


def _resolve(executable: str | None) -> str | None:
    if not executable:
        return None
    candidate = Path(executable)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(executable)


async def _run_probe(coordinator: CoordinatorRuntime) -> CoordinatorProbeResult:
    """Run one minimal real inference via the provider's healthcheck."""
    provider = coordinator.provider
    if provider is None:
        return CoordinatorProbeResult(
            attempted=True,
            succeeded=False,
            error_type="CoordinatorConfigurationError",
            error=f"No coordinator provider is configured for '{coordinator.config.provider}'.",
        )
    try:
        meta = await provider.healthcheck()
    except CoordinatorError as exc:
        return CoordinatorProbeResult(
            attempted=True,
            succeeded=False,
            error_type=type(exc).__name__,
            error=str(exc)[:600],
        )
    return CoordinatorProbeResult(
        attempted=True,
        succeeded=True,
        response_received=bool(meta.get("response_received", True)),
        model_reported=meta.get("model_reported"),
        tool_calls=meta.get("tool_calls"),
        elapsed_ms=meta.get("elapsed_ms"),
        exit_code=meta.get("exit_code"),
    )


def _build_hints(diag: CoordinatorDiagnostics, provider_name: str) -> list[str]:
    hints: list[str] = []
    if provider_name == "gemini_cli":
        if not diag.executable:
            hints.append("Set AITHERNET_COORDINATOR_EXECUTABLE (e.g. 'gemini').")
        elif diag.executable_resolves is False:
            hints.append(
                f"Gemini executable '{diag.executable}' was not found on PATH. Install the "
                "Gemini CLI (Node 20+) and ensure 'gemini' is on PATH, or set an absolute path."
            )
        elif "executable_not_runnable" in diag.missing_configuration:
            hints.append(
                "The Gemini executable resolved but did not run cleanly. The Gemini CLI "
                "requires Node.js 20+; start Aithernet from an environment where 'gemini' runs."
            )
        elif "executable_provider_mismatch" in diag.missing_configuration:
            hints.append(
                "The configured executable does not identify as the Gemini CLI — provider "
                "and executable must match (gemini_cli ↔ gemini)."
            )
        elif diag.configured and not diag.probe.attempted:
            hints.append(
                "Configuration looks ready. Run with probe=true "
                "(`aithernet coordinator diagnostics --probe`) to make one authenticated "
                "headless inference and confirm the Google login works."
            )
        if diag.probe.attempted and diag.probe.succeeded:
            models = ", ".join(diag.probe.model_reported or []) or "default"
            hints.append(f"Authenticated headless inference works. Model(s) reported: {models}.")
        if diag.probe.attempted and diag.probe.succeeded is False:
            hints.append(
                "The headless probe failed — verify the Gemini CLI login "
                "(`gemini` interactively, then re-probe). Auth details are never shown here."
            )
        hints.append(
            f"Gemini reasons; Aithernet executes. The coordinator denies built-in tools and "
            f"treats any tool call as a failure. CLI: {GEMINI_CLI_URL}"
        )
    else:
        if "model" in diag.missing_configuration or "base_url" in diag.missing_configuration:
            hints.append(
                "Set AITHERNET_COORDINATOR_MODEL and AITHERNET_COORDINATOR_BASE_URL for the "
                "openai_compatible provider (e.g. a local Ollama endpoint)."
            )
        if diag.configured and not diag.probe.attempted:
            hints.append(
                "Configuration looks ready. Run with probe=true to make one minimal model call."
            )
    return hints


async def build_coordinator_diagnostics(
    coordinator: CoordinatorRuntime, *, probe: bool = False
) -> CoordinatorDiagnostics:
    """Build a coordinator diagnostic report (optionally running one real inference)."""
    config = coordinator.config
    status = coordinator.status()  # secret-free; runs only the cached identity probe

    is_cli = coordinator.provider is not None and hasattr(coordinator.provider, "runtime_dir")
    executable = config.executable if is_cli else None
    resolved = _resolve(executable) if is_cli else None
    working_directory = (
        str(coordinator.provider.runtime_dir()) if is_cli else None  # type: ignore[union-attr]
    )

    probe_result = (
        await _run_probe(coordinator) if probe else CoordinatorProbeResult(attempted=False)
    )

    diag = CoordinatorDiagnostics(
        provider=config.provider,
        configured=status.configured,
        missing_configuration=status.missing_configuration,
        model=config.model or "default",
        timeout_seconds=config.timeout_seconds,
        executable=executable,
        executable_resolves=(resolved is not None) if is_cli else None,
        resolved_executable_path=resolved,
        cli_version=status.cli_version,
        working_directory=working_directory,
        base_url_configured=bool(config.base_url) and not is_cli,
        api_key_configured=bool(config.api_key) and not is_cli,
        probe=probe_result,
    )
    diag.hints = _build_hints(diag, config.provider)
    return diag
