"""Codex coding execution + bounded workspace (beta.8 phase 6).

The runtime must build the SELECTED coding provider (codex_cli) from the canonical config, run in
an absolute per-task workspace bounded beneath the canonical coding root, and resolve the configured
executable. (Phase 3 made coding_agent.workspace absolute.)
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aithernet.coding_agent.contracts import CodingAgentConfigurationError
from aithernet.coding_agent.providers.codex_cli import CodexCliProvider
from aithernet.coding_agent.runtime import CodingAgentRuntime, bounded_task_workspace
from aithernet.config.settings import CodingAgentConfig


def test_runtime_builds_codex_from_config():
    rt = CodingAgentRuntime.from_config(
        CodingAgentConfig(provider="codex_cli", executable="codex", workspace="/tmp/coding"))
    assert rt.provider is not None
    assert rt.provider.name == "codex_cli"
    assert rt.config.provider == "codex_cli"


def test_bounded_task_workspace_is_absolute_and_contained(tmp_path):
    root = tmp_path / "coding"
    ws = bounded_task_workspace(str(root), "task-abc-123")
    assert Path(ws).is_absolute()
    assert Path(ws) == (root / "task-abc-123").resolve()
    assert str(root.resolve()) in ws


def test_bounded_task_workspace_rejects_escape(tmp_path):
    with pytest.raises(CodingAgentConfigurationError):
        bounded_task_workspace(str(tmp_path / "coding"), "../escape")
    with pytest.raises(CodingAgentConfigurationError):
        bounded_task_workspace(str(tmp_path / "coding"), "a/b")


def test_per_task_workspaces_are_distinct(tmp_path):
    root = str(tmp_path / "coding")
    assert bounded_task_workspace(root, "t1") != bounded_task_workspace(root, "t2")


def test_codex_provider_resolves_absolute_executable(tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    p = CodexCliProvider(CodingAgentConfig(provider="codex_cli", executable=str(fake),
                                           workspace=str(tmp_path / "coding")))
    # The configured ABSOLUTE executable resolves (the path is found + executable). A fake that is
    # not really codex may still fail the codex identity probe — but it must NOT be 'missing'.
    assert p._resolve_executable() == str(fake)
    assert "executable_missing" not in p.check_ready()
    assert os.access(fake, os.X_OK)
