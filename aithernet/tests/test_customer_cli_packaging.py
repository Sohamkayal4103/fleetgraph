"""Packaging + docs-regression for the customer CLI (Stage 14G, Commit 5).

Proves the setup/components/agents/doctor commands are registered, that every `aithernet …` command
shown in docs/customer-cli.md actually exists (no doc drift), and that the managed-component specs
ship in the relocatable install payload the .deb is built from (`pip install --target`, same as
scripts/build_deb.sh).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aithernet.cli import app

_ROOT = Path(__file__).resolve().parent.parent
_runner = CliRunner()


def _walk_command_names(typer_app, prefix=()):
    """Yield the full command paths registered on a Typer app (groups + commands)."""
    for cmd in typer_app.registered_commands:
        name = cmd.name or (cmd.callback.__name__.replace("_", "-") if cmd.callback else "")
        yield (*prefix, name)
    for grp in typer_app.registered_groups:
        sub = grp.typer_instance
        gname = grp.name or sub.info.name
        yield (*prefix, gname)
        yield from _walk_command_names(sub, (*prefix, gname))


def _registered_paths():
    return {" ".join(p) for p in _walk_command_names(app)}


def test_top_level_commands_registered():
    paths = _registered_paths()
    assert "setup" in paths
    assert "doctor" in paths
    for sub in ("list", "plan", "install", "verify", "repair", "remove", "status", "adopt"):
        assert f"components {sub}" in paths, f"components {sub} missing"
    for sub in ("providers", "configure", "test", "remove", "provider-status"):
        assert f"agents {sub}" in paths, f"agents {sub} missing"
    assert "agents configure coordinator" in paths
    assert "agents configure coding" in paths


def test_no_collision_with_existing_agents_status():
    # the Stage 7 external-agent `agents status` must still exist alongside provider commands
    assert "agents status" in _registered_paths()


def test_customer_mission_verbs_are_canonical():
    """Customer mission verbs are exposed over the canonical engine (no second mission system)."""
    paths = _registered_paths()
    for verb in ("create", "run", "status", "events", "artifacts", "cancel", "resume", "export"):
        assert f"mission {verb}" in paths, f"mission {verb} missing"
    # they coexist with the canonical primitives they delegate to
    for canon in ("submit", "show", "start", "timeline"):
        assert f"mission {canon}" in paths


def test_duplicate_rf_mission_engine_is_removed():
    # the parallel mission engine/event store/workspace must no longer exist
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("aithernet.missions.rf.engine")


def test_docs_commands_all_exist():
    """Every `aithernet <cmd> …` example in the customer-facing docs must be a real command path."""
    paths = _registered_paths()
    missing = []
    for rel in ("docs/customer-cli.md", "docs/clean-machine-acceptance.md",
                "docs/rf-mcp-component.md"):
        for line in (_ROOT / rel).read_text().splitlines():
            for m in re.finditer(r"\baithernet\s+([a-z][\w-]*(?:\s+[a-z][\w-]*){0,2})", line):
                tokens = m.group(1).split()
                # the longest token-prefix that is a registered path (args/values follow it)
                if not any(" ".join(tokens[:n]) in paths for n in range(len(tokens), 0, -1)):
                    missing.append(f"{rel}: {' '.join(tokens)}")
    assert not missing, f"docs reference unknown commands: {sorted(set(missing))}"


def test_setup_and_doctor_help_render():
    for cmd in (["setup", "--help"], ["doctor", "--help"], ["components", "--help"],
                ["agents", "--help"]):
        result = _runner.invoke(app, cmd)
        assert result.exit_code == 0, f"{cmd} -> {result.output}"


def test_component_specs_ship_in_install_payload(tmp_path):
    """The relocatable install tree the .deb is built from carries the managed-component specs."""
    pytest.importorskip("build")
    wheeldir = tmp_path / "wheel"
    proc = subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(wheeldir),
                           str(_ROOT)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        pytest.skip(f"wheel build unavailable: {proc.stderr[-200:]}")
    wheel = next(wheeldir.glob("*.whl"))
    target = tmp_path / "lib"
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
                    "--target", str(target), str(wheel)], check=True, timeout=300)
    spec = target / "aithernet" / "_component_specs" / "rf-mcp" / "component.toml"
    assert spec.is_file(), "rf-mcp spec missing from the install payload"
    assert "upstream_commit" in spec.read_text()
