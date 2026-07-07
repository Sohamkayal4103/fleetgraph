"""Aithernet — autonomous SDR node runtime.

Stage 1 provides the deterministic node foundation: configuration, a SQLite-backed
state store, mission and event persistence, a runtime event bus, a FastAPI service,
and a Typer CLI. Coordinator/coding agents, GNU Radio MCP access, peer communication,
and the web dashboard are defined as future integration points and are not active yet.
"""

def _resolve_version() -> str:
    """Single source of truth for the runtime version: the installed package metadata.

    Falls back to the pyproject value only when the distribution is not installed (e.g. a
    bare source tree without an editable/regular install). Everything that reports a
    ``software_version`` MUST import this so a customer's installed ``.deb``/wheel reports
    its real version rather than a hardcoded string.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("aithernet")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 — importlib.metadata should always exist on 3.8+
        pass
    return "1.0.0-beta.9"


__version__ = _resolve_version()
