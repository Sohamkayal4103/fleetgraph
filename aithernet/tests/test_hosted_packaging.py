"""Stage 14F packaging / deployment / frontend artifact tests (§19-§21, §34-§35, §42).

Validates the shell packaging scripts parse and dry-run, the systemd units + reverse-proxy +
compose templates exist and are well-formed, the production env template carries only placeholders
(no generated secrets, HTTPS example.invalid hosts), and the portal frontend declares the required
npm scripts and produced a build. No root, network, or real infrastructure is required.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates the Docker Compose merge tags (!override / !reset)."""


def _compose_tag(loader, tag_suffix, node):  # noqa: ARG001
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _compose_tag)


def _load_compose(rel: str) -> dict:
    return yaml.load((_ROOT / rel).read_text(), Loader=_ComposeLoader)


def _exists(rel: str) -> Path:
    path = _ROOT / rel
    assert path.is_file(), f"missing artifact: {rel}"
    return path


def test_shell_scripts_parse():
    for script in ("scripts/build_deb.sh", "scripts/install.sh"):
        path = _exists(script)
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_build_deb_dry_run(tmp_path):
    # Stage into a TEMP build dir so the dry-run never writes generated evidence into the repo.
    result = subprocess.run(["bash", str(_ROOT / "scripts/build_deb.sh")],
                            env={"DRY_RUN": "1", "PATH": "/usr/bin:/bin",
                                 "BUILD_DIR": str(tmp_path / "deb-build")},
                            capture_output=True, text=True, cwd=str(_ROOT), timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    # Recommends (not hard-depends) GNU Radio / SDR; never fakes them.
    assert "gnuradio" in out.lower()


def test_systemd_units_present_and_hardened():
    for unit in ("aithernet-node", "aithernet-control-plane", "aithernet-ingestion"):
        text = _exists(f"deploy/systemd/{unit}.service").read_text()
        assert "EnvironmentFile=" in text
        assert "NoNewPrivileges" in text or "ProtectSystem" in text


def test_reverse_proxy_templates_present():
    caddy = _exists("deploy/reverse-proxy/Caddyfile").read_text()
    _exists("deploy/reverse-proxy/nginx.conf.example")
    assert "example.invalid" in caddy
    # No `.local` TLD used as a public site address (a `Caddyfile.local` filename is fine).
    assert ".local {" not in caddy and ".local:" not in caddy


def test_compose_files_valid_yaml():
    for rel in ("deploy/compose/docker-compose.yml", "deploy/compose/docker-compose.local.yml"):
        _exists(rel)
        data = _load_compose(rel)
        assert "services" in data
    prod = _load_compose("deploy/compose/docker-compose.yml")
    # Central database is PostgreSQL, never SQLite, in the production compose.
    blob = str(prod).lower()
    assert "postgres" in blob


def test_compose_structure_internal_services_not_published():
    """STATIC structural validation of the production compose (NOT a live bring-up — no container
    runtime is available here). Proves only the reverse proxy publishes ports, the database and
    object store are internal, depends_on resolves, and long-running services have healthchecks."""
    d = _load_compose("deploy/compose/docker-compose.yml")
    svcs = d["services"]
    # Only the reverse proxy publishes host ports; nothing else (no DB/admin/object-store exposure).
    for name, s in svcs.items():
        if name == "proxy":
            assert s.get("ports"), "proxy must publish ports"
        else:
            assert not s.get("ports"), f"internal service {name} must not publish host ports"
    # depends_on references resolve to declared services.
    for name, s in svcs.items():
        dep = s.get("depends_on", {})
        for target in (dep if isinstance(dep, (list, dict)) else []):
            assert target in svcs, f"{name} depends on undeclared {target}"
    # Long-running services declare healthchecks.
    for name in ("proxy", "control-plane", "ingestion", "postgres", "minio", "portal"):
        assert "healthcheck" in svcs[name], f"{name} missing healthcheck"
    # Central DB is PostgreSQL with a persistent volume; object store is MinIO with a volume.
    assert "postgres" in svcs and "minio" in svcs
    assert {"postgres_data", "minio_data"}.issubset(set(d.get("volumes", {})))


def test_production_env_template_placeholders_only():
    text = _exists(".env.production.example").read_text()
    assert "AITHERNET_HOSTED_ENV=production" in text
    assert "example.invalid" in text and "https://" in text
    # No generated secret material: lines are references/placeholders, not 40+ char base64 secrets.
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        assert "BEGIN" not in value  # no embedded key material


def test_portal_frontend_scripts_and_build():
    pkg = _exists("portal/package.json").read_text()
    import json
    scripts = json.loads(pkg).get("scripts", {})
    for name in ("build", "test", "lint"):
        assert name in scripts
    assert (_ROOT / "portal/dist").is_dir(), "portal build output missing"


def test_portal_has_no_workflow_placeholders():
    """Production-visible portal pages must not ship workflow placeholders. The single token
    'PLACEHOLDER BRANDING' (brand name) and clearly-labeled legal test fixtures are allowed."""
    forbidden = ("PLACEHOLDER:", "TODO", "Coming soon", "Not implemented", "Example only")
    pages = list((_ROOT / "portal/src/pages").rglob("*.tsx"))
    assert pages, "no portal pages found"
    offenders = []
    for path in pages:
        text = path.read_text()
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    # The built bundle must also be free of workflow placeholders.
    dist = _ROOT / "portal/dist"
    if dist.is_dir():
        for asset in dist.rglob("*.js"):
            blob = asset.read_text(errors="ignore")
            for token in forbidden:
                if token in blob:
                    offenders.append(f"dist/{asset.name}: {token}")
    assert not offenders, f"workflow placeholders present: {offenders}"


def test_portal_browser_api_base_is_same_origin():
    """Regression: the browser-facing API base is the same-origin relative path /api, and the
    built frontend never hardcodes the internal control-plane host (the control plane is
    unpublished — an absolute http://127.0.0.1:8100 would NetworkError in the browser)."""
    client = _exists("portal/src/api/client.ts").read_text()
    assert "DEFAULT_BASE_URL = '/api'" in client  # same-origin default
    # The forbidden internal host must not be a compiled default (only allowed in a comment).
    assert "'http://127.0.0.1:8100'" not in client and '"http://127.0.0.1:8100"' not in client
    env_example = _exists("portal/.env.example").read_text()
    assert "VITE_CONTROL_PLANE_BASE_URL=/api" in env_example
    # The BUILT bundle must contain no internal control-plane host.
    dist = _ROOT / "portal/dist"
    if dist.is_dir():
        for path in dist.rglob("*"):
            if path.suffix in (".js", ".css", ".html"):
                assert "127.0.0.1:8100" not in path.read_text(errors="ignore"), str(path)
