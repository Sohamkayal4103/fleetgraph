"""beta.7 -> beta.8 state migration / repair (single helper, not a second repair subsystem).

beta.7 left node state split: setup created the node identity under the *data* root
(``~/.local/share/aithernet/identity``) while the running service used an *XDG state* identity dir
(``~/.local/state/aithernet/identity``) plus a placeholder node id and a cwd database. This module
detects that split and binds the ONE canonical ``node.yaml`` (see
:func:`aithernet.provisioning.wizard.write_canonical_config`) to the real, setup-created identity —
idempotently, safe to rerun, with an auditable record. It NEVER reads, moves-without-backup, or
prints private key material; it reasons only over the public ``identity.json`` fingerprint.

It is invoked from the existing setup repair/resume flow (``aithernet setup --repair`` /
``--migrate``), not as a standalone repair tool.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from aithernet.ops.paths import NodePaths
from aithernet.provisioning import state as sstate


class AmbiguousIdentityError(Exception):
    """Two DIFFERENT private identities were found; refuse to choose one silently."""


_IDENTITY_DOC = "identity.json"
_PRIVATE_KEY = "node_ed25519_private.pem"


def _public_fingerprint(identity_dir: Path) -> str | None:
    """The public fingerprint recorded in ``identity.json`` (public-safe), or None if absent.
    Never reads the private key."""
    doc = identity_dir / _IDENTITY_DOC
    if not doc.is_file():
        return None
    try:
        return str(json.loads(doc.read_text()).get("fingerprint") or "") or None
    except (ValueError, OSError):
        return None


def _xdg_state_identity_dir() -> Path:
    from aithernet.transport.identity import default_identity_dir
    return default_identity_dir()


def discover_identity_locations(state_root: Path) -> dict[str, str]:
    """Map each candidate identity directory that holds a real key to its public fingerprint.

    Candidates: the canonical ``<state_root>/identity`` (where setup writes) and the legacy XDG
    state dir the beta.7 runtime used. Only directories with BOTH an identity.json and a private
    key file count as a real identity."""
    paths = NodePaths.from_root(state_root)
    candidates = {paths.identity_dir, _xdg_state_identity_dir()}
    found: dict[str, str] = {}
    for d in candidates:
        if (d / _PRIVATE_KEY).is_file():
            fp = _public_fingerprint(d)
            if fp:
                found[str(d)] = fp
    return found


def resolve_canonical_identity(state_root: Path) -> tuple[Path | None, str | None]:
    """Resolve the single real identity. Returns ``(dir, fingerprint)`` or ``(None, None)`` when
    no identity exists. Raises :class:`AmbiguousIdentityError` if two DIFFERENT private identities
    are present (the operator must resolve it; we never pick silently)."""
    locations = discover_identity_locations(state_root)
    if not locations:
        return None, None
    distinct = set(locations.values())
    if len(distinct) > 1:
        from aithernet.transport.identity import short_fingerprint
        summary = ", ".join(f"{d} ({short_fingerprint(fp)})" for d, fp in sorted(locations.items()))
        raise AmbiguousIdentityError(
            "conflicting node identities found; refusing to choose silently: " + summary)
    paths = NodePaths.from_root(state_root)
    # Prefer the canonical location when it holds the (single) identity; else the legacy one.
    if str(paths.identity_dir) in locations:
        return paths.identity_dir, locations[str(paths.identity_dir)]
    only_dir = next(iter(locations))
    return Path(only_dir), locations[only_dir]


def _next_record_path(paths: NodePaths) -> Path:
    mig = paths.root / "setup" / "migrations"
    mig.mkdir(parents=True, exist_ok=True)
    n = len(list(mig.glob("migration-*.json"))) + 1
    return mig / f"migration-{n:03d}.json"


def migrate_beta7_state(state_root: Path | None = None, *, dry_run: bool = False) -> dict:
    """Detect + repair the beta.7 split, idempotently, binding the canonical config to the real
    identity. Returns an auditable record dict (also written under ``<root>/setup/migrations`` when
    not a dry run). Never exposes private key contents."""
    from aithernet.agents import providers as prov
    from aithernet.transport.identity import short_fingerprint

    root = state_root or sstate.default_state_root()
    paths = NodePaths.from_root(root)
    record: dict = {"state_root": str(paths.root), "dry_run": dry_run, "actions": [],
                    "detected": {}, "result": {}}

    locations = discover_identity_locations(paths.root)
    record["detected"]["identity_locations"] = {
        d: short_fingerprint(fp) for d, fp in locations.items()}

    identity_dir, fingerprint = resolve_canonical_identity(paths.root)
    record["detected"]["canonical_identity"] = (
        {"dir": str(identity_dir), "fingerprint": short_fingerprint(fingerprint)}
        if identity_dir else None)

    st = sstate.load_state(paths.root) or sstate.SetupState()
    # Recover node id/name: setup state first, else the public identity doc.
    node_name = st.node_name or ""
    node_id = st.node_id or ""
    if identity_dir is not None:
        try:
            doc = json.loads((identity_dir / _IDENTITY_DOC).read_text())
            node_name = node_name or str(doc.get("node_name") or "")
            node_id = node_id or str(doc.get("node_id") or "")
        except (ValueError, OSError):
            pass
    node_name = node_name or "aithernet-node"

    # If the real identity lives in the legacy XDG dir, MOVE it into the canonical dir (preserve the
    # private key; back up nothing-to-overwrite). Never duplicate the private key into two places.
    if identity_dir is not None and identity_dir != paths.identity_dir:
        record["actions"].append(f"relocate identity {identity_dir} -> {paths.identity_dir}")
        if not dry_run:
            paths.identity_dir.parent.mkdir(parents=True, exist_ok=True)
            if paths.identity_dir.exists() and not any(paths.identity_dir.iterdir()):
                paths.identity_dir.rmdir()
            shutil.move(str(identity_dir), str(paths.identity_dir))
        identity_dir = paths.identity_dir

    # Back up any existing node.yaml before rewriting it.
    cfg_path = prov.node_config_path(paths.root)
    if cfg_path.is_file():
        backup = _backup_config(cfg_path, paths, dry_run)
        record["actions"].append(f"backup config -> {backup}")

    # Bind the canonical config to the resolved identity + absolute paths (idempotent).
    if not dry_run:
        st.node_name = node_name
        st.node_id = node_id  # keep existing id if present; else write_canonical_config seeds one
        sstate.save_state(st, paths.root)
        from aithernet.provisioning.wizard import SetupContext, write_canonical_config
        ctx = SetupContext(st, state_root=paths.root)
        info = write_canonical_config(ctx)
        record["result"]["config"] = info["config"]
        record["result"]["node_id"] = info["node_id"]
        record["result"]["identity_dir"] = info["identity_dir"]
    record["actions"].append("bind canonical node.yaml (node id + absolute db/identity/workspace)")

    # Rewrite the user service unit so it pins the canonical config (only if a user unit exists).
    from aithernet.provisioning import services
    if services.installed_scope() == "user" or st.service_mode == services.USER:
        record["actions"].append("reinstall user unit pinned to canonical config")
        record["actions"].append("persist managed runtime PATH for CLI providers")
        if not dry_run:
            services.install_user_unit(state_root=paths.root)
            # Defect 2: carry forward CLI-provider runtime PATH so providers run under the
            # repaired service without a manual drop-in.
            try:
                from aithernet.config.loader import load_config
                services.write_runtime_env(load_config(str(paths.config_file)))
            except Exception:  # noqa: BLE001 — PATH persistence is best-effort during migration
                pass

    # Verify the resulting runtime identity + database (never expose the private key).
    if not dry_run:
        record["result"]["verified"] = _verify(paths, fingerprint)
        out = _next_record_path(paths)
        out.write_text(json.dumps(record, indent=2, sort_keys=True))
        record["record_file"] = str(out)
    return record


def _backup_config(cfg_path: Path, paths: NodePaths, dry_run: bool) -> Path:
    mig = paths.root / "setup" / "migrations"
    n = len(list(mig.glob("node.yaml.*.bak"))) + 1 if mig.exists() else 1
    backup = mig / f"node.yaml.{n:03d}.bak"
    if not dry_run:
        mig.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg_path, backup)
    return backup


def _verify(paths: NodePaths, expected_fp: str | None) -> dict:
    """Confirm the canonical config now resolves the real identity + an absolute database. Reads
    only the public identity document."""
    from aithernet.config.loader import load_config
    cfg = load_config(paths.config_file)
    ident_dir = Path(cfg.agent_transport.identity.state_directory or paths.identity_dir)
    actual_fp = _public_fingerprint(ident_dir)
    placeholder = "00000000-0000-4000-8000-000000000001"
    return {
        "node_id_real": bool(cfg.node_id) and cfg.node_id != placeholder,
        "identity_matches": (expected_fp is None) or (actual_fp == expected_fp),
        "database_absolute": cfg.database_url.startswith("sqlite:////"),
        "identity_dir": str(ident_dir),
    }
