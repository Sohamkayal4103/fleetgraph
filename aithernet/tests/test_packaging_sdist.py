"""Packaging-content regression: the authoritative ``python -m build`` sdist must be clean.

Builds the ACTUAL source distribution from the real project (no ``git archive`` workaround) and
inspects its archive members. Fails if any forbidden path or suspicious credential filename leaks,
or if a file required to build/install is missing. This guards the Stage 14F packaging fix that
keeps frontend dependency trees, local-environment files, secrets/keys, databases, runtime/release
artifact stores and external RF checkouts out of the released sdist.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Forbidden member patterns (matched against each archive path, case-insensitive).
FORBIDDEN = [
    r"(^|/)\.env$",
    r"(^|/)\.env\.",
    r"(^|/)node_modules/",
    r"(^|/)dist/",
    r"(^|/)build/",
    r"(^|/)\.venv/",
    r"(^|/)__pycache__/",
    r"(^|/)\.aithernet_state/",
    r"\.db$",
    r"\.sqlite3?$",
    r"\.whl$",
    r"\.deb$",
    r"\.tar\.gz$",
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"credential",
    r"oauth",
    r"secret",
    r"(^|/)gr-mcp/",
    r"(^|/)marconi/",
    r"(^|/)workspace/",
    r"(^|/)identity/",
]

# Files/dirs that MUST remain in the sdist (needed to build/install).
REQUIRED = [
    "pyproject.toml",
    "README.md",
    "src/aithernet/__init__.py",
    "src/aithernet/state/migrations.py",
    "services/control_plane/cli.py",
    "packaging/README.md",
    "scripts/build_deb.sh",
    "scripts/install.sh",
    "deploy/systemd/aithernet-node.service",
]


def _build_sdist(out_dir: Path) -> Path:
    pytest.importorskip("build", reason="the `build` frontend is required to build the sdist")
    pytest.importorskip("hatchling", reason="hatchling backend required for --no-isolation build")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation",
         "--outdir", str(out_dir), str(REPO_ROOT)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"`python -m build --sdist` failed:\n{proc.stdout}\n{proc.stderr}"
    sdists = list(out_dir.glob("*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"
    return sdists[0]


def test_standard_build_sdist_is_clean(tmp_path) -> None:
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist, "r:gz") as tar:
        members = [m.name for m in tar.getmembers() if m.isfile()]
    # strip the leading "<name>-<version>/" prefix for pattern matching
    rel = [m.split("/", 1)[1] if "/" in m else m for m in members]

    leaks: list[str] = []
    for path in rel:
        # PKG-INFO and the license/readme are legitimate; only flag real forbidden members.
        for pat in FORBIDDEN:
            if re.search(pat, path, re.IGNORECASE):
                leaks.append(f"{path}  (matched /{pat}/)")
                break
    assert not leaks, "forbidden members leaked into the sdist:\n" + "\n".join(sorted(leaks))

    for required in REQUIRED:
        assert any(r == required for r in rel), f"required file missing from sdist: {required}"

    # Sanity bound: a clean python sdist is well under 5 MB (the polluted one was ~38 MB).
    assert sdist.stat().st_size < 5 * 1024 * 1024, (
        f"sdist unexpectedly large ({sdist.stat().st_size} bytes) — frontend deps may have leaked"
    )
