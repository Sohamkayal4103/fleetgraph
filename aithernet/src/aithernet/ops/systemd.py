"""systemd unit generator (Stage 14A, area 4).

Renders a hardened, deployment-specific ``aithernet.service`` unit from the node's resolved
paths. The generated unit is working-directory independent (it uses the node state root, never
the CWD), references an EnvironmentFile (secrets are never interpolated on the command line),
uses a bounded restart backoff, reaps the whole subprocess tree on stop (no orphan gr-mcp/
coordinator/coding processes), and writes structured logs to the journal. Docker is intentionally
NOT the primary path — it may be documented as optional future work.
"""

from __future__ import annotations

from pathlib import Path

from aithernet.ops.paths import NodePaths

_TEMPLATE = """[Unit]
Description=Aithernet autonomous SDR node ({node_id})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={state_root}
EnvironmentFile=-{env_file}
ExecStartPre={executable} node preflight --config {config_file}
ExecStart={executable} start --config {config_file}
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=5
KillSignal=SIGINT
TimeoutStopSec={stop_timeout}
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={state_root}
ProtectControlGroups=true
RestrictRealtime=true
LockPersonality=true
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aithernet
[Install]
WantedBy=multi-user.target
"""


def render_systemd_unit(
    *,
    node_id: str,
    state_root: str | Path,
    executable: str = "/opt/aithernet/.venv/bin/aithernet",
    user: str = "aithernet",
    env_file: str | None = None,
    stop_timeout: int = 45,
) -> str:
    """Render a hardened systemd unit for a node rooted at ``state_root``."""
    paths = NodePaths.from_root(state_root)
    return _TEMPLATE.format(
        node_id=node_id,
        user=user,
        state_root=str(paths.root),
        env_file=env_file or str(paths.config_dir / "aithernet.env"),
        executable=executable,
        config_file=str(paths.config_file),
        stop_timeout=stop_timeout,
    )
