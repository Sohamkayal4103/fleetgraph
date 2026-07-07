"""ASGI entrypoint.

Exposes a module-level ``app`` so the node can be served directly with, e.g.::

    uvicorn aithernet.main:app --host 127.0.0.1 --port 8080

Configuration is resolved from ``AITHERNET_CONFIG`` (or ``configs/node.yaml``) and the
environment. The CLI ``aithernet start`` command is the preferred entrypoint; it reads
the same configuration and binds host/port from it.
"""

from __future__ import annotations

from fastapi import FastAPI

from aithernet.api.app import create_app


def build_app() -> FastAPI:
    """Build the application from resolved configuration."""
    return create_app()


app = build_app()
