"""Part C: the Codex CLI coding-agent provider.

A fake ``codex`` script stands in for the real CLI: it answers ``--version`` (so the
identity probe passes), reads the prompt from stdin, edits a file inside the configured
workspace, writes the final message to the ``-o`` file, emits a JSONL event, and exits
with a controllable code. Neither Codex nor a network is required.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aithernet.coding_agent.contracts import (
    CodingAgentProviderError,
    CodingTaskExecutionInput,
)
from aithernet.coding_agent.providers.codex_cli import CodexCliProvider
from aithernet.config.settings import CodingAgentConfig

_VERSION = "codex-cli 9.9.9-fake"


def _write_fake_codex(
    path: Path,
    *,
    last_message: str = "Created the requested file and reported the change.",
    create_file: str | None = "CODEX_SMOKE_TEST.md",
    exit_code: int = 0,
    sleep: float = 0.0,
) -> Path:
    create = (
        f"open(os.path.join(workdir, {create_file!r}), 'w').write('made by fake codex')\n"
        if create_file
        else ""
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, json, time\n"
        "args = sys.argv[1:]\n"
        "if '--version' in args:\n"
        f"    print({_VERSION!r})\n"
        "    sys.exit(0)\n"
        "def opt(flag):\n"
        "    return args[args.index(flag)+1] if flag in args else None\n"
        "workdir = opt('-C') or os.getcwd()\n"
        "out = opt('-o')\n"
        "prompt = sys.stdin.read()\n"
        f"time.sleep({sleep})\n"
        f"{create}"
        f"if out:\n    open(out, 'w').write({last_message!r})\n"
        "print(json.dumps({'type': 'item.started', 'item': {'type': 'agent_reasoning'}}))\n"
        "msg = {'type': 'item.completed',"
        f" 'item': {{'type': 'agent_message', 'text': {last_message!r}}}}}\n"
        "print(json.dumps(msg))\n"
        "sys.stderr.write('fake codex stderr line\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    path.chmod(0o755)
    return path


def _task(
    workspace: str, objective: str = "Create CODEX_SMOKE_TEST.md"
) -> CodingTaskExecutionInput:
    return CodingTaskExecutionInput(
        task_id="t-codex-1",
        mission_id="m-1",
        objective=objective,
        context={},
        available_tools=[],
        expected_outputs=["CODEX_SMOKE_TEST.md"],
        reporting_requirements=["report the file changed"],
        workspace=workspace,
        current_time=datetime.now(UTC),
    )


def _provider(executable: Path, **overrides) -> CodexCliProvider:
    return CodexCliProvider(
        CodingAgentConfig(provider="codex_cli", executable=str(executable), **overrides)
    )


def test_argv_uses_codex_exec_stdin_and_workspace_write(tmp_path) -> None:
    fake = _write_fake_codex(tmp_path / "codex")
    provider = _provider(fake)
    argv = provider._argv(str(fake), "/work", "/tmp/last.txt")
    assert argv[1] == "exec"
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("-C") + 1] == "/work"
    assert argv[argv.index("-o") + 1] == "/tmp/last.txt"
    assert "--skip-git-repo-check" in argv
    assert argv[-1] == "-"  # prompt read from stdin, not argv


def test_execute_runs_codex_and_edits_workspace(tmp_path) -> None:
    workspace = tmp_path / "ws"
    fake = _write_fake_codex(tmp_path / "codex")
    provider = _provider(fake)

    result = asyncio.run(provider.execute(_task(str(workspace))))

    assert result.status == "completed"
    assert result.exit_code == 0
    # Codex actually performed work inside the workspace.
    assert (workspace / "CODEX_SMOKE_TEST.md").is_file()
    # Summary derived from Codex's final message (never fabricated).
    assert "Created the requested file" in result.summary
    # Raw output retained for audit.
    assert "fake codex stderr line" in result.stderr
    assert "agent_message" in result.stdout
    # Payload metadata, no secrets/prompt.
    assert result.payload["provider"] == "codex_cli"
    assert result.payload["codex_version"] == _VERSION
    assert result.payload["sandbox"] == "workspace-write"
    # files_changed/commands_run not fabricated.
    assert result.files_changed == []
    assert result.commands_run == []


def test_nonzero_exit_is_failed_result(tmp_path) -> None:
    workspace = tmp_path / "ws"
    fake = _write_fake_codex(
        tmp_path / "codex", exit_code=3, last_message="", create_file=None
    )
    provider = _provider(fake)

    result = asyncio.run(provider.execute(_task(str(workspace))))

    assert result.status == "failed"
    assert result.exit_code == 3
    # A failed process is never reported completed.
    assert "exit" in result.summary.lower() or "3" in result.summary


def test_timeout_raises_provider_error_and_kills_process(tmp_path) -> None:
    workspace = tmp_path / "ws"
    fake = _write_fake_codex(tmp_path / "codex", sleep=5.0)
    provider = _provider(fake, timeout_seconds=0.5)

    with pytest.raises(CodingAgentProviderError) as exc:
        asyncio.run(provider.execute(_task(str(workspace))))
    assert "timed out" in str(exc.value)


def test_mismatched_executable_raises_configuration_error(tmp_path) -> None:
    # A binary that is not Codex must not be run as Codex.
    not_codex = tmp_path / "tool"
    not_codex.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('totally-different-tool 1.0')\nsys.exit(0)\n"
    )
    not_codex.chmod(0o755)
    provider = _provider(not_codex)
    from aithernet.coding_agent.contracts import CodingAgentConfigurationError

    with pytest.raises(CodingAgentConfigurationError):
        asyncio.run(provider.execute(_task(str(tmp_path / "ws"))))
