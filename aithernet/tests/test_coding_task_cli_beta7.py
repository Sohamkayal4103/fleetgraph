"""beta.7 (FIX 9) — coding-task inspection CLI (status/logs/artifacts) exists, and the mission
timeline surfaces the coding task id so users can inspect it."""
from __future__ import annotations

from typer.testing import CliRunner

from aithernet.cli import app, coding_task_app
from aithernet.schemas.mission_runs import MissionTimelineEntry

runner = CliRunner()


def test_coding_task_status_logs_artifacts_are_registered():
    names = {c.name for c in coding_task_app.registered_commands}
    assert {"status", "logs", "artifacts"} <= names
    # the exact beta.6 miss: `coding-task status <id>` must be a real command
    r = runner.invoke(app, ["coding-task", "status", "--help"])
    assert r.exit_code == 0
    assert "commands run" in r.output.lower()


def test_timeline_entry_carries_coding_task_id():
    from datetime import datetime, timezone
    e = MissionTimelineEntry(at=datetime.now(timezone.utc), kind="event",
                             event_type="coding_task.started", message="Coding task abc started.",
                             coding_task_id="abc")
    assert e.coding_task_id == "abc"
    # default stays None for non-coding events (no noise)
    e2 = MissionTimelineEntry(at=datetime.now(timezone.utc), kind="event", message="x")
    assert e2.coding_task_id is None
