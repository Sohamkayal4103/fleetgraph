"""Regression tests for the Gemini CLI coordinator IDE-integration fix (Stage 12 patch).

Root cause being guarded: when Aithernet is started from a VS Code integrated environment,
the Gemini CLI inherits ``GEMINI_CLI_IDE_*`` companion variables and its IDE integration
tries to attach to the editor's open workspace — which mismatches the isolated coordinator
runtime directory, so Gemini exits 0 with a directory-mismatch error and no JSON.

A fake ``gemini`` executable (no real CLI/network/account) records the environment it is
spawned with, so the tests can prove the IDE variables are stripped while authentication
variables survive, and can simulate the directory-mismatch failure. The autonomous-loop
tests use deterministic in-process fakes (no Gemini at all).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aithernet.config.settings import (
    CoordinatorConfig,
    MissionBudgets,
    MissionExecutionConfig,
    NodeConfig,
)
from aithernet.coordinator.contracts import (
    CoordinatorDecision,
    CoordinatorInput,
    CoordinatorProviderError,
)
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.providers.gemini_cli import (
    _IDE_ENV_PREFIX,
    _RUNTIME_SETTINGS,
    GeminiCLIProvider,
)
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.orchestrator.runtime import NodeRuntime
from aithernet.schemas.missions import MissionCreate, MissionStatus
from aithernet.state.models import utcnow
from aithernet.state.repositories import MissionExecutionRunRepository

_VALID_DECISION = {
    "summary": "Reviewed the node state.",
    "next_target": "respond",
    "action": "acknowledge",
    "message": "Node status summarized.",
    "structured_payload": {},
    "expected_result": "The mission source is informed.",
    "confidence": 0.9,
    "mission_control": {"disposition": "complete", "final_response": "All good."},
}

#: An IDE companion stderr matching the observed production failure.
_IDE_MISMATCH_STDERR = (
    "[ERROR] [IDEClient] Directory mismatch. Gemini CLI is running in a different "
    "location than the open workspace in the IDE. Please run the CLI from one of the "
    "following directories: /home/operator/gnu/aithernet"
)


def _write_fake_gemini(
    path: Path,
    *,
    version: str = "0.46.0-fake",
    response: str | None = json.dumps(_VALID_DECISION),
    env_capture: Path | None = None,
    directory_mismatch: bool = False,
    exit_code: int = 0,
) -> Path:
    """Write a fake ``gemini`` that records its spawn environment and prints configurable JSON.

    The fake dumps the NAMES of the environment variables it received (never their values)
    to ``env_capture`` so tests can assert presence/absence without persisting secrets. When
    ``directory_mismatch`` is set it emulates the IDE-integration failure: exit 0, no stdout,
    an ``[IDEClient] Directory mismatch`` stderr.
    """
    payload = {
        "session_id": "fake-session",
        "response": response,
        "stats": {"models": {"gemini-3-flash-preview": {}}, "tools": {"totalCalls": 0}},
    }
    out = "" if directory_mismatch else json.dumps(payload)
    lines = [
        "#!/usr/bin/env python3",
        "import sys, os",
        "args = sys.argv[1:]",
        "if '--version' in args:",
        f"    print({version!r})",
        "    sys.exit(0)",
        "data = sys.stdin.read()",
    ]
    if env_capture is not None:
        # Record only the variable NAMES (sorted), one per line — never values.
        lines.append(
            f"open(r{str(env_capture)!r}, 'w', encoding='utf-8')"
            ".write(chr(10).join(sorted(os.environ)))"
        )
    if directory_mismatch:
        lines.append(f"sys.stderr.write({_IDE_MISMATCH_STDERR!r})")
        lines.append("sys.exit(0)")
    else:
        lines.append(f"sys.stdout.write({out!r})")
        lines.append(f"sys.exit({exit_code})")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def _provider(executable: Path | str, **overrides) -> GeminiCLIProvider:
    cfg = CoordinatorConfig(provider="gemini_cli", executable=str(executable), **overrides)
    return GeminiCLIProvider(cfg)


def _input() -> CoordinatorInput:
    return CoordinatorInput(
        mission_id="m-1",
        mission_content="Summarize the current node status.",
        mission_source_type="user",
        mission_status="received",
        current_time=datetime.now(UTC),
    )


def _decide(provider: GeminiCLIProvider):
    return asyncio.run(provider.decide(_input()))


def _set_ide_env(monkeypatch, capture: Path | None = None) -> None:
    """Populate the inherited environment with IDE companion variables (as VS Code would)."""
    monkeypatch.setenv("GEMINI_CLI_IDE_WORKSPACE_PATH", "/home/operator/gnu/aithernet")
    monkeypatch.setenv("GEMINI_CLI_IDE_SERVER_PORT", "57321")
    monkeypatch.setenv("GEMINI_CLI_IDE_FUTURE_FLAG", "anything")
    monkeypatch.setenv("HOME", str(Path.home()))
    if capture is not None:
        # A non-IDE var used only to tell the fake where to record its environment.
        monkeypatch.setenv("FAKE_GEMINI_ENV_CAPTURE", str(capture))


# ================================================================================
# 1-4 + 10: subprocess environment is stripped of IDE vars, auth vars survive
# ================================================================================


def _capture_child_env(tmp_path, monkeypatch) -> list[str]:
    capture = tmp_path / "env_names.txt"
    fake = _write_fake_gemini(tmp_path / "gemini", env_capture=capture)
    _set_ide_env(monkeypatch, capture)
    _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    return capture.read_text().splitlines()


def test_ide_workspace_path_absent_in_child(tmp_path, monkeypatch) -> None:
    names = _capture_child_env(tmp_path, monkeypatch)
    assert "GEMINI_CLI_IDE_WORKSPACE_PATH" not in names


def test_ide_server_port_absent_in_child(tmp_path, monkeypatch) -> None:
    names = _capture_child_env(tmp_path, monkeypatch)
    assert "GEMINI_CLI_IDE_SERVER_PORT" not in names


def test_arbitrary_future_ide_vars_removed(tmp_path, monkeypatch) -> None:
    names = _capture_child_env(tmp_path, monkeypatch)
    assert not any(name.startswith(_IDE_ENV_PREFIX) for name in names)


def test_non_ide_auth_vars_remain(tmp_path, monkeypatch) -> None:
    names = _capture_child_env(tmp_path, monkeypatch)
    # HOME/PATH (auth + execution) and the non-IDE marker survive the strip.
    assert "HOME" in names
    assert "PATH" in names
    assert "FAKE_GEMINI_ENV_CAPTURE" in names


def test_child_env_builder_unit(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_CLI_IDE_WORKSPACE_PATH", "/x")
    monkeypatch.setenv("GEMINI_CLI_IDE_SERVER_PORT", "1")
    monkeypatch.setenv("HOME", "/home/user")
    env = _provider("gemini")._child_env()
    assert not any(k.startswith(_IDE_ENV_PREFIX) for k in env)
    assert env.get("HOME") == "/home/user"
    assert "PATH" in env


# ================================================================================
# 5-6: runtime settings disable IDE and preserve tool restrictions
# ================================================================================


def test_runtime_settings_disable_ide(tmp_path) -> None:
    provider = _provider("gemini", working_directory=str(tmp_path / "rt"))
    runtime = provider._ensure_runtime_dir()
    settings = json.loads((runtime / ".gemini" / "settings.json").read_text())
    assert settings["ide"]["enabled"] is False


def test_runtime_settings_preserve_tool_restrictions(tmp_path) -> None:
    provider = _provider("gemini", working_directory=str(tmp_path / "rt"))
    runtime = provider._ensure_runtime_dir()
    settings = json.loads((runtime / ".gemini" / "settings.json").read_text())
    # The existing tool/MCP restrictions are NOT weakened by adding ide.enabled.
    assert settings["tools"]["core"] == []
    assert settings["mcpServers"] == {}


def test_runtime_settings_constant_shape() -> None:
    assert _RUNTIME_SETTINGS["ide"] == {"enabled": False}
    assert _RUNTIME_SETTINGS["tools"] == {"core": []}
    assert _RUNTIME_SETTINGS["mcpServers"] == {}


# ================================================================================
# 7: diagnostics and normal invocation share one environment builder
# ================================================================================


def test_diagnostics_and_decision_share_env_builder(tmp_path, monkeypatch) -> None:
    # The diagnostics probe (healthcheck) returns "ok"; capture its child env.
    probe_capture = tmp_path / "probe_env.txt"
    probe_fake = _write_fake_gemini(
        tmp_path / "gemini_probe",
        response=json.dumps({"ok": True}),
        env_capture=probe_capture,
    )
    decide_capture = tmp_path / "decide_env.txt"
    decide_fake = _write_fake_gemini(tmp_path / "gemini_decide", env_capture=decide_capture)

    _set_ide_env(monkeypatch)
    monkeypatch.setenv("FAKE_GEMINI_ENV_CAPTURE", str(probe_capture))
    asyncio.run(_provider(probe_fake, working_directory=str(tmp_path / "rt")).healthcheck())
    monkeypatch.setenv("FAKE_GEMINI_ENV_CAPTURE", str(decide_capture))
    _decide(_provider(decide_fake, working_directory=str(tmp_path / "rt2")))

    probe_names = set(probe_capture.read_text().splitlines())
    decide_names = set(decide_capture.read_text().splitlines())
    # Both paths strip every IDE var and both keep HOME — i.e. the same builder.
    assert not any(n.startswith(_IDE_ENV_PREFIX) for n in probe_names)
    assert not any(n.startswith(_IDE_ENV_PREFIX) for n in decide_names)
    assert "HOME" in probe_names and "HOME" in decide_names


# ================================================================================
# 8: a fake executable returns valid JSON when IDE variables are absent
# ================================================================================


def test_valid_json_when_ide_vars_present_but_stripped(tmp_path, monkeypatch) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini")
    _set_ide_env(monkeypatch)
    decision = _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    assert decision.mission_id == "m-1"
    assert decision.mission_control.disposition.value == "complete"


# ================================================================================
# 9: a fake simulating directory mismatch produces a clear sanitized failure
# ================================================================================


def test_directory_mismatch_produces_clear_error(tmp_path, monkeypatch) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", directory_mismatch=True)
    _set_ide_env(monkeypatch)
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    message = str(exc.value)
    assert "IDE integration attempted to attach to a different workspace" in message
    assert "disabled" in message


# ================================================================================
# 10: no environment values appear in errors
# ================================================================================


def test_no_env_values_in_error(tmp_path, monkeypatch) -> None:
    fake = _write_fake_gemini(tmp_path / "gemini", directory_mismatch=True)
    monkeypatch.setenv("GEMINI_CLI_IDE_WORKSPACE_PATH", "/secret/workspace/path/value")
    monkeypatch.setenv("GEMINI_CLI_IDE_SERVER_PORT", "59999")
    with pytest.raises(CoordinatorProviderError) as exc:
        _decide(_provider(fake, working_directory=str(tmp_path / "rt")))
    message = str(exc.value)
    # The bounded sanitized stderr preview is allowed; raw env VALUES are not surfaced.
    assert "/secret/workspace/path/value" not in message
    assert "59999" not in message


# ================================================================================
# Autonomous-run failure handling (11-14) — deterministic in-process coordinators
# ================================================================================

TERMINAL_RUN = {"completed", "blocked", "failed", "cancelled"}


class _AlwaysFailProvider(CoordinatorProvider):
    name = "always_fail"

    def __init__(self) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self.calls = 0

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload) -> CoordinatorDecision:
        self.calls += 1
        raise CoordinatorProviderError("simulated Gemini failure")


class _IdeFailThenSucceedProvider(CoordinatorProvider):
    """Fails like the IDE-mismatch bug on the first call, then completes (post-fix)."""

    name = "ide_then_ok"

    def __init__(self) -> None:
        super().__init__(CoordinatorConfig(provider=self.name))
        self.calls = 0

    def check_ready(self) -> list[str]:
        return []

    async def decide(self, payload) -> CoordinatorDecision:
        self.calls += 1
        if self.calls == 1:
            raise CoordinatorProviderError("Gemini CLI produced no JSON output (IDE mismatch)")
        return CoordinatorDecision.from_model_json(
            {
                "summary": "ok",
                "next_target": "respond",
                "action": "a",
                "message": "m",
                "structured_payload": {},
                "expected_result": "e",
                "mission_control": {"disposition": "complete", "final_response": "done"},
            },
            mission_id=payload.mission_id,
        )


def _runtime(tmp_path, provider, *, max_failures=3):
    me = MissionExecutionConfig(
        enabled=False,
        poll_interval_seconds=0.1,
        default_budgets=MissionBudgets(max_consecutive_failures=max_failures),
    )
    config = NodeConfig(
        node_id="ide-node",
        node_name="t",
        database_url=f"sqlite:///{tmp_path / 'ide.db'}",
        mission_execution=me,
    )
    return NodeRuntime.from_config(
        config, coordinator=CoordinatorRuntime(CoordinatorConfig(provider=provider.name), provider)
    )


def _mk_mission(runtime):
    payload = MissionCreate(content="assess node", source_type="user")
    return asyncio.run(runtime.create_mission(payload))


def test_autonomous_succeeds_when_ide_vars_originally_present(tmp_path, monkeypatch) -> None:
    # Even if the process env originally contains IDE vars, a working coordinator completes.
    _set_ide_env(monkeypatch)
    provider = _IdeFailThenSucceedProvider()
    runtime = _runtime(tmp_path, provider, max_failures=5)
    mission = _mk_mission(runtime)
    asyncio.run(runtime.worker_manager.enqueue_mission(mission.id))
    for _ in range(10):
        status = asyncio.run(runtime.worker_manager.run_one_step(mission.id))
        if status.run.status in TERMINAL_RUN:
            break
    assert status.run.status == "completed"


def test_repeated_failures_exhaust_budget_no_infinite_retry(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    runtime = _runtime(tmp_path, provider, max_failures=3)
    mission = _mk_mission(runtime)
    asyncio.run(runtime.worker_manager.enqueue_mission(mission.id))
    last = None
    # Bounded: must reach a terminal (blocked) state well within the loop cap.
    for _ in range(20):
        last = asyncio.run(runtime.worker_manager.run_one_step(mission.id))
        if last.run.status in TERMINAL_RUN:
            break
    assert last.run.status == "blocked"
    # The coordinator was not retried indefinitely (bounded by the failure budget).
    assert provider.calls <= 3


def test_recovery_preserves_failure_counters(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    runtime = _runtime(tmp_path, provider, max_failures=5)
    mission = _mk_mission(runtime)
    run_read, _ = asyncio.run(runtime.worker_manager.enqueue_mission(mission.id))
    # Two failing iterations.
    asyncio.run(runtime.worker_manager.run_one_step(mission.id))
    asyncio.run(runtime.worker_manager.run_one_step(mission.id))
    before = runtime.mission_execution_status(mission.id).run.consecutive_failures
    assert before == 2
    # Simulate an interrupted active run + restart recovery.
    from datetime import timedelta

    past = utcnow() - timedelta(seconds=120)
    with runtime.session_scope() as s:
        MissionExecutionRunRepository(s).acquire_lease(
            run_read.id, owner_id="dead", token="t", now=past, expiry=past
        )
        s.commit()
    asyncio.run(runtime.worker_manager.recover())
    after = runtime.mission_execution_status(mission.id).run.consecutive_failures
    # Recovery requeues but does NOT reset the consecutive-failure usage.
    assert after == before


def test_cancellation_prevents_further_coordinator_calls(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    runtime = _runtime(tmp_path, provider, max_failures=5)
    mission = _mk_mission(runtime)
    asyncio.run(runtime.worker_manager.enqueue_mission(mission.id))
    asyncio.run(runtime.worker_manager.cancel_mission(mission.id))
    calls_at_cancel = provider.calls
    assert runtime.mission_execution_status(mission.id).run.status == "cancelled"
    assert runtime.get_mission(mission.id).status is MissionStatus.CANCELLED
    # A cancelled run makes no further coordinator calls.
    assert provider.calls == calls_at_cancel
