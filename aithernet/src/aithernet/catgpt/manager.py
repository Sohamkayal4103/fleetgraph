"""Lifecycle manager for the Aithernet-managed CatGPT Gateway sidecar (beta.5, Part B/C).

Renders an Aithernet-authored ``docker-compose.yml`` + a 0600 ``gateway.env``, manages the
container through an injectable Docker runner, binds the OpenAI-compatible API to loopback by
default, and stores the local bearer token via Aithernet's managed secret store (as
``CATGPT_GATEWAY_API_KEY``) — the coordinator provider then references it by name, never by value.

Never stores the browser session/cookies or the user's web credentials; the browser profile lives
in a user-owned Docker volume, and the user logs in themselves via noVNC. The bearer token and VNC
password are never returned by status/logs or written to any log.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from aithernet.agents import providers as _providers

# ── defaults ────────────────────────────────────────────────────────────────────
DEFAULT_API_HOST = "127.0.0.1"        # Aithernet contract: bind the API to loopback by default
DEFAULT_API_PORT = 8000
DEFAULT_NOVNC_PORT = 6080
DEFAULT_PROVIDER = "chatgpt"          # or "claude"
PROJECT_NAME = "aithernet-catgpt"     # isolates from a hand-run public `catgpt` project
CONTAINER_NAME = "aithernet-catgpt"
API_KEY_REF = "CATGPT_GATEWAY_API_KEY"
VALID_PROVIDERS = ("chatgpt", "claude")

#: The upstream component this wrapper drives, pinned for reproducibility (see NOTICE / docs).
UPSTREAM_REPO = "https://github.com/GautamVhavle/CatGPT-Gateway"
UPSTREAM_LICENSE = "MIT"
UPSTREAM_COMMIT = "79a1b69d429fa9951d796289b60470fb595cbb42"

# beta.5: Aithernet ships the CatGPT Gateway runtime as a signed release image artifact so a fresh
# client never clones, builds, or pulls from a public repo. The image is tagged deterministically
# and shipped as a compressed `docker save` tar; the manager loads it locally (never Docker Hub).
GATEWAY_COMPONENT_VERSION = "1.0.0-beta.7"
IMAGE_REPO = "aithernet/catgpt-gateway"
DEFAULT_IMAGE = f"{IMAGE_REPO}:{GATEWAY_COMPONENT_VERSION}"
IMAGE_ARTIFACT = f"catgpt-gateway-{GATEWAY_COMPONENT_VERSION}-linux-amd64.docker.tar.zst"
#: An operator override pointing directly at the image tar (advanced/offline).
IMAGE_TAR_ENV = "AITHERNET_CATGPT_IMAGE_TAR"

MISSING_IMAGE_MSG = (
    "Managed CatGPT Gateway image is missing from the release bundle. "
    "Re-download the complete portal bundle or run `aithernet components install catgpt-gateway`."
)


def state_dir(state_root: Path | None = None) -> Path:
    """``<state_root>/catgpt`` — where the managed compose/env/config live (0700)."""
    return _providers._state_root(state_root) / "catgpt"


@dataclass
class RunResult:
    """The outcome of an injectable Docker command (safe to surface; no secrets)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerRunner:
    """Default runner: shells out to ``docker compose`` (never with ``sudo``). Injectable so tests
    substitute a fake process/docker layer."""

    def __init__(self, *, binary: str = "docker") -> None:
        self.binary = binary

    def available(self) -> bool:
        """True if the docker CLI is installed (on PATH)."""
        return shutil.which(self.binary) is not None

    def accessible(self) -> bool:
        """True if the docker daemon is reachable AND this user may talk to it (``docker ps``)."""
        if not self.available():
            return False
        return self.run(["ps", "--format", "{{.Names}}"], timeout=15).ok

    def run(self, args: list[str], *, timeout: float = 120.0) -> RunResult:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, no shell, args are ours
                [self.binary, *args],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except FileNotFoundError:
            return RunResult(127, "", f"{self.binary} not found on PATH")
        except subprocess.TimeoutExpired:
            return RunResult(124, "", f"{self.binary} {' '.join(args)} timed out")
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def image_present(self, tag: str, *, timeout: float = 20.0) -> bool:
        """True if ``tag`` is already loaded locally (``docker image inspect``)."""
        return self.run(["image", "inspect", tag], timeout=timeout).ok

    def load_image_tar(self, tar: Path, *, timeout: float = 900.0) -> RunResult:
        """Load a ``docker save`` tar into the local daemon. Supports zstd (``.tar.zst``/``.zst``)
        by decompressing to a pipe, and plain ``.tar``. NEVER pulls from a registry."""
        tar = Path(tar)
        name = tar.name.lower()
        if name.endswith(".zst"):
            zstd = shutil.which("zstd")
            if zstd is None:
                return RunResult(127, "", "zstd not found on PATH (needed to decompress the image)")
            try:
                dec = subprocess.Popen(  # noqa: S603 - fixed binaries, no shell
                    [zstd, "-dc", str(tar)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                proc = subprocess.run(  # noqa: S603
                    [self.binary, "load"], stdin=dec.stdout,
                    capture_output=True, text=True, timeout=timeout, check=False)
                if dec.stdout:
                    dec.stdout.close()
                dec.wait(timeout=timeout)
                err = proc.stderr or ""
                if dec.returncode not in (0, None):
                    err = (err + " " + (dec.stderr.read().decode("utf-8", "replace")
                                        if dec.stderr else "")).strip()
                return RunResult(proc.returncode, proc.stdout or "", err)
            except FileNotFoundError as exc:
                return RunResult(127, "", str(exc))
            except subprocess.TimeoutExpired:
                return RunResult(124, "", "docker load timed out")
        return self.run(["load", "-i", str(tar)], timeout=timeout)


@dataclass
class GatewayConfig:
    """Aithernet-managed, NON-secret gateway settings (persisted to ``config.json``)."""

    provider: str = DEFAULT_PROVIDER
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    novnc_port: int = DEFAULT_NOVNC_PORT
    # image: versioned Aithernet tag (never plain/unqualified). image_artifact: the shipped
    # `docker save` tar to load from. source_dir: advanced — BUILD from a pinned checkout instead.
    image: str = DEFAULT_IMAGE
    image_artifact: str = IMAGE_ARTIFACT
    source_dir: str | None = None
    project_name: str = PROJECT_NAME
    api_key_ref: str = API_KEY_REF
    managed: bool = True

    @property
    def base_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}/v1"

    @property
    def novnc_url(self) -> str:
        return f"http://{self.api_host}:{self.novnc_port}"

    @property
    def novnc_open_url(self) -> str:
        """The direct noVNC client URL that auto-connects (avoids the bare directory listing)."""
        return f"http://{self.api_host}:{self.novnc_port}/vnc.html?autoconnect=1"


class CatGptManagerError(RuntimeError):
    """A safe, actionable manager error (message never contains a secret)."""


_ENV_VALUE = re.compile(r"^[\x20-\x7e]+$")  # printable ASCII, no newlines


class CatGptGatewayManager:
    """Set up / start / status / logs / stop the Aithernet-managed CatGPT Gateway sidecar."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        runner: DockerRunner | None = None,
        secret_env_file: Path | None = None,
    ) -> None:
        self._state_root = state_root
        self.dir = state_dir(state_root)
        self.runner = runner or DockerRunner()
        self._secret_env_file = secret_env_file

    # -- paths --------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

    @property
    def compose_path(self) -> Path:
        return self.dir / "docker-compose.yml"

    @property
    def env_path(self) -> Path:
        return self.dir / "gateway.env"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "docker-logs"

    def is_configured(self) -> bool:
        return self.config_path.is_file() and self.compose_path.is_file()

    # -- shipped runtime image (Option A: no clone/build/pull/docker-login) --------
    def image_tag(self) -> str:
        try:
            return self.load_config().image
        except CatGptManagerError:
            return DEFAULT_IMAGE

    def image_present(self) -> bool:
        """True if the versioned Aithernet gateway image is already loaded locally."""
        return self.runner.available() and self.runner.image_present(self.image_tag())

    def _image_search_dirs(self) -> list[Path]:
        """Where a shipped/downloaded image tar may live (most specific first)."""
        root = _providers._state_root(self._state_root)
        dirs = [
            self.dir,                                     # the managed state dir
            root / "components" / "catgpt-gateway",       # managed component cache
            root / "downloads",                           # portal-downloaded bundle
            root / f"lan-release-{GATEWAY_COMPONENT_VERSION}" / "archive",  # local release bundle
            Path("/opt/aithernet/share/catgpt-gateway"),  # shipped alongside the .deb
            Path("/opt/aithernet/components/catgpt-gateway"),
            Path.cwd(),                                   # current release bundle dir
        ]
        env_dir = os.environ.get("AITHERNET_CATGPT_BUNDLE_DIR")
        if env_dir:
            dirs.insert(0, Path(env_dir))
        return dirs

    def locate_image_tar(self) -> Path | None:
        """Find the shipped image tar (env override first, then the known bundle locations)."""
        override = os.environ.get(IMAGE_TAR_ENV)
        if override and Path(override).is_file():
            return Path(override)
        names = [IMAGE_ARTIFACT, IMAGE_ARTIFACT.removesuffix(".zst"),
                 "catgpt-gateway.docker.tar.zst", "catgpt-gateway.docker.tar"]
        seen: set[str] = set()
        for d in self._image_search_dirs():
            key = str(d)
            if key in seen:
                continue
            seen.add(key)
            for name in names:
                cand = d / name
                if cand.is_file():
                    return cand
        return None

    def ensure_image(self, *, load_timeout: float = 900.0) -> dict:
        """Guarantee the versioned image is loaded locally, WITHOUT any registry pull.

        Returns ``{present, source, tag, tar}``. ``source`` is ``local`` (already loaded),
        ``loaded_from_release_bundle`` (loaded from a shipped tar), or raises
        :class:`CatGptManagerError` with the actionable missing-image message.
        """
        tag = self.image_tag()
        if self.image_present():
            return {"present": True, "source": "local", "tag": tag, "tar": None}
        cfg = self.load_config()
        if cfg.source_dir:  # advanced BUILD mode: compose --build handles it, no tar needed
            return {"present": False, "source": "build", "tag": tag, "tar": None}
        tar = self.locate_image_tar()
        if tar is None:
            raise CatGptManagerError(MISSING_IMAGE_MSG)
        res = self.runner.load_image_tar(tar, timeout=load_timeout)
        if not res.ok:
            raise CatGptManagerError(
                f"failed to load the managed CatGPT Gateway image from {tar.name}: "
                f"{(res.stderr or res.stdout or 'docker load failed').strip()[:200]}")
        if not self.image_present():
            raise CatGptManagerError(
                f"loaded {tar.name} but the expected image tag {tag} is not present — the shipped "
                "tar may be for a different version")
        return {"present": True, "source": "loaded_from_release_bundle", "tag": tag,
                "tar": str(tar)}

    def image_status(self) -> dict:
        """Non-mutating snapshot of the shipped-image situation for status/setup output."""
        present = self.image_present()
        tar = None if present else self.locate_image_tar()
        if present:
            source = "local_cache"
        elif tar is not None:
            source = "release_bundle_available"
        else:
            source = "missing"
        return {"managed_image_present": present, "managed_image_source": source,
                "image_tag": self.image_tag(), "image_tar": (str(tar) if tar else None)}

    # -- config load/save ---------------------------------------------------------
    def load_config(self) -> GatewayConfig:
        if not self.config_path.is_file():
            raise CatGptManagerError(
                "CatGPT gateway is not set up yet. Run:  aithernet catgpt setup")
        raw = json.loads(self.config_path.read_text())
        known = {k: raw[k] for k in GatewayConfig().__dict__ if k in raw}
        return GatewayConfig(**known)

    def _save_config(self, cfg: GatewayConfig) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.config_path.write_text(json.dumps(asdict(cfg), indent=2, sort_keys=True) + "\n")

    # -- setup --------------------------------------------------------------------
    def setup(
        self,
        *,
        provider: str = DEFAULT_PROVIDER,
        api_host: str = DEFAULT_API_HOST,
        api_port: int = DEFAULT_API_PORT,
        novnc_port: int = DEFAULT_NOVNC_PORT,
        api_token: str | None = None,
        vnc_password: str | None = None,
        image: str = DEFAULT_IMAGE,
        source_dir: str | None = None,
        configure_coordinator: bool = True,
    ) -> dict:
        """Create the managed config/compose/env, store the bearer token via managed secrets, and
        (optionally) point the coordinator provider at the local gateway. Returns a secret-free
        summary. A token/password is GENERATED when not supplied."""
        if provider not in VALID_PROVIDERS:
            raise CatGptManagerError(
                f"provider must be one of {', '.join(VALID_PROVIDERS)} (got '{provider}')")
        token = api_token or secrets.token_urlsafe(24)
        vnc = vnc_password or secrets.token_urlsafe(9)
        for label, value in (("api token", token), ("vnc password", vnc)):
            if not _ENV_VALUE.match(value):
                raise CatGptManagerError(f"{label} must be printable ASCII on a single line")

        if source_dir:
            src = Path(source_dir).expanduser()
            if not (src / "Dockerfile").is_file():
                raise CatGptManagerError(
                    f"--source-dir {src} does not contain a Dockerfile (expected a CatGPT-Gateway "
                    "checkout to build the pinned component from)")
            source_dir = str(src.resolve())

        cfg = GatewayConfig(
            provider=provider, api_host=api_host, api_port=int(api_port),
            novnc_port=int(novnc_port), image=image, source_dir=source_dir,
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._save_config(cfg)
        self._write_env(cfg, token=token, vnc_password=vnc)
        self._write_compose(cfg)

        # Store the local bearer token in Aithernet's managed secret store (0600). The coordinator
        # provider references it by NAME (api_key_ref), never by value.
        _providers.set_secret(API_KEY_REF, token, env_file=self._secret_env_file)

        coordinator_configured = False
        if configure_coordinator:
            _providers.configure(
                "coordinator", "catgpt_gateway",
                base_url=cfg.base_url, api_key_ref=API_KEY_REF,
                state_root=self._state_root,
            )
            coordinator_configured = True

        img = self.image_status()
        summary = {
            "state_dir": str(self.dir),
            "provider": provider,
            "base_url": cfg.base_url,
            "novnc_url": cfg.novnc_url,
            "novnc_open_url": cfg.novnc_open_url,
            "api_host": api_host,
            "api_port": cfg.api_port,
            "novnc_port": cfg.novnc_port,
            "api_key_ref": API_KEY_REF,
            "token_stored": True,          # NEVER the token itself
            "vnc_password_generated": vnc_password is None,
            "source_mode": "build (source-dir)" if source_dir else "bundled image",
            "image": cfg.image,
            "image_artifact": None if source_dir else cfg.image_artifact,
            "image_loaded": img["managed_image_present"],
            "managed_image_source": img["managed_image_source"],
            "source_dir": source_dir,
            "coordinator_configured": coordinator_configured,
            "loopback_only": api_host in ("127.0.0.1", "localhost", "::1"),
            "docker_installed": self.runner.available(),
            "docker_accessible": self.runner.accessible(),
        }
        return summary

    # -- env + compose rendering --------------------------------------------------
    def _write_env(self, cfg: GatewayConfig, *, token: str, vnc_password: str) -> None:
        """Write the gateway's own env file (0600). Holds API_TOKEN/VNC_PASSWORD because the
        CONTAINER needs them; the file is user-owned and never logged/echoed by Aithernet."""
        # NOTE: the container always listens on 8000/6080 INSIDE the container; the host-side bind
        # (loopback + chosen port) is done by the compose port mapping, so API_HOST stays 0.0.0.0
        # inside the container namespace (safe: only the loopback host port is published).
        lines = [
            "# Aithernet-managed CatGPT Gateway environment (0600). Do not commit. Not logged.",
            f"PROVIDER={cfg.provider}",
            "API_HOST=0.0.0.0",
            "API_PORT=8000",
            f"API_TOKEN={token}",
            f"VNC_PASSWORD={vnc_password}",
            "HEADLESS=false",
            "CHATGPT_URL=https://chatgpt.com",
            "CLAUDE_URL=https://claude.ai",
            "RESPONSE_TIMEOUT=120000",
            "SELECTOR_TIMEOUT=10000",
            "LOG_LEVEL=INFO",
            "VERBOSE=false",
        ]
        tmp = self.env_path.with_suffix(".env.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, self.env_path)
        os.chmod(self.env_path, 0o600)

    def _write_compose(self, cfg: GatewayConfig) -> None:
        """Render the Aithernet-authored compose. Ports bind to the chosen host (loopback default);
        the browser profile is a user-owned named volume; logs bind-mount to the state dir."""
        if cfg.source_dir:
            build_or_image = (
                f"    build:\n"
                f"      context: {cfg.source_dir}\n"
                f"      dockerfile: Dockerfile\n"
                f"    image: {cfg.image}\n"
            )
        else:
            # Bundled-image mode: the versioned Aithernet image is loaded locally by the manager
            # before `up`. pull_policy: never guarantees compose never reaches Docker Hub for the
            # unqualified/plain name (the fresh-client pull-access failure this fixes).
            build_or_image = f"    image: {cfg.image}\n    pull_policy: never\n"
        compose = (
            "# Aithernet-managed CatGPT Gateway sidecar — generated by `aithernet catgpt setup`.\n"
            "# Regenerate via `aithernet catgpt setup`; manual edits may be overwritten.\n"
            f"# Upstream: {UPSTREAM_REPO} ({UPSTREAM_LICENSE}) — pinned local component.\n"
            "services:\n"
            "  catgpt:\n"
            f"    container_name: {CONTAINER_NAME}\n"
            + build_or_image +
            "    restart: unless-stopped\n"
            "    env_file:\n"
            f"      - {self.env_path}\n"
            "    ports:\n"
            f"      - \"{cfg.api_host}:{cfg.api_port}:8000\"\n"
            f"      - \"{cfg.api_host}:{cfg.novnc_port}:6080\"\n"
            "    volumes:\n"
            "      - catgpt_browser_data:/app/browser_data\n"
            f"      - {self.logs_dir}:/app/logs\n"
            "    security_opt:\n"
            "      - seccomp=unconfined\n"
            "    shm_size: \"2gb\"\n"
            "volumes:\n"
            "  catgpt_browser_data:\n"
            "    name: aithernet_catgpt_browser_data\n"
            "    driver: local\n"
        )
        self.compose_path.write_text(compose)

    # -- docker compose lifecycle -------------------------------------------------
    def _compose(self, *args: str, timeout: float = 120.0) -> RunResult:
        base = ["compose", "-p", PROJECT_NAME, "-f", str(self.compose_path)]
        return self.runner.run([*base, *args], timeout=timeout)

    def start(self, *, build: bool | None = None, timeout: float = 600.0) -> RunResult:
        """Start the managed gateway.

        In the default bundled-image mode the shipped image is loaded locally FIRST (never pulled
        from a registry); a missing image tar raises the actionable :data:`MISSING_IMAGE_MSG`. In
        the advanced source-dir mode, compose builds it.
        """
        cfg = self.load_config()
        do_build = cfg.source_dir is not None if build is None else build
        if not do_build:
            self.ensure_image(load_timeout=timeout)  # load shipped image; clear error if missing
        args = ["up", "-d"]
        if do_build:
            args.append("--build")
        return self._compose(*args, timeout=timeout)

    def stop(self, *, timeout: float = 120.0) -> RunResult:
        """`docker compose down` (keeps the browser-data volume so the login survives)."""
        return self._compose("down", timeout=timeout)

    def logs(self, *, tail: int = 200, timeout: float = 30.0) -> RunResult:
        """`docker compose logs` — bounded; the gateway is configured LOG_LEVEL=INFO (no token)."""
        return self._compose("logs", "--no-color", "--tail", str(int(tail)), timeout=timeout)

    def container_running(self, *, timeout: float = 20.0) -> bool:
        """True if the managed container is up (via ``compose ps``). Best-effort/tolerant."""
        res = self._compose("ps", "--status", "running", "--format", "{{.Name}}", timeout=timeout)
        if not res.ok:
            # older compose without --status: fall back to a plain ps
            res = self._compose("ps", "--format", "{{.Name}}", timeout=timeout)
        return CONTAINER_NAME in (res.stdout or "")

    # -- status (container state + contract probe) --------------------------------
    def status(self, *, probe: bool = True, timeout: float = 5.0, client=None) -> dict:
        """A secret-free status dict: managed flag, gateway_status, endpoint_reachable, model,
        credential_available, and the compose/config locations. ``client`` is for tests (an
        ``httpx.Client`` bound to a MockTransport)."""
        from aithernet.catgpt.contract import classify_gateway_status, verify_contract

        cfg = self.load_config()
        running = False
        if self.runner.available():
            running = self.container_running()

        credential_available = self._token_present()
        report = None
        if probe:
            token = self._token_value() if credential_available else None
            report = verify_contract(cfg.base_url, api_key=token, timeout=timeout, client=client)

        gw_status = classify_gateway_status(
            container_running=running, report=report,
        )
        img = self.image_status()
        out = {
            "managed_gateway": True,
            "gateway_status": gw_status,
            "provider": cfg.provider,
            "base_url": cfg.base_url,
            "novnc_url": cfg.novnc_url,
            "novnc_open_url": cfg.novnc_open_url,
            "docker_installed": self.runner.available(),
            "docker_accessible": self.runner.accessible(),
            "managed_image_present": img["managed_image_present"],
            "managed_image_source": img["managed_image_source"],
            "image": img["image_tag"],
            "image_artifact": cfg.image_artifact,
            "container_running": running,
            "credential_available": credential_available,
            "api_key_ref": cfg.api_key_ref,
            "endpoint_reachable": bool(report and report.endpoint_reachable),
            "login_required": gw_status == "login_required",
            "ready": gw_status == "ready",
            "model": (report.models[0] if report and report.models else None),
            "models": (report.models if report else []),
            "compose_path": str(self.compose_path),
            "docker_available": self.runner.available(),   # back-compat alias
            "loopback_only": cfg.api_host in ("127.0.0.1", "localhost", "::1"),
        }
        return out

    # -- docker runtime install (Ubuntu; executed by the CLI only after confirmation) ---------
    @staticmethod
    def docker_install_plan() -> list[list[str]]:
        """The Ubuntu commands to install + enable Docker and add the user to the docker group.

        Returned as a list of argv (never a shell string). The CLI runs these ONLY after explicit
        confirmation — Aithernet never silently installs Docker or runs sudo on its own.
        """
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"
        return [
            ["sudo", "apt", "update"],
            ["sudo", "apt", "install", "-y", "docker.io", "docker-compose-v2"],
            ["sudo", "systemctl", "enable", "--now", "docker"],
            ["sudo", "usermod", "-aG", "docker", user],
        ]

    # -- secret helpers (never return the value in status/logs) -------------------
    def _token_present(self) -> bool:
        env_file = self._secret_env_file or _providers.secret_env_file()
        if os.environ.get(API_KEY_REF):
            return True
        return API_KEY_REF in _providers.list_secret_names(env_file)

    def _token_value(self) -> str | None:
        return _providers.resolve_secret_value(API_KEY_REF, env_file=self._secret_env_file)

    # -- noVNC password read-back (local login password; NOT a web-account credential) ----------
    def read_vnc_password(self) -> str:
        """Return the local noVNC/VNC password from the 0600 ``gateway.env``.

        This is the password noVNC may prompt for to view the local login browser — it is NOT the
        ChatGPT/Claude password and NOT the API bearer token. Used ONLY by ``catgpt vnc-password``;
        the value is never logged or included in ``status``/``setup`` output.
        """
        if not self.env_path.is_file():
            raise CatGptManagerError(
                "CatGPT gateway is not set up yet. Run:  aithernet catgpt setup")
        for ln in self.env_path.read_text().splitlines():
            if ln.startswith("VNC_PASSWORD="):
                return ln.split("=", 1)[1].strip()
        raise CatGptManagerError(
            "no VNC password found in gateway.env — re-run `aithernet catgpt setup`")
