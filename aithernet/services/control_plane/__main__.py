"""Runnable entrypoint for the hosted control plane (Stage 14F).

    python -m services.control_plane

Configuration comes from the environment (see ``HostedConfig.from_env``). This process is
independent of the local node — it never imports or starts the node runtime.
"""

from __future__ import annotations

import uvicorn

from services.control_plane.app import create_app
from services.control_plane.config import HostedConfig


def main() -> None:
    config = HostedConfig.from_env()
    # Fail closed in production: refuse to start on placeholder/missing required
    # values, loopback public URLs, insecure cookies, SQLite, or the dev email sink.
    if config.is_production:
        config.require_valid()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


if __name__ == "__main__":
    main()
