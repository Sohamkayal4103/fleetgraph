"""beta.9 Defect 4: `aithernet run "<prompt>"` is the one-command workflow — it creates the
mission, starts autonomous execution through the canonical engine (background worker), streams
progress, prints the final result + identifiers, and returns a meaningful exit code. An unreachable
node routes the customer to guided setup, never a raw traceback.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from typer.testing import CliRunner

from aithernet.api.app import create_app
from aithernet.cli import app as cli_app
from aithernet.config.settings import (
    CoordinatorConfig,
    MissionBudgets,
    MissionExecutionConfig,
    NodeConfig,
)
from aithernet.coordinator.contracts import CoordinatorDecision
from aithernet.coordinator.providers.base import CoordinatorProvider
from aithernet.coordinator.runtime import CoordinatorRuntime
from aithernet.orchestrator.runtime import NodeRuntime


def _dec(disposition, *, target="respond", **mc):
    return {"summary": "s", "next_target": target, "action": "act", "message": "m",
            "structured_payload": {}, "expected_result": "ok",
            "mission_control": {"disposition": disposition, **mc}}


class SeqProvider(CoordinatorProvider):
    name = "seq"

    def __init__(self, seq):
        super().__init__(CoordinatorConfig(provider=self.name))
        self.seq = seq
        self.calls = 0

    def check_ready(self):
        return []

    async def decide(self, payload):
        fields = self.seq[min(self.calls, len(self.seq) - 1)]
        self.calls += 1
        return CoordinatorDecision.from_model_json(fields, mission_id=payload.mission_id)


@contextmanager
def _serve(app):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server failed to start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _runtime(tmp_path, seq):
    me = MissionExecutionConfig(
        enabled=True, lease_duration_seconds=30, lease_renew_interval_seconds=10,
        poll_interval_seconds=0.1, default_budgets=MissionBudgets())
    cfg = NodeConfig(node_id="t", node_name="t",
                     database_url=f"sqlite:///{tmp_path / 'run.db'}", mission_execution=me)
    return NodeRuntime.from_config(
        cfg, coordinator=CoordinatorRuntime(CoordinatorConfig(provider="seq"), SeqProvider(seq)))


def test_run_one_command_completes_and_prints_result(tmp_path):
    rt = _runtime(tmp_path, [_dec("continue", target="respond"),
                             _dec("complete", final_response="all good")])
    with _serve(create_app(runtime=rt)) as base:
        result = CliRunner().invoke(
            cli_app, ["run", "make a thing", "--url", base, "--interval", "0.1", "--timeout", "30"])
    assert result.exit_code == 0, result.output
    assert "Mission created:" in result.output
    assert "Mission completed" in result.output
    assert "all good" in result.output
    # identifiers are printed for both mission and run
    assert "mission:" in result.output and "run:" in result.output


def test_run_blocked_returns_nonzero(tmp_path):
    # An immediate permanent block (unknown route) -> mission blocks -> exit code 1.
    rt = _runtime(tmp_path, [_dec("continue", target="does_not_exist")])
    with _serve(create_app(runtime=rt)) as base:
        result = CliRunner().invoke(
            cli_app, ["run", "do x", "--url", base, "--interval", "0.1", "--timeout", "30"])
    assert result.exit_code == 1, result.output
    assert "mission:" in result.output


def test_run_detach_returns_immediately(tmp_path):
    rt = _runtime(tmp_path, [_dec("complete", final_response="bg")])
    with _serve(create_app(runtime=rt)) as base:
        result = CliRunner().invoke(cli_app, ["run", "do x", "--url", base, "--detach"])
    assert result.exit_code == 0, result.output
    assert "Detached" in result.output
    assert "aithernet mission watch" in result.output


def test_run_unreachable_directs_to_setup(tmp_path, monkeypatch):
    from aithernet.config import loader
    monkeypatch.setattr(
        loader, "canonical_node_config_path", lambda: tmp_path / "nope" / "node.yaml")
    # Nothing is listening on this port -> guidance, exit 2 (not a traceback).
    result = CliRunner().invoke(cli_app, ["run", "do x", "--url", "http://127.0.0.1:1"])
    assert result.exit_code == 2, result.output
    assert "aithernet setup" in result.output


def test_run_unreachable_but_setup_done_directs_to_start(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "node.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("node_id: x\n")
    from aithernet.config import loader
    monkeypatch.setattr(loader, "canonical_node_config_path", lambda: cfg)
    result = CliRunner().invoke(cli_app, ["run", "do x", "--url", "http://127.0.0.1:1"])
    assert result.exit_code == 2, result.output
    assert "service start" in result.output
