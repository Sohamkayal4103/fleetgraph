"""Production-safe node directory layout (Stage 14A, area 1).

A node's persistent state lives beneath an explicit *state root* with a deterministic layout,
so production never depends on the repository working directory. The layout is:

    <root>/
    ├── config/        node.yaml + .env (operator-supplied configuration)
    ├── db/            SQLite database
    ├── identity/      Ed25519 node identity key (0700)
    ├── run/           PID + runtime state
    ├── logs/          rotating application logs
    ├── backups/       node backups + rollback snapshots
    ├── rf/            per-backend RF workspaces (containment boundary)
    ├── coordinator/   coordinator runtime scratch
    ├── coding/        coding-agent workspace
    ├── artifacts/     content-addressed artifact store
    └── tmp/           temporary files

``NodePaths`` only RESOLVES paths; it never creates or mutates anything (provisioning does that).
Directories that hold secrets (identity) are created 0700; the root and data dirs are 0750.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# (attribute, relative dir, octal mode) — identity is private (0700), the rest are 0750.
LAYOUT: tuple[tuple[str, str, int], ...] = (
    ("config_dir", "config", 0o750),
    ("db_dir", "db", 0o750),
    ("identity_dir", "identity", 0o700),
    ("run_dir", "run", 0o750),
    ("logs_dir", "logs", 0o750),
    ("backups_dir", "backups", 0o750),
    ("rf_dir", "rf", 0o750),
    ("coordinator_dir", "coordinator", 0o750),
    ("coding_dir", "coding", 0o750),
    ("artifacts_dir", "artifacts", 0o750),
    ("tmp_dir", "tmp", 0o750),
)


@dataclass(frozen=True)
class NodePaths:
    """Resolved production paths for a node, rooted at an explicit state root."""

    root: Path

    @classmethod
    def from_root(cls, state_root: str | Path) -> NodePaths:
        return cls(Path(state_root).expanduser().resolve())

    # -- standard layout directories ---------------------------------------------

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def identity_dir(self) -> Path:
        return self.root / "identity"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def rf_dir(self) -> Path:
        return self.root / "rf"

    @property
    def coordinator_dir(self) -> Path:
        return self.root / "coordinator"

    @property
    def coding_dir(self) -> Path:
        return self.root / "coding"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    # -- well-known files --------------------------------------------------------

    @property
    def config_file(self) -> Path:
        return self.config_dir / "node.yaml"

    @property
    def database_file(self) -> Path:
        return self.db_dir / "aithernet.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_file}"

    @property
    def pid_file(self) -> Path:
        return self.run_dir / "aithernet.pid"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "aithernet.log"

    def all_dirs(self) -> list[tuple[Path, int]]:
        """Every layout directory paired with its target permission mode (root first)."""
        dirs: list[tuple[Path, int]] = [(self.root, 0o750)]
        for attr, _rel, mode in LAYOUT:
            dirs.append((getattr(self, attr), mode))
        return dirs

    def describe(self) -> dict[str, str]:
        """A sanitized name→path map for diagnostics (paths are the operator's own layout)."""
        return {attr: str(getattr(self, attr)) for attr, _rel, _mode in LAYOUT}
